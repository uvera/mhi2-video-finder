"""Load settings from optional TOML config and MHI2_VIDEO_FINDER_* env vars."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import tomllib


def _expand(p: str | Path) -> Path:
    return Path(os.path.expanduser(str(p))).resolve()


def _config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    if base:
        return _expand(Path(base) / "mhi2-video-finder" / "config.toml")
    return _expand("~/.config/mhi2-video-finder/config.toml")


def default_config_path() -> Path:
    """Resolved path to the default ``config.toml`` (XDG or ``~/.config/...``)."""
    return _config_path()


def _env_key(name: str) -> str:
    return f"MHI2_VIDEO_FINDER_{name.upper()}"


def _fps_to_str(val: Any) -> str:
    if isinstance(val, bool):
        raise TypeError("fps must be number or string")
    if isinstance(val, int | float):
        return str(val)
    return str(val).strip()


def _coerce_field(name: str, raw: str) -> Any:
    if name == "fps":
        return raw.strip()
    if name in (
        "embed_metadata",
        "embed_album_art",
        "vaapi_cbr",
        "remote_auto_download",
        "remote_auto_download_daemon_imports",
        "groq_enabled",
        "library_skip_bulk_infer_if_tagged",
        "library_bulk_infer_mp4_compat_mode",
    ):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if name in (
        "max_width",
        "max_height",
        "video_bitrate_k",
        "video_bitrate_peak_k",
        "audio_bitrate_k",
        "search_limit",
        "audio_sample_rate",
        "ffmpeg_threads",
        "ffmpeg_nice",
        "ffmpeg_cpu_limit_percent",
        "max_parallel_downloads",
        "max_parallel_converts",
        "vaapi_profile",
        "job_retention_days",
    ):
        return int(raw)
    if name == "groq_temperature":
        return float(raw.strip())
    if name == "library_last_folder":
        r = raw.strip()
        return None if not r else _expand(r)
    if name in ("raw_cache_dir", "output_dir", "remote_download_dir"):
        return _expand(raw)
    return raw


@dataclass
class Settings:
    # Audi USB "music interface" video: max 720x576, 25 fps (see README).
    max_width: int = 720
    max_height: int = 576
    # ffmpeg fps filter; Audi allows at most 25 fps.
    fps: str = "25"
    # Average video bitrate (kbit/s); default leaves headroom under peak cap.
    video_bitrate_k: int = 1800
    # Hard cap for -maxrate (kbit/s). Audi: 2000 max; raise for home/other players only.
    video_bitrate_peak_k: int = 2000
    audio_bitrate_k: int = 320
    # baseline + level 3.1: many car USB players reject main/high H.264 or unspecified level.
    h264_profile: str = "baseline"
    # Set to "" to omit (not recommended for MMI/MHI2). Examples: "3.0", "3.1", "4.0".
    h264_level: str = "3.1"
    # libx264: slower presets = better quality at the same target bitrate (recommended for USB); veryfast/ultrafast = faster encodes.
    preset: str = "medium"
    # libx264 only. Try "zerolatency" for a small speed bump (slightly worse compression). "" = omit.
    h264_tune: str = ""
    # If > 0, ffmpeg global -threads (affects decode/encode). 0 = ffmpeg default (usually fine).
    ffmpeg_threads: int = 0
    # Unix only: add this much niceness (0–19) so ffmpeg yields CPU to other apps. 0 = unchanged.
    ffmpeg_nice: int = 0
    # If 1–100 and ``cpulimit`` is on PATH, cap average CPU (per process tree). 0 = off. Linux: pacman -S cpulimit
    ffmpeg_cpu_limit_percent: int = 0
    # GUI / CLI: concurrent yt-dlp downloads and ffmpeg encodes (each ≥ 1).
    max_parallel_downloads: int = 4
    max_parallel_converts: int = 4
    # Video encoder: libx264 (CPU), h264_nvenc/nvenc (NVIDIA), h264_vaapi/vaapi (Intel/AMD via VAAPI).
    video_encoder: str = "libx264"
    # NVENC preset: p1 (fastest) … p7 (slowest) on newer ffmpeg; try "llhp" or "fast" if p1 is rejected.
    nvenc_preset: str = "p1"
    # DRM render node for VAAPI (AMD/Intel). Must exist and grant access (e.g. video/render group).
    vaapi_device: str = "/dev/dri/renderD128"
    # h264_vaapi -profile:v (ffmpeg enum). 0 = derive from h264_profile (baseline → 578 constrained_baseline).
    vaapi_profile: int = 0
    # VAAPI: CBR (ffmpeg rc_mode 2) steadies bitrate vs auto/VBR — often fewer glitches on car USB decoders.
    vaapi_cbr: bool = True
    # Downscale algorithm in vf scale; bicubic/lanczos look sharper than fast_bilinear at 720p-class sizes.
    scale_flags: str = "bicubic"
    # Resample audio before AAC; 48000 is typical for video. Try 44100 if the car still refuses to play.
    audio_sample_rate: int = 48000
    raw_cache_dir: Path = field(default_factory=lambda: _expand("~/.cache/mhi2-video-finder/raw"))
    output_dir: Path = field(default_factory=lambda: _expand("~/Videos/mhi2-video-finder"))
    search_limit: int = 15
    match_filter: str | None = None
    youtube_api_key: str | None = None
    # Embed iTunes-style tags (title, artist, album, …) from YouTube / yt-dlp metadata.
    embed_metadata: bool = True
    # Add highest-resolution thumbnail as MJPEG attached picture (album art).
    embed_album_art: bool = True
    # GUI: run downloads/transcodes locally or on a remote daemon (see docs/daemon-api.yaml).
    processing_backend: str = "local"
    remote_base_url: str = ""
    remote_bearer_token: str = ""
    remote_download_dir: Path = field(default_factory=lambda: _expand("~/Videos/mhi2-video-finder-remote"))
    remote_auto_download: bool = False
    remote_auto_download_daemon_imports: bool = False
    # Daemon: delete a finished job's output file (and its record) this many days after
    # completion if nobody has fetched it; a later request just re-downloads. 0 disables.
    job_retention_days: int = 3
    # Local Library tab: last browsed folder (optional).
    library_last_folder: Path | None = None
    # Guess selected: skip files that already have non-empty artist + title tags (ffprobe).
    library_skip_bulk_infer_if_tagged: bool = False
    # Guess selected: title-only AI mode for MP4 compatibility (leave artist empty).
    library_bulk_infer_mp4_compat_mode: bool = False
    # Groq (OpenAI-compatible) for metadata inference.
    groq_enabled: bool = False
    groq_api_key: str | None = None
    groq_model: str = "llama-3.1-8b-instant"
    groq_base_url: str = "https://api.groq.com/openai/v1"
    groq_temperature: float = 0.2

    def merged_output_dir(self) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        return self.output_dir

    def merged_raw_cache_dir(self) -> Path:
        self.raw_cache_dir.mkdir(parents=True, exist_ok=True)
        return self.raw_cache_dir

    def merged_remote_download_dir(self) -> Path:
        self.remote_download_dir.mkdir(parents=True, exist_ok=True)
        return self.remote_download_dir


def _load_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


def load_settings(path: Path | None = None) -> Settings:
    cfg_path = path or _config_path()
    data = _load_toml(cfg_path)
    s = Settings()
    key_map = {f.name: f.name for f in fields(Settings)}
    # TOML keys use same names as dataclass fields
    for name in key_map:
        if name not in data:
            continue
        val = data[name]
        if name == "library_last_folder":
            if val is None or (isinstance(val, str) and not str(val).strip()):
                setattr(s, name, None)
                continue
            if isinstance(val, str):
                val = _expand(val)
        elif name in ("raw_cache_dir", "output_dir", "remote_download_dir") and isinstance(val, str):
            val = _expand(val)
        if name == "fps":
            val = _fps_to_str(val)
        if name in (
            "embed_metadata",
            "embed_album_art",
            "vaapi_cbr",
            "remote_auto_download",
            "remote_auto_download_daemon_imports",
            "groq_enabled",
            "library_skip_bulk_infer_if_tagged",
            "library_bulk_infer_mp4_compat_mode",
        ) and isinstance(val, str):
            val = val.strip().lower() in ("1", "true", "yes", "on")
        if name == "groq_temperature" and isinstance(val, int | float):
            val = float(val)
        setattr(s, name, val)

    for f in fields(Settings):
        ev = os.environ.get(_env_key(f.name))
        if ev is None:
            continue
        setattr(s, f.name, _coerce_field(f.name, ev))

    if s.youtube_api_key is None:
        s.youtube_api_key = os.environ.get("YOUTUBE_API_KEY")

    if s.groq_api_key is None:
        s.groq_api_key = os.environ.get("GROQ_API_KEY")

    s.fps = _fps_to_str(s.fps)

    return s


def _toml_escape_string(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _format_toml_scalar(val: Any) -> str:
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, int):
        return str(val)
    if isinstance(val, float):
        return str(val)
    if isinstance(val, str):
        return f'"{_toml_escape_string(val)}"'
    raise TypeError(f"unsupported TOML type: {type(val)!r}")


def _write_merged_config(path: Path, data: dict[str, Any]) -> None:
    lines = ["# mhi2-video-finder config", ""]
    for key in sorted(data.keys()):
        val = data[key]
        if val is None:
            continue
        lines.append(f"{key} = {_format_toml_scalar(val)}")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def save_settings(settings: Settings, path: Path | None = None) -> Path:
    """Merge ``settings`` into ``path`` (default: XDG config) and write TOML.

    Keys from :class:`Settings` are updated; other keys already in the file are kept.
    """
    cfg_path = path or _config_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    merged: dict[str, Any] = dict(_load_toml(cfg_path))
    for f in fields(Settings):
        val = getattr(settings, f.name)
        if val is None:
            merged.pop(f.name, None)
            continue
        if isinstance(val, Path):
            merged[f.name] = str(val)
        else:
            merged[f.name] = val
    _write_merged_config(cfg_path, merged)
    return cfg_path


def _video_bitrate_bps(settings: Settings) -> tuple[int, int, int]:
    """Return (target_b:v in bps, maxrate bps, bufsize bps). Respects video_bitrate_peak_k."""
    peak_k = settings.video_bitrate_peak_k
    avg = settings.video_bitrate_k * 1000
    if peak_k and peak_k > 0:
        peak = peak_k * 1000
        vb = min(avg, peak)
        maxr = min(int(settings.video_bitrate_k * 1000 * 11 // 10), peak)
        if maxr < vb:
            maxr = vb
        bufsize = peak * 2
        return vb, maxr, bufsize
    vb = avg
    maxr = int(vb * 11 // 10)
    bufsize = vb * 2
    return vb, maxr, bufsize


def build_ffmpeg_video_filter(settings: Settings) -> str:
    """Scale to fit within max_width x max_height, square pixels, even size; CFR via fps filter."""
    flags = settings.scale_flags.strip() or "bicubic"
    return (
        f"scale=w=min({settings.max_width}\\,iw):h=min({settings.max_height}\\,ih):"
        "force_original_aspect_ratio=decrease:"
        f"flags={flags},"
        "scale=trunc(iw/2)*2:trunc(ih/2)*2,"
        f"fps={settings.fps},"
        "format=yuv420p"
    )


# ffmpeg h264_vaapi -profile:v names → integer (see `ffmpeg -h encoder=h264_vaapi`).
_VAAPI_H264_PROFILE_BY_NAME: dict[str, int] = {
    "baseline": 578,
    "constrained_baseline": 578,
    "main": 77,
    "high": 100,
    "high10": 110,
}


def build_ffmpeg_video_filter_vaapi(settings: Settings) -> str:
    """Same geometry/fps as software path, then upload NV12 for h264_vaapi."""
    flags = settings.scale_flags.strip() or "bicubic"
    return (
        f"scale=w=min({settings.max_width}\\,iw):h=min({settings.max_height}\\,ih):"
        "force_original_aspect_ratio=decrease:"
        f"flags={flags},"
        "scale=trunc(iw/2)*2:trunc(ih/2)*2,"
        f"fps={settings.fps},"
        "format=nv12,hwupload"
    )


def _vaapi_h264_profile_id(settings: Settings) -> int | None:
    if settings.vaapi_profile and settings.vaapi_profile > 0:
        return settings.vaapi_profile
    name = (settings.h264_profile or "").strip().lower()
    return _VAAPI_H264_PROFILE_BY_NAME.get(name)


def _vaapi_h264_level_id(level: str) -> int | None:
    s = level.strip()
    if not s:
        return None
    compact = s.replace(".", "")
    if not compact.isdigit():
        return None
    return int(compact)


def build_ffmpeg_command(
    input_path: Path,
    output_path: Path,
    settings: Settings,
    *,
    metadata_args: list[str] | None = None,
) -> list[str]:
    enc = (settings.video_encoder or "libx264").strip().lower()
    vf = (
        build_ffmpeg_video_filter_vaapi(settings)
        if enc in ("h264_vaapi", "vaapi")
        else build_ffmpeg_video_filter(settings)
    )
    vb, maxr, bufsize = _video_bitrate_bps(settings)
    ab = settings.audio_bitrate_k * 1000

    cmd: list[str] = ["ffmpeg", "-y"]
    if settings.ffmpeg_threads and settings.ffmpeg_threads > 0:
        cmd.extend(["-threads", str(settings.ffmpeg_threads)])
    if enc in ("h264_vaapi", "vaapi"):
        dev = (settings.vaapi_device or "").strip()
        if not dev:
            raise ValueError("vaapi_device is empty; set a DRM render node (e.g. /dev/dri/renderD128).")
        cmd.extend(["-vaapi_device", dev])
    cmd.extend(
        [
            "-i",
            str(input_path),
            "-vf",
            vf,
        ]
    )

    if enc in ("h264_nvenc", "nvenc"):
        cmd.extend(
            [
                "-c:v",
                "h264_nvenc",
                "-preset",
                settings.nvenc_preset.strip() or "p1",
            ]
        )
        prof = (settings.h264_profile or "").strip()
        if prof:
            cmd.extend(["-profile:v", prof])
        lvl = (settings.h264_level or "").strip()
        if lvl:
            cmd.extend(["-level:v", lvl])
        cmd.extend(
            [
                "-b:v",
                str(vb),
                "-maxrate",
                str(maxr),
                "-bufsize",
                str(bufsize),
                "-pix_fmt",
                "yuv420p",
            ]
        )
    elif enc in ("h264_vaapi", "vaapi"):
        cmd.extend(["-c:v", "h264_vaapi"])
        prof = _vaapi_h264_profile_id(settings)
        if prof is not None:
            cmd.extend(["-profile:v", str(prof)])
        lvl = _vaapi_h264_level_id(settings.h264_level)
        if lvl is not None:
            cmd.extend(["-level:v", str(lvl)])
        cmd.extend(
            [
                "-b:v",
                str(vb),
                "-maxrate",
                str(maxr),
                "-bufsize",
                str(bufsize),
            ]
        )
        if settings.vaapi_cbr:
            cmd.extend(["-rc_mode", "2"])
        cmd.extend(
            [
                "-pix_fmt",
                "vaapi",
            ]
        )
    elif enc == "libx264":
        cmd.extend(
            [
                "-c:v",
                "libx264",
                "-profile:v",
                settings.h264_profile,
            ]
        )
        lvl = (settings.h264_level or "").strip()
        if lvl:
            cmd.extend(["-level:v", lvl])
        tune = (settings.h264_tune or "").strip()
        if tune:
            cmd.extend(["-tune", tune])
        cmd.extend(
            [
                "-preset",
                settings.preset,
                "-b:v",
                str(vb),
                "-maxrate",
                str(maxr),
                "-bufsize",
                str(bufsize),
                "-pix_fmt",
                "yuv420p",
            ]
        )
    else:
        raise ValueError(
            f"Unknown video_encoder {enc!r}. Use libx264, h264_nvenc, nvenc, h264_vaapi, or vaapi."
        )

    if settings.audio_sample_rate and settings.audio_sample_rate > 0:
        cmd.extend(["-ar", str(settings.audio_sample_rate)])

    cmd.extend(
        [
            "-c:a",
            "aac",
            "-profile:a",
            "aac_low",
            "-b:a",
            str(ab),
            "-ac",
            "2",
        ]
    )
    if metadata_args:
        cmd.extend(metadata_args)
    cmd.extend(["-movflags", "+faststart", str(output_path)])
    return cmd
