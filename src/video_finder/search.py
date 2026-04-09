"""YouTube search and channel listing via yt-dlp."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import yt_dlp


@dataclass
class VideoCandidate:
    video_id: str
    title: str
    duration: int | None
    channel: str
    url: str


def _normalize_channel_videos_url(channel: str) -> str:
    s = channel.strip()
    if s.startswith("http"):
        parsed = urlparse(s)
        path = parsed.path.rstrip("/")
        if path.endswith("/videos"):
            return s.split("?")[0]
        base = f"{parsed.scheme}://{parsed.netloc}{path}"
        return base + "/videos"
    handle = s.removeprefix("@")
    return f"https://www.youtube.com/@{handle}/videos"


def _entry_to_candidate(entry: dict[str, Any]) -> VideoCandidate | None:
    eid = str(entry.get("id") or "")
    vid = ""
    if len(eid) == 11:
        vid = eid
    url = str(entry.get("url") or "")
    if not vid and "watch?v=" in url:
        vid = url.split("v=", 1)[1].split("&")[0][:11]
    if len(vid) != 11:
        return None
    title = entry.get("title") or "(no title)"
    channel = entry.get("channel") or entry.get("uploader") or ""
    duration = entry.get("duration")
    if duration is not None:
        duration = int(duration)
    return VideoCandidate(
        video_id=vid,
        title=title,
        duration=duration,
        channel=channel,
        url=f"https://www.youtube.com/watch?v={vid}",
    )


def _ydl_opts_base(
    *,
    playlistend: int | None = None,
    match_filter: str | None = None,
) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
        "ignoreerrors": True,
    }
    if playlistend is not None:
        opts["playlistend"] = playlistend
    if match_filter:
        opts["match_filter"] = match_filter
    return opts


def _yt_dlp_search_url(query: str, limit: int | None) -> str:
    """ytsearchall: pages until YouTube stops returning continuations; limit N caps to N results."""
    if limit is None:
        return f"ytsearchall:{query}"
    return f"ytsearch{max(1, limit)}:{query}"


def video_from_url(
    url: str,
    *,
    match_filter: str | None = None,
) -> VideoCandidate:
    """Resolve a single watch URL (youtube.com, youtu.be, Shorts, etc.) via yt-dlp."""
    u = url.strip()
    if not u:
        raise ValueError("URL is empty")
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
    }
    if match_filter:
        opts["match_filter"] = match_filter
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(u, download=False)
    if not info:
        raise ValueError("Could not resolve URL")
    entries = info.get("entries")
    if entries:
        valid = [e for e in entries if e]
        if len(valid) > 1:
            raise ValueError(
                "That URL lists multiple videos. Use Source → Playlist, or paste a single video link."
            )
        if len(valid) == 1:
            c = _entry_to_candidate(valid[0] if isinstance(valid[0], dict) else {})
            if c:
                return c
        raise ValueError("No video found at URL")
    c = _entry_to_candidate(info if isinstance(info, dict) else {})
    if not c:
        raise ValueError("Not a recognizable single video URL")
    return c


def search_youtube(
    query: str,
    *,
    limit: int | None = 15,
    match_filter: str | None = None,
) -> list[VideoCandidate]:
    url = _yt_dlp_search_url(query, limit)
    opts = _ydl_opts_base(match_filter=match_filter)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    entries = (info or {}).get("entries") or []
    out: list[VideoCandidate] = []
    for e in entries:
        if not e:
            continue
        c = _entry_to_candidate(e)
        if c:
            out.append(c)
    return out


def list_playlist_videos(
    playlist_url: str,
    *,
    limit: int | None = 20,
    match_filter: str | None = None,
) -> list[VideoCandidate]:
    """List videos from a YouTube playlist URL (or any URL yt-dlp treats as a playlist)."""
    url = playlist_url.strip()
    opts = _ydl_opts_base(
        playlistend=limit if limit is not None else None,
        match_filter=match_filter,
    )
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    entries = (info or {}).get("entries") or []
    out: list[VideoCandidate] = []
    for e in entries:
        if not e:
            continue
        c = _entry_to_candidate(e)
        if c:
            out.append(c)
    return out


def list_channel_videos(
    channel: str,
    *,
    limit: int | None = 20,
    match_filter: str | None = None,
) -> list[VideoCandidate]:
    url = _normalize_channel_videos_url(channel)
    opts = _ydl_opts_base(
        playlistend=limit if limit is not None else None,
        match_filter=match_filter,
    )
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    entries = (info or {}).get("entries") or []
    out: list[VideoCandidate] = []
    for e in entries:
        if not e:
            continue
        c = _entry_to_candidate(e)
        if c:
            out.append(c)
    return out


QUERY_TEMPLATES: dict[str, str] = {
    "music_video": "{artist} {title} official music video",
    "vevo": "{artist} {title} vevo",
    "official": "{artist} {title} official video",
    "plain": "{artist} {title}",
}


def format_mv_query(
    *,
    artist: str,
    title: str | None = None,
    template: str = "music_video",
) -> str:
    t = QUERY_TEMPLATES.get(template, QUERY_TEMPLATES["music_video"])
    parts = {"artist": artist.strip(), "title": (title or "").strip()}
    if not parts["title"]:
        return " ".join(t.format(artist=parts["artist"], title="").split())
    return t.format(artist=parts["artist"], title=parts["title"]).strip()
