"""Tab construction for the main window: Search / Settings / Downloads / Convert / Diagnostics / Library."""

from __future__ import annotations

from functools import partial
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractScrollArea,
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
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .settings_mixin import _ENCODER_CHOICES
from .styles import (
    DIAGNOSTICS_IDLE_STYLE,
    DIAGNOSTICS_OFFLINE_STYLE,
    DIAGNOSTICS_ONLINE_STYLE,
    HINT_LABEL_STYLE,
)

# Library table column indices. Shared with library-management code in window.py (row fill,
# checkbox/AI-status updates, etc.) that has not yet moved out of window.py, so window.py
# imports these back from here rather than each module defining its own copy.
_LIB_COL_CHECK = 0
_LIB_COL_FILE = 1
_LIB_COL_ARTIST = 2
_LIB_COL_SONG = 3
_LIB_COL_GUESS_STATUS = 4


class TabBuildersMixin:
    """Builds the QWidget content for each tab of the main window."""

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
