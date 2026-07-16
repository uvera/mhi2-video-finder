"""Background QThreads for search, download, and transcode."""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QObject, QThread, pyqtSignal
from yt_dlp.utils import DownloadCancelled

from mhi2_video_finder.config import Settings
from mhi2_video_finder.download import download_to_cache
from mhi2_video_finder.exceptions import OperationCancelled
from mhi2_video_finder.search import VideoCandidate
from mhi2_video_finder.transcode import transcode

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
            from mhi2_video_finder.search import list_channel_videos, list_playlist_videos, video_from_url
            from mhi2_video_finder.workflow import gather_search_rows

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


class BulkUrlResolveWorker(QThread):
    """Resolve many watch URLs to VideoCandidate in the background (yt-dlp per URL)."""

    finished_ok = pyqtSignal(list, list)  # list[VideoCandidate], list[tuple[str, str]] url, err

    def __init__(self, urls: list[str], settings: Settings, parent=None) -> None:
        super().__init__(parent)
        self._urls = urls
        self._settings = settings

    def run(self) -> None:
        from mhi2_video_finder.search import video_from_url

        mf = self._settings.match_filter
        ok: list[VideoCandidate] = []
        failures: list[tuple[str, str]] = []
        for u in self._urls:
            if self.isInterruptionRequested():
                return
            try:
                ok.append(video_from_url(u, match_filter=mf))
            except Exception as e:
                failures.append((u, str(e)))
        if self.isInterruptionRequested():
            return
        self.finished_ok.emit(ok, failures)


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


class LibraryScanWorker(QThread):
    """Recursive folder walk off the GUI thread (Library » Find all videos)."""

    finished_ok = pyqtSignal(list)
    failed = pyqtSignal(str)

    def __init__(self, root: Path, parent=None) -> None:
        super().__init__(parent)
        self._root = root

    def run(self) -> None:
        try:
            from mhi2_video_finder.local_library import iter_video_files_recursive

            if self.isInterruptionRequested():
                return
            paths = iter_video_files_recursive(self._root)
            resolved_strs: list[str] = []
            for p in paths:
                try:
                    resolved_strs.append(str(p.resolve()))
                except OSError:
                    resolved_strs.append(str(p))
            self.finished_ok.emit(resolved_strs)
        except Exception as e:
            if self.isInterruptionRequested():
                return
            self.failed.emit(str(e).strip() or "Could not scan folder.")


class LibraryProbeWorker(QThread):
    """ffprobe in background for Library tab selection."""

    finished_ok = pyqtSignal(int, str, str, str, str)  # row_idx, author, title, summary, rel_display
    failed = pyqtSignal(int, str)

    def __init__(self, row_idx: int, path: Path, *, root: Path | None = None, parent=None) -> None:
        super().__init__(parent)
        self._row_idx = row_idx
        self._path = path
        self._root = root

    def run(self) -> None:
        try:
            from mhi2_video_finder.ffprobe_util import (
                ffprobe_json,
                format_tags_from_probe,
                summarize_probe,
            )

            if self.isInterruptionRequested():
                return
            data = ffprobe_json(self._path)
            author, title = format_tags_from_probe(data)
            summary = summarize_probe(data)
            rel = str(self._path)
            if self._root is not None:
                try:
                    rel = str(self._path.resolve().relative_to(self._root.resolve()))
                except ValueError:
                    rel = str(self._path)
            self.finished_ok.emit(self._row_idx, author, title, summary, rel)
        except Exception as e:
            if self.isInterruptionRequested():
                return
            self.failed.emit(self._row_idx, str(e))


class LibrarySaveWorker(QThread):
    """Remux metadata / rename on a background thread (ffmpeg must not block the UI)."""

    finished_ok = pyqtSignal(int, object)
    failed = pyqtSignal(int, str)

    def __init__(
        self,
        row_idx: int,
        settings: Settings,
        path: Path,
        author: str,
        song_name: str,
        filename_stem: str,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._row_idx = row_idx
        self._settings = settings
        self._path = path
        self._author = author
        self._song_name = song_name
        self._filename_stem = filename_stem

    def run(self) -> None:
        try:
            from mhi2_video_finder.local_tags import apply_author_song_and_filename

            if self.isInterruptionRequested():
                return
            new_path = apply_author_song_and_filename(
                self._path,
                author=self._author,
                song_name=self._song_name,
                filename_stem=self._filename_stem,
                settings_nice=self._settings.ffmpeg_nice,
                settings_cpu_limit=self._settings.ffmpeg_cpu_limit_percent,
            )
            self.finished_ok.emit(self._row_idx, new_path)
        except Exception as e:
            if self.isInterruptionRequested():
                return
            self.failed.emit(
                self._row_idx,
                str(e).strip() or "Could not save the file.",
            )


class LibraryBulkRenameToSongWorker(QThread):
    """Rename selected library files to song title from embedded metadata."""

    row_renamed = pyqtSignal(int, object, str)  # row_idx, new_path, song_title
    row_skipped = pyqtSignal(int, str)
    row_failed = pyqtSignal(int, str)
    batch_done = pyqtSignal(int, int, int)  # renamed, skipped, failed

    def __init__(self, targets: list[tuple[int, Path]], parent=None) -> None:
        super().__init__(parent)
        self._targets = targets

    def run(self) -> None:
        from mhi2_video_finder.ffprobe_util import ffprobe_json, format_tags_from_probe
        from mhi2_video_finder.local_tags import rename_if_needed

        renamed = 0
        skipped = 0
        failed = 0
        for row_idx, path in self._targets:
            if self.isInterruptionRequested():
                return
            try:
                p = path.expanduser().resolve()
                if not p.is_file():
                    failed += 1
                    self.row_failed.emit(row_idx, "File is missing.")
                    continue
                data = ffprobe_json(p)
                _artist, title = format_tags_from_probe(data)
                song_title = (title or "").strip()
                if not song_title:
                    skipped += 1
                    self.row_skipped.emit(row_idx, "No song title found in metadata.")
                    continue
                new_path = rename_if_needed(p, song_title)
                renamed += 1
                self.row_renamed.emit(row_idx, new_path, song_title)
            except Exception as e:
                failed += 1
                self.row_failed.emit(row_idx, str(e).strip() or "Could not rename file.")
        self.batch_done.emit(renamed, skipped, failed)


class LibraryBulkClearMetadataWorker(QThread):
    """Clear metadata for selected library files in the background."""

    row_cleared = pyqtSignal(int)
    row_failed = pyqtSignal(int, str)
    batch_done = pyqtSignal(int, int)  # cleared, failed

    def __init__(self, targets: list[tuple[int, Path]], settings: Settings, parent=None) -> None:
        super().__init__(parent)
        self._targets = targets
        self._settings = settings

    def run(self) -> None:
        from mhi2_video_finder.local_tags import clear_metadata_inplace

        cleared = 0
        failed = 0
        for row_idx, path in self._targets:
            if self.isInterruptionRequested():
                return
            try:
                p = path.expanduser().resolve()
                if not p.is_file():
                    failed += 1
                    self.row_failed.emit(row_idx, "File is missing.")
                    continue
                clear_metadata_inplace(
                    p,
                    nice_delta=self._settings.ffmpeg_nice,
                    cpu_limit_percent=self._settings.ffmpeg_cpu_limit_percent,
                )
                cleared += 1
                self.row_cleared.emit(row_idx)
            except Exception as e:
                failed += 1
                self.row_failed.emit(row_idx, str(e).strip() or "Could not clear metadata.")
        self.batch_done.emit(cleared, failed)


class GroqInferWorker(QThread):
    """Background ffprobe (when needed) + Groq inference — never block the GUI thread."""

    finished_ok = pyqtSignal(str, str)
    failed = pyqtSignal(str)
    probe_cached = pyqtSignal(int, str)
    skipped = pyqtSignal(int, str)

    def __init__(
        self,
        settings: Settings,
        *,
        row_idx: int,
        filename: str,
        folder_hint: str,
        path: Path | None,
        probe_summary: str,
        skip_if_existing_tags: bool = False,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._row_idx = row_idx
        self._filename = filename
        self._folder_hint = (folder_hint or "").strip()
        self._path = path
        self._probe_summary = (probe_summary or "").strip()
        self._skip_if_existing_tags = skip_if_existing_tags

    def run(self) -> None:
        try:
            from mhi2_video_finder.ffprobe_util import (
                ffprobe_json,
                format_tags_from_probe,
                summarize_probe,
            )
            from mhi2_video_finder.groq_infer import GroqInferenceError, infer_author_song

            if self.isInterruptionRequested():
                return

            summary = self._probe_summary
            need_probe_json = self._skip_if_existing_tags or not summary

            if need_probe_json:
                if self._path is None:
                    self.failed.emit("No media summary and no file path for ffprobe.")
                    return
                try:
                    data = ffprobe_json(self._path)
                except Exception as e:
                    self.failed.emit(str(e))
                    return
                artist_tag, title_tag = format_tags_from_probe(data)
                if self._skip_if_existing_tags and artist_tag.strip() and title_tag.strip():
                    self.skipped.emit(
                        self._row_idx,
                        "Already has artist and title in the file.",
                    )
                    return
                if not summary:
                    summary = summarize_probe(data)
                    self.probe_cached.emit(self._row_idx, summary)

            author, song = infer_author_song(
                self._settings,
                filename=self._filename,
                folder_hint=self._folder_hint,
                probe_summary=summary,
            )
            self.finished_ok.emit(author, song)
        except GroqInferenceError as e:
            self.failed.emit(str(e))
        except Exception as e:
            if self.isInterruptionRequested():
                return
            self.failed.emit(str(e))


def new_job_id() -> str:
    return str(uuid.uuid4())
