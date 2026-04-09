"""Main PyQt6 window: Search / Downloads / Convert tabs."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, QTimer
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

from video_finder import __version__
from video_finder.config import Settings, load_settings
from video_finder.search import VideoCandidate
from video_finder.workflow import ensure_output_dir, safe_stem, unique_out_path

from .job_store import JobStore
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
        self._tray: QSystemTrayIcon | None = None
        if QSystemTrayIcon.isSystemTrayAvailable():
            self._tray = QSystemTrayIcon(self)
            self._tray.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogDetailedView))
            self._tray.setToolTip("video-finder")
            self._tray.show()

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
            if outp.is_file() and outp.stat().st_size >= 4096:
                self._store.delete(job.job_id)
                continue
            self._jobs[job.job_id] = job
            self._job_order.append(job.job_id)

        for jid in list(self._job_order):
            job = self._jobs[jid]
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
                "video-finder",
                msg,
                QSystemTrayIcon.MessageIcon.Information,
                4000,
            )
        else:
            box = QMessageBox(
                QMessageBox.Icon.Information,
                "video-finder",
                msg,
                QMessageBox.StandardButton.Ok,
                self,
            )
            box.setModal(False)
            box.show()
            QTimer.singleShot(3500, box.close)

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
            job = UiJob(job_id=jid, candidate=c, out_path=outp, no_embed=ne)
            self._jobs[jid] = job
            self._job_order.append(jid)
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

    def _refresh_downloads_table(self) -> None:
        rows = [self._jobs[j] for j in self._job_order if self._jobs[j].download_status != "done"]
        self.downloads_table.setRowCount(len(rows))
        for i, job in enumerate(rows):
            title_it = QTableWidgetItem(job.candidate.title)
            title_it.setData(Qt.ItemDataRole.UserRole, job.job_id)
            self.downloads_table.setItem(i, 0, title_it)
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
            self.downloads_table.setItem(i, 1, QTableWidgetItem(detail))
            pct = job.download_percent
            if pct < 0:
                prog_txt = "—" if st != "downloading" else "…"
            else:
                prog_txt = f"{pct:.1f}%"
            self.downloads_table.setItem(i, 2, QTableWidgetItem(prog_txt))
            extra = f"{job.download_speed}  {job.download_eta}".strip()
            self.downloads_table.setItem(i, 3, QTableWidgetItem(extra))
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
        h.addWidget(cancel_btn)
        h.addWidget(restart_btn)
        return w

    def _refresh_convert_table(self) -> None:
        rows = [
            self._jobs[j]
            for j in self._job_order
            if self._jobs[j].download_status == "done" and self._jobs[j].convert_status != "done"
        ]
        self.convert_table.setRowCount(len(rows))
        for i, job in enumerate(rows):
            title_it = QTableWidgetItem(job.candidate.title)
            title_it.setData(Qt.ItemDataRole.UserRole, job.job_id)
            self.convert_table.setItem(i, 0, title_it)
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
            self.convert_table.setItem(i, 1, QTableWidgetItem(detail))
            if job.convert_indeterminate:
                prog_txt = "…"
            else:
                prog_txt = f"{job.convert_percent:.1f}%"
            self.convert_table.setItem(i, 2, QTableWidgetItem(prog_txt))
            self.convert_table.setCellWidget(i, 3, self._convert_actions_widget(job))

    def _convert_actions_widget(self, job: UiJob) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(2, 0, 2, 0)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setEnabled(job.convert_status in ("queued", "waiting", "converting"))
        cancel_btn.clicked.connect(lambda *, jid=job.job_id: self._cancel_convert_for_job(jid))
        restart_btn = QPushButton("Restart")
        raw_ok = job.raw_path is not None and job.raw_path.is_file()
        restart_btn.setEnabled(
            raw_ok and job.convert_status in ("failed", "cancelled")
        )
        restart_btn.clicked.connect(lambda *, jid=job.job_id: self._restart_convert_for_job(jid))
        h.addWidget(cancel_btn)
        h.addWidget(restart_btn)
        return w

    def _cancel_download_for_job(self, job_id: str) -> None:
        self._dl.cancel_download(job_id)

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
        self._refresh_convert_table()

    def _on_cv_done(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if not job:
            return
        job.convert_status = "done"
        job.convert_percent = 100.0
        job.convert_indeterminate = False
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

    def closeEvent(self, event) -> None:
        self._persist_all_jobs()
        self._store.close()
        self._dl.stop()
        self._cv.stop()
        if self._tray is not None:
            self._tray.hide()
        super().closeEvent(event)
