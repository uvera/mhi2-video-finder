"""Tests for ffmpeg progress file parsing."""

from pathlib import Path

from video_finder.ffmpeg_progress import (
    insert_ffmpeg_progress_args,
    last_out_time_ms_from_progress_file,
)


def test_insert_ffmpeg_progress_args(tmp_path: Path) -> None:
    prog = tmp_path / "p.txt"
    cmd = ["ffmpeg", "-y", "-i", "in.mkv", "out.mp4"]
    out = insert_ffmpeg_progress_args(cmd, prog)
    assert out[0] == "ffmpeg"
    assert out[1] == "-y"
    assert "-progress" in out
    assert str(prog) in out


def test_last_out_time_ms_from_progress_file(tmp_path: Path) -> None:
    f = tmp_path / "ff.txt"
    f.write_text("foo=1\nout_time_ms=5000\nbar=2\nout_time_ms=9000\n", encoding="utf-8")
    assert last_out_time_ms_from_progress_file(f) == 9000
