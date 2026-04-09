"""Download YouTube videos with yt-dlp."""

from __future__ import annotations

import re
from pathlib import Path
from collections.abc import Callable
from typing import Any

import yt_dlp

_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


def normalize_watch_url(url_or_id: str) -> str:
    s = url_or_id.strip()
    if s.startswith("http"):
        return s
    if _VIDEO_ID.match(s):
        return f"https://www.youtube.com/watch?v={s}"
    return s


def download_to_cache(
    url_or_id: str,
    cache_dir: Path,
    *,
    progress_hooks: list[Callable[[dict[str, Any]], None]] | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Download best video+audio merged to MKV; return path and yt-dlp info dict (for tags / thumbnails).

    When ``progress_hooks`` is set, terminal spam is suppressed (quiet mode) and hooks receive yt-dlp
    progress dicts (see yt-dlp ``progress_hooks`` documentation).
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    url = normalize_watch_url(url_or_id)

    outtmpl = str(cache_dir / "%(id)s.%(ext)s")
    use_hooks = bool(progress_hooks)
    opts: dict[str, Any] = {
        "outtmpl": outtmpl,
        "format": "bv*+ba/best",
        "merge_output_format": "mkv",
        "quiet": use_hooks,
        "no_warnings": use_hooks,
        "ignoreerrors": False,
    }
    if progress_hooks:
        opts["progress_hooks"] = list(progress_hooks)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    if not info:
        raise FileNotFoundError(f"extract_info returned no data for {url!r}")

    if info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            raise FileNotFoundError(f"Playlist produced no entries for {url!r}")
        info = entries[-1]

    vid = str(info.get("id") or "video")
    matches = sorted(cache_dir.glob(f"{vid}.*"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in matches:
        if p.suffix.lower() in (".part", ".ytdl", ".temp"):
            continue
        if p.is_file():
            return p, info
    raise FileNotFoundError(f"No downloaded file for id={vid} in {cache_dir}")
