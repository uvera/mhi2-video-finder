"""Remote daemon row → UiJob snapshot mapping (no Qt widgets)."""

from __future__ import annotations

from pathlib import Path

from mhi2_video_finder.search import VideoCandidate
from mhi2_video_finder.ui.models import UiJob
from mhi2_video_finder.ui.window import MainWindow


def _minimal_remote_job(tmp_path: Path) -> UiJob:
    cand = VideoCandidate(
        video_id="dQw4w9WgXcQ",
        title="T",
        duration=None,
        channel="C",
        url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    )
    return UiJob(
        job_id="local-1",
        candidate=cand,
        out_path=tmp_path / "o.mp4",
        backend="remote",
        remote_job_id="r1",
    )


def test_apply_snapshot_downloading_convert_phase_negative_percent_is_queued(tmp_path: Path) -> None:
    """Matches daemon gap after download, before convert worker updates status."""
    job = _minimal_remote_job(tmp_path)
    row = {
        "status": "downloading",
        "phase": "convert",
        "download_percent": 100.0,
        "convert_percent": -1.0,
    }
    MainWindow._apply_remote_status_snapshot(job, row)
    assert job.download_status == "done"
    assert job.convert_status == "queued"
    assert job.convert_percent == 0.0
    assert job.convert_indeterminate is False


def test_apply_snapshot_downloading_convert_phase_with_percent_is_converting(tmp_path: Path) -> None:
    job = _minimal_remote_job(tmp_path)
    row = {
        "status": "downloading",
        "phase": "convert",
        "download_percent": 100.0,
        "convert_percent": 12.5,
    }
    MainWindow._apply_remote_status_snapshot(job, row)
    assert job.download_status == "done"
    assert job.convert_status == "converting"
    assert job.convert_percent == 12.5


def test_apply_snapshot_converting_negative_percent_is_indeterminate(tmp_path: Path) -> None:
    job = _minimal_remote_job(tmp_path)
    row = {"status": "converting", "phase": "convert", "convert_percent": -1.0}
    MainWindow._apply_remote_status_snapshot(job, row)
    assert job.convert_status == "converting"
    assert job.convert_indeterminate is True
    assert job.convert_percent == 0.0
