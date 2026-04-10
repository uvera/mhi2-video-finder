"""Tests for UI progress helpers."""

from mhi2_video_finder.ui.progress_util import format_bytes, ytdlp_progress_percent_and_labels


def test_format_bytes() -> None:
    assert "B" in format_bytes(100)
    assert "KiB" in format_bytes(2048)


def test_ytdlp_progress_downloading() -> None:
    pct, speed, eta = ytdlp_progress_percent_and_labels(
        {
            "status": "downloading",
            "downloaded_bytes": 50,
            "total_bytes": 100,
            "speed": 1024,
            "eta": 65,
        }
    )
    assert 49 <= pct <= 51
    assert "/s" in speed
    assert eta


def test_ytdlp_progress_finished() -> None:
    pct, _, _ = ytdlp_progress_percent_and_labels({"status": "finished"})
    assert pct == 100.0


def test_ytdlp_progress_unknown_total() -> None:
    pct, _, _ = ytdlp_progress_percent_and_labels(
        {"status": "downloading", "downloaded_bytes": 1000}
    )
    assert pct < 0
