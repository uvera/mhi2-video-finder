"""Tests for Groq inference JSON parsing (mocked HTTP)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from mhi2_video_finder.config import Settings
from mhi2_video_finder.groq_infer import (
    GroqInferenceError,
    _filename_tokens,
    _restrict_to_filename_tokens,
    infer_author_song,
)


def test_infer_author_song_parses_response(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeResp:
        status_code = 200

        def json(self) -> dict:
            return {
                "choices": [
                    {"message": {"content": '{"author": "Daft Punk", "song_name": "Around the World"}'}}
                ]
            }

    fake_client = MagicMock()
    fake_client.__enter__.return_value = fake_client
    fake_client.__exit__.return_value = None
    fake_client.post.return_value = FakeResp()

    monkeypatch.setattr("mhi2_video_finder.groq_infer.httpx.Client", lambda **kw: fake_client)

    s = Settings(groq_api_key="x", groq_model="m", groq_base_url="https://api.groq.com/openai/v1")
    author, song = infer_author_song(s, filename="Daft Punk - Around the World.mp4", probe_summary="dur: 1")
    assert author == "Daft Punk"
    assert song == "Around the World"


def test_infer_author_song_custom_prompt_still_filters_filename_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeResp:
        status_code = 200

        def json(self) -> dict:
            return {
                "choices": [
                    {
                        "message": {
                            "content": '{"author": "Daft Punk Live", "song_name": "Around the World Tonight"}'
                        }
                    }
                ]
            }

    fake_client = MagicMock()
    fake_client.__enter__.return_value = fake_client
    fake_client.__exit__.return_value = None
    fake_client.post.return_value = FakeResp()
    monkeypatch.setattr("mhi2_video_finder.groq_infer.httpx.Client", lambda **kw: fake_client)

    s = Settings(groq_api_key="x", groq_model="m", groq_base_url="https://api.groq.com/openai/v1")
    author, song = infer_author_song(
        s,
        filename="Daft Punk - Around the World.mp4",
        probe_summary="dur: 1",
        system_prompt="Custom instructions only.",
    )
    sent = fake_client.post.call_args.kwargs["json"]["messages"][0]["content"]
    assert sent == "Custom instructions only."
    assert author == "Daft Punk"
    assert song == "Around the World"


def test_filename_tokens_keep_cyrillic_and_drop_extension() -> None:
    filename = "IVANA - ALO_ DEVOYKITE _ Ивана - Ало_ девойките_ 2011.mp4"
    allowed = _filename_tokens(filename)
    assert "mp4" not in allowed
    assert "ивана" in allowed
    assert _restrict_to_filename_tokens("Ivana", allowed) == "Ivana"
    assert _restrict_to_filename_tokens("Alo, devoykite", allowed) == "Alo, devoykite"
    assert _restrict_to_filename_tokens("Ивана", allowed) == "Ивана"
    assert _restrict_to_filename_tokens("PlanetaOfficial", allowed) == ""


def test_infer_author_song_no_key() -> None:
    s = Settings(groq_api_key=None)
    with pytest.raises(GroqInferenceError, match="API key"):
        infer_author_song(s, filename="x.mp4", probe_summary="")
