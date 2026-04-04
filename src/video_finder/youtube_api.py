"""Optional YouTube Data API v3 search (Music category)."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass


@dataclass
class ApiSearchResult:
    video_id: str
    title: str
    channel_title: str


def search_music_videos(
    api_key: str,
    query: str,
    *,
    channel_id: str | None = None,
    max_results: int = 10,
) -> list[ApiSearchResult]:
    params: dict[str, str | int] = {
        "part": "snippet",
        "type": "video",
        "videoCategoryId": "10",
        "q": query,
        "maxResults": max(1, min(max_results, 50)),
        "key": api_key,
    }
    if channel_id:
        params["channelId"] = channel_id
    url = "https://www.googleapis.com/youtube/v3/search?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise RuntimeError(f"YouTube API HTTP {e.code}: {body}") from e

    out: list[ApiSearchResult] = []
    for item in payload.get("items", []):
        vid = item.get("id", {}).get("videoId")
        sn = item.get("snippet", {})
        title = sn.get("title") or ""
        ch = sn.get("channelTitle") or ""
        if vid:
            out.append(ApiSearchResult(video_id=vid, title=title, channel_title=ch))
    return out
