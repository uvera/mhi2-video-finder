"""Two-stage job pipeline: download then transcode."""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from yt_dlp.utils import DownloadCancelled

from mhi2_video_finder.config import Settings
from mhi2_video_finder.daemon.hub import WsHub
from mhi2_video_finder.daemon.models import DaemonJobRow, JobPhase, JobStatus
from mhi2_video_finder.daemon.store import DaemonJobStore, ytdlp_info_from_json, ytdlp_info_to_json
from mhi2_video_finder.daemon.urlvalidate import validate_youtube_url
from mhi2_video_finder.debug_runtime_log import emit_debug_log as _debug_log
from mhi2_video_finder.download import download_to_cache
from mhi2_video_finder.exceptions import OperationCancelled
from mhi2_video_finder.transcode import transcode
from mhi2_video_finder.workflow import ensure_output_dir, safe_join, unique_out_path
from mhi2_video_finder.ytdlp_progress_map import ytdlp_progress_percent_and_labels

_DOWNLOAD_STUCK_SECONDS = 10.0

_log = logging.getLogger(__name__)

# YouTube video IDs are always exactly 11 URL-safe characters; used to validate
# CreateJobBody.video_id, which otherwise feeds unsanitized into unique_out_path's
# collision-suffix filename ("{stem}_{video_id}.mp4") with no other path guard.
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

# Matches absolute filesystem paths so server-side details (cache/output/state
# dirs, filenames) never leak into client-facing job error text. Quoted paths
# (Python's OSError.__str__ always quotes them, e.g. "'/a/b c.mp4'") are matched
# through the closing quote rather than stopping at the first space, since
# safe_stem() allows spaces in output filenames; unquoted paths fall back to
# stopping at whitespace.
_ABS_PATH_RE = re.compile(
    r"'/[^']*'"
    r'|"/[^"]*"'
    r"|(?<![\w.])/(?:[^\s'\"]+/)*[^\s'\":]*"
)


def _redact_paths(text: str) -> str:
    return _ABS_PATH_RE.sub("<path>", text)


class _DownloadStalled(RuntimeError):
    pass


class JobEngine:
    def __init__(
        self,
        settings: Settings,
        store: DaemonJobStore,
        hub: WsHub,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._settings = settings
        self._store = store
        self._hub = hub
        self._loop = loop
        dl_workers = max(1, min(32, int(settings.max_parallel_downloads)))
        cv_workers = max(1, min(32, int(settings.max_parallel_converts)))
        self._download_executor = ThreadPoolExecutor(
            max_workers=dl_workers, thread_name_prefix="vf-daemon-dl"
        )
        # Keep conversion isolated from downloads so new jobs can continue downloading.
        self._convert_executor = ThreadPoolExecutor(max_workers=cv_workers, thread_name_prefix="vf-daemon-cv")
        # region agent log
        _debug_log(
            "H3",
            "daemon/engine.py:67",
            "engine initialized",
            {"download_workers": dl_workers, "convert_workers": cv_workers},
        )
        # endregion
        self._lock = threading.Lock()
        self._cancel_events: dict[str, threading.Event] = {}

    @staticmethod
    def _executor_queue_size(executor: ThreadPoolExecutor) -> int | None:
        q = getattr(executor, "_work_queue", None)
        if q is None:
            return None
        try:
            return int(q.qsize())
        except Exception:
            return None

    def shutdown(self, *, wait: bool = True) -> None:
        with self._lock:
            for ev in self._cancel_events.values():
                ev.set()
        self._download_executor.shutdown(wait=wait, cancel_futures=True)
        self._convert_executor.shutdown(wait=wait, cancel_futures=True)

    def _schedule_emit(self, job_id: str, message: dict[str, Any]) -> None:
        try:
            asyncio.run_coroutine_threadsafe(self._hub.broadcast_job(job_id, message), self._loop)
        except RuntimeError:
            pass

    def emit(self, job_id: str, message: dict[str, Any]) -> None:
        self._schedule_emit(job_id, message)

    def _abort_for(self, job_id: str) -> threading.Event:
        with self._lock:
            ev = self._cancel_events.get(job_id)
            if ev is None:
                ev = threading.Event()
                self._cancel_events[job_id] = ev
            return ev

    def _clear_abort_for(self, job_id: str) -> None:
        with self._lock:
            self._cancel_events.pop(job_id, None)

    def cancel_job(self, job_id: str) -> bool:
        row = self._store.get(job_id)
        if not row:
            return False
        if row.status in (JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED):
            return False
        self._abort_for(job_id).set()
        return True

    def _validate_safe_subdir_and_stem(self, subdir: str, output_stem: str) -> None:
        """Reject subdir/output_stem values that would write outside the output dir."""
        base_out = self._settings.merged_output_dir()
        try:
            if subdir:
                safe_join(base_out, subdir)
            if output_stem:
                safe_join(base_out, f"{output_stem}.mp4")
        except ValueError as e:
            raise ValueError(f"invalid subdir or output_stem: {e}") from e

    @staticmethod
    def _validate_safe_video_id(video_id: str) -> None:
        """Reject a video_id that isn't a plain YouTube ID (it lands unsanitized in
        unique_out_path's collision-suffix filename)."""
        if video_id and not _VIDEO_ID_RE.match(video_id):
            raise ValueError("invalid video_id")

    def _safe_unlink_job_path(self, path_str: str | None) -> None:
        if not path_str:
            return
        p = Path(path_str).expanduser().resolve()
        out_base = self._settings.merged_output_dir().resolve()
        cache_base = self._settings.merged_raw_cache_dir().resolve()
        for base in (out_base, cache_base):
            try:
                p.relative_to(base)
            except ValueError:
                continue
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
            return

    def delete_job(self, job_id: str) -> bool:
        """Remove job from the store; abort if still active. Best-effort cache/output cleanup."""
        row = self._store.get(job_id)
        if not row:
            return False
        if row.status not in (JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED):
            self._abort_for(job_id).set()
        self._safe_unlink_job_path(row.raw_path)
        self._safe_unlink_job_path(row.output_path)
        self._store.delete(job_id)
        self._clear_abort_for(job_id)
        self.emit(job_id, {"type": "job_deleted", "job_id": job_id})
        return True

    def create_job(
        self,
        *,
        url: str,
        subdir: str,
        output_stem: str,
        video_id: str,
        title: str,
        channel: str,
        no_embed: bool,
    ) -> DaemonJobRow:
        url = validate_youtube_url(url)
        subdir = subdir.strip()
        output_stem = output_stem.strip()
        video_id = video_id.strip()
        self._validate_safe_subdir_and_stem(subdir, output_stem)
        self._validate_safe_video_id(video_id)
        job_id = str(uuid.uuid4())
        row = DaemonJobRow(
            job_id=job_id,
            url=url,
            subdir=subdir,
            output_stem=output_stem,
            video_id=video_id,
            title=title.strip(),
            channel=channel.strip(),
            no_embed=no_embed,
            status=JobStatus.QUEUED,
            phase=None,
        )
        self._store.insert(row)
        self._download_executor.submit(self._run_download_stage, job_id)
        # region agent log
        _debug_log(
            "H3",
            "daemon/engine.py:141",
            "job created and download submitted",
            {
                "job_id": job_id,
                "status": row.status.value,
                "download_queue_size": self._executor_queue_size(self._download_executor),
                "convert_queue_size": self._executor_queue_size(self._convert_executor),
            },
        )
        # endregion
        self.emit(
            job_id,
            {"type": "job_state_changed", "job_id": job_id, "status": "queued", "phase": None},
        )
        return row

    def sweep_expired_jobs(self, retention_seconds: float) -> int:
        """Delete finished jobs (file + row) past retention; a later request just re-downloads."""
        if retention_seconds <= 0:
            return 0
        cutoff = time.time() - retention_seconds
        rows = self._store.list_stale_done(cutoff)
        return sum(1 for row in rows if self.delete_job(row.job_id))

    def recover_non_terminal(self) -> None:
        for row in self._store.list_non_terminal():
            raw_ok = bool(
                row.raw_path and Path(row.raw_path).is_file() and Path(row.raw_path).stat().st_size > 4096
            )
            if raw_ok:
                self._convert_executor.submit(self._run_convert_stage, row.job_id)
            else:
                self._download_executor.submit(self._run_download_stage, row.job_id)

    def _run_download_stage(self, job_id: str) -> None:
        # region agent log
        _debug_log(
            "H3",
            "daemon/engine.py:164",
            "download stage entered",
            {
                "job_id": job_id,
                "thread": threading.current_thread().name,
                "download_queue_size": self._executor_queue_size(self._download_executor),
                "convert_queue_size": self._executor_queue_size(self._convert_executor),
            },
        )
        # endregion
        row = self._store.get(job_id)
        if not row:
            return
        if row.status in (JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED):
            return

        ev = self._abort_for(job_id)

        def cancelled() -> bool:
            return ev.is_set()

        if cancelled() and row.status == JobStatus.QUEUED:
            self._finish_cancelled(job_id)
            return

        raw_ok = bool(
            row.raw_path and Path(row.raw_path).is_file() and Path(row.raw_path).stat().st_size > 4096
        )
        # Resume after restart: skip download if raw cache still present
        if raw_ok:
            self._convert_executor.submit(self._run_convert_stage, job_id)
            return

        self._store.update(
            job_id,
            status=JobStatus.DOWNLOADING,
            phase=JobPhase.DOWNLOAD,
            download_percent=-1.0,
            clear_error_phase=True,
            error="",
            clear_finished=True,
            finished_at=None,
        )
        self.emit(
            job_id,
            {
                "type": "job_state_changed",
                "job_id": job_id,
                "status": JobStatus.DOWNLOADING.value,
                "phase": JobPhase.DOWNLOAD.value,
            },
        )

        cache = self._settings.merged_raw_cache_dir()
        retried_after_stall = False
        raw_path: Path | None = None
        yinfo: dict[str, Any] | None = None
        for attempt in (1, 2):
            last_pct: float | None = None
            last_pct_change_at = time.monotonic()

            def hook(d: dict[str, Any]) -> None:
                nonlocal last_pct, last_pct_change_at
                pct, speed, eta = ytdlp_progress_percent_and_labels(d)
                now = time.monotonic()
                if 0.0 <= pct < 100.0:
                    if last_pct is None or pct != last_pct:
                        last_pct = pct
                        last_pct_change_at = now
                    elif now - last_pct_change_at > _DOWNLOAD_STUCK_SECONDS:
                        raise _DownloadStalled(
                            f"download progress stuck at {pct:.2f}% for over "
                            f"{_DOWNLOAD_STUCK_SECONDS:.0f} seconds"
                        )
                self._store.update(
                    job_id,
                    download_percent=pct,
                    speed=speed or "",
                    eta=eta or "",
                )
                self.emit(
                    job_id,
                    {
                        "type": "job_progress",
                        "job_id": job_id,
                        "phase": JobPhase.DOWNLOAD.value,
                        "percent": pct,
                        "speed": speed,
                        "eta": eta,
                        "indeterminate": pct < 0,
                    },
                )

            try:
                raw_path, yinfo = download_to_cache(
                    row.url,
                    cache,
                    progress_hooks=[hook],
                    should_cancel=cancelled,
                )
                break
            except DownloadCancelled:
                self._finish_cancelled(job_id)
                return
            except _DownloadStalled:
                if attempt == 1:
                    retried_after_stall = True
                    self._store.update(job_id, download_percent=-1.0, speed="", eta="")
                    continue
                self._finish_cancelled(job_id)
                return
            except Exception as e:
                _log.exception("download stage failed for job %s", job_id)
                if retried_after_stall:
                    self._finish_cancelled(job_id)
                else:
                    self._finish_failed(job_id, JobPhase.DOWNLOAD, str(e))
                return
        if raw_path is None:
            self._finish_cancelled(job_id)
            return

        jjson = ytdlp_info_to_json(yinfo)
        vid = str(yinfo.get("id") or row.video_id or "") if yinfo else row.video_id
        title = str(yinfo.get("title") or row.title or "") if yinfo else row.title
        ch = ""
        if yinfo:
            ch = str(yinfo.get("uploader") or yinfo.get("channel") or row.channel or "")
        self._store.update(
            job_id,
            raw_path=str(raw_path),
            ytdlp_info_json=jjson,
            download_percent=100.0,
            phase=JobPhase.CONVERT,
            speed="",
            eta="",
        )
        # Refresh video_id / title / channel before notifying clients so GET /v1/jobs and
        # WebSocket payloads see metadata (e.g. Telegram-created jobs with empty title).
        self._store.update_meta(job_id, video_id=vid, title=title, channel=ch)
        state_msg: dict[str, Any] = {
            "type": "job_state_changed",
            "job_id": job_id,
            "status": JobStatus.DOWNLOADING.value,
            "phase": JobPhase.CONVERT.value,
        }
        if title:
            state_msg["title"] = title
        if ch:
            state_msg["channel"] = ch
        if vid:
            state_msg["video_id"] = vid
        self.emit(job_id, state_msg)
        self._convert_executor.submit(self._run_convert_stage, job_id)
        # region agent log
        _debug_log(
            "H3",
            "daemon/engine.py:306",
            "download finished and convert submitted",
            {
                "job_id": job_id,
                "raw_path": str(raw_path),
                "download_queue_size": self._executor_queue_size(self._download_executor),
                "convert_queue_size": self._executor_queue_size(self._convert_executor),
            },
        )
        # endregion

    def _run_convert_stage(self, job_id: str) -> None:
        # region agent log
        _debug_log(
            "H3",
            "daemon/engine.py:318",
            "convert stage entered",
            {
                "job_id": job_id,
                "thread": threading.current_thread().name,
                "download_queue_size": self._executor_queue_size(self._download_executor),
                "convert_queue_size": self._executor_queue_size(self._convert_executor),
            },
        )
        # endregion
        row = self._store.get(job_id)
        if not row:
            return
        if row.status in (JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED):
            return

        ev = self._abort_for(job_id)

        def cancelled() -> bool:
            return ev.is_set()

        if cancelled():
            self._finish_cancelled(job_id)
            return

        yinfo = ytdlp_info_from_json(row.ytdlp_info_json)
        raw_path = Path(row.raw_path) if row.raw_path else None
        if raw_path is None or not raw_path.is_file():
            self._finish_failed(job_id, JobPhase.DOWNLOAD, "missing downloaded cache file")
            return

        base_out = self._settings.merged_output_dir()
        try:
            self._validate_safe_subdir_and_stem(row.subdir.strip(), row.output_stem or "")
        except ValueError as e:
            self._finish_failed(job_id, JobPhase.CONVERT, str(e))
            return
        out_folder = ensure_output_dir(base_out / row.subdir) if row.subdir.strip() else base_out
        stem = row.output_stem or "video"
        vid_for_name = row.video_id or (yinfo or {}).get("id") or "video"
        outp = unique_out_path(out_folder, stem, str(vid_for_name))
        try:
            outp.expanduser().resolve().relative_to(out_folder.resolve())
        except ValueError:
            # video_id (unlike subdir/output_stem above) isn't re-validated here by
            # format, since it may legitimately come from yt-dlp's info dict rather
            # than the client; this catches it regardless of source by checking the
            # actual path that would be handed to transcode().
            self._finish_failed(job_id, JobPhase.CONVERT, "invalid output path")
            return

        self._store.update(
            job_id,
            status=JobStatus.CONVERTING,
            phase=JobPhase.CONVERT,
            output_path=str(outp),
            convert_percent=0.0,
        )
        self.emit(
            job_id,
            {
                "type": "job_state_changed",
                "job_id": job_id,
                "status": JobStatus.CONVERTING.value,
                "phase": JobPhase.CONVERT.value,
            },
        )

        def on_prog(p: float | None) -> None:
            pct = -1.0 if p is None else float(p)
            self._store.update(job_id, convert_percent=pct)
            self.emit(
                job_id,
                {
                    "type": "job_progress",
                    "job_id": job_id,
                    "phase": JobPhase.CONVERT.value,
                    "percent": pct,
                    "speed": "",
                    "eta": "",
                    "indeterminate": p is None,
                },
            )

        try:
            transcode(
                raw_path,
                outp,
                self._settings,
                ytdlp_info=None if row.no_embed else yinfo,
                on_encode_progress=on_prog,
                should_cancel=cancelled,
            )
        except OperationCancelled:
            self._finish_cancelled(job_id)
            return
        except Exception as e:
            _log.exception("convert stage failed for job %s", job_id)
            self._finish_failed(job_id, JobPhase.CONVERT, str(e))
            return

        # Success: remove raw cache file to save space
        try:
            raw_path.unlink(missing_ok=True)
        except OSError:
            pass

        now = time.time()
        self._store.update(
            job_id,
            status=JobStatus.DONE,
            clear_phase=True,
            convert_percent=100.0,
            finished_at=now,
            raw_path=None,
        )
        done_row = self._store.get(job_id)
        done_state: dict[str, Any] = {
            "type": "job_state_changed",
            "job_id": job_id,
            "status": JobStatus.DONE.value,
            "phase": None,
        }
        done_finish: dict[str, Any] = {
            "type": "job_done",
            "job_id": job_id,
            "download_url": f"/v1/jobs/{job_id}/download",
        }
        # Repeat title/channel/video_id on terminal events so clients that missed the post-download
        # WS (e.g. reconnect, subscribe race) still refresh UI / SQLite before showing complete.
        if done_row:
            if done_row.title:
                done_state["title"] = done_row.title
                done_finish["title"] = done_row.title
            if done_row.channel:
                done_state["channel"] = done_row.channel
                done_finish["channel"] = done_row.channel
            v = str(done_row.video_id or "").strip()
            if len(v) == 11:
                done_state["video_id"] = v
                done_finish["video_id"] = v
        self.emit(job_id, done_state)
        self.emit(job_id, done_finish)
        self._clear_abort_for(job_id)

    def _finish_failed(self, job_id: str, phase: JobPhase, err: str) -> None:
        now = time.time()
        err = _redact_paths(err)[:4000]
        self._store.update(
            job_id,
            status=JobStatus.FAILED,
            phase=phase,
            error=err,
            error_phase=phase,
            finished_at=now,
        )
        self.emit(
            job_id,
            {
                "type": "job_failed",
                "job_id": job_id,
                "error": err,
                "phase": phase.value,
            },
        )
        self._clear_abort_for(job_id)

    def _finish_cancelled(self, job_id: str) -> None:
        row = self._store.get(job_id)
        eph = row.phase if row and row.phase else JobPhase.DOWNLOAD
        now = time.time()
        self._store.update(
            job_id,
            status=JobStatus.CANCELLED,
            error_phase=eph,
            clear_phase=True,
            finished_at=now,
        )
        self.emit(
            job_id,
            {"type": "job_cancelled", "job_id": job_id, "phase": eph.value},
        )
        self._clear_abort_for(job_id)
