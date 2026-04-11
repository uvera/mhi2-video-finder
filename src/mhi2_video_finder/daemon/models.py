"""Daemon job state machine and row model."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class JobStatus(str, Enum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    CONVERTING = "converting"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobPhase(str, Enum):
    DOWNLOAD = "download"
    CONVERT = "convert"


@dataclass
class DaemonJobRow:
    job_id: str
    url: str
    subdir: str = ""
    output_stem: str = ""
    video_id: str = ""
    title: str = ""
    channel: str = ""
    no_embed: bool = False
    status: JobStatus = JobStatus.QUEUED
    phase: JobPhase | None = None
    download_percent: float = -1.0
    convert_percent: float = -1.0
    speed: str = ""
    eta: str = ""
    error: str = ""
    error_phase: JobPhase | None = None
    raw_path: str | None = None
    output_path: str | None = None
    ytdlp_info_json: str | None = None
    created_at: float = 0.0
    updated_at: float = 0.0
    finished_at: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_status_dict(self) -> dict[str, Any]:
        phase_out = self.phase
        if self.status == JobStatus.CANCELLED and phase_out is None and self.error_phase:
            phase_out = self.error_phase
        return {
            "job_id": self.job_id,
            "status": self.status.value,
            "phase": phase_out.value if phase_out else None,
            "title": self.title,
            "channel": self.channel,
            "url": self.url,
            "video_id": self.video_id,
            "no_embed": self.no_embed,
            "download_percent": self.download_percent,
            "convert_percent": self.convert_percent,
            "speed": self.speed,
            "eta": self.eta,
            "error": self.error,
            "error_phase": self.error_phase.value if self.error_phase else None,
            "output_path": self.output_path,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "finished_at": self.finished_at,
        }


# Transitions (documentation / validation helper)
# queued -> downloading -> converting -> done
# any non-terminal -> cancelled (user)
# any non-terminal -> failed (error) with error_phase
