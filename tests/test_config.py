"""Tests for config / ffmpeg argv construction."""

from __future__ import annotations

from pathlib import Path

import pytest

from mhi2_video_finder.config import (
    Settings,
    build_ffmpeg_command,
    build_ffmpeg_video_filter,
    build_ffmpeg_video_filter_vaapi,
    load_settings,
    save_settings,
)


def test_build_ffmpeg_video_filter_scale_and_fps() -> None:
    s = Settings(max_width=720, max_height=576, fps="25")
    vf = build_ffmpeg_video_filter(s)
    assert "720" in vf and "576" in vf and "fps=25" in vf and "yuv420p" in vf and "flags=bicubic" in vf


def test_build_ffmpeg_command_order_and_bitrates() -> None:
    s = Settings(
        video_bitrate_k=2000,
        video_bitrate_peak_k=2000,
        audio_bitrate_k=160,
        h264_profile="baseline",
        preset="veryfast",
    )
    cmd = build_ffmpeg_command(Path("/in.mkv"), Path("/out.mp4"), s)
    assert cmd[0] == "ffmpeg"
    assert "-i" in cmd
    idx = cmd.index("-i")
    assert cmd[idx + 1] == "/in.mkv"
    assert "-b:v" in cmd
    assert "2000000" in cmd
    assert "-maxrate" in cmd
    maxr_i = cmd.index("-maxrate") + 1
    assert cmd[maxr_i] == "2000000"
    assert "-b:a" in cmd
    assert "160000" in cmd
    assert cmd[-1] == "/out.mp4"
    assert "baseline" in cmd
    assert "libx264" in cmd


def test_build_ffmpeg_video_filter_vaapi_uploads_nv12() -> None:
    s = Settings(max_width=720, max_height=576, fps="25")
    vf = build_ffmpeg_video_filter_vaapi(s)
    assert "fps=25" in vf and "format=nv12,hwupload" in vf and "yuv420p" not in vf


def test_build_ffmpeg_command_vaapi() -> None:
    s = Settings(
        video_encoder="h264_vaapi",
        vaapi_device="/dev/dri/renderD128",
        h264_profile="baseline",
        h264_level="3.1",
        video_bitrate_k=1800,
        video_bitrate_peak_k=2000,
        audio_bitrate_k=160,
    )
    cmd = build_ffmpeg_command(Path("/in.mkv"), Path("/out.mp4"), s)
    assert cmd[0:2] == ["ffmpeg", "-y"]
    i_dev = cmd.index("-vaapi_device")
    assert cmd[i_dev + 1] == "/dev/dri/renderD128"
    i_in = cmd.index("-i")
    assert i_dev < i_in
    assert cmd[i_in + 1] == "/in.mkv"
    assert "h264_vaapi" in cmd
    assert "-rc_mode" in cmd
    assert cmd[cmd.index("-rc_mode") + 1] == "2"
    assert "-profile:v" in cmd
    p = cmd.index("-profile:v") + 1
    assert cmd[p] == "578"
    assert "-level:v" in cmd
    assert cmd[cmd.index("-level:v") + 1] == "31"
    assert "format=nv12,hwupload" in cmd[cmd.index("-vf") + 1]
    assert "libx264" not in cmd


def test_build_ffmpeg_command_vaapi_explicit_profile() -> None:
    s = Settings(video_encoder="vaapi", vaapi_profile=77, h264_profile="baseline")
    cmd = build_ffmpeg_command(Path("/in.mkv"), Path("/out.mp4"), s)
    assert cmd[cmd.index("-profile:v") + 1] == "77"


def test_build_ffmpeg_command_vaapi_rc_mode_omitted_when_cbr_disabled() -> None:
    s = Settings(video_encoder="h264_vaapi", vaapi_device="/dev/dri/renderD128", vaapi_cbr=False)
    cmd = build_ffmpeg_command(Path("/in.mkv"), Path("/out.mp4"), s)
    assert "-rc_mode" not in cmd


def test_build_ffmpeg_command_nvenc() -> None:
    s = Settings(
        video_encoder="h264_nvenc",
        nvenc_preset="p1",
        video_bitrate_k=3000,
        video_bitrate_peak_k=4000,
        audio_bitrate_k=320,
    )
    cmd = build_ffmpeg_command(Path("/in.mkv"), Path("/out.mp4"), s)
    assert "h264_nvenc" in cmd
    assert "p1" in cmd
    assert "libx264" not in cmd
    assert "3000000" in cmd


def test_video_bitrate_peak_caps_maxrate() -> None:
    s = Settings(video_bitrate_k=3000, video_bitrate_peak_k=2000)
    cmd = build_ffmpeg_command(Path("/in.mkv"), Path("/out.mp4"), s)
    i = cmd.index("-maxrate") + 1
    assert cmd[i] == "2000000"
    j = cmd.index("-b:v") + 1
    assert cmd[j] == "2000000"


def test_default_settings_match_audi_usb_limits() -> None:
    s = Settings()
    assert s.max_width == 720 and s.max_height == 576
    assert s.fps == "25"
    assert s.video_bitrate_peak_k == 2000
    assert s.video_bitrate_k <= s.video_bitrate_peak_k


def test_build_ffmpeg_command_threads_and_tune() -> None:
    s = Settings(
        ffmpeg_threads=8,
        h264_tune="zerolatency",
        video_bitrate_peak_k=0,
        video_bitrate_k=1000,
    )
    cmd = build_ffmpeg_command(Path("/in.mkv"), Path("/out.mp4"), s)
    assert cmd[0:4] == ["ffmpeg", "-y", "-threads", "8"]
    assert "-tune" in cmd
    i = cmd.index("-tune")
    assert cmd[i + 1] == "zerolatency"


def test_build_ffmpeg_command_unknown_encoder() -> None:
    s = Settings(video_encoder="bogus")
    with pytest.raises(ValueError, match="Unknown video_encoder"):
        build_ffmpeg_command(Path("/in.mkv"), Path("/out.mp4"), s)


def test_load_settings_env_override_int(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("MHI2_VIDEO_FINDER_FPS", raising=False)
    monkeypatch.setenv("MHI2_VIDEO_FINDER_FPS", "30")
    s = load_settings(tmp_path / "nope.toml")
    assert s.fps == "30"


def test_load_settings_youtube_key_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("YOUTUBE_API_KEY", "abc")
    s = load_settings(tmp_path / "nope.toml")
    assert s.youtube_api_key == "abc"


def test_load_settings_groq_key_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    s = load_settings(tmp_path / "nope.toml")
    assert s.groq_api_key == "gsk_test"


def test_load_settings_from_toml(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    out = tmp_path / "outdir"
    raw = tmp_path / "rawdir"
    cfg.write_text(
        f"""
max_width = 640
fps = 24
output_dir = "{out.as_posix()}"
raw_cache_dir = "{raw.as_posix()}"
""",
        encoding="utf-8",
    )
    s = load_settings(cfg)
    assert s.max_width == 640
    assert s.fps == "24"
    assert s.output_dir == out.resolve()
    assert s.raw_cache_dir == raw.resolve()


def test_settings_parallel_defaults() -> None:
    s = Settings()
    assert s.max_parallel_downloads == 4
    assert s.max_parallel_converts == 4


def test_save_settings_merges_unknown_keys(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text("search_limit = 3\nfoo = 99\n", encoding="utf-8")
    s = load_settings(cfg)
    s.max_parallel_downloads = 2
    s.ffmpeg_nice = 10
    save_settings(s, cfg)
    text = cfg.read_text()
    assert "foo = 99" in text
    assert "max_parallel_downloads = 2" in text
    assert "ffmpeg_nice = 10" in text
    s2 = load_settings(cfg)
    assert s2.max_parallel_downloads == 2
    assert s2.ffmpeg_nice == 10


def test_load_settings_mp4_compat_mode_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MHI2_VIDEO_FINDER_LIBRARY_BULK_INFER_MP4_COMPAT_MODE", "true")
    s = load_settings(tmp_path / "nope.toml")
    assert s.library_bulk_infer_mp4_compat_mode is True
