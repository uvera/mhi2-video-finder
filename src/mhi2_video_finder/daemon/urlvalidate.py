"""Restrict job URLs to YouTube."""

from __future__ import annotations

from urllib.parse import urlparse

from mhi2_video_finder.download import normalize_watch_url


def validate_youtube_url(url_or_id: str) -> str:
    s = (url_or_id or "").strip()
    if not s:
        raise ValueError("empty url")
    normalized = normalize_watch_url(s)
    p = urlparse(normalized)
    if p.scheme not in ("http", "https"):
        raise ValueError("only http(s) URLs allowed")
    host = (p.netloc or "").lower()
    if host in ("youtu.be", "www.youtu.be"):
        return normalized
    if host in ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"):
        return normalized
    if host.endswith(".youtube.com"):
        return normalized
    raise ValueError(f"URL host not allowed: {host!r}")
