"""UI job state."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mhi2_video_finder.search import VideoCandidate


@dataclass
class UiJob:
    job_id: str
    candidate: VideoCandidate
    out_path: Path
    no_embed: bool = False
    # "local" = this machine runs yt-dlp + ffmpeg; "remote" = server daemon
    backend: str = "local"
    # Remote: whether the MP4 has been fetched to out_path (row can stay in the list after save)
    remote_saved_locally: bool = True
    # Remote: daemon job id (REST/WS); local job_id stays stable for the UI row
    remote_job_id: str | None = None
    # Remote: "desktop" = queued from this app (may auto-download when done); "remote_sync" =
    # imported from daemon list (manual save only)
    remote_job_origin: str = "desktop"
    # Download phase
    download_status: str = "queued"  # queued | downloading | done | failed
    download_percent: float = -1.0  # -1 = unknown
    download_speed: str = ""
    download_eta: str = ""
    download_error: str = ""
    raw_path: Path | None = None
    ytdlp_info: dict[str, Any] | None = None
    # Convert phase
    convert_status: str = "waiting"  # waiting | queued | converting | done | failed
    convert_percent: float = 0.0
    convert_indeterminate: bool = False
    convert_error: str = ""
    # Remote save-to-PC transfer state (transient, not persisted).
    remote_fetch_in_progress: bool = False
    remote_fetch_bytes_done: int = 0
    remote_fetch_bytes_total: int = 0
    # Convert tab metadata editor state (not tied to download/convert pipeline).
    meta_artist: str = ""
    meta_title: str = ""
    meta_filename_stem: str = ""
    meta_probe_summary: str = ""
    meta_guess_status: str = "None"

    def download_done(self) -> bool:
        return self.download_status == "done"

    def convert_done(self) -> bool:
        return self.convert_status == "done"
