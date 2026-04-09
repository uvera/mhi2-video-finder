"""UI job state."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from video_finder.search import VideoCandidate


@dataclass
class UiJob:
    job_id: str
    candidate: VideoCandidate
    out_path: Path
    no_embed: bool = False
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

    def download_done(self) -> bool:
        return self.download_status == "done"

    def convert_done(self) -> bool:
        return self.convert_status == "done"
