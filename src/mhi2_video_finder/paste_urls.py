"""Extract canonical YouTube watch URLs from pasted text (plain lines, chat logs, …)."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, unquote, urlparse

_YT_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")

# YouTube URL-ish spans in messy paste (timestamp / name prefixes, missing scheme, …).
_YOUTUBE_URL_SPAN = re.compile(
    r"(?:https?://)?"
    r"(?:(?:[\w-]+\.)*(?:youtube\.com|youtube-nocookie\.com)/watch\?[^\s#]+|"
    r"(?:[\w-]+\.)*music\.youtube\.com/watch\?[^\s#]+|"
    r"(?:[\w-]+\.)*(?:youtube\.com|youtube-nocookie\.com)/shorts/[A-Za-z0-9_-]{11}[^\s#]*|"
    r"(?:[\w-]+\.)*(?:youtube\.com|youtube-nocookie\.com)/embed/[A-Za-z0-9_-]{11}[^\s#]*|"
    r"(?:www\.)?youtu\.be/[A-Za-z0-9_-]{11}[^\s#]*)",
    re.IGNORECASE,
)


def _normalize_youtube_url_fragment(raw: str) -> str | None:
    s = raw.strip()
    if not s:
        return None
    if not re.match(r"^https?://", s, re.IGNORECASE):
        s = "https://" + s
    p = urlparse(s)
    host = (p.netloc or "").lower()
    if host.endswith("."):
        host = host[:-1]

    path = p.path or ""

    if host == "youtu.be" or host.endswith(".youtu.be"):
        vid = path.strip("/").split("/")[0]
        return f"https://www.youtube.com/watch?v={vid}" if _YT_ID.match(vid) else None

    if not (host == "youtube.com" or host.endswith(".youtube.com") or host.endswith("youtube-nocookie.com")):
        return None

    segs = [x for x in path.split("/") if x]
    if len(segs) >= 2 and segs[0].lower() == "shorts" and _YT_ID.match(segs[1]):
        return f"https://www.youtube.com/watch?v={segs[1]}"
    if len(segs) >= 2 and segs[0].lower() == "embed" and _YT_ID.match(segs[1]):
        return f"https://www.youtube.com/watch?v={segs[1]}"

    q = parse_qs(p.query, keep_blank_values=False)
    for v in q.get("v", []):
        v = unquote(v.strip())
        if _YT_ID.match(v):
            return f"https://www.youtube.com/watch?v={v}"
    return None


def parse_pasted_video_urls(text: str) -> list[str]:
    """Return unique canonical watch URLs in first-seen order.

    Strips chat-style prefixes and query noise (``list``, ``t``, ``pp``, …) so each link
    resolves as a single video.
    """
    seen: set[str] = set()
    out: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        for m in _YOUTUBE_URL_SPAN.finditer(s):
            canon = _normalize_youtube_url_fragment(m.group(0))
            if canon and canon not in seen:
                seen.add(canon)
                out.append(canon)
    return out
