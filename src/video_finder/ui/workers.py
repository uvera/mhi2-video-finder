"""Background QThreads for search, download, and transcode."""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QObject, QThread, pyqtSignal
from yt_dlp.utils import DownloadCancelled

from video_finder.config import Settings
from video_finder.download import download_to_cache
from video_finder.exceptions import OperationCancelled
from video_finder.transcode import transcode

from .progress_util import ytdlp_progress_percent_and_labels


class SearchWorker(QThread):
    finished_ok = pyqtSignal(list)
    failed = pyqtSignal(str)

    def __init__(
        self,
        *,
        mode: str,
        query: str,
        settings: Settings,
        use_youtube_api: bool,
        artist: str,
        title: str,
        template: str,
        limit: int | None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._mode = mode
        self._query = query.strip()
        self._settings = settings
        self._use_api = use_youtube_api
        self._artist = artist.strip() or None
        self._title = title.strip() or None
        self._template = template
        self._limit = limit

    def run(self) -> None:
        try:
            from video_finder.search import list_channel_videos, list_playlist_videos, video_from_url
            from video_finder.workflow import gather_search_rows

            if self.isInterruptionRequested():
                return

            mf = self._settings.match_filter
            n = self._limit
            if self._mode == "channel":
                rows = list_channel_videos(self._query, limit=n, match_filter=mf)
            elif self._mode == "playlist":
                rows = list_playlist_videos(self._query, limit=n, match_filter=mf)
            elif self._mode == "video_url":
                rows = [video_from_url(self._query, match_filter=mf)]
            elif self._mode == "search" and self._use_api:
                _, rows = gather_search_rows(
                    self._settings,
                    n=n,
                    mf=mf,
                    use_youtube_api=True,
                    query=self._query,
                    artist=self._artist,
                    title=self._title,
                    template=self._template,
                    channel_id=None,
                )
            elif self._mode == "search" and self._artist:
                _, rows = gather_search_rows(
                    self._settings,
                    n=n,
                    mf=mf,
                    use_youtube_api=False,
                    query="",
                    artist=self._artist,
                    title=self._title,
                    template=self._template,
                    channel_id=None,
                )
            else:
                _, rows = gather_search_rows(
                    self._settings,
                    n=n,
                    mf=mf,
                    use_youtube_api=False,
                    query=self._query,
                    artist=None,
                    title=None,
                    template=self._template,
                    channel_id=None,
                )
            if self.isInterruptionRequested():
                return
            self.finished_ok.emit(rows)
        except Exception as e:
            if self.isInterruptionRequested():
                return
            self.failed.emit(str(e))


class DownloadService(QObject):
    """Parallel downloads with per-job cancel."""

    progress = pyqtSignal(str, float, str, str)  # id, pct, speed, eta
    item_done = pyqtSignal(str, str, object)  # id, raw_path str, yinfo
    item_failed = pyqtSignal(str, str)
    download_cancelled = pyqtSignal(str)

    def __init__(self, settings: Settings, parent=None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._max_workers = max(1, min(32, int(settings.max_parallel_downloads)))
        self._executor = ThreadPoolExecutor(
            max_workers=self._max_workers,
            thread_name_prefix="vf-dl",
        )
        self._lock = threading.Lock()
        self._pending_cancel: set[str] = set()
        self._abort_events: dict[str, threading.Event] = {}
        self._stopped = False

    def set_settings(self, settings: Settings) -> None:
        self._settings = settings

    def set_max_workers(self, n: int) -> None:
        """Replace the pool after in-flight work on the old pool finishes (waits)."""
        n = max(1, min(32, int(n)))
        with self._lock:
            if self._stopped or n == self._max_workers:
                return
            old = self._executor
            self._executor = ThreadPoolExecutor(max_workers=n, thread_name_prefix="vf-dl")
            self._max_workers = n
        old.shutdown(wait=True, cancel_futures=False)

    def cancel_download(self, job_id: str) -> None:
        with self._lock:
            if job_id in self._abort_events:
                self._abort_events[job_id].set()
            else:
                self._pending_cancel.add(job_id)

    def enqueue(self, job_id: str, url: str) -> None:
        with self._lock:
            if self._stopped:
                return
        try:
            self._executor.submit(self._run_download, job_id, url)
        except RuntimeError:
            pass

    def _run_download(self, job_id: str, url: str) -> None:
        with self._lock:
            if self._stopped:
                return
            if job_id in self._pending_cancel:
                self._pending_cancel.discard(job_id)
                self.download_cancelled.emit(job_id)
                return
            ev = threading.Event()
            self._abort_events[job_id] = ev

        cache = self._settings.merged_raw_cache_dir()

        def hook(d: dict[str, Any], jid: str = job_id) -> None:
            pct, speed, eta = ytdlp_progress_percent_and_labels(d)
            self.progress.emit(jid, pct, speed, eta)

        try:
            raw, yinfo = download_to_cache(
                url,
                cache,
                progress_hooks=[hook],
                should_cancel=lambda: ev.is_set(),
            )
            self.item_done.emit(job_id, str(raw), yinfo)
        except DownloadCancelled:
            self.download_cancelled.emit(job_id)
        except Exception as e:
            self.item_failed.emit(job_id, str(e))
        finally:
            with self._lock:
                self._abort_events.pop(job_id, None)

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            for ev in list(self._abort_events.values()):
                ev.set()
        self._executor.shutdown(wait=True, cancel_futures=True)


class ConvertService(QObject):
    """Parallel transcodes with per-job cancel."""

    progress = pyqtSignal(str, object)  # id, percent float or None for indeterminate
    item_done = pyqtSignal(str)
    item_failed = pyqtSignal(str, str)
    convert_cancelled = pyqtSignal(str)

    def __init__(self, settings: Settings, *, no_embed: bool, parent=None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._no_embed = no_embed
        self._max_workers = max(1, min(32, int(settings.max_parallel_converts)))
        self._executor = ThreadPoolExecutor(
            max_workers=self._max_workers,
            thread_name_prefix="vf-cv",
        )
        self._lock = threading.Lock()
        self._pending_cancel: set[str] = set()
        self._abort_events: dict[str, threading.Event] = {}
        self._stopped = False

    def set_settings(self, settings: Settings) -> None:
        self._settings = settings

    def set_max_workers(self, n: int) -> None:
        n = max(1, min(32, int(n)))
        with self._lock:
            if self._stopped or n == self._max_workers:
                return
            old = self._executor
            self._executor = ThreadPoolExecutor(max_workers=n, thread_name_prefix="vf-cv")
            self._max_workers = n
        old.shutdown(wait=True, cancel_futures=False)

    def cancel_convert(self, job_id: str) -> None:
        with self._lock:
            if job_id in self._abort_events:
                self._abort_events[job_id].set()
            else:
                self._pending_cancel.add(job_id)

    def set_no_embed(self, v: bool) -> None:
        self._no_embed = v

    def enqueue(
        self,
        job_id: str,
        raw_path: Path,
        out_path: Path,
        yinfo: dict[str, Any] | None,
        *,
        no_embed: bool,
    ) -> None:
        with self._lock:
            if self._stopped:
                return
        try:
            self._executor.submit(self._run_convert, job_id, raw_path, out_path, yinfo, no_embed)
        except RuntimeError:
            pass

    def _run_convert(
        self,
        job_id: str,
        raw_path: Path,
        out_path: Path,
        yinfo: dict[str, Any] | None,
        no_embed: bool,
    ) -> None:
        with self._lock:
            if self._stopped:
                return
            if job_id in self._pending_cancel:
                self._pending_cancel.discard(job_id)
                self.convert_cancelled.emit(job_id)
                return
            ev = threading.Event()
            self._abort_events[job_id] = ev

        def on_prog(p: float | None, jid: str = job_id) -> None:
            self.progress.emit(jid, p)

        try:
            transcode(
                raw_path,
                out_path,
                self._settings,
                ytdlp_info=None if no_embed else yinfo,
                on_encode_progress=on_prog,
                should_cancel=lambda: ev.is_set(),
            )
            self.item_done.emit(job_id)
        except OperationCancelled:
            self.convert_cancelled.emit(job_id)
        except Exception as e:
            self.item_failed.emit(job_id, str(e))
        finally:
            with self._lock:
                self._abort_events.pop(job_id, None)

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            for ev in list(self._abort_events.values()):
                ev.set()
        self._executor.shutdown(wait=True, cancel_futures=True)


def new_job_id() -> str:
    return str(uuid.uuid4())
