"""Main PyQt6 window: Search / Downloads / Convert tabs."""

from __future__ import annotations

import threading
from functools import partial
from pathlib import Path
from typing import Literal

import httpx
from PyQt6.QtCore import QObject, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractScrollArea,
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStyle,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from mhi2_video_finder import __version__
from mhi2_video_finder.build_info import build_date_display
from mhi2_video_finder.config import Settings, default_config_path, load_settings, save_settings
from mhi2_video_finder.local_library import LibraryFileRow
from mhi2_video_finder.paste_urls import parse_pasted_video_urls
from mhi2_video_finder.search import VideoCandidate
from mhi2_video_finder.workflow import (
    ensure_output_dir,
    infer_subdir_name,
    remote_save_out_path,
    safe_stem,
    slug_dir_name,
    unique_out_path,
)

from .backends.remote import RemoteJobController
from .job_store import JobStore
from .models import UiJob
from .styles import (
    DIAGNOSTICS_IDLE_STYLE,
    DIAGNOSTICS_OFFLINE_STYLE,
    DIAGNOSTICS_ONLINE_STYLE,
    HINT_LABEL_STYLE,
)
from .window_geometry import WindowGeometryMixin
from .workers import (
    BulkUrlResolveWorker,
    ConvertService,
    DownloadService,
    GroqInferWorker,
    LibraryBulkClearMetadataWorker,
    LibraryBulkRenameToSongWorker,
    LibraryProbeWorker,
    LibrarySaveWorker,
    LibraryScanWorker,
    SearchWorker,
    new_job_id,
)

# Stored in QComboBox userData; must match config ``video_encoder`` values.
_ENCODER_CHOICES: tuple[tuple[str, str], ...] = (
    ("libx264 (CPU — best for picky car USB / MHI2)", "libx264"),
    ("h264_vaapi (Intel / AMD GPU)", "h264_vaapi"),
    ("h264_nvenc (NVIDIA GPU)", "h264_nvenc"),
)

# Remote-download target subfolder for jobs imported from the daemon (Telegram / API), not from this UI queue.
_REMOTE_DAEMON_IMPORT_SUBDIR = "daemon-imports"
# Poll daemon job list so Telegram/API-created jobs appear without restarting the UI.
_REMOTE_JOBS_POLL_MS = 10_000
# Poll daemon diagnostics (uptime, build info, job counts) for the Daemon Diagnostics tab.
_DIAGNOSTICS_POLL_MS = 15_000
_LIB_COL_CHECK = 0
_LIB_COL_FILE = 1
_LIB_COL_ARTIST = 2
_LIB_COL_SONG = 3
_LIB_COL_GUESS_STATUS = 4
# Table rows inserted per event-loop slice so Find-all-videos stays responsive.
_LIBRARY_TABLE_FILL_BATCH_ROWS = 100
# Concurrent Groq requests while earlier rows may still be saving (ffmpeg).
_MAX_PARALLEL_LIBRARY_GROQ = 3
# Concurrent ffprobe reads while loading Library artist/title columns.
_MAX_PARALLEL_LIBRARY_PREFETCH_PROBES = 4


def _display_title_for_remote_row(raw_title: str) -> str:
    t = raw_title.strip()
    return t if t else "(no title)"


class _RemoteFetchBridge(QObject):
    ok = pyqtSignal(str)
    fail = pyqtSignal(str, str)
    progress = pyqtSignal(str, int, int)


class MainWindow(QMainWindow, WindowGeometryMixin):
    def __init__(self, *, config_path: Path | None = None) -> None:
        super().__init__()
        self.setWindowTitle(f"mhi2-video-finder {__version__}")
        self._work_area_screen_hooked = False
        self._apply_initial_window_geometry()

        self._config_path = config_path
        self._settings: Settings = load_settings(config_path)
        self._results: list[VideoCandidate] = []
        self._jobs: dict[str, UiJob] = {}
        self._job_order: list[str] = []

        self._search_worker: SearchWorker | None = None
        self._search_seq: int = 0
        self._bulk_resolve_worker: BulkUrlResolveWorker | None = None
        self._bulk_resolve_seq: int = 0
        self._tray: QSystemTrayIcon | None = None
        if QSystemTrayIcon.isSystemTrayAvailable():
            self._tray = QSystemTrayIcon(self)
            self._tray.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogDetailedView))
            self._tray.setToolTip("mhi2-video-finder")
            self._tray.show()

        self._remote: RemoteJobController | None = None
        self._use_remote = self._settings.processing_backend.strip().lower() == "remote"
        self._initial_remote = self._use_remote
        self._fetch_bridge = _RemoteFetchBridge(self)
        self._fetch_bridge.ok.connect(self._on_remote_fetch_ok)
        self._fetch_bridge.fail.connect(self._on_remote_fetch_fail)
        self._fetch_bridge.progress.connect(self._on_remote_fetch_progress)

        if self._use_remote:
            self._remote = RemoteJobController(lambda: self._settings)
            self._remote.connection_error.connect(self._on_remote_connection_error)
            self._remote.remote_registered.connect(self._on_remote_registered)
            self._remote.remote_missing.connect(self._on_remote_missing)
            self._remote.recent_jobs_ready.connect(self._on_remote_daemon_jobs_imported)
            self._remote.remote_meta_updated.connect(self._on_remote_meta_updated)
            self._remote.diagnostics_ready.connect(self._on_diagnostics_ready)
            self._remote.diagnostics_failed.connect(self._on_diagnostics_failed)
            self._dl = self._remote.dl
            self._cv = self._remote.cv
            self._remote.start_ws()
        else:
            self._dl = DownloadService(self._settings, parent=self)
            self._cv = ConvertService(self._settings, no_embed=False, parent=self)
        self._dl.progress.connect(self._on_dl_progress)
        self._dl.item_done.connect(self._on_dl_done)
        self._dl.item_failed.connect(self._on_dl_failed)
        self._dl.download_cancelled.connect(self._on_dl_cancelled)
        self._cv.progress.connect(self._on_cv_progress)
        self._cv.item_done.connect(self._on_cv_done)
        self._cv.item_failed.connect(self._on_cv_failed)
        self._cv.convert_cancelled.connect(self._on_cv_cancelled)

        self._store = JobStore()

        self._library_rows: list[LibraryFileRow] = []
        self._library_root: Path | None = None
        self._library_populating_table = False
        self._library_block_edit_signals = False
        self._library_probe_worker: LibraryProbeWorker | None = None
        self._library_prefetch_pending_probe_rows: list[int] = []
        self._library_prefetch_probe_workers: dict[int, LibraryProbeWorker] = {}
        self._library_prefetch_probe_total: int = 0
        self._library_prefetch_probe_done: int = 0
        self._library_prefetch_probe_failures: int = 0
        self._library_infer_pending: list[int] = []
        self._library_infer_groq_slots_busy: int = 0
        self._library_infer_total: int = 0
        self._library_infer_done: int = 0
        self._library_infer_skip_if_tagged: bool = False
        self._library_infer_mp4_compat_mode: bool = False
        self._library_apply_save_worker: LibrarySaveWorker | None = None
        self._library_bulk_rename_worker: LibraryBulkRenameToSongWorker | None = None
        self._library_bulk_clear_metadata_worker: LibraryBulkClearMetadataWorker | None = None
        self._library_scan_worker: LibraryScanWorker | None = None
        self._convert_block_edit_signals = False
        self._convert_probe_worker: LibraryProbeWorker | None = None
        self._convert_apply_save_worker: LibrarySaveWorker | None = None
        self._convert_apply_save_job_id: str | None = None
        self._convert_infer_pending: list[str] = []
        self._convert_infer_groq_slots_busy: int = 0
        self._convert_infer_total: int = 0
        self._convert_infer_done: int = 0
        self._convert_infer_skip_if_tagged: bool = False
        self._convert_infer_mp4_compat_mode: bool = False
        self._convert_infer_save_workers: dict[str, LibrarySaveWorker] = {}

        self._status_message_timer = QTimer(self)
        self._status_message_timer.setSingleShot(True)
        self._status_message_timer.timeout.connect(self._hide_status_message)

        tabs = QTabWidget()
        tabs.addTab(self._build_search_tab(), "Search")
        tabs.addTab(self._build_downloads_tab(), "Downloads")
        tabs.addTab(self._build_convert_tab(), "Convert")
        tabs.addTab(self._build_library_tab(), "Library")
        tabs.addTab(self._build_settings_tab(), "Settings")
        tabs.addTab(self._build_daemon_diagnostics_tab(), "Daemon Diagnostics")

        version_row = QWidget()
        version_row_lay = QHBoxLayout(version_row)
        version_row_lay.setContentsMargins(8, 6, 8, 6)
        version_row_lay.addStretch(1)
        version_label = QLabel(f"mhi2-video-finder v{__version__} · built {build_date_display()}")
        version_label.setStyleSheet(
            "color: palette(text); background-color: palette(button);"
            "padding: 2px 8px; border-radius: 4px; font-weight: 600;"
        )
        version_row_lay.addWidget(version_label)

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        # Breathing room above the physical window bottom so descenders / anti-aliasing are not cut off.
        root.setContentsMargins(0, 0, 0, 16)
        root.setSpacing(0)
        root.addWidget(version_row, 0)
        root.addWidget(tabs, 1)

        # Keep status strip inside the central layout (not QStatusBar). Some Wayland compositors
        # place QStatusBar outside the client region in windowed mode; fullscreen is unaffected.
        self._message_bar = QFrame()
        self._message_bar.setVisible(False)
        self._message_bar.setStyleSheet(
            "QFrame { background-color: palette(window); border-top: 1px solid palette(mid); }"
        )
        mb_row = QHBoxLayout(self._message_bar)
        mb_row.setContentsMargins(10, 6, 10, 6)
        mb_row.setSpacing(10)
        self._message_icon = QLabel()
        self._message_icon.setFixedSize(22, 22)
        self._message_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._message_text = QTextEdit()
        self._message_text.setReadOnly(True)
        self._message_text.setAcceptRichText(False)
        self._message_text.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self._message_text.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._message_text.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._message_text.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        self._message_text.setStyleSheet("QTextEdit { border: 0; background: transparent; padding: 0; }")
        self._message_text.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._message_text.setMinimumHeight(max(54, self.fontMetrics().height() * 3))
        self._message_text.setMaximumHeight(max(120, self.fontMetrics().height() * 8))
        mb_row.addWidget(self._message_icon, alignment=Qt.AlignmentFlag.AlignTop)
        mb_row.addWidget(self._message_text, stretch=1)

        self._status = QLabel("")
        self._status.setStyleSheet("color: palette(mid); padding: 4px 6px 10px 6px;")
        self._status.setWordWrap(True)
        self._status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        # Empty label has zero min height; without this the footer collapses and tabs steal all space.
        self._status.setMinimumHeight(max(22, self.fontMetrics().height() + 6))

        footer = QWidget()
        fl = QVBoxLayout(footer)
        fl.setContentsMargins(0, 0, 0, 0)
        fl.setSpacing(0)
        fl.addWidget(self._message_bar)
        fl.addWidget(self._status)
        footer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        footer.setMinimumHeight(self._status.minimumHeight())
        root.addWidget(footer, 0)

        if self._remote is not None:
            self._remote.user_status.connect(self._status.setText)

        self._apply_settings_to_widgets()

        self._load_persisted_jobs()
        self._refresh_downloads_table()
        self._refresh_convert_table()
        if self._remote is not None:
            self._remote.fetch_recent_jobs_async(200)
            self._remote_jobs_poll = QTimer(self)
            self._remote_jobs_poll.setInterval(_REMOTE_JOBS_POLL_MS)
            self._remote_jobs_poll.timeout.connect(self._poll_remote_daemon_jobs)
            self._remote_jobs_poll.start()
            self._remote.fetch_diagnostics_async()
            self._diagnostics_poll = QTimer(self)
            self._diagnostics_poll.setInterval(_DIAGNOSTICS_POLL_MS)
            self._diagnostics_poll.timeout.connect(self._poll_diagnostics)
            self._diagnostics_poll.start()

    def _poll_remote_daemon_jobs(self) -> None:
        if self._remote is not None and self._use_remote:
            self._remote.fetch_recent_jobs_async(200)

    def _poll_diagnostics(self) -> None:
        if self._remote is not None and self._use_remote:
            self._remote.fetch_diagnostics_async()

    def _hide_status_message(self) -> None:
        self._message_bar.setVisible(False)
        self._message_text.clear()
        self._message_icon.clear()

    def _show_status_message(
        self,
        message: str,
        *,
        kind: Literal["success", "error", "info"] = "success",
        duration_ms: int = 10000,
    ) -> None:
        """Transient bottom bar with icon (check / error / info); hides after ``duration_ms``."""
        style = self.style()
        if kind == "success":
            icon = style.standardIcon(QStyle.StandardPixmap.SP_DialogYesButton)
        elif kind == "error":
            icon = style.standardIcon(QStyle.StandardPixmap.SP_MessageBoxCritical)
        else:
            icon = style.standardIcon(QStyle.StandardPixmap.SP_MessageBoxInformation)
        pm = icon.pixmap(QSize(20, 20))
        self._message_icon.setPixmap(pm)
        self._message_text.setPlainText(message)
        self._message_text.verticalScrollBar().setValue(0)
        self._message_bar.setVisible(True)
        self._status_message_timer.start(duration_ms)

    def _remote_sync_import_root(self) -> Path:
        return ensure_output_dir(self._settings.merged_remote_download_dir() / _REMOTE_DAEMON_IMPORT_SUBDIR)

    def _remote_sync_out_folder_for_channel(self, channel: str) -> Path:
        """Subfolder under daemon-imports from uploader/channel (same idea as CLI --infer-subdir)."""
        root = self._remote_sync_import_root()
        sub = infer_subdir_name(artist=None, channel=channel or None, fallback="")
        if sub:
            return ensure_output_dir(root / sub)
        return root

    def _stem_for_remote_sync_job(self, job: UiJob) -> str:
        raw = (job.candidate.title or "").strip()
        fb = job.candidate.video_id or (job.remote_job_id or "")[:12] or "video"
        return safe_stem(raw, fb)

    def _recompute_remote_sync_out_path_if_needed(self, job: UiJob) -> bool:
        """Update local save path from title/channel metadata (Telegram-imported jobs only)."""
        if job.backend != "remote" or job.remote_job_origin != "remote_sync":
            return False
        if job.remote_saved_locally:
            return False
        folder = self._remote_sync_out_folder_for_channel(job.candidate.channel)
        stem = self._stem_for_remote_sync_job(job)
        new_p = remote_save_out_path(folder, stem)
        if new_p.resolve() != job.out_path.resolve():
            job.out_path = new_p
            return True
        return False

    def _remote_import_autosave_enabled(self) -> bool:
        return bool(self._settings.remote_auto_download_daemon_imports)

    def _should_autosave_remote_job(self, job: UiJob) -> bool:
        if job.backend != "remote":
            return False
        if job.convert_status != "done" or job.remote_saved_locally:
            return False
        if not job.remote_job_id:
            return False
        if job.remote_fetch_in_progress:
            return False
        return (self._settings.remote_auto_download and job.remote_job_origin == "desktop") or (
            self._remote_import_autosave_enabled() and job.remote_job_origin == "remote_sync"
        )

    def _on_remote_daemon_jobs_imported(self, rows: object) -> None:
        """Materialize daemon-side jobs into local rows (no POST /v1/jobs)."""
        if not isinstance(rows, list) or not self._use_remote or self._remote is None:
            return
        existing_remote = {j.remote_job_id for j in self._jobs.values() if j.remote_job_id}
        added = 0
        to_persist: list[tuple[UiJob, int]] = []
        to_sync: list[tuple[str, str]] = []
        to_autosave: list[UiJob] = []
        # Server returns newest-first; append oldest first so recent imports land at the bottom.
        for row in reversed(rows):
            if not isinstance(row, dict):
                continue
            rid = row.get("job_id")
            if not isinstance(rid, str) or not rid.strip() or rid in existing_remote:
                continue
            url = row.get("url")
            if not isinstance(url, str) or not url.strip():
                continue
            existing_remote.add(rid)

            vid = str(row.get("video_id") or "").strip()
            if len(vid) != 11 and "watch?v=" in url:
                vid = url.split("v=", 1)[1].split("&")[0][:11]
            if len(vid) != 11:
                vid = ""
            canon_url = f"https://www.youtube.com/watch?v={vid}" if len(vid) == 11 else url.strip()
            raw_title = str(row.get("title") or "").strip()
            title = _display_title_for_remote_row(raw_title)
            channel = str(row.get("channel") or "").strip()
            cand = VideoCandidate(
                video_id=vid,
                title=title,
                duration=None,
                channel=channel,
                url=canon_url,
            )
            out_folder = self._remote_sync_out_folder_for_channel(channel)
            stem = safe_stem(raw_title, cand.video_id or rid[:12])
            outp = remote_save_out_path(out_folder, stem)

            jid = new_job_id()
            job = UiJob(
                job_id=jid,
                candidate=cand,
                out_path=outp,
                no_embed=bool(row.get("no_embed")),
                backend="remote",
                remote_saved_locally=False,
                remote_job_id=rid,
                remote_job_origin="remote_sync",
                download_status="queued",
                convert_status="waiting",
                meta_filename_stem=outp.stem,
            )
            self._jobs[jid] = job
            self._job_order.append(jid)
            self._apply_remote_status_snapshot(job, row)
            to_persist.append((job, len(self._job_order) - 1))
            self._remote.register_existing(jid, rid)
            if self._needs_remote_status_sync(row):
                to_sync.append((jid, rid))
            if self._should_autosave_remote_job(job):
                to_autosave.append(job)
            added += 1

        if added:
            self._store.upsert_many(to_persist)
            for local_id, remote_id in to_sync:
                self._remote.sync_job_from_server(local_id, remote_id)
            self._refresh_downloads_table()
            self._refresh_convert_table()
            self._status.setText(f"Imported {added} job(s) from remote daemon.")
            for job in to_autosave:
                self._start_remote_fetch(job)

    @staticmethod
    def _needs_remote_status_sync(row: dict[str, object]) -> bool:
        st = str(row.get("status") or "").strip().lower()
        return st not in ("done", "failed", "cancelled")

    @staticmethod
    def _needs_remote_status_sync_for_job(job: UiJob) -> bool:
        if job.download_status in ("queued", "downloading"):
            return True
        if job.download_status == "done" and job.convert_status in ("queued", "waiting", "converting"):
            return True
        return False

    @staticmethod
    def _apply_remote_status_snapshot(job: UiJob, row: dict[str, object]) -> None:
        st = str(row.get("status") or "").strip().lower()
        phase = str(row.get("phase") or "").strip().lower()
        err = str(row.get("error") or "").strip()
        err_phase = str(row.get("error_phase") or "").strip().lower()

        if st in ("queued", "downloading"):
            job.download_status = "downloading" if st == "downloading" else "queued"
            pct = row.get("download_percent")
            if isinstance(pct, (int, float)):
                job.download_percent = float(pct)
            else:
                job.download_percent = -1.0
            job.download_speed = str(row.get("speed") or "")
            job.download_eta = str(row.get("eta") or "")
            if phase == "convert":
                job.download_status = "done"
                job.download_percent = 100.0
                # Daemon keeps status "downloading" + phase "convert" with convert_percent -1
                # until the convert worker starts; that is a convert queue slot, not ffmpeg running.
                cp = row.get("convert_percent")
                cp_f = float(cp) if isinstance(cp, (int, float)) else -1.0
                if cp_f >= 0.0:
                    job.convert_status = "converting"
                    job.convert_percent = cp_f
                    job.convert_indeterminate = False
                else:
                    job.convert_status = "queued"
                    job.convert_percent = 0.0
                    job.convert_indeterminate = False
            return

        if st == "converting":
            job.download_status = "done"
            job.download_percent = 100.0
            job.convert_status = "converting"
            cp = row.get("convert_percent")
            cp_f = float(cp) if isinstance(cp, (int, float)) else -1.0
            if cp_f >= 0.0:
                job.convert_percent = cp_f
                job.convert_indeterminate = False
            else:
                job.convert_percent = 0.0
                job.convert_indeterminate = True
            return

        if st == "done":
            job.download_status = "done"
            job.download_percent = 100.0
            job.convert_status = "done"
            job.convert_percent = 100.0
            job.convert_indeterminate = False
            return

        if st == "failed":
            if err_phase == "convert":
                job.download_status = "done"
                job.download_percent = 100.0
                job.convert_status = "failed"
                job.convert_error = err or "failed"
            else:
                job.download_status = "failed"
                job.download_error = err or "failed"
                job.convert_status = "waiting"
                job.convert_percent = 0.0
                job.convert_indeterminate = False
            return

        if st == "cancelled":
            if phase == "convert":
                job.download_status = "done"
                job.download_percent = 100.0
                job.convert_status = "cancelled"
                job.convert_percent = 0.0
                job.convert_indeterminate = False
            else:
                job.download_status = "cancelled"
                job.download_percent = -1.0
                job.download_speed = ""
                job.download_eta = ""

    def _persist_job(self, job: UiJob) -> None:
        try:
            seq = self._job_order.index(job.job_id)
        except ValueError:
            return
        self._store.upsert(job, seq)

    def _persist_all_jobs(self) -> None:
        self._store.prune_not_in(set(self._job_order))
        for seq, jid in enumerate(self._job_order):
            if jid in self._jobs:
                self._store.upsert(self._jobs[jid], seq)

    def _load_persisted_jobs(self) -> None:
        to_autosave_remote: list[UiJob] = []
        for job in self._store.load_all():
            outp = job.out_path
            if not job.meta_filename_stem.strip():
                job.meta_filename_stem = outp.stem
            if job.backend != "remote" and outp.is_file() and outp.stat().st_size >= 4096:
                self._store.delete(job.job_id)
                continue
            self._jobs[job.job_id] = job
            self._job_order.append(job.job_id)

        for jid in list(self._job_order):
            job = self._jobs[jid]
            if job.backend == "remote":
                remote_state_normalized = False
                if job.download_status == "downloading":
                    # "downloading" is transient; after app restart we only trust fresh
                    # daemon status snapshots/events.
                    job.download_status = "queued"
                    job.download_percent = -1.0
                    job.download_speed = ""
                    job.download_eta = ""
                    remote_state_normalized = True
                if job.convert_status == "converting":
                    job.convert_status = "queued"
                    job.convert_percent = 0.0
                    job.convert_indeterminate = False
                    remote_state_normalized = True

                if self._remote is None:
                    if job.download_status in ("queued", "downloading"):
                        job.download_status = "failed"
                        job.download_error = (
                            "Job belongs to remote processing, but the UI is currently running "
                            "in local mode. Switch backend to remote and restart, or remove this row."
                        )
                        remote_state_normalized = True
                    if remote_state_normalized:
                        self._persist_job(job)
                    continue

                if not job.remote_job_id and job.download_status in ("queued", "downloading"):
                    job.download_status = "failed"
                    job.download_error = (
                        "Remote job was never submitted to the server. Remove it and queue again."
                    )
                    self._persist_job(job)
                    continue
                if remote_state_normalized:
                    self._persist_job(job)
                if self._remote and job.remote_job_origin == "remote_sync" and not job.remote_saved_locally:
                    if self._recompute_remote_sync_out_path_if_needed(job):
                        self._persist_job(job)
                if self._remote and job.remote_job_id and self._needs_remote_status_sync_for_job(job):
                    self._remote.register_existing(jid, job.remote_job_id)
                    self._remote.sync_job_from_server(jid, job.remote_job_id)
                elif self._should_autosave_remote_job(job):
                    to_autosave_remote.append(job)
                continue

            if job.download_status == "downloading":
                job.download_status = "queued"
                job.download_percent = -1.0
                job.download_speed = ""
                job.download_eta = ""

            raw_ok = job.raw_path is not None and job.raw_path.is_file()

            if job.download_status == "done" and not raw_ok:
                job.download_status = "queued"
                job.download_percent = -1.0
                job.ytdlp_info = None
                job.convert_status = "waiting"

            if job.download_status in ("failed", "cancelled"):
                continue

            if job.download_status == "queued":
                self._dl.enqueue(jid, job.candidate.url)
                continue

            if job.download_status == "done" and raw_ok:
                if job.convert_status in ("queued", "waiting", "converting"):
                    job.convert_status = "queued"
                    job.convert_percent = 0.0
                    self._cv.enqueue(
                        jid,
                        job.raw_path,
                        job.out_path,
                        job.ytdlp_info,
                        no_embed=job.no_embed,
                    )
        for job in to_autosave_remote:
            self._start_remote_fetch(job)

    @staticmethod
    def _canonical_video_encoder(enc: str) -> str:
        e = (enc or "").strip().lower()
        if e == "vaapi":
            return "h264_vaapi"
        if e == "nvenc":
            return "h264_nvenc"
        if e in ("h264_vaapi", "h264_nvenc", "libx264"):
            return e
        return "libx264"

    def _apply_settings_to_widgets(self) -> None:
        want_remote = self._settings.processing_backend.strip().lower() == "remote"
        self.ui_processing_backend.blockSignals(True)
        self.ui_processing_backend.setCurrentIndex(1 if want_remote else 0)
        self.ui_processing_backend.blockSignals(False)
        self.ui_remote_url.setText((self._settings.remote_base_url or "").strip())
        self.ui_remote_token.setText((self._settings.remote_bearer_token or "").strip())
        self.ui_remote_dl_dir.setText(str(self._settings.remote_download_dir))
        self.ui_remote_auto_download.setChecked(self._settings.remote_auto_download)
        self.ui_remote_auto_download_daemon_imports.setChecked(
            self._settings.remote_auto_download_daemon_imports
        )

        self.limit_spin.setValue(self._settings.search_limit if self._settings.search_limit else 15)
        self.ui_ffmpeg_threads.setValue(max(0, min(32, self._settings.ffmpeg_threads)))
        self.ui_ffmpeg_nice.setValue(max(0, min(19, self._settings.ffmpeg_nice)))
        self.ui_ffmpeg_cpu_limit.setValue(max(0, min(100, self._settings.ffmpeg_cpu_limit_percent)))
        self.ui_max_parallel_dl.setValue(max(1, min(32, self._settings.max_parallel_downloads)))
        self.ui_max_parallel_cv.setValue(max(1, min(32, self._settings.max_parallel_converts)))

        want_enc = self._canonical_video_encoder(self._settings.video_encoder)
        idx = 0
        for i in range(self.ui_video_encoder.count()):
            if self.ui_video_encoder.itemData(i) == want_enc:
                idx = i
                break
        self.ui_video_encoder.blockSignals(True)
        self.ui_video_encoder.setCurrentIndex(idx)
        self.ui_video_encoder.blockSignals(False)
        self.ui_embed_metadata.setChecked(self._settings.embed_metadata)
        self.ui_embed_album_art.setChecked(self._settings.embed_album_art)
        self.ui_vaapi_device.setText((self._settings.vaapi_device or "").strip())
        self.ui_vaapi_cbr.setChecked(self._settings.vaapi_cbr)
        self._update_vaapi_options_visibility()

        lf = self._settings.library_last_folder
        self.ui_library_folder.setText(str(lf) if lf is not None else "")

        self.ui_groq_enabled.setChecked(self._settings.groq_enabled)
        self.ui_groq_api_key.setText((self._settings.groq_api_key or "").strip())
        self.ui_groq_model.setText((self._settings.groq_model or "").strip() or "llama-3.1-8b-instant")
        self.ui_groq_base_url.setText((self._settings.groq_base_url or "").strip())
        self.ui_groq_temperature.setValue(float(self._settings.groq_temperature))

        if hasattr(self, "library_skip_tagged_cb"):
            self.library_skip_tagged_cb.blockSignals(True)
            self.library_skip_tagged_cb.setChecked(self._settings.library_skip_bulk_infer_if_tagged)
            self.library_skip_tagged_cb.blockSignals(False)
        if hasattr(self, "library_mp4_compat_cb"):
            self.library_mp4_compat_cb.blockSignals(True)
            self.library_mp4_compat_cb.setChecked(self._settings.library_bulk_infer_mp4_compat_mode)
            self.library_mp4_compat_cb.blockSignals(False)
        if hasattr(self, "convert_skip_tagged_cb"):
            self.convert_skip_tagged_cb.blockSignals(True)
            self.convert_skip_tagged_cb.setChecked(self._settings.library_skip_bulk_infer_if_tagged)
            self.convert_skip_tagged_cb.blockSignals(False)
        if hasattr(self, "convert_mp4_compat_cb"):
            self.convert_mp4_compat_cb.blockSignals(True)
            self.convert_mp4_compat_cb.setChecked(self._settings.library_bulk_infer_mp4_compat_mode)
            self.convert_mp4_compat_cb.blockSignals(False)

    def _sync_widgets_to_settings(self) -> None:
        pb = self.ui_processing_backend.currentData()
        self._settings.processing_backend = pb if pb in ("local", "remote") else "local"
        self._settings.remote_base_url = self.ui_remote_url.text().strip()
        self._settings.remote_bearer_token = self.ui_remote_token.text().strip()
        rd = self.ui_remote_dl_dir.text().strip()
        if rd:
            self._settings.remote_download_dir = Path(rd).expanduser().resolve()
        self._settings.remote_auto_download = self.ui_remote_auto_download.isChecked()
        self._settings.remote_auto_download_daemon_imports = (
            self.ui_remote_auto_download_daemon_imports.isChecked()
        )

        self._settings.ffmpeg_threads = self.ui_ffmpeg_threads.value()
        self._settings.ffmpeg_nice = self.ui_ffmpeg_nice.value()
        self._settings.ffmpeg_cpu_limit_percent = self.ui_ffmpeg_cpu_limit.value()
        self._settings.max_parallel_downloads = self.ui_max_parallel_dl.value()
        self._settings.max_parallel_converts = self.ui_max_parallel_cv.value()
        data = self.ui_video_encoder.currentData()
        self._settings.video_encoder = data if isinstance(data, str) else "libx264"
        self._settings.embed_metadata = self.ui_embed_metadata.isChecked()
        self._settings.embed_album_art = self.ui_embed_album_art.isChecked()
        dev = self.ui_vaapi_device.text().strip()
        self._settings.vaapi_device = dev if dev else "/dev/dri/renderD128"
        self._settings.vaapi_cbr = self.ui_vaapi_cbr.isChecked()

        lib = self.ui_library_folder.text().strip()
        self._settings.library_last_folder = Path(lib).expanduser().resolve() if lib else None

        self._settings.groq_enabled = self.ui_groq_enabled.isChecked()
        ga = self.ui_groq_api_key.text().strip()
        self._settings.groq_api_key = ga if ga else None
        self._settings.groq_model = self.ui_groq_model.text().strip() or "llama-3.1-8b-instant"
        self._settings.groq_base_url = (
            self.ui_groq_base_url.text().strip() or "https://api.groq.com/openai/v1"
        )
        self._settings.groq_temperature = float(self.ui_groq_temperature.value())

        if hasattr(self, "library_skip_tagged_cb"):
            self._settings.library_skip_bulk_infer_if_tagged = self.library_skip_tagged_cb.isChecked()
        if hasattr(self, "library_mp4_compat_cb"):
            self._settings.library_bulk_infer_mp4_compat_mode = self.library_mp4_compat_cb.isChecked()
        if hasattr(self, "convert_skip_tagged_cb"):
            self._settings.library_skip_bulk_infer_if_tagged = self.convert_skip_tagged_cb.isChecked()
        if hasattr(self, "convert_mp4_compat_cb"):
            self._settings.library_bulk_infer_mp4_compat_mode = self.convert_mp4_compat_cb.isChecked()

    def _update_vaapi_options_visibility(self) -> None:
        data = self.ui_video_encoder.currentData()
        enc = data if isinstance(data, str) else "libx264"
        self._vaapi_frame.setVisible(enc in ("h264_vaapi", "vaapi"))

    def _on_video_encoder_changed(self, _index: int) -> None:
        self._update_vaapi_options_visibility()

    def _settings_config_target(self) -> Path:
        t = self.config_edit.text().strip()
        return Path(t) if t else default_config_path()

    def _apply_session_settings(self) -> None:
        self._sync_widgets_to_settings()
        want_remote = self._settings.processing_backend.strip().lower() == "remote"
        if want_remote != self._initial_remote:
            QMessageBox.warning(
                self,
                "Restart required",
                "Switching between local and remote processing only takes effect after you "
                "restart mhi2-video-finder-ui.",
            )
        self._dl.set_settings(self._settings)
        self._cv.set_settings(self._settings)
        self._dl.set_max_workers(self._settings.max_parallel_downloads)
        self._cv.set_max_workers(self._settings.max_parallel_converts)
        QMessageBox.information(
            self,
            "Settings",
            "Applied for this session. New encodes use the encoder, embed options, FFmpeg limits, "
            "and queue width from this tab.",
        )

    def _save_settings_to_file(self) -> None:
        self._sync_widgets_to_settings()
        path = self._settings_config_target()
        try:
            save_settings(self._settings, path)
        except OSError as e:
            QMessageBox.critical(self, "Save failed", str(e))
            return
        if not self.config_edit.text().strip():
            self.config_edit.setText(str(path))
        self._config_path = path
        QMessageBox.information(self, "Settings", f"Saved to:\n{path}")

    def _reload_settings(self) -> None:
        p = Path(self.config_edit.text().strip()) if self.config_edit.text().strip() else None
        self._config_path = p
        self._settings = load_settings(p)
        self._apply_settings_to_widgets()
        self._dl.set_settings(self._settings)
        self._cv.set_settings(self._settings)
        QMessageBox.information(
            self,
            "Settings",
            "Config reloaded. Encode options and paths apply to new work; restart the app if something "
            "still looks stale.",
        )

    def _build_search_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)

        cfg_row = QHBoxLayout()
        cfg_row.addWidget(QLabel("Config file (optional):"))
        self.config_edit = QLineEdit()
        self.config_edit.setPlaceholderText("Default: XDG config path")
        if self._config_path:
            self.config_edit.setText(str(self._config_path))
        cfg_row.addWidget(self.config_edit)
        browse_cfg = QPushButton("Browse…")
        browse_cfg.clicked.connect(self._browse_config)
        cfg_row.addWidget(browse_cfg)
        reload_cfg = QPushButton("Reload config")
        reload_cfg.clicked.connect(self._reload_settings)
        cfg_row.addWidget(reload_cfg)
        lay.addLayout(cfg_row)

        src_box = QGroupBox("Source")
        src_grid = QGridLayout(src_box)
        self.source_combo = QComboBox()
        self.source_combo.addItems(["Search term", "Channel", "Playlist", "Video URL"])
        self.source_combo.currentIndexChanged.connect(self._update_source_widgets)
        self.source_combo.currentIndexChanged.connect(self._on_search_inputs_changed)
        src_grid.addWidget(QLabel("Type:"), 0, 0)
        src_grid.addWidget(self.source_combo, 0, 1)
        self.query_edit = QLineEdit()
        self.query_edit.setPlaceholderText("Query, @channel, channel / playlist URL, or single video URL")
        self.query_edit.textChanged.connect(self._on_search_inputs_changed)
        src_grid.addWidget(QLabel("Input:"), 1, 0)
        src_grid.addWidget(self.query_edit, 1, 1)
        self.use_api_cb = QCheckBox("Use YouTube Data API (needs API key)")
        self.use_api_cb.toggled.connect(self._update_source_widgets)
        src_grid.addWidget(self.use_api_cb, 2, 0, 1, 2)
        self.artist_edit = QLineEdit()
        self.artist_edit.setPlaceholderText("Artist (optional; with API or template)")
        self.artist_edit.textChanged.connect(self._on_search_inputs_changed)
        self.title_edit = QLineEdit()
        self.title_edit.setPlaceholderText("Song title (optional)")
        self.title_edit.textChanged.connect(self._on_search_inputs_changed)
        src_grid.addWidget(QLabel("Artist:"), 3, 0)
        src_grid.addWidget(self.artist_edit, 3, 1)
        src_grid.addWidget(QLabel("Title:"), 4, 0)
        src_grid.addWidget(self.title_edit, 4, 1)
        self.template_combo = QComboBox()
        self.template_combo.addItems(["music_video", "vevo", "official", "plain"])
        src_grid.addWidget(QLabel("Template:"), 5, 0)
        src_grid.addWidget(self.template_combo, 5, 1)
        lay.addWidget(src_box)
        self._update_source_widgets()

        paste_box = QGroupBox("Paste many video URLs")
        paste_lay = QVBoxLayout(paste_box)
        self.bulk_urls_edit = QTextEdit()
        self.bulk_urls_edit.setPlaceholderText(
            "YouTube links (watch, youtu.be, Shorts, …), one per line or pasted from chat logs. "
            "Extra query params and timestamp prefixes are stripped. "
            "Empty lines and lines starting with # are ignored."
        )
        self.bulk_urls_edit.setAcceptRichText(False)
        self.bulk_urls_edit.setMinimumHeight(100)
        self.bulk_urls_edit.setToolTip(
            "Resolve each link with yt-dlp, then queue downloads using the subfolder and "
            "transcode options below."
        )
        paste_lay.addWidget(self.bulk_urls_edit)
        bulk_btn_row = QHBoxLayout()
        self.bulk_queue_btn = QPushButton("Queue pasted URLs for download")
        self.bulk_queue_btn.clicked.connect(self._start_queue_pasted_urls)
        bulk_btn_row.addWidget(self.bulk_queue_btn)
        bulk_btn_row.addStretch()
        paste_lay.addLayout(bulk_btn_row)
        lay.addWidget(paste_box)

        opt_row = QHBoxLayout()
        opt_row.addWidget(QLabel("Result limit (0 = no limit):"))
        self.limit_spin = QSpinBox()
        self.limit_spin.setRange(0, 500)
        opt_row.addWidget(self.limit_spin)
        opt_row.addStretch()
        lay.addLayout(opt_row)

        btn_row = QHBoxLayout()
        self.search_btn = QPushButton("Search")
        self.search_btn.clicked.connect(self._start_search)
        btn_row.addWidget(self.search_btn)
        self.select_all_cb = QCheckBox("Select all results")
        self.select_all_cb.setEnabled(False)
        self.select_all_cb.toggled.connect(self._set_all_results_checked)
        btn_row.addWidget(self.select_all_cb)
        btn_row.addStretch()
        lay.addLayout(btn_row)

        self.results_table = QTableWidget(0, 5)
        self.results_table.setHorizontalHeaderLabels(["", "Title", "Channel", "Duration", "URL"])
        self.results_table.horizontalHeader().setStretchLastSection(True)
        self.results_table.setColumnWidth(0, 36)
        self.results_table.setColumnWidth(3, 72)
        self.results_table.setMinimumHeight(96)
        self.results_table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        lay.addWidget(self.results_table, 1)

        self.subdir_edit = QLineEdit()
        self.subdir_edit.setPlaceholderText("Output subfolder under Videos/mhi2-video-finder")
        self.subdir_edit.setText("gui-downloads")
        self.subdir_edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.subdir_edit.setMinimumWidth(80)

        queue_row1 = QHBoxLayout()
        queue_row1.addWidget(QLabel("Subfolder:"))
        queue_row1.addWidget(self.subdir_edit, 1)

        self.infer_subdir_cb = QCheckBox("Infer subfolder from artist / channel")
        self.infer_subdir_cb.setToolTip(
            "After a successful search, set the subfolder from the Artist field when it is filled, "
            "from the channel query when Source is “Channel videos”, "
            "or from the first result’s channel name otherwise."
        )
        self.infer_subdir_cb.toggled.connect(self._on_infer_subdir_toggled)
        self.no_embed_cb = QCheckBox("Transcode only (no tags / album art)")
        self.queue_btn = QPushButton("Queue selected for download")
        self.queue_btn.clicked.connect(self._queue_selected)

        queue_row2 = QHBoxLayout()
        queue_row2.addWidget(self.infer_subdir_cb)
        queue_row2.addWidget(self.no_embed_cb)
        queue_row2.addWidget(self.queue_btn)
        queue_row2.addStretch()

        queue_outer = QVBoxLayout()
        queue_outer.addLayout(queue_row1)
        queue_outer.addLayout(queue_row2)
        lay.addLayout(queue_outer)

        return w

    def _build_settings_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)

        proc_box = QGroupBox("Processing backend (GUI)")
        pg = QGridLayout(proc_box)
        pg.addWidget(QLabel("Run jobs on:"), 0, 0)
        self.ui_processing_backend = QComboBox()
        self.ui_processing_backend.addItem("This computer (local)", "local")
        self.ui_processing_backend.addItem("Remote server (Podman daemon)", "remote")
        self.ui_processing_backend.setToolTip(
            "Local uses yt-dlp and ffmpeg on this machine. Remote submits jobs to "
            "mhi2-video-finder-daemon. Switching modes requires restarting the app."
        )
        pg.addWidget(self.ui_processing_backend, 0, 1)
        pg.addWidget(QLabel("Remote base URL:"), 1, 0)
        self.ui_remote_url = QLineEdit()
        self.ui_remote_url.setPlaceholderText("http://server:8765")
        pg.addWidget(self.ui_remote_url, 1, 1)
        pg.addWidget(QLabel("Bearer token:"), 2, 0)
        self.ui_remote_token = QLineEdit()
        self.ui_remote_token.setEchoMode(QLineEdit.EchoMode.Password)
        self.ui_remote_token.setPlaceholderText("DAEMON_BEARER_TOKEN value (if set on server)")
        pg.addWidget(self.ui_remote_token, 2, 1)
        pg.addWidget(QLabel("Local folder for remote files:"), 3, 0)
        rdir_row = QHBoxLayout()
        self.ui_remote_dl_dir = QLineEdit()
        self.ui_remote_dl_dir.setPlaceholderText("Where to save MP4s downloaded from the server")
        br = QPushButton("Browse…")
        br.clicked.connect(self._browse_remote_dl_dir)
        rdir_row.addWidget(self.ui_remote_dl_dir)
        rdir_row.addWidget(br)
        pg.addLayout(rdir_row, 3, 1)
        self.ui_remote_auto_download = QCheckBox("Auto-download finished remote jobs to the folder above")
        pg.addWidget(self.ui_remote_auto_download, 4, 0, 1, 2)
        self.ui_remote_auto_download_daemon_imports = QCheckBox(
            "Auto-save imported daemon jobs to PC when they are already done"
        )
        pg.addWidget(self.ui_remote_auto_download_daemon_imports, 5, 0, 1, 2)
        lay.addWidget(proc_box)

        out_box = QGroupBox("Output encoding (car USB / MHI2)")
        og = QGridLayout(out_box)
        og.addWidget(QLabel("Video encoder:"), 0, 0)
        self.ui_video_encoder = QComboBox()
        for label, data in _ENCODER_CHOICES:
            self.ui_video_encoder.addItem(label, data)
        self.ui_video_encoder.setToolTip(
            "libx264 usually matches strict H.264 baseline streams best. "
            "GPU encoders are faster but some car systems play them worse."
        )
        self.ui_video_encoder.currentIndexChanged.connect(self._on_video_encoder_changed)
        og.addWidget(self.ui_video_encoder, 0, 1)
        self.ui_embed_metadata = QCheckBox("Embed title / artist / album metadata (from yt-dlp)")
        self.ui_embed_metadata.setToolTip(
            "When unchecked, ffmpeg skips -metadata tags from the download info."
        )
        og.addWidget(self.ui_embed_metadata, 1, 0, 1, 2)
        self.ui_embed_album_art = QCheckBox("Embed album art (adds MJPEG attached-picture stream)")
        self.ui_embed_album_art.setToolTip(
            "Adds a second video track for cover art. Turn off if a player stutters or misbehaves."
        )
        og.addWidget(self.ui_embed_album_art, 2, 0, 1, 2)
        self._vaapi_frame = QWidget()
        vf = QGridLayout(self._vaapi_frame)
        vf.setContentsMargins(0, 0, 0, 0)
        vf.addWidget(QLabel("VAAPI render node:"), 0, 0)
        self.ui_vaapi_device = QLineEdit()
        self.ui_vaapi_device.setPlaceholderText("/dev/dri/renderD128")
        vf.addWidget(self.ui_vaapi_device, 0, 1)
        self.ui_vaapi_cbr = QCheckBox("VAAPI constant bitrate (CBR)")
        self.ui_vaapi_cbr.setToolTip("Steadier bitrate; can help picky USB decoders when using VAAPI.")
        vf.addWidget(self.ui_vaapi_cbr, 1, 0, 1, 2)
        og.addWidget(self._vaapi_frame, 3, 0, 1, 2)
        lay.addWidget(out_box)

        enc = QGroupBox("FFmpeg / CPU")
        g = QGridLayout(enc)
        g.addWidget(QLabel("ffmpeg -threads (0 = default):"), 0, 0)
        self.ui_ffmpeg_threads = QSpinBox()
        self.ui_ffmpeg_threads.setRange(0, 32)
        self.ui_ffmpeg_threads.setToolTip("Cap decoder/encoder threads. 0 leaves ffmpeg default.")
        g.addWidget(self.ui_ffmpeg_threads, 0, 1)
        g.addWidget(QLabel("Nice (+priority, Unix):"), 1, 0)
        self.ui_ffmpeg_nice = QSpinBox()
        self.ui_ffmpeg_nice.setRange(0, 19)
        self.ui_ffmpeg_nice.setToolTip("Higher = lower priority vs other processes when CPU is busy.")
        g.addWidget(self.ui_ffmpeg_nice, 1, 1)
        g.addWidget(QLabel("CPU limit % (0 = off):"), 2, 0)
        self.ui_ffmpeg_cpu_limit = QSpinBox()
        self.ui_ffmpeg_cpu_limit.setRange(0, 100)
        self.ui_ffmpeg_cpu_limit.setToolTip("Needs cpulimit on PATH. Average CPU cap per encode.")
        g.addWidget(self.ui_ffmpeg_cpu_limit, 2, 1)
        lay.addWidget(enc)

        qbox = QGroupBox("Queue — max jobs at once")
        qg = QGridLayout(qbox)
        qg.addWidget(QLabel("Parallel downloads:"), 0, 0)
        self.ui_max_parallel_dl = QSpinBox()
        self.ui_max_parallel_dl.setRange(1, 32)
        qg.addWidget(self.ui_max_parallel_dl, 0, 1)
        qg.addWidget(QLabel("Parallel converts:"), 1, 0)
        self.ui_max_parallel_cv = QSpinBox()
        self.ui_max_parallel_cv.setRange(1, 32)
        qg.addWidget(self.ui_max_parallel_cv, 1, 1)
        lay.addWidget(qbox)

        groq_box = QGroupBox("AI assistant (Library tab)")
        gg = QGridLayout(groq_box)
        self.ui_groq_enabled = QCheckBox("Turn on AI guesses for artist & song")
        gg.addWidget(self.ui_groq_enabled, 0, 0, 1, 2)
        gg.addWidget(QLabel("API key:"), 1, 0)
        self.ui_groq_api_key = QLineEdit()
        self.ui_groq_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.ui_groq_api_key.setPlaceholderText("gsk_… or set GROQ_API_KEY")
        gg.addWidget(self.ui_groq_api_key, 1, 1)
        gg.addWidget(QLabel("Model:"), 2, 0)
        self.ui_groq_model = QLineEdit()
        self.ui_groq_model.setPlaceholderText("llama-3.1-8b-instant")
        gg.addWidget(self.ui_groq_model, 2, 1)
        gg.addWidget(QLabel("Base URL:"), 3, 0)
        self.ui_groq_base_url = QLineEdit()
        self.ui_groq_base_url.setPlaceholderText("https://api.groq.com/openai/v1")
        gg.addWidget(self.ui_groq_base_url, 3, 1)
        gg.addWidget(QLabel("Temperature:"), 4, 0)
        self.ui_groq_temperature = QDoubleSpinBox()
        self.ui_groq_temperature.setRange(0.0, 2.0)
        self.ui_groq_temperature.setSingleStep(0.05)
        self.ui_groq_temperature.setDecimals(2)
        gg.addWidget(self.ui_groq_temperature, 4, 1)
        lay.addWidget(groq_box)

        hint = QLabel(
            "Uses the config file path from the Search tab (or the default XDG path if empty). "
            "Apply updates workers for this session; Save writes merged values into config.toml "
            "(video_encoder, embed_metadata, embed_album_art, vaapi_*, ffmpeg_*, parallel queues, "
            "library_last_folder, AI assistant / Groq settings)."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(HINT_LABEL_STYLE)
        lay.addWidget(hint)

        row = QHBoxLayout()
        apply_btn = QPushButton("Use these settings now")
        apply_btn.clicked.connect(self._apply_session_settings)
        row.addWidget(apply_btn)
        save_btn = QPushButton("Save for next time")
        save_btn.clicked.connect(self._save_settings_to_file)
        row.addWidget(save_btn)
        row.addStretch()
        lay.addLayout(row)
        lay.addStretch()
        return w

    def _update_source_widgets(self) -> None:
        idx = self.source_combo.currentIndex()
        is_search = idx == 0
        is_video_url = idx == 3
        self.use_api_cb.setEnabled(is_search)
        self.artist_edit.setEnabled(is_search)
        self.title_edit.setEnabled(is_search)
        self.template_combo.setEnabled(is_search)
        if is_video_url:
            self.query_edit.setPlaceholderText("Paste YouTube video URL (watch, youtu.be, Shorts, …)")
        else:
            self.query_edit.setPlaceholderText("Query, @channel, channel / playlist URL, or single video URL")

    def _browse_config(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "config.toml", str(Path.home()), "TOML (*.toml);;All (*)")
        if path:
            self.config_edit.setText(path)

    def _browse_remote_dl_dir(self) -> None:
        start = self.ui_remote_dl_dir.text().strip() or str(Path.home())
        d = QFileDialog.getExistingDirectory(self, "Folder for files downloaded from remote server", start)
        if d:
            self.ui_remote_dl_dir.setText(d)

    def _limit_value(self) -> int | None:
        v = self.limit_spin.value()
        return None if v == 0 else v

    def _on_search_inputs_changed(self) -> None:
        if not (self._search_worker and self._search_worker.isRunning()):
            return
        self._search_seq += 1
        self.search_btn.setEnabled(True)
        self._search_worker.requestInterruption()

    def _start_search(self) -> None:
        q = self.query_edit.text().strip()
        idx = self.source_combo.currentIndex()
        mode = ("search", "channel", "playlist", "video_url")[idx]
        if not q and not (mode == "search" and self.artist_edit.text().strip()):
            QMessageBox.warning(
                self,
                "Search",
                "Enter a search query, channel, playlist, or video URL (or an artist for template search).",
            )
            return

        self._settings = load_settings(
            Path(self.config_edit.text().strip()) if self.config_edit.text().strip() else None
        )
        if self.use_api_cb.isChecked() and mode == "search" and not self._settings.youtube_api_key:
            QMessageBox.warning(
                self, "API", "Set YOUTUBE_API_KEY or youtube_api_key in config for API search."
            )
            return

        if self._search_worker and self._search_worker.isRunning():
            self._search_worker.requestInterruption()

        self._search_seq += 1
        run_seq = self._search_seq
        self.search_btn.setEnabled(False)
        self._search_worker = SearchWorker(
            mode=mode,
            query=q,
            settings=self._settings,
            use_youtube_api=self.use_api_cb.isChecked() and mode == "search",
            artist=self.artist_edit.text(),
            title=self.title_edit.text(),
            template=self.template_combo.currentText(),
            limit=self._limit_value(),
            parent=self,
        )
        self._search_worker.finished_ok.connect(partial(self._search_finished, _seq=run_seq))
        self._search_worker.failed.connect(partial(self._search_failed, _seq=run_seq))
        self._search_worker.finished.connect(self._search_thread_done)
        self._search_worker.start()

    def _search_thread_done(self) -> None:
        worker = self.sender()
        if worker is not self._search_worker:
            if isinstance(worker, SearchWorker):
                worker.deleteLater()
            return
        self.search_btn.setEnabled(True)
        self._search_worker.deleteLater()
        self._search_worker = None

    def _search_finished(self, rows: list, *, _seq: int) -> None:
        if _seq != self._search_seq:
            return
        self._results = list(rows)
        self.select_all_cb.blockSignals(True)
        self.select_all_cb.setChecked(False)
        self.select_all_cb.blockSignals(False)
        self.select_all_cb.setEnabled(bool(self._results))
        self.results_table.setRowCount(0)
        from mhi2_video_finder.workflow import fmt_duration

        for i, c in enumerate(self._results):
            self.results_table.insertRow(i)
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            chk.setCheckState(Qt.CheckState.Unchecked)
            self.results_table.setItem(i, 0, chk)
            self.results_table.setItem(i, 1, QTableWidgetItem(c.title))
            self.results_table.setItem(i, 2, QTableWidgetItem(c.channel))
            self.results_table.setItem(i, 3, QTableWidgetItem(fmt_duration(c.duration)))
            self.results_table.setItem(i, 4, QTableWidgetItem(c.url))

        self._apply_inferred_subdir_from_results()

    def _apply_inferred_subdir_from_results(self) -> None:
        if not self.infer_subdir_cb.isChecked() or not self._results:
            return
        artist = self.artist_edit.text().strip()
        idx = self.source_combo.currentIndex()
        mode = ("search", "channel", "playlist", "video_url")[idx]
        if artist:
            sub = slug_dir_name(artist, "gui-downloads")
        elif mode == "channel":
            sub = slug_dir_name(self.query_edit.text(), "gui-downloads")
        else:
            sub = slug_dir_name(self._results[0].channel, "gui-downloads")
        self.subdir_edit.setText(sub)

    def _on_infer_subdir_toggled(self, checked: bool) -> None:
        if checked and self._results:
            self._apply_inferred_subdir_from_results()

    def _set_all_results_checked(self, checked: bool) -> None:
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for i in range(self.results_table.rowCount()):
            it = self.results_table.item(i, 0)
            if it is not None:
                it.setCheckState(state)

    def _search_failed(self, msg: str, *, _seq: int) -> None:
        if _seq != self._search_seq:
            return
        QMessageBox.critical(self, "Search failed", msg)

    def _deselect_result_checkboxes(self) -> None:
        self.select_all_cb.blockSignals(True)
        self.select_all_cb.setChecked(False)
        self.select_all_cb.blockSignals(False)
        for i in range(self.results_table.rowCount()):
            it = self.results_table.item(i, 0)
            if it is not None:
                it.setCheckState(Qt.CheckState.Unchecked)

    def _notify_queued(self, count: int) -> None:
        msg = f"Queued {count} video(s) for download."
        self._status.setText(msg)
        QApplication.beep()
        if self._tray is not None:
            self._tray.showMessage(
                "mhi2-video-finder",
                msg,
                QSystemTrayIcon.MessageIcon.Information,
                4000,
            )
        else:
            box = QMessageBox(
                QMessageBox.Icon.Information,
                "mhi2-video-finder",
                msg,
                QMessageBox.StandardButton.Ok,
                self,
            )
            box.setModal(False)
            box.show()
            QTimer.singleShot(3500, box.close)

    def _output_folder(self) -> Path:
        sub = self.subdir_edit.text().strip() or "gui-downloads"
        if self._use_remote:
            base = self._settings.merged_remote_download_dir()
        else:
            base = self._settings.merged_output_dir()
        return ensure_output_dir(base / sub)

    def _enqueue_job_for_candidate(self, c: VideoCandidate) -> None:
        out_folder = self._output_folder()
        self._cv.set_no_embed(self.no_embed_cb.isChecked())
        ne = self.no_embed_cb.isChecked()
        jid = new_job_id()
        stem = safe_stem(c.title, c.video_id)
        backend = "remote" if self._use_remote else "local"
        outp = (
            remote_save_out_path(out_folder, stem)
            if backend == "remote"
            else unique_out_path(out_folder, stem, c.video_id)
        )
        job = UiJob(
            job_id=jid,
            candidate=c,
            out_path=outp,
            no_embed=ne,
            backend=backend,
            remote_saved_locally=backend != "remote",
            remote_job_origin="desktop",
            meta_filename_stem=outp.stem,
        )
        self._jobs[jid] = job
        self._job_order.append(jid)
        if self._use_remote and self._remote:
            sub = self.subdir_edit.text().strip() or "gui-downloads"
            self._remote.set_pending_job(
                jid,
                subdir=sub,
                output_stem=stem,
                video_id=c.video_id,
                title=c.title,
                channel=c.channel,
                no_embed=ne,
            )
        self._dl.enqueue(jid, c.url)
        job.download_status = "queued"
        self._persist_job(job)

    def _queue_selected(self) -> None:
        if not self._results:
            QMessageBox.information(self, "Queue", "Run a search first.")
            return
        count = 0
        for i in range(self.results_table.rowCount()):
            it = self.results_table.item(i, 0)
            if it is None or it.checkState() != Qt.CheckState.Checked:
                continue
            self._enqueue_job_for_candidate(self._results[i])
            count += 1
        if count == 0:
            QMessageBox.information(self, "Queue", "Check one or more videos to queue.")
            return
        self._deselect_result_checkboxes()
        self._notify_queued(count)
        self._refresh_downloads_table()
        self._refresh_convert_table()

    def _start_queue_pasted_urls(self) -> None:
        urls = parse_pasted_video_urls(self.bulk_urls_edit.toPlainText())
        if not urls:
            QMessageBox.information(
                self,
                "Paste URLs",
                "Paste one or more video URLs (one per line), then try again.",
            )
            return

        self._settings = load_settings(
            Path(self.config_edit.text().strip()) if self.config_edit.text().strip() else None
        )

        if self._bulk_resolve_worker and self._bulk_resolve_worker.isRunning():
            self._bulk_resolve_worker.requestInterruption()

        self._bulk_resolve_seq += 1
        run_seq = self._bulk_resolve_seq
        self.bulk_queue_btn.setEnabled(False)
        self._status.setText(f"Resolving {len(urls)} pasted URL(s)…")

        self._bulk_resolve_worker = BulkUrlResolveWorker(urls, self._settings, parent=self)
        self._bulk_resolve_worker.finished_ok.connect(partial(self._bulk_resolve_finished, _seq=run_seq))
        self._bulk_resolve_worker.finished.connect(self._bulk_resolve_thread_done)
        self._bulk_resolve_worker.start()

    def _bulk_resolve_thread_done(self) -> None:
        worker = self.sender()
        if worker is not self._bulk_resolve_worker:
            if isinstance(worker, BulkUrlResolveWorker):
                worker.deleteLater()
            return
        self.bulk_queue_btn.setEnabled(True)
        self._bulk_resolve_worker.deleteLater()
        self._bulk_resolve_worker = None

    def _bulk_resolve_finished(
        self,
        candidates: list,
        failures: list,
        *,
        _seq: int,
    ) -> None:
        if _seq != self._bulk_resolve_seq:
            return
        ok = [c for c in candidates if isinstance(c, VideoCandidate)]
        bad: list[tuple[str, str]] = []
        for item in failures:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                u, err = item[0], item[1]
                if isinstance(u, str) and isinstance(err, str):
                    bad.append((u, err))

        if not ok and bad:
            lines = "\n".join(f"{u}\n  → {e}" for u, e in bad[:8])
            more = f"\n… and {len(bad) - 8} more" if len(bad) > 8 else ""
            QMessageBox.critical(
                self,
                "Could not resolve URLs",
                f"No links could be resolved:\n\n{lines}{more}",
            )
            self._status.setText("Pasted URL resolution failed.")
            return

        for c in ok:
            self._enqueue_job_for_candidate(c)
        self._notify_queued(len(ok))
        self._refresh_downloads_table()
        self._refresh_convert_table()

        if bad:
            lines = "\n".join(f"{u}\n  → {e}" for u, e in bad[:6])
            more = f"\n… and {len(bad) - 6} more" if len(bad) > 6 else ""
            QMessageBox.warning(
                self,
                "Some URLs skipped",
                f"Queued {len(ok)} video(s). These lines failed:\n\n{lines}{more}",
            )
        else:
            self._status.setText(f"Queued {len(ok)} video(s) from pasted URLs.")

    def _build_daemon_diagnostics_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)

        self._diag_disabled_label = QLabel(
            "Diagnostics require the Remote processing backend. Enable it under "
            "Settings → Processing backend, set the server URL, then restart the app."
        )
        self._diag_disabled_label.setWordWrap(True)
        self._diag_disabled_label.setVisible(self._remote is None)
        lay.addWidget(self._diag_disabled_label)

        top_row = QHBoxLayout()
        self._diag_status_label = QLabel("Status: —")
        self._diag_status_label.setStyleSheet(DIAGNOSTICS_IDLE_STYLE)
        top_row.addWidget(self._diag_status_label)
        top_row.addStretch(1)
        self._diag_refresh_btn = QPushButton("Refresh now")
        self._diag_refresh_btn.setEnabled(self._remote is not None)
        self._diag_refresh_btn.clicked.connect(self._refresh_diagnostics_clicked)
        top_row.addWidget(self._diag_refresh_btn)
        lay.addLayout(top_row)

        build_box = QGroupBox("Daemon build")
        bg = QGridLayout(build_box)
        self._diag_version_val = QLabel("—")
        self._diag_commit_val = QLabel("—")
        self._diag_build_date_val = QLabel("—")
        self._diag_uptime_val = QLabel("—")
        for row, (label, value) in enumerate(
            (
                ("Version:", self._diag_version_val),
                ("Build commit:", self._diag_commit_val),
                ("Build date:", self._diag_build_date_val),
                ("Uptime:", self._diag_uptime_val),
            )
        ):
            bg.addWidget(QLabel(label), row, 0)
            bg.addWidget(value, row, 1)
        lay.addWidget(build_box)

        cfg_box = QGroupBox("Daemon configuration")
        cg = QGridLayout(cfg_box)
        self._diag_max_dl_val = QLabel("—")
        self._diag_max_cv_val = QLabel("—")
        self._diag_encoder_val = QLabel("—")
        self._diag_retention_val = QLabel("—")
        for row, (label, value) in enumerate(
            (
                ("Parallel downloads:", self._diag_max_dl_val),
                ("Parallel converts:", self._diag_max_cv_val),
                ("Video encoder:", self._diag_encoder_val),
                ("Job retention (days):", self._diag_retention_val),
            )
        ):
            cg.addWidget(QLabel(label), row, 0)
            cg.addWidget(value, row, 1)
        lay.addWidget(cfg_box)

        jobs_box = QGroupBox("Job counts")
        jg = QGridLayout(jobs_box)
        self._diag_job_count_labels: dict[str, QLabel] = {}
        for col, key in enumerate(
            ("queued", "downloading", "converting", "done", "failed", "cancelled", "total")
        ):
            jg.addWidget(QLabel(key.capitalize() + ":"), 0, col * 2)
            val = QLabel("—")
            self._diag_job_count_labels[key] = val
            jg.addWidget(val, 0, col * 2 + 1)
        lay.addWidget(jobs_box)

        lay.addStretch(1)
        return w

    @staticmethod
    def _format_uptime(seconds: float) -> str:
        s = max(0, int(seconds))
        days, s = divmod(s, 86400)
        hours, s = divmod(s, 3600)
        minutes, s = divmod(s, 60)
        parts = []
        if days:
            parts.append(f"{days}d")
        if hours or days:
            parts.append(f"{hours}h")
        parts.append(f"{minutes}m")
        return " ".join(parts)

    def _refresh_diagnostics_clicked(self) -> None:
        if self._remote is not None:
            self._diag_status_label.setText("Status: checking…")
            self._remote.fetch_diagnostics_async()

    def _on_diagnostics_ready(self, data: dict) -> None:
        self._diag_status_label.setText("Status: online")
        self._diag_status_label.setStyleSheet(DIAGNOSTICS_ONLINE_STYLE)
        self._diag_version_val.setText(str(data.get("app_version") or "—"))
        self._diag_commit_val.setText(str(data.get("build_commit") or "—"))
        self._diag_build_date_val.setText(str(data.get("build_date") or "—"))
        uptime = data.get("uptime_seconds")
        self._diag_uptime_val.setText(
            self._format_uptime(float(uptime)) if isinstance(uptime, (int, float)) else "—"
        )
        self._diag_max_dl_val.setText(str(data.get("max_parallel_downloads") or "—"))
        self._diag_max_cv_val.setText(str(data.get("max_parallel_converts") or "—"))
        self._diag_encoder_val.setText(str(data.get("video_encoder") or "—"))
        self._diag_retention_val.setText(str(data.get("job_retention_days") or "—"))
        counts = data.get("job_counts")
        if isinstance(counts, dict):
            for key, label in self._diag_job_count_labels.items():
                label.setText(str(counts.get(key, 0)))

    def _on_diagnostics_failed(self, reason: str) -> None:
        self._diag_status_label.setText(f"Status: offline — {reason}")
        self._diag_status_label.setStyleSheet(DIAGNOSTICS_OFFLINE_STYLE)

    def _build_downloads_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        bulk = QHBoxLayout()
        sa = QPushButton("Select all")
        sa.clicked.connect(partial(self._set_all_queue_checks, "downloads", True))
        clr = QPushButton("Clear selection")
        clr.clicked.connect(partial(self._set_all_queue_checks, "downloads", False))
        cancel_sel = QPushButton("Cancel selected")
        cancel_sel.setToolTip("Stop the download for each checked row that is queued or in progress.")
        cancel_sel.clicked.connect(self._cancel_selected_downloads)
        remove_sel = QPushButton("Remove selected")
        remove_sel.setToolTip("Remove checked rows from the queue (one confirmation for all).")
        remove_sel.clicked.connect(partial(self._remove_selected_queue_jobs, "downloads"))
        bulk.addWidget(sa)
        bulk.addWidget(clr)
        bulk.addWidget(cancel_sel)
        bulk.addWidget(remove_sel)
        bulk.addStretch()
        lay.addLayout(bulk)
        self.downloads_table = QTableWidget(0, 6)
        self.downloads_table.setHorizontalHeaderLabels(
            ["", "Title", "Status", "Progress", "Speed / ETA", "Actions"]
        )
        hh = self.downloads_table.horizontalHeader()
        hh.setStretchLastSection(True)
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        hh.resizeSection(0, 28)
        self.downloads_table.setMinimumHeight(96)
        self.downloads_table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        lay.addWidget(self.downloads_table, 1)
        return w

    def _build_convert_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        bulk = QHBoxLayout()
        sa = QPushButton("Select all")
        sa.clicked.connect(partial(self._set_all_queue_checks, "convert", True))
        clr = QPushButton("Clear selection")
        clr.clicked.connect(partial(self._set_all_queue_checks, "convert", False))
        cancel_sel = QPushButton("Cancel selected")
        cancel_sel.setToolTip("Stop conversion for each checked row that is queued or in progress.")
        cancel_sel.clicked.connect(self._cancel_selected_converts)
        remove_sel = QPushButton("Remove selected")
        remove_sel.setToolTip("Remove checked rows from the queue (one confirmation for all).")
        remove_sel.clicked.connect(partial(self._remove_selected_queue_jobs, "convert"))
        bulk.addWidget(sa)
        bulk.addWidget(clr)
        bulk.addWidget(cancel_sel)
        bulk.addWidget(remove_sel)
        bulk.addStretch()
        lay.addLayout(bulk)
        self.convert_table = QTableWidget(0, 6)
        self.convert_table.setHorizontalHeaderLabels(
            ["", "Title", "Status", "Progress", "AI status", "Actions"]
        )
        ch = self.convert_table.horizontalHeader()
        ch.setStretchLastSection(True)
        ch.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        ch.resizeSection(0, 28)
        self.convert_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.convert_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.convert_table.itemSelectionChanged.connect(self._convert_on_selection_changed)
        self.convert_table.setMinimumHeight(96)
        self.convert_table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        lay.addWidget(self.convert_table, 1)

        edit_box = QGroupBox("Selected converted file")
        eg = QGridLayout(edit_box)
        self.ui_cv_artist = QLineEdit()
        self.ui_cv_artist.setPlaceholderText("Artist")
        self.ui_cv_song = QLineEdit()
        self.ui_cv_song.setPlaceholderText("Song title")
        self.ui_cv_stem = QLineEdit()
        self.ui_cv_stem.setPlaceholderText("File name (no extension)")
        eg.addWidget(QLabel("Artist:"), 0, 0)
        eg.addWidget(self.ui_cv_artist, 0, 1)
        eg.addWidget(QLabel("Title:"), 1, 0)
        eg.addWidget(self.ui_cv_song, 1, 1)
        eg.addWidget(QLabel("Rename file to:"), 2, 0)
        eg.addWidget(self.ui_cv_stem, 2, 1)
        self.ui_cv_artist.textChanged.connect(self._convert_on_edit_changed)
        self.ui_cv_song.textChanged.connect(self._convert_on_edit_changed)
        self.ui_cv_stem.textChanged.connect(self._convert_on_edit_changed)
        lay.addWidget(edit_box)

        actions = QHBoxLayout()
        self.convert_guess_selected_btn = QPushButton("Guess selected")
        self.convert_guess_selected_btn.setToolTip(
            "Run AI on checked finished files, write Artist/Title, and save tags to each file."
        )
        self.convert_guess_selected_btn.clicked.connect(self._convert_infer_checked_groq)
        actions.addWidget(self.convert_guess_selected_btn)
        self.convert_guess_current_btn = QPushButton("Guess artist & song")
        self.convert_guess_current_btn.setToolTip(
            "Guess metadata for the selected row and save tags to file."
        )
        self.convert_guess_current_btn.clicked.connect(self._convert_infer_groq)
        actions.addWidget(self.convert_guess_current_btn)
        self.convert_apply_btn = QPushButton("Save to file")
        self.convert_apply_btn.setToolTip(
            "Write Artist/Title to the selected converted file and rename it if needed."
        )
        self.convert_apply_btn.clicked.connect(self._convert_apply_changes)
        actions.addWidget(self.convert_apply_btn)
        actions.addStretch()
        lay.addLayout(actions)

        bulk_opts = QHBoxLayout()
        self.convert_skip_tagged_cb = QCheckBox(
            "Skip files that already have artist & title tags (Guess selected only)"
        )
        self.convert_skip_tagged_cb.setToolTip(
            "Uses ffprobe: if both artist and title exist in the file metadata, that row is skipped. "
            "Single-row Guess artist & song always runs."
        )
        self.convert_skip_tagged_cb.setChecked(self._settings.library_skip_bulk_infer_if_tagged)
        self.convert_skip_tagged_cb.toggled.connect(self._convert_on_bulk_infer_option_toggled)
        bulk_opts.addWidget(self.convert_skip_tagged_cb)
        self.convert_mp4_compat_cb = QCheckBox(
            "MP4 compatibility mode (Guess selected / Guess artist & song: Title becomes 'Title - Artist')"
        )
        self.convert_mp4_compat_cb.setToolTip(
            "AI guessing leaves Artist empty and writes Title as 'song - artist'. Useful for MHI2 setups "
            "that ignore MP4 artist tags."
        )
        self.convert_mp4_compat_cb.setChecked(self._settings.library_bulk_infer_mp4_compat_mode)
        self.convert_mp4_compat_cb.toggled.connect(self._convert_on_bulk_infer_option_toggled)
        bulk_opts.addWidget(self.convert_mp4_compat_cb)
        bulk_opts.addStretch()
        lay.addLayout(bulk_opts)

        hint = QLabel(
            "Select a converted row, edit Artist/Title or file name, then save. "
            "Guess artist & song uses the selected row. Guess selected processes checked finished rows."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(HINT_LABEL_STYLE)
        lay.addWidget(hint)
        return w

    def _convert_current_job_id(self) -> str | None:
        row = self.convert_table.currentRow()
        if row < 0:
            return None
        title_it = self.convert_table.item(row, 1)
        if title_it is None:
            return None
        jid = title_it.data(Qt.ItemDataRole.UserRole)
        return jid if isinstance(jid, str) else None

    def _convert_select_row_for_job_id(self, job_id: str) -> None:
        for i in range(self.convert_table.rowCount()):
            title_it = self.convert_table.item(i, 1)
            if title_it is None:
                continue
            jid = title_it.data(Qt.ItemDataRole.UserRole)
            if jid == job_id:
                self.convert_table.setCurrentCell(i, 1)
                return

    @staticmethod
    def _convert_job_ready_for_metadata(job: UiJob) -> bool:
        if job.convert_status != "done":
            return False
        if job.backend == "remote" and not job.remote_saved_locally:
            return False
        return job.out_path.is_file()

    def _convert_on_selection_changed(self) -> None:
        if self._convert_block_edit_signals:
            return
        jid = self._convert_current_job_id()
        if not jid:
            return
        job = self._jobs.get(jid)
        if job is None:
            return
        if not job.meta_filename_stem.strip():
            job.meta_filename_stem = job.out_path.stem
        self._convert_block_edit_signals = True
        try:
            self.ui_cv_artist.setText(job.meta_artist)
            self.ui_cv_song.setText(job.meta_title)
            self.ui_cv_stem.setText(job.meta_filename_stem)
        finally:
            self._convert_block_edit_signals = False
        self._convert_start_probe_for_job(jid)

    def _convert_start_probe_for_job(self, job_id: str) -> None:
        if self._convert_probe_worker is not None:
            self._convert_probe_worker.requestInterruption()
            self._convert_probe_worker.wait(3000)
            self._convert_probe_worker = None
        job = self._jobs.get(job_id)
        if job is None or not self._convert_job_ready_for_metadata(job):
            return
        self._status.setText(f"Reading metadata… {job.out_path.name}")
        w = LibraryProbeWorker(0, job.out_path, root=None, parent=self)
        self._convert_probe_worker = w
        w.finished_ok.connect(
            lambda _row, artist, title, summary, _rel, jid=job_id: self._convert_on_probe_ok(
                jid, artist, title, summary
            )
        )
        w.failed.connect(lambda _row, err, jid=job_id: self._convert_on_probe_fail(jid, err))
        w.finished.connect(self._convert_probe_finished_cleanup)
        w.start()

    def _convert_probe_finished_cleanup(self) -> None:
        self._convert_probe_worker = None

    def _convert_on_probe_ok(self, job_id: str, artist: str, title: str, summary: str) -> None:
        job = self._jobs.get(job_id)
        if job is None:
            return
        job.meta_probe_summary = summary
        if artist and not job.meta_artist:
            job.meta_artist = artist
        if title and not job.meta_title:
            job.meta_title = title
        if self._convert_current_job_id() == job_id and not self._convert_block_edit_signals:
            self._convert_block_edit_signals = True
            try:
                self.ui_cv_artist.setText(job.meta_artist)
                self.ui_cv_song.setText(job.meta_title)
            finally:
                self._convert_block_edit_signals = False
        self._status.setText(f"Ready — {job.out_path.name}")

    def _convert_on_probe_fail(self, _job_id: str, err: str) -> None:
        self._show_status_message(f"Could not read media info: {err}", kind="error")

    def _convert_on_edit_changed(self, *_args: object) -> None:
        if self._convert_block_edit_signals:
            return
        jid = self._convert_current_job_id()
        if not jid:
            return
        job = self._jobs.get(jid)
        if job is None:
            return
        job.meta_artist = self.ui_cv_artist.text().strip()
        job.meta_title = self.ui_cv_song.text().strip()
        job.meta_filename_stem = self.ui_cv_stem.text().strip()

    def _convert_set_guess_status(self, job_id: str, status: str, *, tooltip: str = "") -> None:
        job = self._jobs.get(job_id)
        if job is None:
            return
        job.meta_guess_status = status
        row = self._row_index_for_job_id(self.convert_table, job_id)
        if row < 0:
            return
        it = self.convert_table.item(row, 4)
        if it is None:
            it = QTableWidgetItem(status)
            self.convert_table.setItem(row, 4, it)
        else:
            it.setText(status)
        it.setToolTip(tooltip)

    def _convert_prepare_save_payload(
        self, job_id: str, *, show_errors: bool
    ) -> tuple[UiJob, Path, str, str, str] | None:
        job = self._jobs.get(job_id)
        if job is None:
            if show_errors:
                self._show_status_message("Invalid queue row.", kind="error")
            return None
        if not self._convert_job_ready_for_metadata(job):
            if show_errors:
                self._show_status_message(
                    "This row is not ready yet. Save works after conversion finishes and the file exists.",
                    kind="info",
                )
            return None
        if self._convert_current_job_id() == job_id:
            job.meta_artist = self.ui_cv_artist.text().strip()
            job.meta_title = self.ui_cv_song.text().strip()
            job.meta_filename_stem = self.ui_cv_stem.text().strip()
        if not job.meta_filename_stem.strip():
            job.meta_filename_stem = job.out_path.stem
        return (
            job,
            job.out_path,
            job.meta_artist,
            job.meta_title,
            job.meta_filename_stem,
        )

    def _convert_apply_saved_path_to_job(self, job: UiJob, new_path: Path) -> None:
        job.out_path = new_path
        job.meta_filename_stem = new_path.stem
        self._persist_job(job)
        selected = self._convert_current_job_id()
        self._refresh_convert_table()
        target = selected or job.job_id
        self._convert_select_row_for_job_id(target)
        if target == job.job_id:
            self._convert_block_edit_signals = True
            try:
                self.ui_cv_stem.setText(job.meta_filename_stem)
                self.ui_cv_artist.setText(job.meta_artist)
                self.ui_cv_song.setText(job.meta_title)
            finally:
                self._convert_block_edit_signals = False

    def _convert_on_apply_save_worker_finished(self) -> None:
        self._convert_apply_save_worker = None
        self._convert_apply_save_job_id = None
        self.convert_apply_btn.setEnabled(True)

    def _convert_on_apply_save_ok(self, job_id: str, new_path_obj: object) -> None:
        job = self._jobs.get(job_id)
        if job is None:
            return
        new_path = new_path_obj if isinstance(new_path_obj, Path) else Path(str(new_path_obj))
        self._convert_apply_saved_path_to_job(job, new_path)
        self._convert_set_guess_status(job_id, "Saved")
        self._show_status_message(f"All set — saved “{new_path.name}”.", kind="success")
        self._status.setText("")

    def _convert_on_apply_save_fail(self, job_id: str, err: str) -> None:
        self._convert_set_guess_status(job_id, "Save failed", tooltip=err)
        self._show_status_message(err, kind="error")
        self._status.setText("")

    def _convert_apply_changes(self) -> None:
        if self._convert_apply_save_worker is not None:
            self._show_status_message("Another save is still in progress.", kind="info")
            return
        jid = self._convert_current_job_id()
        if not jid:
            self._show_status_message("Select a converted row first.", kind="info")
            return
        payload = self._convert_prepare_save_payload(jid, show_errors=True)
        if payload is None:
            return
        job, path, artist, song_name, stem = payload
        self._status.setText("Saving metadata…")
        self._convert_set_guess_status(
            job.job_id,
            "Saving",
            tooltip="Writing tags to the file (stream copy — no re-encode).",
        )
        self.convert_apply_btn.setEnabled(False)
        w = LibrarySaveWorker(
            0,
            self._settings,
            path,
            artist,
            song_name,
            stem,
            parent=self,
        )
        self._convert_apply_save_worker = w
        self._convert_apply_save_job_id = job.job_id
        w.finished_ok.connect(
            lambda _row, new_path, jid=job.job_id: self._convert_on_apply_save_ok(jid, new_path)
        )
        w.failed.connect(lambda _row, err, jid=job.job_id: self._convert_on_apply_save_fail(jid, err))
        w.finished.connect(w.deleteLater)
        w.finished.connect(self._convert_on_apply_save_worker_finished)
        w.start()

    def _convert_on_bulk_infer_option_toggled(self, _checked: bool) -> None:
        if hasattr(self, "library_skip_tagged_cb"):
            self.library_skip_tagged_cb.blockSignals(True)
            self.library_skip_tagged_cb.setChecked(self.convert_skip_tagged_cb.isChecked())
            self.library_skip_tagged_cb.blockSignals(False)
        if hasattr(self, "library_mp4_compat_cb"):
            self.library_mp4_compat_cb.blockSignals(True)
            self.library_mp4_compat_cb.setChecked(self.convert_mp4_compat_cb.isChecked())
            self.library_mp4_compat_cb.blockSignals(False)
        self._sync_widgets_to_settings()
        try:
            save_settings(self._settings, self._config_path)
        except OSError:
            pass

    def _convert_infer_groq(self) -> None:
        jid = self._convert_current_job_id()
        if not jid:
            self._show_status_message("Select a converted row first.", kind="info")
            return
        self._convert_start_infer_queue(
            [jid],
            skip_if_tagged=False,
            mp4_compat_mode=self.convert_mp4_compat_cb.isChecked(),
        )

    def _convert_infer_checked_groq(self) -> None:
        checked = self._checked_job_ids_from_queue_table(self.convert_table)
        if not checked:
            self._show_status_message("Check one or more convert rows first.", kind="info")
            return
        self._convert_start_infer_queue(
            checked,
            skip_if_tagged=self.convert_skip_tagged_cb.isChecked(),
            mp4_compat_mode=self.convert_mp4_compat_cb.isChecked(),
        )

    def _convert_infer_batch_in_progress(self) -> bool:
        return self._convert_infer_total > 0 and self._convert_infer_done < self._convert_infer_total

    def _convert_start_infer_queue(
        self,
        job_ids: list[str],
        *,
        skip_if_tagged: bool = False,
        mp4_compat_mode: bool = False,
    ) -> None:
        key = (self._settings.groq_api_key or "").strip()
        if not key:
            self._show_status_message(
                "Add your API key under Settings → AI assistant (or set GROQ_API_KEY).",
                kind="info",
            )
            return
        if self._convert_infer_batch_in_progress():
            self._show_status_message("Already asking the AI — one moment.", kind="info")
            return
        if self._convert_apply_save_worker is not None:
            self._show_status_message("A manual save is running — try again in a moment.", kind="info")
            return
        valid: list[str] = []
        skipped_not_ready = 0
        for jid in job_ids:
            job = self._jobs.get(jid)
            if job is None:
                continue
            if not self._convert_job_ready_for_metadata(job):
                skipped_not_ready += 1
                self._convert_set_guess_status(
                    jid,
                    "Skipped",
                    tooltip="Convert is not done yet, or file is missing.",
                )
                continue
            valid.append(jid)
        if not valid:
            msg = "Nothing to guess right now."
            if skipped_not_ready:
                msg = "Selected rows are not ready yet (need finished local files)."
            self._show_status_message(msg, kind="info")
            return
        self._convert_infer_skip_if_tagged = skip_if_tagged
        self._convert_infer_mp4_compat_mode = mp4_compat_mode
        self._convert_infer_pending = valid
        self._convert_infer_total = len(valid)
        self._convert_infer_done = 0
        self._convert_try_start_more_groq_infers()

    def _convert_refresh_infer_status_line(self) -> None:
        if self._convert_infer_total <= 0:
            return
        self._status.setText(
            f"Convert AI… Groq {self._convert_infer_groq_slots_busy} active, "
            f"{len(self._convert_infer_pending)} queued • "
            f"{self._convert_infer_done}/{self._convert_infer_total} finished"
        )

    def _convert_try_start_more_groq_infers(self) -> None:
        while (
            self._convert_infer_groq_slots_busy < _MAX_PARALLEL_LIBRARY_GROQ and self._convert_infer_pending
        ):
            jid = self._convert_infer_pending.pop(0)
            self._convert_start_groq_for_job(jid)

    def _convert_start_groq_for_job(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if job is None:
            return
        self._convert_set_guess_status(job_id, "Guessing")
        self._convert_infer_groq_slots_busy += 1
        self._convert_refresh_infer_status_line()
        folder_hint = job.out_path.parent.name
        w = GroqInferWorker(
            self._settings,
            row_idx=0,
            filename=job.out_path.name,
            folder_hint=folder_hint,
            path=job.out_path,
            probe_summary=job.meta_probe_summary or "",
            skip_if_existing_tags=self._convert_infer_skip_if_tagged,
            parent=self,
        )
        w.probe_cached.connect(
            lambda _row, summary, jid=job_id: self._convert_on_groq_probe_cached(jid, summary)
        )
        w.skipped.connect(
            lambda _row, reason, jid=job_id: self._convert_on_parallel_infer_skipped(jid, reason)
        )
        w.finished_ok.connect(
            lambda author, song, jid=job_id: self._convert_on_parallel_infer_ok(jid, author, song)
        )
        w.failed.connect(lambda err, jid=job_id: self._convert_on_parallel_infer_fail(jid, err))
        w.finished.connect(self._convert_on_parallel_groq_thread_finished)
        w.finished.connect(w.deleteLater)
        w.start()

    def _convert_on_groq_probe_cached(self, job_id: str, summary: str) -> None:
        job = self._jobs.get(job_id)
        if job is not None:
            job.meta_probe_summary = summary

    def _convert_on_parallel_groq_thread_finished(self) -> None:
        self._convert_infer_groq_slots_busy = max(0, self._convert_infer_groq_slots_busy - 1)
        self._convert_refresh_infer_status_line()
        self._convert_try_start_more_groq_infers()

    def _convert_bump_infer_batch_progress(self) -> None:
        if self._convert_infer_total <= 0:
            return
        self._convert_infer_done += 1
        self._convert_refresh_infer_status_line()
        if self._convert_infer_done < self._convert_infer_total:
            return
        self._show_status_message(
            f"Convert AI guessing complete ({self._convert_infer_done}/{self._convert_infer_total}).",
            kind="success",
        )
        self._status.setText("")
        self._convert_infer_total = 0
        self._convert_infer_done = 0
        self._convert_infer_pending.clear()
        self._convert_infer_skip_if_tagged = False
        self._convert_infer_mp4_compat_mode = False

    def _convert_on_parallel_infer_ok(self, job_id: str, author: str, song: str) -> None:
        payload = self._convert_prepare_save_payload(job_id, show_errors=False)
        if payload is None:
            self._convert_set_guess_status(job_id, "Save failed", tooltip="File is not ready anymore.")
            self._convert_bump_infer_batch_progress()
            return
        job, path, _artist, _song, stem = payload
        if self._convert_infer_mp4_compat_mode:
            job.meta_artist = ""
            if song and author:
                job.meta_title = f"{song} - {author}"
            elif song:
                job.meta_title = song
            elif author:
                job.meta_title = author
        else:
            if author:
                job.meta_artist = author
            if song:
                job.meta_title = song
        if self._convert_current_job_id() == job_id:
            self._convert_block_edit_signals = True
            try:
                self.ui_cv_artist.setText(job.meta_artist)
                self.ui_cv_song.setText(job.meta_title)
                self.ui_cv_stem.setText(job.meta_filename_stem)
            finally:
                self._convert_block_edit_signals = False
        self._convert_set_guess_status(
            job_id,
            "Saving",
            tooltip="Writing AI metadata to the file (stream copy — no re-encode).",
        )
        sw = LibrarySaveWorker(
            0,
            self._settings,
            path,
            job.meta_artist,
            job.meta_title,
            stem,
            parent=self,
        )
        self._convert_infer_save_workers[job_id] = sw
        sw.finished_ok.connect(
            lambda _row, new_path, jid=job_id: self._convert_on_infer_save_ok(jid, new_path)
        )
        sw.failed.connect(lambda _row, err, jid=job_id: self._convert_on_infer_save_fail(jid, err))
        sw.finished.connect(lambda jid=job_id: self._convert_infer_save_workers.pop(jid, None))
        sw.finished.connect(sw.deleteLater)
        sw.start()

    def _convert_on_parallel_infer_fail(self, job_id: str, err: str) -> None:
        self._convert_set_guess_status(job_id, "Failed", tooltip=err.strip())
        self._show_status_message(err.strip() or "Could not reach the AI.", kind="error")
        self._convert_bump_infer_batch_progress()

    def _convert_on_parallel_infer_skipped(self, job_id: str, reason: str) -> None:
        self._convert_set_guess_status(job_id, "Skipped", tooltip=reason.strip())
        self._convert_bump_infer_batch_progress()

    def _convert_on_infer_save_ok(self, job_id: str, new_path_obj: object) -> None:
        job = self._jobs.get(job_id)
        if job is None:
            self._convert_bump_infer_batch_progress()
            return
        new_path = new_path_obj if isinstance(new_path_obj, Path) else Path(str(new_path_obj))
        self._convert_apply_saved_path_to_job(job, new_path)
        self._convert_set_guess_status(job_id, "Saved")
        self._convert_bump_infer_batch_progress()

    def _convert_on_infer_save_fail(self, job_id: str, err: str) -> None:
        self._convert_set_guess_status(job_id, "Save failed", tooltip=err.strip())
        self._show_status_message(err.strip() or "Could not save file.", kind="error")
        self._convert_bump_infer_batch_progress()

    def _build_library_tab(self) -> QWidget:
        """Scrollable tab so a tall table + wrapped hint cannot force the status bar off-screen."""
        inner = QWidget()
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(0, 0, 0, 0)
        top = QHBoxLayout()
        top.addWidget(QLabel("Folder:"))
        self.ui_library_folder = QLineEdit()
        self.ui_library_folder.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.ui_library_folder.setMinimumWidth(120)
        self.ui_library_folder.setPlaceholderText("Path to a folder of videos (subfolders included)")
        if self._settings.library_last_folder is not None:
            self.ui_library_folder.setText(str(self._settings.library_last_folder))
        br = QPushButton("Choose folder…")
        br.clicked.connect(self._browse_library_folder)
        top.addWidget(self.ui_library_folder, stretch=1)
        top.addWidget(br)
        self.library_scan_btn = QPushButton("Find all videos")
        self.library_scan_btn.setToolTip(
            "Discover every video file under this folder, including nested folders."
        )
        self.library_scan_btn.clicked.connect(self._library_scan)
        top.addWidget(self.library_scan_btn)
        lay.addLayout(top)

        self.library_table = QTableWidget(0, 5)
        self.library_table.setHorizontalHeaderLabels(["", "File", "Artist", "Song", "AI status"])
        hh = self.library_table.horizontalHeader()
        hh.setStretchLastSection(True)
        hh.setSectionResizeMode(_LIB_COL_CHECK, QHeaderView.ResizeMode.Fixed)
        hh.resizeSection(_LIB_COL_CHECK, 28)
        hh.setSectionResizeMode(_LIB_COL_FILE, QHeaderView.ResizeMode.Stretch)
        self.library_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.library_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.library_table.itemSelectionChanged.connect(self._library_on_selection_changed)
        self.library_table.setMinimumHeight(96)
        self.library_table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.library_table.setSizeAdjustPolicy(QAbstractScrollArea.SizeAdjustPolicy.AdjustIgnored)
        # Fixed row height so row count does not inflate minimum tab height (fixes status bar clipping).
        vh = self.library_table.verticalHeader()
        vh.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        vh.setDefaultSectionSize(24)
        lay.addWidget(self.library_table, 1)

        edit_box = QGroupBox("This video")
        eg = QGridLayout(edit_box)
        self.ui_lib_author = QLineEdit()
        self.ui_lib_author.setPlaceholderText("Artist")
        self.ui_lib_song = QLineEdit()
        self.ui_lib_song.setPlaceholderText("Song title")
        self.ui_lib_stem = QLineEdit()
        self.ui_lib_stem.setPlaceholderText("File name (no extension — we’ll keep the same type)")
        eg.addWidget(QLabel("Artist:"), 0, 0)
        eg.addWidget(self.ui_lib_author, 0, 1)
        eg.addWidget(QLabel("Song:"), 1, 0)
        eg.addWidget(self.ui_lib_song, 1, 1)
        eg.addWidget(QLabel("Rename file to:"), 2, 0)
        eg.addWidget(self.ui_lib_stem, 2, 1)
        self.ui_lib_author.textChanged.connect(self._library_on_edit_changed)
        self.ui_lib_song.textChanged.connect(self._library_on_edit_changed)
        self.ui_lib_stem.textChanged.connect(self._library_on_edit_changed)
        lay.addWidget(edit_box)

        btn_row = QHBoxLayout()
        select_all_guess_btn = QPushButton("Select all")
        select_all_guess_btn.clicked.connect(partial(self._library_set_all_checks, True))
        btn_row.addWidget(select_all_guess_btn)
        clear_guess_btn = QPushButton("Clear selection")
        clear_guess_btn.clicked.connect(partial(self._library_set_all_checks, False))
        btn_row.addWidget(clear_guess_btn)
        self.library_bulk_rename_song_btn = QPushButton("Rename selected to Song title")
        self.library_bulk_rename_song_btn.setToolTip(
            "Uses each file's embedded metadata title and renames to '{SONG_TITLE}' while keeping extension."
        )
        self.library_bulk_rename_song_btn.clicked.connect(self._library_bulk_rename_to_song_titles)
        btn_row.addWidget(self.library_bulk_rename_song_btn)
        self.library_bulk_clear_metadata_btn = QPushButton("Clear metadata (selected)")
        self.library_bulk_clear_metadata_btn.setToolTip(
            "Removes embedded metadata from each checked file via stream copy (no re-encode)."
        )
        self.library_bulk_clear_metadata_btn.clicked.connect(self._library_bulk_clear_metadata)
        btn_row.addWidget(self.library_bulk_clear_metadata_btn)
        self.library_bulk_infer_btn = QPushButton("Guess selected")
        self.library_bulk_infer_btn.setToolTip(
            "Run AI on checked rows with up to 3 Groq requests at a time; ffprobe and saving stay off the UI "
            "thread so the window stays responsive."
        )
        self.library_bulk_infer_btn.clicked.connect(self._library_infer_checked_groq)
        btn_row.addWidget(self.library_bulk_infer_btn)
        self.library_infer_btn = QPushButton("Guess artist & song")
        self.library_infer_btn.setToolTip(
            "Fill Artist and Song using Groq from the file name and media info, then save tags to the file. "
            "Add your API key under Settings → AI assistant."
        )
        self.library_infer_btn.clicked.connect(self._library_infer_groq)
        btn_row.addWidget(self.library_infer_btn)
        self.library_apply_btn = QPushButton("Save to this file")
        self.library_apply_btn.setToolTip(
            "Write the tags into the file and rename it if you changed the name above. "
            "Uses a quick copy (no re-encode)."
        )
        self.library_apply_btn.clicked.connect(self._library_apply_changes)
        btn_row.addWidget(self.library_apply_btn)
        btn_row.addStretch()
        lay.addLayout(btn_row)

        bulk_opts = QHBoxLayout()
        self.library_skip_tagged_cb = QCheckBox(
            "Skip files that already have artist & title tags (Guess selected only)"
        )
        self.library_skip_tagged_cb.setToolTip(
            "Uses ffprobe: if both artist and title exist in the file’s metadata, that row is skipped "
            "(AI status shows Skipped). Single-row “Guess artist & song” always runs."
        )
        self.library_skip_tagged_cb.setChecked(self._settings.library_skip_bulk_infer_if_tagged)
        self.library_skip_tagged_cb.toggled.connect(self._library_on_bulk_infer_option_toggled)
        bulk_opts.addWidget(self.library_skip_tagged_cb)
        self.library_mp4_compat_cb = QCheckBox(
            "MP4 compatibility mode (Guess selected / Guess artist & song: Song becomes 'Title - Author')"
        )
        self.library_mp4_compat_cb.setToolTip(
            "AI guessing leaves Artist empty and writes Song as 'title - author'. Useful for MHI2 setups "
            "that ignore MP4 artist tags."
        )
        self.library_mp4_compat_cb.setChecked(self._settings.library_bulk_infer_mp4_compat_mode)
        self.library_mp4_compat_cb.toggled.connect(self._library_on_bulk_infer_option_toggled)
        bulk_opts.addWidget(self.library_mp4_compat_cb)
        bulk_opts.addStretch()
        lay.addLayout(bulk_opts)

        lib_hint = QLabel(
            "Pick a folder and choose Find all videos. Click a row to load details, then edit by hand, "
            "or use Guess artist & song / Guess selected — inferred tags are written to each file right away "
            "(same as Save). Configure the AI under Settings → AI assistant (or set GROQ_API_KEY)."
        )
        lib_hint.setWordWrap(True)
        lib_hint.setStyleSheet(HINT_LABEL_STYLE)
        lib_hint.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        lay.addWidget(lib_hint)

        scroll = QScrollArea()
        scroll.setWidget(inner)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setMinimumSize(0, 0)
        scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        inner.setMinimumWidth(0)
        return scroll

    def _browse_library_folder(self) -> None:
        start = self.ui_library_folder.text().strip() or str(Path.home())
        d = QFileDialog.getExistingDirectory(self, "Where are your videos?", start)
        if d:
            self.ui_library_folder.setText(d)

    def _library_scan(self) -> None:
        raw = self.ui_library_folder.text().strip()
        if not raw:
            self._show_status_message("Choose or paste a folder above first.", kind="info")
            return
        root = Path(raw).expanduser().resolve()
        if not root.is_dir():
            self._show_status_message(
                "That path isn’t a folder — check the path and try again.",
                kind="error",
            )
            return
        if self._library_scan_worker is not None and self._library_scan_worker.isRunning():
            self._show_status_message("Already scanning — please wait.", kind="info")
            return
        self._library_cancel_prefetch_probes()
        if self._library_probe_worker is not None:
            self._library_probe_worker.requestInterruption()
            self._library_probe_worker.wait(3000)
            self._library_probe_worker = None
        self._sync_widgets_to_settings()
        self._settings.library_last_folder = root
        self._library_root = root
        self._status.setText(f"Scanning folder… ({root.name})")
        self.library_scan_btn.setEnabled(False)
        w = LibraryScanWorker(root, parent=self)
        self._library_scan_worker = w
        w.finished_ok.connect(self._library_on_scan_finished)
        w.failed.connect(self._library_on_scan_failed)
        w.finished.connect(self._library_on_scan_worker_finished)
        w.start()

    def _library_on_scan_worker_finished(self) -> None:
        self._library_scan_worker = None

    def _library_on_scan_failed(self, err: str) -> None:
        self._status.setText("")
        self.library_scan_btn.setEnabled(True)
        self._show_status_message(err, kind="error")

    def _library_on_scan_finished(self, paths_as_strs: object) -> None:
        if not isinstance(paths_as_strs, list):
            self.library_scan_btn.setEnabled(True)
            self._show_status_message("Scan returned unexpected data.", kind="error")
            return
        paths = [Path(str(s)) for s in paths_as_strs]
        n = len(paths)
        self._library_rows = [LibraryFileRow(p) for p in paths]
        self._status.setText(f"Building table… ({n} video{'s' if n != 1 else ''})")
        self._library_fill_table_batched(0)

    def _library_fill_table_batched(self, start_idx: int = 0) -> None:
        batch = _LIBRARY_TABLE_FILL_BATCH_ROWS
        self._library_populating_table = True
        n = len(self._library_rows)
        if start_idx == 0:
            self.library_table.setRowCount(n)
            self._library_table_root_res = (
                self._library_root.resolve() if self._library_root is not None else None
            )
        root_resolved = getattr(self, "_library_table_root_res", None)
        end = min(start_idx + batch, n)
        for i in range(start_idx, end):
            row = self._library_rows[i]
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            chk.setCheckState(Qt.CheckState.Unchecked)
            self.library_table.setItem(i, _LIB_COL_CHECK, chk)
            if root_resolved is not None:
                try:
                    rel = str(row.path.relative_to(root_resolved))
                except ValueError:
                    rel = row.path.name
            else:
                rel = str(row.path)
            it0 = QTableWidgetItem(rel)
            it0.setData(Qt.ItemDataRole.UserRole, str(row.path))
            it0.setToolTip(str(row.path))
            self.library_table.setItem(i, _LIB_COL_FILE, it0)
            self.library_table.setItem(i, _LIB_COL_ARTIST, QTableWidgetItem(row.author))
            self.library_table.setItem(i, _LIB_COL_SONG, QTableWidgetItem(row.song_name))
            self.library_table.setItem(i, _LIB_COL_GUESS_STATUS, QTableWidgetItem(row.ai_guess_status))
        if end < n:
            QTimer.singleShot(0, lambda e=end: self._library_fill_table_batched(e))
        else:
            self._library_populating_table = False
            self.library_scan_btn.setEnabled(True)
            lib_root = self._library_root
            if lib_root is not None:
                self._status.setText(f"Library: {n} video(s) in {lib_root.name}")
                self._show_status_message(
                    f"Found {n} video{'s' if n != 1 else ''} in this folder.",
                    kind="success",
                )
                self._library_start_prefetch_probes()
            else:
                self._status.setText("")

    def _library_start_prefetch_probes(self) -> None:
        self._library_cancel_prefetch_probes()
        pending: list[int] = []
        for i, row in enumerate(self._library_rows):
            if row.probe_summary:
                continue
            pending.append(i)
        pending.reverse()
        self._library_prefetch_pending_probe_rows = pending
        self._library_prefetch_probe_total = len(pending)
        self._library_prefetch_probe_done = 0
        self._library_prefetch_probe_failures = 0
        if not pending:
            return
        self._library_try_start_more_prefetch_probes()

    def _library_cancel_prefetch_probes(self) -> None:
        self._library_prefetch_pending_probe_rows = []
        workers = list(self._library_prefetch_probe_workers.values())
        self._library_prefetch_probe_workers.clear()
        for w in workers:
            if w.isRunning():
                w.requestInterruption()
                w.wait(3000)
        self._library_prefetch_probe_total = 0
        self._library_prefetch_probe_done = 0
        self._library_prefetch_probe_failures = 0

    def _library_try_start_more_prefetch_probes(self) -> None:
        while (
            len(self._library_prefetch_probe_workers) < _MAX_PARALLEL_LIBRARY_PREFETCH_PROBES
            and self._library_prefetch_pending_probe_rows
        ):
            row_idx = self._library_prefetch_pending_probe_rows.pop()
            if row_idx < 0 or row_idx >= len(self._library_rows):
                continue
            path = self._library_rows[row_idx].path
            w = LibraryProbeWorker(row_idx, path, root=self._library_root, parent=self)
            self._library_prefetch_probe_workers[row_idx] = w
            w.finished_ok.connect(self._library_on_prefetch_probe_ok)
            w.failed.connect(self._library_on_prefetch_probe_fail)
            w.finished.connect(self._library_on_prefetch_probe_worker_finished)
            w.start()

    def _library_on_prefetch_probe_worker_finished(self) -> None:
        sender_obj = self.sender()
        if sender_obj is None:
            return
        for row_idx, worker in list(self._library_prefetch_probe_workers.items()):
            if worker is sender_obj:
                self._library_prefetch_probe_workers.pop(row_idx, None)
                break
        self._library_try_start_more_prefetch_probes()

    def _library_on_prefetch_probe_ok(
        self, row_idx: int, author: str, title: str, summary: str, rel_display: str
    ) -> None:
        self._library_apply_probe_result(row_idx, author, title, summary, rel_display)
        self._library_prefetch_probe_done += 1
        self._library_finish_prefetch_if_complete()

    def _library_on_prefetch_probe_fail(self, _row_idx: int, _err: str) -> None:
        self._library_prefetch_probe_failures += 1
        self._library_prefetch_probe_done += 1
        self._library_finish_prefetch_if_complete()

    def _library_finish_prefetch_if_complete(self) -> None:
        total = self._library_prefetch_probe_total
        done = self._library_prefetch_probe_done
        if total <= 0 or done < total:
            return
        if self._library_prefetch_probe_failures > 0:
            self._show_status_message(
                f"Loaded metadata for most files ({total - self._library_prefetch_probe_failures}/{total}).",
                kind="info",
            )
        self._library_prefetch_pending_probe_rows = []
        self._library_prefetch_probe_total = 0
        self._library_prefetch_probe_done = 0
        self._library_prefetch_probe_failures = 0

    def _library_checked_row_indices(self) -> list[int]:
        out: list[int] = []
        for i in range(self.library_table.rowCount()):
            chk = self.library_table.item(i, _LIB_COL_CHECK)
            if chk is not None and chk.checkState() == Qt.CheckState.Checked:
                out.append(i)
        return out

    def _library_set_all_checks(self, checked: bool) -> None:
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for i in range(self.library_table.rowCount()):
            chk = self.library_table.item(i, _LIB_COL_CHECK)
            if chk is not None:
                chk.setCheckState(state)

    def _library_set_guess_status(self, row_idx: int, status: str, *, tooltip: str = "") -> None:
        if row_idx < 0 or row_idx >= len(self._library_rows):
            return
        row = self._library_rows[row_idx]
        row.ai_guess_status = status
        st_item = self.library_table.item(row_idx, _LIB_COL_GUESS_STATUS)
        if st_item is None:
            st_item = QTableWidgetItem(status)
            self.library_table.setItem(row_idx, _LIB_COL_GUESS_STATUS, st_item)
        else:
            st_item.setText(status)
        st_item.setToolTip(tooltip)

    def _library_current_row_index(self) -> int:
        r = self.library_table.currentRow()
        if r < 0 or r >= len(self._library_rows):
            return -1
        return r

    def _library_on_selection_changed(self) -> None:
        if self._library_populating_table:
            return
        r = self._library_current_row_index()
        if r < 0:
            return
        row = self._library_rows[r]
        self._library_block_edit_signals = True
        try:
            self.ui_lib_author.setText(row.author)
            self.ui_lib_song.setText(row.song_name)
            self.ui_lib_stem.setText(row.filename_stem)
        finally:
            self._library_block_edit_signals = False
        self._library_start_probe(r)

    def _library_start_probe(self, row_idx: int) -> None:
        if self._library_probe_worker is not None:
            self._library_probe_worker.requestInterruption()
            self._library_probe_worker.wait(3000)
            self._library_probe_worker = None
        if row_idx < 0 or row_idx >= len(self._library_rows):
            return
        path = self._library_rows[row_idx].path
        self._status.setText(f"Reading {path.name}…")
        w = LibraryProbeWorker(row_idx, path, root=self._library_root, parent=self)
        self._library_probe_worker = w
        w.finished_ok.connect(self._library_on_probe_ok)
        w.failed.connect(self._library_on_probe_fail)
        w.finished.connect(self._library_probe_finished_cleanup)
        w.start()

    def _library_probe_finished_cleanup(self) -> None:
        self._library_probe_worker = None

    def _library_on_probe_ok(
        self, row_idx: int, author: str, title: str, summary: str, rel_display: str
    ) -> None:
        if row_idx < 0 or row_idx >= len(self._library_rows):
            return
        self._library_apply_probe_result(row_idx, author, title, summary, rel_display)
        row = self._library_rows[row_idx]
        if self.library_table.currentRow() == row_idx and not self._library_block_edit_signals:
            self._library_block_edit_signals = True
            try:
                self.ui_lib_author.setText(row.author)
                self.ui_lib_song.setText(row.song_name)
            finally:
                self._library_block_edit_signals = False
        self._status.setText(f"Ready — {row.path.name}")

    def _library_apply_probe_result(
        self, row_idx: int, author: str, title: str, summary: str, rel_display: str
    ) -> None:
        if row_idx < 0 or row_idx >= len(self._library_rows):
            return
        row = self._library_rows[row_idx]
        row.probe_summary = summary
        if author and not row.author:
            row.author = author
        if title and not row.song_name:
            row.song_name = title
        it0 = self.library_table.item(row_idx, _LIB_COL_FILE)
        if it0 is not None:
            it0.setText(rel_display)
            it0.setToolTip(str(row.path))
        it_a = self.library_table.item(row_idx, _LIB_COL_ARTIST)
        it_s = self.library_table.item(row_idx, _LIB_COL_SONG)
        if it_a is not None:
            it_a.setText(row.author)
        if it_s is not None:
            it_s.setText(row.song_name)

    def _library_on_probe_fail(self, row_idx: int, err: str) -> None:
        self._status.setText("")
        self._show_status_message(f"Could not read media info: {err}", kind="error")

    def _library_on_edit_changed(self, *_args: object) -> None:
        if self._library_block_edit_signals:
            return
        r = self._library_current_row_index()
        if r < 0:
            return
        row = self._library_rows[r]
        row.author = self.ui_lib_author.text().strip()
        row.song_name = self.ui_lib_song.text().strip()
        row.filename_stem = self.ui_lib_stem.text().strip()
        a_item = self.library_table.item(r, _LIB_COL_ARTIST)
        s_item = self.library_table.item(r, _LIB_COL_SONG)
        if a_item is not None:
            a_item.setText(row.author)
        if s_item is not None:
            s_item.setText(row.song_name)

    def _library_on_bulk_infer_option_toggled(self, _checked: bool) -> None:
        if hasattr(self, "convert_skip_tagged_cb"):
            self.convert_skip_tagged_cb.blockSignals(True)
            self.convert_skip_tagged_cb.setChecked(self.library_skip_tagged_cb.isChecked())
            self.convert_skip_tagged_cb.blockSignals(False)
        if hasattr(self, "convert_mp4_compat_cb"):
            self.convert_mp4_compat_cb.blockSignals(True)
            self.convert_mp4_compat_cb.setChecked(self.library_mp4_compat_cb.isChecked())
            self.convert_mp4_compat_cb.blockSignals(False)
        self._sync_widgets_to_settings()
        try:
            save_settings(self._settings, self._config_path)
        except OSError:
            pass

    def _library_infer_groq(self) -> None:
        r = self._library_current_row_index()
        if r < 0:
            self._show_status_message("Select a video in the list first.", kind="info")
            return
        self._library_start_infer_queue(
            [r],
            skip_if_tagged=False,
            mp4_compat_mode=self.library_mp4_compat_cb.isChecked(),
        )

    def _library_infer_checked_groq(self) -> None:
        checked = self._library_checked_row_indices()
        if not checked:
            self._show_status_message("Check one or more rows first.", kind="info")
            return
        self._library_start_infer_queue(
            checked,
            skip_if_tagged=self.library_skip_tagged_cb.isChecked(),
            mp4_compat_mode=self.library_mp4_compat_cb.isChecked(),
        )

    def _library_infer_batch_in_progress(self) -> bool:
        return self._library_infer_total > 0 and self._library_infer_done < self._library_infer_total

    def _library_start_infer_queue(
        self,
        row_indices: list[int],
        *,
        skip_if_tagged: bool = False,
        mp4_compat_mode: bool = False,
    ) -> None:
        self._sync_widgets_to_settings()
        key = (self._settings.groq_api_key or "").strip()
        if not key:
            self._show_status_message(
                "Add your API key under Settings → AI assistant (or set GROQ_API_KEY).",
                kind="info",
            )
            return
        if self._library_infer_batch_in_progress():
            self._show_status_message("Already asking the AI — one moment.", kind="info")
            return
        valid = [i for i in row_indices if 0 <= i < len(self._library_rows)]
        if not valid:
            self._show_status_message("Nothing to guess right now.", kind="info")
            return
        self._library_infer_skip_if_tagged = skip_if_tagged
        self._library_infer_mp4_compat_mode = mp4_compat_mode
        self._library_infer_pending = valid
        self._library_infer_total = len(valid)
        self._library_infer_done = 0
        self._library_try_start_more_groq_infers()

    def _library_refresh_infer_status_line(self) -> None:
        if self._library_infer_total <= 0:
            return
        self._status.setText(
            f"Library AI… Groq {self._library_infer_groq_slots_busy} active, "
            f"{len(self._library_infer_pending)} queued • "
            f"{self._library_infer_done}/{self._library_infer_total} finished"
        )

    def _library_try_start_more_groq_infers(self) -> None:
        while (
            self._library_infer_groq_slots_busy < _MAX_PARALLEL_LIBRARY_GROQ and self._library_infer_pending
        ):
            row_idx = self._library_infer_pending.pop(0)
            self._library_start_groq_for_row(row_idx)

    def _library_bump_infer_batch_progress(self) -> None:
        """One library row finished the infer pipeline (probe skip, Groq fail, or save done)."""
        if self._library_infer_total <= 0:
            return
        self._library_infer_done += 1
        self._library_refresh_infer_status_line()
        self._library_finish_infer_batch_if_complete()

    def _library_on_groq_probe_cached(self, row_idx: int, summary: str) -> None:
        if 0 <= row_idx < len(self._library_rows):
            self._library_rows[row_idx].probe_summary = summary

    def _library_start_groq_for_row(self, row_idx: int) -> None:
        row = self._library_rows[row_idx]
        folder_hint = ""
        try:
            if self._library_root is not None:
                folder_hint = str(row.path.parent.resolve().relative_to(self._library_root.resolve()))
            else:
                folder_hint = row.path.parent.name
        except ValueError:
            folder_hint = row.path.parent.name
        self._library_set_guess_status(row_idx, "Guessing")
        self._library_infer_groq_slots_busy += 1
        self._library_refresh_infer_status_line()
        w = GroqInferWorker(
            self._settings,
            row_idx=row_idx,
            filename=row.path.name,
            folder_hint=folder_hint,
            path=row.path,
            probe_summary=row.probe_summary or "",
            skip_if_existing_tags=self._library_infer_skip_if_tagged,
            parent=self,
        )
        w.probe_cached.connect(self._library_on_groq_probe_cached)
        w.skipped.connect(self._library_on_parallel_infer_skipped)
        w.finished_ok.connect(partial(self._library_on_parallel_infer_ok, row_idx))
        w.failed.connect(partial(self._library_on_parallel_infer_fail, row_idx))
        w.finished.connect(self._library_on_parallel_groq_thread_finished)
        w.start()

    def _library_on_parallel_groq_thread_finished(self) -> None:
        self._library_infer_groq_slots_busy = max(0, self._library_infer_groq_slots_busy - 1)
        self._library_refresh_infer_status_line()
        self._library_try_start_more_groq_infers()

    def _library_on_parallel_infer_ok(self, row_idx: int, author: str, song: str) -> None:
        r = row_idx
        if r < 0 or r >= len(self._library_rows):
            return
        row = self._library_rows[r]
        if self._library_infer_mp4_compat_mode:
            row.author = ""
            if song and author:
                row.song_name = f"{song} - {author}"
            elif song:
                row.song_name = song
            elif author:
                row.song_name = author
        else:
            if author:
                row.author = author
            if song:
                row.song_name = song
        if self.library_table.currentRow() == r:
            self._library_block_edit_signals = True
            try:
                self.ui_lib_author.setText(row.author)
                self.ui_lib_song.setText(row.song_name)
            finally:
                self._library_block_edit_signals = False
        a_item = self.library_table.item(r, _LIB_COL_ARTIST)
        s_item = self.library_table.item(r, _LIB_COL_SONG)
        if a_item is not None:
            a_item.setText(row.author)
        if s_item is not None:
            s_item.setText(row.song_name)
        payload = self._library_prepare_save_payload(r, show_errors=True)
        if payload is None:
            self._library_set_guess_status(r, "Save failed", tooltip="Could not read row or file is missing.")
            self._library_bump_infer_batch_progress()
            return
        path, art, song_name, stem = payload
        self._library_refresh_infer_status_line()
        self._library_set_guess_status(
            r,
            "Saving",
            tooltip="Writing tags to the file (stream copy — no re-encode).",
        )
        sw = LibrarySaveWorker(
            r,
            self._settings,
            path,
            art,
            song_name,
            stem,
            parent=self,
        )
        sw.finished_ok.connect(self._library_on_infer_save_ok)
        sw.failed.connect(self._library_on_infer_save_fail)
        sw.finished.connect(sw.deleteLater)
        sw.start()

    def _library_on_parallel_infer_fail(self, row_idx: int, err: str) -> None:
        self._library_set_guess_status(row_idx, "Failed", tooltip=err.strip())
        self._show_status_message(err.strip() or "Could not reach the AI.", kind="error")
        self._library_bump_infer_batch_progress()

    def _library_on_parallel_infer_skipped(self, row_idx: int, reason: str) -> None:
        self._library_set_guess_status(row_idx, "Skipped", tooltip=reason.strip())
        self._library_bump_infer_batch_progress()

    def _library_finish_infer_batch_if_complete(self) -> None:
        if self._library_infer_total <= 0:
            return
        if self._library_infer_done < self._library_infer_total:
            return
        self._show_status_message(
            f"AI guessing complete ({self._library_infer_done}/{self._library_infer_total}).",
            kind="success",
        )
        self._status.setText("")
        self._library_infer_total = 0
        self._library_infer_done = 0
        self._library_infer_pending.clear()
        self._library_infer_skip_if_tagged = False
        self._library_infer_mp4_compat_mode = False

    def _library_prepare_save_payload(
        self, r: int, *, show_errors: bool
    ) -> tuple[Path, str, str, str] | None:
        """Gather path and tag fields on the main thread before starting ``LibrarySaveWorker``."""
        self._sync_widgets_to_settings()
        if r < 0 or r >= len(self._library_rows):
            msg = "Invalid row."
            if show_errors:
                self._show_status_message(msg, kind="error")
            return None
        row = self._library_rows[r]
        if self.library_table.currentRow() == r:
            row.author = self.ui_lib_author.text().strip()
            row.song_name = self.ui_lib_song.text().strip()
            row.filename_stem = self.ui_lib_stem.text().strip()
        path = row.path
        if not path.is_file():
            msg = "That file isn’t there anymore — try scanning the folder again."
            if show_errors:
                self._show_status_message(msg, kind="error")
            return None
        return path, row.author, row.song_name, row.filename_stem

    def _library_apply_saved_path_to_ui(self, r: int, new_path: Path) -> None:
        row = self._library_rows[r]
        row.path = new_path
        row.filename_stem = new_path.stem
        it0 = self.library_table.item(r, _LIB_COL_FILE)
        if it0 is not None:
            it0.setData(Qt.ItemDataRole.UserRole, str(new_path))
            if self._library_root is not None:
                try:
                    rel = str(new_path.resolve().relative_to(self._library_root.resolve()))
                except ValueError:
                    rel = new_path.name
            else:
                rel = new_path.name
            it0.setText(rel)
            it0.setToolTip(str(new_path))
        if self.library_table.currentRow() == r:
            self._library_block_edit_signals = True
            try:
                self.ui_lib_stem.setText(row.filename_stem)
            finally:
                self._library_block_edit_signals = False
        it_a = self.library_table.item(r, _LIB_COL_ARTIST)
        it_s = self.library_table.item(r, _LIB_COL_SONG)
        if it_a is not None:
            it_a.setText(row.author)
        if it_s is not None:
            it_s.setText(row.song_name)

    def _library_on_infer_save_ok(self, r: int, new_path_obj: object) -> None:
        new_path = new_path_obj if isinstance(new_path_obj, Path) else Path(new_path_obj)
        self._library_apply_saved_path_to_ui(r, new_path)
        bits = [x for x in (self._library_rows[r].author, self._library_rows[r].song_name) if x]
        if bits:
            self._library_set_guess_status(r, "Saved")
        else:
            self._library_set_guess_status(
                r,
                "Saved",
                tooltip="AI returned empty artist/title; file was updated anyway.",
            )
        self._library_bump_infer_batch_progress()

    def _library_on_infer_save_fail(self, r: int, err: str) -> None:
        self._library_set_guess_status(r, "Save failed", tooltip=err)
        self._show_status_message(err, kind="error")
        self._library_bump_infer_batch_progress()

    def _library_on_apply_save_worker_finished(self) -> None:
        self._library_apply_save_worker = None
        self.library_apply_btn.setEnabled(True)

    def _library_on_apply_save_ok(self, r: int, new_path_obj: object) -> None:
        new_path = new_path_obj if isinstance(new_path_obj, Path) else Path(new_path_obj)
        self._library_apply_saved_path_to_ui(r, new_path)
        self._library_set_guess_status(r, "Saved")
        self._show_status_message(f"All set — saved “{new_path.name}”.", kind="success")
        self._status.setText("")

    def _library_on_apply_save_fail(self, r: int, err: str) -> None:
        self._library_set_guess_status(r, "Save failed", tooltip=err)
        self._show_status_message(err, kind="error")
        self._status.setText("")

    def _library_apply_changes(self) -> None:
        if self._library_apply_save_worker is not None:
            self._show_status_message("Another save is still in progress.", kind="info")
            return
        r = self._library_current_row_index()
        if r < 0:
            self._show_status_message("Select a video in the list first.", kind="info")
            return
        payload = self._library_prepare_save_payload(r, show_errors=True)
        if payload is None:
            return
        path, author, song_name, stem = payload
        self._status.setText("Saving…")
        self._library_set_guess_status(
            r,
            "Saving",
            tooltip="Writing tags to the file (stream copy — no re-encode).",
        )
        self.library_apply_btn.setEnabled(False)
        w = LibrarySaveWorker(
            r,
            self._settings,
            path,
            author,
            song_name,
            stem,
            parent=self,
        )
        self._library_apply_save_worker = w
        w.finished_ok.connect(self._library_on_apply_save_ok)
        w.failed.connect(self._library_on_apply_save_fail)
        w.finished.connect(w.deleteLater)
        w.finished.connect(self._library_on_apply_save_worker_finished)
        w.start()

    def _library_bulk_rename_to_song_titles(self) -> None:
        if self._library_bulk_rename_worker is not None:
            self._show_status_message("Bulk rename is already in progress.", kind="info")
            return
        checked = self._library_checked_row_indices()
        if not checked:
            self._show_status_message("Check one or more rows first.", kind="info")
            return
        targets: list[tuple[int, Path]] = []
        for idx in checked:
            if 0 <= idx < len(self._library_rows):
                targets.append((idx, self._library_rows[idx].path))
                self._library_set_guess_status(
                    idx,
                    "Renaming",
                    tooltip="Reading metadata title and renaming the file.",
                )
        if not targets:
            self._show_status_message("Nothing to rename right now.", kind="info")
            return
        self.library_bulk_rename_song_btn.setEnabled(False)
        self._status.setText(f"Bulk rename… {len(targets)} file(s)")
        w = LibraryBulkRenameToSongWorker(targets, parent=self)
        self._library_bulk_rename_worker = w
        w.row_renamed.connect(self._library_on_bulk_rename_row_renamed)
        w.row_skipped.connect(self._library_on_bulk_rename_row_skipped)
        w.row_failed.connect(self._library_on_bulk_rename_row_failed)
        w.batch_done.connect(self._library_on_bulk_rename_batch_done)
        w.finished.connect(w.deleteLater)
        w.finished.connect(self._library_on_bulk_rename_worker_finished)
        w.start()

    def _library_on_bulk_rename_row_renamed(self, row_idx: int, new_path_obj: object, song: str) -> None:
        if row_idx < 0 or row_idx >= len(self._library_rows):
            return
        row = self._library_rows[row_idx]
        row.song_name = (song or "").strip()
        new_path = new_path_obj if isinstance(new_path_obj, Path) else Path(str(new_path_obj))
        self._library_apply_saved_path_to_ui(row_idx, new_path)
        if self.library_table.currentRow() == row_idx:
            self._library_block_edit_signals = True
            try:
                self.ui_lib_song.setText(row.song_name)
            finally:
                self._library_block_edit_signals = False
        self._library_set_guess_status(row_idx, "Renamed")

    def _library_on_bulk_rename_row_skipped(self, row_idx: int, reason: str) -> None:
        self._library_set_guess_status(row_idx, "Skipped", tooltip=reason.strip())

    def _library_on_bulk_rename_row_failed(self, row_idx: int, err: str) -> None:
        self._library_set_guess_status(row_idx, "Rename failed", tooltip=err.strip())

    def _library_on_bulk_rename_batch_done(self, renamed: int, skipped: int, failed: int) -> None:
        self._status.setText("")
        total = renamed + skipped + failed
        self._show_status_message(
            f"Bulk rename complete ({renamed} renamed, {skipped} skipped, {failed} failed; {total} total).",
            kind="success" if failed == 0 else "info",
        )

    def _library_on_bulk_rename_worker_finished(self) -> None:
        self._library_bulk_rename_worker = None
        self.library_bulk_rename_song_btn.setEnabled(True)

    def _library_bulk_clear_metadata(self) -> None:
        if self._library_bulk_clear_metadata_worker is not None:
            self._show_status_message("Bulk metadata clear is already in progress.", kind="info")
            return
        checked = self._library_checked_row_indices()
        if not checked:
            self._show_status_message("Check one or more rows first.", kind="info")
            return
        targets: list[tuple[int, Path]] = []
        skipped_empty = 0
        for idx in checked:
            if 0 <= idx < len(self._library_rows):
                row = self._library_rows[idx]
                if not row.author.strip() and not row.song_name.strip():
                    skipped_empty += 1
                    self._library_set_guess_status(
                        idx,
                        "Skipped",
                        tooltip="Artist and Song are empty in the table.",
                    )
                    continue
                targets.append((idx, row.path))
                self._library_set_guess_status(
                    idx,
                    "Clearing metadata",
                    tooltip="Removing embedded metadata from this file.",
                )
        if not targets:
            if skipped_empty > 0:
                self._show_status_message(
                    "Nothing to clear: selected rows have empty Artist and Song.",
                    kind="info",
                )
            else:
                self._show_status_message("Nothing to clear right now.", kind="info")
            return
        self.library_bulk_clear_metadata_btn.setEnabled(False)
        self._status.setText(f"Clearing metadata… {len(targets)} file(s)")
        w = LibraryBulkClearMetadataWorker(targets, self._settings, parent=self)
        self._library_bulk_clear_metadata_worker = w
        w.row_cleared.connect(self._library_on_bulk_clear_metadata_row_cleared)
        w.row_failed.connect(self._library_on_bulk_clear_metadata_row_failed)
        w.batch_done.connect(self._library_on_bulk_clear_metadata_batch_done)
        w.finished.connect(w.deleteLater)
        w.finished.connect(self._library_on_bulk_clear_metadata_worker_finished)
        w.start()

    def _library_on_bulk_clear_metadata_row_cleared(self, row_idx: int) -> None:
        if row_idx < 0 or row_idx >= len(self._library_rows):
            return
        row = self._library_rows[row_idx]
        row.author = ""
        row.song_name = ""
        row.probe_summary = ""
        it_a = self.library_table.item(row_idx, _LIB_COL_ARTIST)
        it_s = self.library_table.item(row_idx, _LIB_COL_SONG)
        if it_a is not None:
            it_a.setText("")
        if it_s is not None:
            it_s.setText("")
        if self.library_table.currentRow() == row_idx:
            self._library_block_edit_signals = True
            try:
                self.ui_lib_author.setText("")
                self.ui_lib_song.setText("")
            finally:
                self._library_block_edit_signals = False
        self._library_set_guess_status(row_idx, "Metadata cleared")

    def _library_on_bulk_clear_metadata_row_failed(self, row_idx: int, err: str) -> None:
        self._library_set_guess_status(row_idx, "Clear failed", tooltip=err.strip())

    def _library_on_bulk_clear_metadata_batch_done(self, cleared: int, failed: int) -> None:
        self._status.setText("")
        total = cleared + failed
        self._show_status_message(
            f"Metadata clear complete ({cleared} cleared, {failed} failed; {total} total).",
            kind="success" if failed == 0 else "info",
        )

    def _library_on_bulk_clear_metadata_worker_finished(self) -> None:
        self._library_bulk_clear_metadata_worker = None
        self.library_bulk_clear_metadata_btn.setEnabled(True)

    @staticmethod
    def _row_index_for_job_id(table: QTableWidget, job_id: str) -> int:
        for i in range(table.rowCount()):
            it = table.item(i, 1)
            if it is not None and it.data(Qt.ItemDataRole.UserRole) == job_id:
                return i
        return -1

    def _checked_job_ids_from_queue_table(self, table: QTableWidget) -> list[str]:
        out: list[str] = []
        for i in range(table.rowCount()):
            chk = table.item(i, 0)
            if chk is None or chk.checkState() != Qt.CheckState.Checked:
                continue
            title_it = table.item(i, 1)
            if title_it is None:
                continue
            jid = title_it.data(Qt.ItemDataRole.UserRole)
            if isinstance(jid, str):
                out.append(jid)
        return out

    def _set_all_queue_checks(self, which: str, checked: bool) -> None:
        table = self.downloads_table if which == "downloads" else self.convert_table
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for i in range(table.rowCount()):
            it = table.item(i, 0)
            if it is not None:
                it.setCheckState(state)

    def _cancel_selected_downloads(self) -> None:
        ids = self._checked_job_ids_from_queue_table(self.downloads_table)
        if not ids:
            QMessageBox.information(self, "Cancel", "Check one or more download rows first.")
            return
        n = 0
        for jid in ids:
            job = self._jobs.get(jid)
            if job and job.download_status in ("queued", "downloading"):
                self._dl.cancel_download(jid)
                n += 1
        if n == 0:
            QMessageBox.information(
                self,
                "Cancel",
                "None of the checked rows are queued or downloading (cancel is only for active downloads).",
            )
        else:
            self._status.setText(f"Cancelled download for {n} job(s).")

    def _cancel_selected_converts(self) -> None:
        ids = self._checked_job_ids_from_queue_table(self.convert_table)
        if not ids:
            QMessageBox.information(self, "Cancel", "Check one or more convert rows first.")
            return
        n = 0
        for jid in ids:
            job = self._jobs.get(jid)
            if job and job.convert_status in ("queued", "waiting", "converting"):
                self._cv.cancel_convert(jid)
                n += 1
        if n == 0:
            QMessageBox.information(
                self,
                "Cancel",
                "None of the checked rows are queued or converting.",
            )
        else:
            self._status.setText(f"Cancelled conversion for {n} job(s).")

    def _remove_selected_queue_jobs(self, which: str) -> None:
        table = self.downloads_table if which == "downloads" else self.convert_table
        ids = self._checked_job_ids_from_queue_table(table)
        if not ids:
            QMessageBox.information(self, "Remove", "Check one or more rows first.")
            return
        titles = [self._jobs[j].candidate.title for j in ids if j in self._jobs]
        preview = "\n".join(f"• {t}" for t in titles[:8])
        more = f"\n… and {len(titles) - 8} more" if len(titles) > 8 else ""
        ent_word = "entry" if len(ids) == 1 else "entries"
        if (
            QMessageBox.question(
                self,
                "Remove from queue",
                f"Remove {len(ids)} {ent_word} from the download/convert queue?\n\n"
                f"{preview}{more}\n\n"
                "Files already on disk are not deleted.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self._purge_jobs_from_queue(ids)

    def _purge_jobs_from_queue(self, job_ids: list[str]) -> None:
        for job_id in job_ids:
            job = self._jobs.get(job_id)
            if not job:
                continue
            need_cancel_dl = job.download_status in ("queued", "downloading")
            need_cancel_cv = job.download_status == "done" and job.convert_status in (
                "queued",
                "waiting",
                "converting",
            )
            rid = (job.remote_job_id or "").strip()
            if job.backend == "remote" and rid:
                if self._remote is None:
                    self._status.setText("Remote backend unavailable; cannot remove job from server.")
                    continue
                if not self._remote.delete_server_job_sync(rid):
                    continue
                self._remote.reset_local_tracking(job_id)
                self._remote.clear_pending(job_id)
                need_cancel_dl = False
                need_cancel_cv = False
            self._job_order = [j for j in self._job_order if j != job_id]
            self._jobs.pop(job_id, None)
            self._store.delete(job_id)
            if need_cancel_dl:
                self._dl.cancel_download(job_id)
            if need_cancel_cv:
                self._cv.cancel_convert(job_id)
        self._refresh_downloads_table()
        self._refresh_convert_table()

    @staticmethod
    def _set_table_text(table: QTableWidget, row: int, col: int, text: str) -> None:
        it = table.item(row, col)
        if it is None:
            it = QTableWidgetItem(text)
            table.setItem(row, col, it)
        else:
            it.setText(text)

    def _downloads_status_progress_speed(self, job: UiJob) -> tuple[str, str, str]:
        st = job.download_status
        if st == "downloading":
            detail = "Downloading"
        elif st == "queued":
            detail = "Queued"
        elif st == "failed":
            detail = job.download_error or "Failed"
        elif st == "cancelled":
            detail = "Cancelled"
        else:
            detail = st
        pct = job.download_percent
        if pct < 0:
            prog_txt = "—" if st != "downloading" else "…"
        else:
            prog_txt = f"{pct:.1f}%"
        extra = f"{job.download_speed}  {job.download_eta}".strip()
        return detail, prog_txt, extra

    def _convert_status_progress(self, job: UiJob) -> tuple[str, str]:
        if job.backend == "remote" and job.convert_status == "done":
            if job.remote_fetch_in_progress:
                if job.remote_fetch_bytes_total > 0:
                    pct = (job.remote_fetch_bytes_done / job.remote_fetch_bytes_total) * 100.0
                    pct = max(0.0, min(100.0, pct))
                    return "Saving to PC...", f"{pct:.1f}%"
                return "Saving to PC...", "..."
            if not job.remote_saved_locally:
                return "Done on server — save to PC", "100.0%"
            return "Saved to PC", "100.0%"
        st = job.convert_status
        if st == "converting":
            detail = "Converting"
        elif st in ("queued", "waiting"):
            detail = "Queued"
        elif st == "failed":
            detail = job.convert_error or "Failed"
        elif st == "cancelled":
            detail = "Cancelled"
        elif st == "done":
            detail = "Done"
        else:
            detail = st
        if job.convert_indeterminate or job.convert_percent < 0.0:
            prog_txt = "…"
        else:
            prog_txt = f"{job.convert_percent:.1f}%"
        return detail, prog_txt

    def _refresh_downloads_table(self) -> None:
        rows = [self._jobs[j] for j in self._job_order if self._jobs[j].download_status != "done"]
        self.downloads_table.setRowCount(len(rows))
        for i, job in enumerate(rows):
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            chk.setCheckState(Qt.CheckState.Unchecked)
            self.downloads_table.setItem(i, 0, chk)
            title_it = QTableWidgetItem(job.candidate.title)
            title_it.setData(Qt.ItemDataRole.UserRole, job.job_id)
            self.downloads_table.setItem(i, 1, title_it)
            d1, d2, d3 = self._downloads_status_progress_speed(job)
            self.downloads_table.setItem(i, 2, QTableWidgetItem(d1))
            self.downloads_table.setItem(i, 3, QTableWidgetItem(d2))
            self.downloads_table.setItem(i, 4, QTableWidgetItem(d3))
            self.downloads_table.setCellWidget(i, 5, self._download_actions_widget(job))

    def _download_actions_widget(self, job: UiJob) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(2, 0, 2, 0)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setEnabled(job.download_status in ("queued", "downloading"))
        cancel_btn.clicked.connect(lambda *, jid=job.job_id: self._cancel_download_for_job(jid))
        restart_btn = QPushButton("Restart")
        restart_btn.setEnabled(job.download_status in ("failed", "cancelled"))
        restart_btn.clicked.connect(lambda *, jid=job.job_id: self._restart_download_for_job(jid))
        remove_btn = QPushButton("Remove")
        remove_btn.setToolTip("Remove this entry from the queue (cancels an active download if needed).")
        remove_btn.clicked.connect(lambda *, jid=job.job_id: self._remove_job_from_queue(jid))
        h.addWidget(cancel_btn)
        h.addWidget(restart_btn)
        h.addWidget(remove_btn)
        return w

    def _refresh_convert_table(self) -> None:
        selected_job_id = self._convert_current_job_id()
        # _job_order is append-only (oldest first); reverse it so the newest job is on top.
        rows = [self._jobs[j] for j in reversed(self._job_order) if self._jobs[j].download_status == "done"]
        self.convert_table.blockSignals(True)
        self.convert_table.setRowCount(len(rows))
        for i, job in enumerate(rows):
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            chk.setCheckState(Qt.CheckState.Unchecked)
            self.convert_table.setItem(i, 0, chk)
            title_it = QTableWidgetItem(job.candidate.title)
            title_it.setData(Qt.ItemDataRole.UserRole, job.job_id)
            self.convert_table.setItem(i, 1, title_it)
            d1, d2 = self._convert_status_progress(job)
            self.convert_table.setItem(i, 2, QTableWidgetItem(d1))
            self.convert_table.setItem(i, 3, QTableWidgetItem(d2))
            self.convert_table.setItem(i, 4, QTableWidgetItem(job.meta_guess_status or "None"))
            self.convert_table.setCellWidget(i, 5, self._convert_actions_widget(job))
        self.convert_table.blockSignals(False)
        if selected_job_id:
            self._convert_select_row_for_job_id(selected_job_id)

    def _update_downloads_row_cells(self, job: UiJob) -> None:
        row = self._row_index_for_job_id(self.downloads_table, job.job_id)
        if row < 0:
            self._refresh_downloads_table()
            return
        d1, d2, d3 = self._downloads_status_progress_speed(job)
        self._set_table_text(self.downloads_table, row, 2, d1)
        self._set_table_text(self.downloads_table, row, 3, d2)
        self._set_table_text(self.downloads_table, row, 4, d3)

    def _update_convert_row_cells(self, job: UiJob) -> None:
        row = self._row_index_for_job_id(self.convert_table, job.job_id)
        if row < 0:
            self._refresh_convert_table()
            return
        d1, d2 = self._convert_status_progress(job)
        self._set_table_text(self.convert_table, row, 2, d1)
        self._set_table_text(self.convert_table, row, 3, d2)
        self._set_table_text(self.convert_table, row, 4, job.meta_guess_status or "None")

    def _convert_actions_widget(self, job: UiJob) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(2, 0, 2, 0)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setEnabled(job.convert_status in ("queued", "waiting", "converting"))
        cancel_btn.clicked.connect(lambda *, jid=job.job_id: self._cancel_convert_for_job(jid))
        restart_btn = QPushButton("Re-run conversion")
        restart_btn.setEnabled(
            job.download_status == "done" and job.convert_status in ("failed", "cancelled", "done")
        )
        restart_btn.clicked.connect(lambda *, jid=job.job_id: self._rerun_convert_for_job(jid))
        need_save = job.backend == "remote" and job.convert_status == "done"
        overwrite = need_save and (job.out_path.is_file() or job.remote_saved_locally)
        save_label = "ReSave to PC" if overwrite else "Save to PC"
        save_btn = QPushButton(save_label)
        save_btn.setVisible(need_save)
        save_btn.setEnabled(need_save and not job.remote_fetch_in_progress)
        if need_save and job.out_path.is_file():
            save_btn.setToolTip("A file already exists at this path; saving will overwrite it.")
        save_btn.clicked.connect(lambda *, jid=job.job_id: self._save_remote_to_pc(jid))
        remove_btn = QPushButton("Remove")
        remove_btn.setToolTip("Remove this entry from the queue (cancels an active transcode if needed).")
        remove_btn.clicked.connect(lambda *, jid=job.job_id: self._remove_job_from_queue(jid))
        h.addWidget(cancel_btn)
        h.addWidget(restart_btn)
        h.addWidget(save_btn)
        h.addWidget(remove_btn)
        return w

    def _cancel_download_for_job(self, job_id: str) -> None:
        self._dl.cancel_download(job_id)

    def _remove_job_from_queue(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if not job:
            return
        title = job.candidate.title
        if (
            QMessageBox.question(
                self,
                "Remove from queue",
                f"Remove “{title}” from the download/convert queue?\n\n"
                "This drops the job from the list; files already on disk are not deleted.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self._purge_jobs_from_queue([job_id])

    def _restart_download_for_job(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if not job:
            return
        if job.download_status not in ("failed", "cancelled"):
            return
        self._requeue_full_pipeline(job)

    def _requeue_full_pipeline(self, job: UiJob) -> None:
        job.download_status = "queued"
        job.download_percent = -1.0
        job.download_speed = ""
        job.download_eta = ""
        job.download_error = ""
        job.raw_path = None
        job.ytdlp_info = None
        job.convert_status = "waiting"
        job.convert_percent = 0.0
        job.convert_error = ""
        job.convert_indeterminate = False
        job.remote_saved_locally = job.backend != "remote"
        job.remote_job_id = None
        if job.backend == "remote":
            job.remote_job_origin = "desktop"
        if job.backend == "remote" and self._remote:
            self._remote.reset_local_tracking(job.job_id)
            sub = self.subdir_edit.text().strip() or "gui-downloads"
            stem = safe_stem(job.candidate.title, job.candidate.video_id)
            self._remote.set_pending_job(
                job.job_id,
                subdir=sub,
                output_stem=stem,
                video_id=job.candidate.video_id,
                title=job.candidate.title,
                channel=job.candidate.channel,
                no_embed=job.no_embed,
            )
        self._dl.enqueue(job.job_id, job.candidate.url)
        self._persist_job(job)
        self._refresh_downloads_table()
        self._refresh_convert_table()

    def _cancel_convert_for_job(self, job_id: str) -> None:
        self._cv.cancel_convert(job_id)

    def _rerun_convert_for_job(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if not job or job.download_status != "done":
            return
        if job.convert_status in ("queued", "waiting", "converting"):
            return
        job.no_embed = self.no_embed_cb.isChecked()
        if job.backend == "remote":
            self._requeue_full_pipeline(job)
            self._status.setText(f"Re-queued remote conversion: {job.candidate.title}")
            return
        if not job.raw_path or not job.raw_path.is_file():
            self._requeue_full_pipeline(job)
            self._status.setText(
                f"Raw cache missing, re-downloading before conversion: {job.candidate.title}"
            )
            return
        job.convert_status = "queued"
        job.convert_percent = 0.0
        job.convert_error = ""
        job.convert_indeterminate = False
        self._cv.enqueue(
            job_id,
            job.raw_path,
            job.out_path,
            job.ytdlp_info,
            no_embed=job.no_embed,
        )
        self._persist_job(job)
        self._refresh_convert_table()

    def _on_dl_progress(self, job_id: str, pct: float, speed: str, eta: str) -> None:
        job = self._jobs.get(job_id)
        if not job:
            return
        job.download_status = "downloading"
        job.download_percent = pct
        job.download_speed = speed
        job.download_eta = eta
        self._update_downloads_row_cells(job)

    def _on_dl_done(self, job_id: str, raw_path: str, yinfo: object) -> None:
        job = self._jobs.get(job_id)
        if not job:
            return
        job.download_status = "done"
        job.download_percent = 100.0
        if job.backend == "remote":
            job.raw_path = None
            job.ytdlp_info = None
            job.convert_status = "queued"
            job.convert_percent = 0.0
            job.convert_indeterminate = False
            self._persist_job(job)
            self._refresh_downloads_table()
            self._refresh_convert_table()
            return
        job.raw_path = Path(raw_path)
        job.ytdlp_info = yinfo if isinstance(yinfo, dict) else None
        job.convert_status = "queued"
        job.convert_percent = 0.0
        job.convert_indeterminate = False
        self._cv.enqueue(
            job_id,
            job.raw_path,
            job.out_path,
            job.ytdlp_info,
            no_embed=job.no_embed,
        )
        self._persist_job(job)
        self._refresh_downloads_table()
        self._refresh_convert_table()

    def _on_dl_failed(self, job_id: str, err: str) -> None:
        job = self._jobs.get(job_id)
        if not job:
            return
        job.download_status = "failed"
        job.download_error = err
        self._persist_job(job)
        self._refresh_downloads_table()

    def _on_dl_cancelled(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if not job:
            return
        job.download_status = "cancelled"
        job.download_percent = -1.0
        job.download_speed = ""
        job.download_eta = ""
        self._persist_job(job)
        self._refresh_downloads_table()

    def _on_cv_progress(self, job_id: str, pct: object) -> None:
        job = self._jobs.get(job_id)
        if not job:
            return
        job.convert_status = "converting"
        if pct is None or (isinstance(pct, (int, float)) and float(pct) < 0.0):
            job.convert_indeterminate = True
        else:
            job.convert_indeterminate = False
            job.convert_percent = float(pct)
        self._update_convert_row_cells(job)

    def _on_cv_done(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if not job:
            return
        job.convert_status = "done"
        job.convert_percent = 100.0
        job.convert_indeterminate = False
        if job.backend == "remote":
            job.remote_saved_locally = False
            self._persist_job(job)
            self._refresh_downloads_table()
            self._refresh_convert_table()
            if self._should_autosave_remote_job(job):
                self._start_remote_fetch(job)
            else:
                self._status.setText(f"Remote job done — save to PC: {job.candidate.title}")
            return
        self._persist_job(job)
        self._refresh_downloads_table()
        self._refresh_convert_table()
        self._status.setText(f"Saved: {job.out_path}")

    def _on_cv_failed(self, job_id: str, err: str) -> None:
        job = self._jobs.get(job_id)
        if not job:
            return
        job.convert_status = "failed"
        job.convert_error = err
        self._persist_job(job)
        self._refresh_convert_table()

    def _on_cv_cancelled(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if not job:
            return
        job.convert_status = "cancelled"
        job.convert_indeterminate = False
        self._persist_job(job)
        self._refresh_convert_table()

    def _on_remote_registered(self, local_id: str, remote_id: str) -> None:
        job = self._jobs.get(local_id)
        if job:
            job.remote_job_id = remote_id
            self._persist_job(job)

    def _on_remote_meta_updated(self, local_id: str, title: str, channel: str, video_id: str) -> None:
        job = self._jobs.get(local_id)
        if not job:
            return
        changed = False
        if title and job.candidate.title != title:
            job.candidate.title = title
            changed = True
        if channel and job.candidate.channel != channel:
            job.candidate.channel = channel
            changed = True
        if len(video_id) == 11 and job.candidate.video_id != video_id:
            job.candidate.video_id = video_id
            job.candidate.url = f"https://www.youtube.com/watch?v={video_id}"
            changed = True
        if not changed:
            return
        self._recompute_remote_sync_out_path_if_needed(job)
        self._persist_job(job)
        new_title = job.candidate.title
        for table in (self.downloads_table, self.convert_table):
            row = self._row_index_for_job_id(table, local_id)
            if row >= 0:
                self._set_table_text(table, row, 1, new_title)

    def _on_remote_connection_error(self, msg: str) -> None:
        self._status.setText(f"Remote: {msg}")

    def _mark_remote_missing_and_requeue(self, local_id: str, reason: str) -> None:
        job = self._jobs.get(local_id)
        if not job:
            return
        if self._remote is not None:
            self._remote.reset_local_tracking(local_id)
        job.remote_job_id = None
        job.remote_saved_locally = False
        job.remote_fetch_in_progress = False
        job.remote_fetch_bytes_done = 0
        job.remote_fetch_bytes_total = 0
        job.download_status = "failed"
        job.download_percent = -1.0
        job.download_speed = ""
        job.download_eta = ""
        job.download_error = reason
        # Remote jobs are one server-side pipeline; when the remote id disappears,
        # move the row back to download-failed so Restart re-enqueues end-to-end.
        job.convert_status = "waiting"
        job.convert_percent = 0.0
        job.convert_indeterminate = False
        job.convert_error = ""
        self._persist_job(job)
        self._refresh_downloads_table()
        self._refresh_convert_table()
        self._status.setText("Remote job no longer exists on server; click Restart to re-enqueue.")

    def _on_remote_missing(self, local_id: str, reason: str) -> None:
        self._mark_remote_missing_and_requeue(local_id, reason)

    def _save_remote_to_pc(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if job:
            self._start_remote_fetch(job)

    def _start_remote_fetch(self, job: UiJob) -> None:
        if job.remote_fetch_in_progress:
            return
        if not job.remote_job_id:
            QMessageBox.warning(self, "Save", "Missing remote job id.")
            return
        base = (self._settings.remote_base_url or "").strip().rstrip("/")
        if not base:
            QMessageBox.warning(self, "Save", "Set remote base URL in Settings.")
            return
        rid = job.remote_job_id
        url = f"{base}/v1/jobs/{rid}/download"
        headers: dict[str, str] = {}
        tok = (self._settings.remote_bearer_token or "").strip()
        if tok:
            headers["Authorization"] = f"Bearer {tok}"
        out = job.out_path
        job.remote_fetch_in_progress = True
        job.remote_fetch_bytes_done = 0
        job.remote_fetch_bytes_total = 0
        self._update_convert_row_cells(job)
        self._status.setText(f"Saving to PC... {job.candidate.title}")

        def work() -> None:
            try:
                with httpx.Client(timeout=600.0) as c:
                    with c.stream("GET", url, headers=headers) as r:
                        r.raise_for_status()
                        content_len = int(r.headers.get("content-length") or "0")
                        if content_len < 0:
                            content_len = 0
                        bytes_done = 0
                        self._fetch_bridge.progress.emit(job.job_id, bytes_done, content_len)
                        out.parent.mkdir(parents=True, exist_ok=True)
                        with open(out, "wb") as f:
                            for chunk in r.iter_bytes(chunk_size=1024 * 512):
                                if not chunk:
                                    continue
                                f.write(chunk)
                                bytes_done += len(chunk)
                                self._fetch_bridge.progress.emit(job.job_id, bytes_done, content_len)
                self._fetch_bridge.ok.emit(job.job_id)
            except OSError as e:
                self._fetch_bridge.fail.emit(job.job_id, str(e))
            except (httpx.HTTPError, ValueError) as e:
                self._fetch_bridge.fail.emit(job.job_id, str(e))

        threading.Thread(target=work, daemon=True).start()

    def _on_remote_fetch_progress(self, job_id: str, bytes_done: int, bytes_total: int) -> None:
        job = self._jobs.get(job_id)
        if not job:
            return
        job.remote_fetch_in_progress = True
        job.remote_fetch_bytes_done = max(0, int(bytes_done))
        job.remote_fetch_bytes_total = max(0, int(bytes_total))
        self._update_convert_row_cells(job)

    def _on_remote_fetch_ok(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if not job:
            return
        job.remote_fetch_in_progress = False
        job.remote_fetch_bytes_done = 0
        job.remote_fetch_bytes_total = 0
        job.remote_saved_locally = True
        self._persist_job(job)
        self._refresh_convert_table()
        self._status.setText(f"Saved: {job.out_path}")
        # The file is safely on this PC now; tell the daemon to drop its copy and job
        # record so server disk isn't holding a redundant file forever.
        rid = (job.remote_job_id or "").strip()
        if rid and self._remote is not None:
            remote = self._remote

            def _cleanup_server_copy(remote_id: str = rid) -> None:
                if remote.delete_server_job_sync(remote_id):
                    remote.reset_local_tracking(job_id)

            threading.Thread(target=_cleanup_server_copy, daemon=True).start()

    def _on_remote_fetch_fail(self, job_id: str, err: str) -> None:
        job = self._jobs.get(job_id)
        if job:
            job.remote_fetch_in_progress = False
            job.remote_fetch_bytes_done = 0
            job.remote_fetch_bytes_total = 0
            self._update_convert_row_cells(job)
        self._status.setText("Saving to PC failed.")
        low = err.lower()
        if "404" in low and "/v1/jobs/" in low and "/download" in low:
            self._mark_remote_missing_and_requeue(
                job_id,
                "Download failed: remote job no longer exists on the server.",
            )
            QMessageBox.warning(
                self,
                "Remote job missing",
                "This server job id no longer exists. The row was moved back to Downloads.\n"
                "Click Restart to re-enqueue.",
            )
            return
        QMessageBox.critical(self, "Download from server failed", err)

    def closeEvent(self, event) -> None:
        self._library_cancel_prefetch_probes()
        if self._convert_probe_worker is not None:
            self._convert_probe_worker.requestInterruption()
            self._convert_probe_worker.wait(3000)
            self._convert_probe_worker = None
        if self._convert_apply_save_worker is not None:
            self._convert_apply_save_worker.requestInterruption()
            self._convert_apply_save_worker.wait(3000)
            self._convert_apply_save_worker = None
        convert_save_workers = list(self._convert_infer_save_workers.values())
        self._convert_infer_save_workers.clear()
        for w in convert_save_workers:
            if w.isRunning():
                w.requestInterruption()
                w.wait(3000)
        if self._library_probe_worker is not None:
            self._library_probe_worker.requestInterruption()
            self._library_probe_worker.wait(3000)
            self._library_probe_worker = None
        if self._library_bulk_clear_metadata_worker is not None:
            self._library_bulk_clear_metadata_worker.requestInterruption()
            self._library_bulk_clear_metadata_worker.wait(3000)
            self._library_bulk_clear_metadata_worker = None
        self._persist_all_jobs()
        self._store.close()
        self._dl.stop()
        self._cv.stop()
        if self._remote is not None:
            self._remote.stop()
        if self._tray is not None:
            self._tray.hide()
        super().closeEvent(event)
