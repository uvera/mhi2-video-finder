"""Tests for ffmpeg progress parsing."""

from pathlib import Path
from unittest.mock import patch

from mhi2_video_finder.ffmpeg_progress import (
    insert_ffmpeg_progress_args,
    insert_ffmpeg_progress_path,
    last_out_time_ms_from_progress_file,
    prepare_ffmpeg_subprocess_argv,
    update_ffmpeg_progress_from_stderr_line,
    wrap_ffmpeg_cpulimit,
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
    f.write_text(
        "foo=1\nout_time_ms=5000\nbar=2\nout_time=00:00:09.000000\n",
        encoding="utf-8",
    )
    assert last_out_time_ms_from_progress_file(f) == 9000


def test_last_out_time_ms_from_progress_file_microseconds(tmp_path: Path) -> None:
    f = tmp_path / "ff.txt"
    f.write_text("out_time_ms=19840000\n", encoding="utf-8")
    assert last_out_time_ms_from_progress_file(f) == 19840


def test_insert_ffmpeg_progress_path(tmp_path: Path) -> None:
    prog = tmp_path / "p.txt"
    cmd = ["ffmpeg", "-y", "-i", "in.mkv", "out.mp4"]
    out = insert_ffmpeg_progress_path(cmd, prog)
    assert out[0:4] == ["ffmpeg", "-y", "-progress", str(prog)]


def test_wrap_ffmpeg_cpulimit_noop_without_binary() -> None:
    cmd = ["ffmpeg", "-y", "-i", "a", "b"]
    with patch("mhi2_video_finder.ffmpeg_progress.shutil.which", return_value=None):
        assert wrap_ffmpeg_cpulimit(cmd, 50) == cmd


def test_wrap_ffmpeg_cpulimit_wraps_when_binary_exists() -> None:
    cmd = ["ffmpeg", "-y", "-i", "a", "b"]
    with patch("mhi2_video_finder.ffmpeg_progress.shutil.which", return_value="/bin/cpulimit"):
        out = wrap_ffmpeg_cpulimit(cmd, 50)
        assert out[:5] == ["/bin/cpulimit", "-z", "-l", "50", "--"]
        assert out[5:] == cmd


def test_prepare_ffmpeg_subprocess_argv_cpu_limit() -> None:
    cmd = ["ffmpeg", "-y", "-i", "a", "b"]
    with patch("mhi2_video_finder.ffmpeg_progress.shutil.which") as w:

        def _which(name: str) -> str | None:
            if name == "cpulimit":
                return "/x/cpulimit"
            if name == "stdbuf":
                return None
            return None

        w.side_effect = _which
        argv, pre = prepare_ffmpeg_subprocess_argv(cmd, nice_delta=0, cpu_limit_percent=40)
        assert argv[:5] == ["/x/cpulimit", "-z", "-l", "40", "--"]
        assert argv[5:] == cmd
        assert pre is None


def test_stderr_duration_and_time_like_ffmpeg_vaapi() -> None:
    state: dict[str, int] = {}
    update_ffmpeg_progress_from_stderr_line(
        state,
        "    DURATION        : 00:03:54.067000000",
    )
    assert state["total_ms"] == int((3 * 60 + 54.067) * 1000)
    update_ffmpeg_progress_from_stderr_line(
        state,
        "frame=  887 fps= 71 q=-0.0 size=    8960KiB time=00:00:35.44 bitrate=2071.1kbits/s speed=2.83x",
    )
    assert state["out_ms"] == int((35.44) * 1000)
    pct = min(99.0, 100.0 * state["out_ms"] / state["total_ms"])
    assert 14 < pct < 16
