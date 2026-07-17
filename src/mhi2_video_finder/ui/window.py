"""Main PyQt6 window: Search / Downloads / Convert tabs."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QSizePolicy,
    QStyle,
    QSystemTrayIcon,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from mhi2_video_finder import __version__
from mhi2_video_finder.build_info import build_date_display
from mhi2_video_finder.config import Settings, load_settings
from mhi2_video_finder.local_library import LibraryFileRow
from mhi2_video_finder.search import VideoCandidate

from .backends.remote import RemoteJobController
from .convert_ai import ConvertAIMixin
from .job_queue import JobQueueMixin
from .job_store import JobStore
from .library_ai import LibraryAIMixin
from .models import UiJob
from .remote_sync import _DIAGNOSTICS_POLL_MS, _REMOTE_JOBS_POLL_MS, RemoteSyncMixin, _RemoteFetchBridge
from .search_tab import SearchMixin
from .settings_mixin import SettingsMixin
from .status_bar import StatusBarMixin
from .tab_builders import TabBuildersMixin
from .window_geometry import WindowGeometryMixin
from .workers import (
    BulkUrlResolveWorker,
    ConvertService,
    DownloadService,
    LibraryBulkClearMetadataWorker,
    LibraryBulkRenameToSongWorker,
    LibraryProbeWorker,
    LibrarySaveWorker,
    LibraryScanWorker,
    SearchWorker,
)


class MainWindow(
    QMainWindow,
    WindowGeometryMixin,
    StatusBarMixin,
    SettingsMixin,
    TabBuildersMixin,
    SearchMixin,
    RemoteSyncMixin,
    LibraryAIMixin,
    ConvertAIMixin,
    JobQueueMixin,
):
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
