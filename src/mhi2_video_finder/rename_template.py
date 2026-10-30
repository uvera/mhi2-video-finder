"""Filename stems from ``{song}`` / ``{artist}`` templates."""

from __future__ import annotations

import re

from mhi2_video_finder.local_tags import sanitize_filename_stem

_TOKEN = re.compile(r"(\{(?:song|artist)\})")
_PLACEHOLDER = re.compile(r"\{(song|artist)\}")


def _separator_only(text: str) -> bool:
    return bool(text) and re.search(r"[A-Za-z0-9]", text) is None


def render_filename_stem(
    template: str,
    *,
    song: str,
    artist: str,
    fallback: str,
) -> str:
    """Fill ``{song}`` and ``{artist}``. Empty placeholders drop adjacent separators.

    The result is sanitized for the filesystem. When nothing remains, ``fallback``
    is returned (the current filename stem).
    """
    values = {"song": (song or "").strip(), "artist": (artist or "").strip()}
    tokens: list[tuple[str, str | None]] = []
    for part in _TOKEN.split(template or ""):
        if not part:
            continue
        match = _PLACEHOLDER.fullmatch(part)
        if match:
            key = match.group(1)
            tokens.append((values[key], key))
        else:
            tokens.append((part, None))

    omit = [False] * len(tokens)
    for i, (text, kind) in enumerate(tokens):
        if kind is None or text:
            continue
        omit[i] = True
        if i > 0 and tokens[i - 1][1] is None and _separator_only(tokens[i - 1][0]):
            omit[i - 1] = True
        if i + 1 < len(tokens) and tokens[i + 1][1] is None and _separator_only(tokens[i + 1][0]):
            omit[i + 1] = True

    rendered = "".join(text for (text, _), drop in zip(tokens, omit, strict=True) if not drop)
    rendered = re.sub(r"\s+", " ", rendered).strip()
    return sanitize_filename_stem(rendered, fallback)
