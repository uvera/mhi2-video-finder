"""SQLite job store for GUI persistence."""

from pathlib import Path

from mhi2_video_finder.search import VideoCandidate
from mhi2_video_finder.ui.job_store import JobStore
from mhi2_video_finder.ui.models import UiJob


def test_job_store_roundtrip(tmp_path: Path) -> None:
    db = tmp_path / "jobs.sqlite"
    store = JobStore(db)
    cand = VideoCandidate(
        video_id="dQw4w9WgXcQ",
        title="Test",
        duration=212,
        channel="Ch",
        url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    )
    job = UiJob(
        job_id="abc-123",
        candidate=cand,
        out_path=tmp_path / "out.mp4",
        no_embed=True,
        download_status="queued",
        convert_status="waiting",
        backend="remote",
        remote_job_id="remote-1",
        remote_job_origin="remote_sync",
    )
    store.upsert(job, 0)
    loaded = store.load_all()
    assert len(loaded) == 1
    j2 = loaded[0]
    assert j2.job_id == job.job_id
    assert j2.candidate.video_id == cand.video_id
    assert j2.no_embed is True
    assert j2.out_path == job.out_path
    assert j2.remote_job_origin == "remote_sync"
    assert j2.remote_job_id == "remote-1"
    store.close()


def test_job_store_deletes_when_fully_done(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "j.sqlite")
    vid = "x" * 11
    cand = VideoCandidate(
        video_id=vid,
        title="t",
        duration=None,
        channel="c",
        url=f"https://youtu.be/{vid}",
    )
    job = UiJob(
        job_id="done-id",
        candidate=cand,
        out_path=tmp_path / "o.mp4",
        download_status="done",
        convert_status="done",
    )
    store.upsert(job, 0)
    assert store.load_all() == []
    store.close()


def test_job_store_keeps_remote_fully_done(tmp_path: Path) -> None:
    """Remote jobs stay in the DB after conversion (and Save to PC) until the user removes them."""
    store = JobStore(tmp_path / "r.sqlite")
    vid = "y" * 11
    cand = VideoCandidate(
        video_id=vid,
        title="Remote done",
        duration=None,
        channel="c",
        url=f"https://youtu.be/{vid}",
    )
    job = UiJob(
        job_id="remote-done",
        candidate=cand,
        out_path=tmp_path / "out.mp4",
        backend="remote",
        remote_job_id="srv-1",
        remote_saved_locally=True,
        download_status="done",
        convert_status="done",
    )
    store.upsert(job, 0)
    loaded = store.load_all()
    assert len(loaded) == 1
    assert loaded[0].job_id == "remote-done"
    assert loaded[0].remote_saved_locally is True
    store.close()
