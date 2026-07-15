"""Tests for Groq inference JSON parsing (mocked HTTP)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from mhi2_video_finder.config import Settings
from mhi2_video_finder.groq_infer import GroqInferenceError, infer_author_song


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


def test_infer_author_song_no_key() -> None:
    s = Settings(groq_api_key=None)
    with pytest.raises(GroqInferenceError, match="API key"):
        infer_author_song(s, filename="x.mp4", probe_summary="")
