"""Main PyQt6 window: Search / Downloads / Convert tabs."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
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
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from video_finder import __version__
from video_finder.config import Settings, load_settings
from video_finder.search import VideoCandidate
from video_finder.workflow import ensure_output_dir, safe_stem, unique_out_path

from .models import UiJob
from .workers import ConvertService, DownloadService, SearchWorker, new_job_id


class MainWindow(QWidget):
    def __init__(self, *, config_path: Path | None = None) -> None:
        super().__init__()
        self.setWindowTitle(f"video-finder {__version__}")
        self.resize(960, 640)

        self._config_path = config_path
        self._settings: Settings = load_settings(config_path)
        self._results: list[VideoCandidate] = []
        self._jobs: dict[str, UiJob] = {}
        self._job_order: list[str] = []

        self._search_worker: SearchWorker | None = None

        self._dl = DownloadService(self._settings, parent=self)
        self._cv = ConvertService(self._settings, no_embed=False, parent=self)
        self._dl.progress.connect(self._on_dl_progress)
        self._dl.item_done.connect(self._on_dl_done)
        self._dl.item_failed.connect(self._on_dl_failed)
        self._cv.progress.connect(self._on_cv_progress)
        self._cv.item_done.connect(self._on_cv_done)
        self._cv.item_failed.connect(self._on_cv_failed)
        self._dl.start()
        self._cv.start()

        tabs = QTabWidget()
        tabs.addTab(self._build_search_tab(), "Search")
        tabs.addTab(self._build_downloads_tab(), "Downloads")
        tabs.addTab(self._build_convert_tab(), "Convert")

        root = QVBoxLayout(self)
        root.addWidget(tabs)
        self._status = QLabel("")
        self._status.setStyleSheet("color: palette(mid);")
        root.addWidget(self._status)

        self._apply_settings_to_widgets()

    def _apply_settings_to_widgets(self) -> None:
        self.limit_spin.setValue(self._settings.search_limit if self._settings.search_limit else 15)

    def _reload_settings(self) -> None:
        p = Path(self.config_edit.text().strip()) if self.config_edit.text().strip() else None
        self._config_path = p
        self._settings = load_settings(p)
        self._apply_settings_to_widgets()
        # Recreate services with new cache paths — simplest: restart app message
        QMessageBox.information(
            self,
            "Settings",
            "Config reloaded for new searches. Restart the app if download paths should change mid-session.",
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
        self.source_combo.addItems(["Search term", "Channel", "Playlist"])
        self.source_combo.currentIndexChanged.connect(self._update_source_widgets)
        src_grid.addWidget(QLabel("Type:"), 0, 0)
        src_grid.addWidget(self.source_combo, 0, 1)
        self.query_edit = QLineEdit()
        self.query_edit.setPlaceholderText("Query, @channel, channel URL, or playlist URL")
        src_grid.addWidget(QLabel("Input:"), 1, 0)
        src_grid.addWidget(self.query_edit, 1, 1)
        self.use_api_cb = QCheckBox("Use YouTube Data API (needs API key)")
        self.use_api_cb.toggled.connect(self._update_source_widgets)
        src_grid.addWidget(self.use_api_cb, 2, 0, 1, 2)
        self.artist_edit = QLineEdit()
        self.artist_edit.setPlaceholderText("Artist (optional; with API or template)")
        self.title_edit = QLineEdit()
        self.title_edit.setPlaceholderText("Song title (optional)")
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
        self.subdir_edit.setPlaceholderText("Output subfolder under Videos/video-finder")
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

    def _update_source_widgets(self) -> None:
        idx = self.source_combo.currentIndex()
        is_search = idx == 0
        self.use_api_cb.setEnabled(is_search)
        self.artist_edit.setEnabled(is_search)
        self.title_edit.setEnabled(is_search)
        self.template_combo.setEnabled(is_search)

    def _browse_config(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "config.toml", str(Path.home()), "TOML (*.toml);;All (*)")
        if path:
            self.config_edit.setText(path)

    def _limit_value(self) -> int | None:
        v = self.limit_spin.value()
        return None if v == 0 else v

    def _start_search(self) -> None:
        if self._search_worker and self._search_worker.isRunning():
            return
        q = self.query_edit.text().strip()
        idx = self.source_combo.currentIndex()
        mode = ("search", "channel", "playlist")[idx]
        if not q and not (mode == "search" and self.artist_edit.text().strip()):
            QMessageBox.warning(
                self,
                "Search",
                "Enter a search query, channel, or playlist URL (or an artist for template search).",
            )
            return

        self._settings = load_settings(Path(self.config_edit.text().strip()) if self.config_edit.text().strip() else None)
        if self.use_api_cb.isChecked() and mode == "search" and not self._settings.youtube_api_key:
            QMessageBox.warning(self, "API", "Set YOUTUBE_API_KEY or youtube_api_key in config for API search.")
            return

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
        self._search_worker.finished_ok.connect(self._search_finished)
        self._search_worker.failed.connect(self._search_failed)
        self._search_worker.finished.connect(self._search_thread_done)
        self._search_worker.start()

    def _search_thread_done(self) -> None:
        self.search_btn.setEnabled(True)
        if self._search_worker:
            self._search_worker.deleteLater()
            self._search_worker = None

    def _search_finished(self, rows: list) -> None:
        self._results = list(rows)
        self.results_table.setRowCount(0)
        from video_finder.workflow import fmt_duration

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

    def _search_failed(self, msg: str) -> None:
        QMessageBox.critical(self, "Search failed", msg)

    def _output_folder(self) -> Path:
        sub = self.subdir_edit.text().strip() or "gui-downloads"
        base = self._settings.merged_output_dir()
        return ensure_output_dir(base / sub)

    def _queue_selected(self) -> None:
        if not self._results:
            QMessageBox.information(self, "Queue", "Run a search first.")
            return
        out_folder = self._output_folder()
        self._cv.set_no_embed(self.no_embed_cb.isChecked())
        count = 0
        for i in range(self.results_table.rowCount()):
            it = self.results_table.item(i, 0)
            if it is None or it.checkState() != Qt.CheckState.Checked:
                continue
            c = self._results[i]
            jid = new_job_id()
            stem = safe_stem(c.title, c.video_id)
            outp = unique_out_path(out_folder, stem, c.video_id)
            job = UiJob(job_id=jid, candidate=c, out_path=outp)
            self._jobs[jid] = job
            self._job_order.append(jid)
            self._dl.enqueue(jid, c.url)
            job.download_status = "queued"
            count += 1
        if count == 0:
            QMessageBox.information(self, "Queue", "Check one or more videos to queue.")
            return
        self._refresh_downloads_table()
        self._refresh_convert_table()

    def _build_downloads_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.downloads_table = QTableWidget(0, 4)
        self.downloads_table.setHorizontalHeaderLabels(["Title", "Status", "Progress", "Speed / ETA"])
        self.downloads_table.horizontalHeader().setStretchLastSection(True)
        lay.addWidget(self.downloads_table)
        return w

    def _build_convert_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.convert_table = QTableWidget(0, 3)
        self.convert_table.setHorizontalHeaderLabels(["Title", "Status", "Progress"])
        self.convert_table.horizontalHeader().setStretchLastSection(True)
        lay.addWidget(self.convert_table)
        return w

    def _refresh_downloads_table(self) -> None:
        rows = [self._jobs[j] for j in self._job_order if self._jobs[j].download_status != "done"]
        self.downloads_table.setRowCount(len(rows))
        for i, job in enumerate(rows):
            self.downloads_table.setItem(i, 0, QTableWidgetItem(job.candidate.title))
            st = job.download_status
            if st == "downloading":
                detail = "Downloading"
            elif st == "queued":
                detail = "Queued"
            elif st == "failed":
                detail = job.download_error or "Failed"
            else:
                detail = st
            self.downloads_table.setItem(i, 1, QTableWidgetItem(detail))
            pct = job.download_percent
            if pct < 0:
                prog_txt = "—" if st != "downloading" else "…"
            else:
                prog_txt = f"{pct:.1f}%"
            self.downloads_table.setItem(i, 2, QTableWidgetItem(prog_txt))
            extra = f"{job.download_speed}  {job.download_eta}".strip()
            self.downloads_table.setItem(i, 3, QTableWidgetItem(extra))

    def _refresh_convert_table(self) -> None:
        rows = [
            self._jobs[j]
            for j in self._job_order
            if self._jobs[j].download_status == "done" and self._jobs[j].convert_status != "done"
        ]
        self.convert_table.setRowCount(len(rows))
        for i, job in enumerate(rows):
            self.convert_table.setItem(i, 0, QTableWidgetItem(job.candidate.title))
            st = job.convert_status
            if st == "converting":
                detail = "Converting"
            elif st in ("queued", "waiting"):
                detail = "Queued"
            elif st == "failed":
                detail = job.convert_error or "Failed"
            else:
                detail = st
            self.convert_table.setItem(i, 1, QTableWidgetItem(detail))
            if job.convert_indeterminate:
                prog_txt = "…"
            else:
                prog_txt = f"{job.convert_percent:.1f}%"
            self.convert_table.setItem(i, 2, QTableWidgetItem(prog_txt))

    def _on_dl_progress(self, job_id: str, pct: float, speed: str, eta: str) -> None:
        job = self._jobs.get(job_id)
        if not job:
            return
        job.download_status = "downloading"
        job.download_percent = pct
        job.download_speed = speed
        job.download_eta = eta
        self._refresh_downloads_table()

    def _on_dl_done(self, job_id: str, raw_path: str, yinfo: object) -> None:
        job = self._jobs.get(job_id)
        if not job:
            return
        job.download_status = "done"
        job.download_percent = 100.0
        job.raw_path = Path(raw_path)
        job.ytdlp_info = yinfo if isinstance(yinfo, dict) else None
        job.convert_status = "queued"
        self._cv.enqueue(job_id, job.raw_path, job.out_path, job.ytdlp_info)
        self._refresh_downloads_table()
        self._refresh_convert_table()

    def _on_dl_failed(self, job_id: str, err: str) -> None:
        job = self._jobs.get(job_id)
        if not job:
            return
        job.download_status = "failed"
        job.download_error = err
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
        self._refresh_convert_table()

    def _on_cv_done(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if not job:
            return
        job.convert_status = "done"
        job.convert_percent = 100.0
        job.convert_indeterminate = False
        self._refresh_downloads_table()
        self._refresh_convert_table()
        self._status.setText(f"Saved: {job.out_path}")

    def _on_cv_failed(self, job_id: str, err: str) -> None:
        job = self._jobs.get(job_id)
        if not job:
            return
        job.convert_status = "failed"
        job.convert_error = err
        self._refresh_convert_table()

    def closeEvent(self, event) -> None:
        self._dl.stop()
        self._cv.stop()
        self._dl.wait(5000)
        self._cv.wait(5000)
        super().closeEvent(event)
