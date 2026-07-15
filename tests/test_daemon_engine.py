"""JobEngine download-stage retry/cancel behavior tests."""

from __future__ import annotations

import asyncio
import itertools
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from mhi2_video_finder.config import Settings
from mhi2_video_finder.daemon.engine import JobEngine
from mhi2_video_finder.daemon.hub import WsHub
from mhi2_video_finder.daemon.models import DaemonJobRow, JobStatus
from mhi2_video_finder.daemon.store import DaemonJobStore


class _ExecutorStub:
    def __init__(self) -> None:
        self.submissions: list[tuple[object, tuple[object, ...]]] = []

    def submit(self, fn: object, *args: object) -> None:
        self.submissions.append((fn, args))

    def shutdown(self, *, wait: bool = True, cancel_futures: bool = False) -> None:
        del wait, cancel_futures


def _new_engine(tmp_path: Path) -> tuple[JobEngine, DaemonJobStore]:
    store = DaemonJobStore(tmp_path / "jobs.sqlite")
    settings = Settings(raw_cache_dir=tmp_path / "raw", output_dir=tmp_path / "out")
    loop = asyncio.new_event_loop()
    engine = JobEngine(settings, store, WsHub(), loop)
    engine.emit = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    engine._convert_executor = _ExecutorStub()  # type: ignore[assignment]
    return engine, store


def test_download_stall_retries_once_then_continues(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import mhi2_video_finder.daemon.engine as engine_mod

    engine, store = _new_engine(tmp_path)
    store.insert(
        DaemonJobRow(
            job_id="j1",
            url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            status=JobStatus.QUEUED,
        )
    )

    mono = iter(itertools.chain([0.0, 0.0, 11.1, 20.0], itertools.count(30.0, 1.0)))
    monkeypatch.setattr(engine_mod.time, "monotonic", lambda: next(mono))

    calls = {"n": 0}

    def fake_download(url: str, cache_dir: Path, *, progress_hooks, should_cancel):
        del url, cache_dir, should_cancel
        calls["n"] += 1
        if calls["n"] == 1:
            progress_hooks[0]({"status": "downloading", "total_bytes": 100, "downloaded_bytes": 50})
            progress_hooks[0]({"status": "downloading", "total_bytes": 100, "downloaded_bytes": 50})
        raw = tmp_path / "raw" / "ok.mkv"
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_bytes(b"x" * 8192)
        return raw, {"id": "dQw4w9WgXcQ", "title": "T", "uploader": "C"}

    monkeypatch.setattr(engine_mod, "download_to_cache", fake_download)

    engine._run_download_stage("j1")

    assert calls["n"] == 2
    row = store.get("j1")
    assert row is not None
    assert row.status == JobStatus.DOWNLOADING
    assert row.raw_path is not None
    assert len(engine._convert_executor.submissions) == 1  # type: ignore[attr-defined]

    store.close()
    engine.shutdown(wait=False)


def test_sweep_expired_jobs_deletes_stale_done_output_and_row(tmp_path: Path) -> None:
    engine, store = _new_engine(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    stale_out = out_dir / "stale.mp4"
    stale_out.write_bytes(b"x")
    fresh_out = out_dir / "fresh.mp4"
    fresh_out.write_bytes(b"x")

    now = time.time()
    store.insert(
        DaemonJobRow(
            job_id="stale",
            url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            status=JobStatus.DONE,
            output_path=str(stale_out),
            finished_at=now - 4 * 86400,
        )
    )
    store.insert(
        DaemonJobRow(
            job_id="fresh",
            url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            status=JobStatus.DONE,
            output_path=str(fresh_out),
            finished_at=now - 1 * 86400,
        )
    )

    deleted = engine.sweep_expired_jobs(3 * 86400)

    assert deleted == 1
    assert store.get("stale") is None
    assert store.get("fresh") is not None
    assert not stale_out.exists()
    assert fresh_out.exists()

    store.close()
    engine.shutdown(wait=False)


def test_sweep_expired_jobs_disabled_when_retention_non_positive(tmp_path: Path) -> None:
    engine, store = _new_engine(tmp_path)
    store.insert(
        DaemonJobRow(
            job_id="old",
            url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            status=JobStatus.DONE,
            finished_at=time.time() - 30 * 86400,
        )
    )

    assert engine.sweep_expired_jobs(0) == 0
    assert store.get("old") is not None

    store.close()
    engine.shutdown(wait=False)


def test_download_stall_then_second_failure_cancels_job(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import mhi2_video_finder.daemon.engine as engine_mod

    engine, store = _new_engine(tmp_path)
    store.insert(
        DaemonJobRow(
            job_id="j2",
            url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            status=JobStatus.QUEUED,
        )
    )

    mono = iter(itertools.chain([0.0, 0.0, 11.1, 20.0], itertools.count(30.0, 1.0)))
    monkeypatch.setattr(engine_mod.time, "monotonic", lambda: next(mono))

    calls = {"n": 0}

    def fake_download(url: str, cache_dir: Path, *, progress_hooks, should_cancel):
        del url, cache_dir, should_cancel
        calls["n"] += 1
        if calls["n"] == 1:
            progress_hooks[0]({"status": "downloading", "total_bytes": 100, "downloaded_bytes": 50})
            progress_hooks[0]({"status": "downloading", "total_bytes": 100, "downloaded_bytes": 50})
        raise RuntimeError("second attempt failed")

    monkeypatch.setattr(engine_mod, "download_to_cache", fake_download)

    engine._run_download_stage("j2")

    assert calls["n"] == 2
    row = store.get("j2")
    assert row is not None
    assert row.status == JobStatus.CANCELLED

    store.close()
    engine.shutdown(wait=False)
