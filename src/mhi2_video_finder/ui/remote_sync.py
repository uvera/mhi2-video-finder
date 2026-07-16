"""Remote-daemon job sync: import, status polling, and save-to-PC for MainWindow."""

from __future__ import annotations

import threading
from pathlib import Path

import httpx
from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QMessageBox

from mhi2_video_finder.search import VideoCandidate
from mhi2_video_finder.workflow import (
    ensure_output_dir,
    infer_subdir_name,
    remote_save_out_path,
    safe_stem,
)

from .models import UiJob
from .workers import new_job_id

# Remote-download target subfolder for jobs imported from the daemon (Telegram / API), not from this UI queue.
_REMOTE_DAEMON_IMPORT_SUBDIR = "daemon-imports"
# Poll daemon job list so Telegram/API-created jobs appear without restarting the UI.
_REMOTE_JOBS_POLL_MS = 10_000
# Poll daemon diagnostics (uptime, build info, job counts) for the Daemon Diagnostics tab.
_DIAGNOSTICS_POLL_MS = 15_000


def _display_title_for_remote_row(raw_title: str) -> str:
    t = raw_title.strip()
    return t if t else "(no title)"


class _RemoteFetchBridge(QObject):
    ok = pyqtSignal(str)
    fail = pyqtSignal(str, str)
    progress = pyqtSignal(str, int, int)


class RemoteSyncMixin:
    def _poll_remote_daemon_jobs(self) -> None:
        if self._remote is not None and self._use_remote:
            self._remote.fetch_recent_jobs_async(200)

    def _poll_diagnostics(self) -> None:
        if self._remote is not None and self._use_remote:
            self._remote.fetch_diagnostics_async()

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
