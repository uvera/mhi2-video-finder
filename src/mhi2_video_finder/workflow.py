"""Shared orchestration helpers for CLI and GUI (search, naming, multi-pick)."""

from __future__ import annotations

import sys
from pathlib import Path

from mhi2_video_finder.config import Settings
from mhi2_video_finder.search import VideoCandidate, format_mv_query, search_youtube
from mhi2_video_finder.youtube_api import search_music_videos


def fmt_duration(seconds: int | None) -> str:
    if seconds is None:
        return "?"
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:d}:{s:02d}"


def _is_placeholder_title_for_stem(title: str) -> bool:
    """True when ``title`` is empty or a known UI / yt-dlp missing-title sentinel.

    ``safe_stem("(no title)", vid)`` would otherwise become ``_no title_`` on disk.
    """
    t = (title or "").strip()
    if not t:
        return True
    cf = t.casefold()
    if cf == "(no title)":
        return True
    # Slugged forms if an older build or hand-edited path fed the stem back in.
    if cf in ("_no title_", "_no_title_"):
        return True
    return False


def safe_stem(title: str, fallback: str) -> str:
    base = "" if _is_placeholder_title_for_stem(title) else title
    s = "".join(c if c.isalnum() or c in " -_." else "_" for c in base)[:120]
    s = s.strip() or fallback
    return s


def slug_dir_name(text: str, fallback: str = "session") -> str:
    base = "".join(c if c.isalnum() or c in " -_" else "_" for c in text.strip())[:80].strip()
    base = "_".join(base.split()) or fallback
    return base


def infer_subdir_name(*, artist: str | None, channel: str | None, fallback: str = "download") -> str:
    """Slug a subfolder name from artist (if set) or uploader/channel name."""
    if artist and artist.strip():
        return slug_dir_name(artist.strip(), fallback)
    ch = (channel or "").strip()
    if ch:
        return slug_dir_name(ch, fallback)
    return fallback


def ensure_output_dir(folder: Path) -> Path:
    """Create folder on disk immediately; return absolute path."""
    resolved = folder.expanduser().resolve()
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def safe_join(base: Path, *parts: str) -> Path:
    """Join ``parts`` under ``base``; raise ValueError if the result would escape it.

    Guards against path traversal (``..`` segments) and absolute-path overrides
    (``Path("/a") / "/etc/passwd"`` discards the base entirely) in user-supplied
    subdir/filename-stem values, without requiring the path to exist on disk.
    """
    resolved_base = base.expanduser().resolve()
    candidate = resolved_base
    for part in parts:
        if part:
            candidate = candidate / part
    resolved_candidate = candidate.expanduser().resolve()
    try:
        resolved_candidate.relative_to(resolved_base)
    except ValueError:
        raise ValueError("path escapes base directory") from None
    return resolved_candidate


def ensure_and_print_output_dir(folder: Path, *, err_stream: bool = True) -> Path:
    """Create folder and print absolute path (stderr by default), matching CLI behavior."""
    resolved = ensure_output_dir(folder)
    msg = f"Output folder (ready): {resolved}"
    if err_stream:
        print(msg, file=sys.stderr)
        sys.stderr.flush()
    else:
        print(msg)
    return resolved


def parse_multi_pick(spec: str, n: int) -> list[int]:
    """Parse 1-based selections: '1', '1,3', '2-4', '1, 3-5'. Returns sorted unique 0-based indices."""
    spec = spec.strip().lower()
    if not spec or spec == "0":
        return []
    if spec == "all":
        return list(range(n))
    indices: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            try:
                start, end = int(a.strip()), int(b.strip())
            except ValueError:
                continue
            if start > end:
                start, end = end, start
            for i in range(start, end + 1):
                if 1 <= i <= n:
                    indices.add(i - 1)
        else:
            try:
                i = int(part)
            except ValueError:
                continue
            if 1 <= i <= n:
                indices.add(i - 1)
    return sorted(indices)


def unique_out_path(folder: Path, stem: str, video_id: str) -> Path:
    candidate = folder / f"{stem}.mp4"
    if not candidate.exists():
        return candidate
    return folder / f"{stem}_{video_id}.mp4"


def remote_save_out_path(folder: Path, stem: str) -> Path:
    """Local path for remote Save to PC: always ``stem.mp4``, overwriting if it already exists."""
    return folder / f"{stem}.mp4"


def gather_search_rows(
    s: Settings,
    *,
    n: int | None,
    mf: str | None,
    use_youtube_api: bool,
    query: str,
    artist: str | None,
    title: str | None,
    template: str,
    channel_id: str | None,
) -> tuple[str, list[VideoCandidate]]:
    """Returns (display label for subdir default, rows). Raises ValueError if API key missing."""
    if use_youtube_api:
        key = s.youtube_api_key
        if not key:
            raise ValueError("YOUTUBE_API_KEY or youtube_api_key in config is required for API search")
        q = format_mv_query(artist=artist or query, title=title, template=template) if artist else query
        api_rows = search_music_videos(key, q, channel_id=channel_id, max_results=n)
        rows = [
            VideoCandidate(
                video_id=r.video_id,
                title=r.title,
                duration=None,
                channel=r.channel_title,
                url=f"https://www.youtube.com/watch?v={r.video_id}",
            )
            for r in api_rows
        ]
        return (q, rows)
    if artist:
        q = format_mv_query(artist=artist, title=title, template=template)
        rows = search_youtube(q, limit=n, match_filter=mf)
        return (q, rows)
    rows = search_youtube(query, limit=n, match_filter=mf)
    return (query, rows)
