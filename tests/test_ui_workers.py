"""Characterization tests for DownloadService/ConvertService, ahead of extracting
a shared base class. These call the private _run_download/_run_convert methods
directly (same pattern test_daemon_engine.py uses for the daemon's stage
functions) so no QApplication/event loop is needed for signal delivery."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

pytest.importorskip("PyQt6")

from yt_dlp.utils import DownloadCancelled

from mhi2_video_finder.config import Settings
from mhi2_video_finder.exceptions import OperationCancelled
from mhi2_video_finder.ui.workers import ConvertService, DownloadService


def _settings(tmp_path: Path) -> Settings:
    return Settings(raw_cache_dir=tmp_path / "raw", output_dir=tmp_path / "out")


def test_download_service_run_download_success(tmp_path: Path) -> None:
    svc = DownloadService(_settings(tmp_path))
    done: list[tuple[str, str, object]] = []
    svc.item_done.connect(lambda jid, raw, yinfo: done.append((jid, raw, yinfo)))
    raw_path = tmp_path / "raw" / "x.mkv"

    with patch(
        "mhi2_video_finder.ui.workers.download_to_cache",
        return_value=(raw_path, {"id": "abc"}),
    ) as m:
        svc._run_download("job1", "https://example.com/watch?v=abc")

    assert done == [("job1", str(raw_path), {"id": "abc"})]
    m.assert_called_once()
    assert "job1" not in svc._abort_events


def test_download_service_run_download_pending_cancel_skips_download(tmp_path: Path) -> None:
    svc = DownloadService(_settings(tmp_path))
    cancelled: list[str] = []
    svc.download_cancelled.connect(cancelled.append)
    svc._pending_cancel.add("job1")

    with patch("mhi2_video_finder.ui.workers.download_to_cache") as m:
        svc._run_download("job1", "https://example.com/watch?v=abc")

    m.assert_not_called()
    assert cancelled == ["job1"]
    assert "job1" not in svc._pending_cancel


def test_download_service_run_download_cancelled_during_download(tmp_path: Path) -> None:
    svc = DownloadService(_settings(tmp_path))
    cancelled: list[str] = []
    svc.download_cancelled.connect(cancelled.append)

    with patch(
        "mhi2_video_finder.ui.workers.download_to_cache",
        side_effect=DownloadCancelled("stopped"),
    ):
        svc._run_download("job1", "https://example.com/watch?v=abc")

    assert cancelled == ["job1"]


def test_download_service_run_download_failure(tmp_path: Path) -> None:
    svc = DownloadService(_settings(tmp_path))
    failed: list[tuple[str, str]] = []
    svc.item_failed.connect(lambda jid, err: failed.append((jid, err)))

    with patch(
        "mhi2_video_finder.ui.workers.download_to_cache",
        side_effect=RuntimeError("boom"),
    ):
        svc._run_download("job1", "https://example.com/watch?v=abc")

    assert failed == [("job1", "boom")]


def test_download_service_cancel_download_sets_existing_event(tmp_path: Path) -> None:
    import threading

    svc = DownloadService(_settings(tmp_path))
    ev = threading.Event()
    svc._abort_events["job1"] = ev
    svc.cancel_download("job1")
    assert ev.is_set()


def test_download_service_cancel_download_no_event_yet_marks_pending(tmp_path: Path) -> None:
    svc = DownloadService(_settings(tmp_path))
    svc.cancel_download("job1")
    assert "job1" in svc._pending_cancel


def test_download_service_stop_sets_all_abort_events(tmp_path: Path) -> None:
    import threading

    svc = DownloadService(_settings(tmp_path))
    ev = threading.Event()
    svc._abort_events["job1"] = ev
    svc.stop()
    assert svc._stopped is True
    assert ev.is_set()


def test_download_service_set_max_workers_clamps(tmp_path: Path) -> None:
    svc = DownloadService(_settings(tmp_path))
    svc.set_max_workers(999)
    assert svc._max_workers == 32
    svc.set_max_workers(0)
    assert svc._max_workers == 1


def test_convert_service_run_convert_success(tmp_path: Path) -> None:
    svc = ConvertService(_settings(tmp_path), no_embed=False)
    done: list[str] = []
    svc.item_done.connect(done.append)
    raw_path = tmp_path / "raw" / "x.mkv"
    out_path = tmp_path / "out" / "x.mp4"

    with patch("mhi2_video_finder.ui.workers.transcode") as m:
        svc._run_convert("job1", raw_path, out_path, {"id": "abc"}, False)

    assert done == ["job1"]
    m.assert_called_once()
    _, kwargs = m.call_args
    assert kwargs["ytdlp_info"] == {"id": "abc"}


def test_convert_service_run_convert_no_embed_passes_none_ytdlp_info(tmp_path: Path) -> None:
    svc = ConvertService(_settings(tmp_path), no_embed=True)
    raw_path = tmp_path / "raw" / "x.mkv"
    out_path = tmp_path / "out" / "x.mp4"

    with patch("mhi2_video_finder.ui.workers.transcode") as m:
        svc._run_convert("job1", raw_path, out_path, {"id": "abc"}, True)

    _, kwargs = m.call_args
    assert kwargs["ytdlp_info"] is None


def test_convert_service_run_convert_cancelled(tmp_path: Path) -> None:
    svc = ConvertService(_settings(tmp_path), no_embed=False)
    cancelled: list[str] = []
    svc.convert_cancelled.connect(cancelled.append)
    raw_path = tmp_path / "raw" / "x.mkv"
    out_path = tmp_path / "out" / "x.mp4"

    with patch(
        "mhi2_video_finder.ui.workers.transcode",
        side_effect=OperationCancelled("stopped"),
    ):
        svc._run_convert("job1", raw_path, out_path, None, False)

    assert cancelled == ["job1"]


def test_convert_service_run_convert_failure(tmp_path: Path) -> None:
    svc = ConvertService(_settings(tmp_path), no_embed=False)
    failed: list[tuple[str, str]] = []
    svc.item_failed.connect(lambda jid, err: failed.append((jid, err)))
    raw_path = tmp_path / "raw" / "x.mkv"
    out_path = tmp_path / "out" / "x.mp4"

    with patch(
        "mhi2_video_finder.ui.workers.transcode",
        side_effect=RuntimeError("boom"),
    ):
        svc._run_convert("job1", raw_path, out_path, None, False)

    assert failed == [("job1", "boom")]


def test_convert_service_stop_sets_all_abort_events(tmp_path: Path) -> None:
    import threading

    svc = ConvertService(_settings(tmp_path), no_embed=False)
    ev = threading.Event()
    svc._abort_events["job1"] = ev
    svc.stop()
    assert svc._stopped is True
    assert ev.is_set()
