"""ffprobe JSON helpers for local file context."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any


def ffprobe_json(path: Path, *, timeout: float = 60.0) -> dict[str, Any]:
    """Run ffprobe -print_format json and return parsed root object."""
    path = path.expanduser().resolve()
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_format",
        "-show_streams",
        "-print_format",
        "json",
        str(path),
    ]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(err or f"ffprobe failed with code {proc.returncode}")
    data = json.loads(proc.stdout or "{}")
    if not isinstance(data, dict):
        return {}
    return data


def _tag_str(val: Any) -> str:
    if val is None:
        return ""
    return str(val).strip()


def format_tags_from_probe(data: dict[str, Any]) -> tuple[str, str]:
    """Read artist and title from ffprobe format tags (best-effort)."""
    fmt = data.get("format")
    if not isinstance(fmt, dict):
        return "", ""
    tags = fmt.get("tags")
    if not isinstance(tags, dict):
        return "", ""
    artist = _tag_str(tags.get("artist") or tags.get("ARTIST") or tags.get("album_artist"))
    title = _tag_str(tags.get("title") or tags.get("TITLE"))
    return artist, title


def summarize_probe(data: dict[str, Any], *, max_len: int = 1200) -> str:
    """Short text for LLM context: duration, codecs, resolution, bit rate."""
    lines: list[str] = []
    fmt = data.get("format")
    if isinstance(fmt, dict):
        dur = fmt.get("duration")
        br = fmt.get("bit_rate")
        if dur is not None:
            lines.append(f"duration_seconds: {dur}")
        if br is not None:
            lines.append(f"format_bit_rate: {br}")
    streams = data.get("streams")
    if isinstance(streams, list):
        for i, st in enumerate(streams):
            if not isinstance(st, dict):
                continue
            ctype = st.get("codec_type")
            if ctype not in ("video", "audio"):
                continue
            parts = [f"stream[{i}] type={ctype}"]
            c = st.get("codec_name")
            if c:
                parts.append(f"codec={c}")
            if ctype == "video":
                w, h = st.get("width"), st.get("height")
                if w and h:
                    parts.append(f"{w}x{h}")
            elif ctype == "audio":
                ch = st.get("channels")
                sr = st.get("sample_rate")
                if ch is not None:
                    parts.append(f"ch={ch}")
                if sr is not None:
                    parts.append(f"sample_rate={sr}")
            lines.append(" ".join(parts))
    text = "\n".join(lines)
    if len(text) > max_len:
        return text[: max_len - 1] + "…"
    return text
