"""Groq OpenAI-compatible chat completions for song/artist inference."""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from mhi2_video_finder.config import Settings


class GroqInferenceError(Exception):
    """API or parse failure."""


_SYSTEM_PROMPT = """You help identify music video files. Given a filename, optional folder/path hint, and technical media summary, \
infer the performing artist and song title. Respond with a single JSON object only, no markdown, with keys:
"author" (string) and "song_name" (string). Use empty string if unknown. Do not include other keys."""


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
) -> tuple[str, str]:
    """Call Groq chat completions; return (author, song_name)."""
    key = (settings.groq_api_key or "").strip()
    if not key:
        raise GroqInferenceError("Groq API key is not set (config or GROQ_API_KEY)")
    base = (settings.groq_base_url or "").strip().rstrip("/")
    if not base:
        raise GroqInferenceError("groq_base_url is empty")
    model = (settings.groq_model or "").strip() or "llama-3.1-8b-instant"
    url = f"{base}/chat/completions"

    hint = (folder_hint or "").strip()
    user_msg = f"filename: {filename}\n"
    if hint:
        user_msg += f"folder_hint: {hint}\n"
    user_msg += f"\nffprobe_summary:\n{probe_summary or '(none)'}\n"

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
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
    return author, song
