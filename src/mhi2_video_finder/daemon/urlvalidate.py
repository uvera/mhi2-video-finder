"""Restrict job URLs to a single YouTube video (no playlists/channels)."""

from __future__ import annotations

from urllib.parse import urlparse

from mhi2_video_finder.download import normalize_watch_url
from mhi2_video_finder.paste_urls import parse_pasted_video_urls


def validate_youtube_url(url_or_id: str) -> str:
    s = (url_or_id or "").strip()
    if not s:
        raise ValueError("empty url")
    normalized = normalize_watch_url(s)
    p = urlparse(normalized)
    if p.scheme not in ("http", "https"):
        raise ValueError("only http(s) URLs allowed")
    host = (p.netloc or "").lower()
    if not (
        host in ("youtu.be", "www.youtu.be")
        or host in ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com")
        or host.endswith(".youtube.com")
    ):
        raise ValueError(f"URL host not allowed: {host!r}")
    # A playlist/channel URL (or a watch URL missing ``v=``) has no single-video
    # form and resolves to nothing here; only single-video URLs canonicalize.
    # This also strips playlist context (``list=``) off an otherwise-valid
    # watch URL, so one job can never fan out into a multi-video download.
    canonical = parse_pasted_video_urls(normalized)
    if not canonical:
        raise ValueError("URL must link to a single YouTube video, not a playlist or channel")
    return canonical[0]
