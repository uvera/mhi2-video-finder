"""Main PyQt6 window: Search / Downloads / Convert tabs."""

from __future__ import annotations

from functools import partial
from pathlib import Path
import threading

import httpx
from PyQt6.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QStyle,
    QSystemTrayIcon,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from mhi2_video_finder import __version__
from mhi2_video_finder.config import Settings, default_config_path, load_settings, save_settings
from mhi2_video_finder.search import VideoCandidate
from mhi2_video_finder.workflow import ensure_output_dir, safe_stem, unique_out_path

from .backends.remote import RemoteJobController
from .job_store import JobStore

# Stored in QComboBox userData; must match config ``video_encoder`` values.
_ENCODER_CHOICES: tuple[tuple[str, str], ...] = (
    ("libx264 (CPU — best for picky car USB / MHI2)", "libx264"),
    ("h264_vaapi (Intel / AMD GPU)", "h264_vaapi"),
    ("h264_nvenc (NVIDIA GPU)", "h264_nvenc"),
)
from .models import UiJob
from .workers import ConvertService, DownloadService, SearchWorker, new_job_id


class _RemoteFetchBridge(QObject):
    ok = pyqtSignal(str)
    fail = pyqtSignal(str, str)


class MainWindow(QWidget):
    def __init__(self, *, config_path: Path | None = None) -> None:
        super().__init__()
        self.setWindowTitle(f"mhi2-video-finder {__version__}")
        self.resize(960, 640)

        self._config_path = config_path
        self._settings: Settings = load_settings(config_path)
        self._results: list[VideoCandidate] = []
        self._jobs: dict[str, UiJob] = {}
        self._job_order: list[str] = []

        self._search_worker: SearchWorker | None = None
        self._search_seq: int = 0
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

        if self._use_remote:
            self._remote = RemoteJobController(lambda: self._settings)
            self._remote.connection_error.connect(self._on_remote_connection_error)
            self._remote.remote_registered.connect(self._on_remote_registered)
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

        tabs = QTabWidget()
        tabs.addTab(self._build_search_tab(), "Search")
        tabs.addTab(self._build_downloads_tab(), "Downloads")
        tabs.addTab(self._build_convert_tab(), "Convert")
        tabs.addTab(self._build_settings_tab(), "Settings")

        root = QVBoxLayout(self)
        root.addWidget(tabs)
        self._status = QLabel("")
        self._status.setStyleSheet("color: palette(mid);")
        root.addWidget(self._status)

        self._apply_settings_to_widgets()

        self._load_persisted_jobs()
        self._refresh_downloads_table()
        self._refresh_convert_table()

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
        for job in self._store.load_all():
            outp = job.out_path
            if job.backend != "remote" and outp.is_file() and outp.stat().st_size >= 4096:
                self._store.delete(job.job_id)
                continue
            if job.backend == "remote" and job.convert_status == "done" and job.remote_saved_locally:
                self._store.delete(job.job_id)
                continue
            self._jobs[job.job_id] = job
            self._job_order.append(job.job_id)

        for jid in list(self._job_order):
            job = self._jobs[jid]
            if job.backend == "remote":
                if not job.remote_job_id and job.download_status == "queued":
                    job.download_status = "failed"
                    job.download_error = (
                        "Remote job was never submitted to the server. Remove it and queue again."
                    )
                    self._persist_job(job)
                    continue
                if self._remote and job.remote_job_id:
                    self._remote.register_existing(jid, job.remote_job_id)
                    self._remote.sync_job_from_server(jid, job.remote_job_id)
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

    def _sync_widgets_to_settings(self) -> None:
        pb = self.ui_processing_backend.currentData()
        self._settings.processing_backend = pb if pb in ("local", "remote") else "local"
        self._settings.remote_base_url = self.ui_remote_url.text().strip()
        self._settings.remote_bearer_token = self.ui_remote_token.text().strip()
        rd = self.ui_remote_dl_dir.text().strip()
        if rd:
            self._settings.remote_download_dir = Path(rd).expanduser().resolve()
        self._settings.remote_auto_download = self.ui_remote_auto_download.isChecked()

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
        lay.addLayout(btn_row)

        self.results_table = QTableWidget(0, 5)
        self.results_table.setHorizontalHeaderLabels(["", "Title", "Channel", "Duration", "URL"])
        self.results_table.horizontalHeader().setStretchLastSection(True)
        self.results_table.setColumnWidth(0, 36)
        self.results_table.setColumnWidth(3, 72)
        lay.addWidget(self.results_table)

        queue_row = QHBoxLayout()
        self.subdir_edit = QLineEdit()
        self.subdir_edit.setPlaceholderText("Output subfolder under Videos/mhi2-video-finder")
        self.subdir_edit.setText("gui-downloads")
        queue_row.addWidget(QLabel("Subfolder:"))
        queue_row.addWidget(self.subdir_edit)
        self.no_embed_cb = QCheckBox("Transcode only (no tags / album art)")
        queue_row.addWidget(self.no_embed_cb)
        self.queue_btn = QPushButton("Queue selected for download")
        self.queue_btn.clicked.connect(self._queue_selected)
        queue_row.addWidget(self.queue_btn)
        lay.addLayout(queue_row)

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
        self.ui_embed_metadata.setToolTip("When unchecked, ffmpeg skips -metadata tags from the download info.")
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

        hint = QLabel(
            "Uses the config file path from the Search tab (or the default XDG path if empty). "
            "Apply updates workers for this session; Save writes merged values into config.toml "
            "(video_encoder, embed_metadata, embed_album_art, vaapi_*, ffmpeg_*, parallel queues)."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: palette(mid);")
        lay.addWidget(hint)

        row = QHBoxLayout()
        apply_btn = QPushButton("Apply to session")
        apply_btn.clicked.connect(self._apply_session_settings)
        row.addWidget(apply_btn)
        save_btn = QPushButton("Save to config file")
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

        self._settings = load_settings(Path(self.config_edit.text().strip()) if self.config_edit.text().strip() else None)
        if self.use_api_cb.isChecked() and mode == "search" and not self._settings.youtube_api_key:
            QMessageBox.warning(self, "API", "Set YOUTUBE_API_KEY or youtube_api_key in config for API search.")
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

    def _search_failed(self, msg: str, *, _seq: int) -> None:
        if _seq != self._search_seq:
            return
        QMessageBox.critical(self, "Search failed", msg)

    def _deselect_result_checkboxes(self) -> None:
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

    def _queue_selected(self) -> None:
        if not self._results:
            QMessageBox.information(self, "Queue", "Run a search first.")
            return
        out_folder = self._output_folder()
        self._cv.set_no_embed(self.no_embed_cb.isChecked())
        ne = self.no_embed_cb.isChecked()
        count = 0
        for i in range(self.results_table.rowCount()):
            it = self.results_table.item(i, 0)
            if it is None or it.checkState() != Qt.CheckState.Checked:
                continue
            c = self._results[i]
            jid = new_job_id()
            stem = safe_stem(c.title, c.video_id)
            outp = unique_out_path(out_folder, stem, c.video_id)
            backend = "remote" if self._use_remote else "local"
            job = UiJob(
                job_id=jid,
                candidate=c,
                out_path=outp,
                no_embed=ne,
                backend=backend,
                remote_saved_locally=backend != "remote",
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
            count += 1
        if count == 0:
            QMessageBox.information(self, "Queue", "Check one or more videos to queue.")
            return
        self._deselect_result_checkboxes()
        self._notify_queued(count)
        self._refresh_downloads_table()
        self._refresh_convert_table()

    def _build_downloads_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.downloads_table = QTableWidget(0, 5)
        self.downloads_table.setHorizontalHeaderLabels(["Title", "Status", "Progress", "Speed / ETA", "Actions"])
        self.downloads_table.horizontalHeader().setStretchLastSection(True)
        lay.addWidget(self.downloads_table)
        return w

    def _build_convert_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.convert_table = QTableWidget(0, 4)
        self.convert_table.setHorizontalHeaderLabels(["Title", "Status", "Progress", "Actions"])
        self.convert_table.horizontalHeader().setStretchLastSection(True)
        lay.addWidget(self.convert_table)
        return w

    @staticmethod
    def _row_index_for_job_id(table: QTableWidget, job_id: str) -> int:
        for i in range(table.rowCount()):
            it = table.item(i, 0)
            if it is not None and it.data(Qt.ItemDataRole.UserRole) == job_id:
                return i
        return -1

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
        if (
            job.backend == "remote"
            and job.convert_status == "done"
            and not job.remote_saved_locally
        ):
            return "Done on server — save to PC", "100.0%"
        st = job.convert_status
        if st == "converting":
            detail = "Converting"
        elif st in ("queued", "waiting"):
            detail = "Queued"
        elif st == "failed":
            detail = job.convert_error or "Failed"
        elif st == "cancelled":
            detail = "Cancelled"
        else:
            detail = st
        if job.convert_indeterminate:
            prog_txt = "…"
        else:
            prog_txt = f"{job.convert_percent:.1f}%"
        return detail, prog_txt

    def _refresh_downloads_table(self) -> None:
        rows = [self._jobs[j] for j in self._job_order if self._jobs[j].download_status != "done"]
        self.downloads_table.setRowCount(len(rows))
        for i, job in enumerate(rows):
            title_it = QTableWidgetItem(job.candidate.title)
            title_it.setData(Qt.ItemDataRole.UserRole, job.job_id)
            self.downloads_table.setItem(i, 0, title_it)
            d1, d2, d3 = self._downloads_status_progress_speed(job)
            self.downloads_table.setItem(i, 1, QTableWidgetItem(d1))
            self.downloads_table.setItem(i, 2, QTableWidgetItem(d2))
            self.downloads_table.setItem(i, 3, QTableWidgetItem(d3))
            self.downloads_table.setCellWidget(i, 4, self._download_actions_widget(job))

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
        rows = [
            self._jobs[j]
            for j in self._job_order
            if self._jobs[j].download_status == "done"
            and (
                self._jobs[j].convert_status != "done"
                or (
                    self._jobs[j].backend == "remote"
                    and self._jobs[j].convert_status == "done"
                    and not self._jobs[j].remote_saved_locally
                )
            )
        ]
        self.convert_table.setRowCount(len(rows))
        for i, job in enumerate(rows):
            title_it = QTableWidgetItem(job.candidate.title)
            title_it.setData(Qt.ItemDataRole.UserRole, job.job_id)
            self.convert_table.setItem(i, 0, title_it)
            d1, d2 = self._convert_status_progress(job)
            self.convert_table.setItem(i, 1, QTableWidgetItem(d1))
            self.convert_table.setItem(i, 2, QTableWidgetItem(d2))
            self.convert_table.setCellWidget(i, 3, self._convert_actions_widget(job))

    def _update_downloads_row_cells(self, job: UiJob) -> None:
        row = self._row_index_for_job_id(self.downloads_table, job.job_id)
        if row < 0:
            self._refresh_downloads_table()
            return
        d1, d2, d3 = self._downloads_status_progress_speed(job)
        self._set_table_text(self.downloads_table, row, 1, d1)
        self._set_table_text(self.downloads_table, row, 2, d2)
        self._set_table_text(self.downloads_table, row, 3, d3)

    def _update_convert_row_cells(self, job: UiJob) -> None:
        row = self._row_index_for_job_id(self.convert_table, job.job_id)
        if row < 0:
            self._refresh_convert_table()
            return
        d1, d2 = self._convert_status_progress(job)
        self._set_table_text(self.convert_table, row, 1, d1)
        self._set_table_text(self.convert_table, row, 2, d2)

    def _convert_actions_widget(self, job: UiJob) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(2, 0, 2, 0)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setEnabled(job.convert_status in ("queued", "waiting", "converting"))
        cancel_btn.clicked.connect(lambda *, jid=job.job_id: self._cancel_convert_for_job(jid))
        restart_btn = QPushButton("Restart")
        raw_ok = job.raw_path is not None and job.raw_path.is_file()
        restart_btn.setVisible(job.backend != "remote")
        restart_btn.setEnabled(
            raw_ok and job.convert_status in ("failed", "cancelled")
        )
        restart_btn.clicked.connect(lambda *, jid=job.job_id: self._restart_convert_for_job(jid))
        save_btn = QPushButton("Save to PC")
        need_save = (
            job.backend == "remote"
            and job.convert_status == "done"
            and not job.remote_saved_locally
        )
        save_btn.setVisible(need_save)
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
        need_cancel_dl = job.download_status in ("queued", "downloading")
        need_cancel_cv = job.download_status == "done" and job.convert_status in (
            "queued",
            "waiting",
            "converting",
        )
        self._job_order = [j for j in self._job_order if j != job_id]
        self._jobs.pop(job_id, None)
        self._store.delete(job_id)
        if need_cancel_dl:
            self._dl.cancel_download(job_id)
        if need_cancel_cv:
            self._cv.cancel_convert(job_id)
        self._refresh_downloads_table()
        self._refresh_convert_table()

    def _restart_download_for_job(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if not job:
            return
        if job.download_status not in ("failed", "cancelled"):
            return
        job.download_status = "queued"
        job.download_percent = -1.0
        job.download_speed = ""
        job.download_eta = ""
        job.download_error = ""
        job.remote_job_id = None
        if job.backend == "remote" and self._remote:
            self._remote.reset_local_tracking(job_id)
            sub = self.subdir_edit.text().strip() or "gui-downloads"
            stem = safe_stem(job.candidate.title, job.candidate.video_id)
            self._remote.set_pending_job(
                job_id,
                subdir=sub,
                output_stem=stem,
                video_id=job.candidate.video_id,
                title=job.candidate.title,
                channel=job.candidate.channel,
                no_embed=job.no_embed,
            )
        self._dl.enqueue(job_id, job.candidate.url)
        self._persist_job(job)
        self._refresh_downloads_table()

    def _cancel_convert_for_job(self, job_id: str) -> None:
        self._cv.cancel_convert(job_id)

    def _restart_convert_for_job(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if not job or job.download_status != "done":
            return
        if job.convert_status not in ("failed", "cancelled"):
            return
        if job.backend == "remote":
            QMessageBox.information(
                self,
                "Restart convert",
                "Remote transcodes run on the server. Re-queue the video or fix the job on the server.",
            )
            return
        if not job.raw_path or not job.raw_path.is_file():
            QMessageBox.warning(
                self,
                "Restart convert",
                "Raw download file is missing. Restart the download first.",
            )
            return
        job.no_embed = self.no_embed_cb.isChecked()
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
        if pct is None:
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
            if self._settings.remote_auto_download:
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

    def _on_remote_connection_error(self, msg: str) -> None:
        self._status.setText(f"Remote: {msg}")

    def _save_remote_to_pc(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if job:
            self._start_remote_fetch(job)

    def _start_remote_fetch(self, job: UiJob) -> None:
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

        def work() -> None:
            try:
                with httpx.Client(timeout=600.0) as c:
                    with c.stream("GET", url, headers=headers) as r:
                        r.raise_for_status()
                        out.parent.mkdir(parents=True, exist_ok=True)
                        with open(out, "wb") as f:
                            for chunk in r.iter_bytes(chunk_size=1024 * 512):
                                f.write(chunk)
                self._fetch_bridge.ok.emit(job.job_id)
            except OSError as e:
                self._fetch_bridge.fail.emit(job.job_id, str(e))
            except httpx.HTTPError as e:
                self._fetch_bridge.fail.emit(job.job_id, str(e))

        threading.Thread(target=work, daemon=True).start()

    def _on_remote_fetch_ok(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if not job:
            return
        job.remote_saved_locally = True
        self._persist_job(job)
        self._refresh_convert_table()
        self._status.setText(f"Saved: {job.out_path}")

    def _on_remote_fetch_fail(self, job_id: str, err: str) -> None:
        QMessageBox.critical(self, "Download from server failed", err)

    def closeEvent(self, event) -> None:
        self._persist_all_jobs()
        self._store.close()
        self._dl.stop()
        self._cv.stop()
        if self._remote is not None:
            self._remote.stop()
        if self._tray is not None:
            self._tray.hide()
        super().closeEvent(event)
