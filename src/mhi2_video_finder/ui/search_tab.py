"""Search tab orchestration: running searches and populating results."""

from __future__ import annotations

from functools import partial
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QMessageBox, QTableWidgetItem

from mhi2_video_finder.config import load_settings
from mhi2_video_finder.workflow import slug_dir_name

from .workers import SearchWorker


class SearchMixin:
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
