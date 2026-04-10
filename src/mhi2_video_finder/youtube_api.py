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
    max_results: int | None = 10,
) -> list[ApiSearchResult]:
    out: list[ApiSearchResult] = []
    page_token: str | None = None
    while True:
        if max_results is not None and len(out) >= max_results:
            break
        per_page = 50
        if max_results is not None:
            per_page = max(1, min(50, max_results - len(out)))
        params: dict[str, str | int] = {
            "part": "snippet",
            "type": "video",
            "videoCategoryId": "10",
            "q": query,
            "maxResults": per_page,
            "key": api_key,
        }
        if channel_id:
            params["channelId"] = channel_id
        if page_token:
            params["pageToken"] = page_token
        url = "https://www.googleapis.com/youtube/v3/search?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            raise RuntimeError(f"YouTube API HTTP {e.code}: {body}") from e

        for item in payload.get("items", []):
            vid = item.get("id", {}).get("videoId")
            sn = item.get("snippet", {})
            title = sn.get("title") or ""
            ch = sn.get("channelTitle") or ""
            if vid:
                out.append(ApiSearchResult(video_id=vid, title=title, channel_title=ch))

        page_token = payload.get("nextPageToken")
        if not page_token:
            break

    return out
