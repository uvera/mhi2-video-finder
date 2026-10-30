"""Groq OpenAI-compatible chat completions for song/artist inference."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx

from mhi2_video_finder.config import DEFAULT_GROQ_MODEL, Settings


class GroqInferenceError(Exception):
    """API or parse failure."""


_SYSTEM_PROMPT = (
    "You help identify music video files. Given a filename, optional folder hint, and a "
    "technical media summary, infer the performing artist and the song title.\n"
    'Respond with a single JSON object only, no markdown, with keys "author" and '
    '"song_name". Use an empty string if unknown. Do not include other keys.\n'
    "Use only words that already appear in the filename. You may change capitalization "
    "and punctuation, and you may drop words. Do not add words that are not in the filename.\n"
    "The artist is the performer named in the filename, not a channel, uploader, or folder name.\n"
    "When the filename repeats the same title in Latin letters and another script, "
    "keep the Latin version only.\n"
    "Drop a trailing year and words such as official, video, lyrics, or audio.\n"
    "Do not copy the whole filename into either field.\n"
    "Example shape, not a real title: "
    '"PERFORMER - SONG_ WORDS _ [same title in another script]_ YEAR.mp4" '
    'becomes {"author": "Performer", "song_name": "Song, words"}. '
    "Use words from the file you were given, not from this example."
)

DEFAULT_SYSTEM_PROMPT = _SYSTEM_PROMPT
# Saved prompts from the previous built-in text. Those told the model not to change case or punctuation.
LEGACY_SYSTEM_PROMPT_MARKER = (
    "Do not invent, correct, translate, or expand words beyond the filename tokens."
)


# Letters and digits in any script. Underscore stays a separator, as in "SONG_ WORDS".
_WORD = re.compile(r"[^\W_]+", re.UNICODE)


def _filename_tokens(filename: str) -> set[str]:
    stem = Path(filename).stem if filename else ""
    return {m.group(0).casefold() for m in _WORD.finditer(stem)}


def _restrict_to_filename_tokens(value: str, allowed_tokens: set[str]) -> str:
    """Keep only words that already appear in the filename. Punctuation may change."""
    parts = re.findall(r"[^\W_]+|(?:[^\w]|_)+", value or "", flags=re.UNICODE)
    kept: list[str] = []
    for part in parts:
        if _WORD.fullmatch(part):
            if part.casefold() in allowed_tokens:
                kept.append(part)
        else:
            # Keep separators only between kept tokens.
            if kept:
                kept.append(part)
    restricted = "".join(kept).strip()
    # Avoid dangling punctuation after token filtering.
    restricted = re.sub(r"^[^\w]+|[^\w]+$", "", restricted)
    return restricted.strip()


def _parse_json_object(content: str) -> dict[str, Any]:
    text = (content or "").strip()
    # Model might wrap in ```json ... ```
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        if start < 0:
            raise GroqInferenceError("Response was not valid JSON") from None
        try:
            data, _ = json.JSONDecoder().raw_decode(text[start:])
        except json.JSONDecodeError as e:
            raise GroqInferenceError("Response was not valid JSON") from e
    if not isinstance(data, dict):
        raise GroqInferenceError("JSON root must be an object")
    return data


def infer_author_song(
    settings: Settings,
    *,
    filename: str,
    folder_hint: str = "",
    probe_summary: str,
    timeout: float = 60.0,
    system_prompt: str | None = None,
) -> tuple[str, str]:
    """Call Groq chat completions; return (author, song_name).

    ``system_prompt`` replaces the built-in system prompt when non-empty.
    Artist and song are still limited to tokens that appear in ``filename``.
    """
    key = (settings.groq_api_key or "").strip()
    if not key:
        raise GroqInferenceError("Groq API key is not set (config or GROQ_API_KEY)")
    base = (settings.groq_base_url or "").strip().rstrip("/")
    if not base:
        raise GroqInferenceError("groq_base_url is empty")
    model = (settings.groq_model or "").strip() or DEFAULT_GROQ_MODEL
    url = f"{base}/chat/completions"

    hint = (folder_hint or "").strip()
    allowed_tokens = _filename_tokens(filename)
    user_msg = f"filename: {filename}\n"
    user_msg += f"filename_tokens: {', '.join(sorted(allowed_tokens)) or '(none)'}\n"
    if hint:
        user_msg += f"folder_hint: {hint}\n"
    user_msg += f"\nffprobe_summary:\n{probe_summary or '(none)'}\n"

    prompt = (system_prompt if system_prompt is not None else "").strip() or DEFAULT_SYSTEM_PROMPT
    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": user_msg},
    ]
    payload: dict[str, Any] = {
        "model": model,
        "temperature": float(settings.groq_temperature),
        "messages": messages,
        "response_format": {"type": "json_object"},
    }

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }

    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.post(url, headers=headers, json=payload)
            # Some models reject json_object; retry once without it.
            if r.status_code >= 400 and "response_format" in payload:
                r = client.post(
                    url,
                    headers=headers,
                    json={k: v for k, v in payload.items() if k != "response_format"},
                )
    except httpx.HTTPError as e:
        raise GroqInferenceError(str(e)) from e

    if r.status_code >= 400:
        try:
            detail = r.json()
        except Exception:
            detail = r.text
        raise GroqInferenceError(f"HTTP {r.status_code}: {detail}")

    try:
        body = r.json()
    except json.JSONDecodeError as e:
        raise GroqInferenceError("Invalid JSON from Groq") from e

    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        raise GroqInferenceError("No choices in Groq response")
    msg = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = ""
    if isinstance(msg, dict):
        content = str(msg.get("content") or "")
    data = _parse_json_object(content)
    author = str(data.get("author") or "").strip()
    song = str(data.get("song_name") or data.get("song") or "").strip()
    author = _restrict_to_filename_tokens(author, allowed_tokens)
    song = _restrict_to_filename_tokens(song, allowed_tokens)
    return author, song
