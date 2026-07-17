"""Convert tab mixin: AI metadata tagging pipeline (probe, Groq infer, save)."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QTableWidgetItem

from mhi2_video_finder.config import save_settings

from .library_ai import _MAX_PARALLEL_LIBRARY_GROQ
from .models import UiJob
from .workers import GroqInferWorker, LibraryProbeWorker, LibrarySaveWorker


class ConvertAIMixin:
    """Convert tab: AI metadata tagging pipeline (probe, Groq infer, save)."""

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
