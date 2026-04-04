"""Derive media tags from yt-dlp info and attach album art to MP4 via ffmpeg."""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import URLError, HTTPError
from urllib.request import Request, urlopen

# ffmpeg / iTunes-style keys (MP4)
_FFMPEG_META_KEYS = (
    "title",
    "artist",
    "album",
    "album_artist",
    "date",
    "genre",
    "comment",
)


@dataclass(frozen=True)
class MediaTags:
    title: str = ""
    artist: str = ""
    album: str = ""
    album_artist: str = ""
    date: str = ""
    genre: str = ""
    comment: str = ""

    def ffmpeg_args(self) -> list[str]:
        out: list[str] = []
        for key in _FFMPEG_META_KEYS:
            val = getattr(self, key, "") or ""
            val = _sanitize_meta_value(val)
            if val:
                out.extend(["-metadata", f"{key}={val}"])
        return out

    def any_set(self) -> bool:
        return any((getattr(self, k, "") or "").strip() for k in _FFMPEG_META_KEYS)


def _sanitize_meta_value(s: str) -> str:
    s = s.replace("\x00", "").strip()
    if len(s) > 500:
        s = s[:500] + "…"
    return s


def tags_from_ytdlp(info: dict[str, Any]) -> MediaTags:
    """Map yt-dlp extract_info dict to MP4/iTunes-style tags."""
    track = (info.get("track") or "").strip()
    title_raw = (info.get("title") or "").strip()
    title = track or title_raw

    artist = (info.get("artist") or "").strip()
    if not artist:
        artist = (info.get("channel") or info.get("uploader") or "").strip()

    album = (info.get("album") or "").strip()
    album_artist = (info.get("album_artist") or "").strip()
    if not album_artist and artist:
        album_artist = artist

    date = ""
    ry = info.get("release_year")
    if ry is not None:
        date = str(int(ry)) if isinstance(ry, int | float) else str(ry).strip()
    if not date:
        ud = info.get("upload_date")
        if ud:
            uds = str(ud).strip()
            if len(uds) >= 4 and uds[:4].isdigit():
                date = uds[:4]

    g = info.get("genre")
    if isinstance(g, list):
        genre = ", ".join(str(x) for x in g if x)[:500]
    else:
        genre = str(g or "").strip()

    comment = (info.get("description") or "").strip()
    if len(comment) > 2000:
        comment = comment[:2000] + "…"

    return MediaTags(
        title=title,
        artist=artist,
        album=album,
        album_artist=album_artist,
        date=date,
        genre=genre,
        comment=comment,
    )


def best_thumbnail_url(info: dict[str, Any]) -> str | None:
    thumbs = info.get("thumbnails")
    if isinstance(thumbs, list) and thumbs:

        def area(t: dict[str, Any]) -> int:
            w = t.get("width") or 0
            h = t.get("height") or 0
            try:
                return int(w) * int(h)
            except (TypeError, ValueError):
                return 0

        for t in sorted(thumbs, key=area, reverse=True):
            u = t.get("url")
            if u:
                return str(u)
    u = info.get("thumbnail")
    return str(u) if u else None


def download_thumbnail(url: str, dest: Path, timeout: float = 30.0) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = Request(url, headers={"User-Agent": "video-finder/1.0"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            data = resp.read()
    except (URLError, HTTPError, OSError):
        return False
    if not data or len(data) < 256:
        return False
    dest.write_bytes(data)
    return True


def attach_album_art_to_mp4(
    mp4_path: Path,
    image_path: Path,
    output_path: Path,
) -> None:
    """Remux: copy main video/audio from mp4_path, add image as MJPEG attached_pic."""
    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(suffix=".mp4", prefix="vf-art-", dir=str(output_path.parent))
    os.close(fd)
    tmp_out = Path(tmp_name)
    try:
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(mp4_path.resolve()),
            "-i",
            str(image_path.resolve()),
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-map",
            "1:v:0",
            "-c:v:0",
            "copy",
            "-c:a",
            "copy",
            "-c:v:1",
            "mjpeg",
            "-disposition:v:1",
            "attached_pic",
            "-movflags",
            "+faststart",
            str(tmp_out),
        ]
        subprocess.run(cmd, check=True)
        tmp_out.replace(output_path)
    finally:
        tmp_out.unlink(missing_ok=True)
