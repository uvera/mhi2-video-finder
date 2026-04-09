"""Background QThreads for search, download, and transcode."""

from __future__ import annotations

import queue
import uuid
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QThread, pyqtSignal

from video_finder.config import Settings
from video_finder.download import download_to_cache
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
            from video_finder.search import list_channel_videos, list_playlist_videos
            from video_finder.workflow import gather_search_rows

            mf = self._settings.match_filter
            n = self._limit
            if self._mode == "channel":
                rows = list_channel_videos(self._query, limit=n, match_filter=mf)
            elif self._mode == "playlist":
                rows = list_playlist_videos(self._query, limit=n, match_filter=mf)
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
            self.finished_ok.emit(rows)
        except Exception as e:
            self.failed.emit(str(e))


class DownloadService(QThread):
    """Sequential download queue; emits progress and completion per job id."""

    progress = pyqtSignal(str, float, str, str)  # id, pct, speed, eta
    item_done = pyqtSignal(str, str, object)  # id, raw_path str, yinfo
    item_failed = pyqtSignal(str, str)

    def __init__(self, settings: Settings, parent=None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._q: queue.Queue[tuple[str, str] | None] = queue.Queue()
        self._stop = False

    def stop(self) -> None:
        self._stop = True
        self._q.put(None)

    def enqueue(self, job_id: str, url: str) -> None:
        self._q.put((job_id, url))

    def run(self) -> None:
        cache = self._settings.merged_raw_cache_dir()
        while not self._stop:
            try:
                item = self._q.get(timeout=0.4)
            except queue.Empty:
                continue
            if item is None:
                if self._stop:
                    break
                continue
            job_id, url = item

            def hook(d: dict[str, Any], jid: str = job_id) -> None:
                pct, speed, eta = ytdlp_progress_percent_and_labels(d)
                self.progress.emit(jid, pct, speed, eta)

            try:
                raw, yinfo = download_to_cache(url, cache, progress_hooks=[hook])
                self.item_done.emit(job_id, str(raw), yinfo)
            except Exception as e:
                self.item_failed.emit(job_id, str(e))


class ConvertService(QThread):
    """Sequential transcode queue after downloads."""

    progress = pyqtSignal(str, object)  # id, percent float or None for indeterminate
    item_done = pyqtSignal(str)
    item_failed = pyqtSignal(str, str)

    def __init__(self, settings: Settings, *, no_embed: bool, parent=None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._no_embed = no_embed
        self._q: queue.Queue[tuple[str, Path, Path, dict[str, Any] | None] | None] = queue.Queue()
        self._stop = False

    def set_no_embed(self, v: bool) -> None:
        self._no_embed = v

    def stop(self) -> None:
        self._stop = True
        self._q.put(None)

    def enqueue(self, job_id: str, raw_path: Path, out_path: Path, yinfo: dict[str, Any] | None) -> None:
        self._q.put((job_id, raw_path, out_path, yinfo))

    def run(self) -> None:
        while not self._stop:
            try:
                item = self._q.get(timeout=0.4)
            except queue.Empty:
                continue
            if item is None:
                if self._stop:
                    break
                continue
            job_id, raw_path, out_path, yinfo = item

            def on_prog(p: float | None, jid: str = job_id) -> None:
                self.progress.emit(jid, p)

            try:
                transcode(
                    raw_path,
                    out_path,
                    self._settings,
                    ytdlp_info=None if self._no_embed else yinfo,
                    on_encode_progress=on_prog,
                )
                self.item_done.emit(job_id)
            except Exception as e:
                self.item_failed.emit(job_id, str(e))


def new_job_id() -> str:
    return str(uuid.uuid4())
