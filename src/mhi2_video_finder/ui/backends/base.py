"""Processing backend protocol (local DownloadService/ConvertService vs remote)."""

from __future__ import annotations

from typing import Any, Protocol

from PyQt6.QtCore import QObject


class DownloadBackend(Protocol):
    """Same signals as :class:`mhi2_video_finder.ui.workers.DownloadService`."""

    progress: Any
    item_done: Any
    item_failed: Any
    download_cancelled: Any

    def set_settings(self, settings: Any) -> None: ...
    def set_max_workers(self, n: int) -> None: ...
    def cancel_download(self, job_id: str) -> None: ...
    def enqueue(self, job_id: str, url: str) -> None: ...
    def stop(self) -> None: ...


class ConvertBackend(Protocol):
    """Same signals as :class:`mhi2_video_finder.ui.workers.ConvertService`."""

    progress: Any
    item_done: Any
    item_failed: Any
    convert_cancelled: Any

    def set_settings(self, settings: Any) -> None: ...
    def set_max_workers(self, n: int) -> None: ...
    def set_no_embed(self, v: bool) -> None: ...
    def cancel_convert(self, job_id: str) -> None: ...
    def enqueue(
        self,
        job_id: str,
        raw_path: Any,
        out_path: Any,
        yinfo: dict | None,
        *,
        no_embed: bool,
    ) -> None: ...
    def stop(self) -> None: ...


class HasDownloadConvert(QObject, Protocol):
    dl: DownloadBackend
    cv: ConvertBackend
