"""Job queue orchestration: enqueue, persist, refresh tables, and download/convert callbacks."""

from __future__ import annotations

from functools import partial
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QWidget,
)

from mhi2_video_finder.config import load_settings
from mhi2_video_finder.paste_urls import parse_pasted_video_urls
from mhi2_video_finder.search import VideoCandidate
from mhi2_video_finder.workflow import (
    ensure_output_dir,
    remote_save_out_path,
    safe_stem,
    unique_out_path,
)

from .models import UiJob
from .workers import BulkUrlResolveWorker, new_job_id


class JobQueueMixin:
    """Job queue orchestration mixed into `MainWindow`."""

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
