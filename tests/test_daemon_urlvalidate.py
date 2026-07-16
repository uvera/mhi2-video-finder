"""URL validation for daemon job creation: single YouTube videos only."""

from __future__ import annotations

import pytest

from mhi2_video_finder.daemon.urlvalidate import validate_youtube_url


def test_accepts_plain_watch_url() -> None:
    assert (
        validate_youtube_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    )


def test_accepts_youtu_be_short_url() -> None:
    assert (
        validate_youtube_url("https://youtu.be/dQw4w9WgXcQ") == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    )


def test_accepts_bare_video_id() -> None:
    assert validate_youtube_url("dQw4w9WgXcQ") == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def test_rejects_non_youtube_host() -> None:
    with pytest.raises(ValueError):
        validate_youtube_url("https://example.com/video")


def test_rejects_empty_url() -> None:
    with pytest.raises(ValueError):
        validate_youtube_url("")


def test_rejects_playlist_url() -> None:
    with pytest.raises(ValueError):
        validate_youtube_url("https://www.youtube.com/playlist?list=PLxxxxxxxxxxxxxxxx")


def test_rejects_channel_url() -> None:
    with pytest.raises(ValueError):
        validate_youtube_url("https://www.youtube.com/channel/UCxxxxxxxxxxxxxxxxxxxxxx")


def test_rejects_handle_channel_url() -> None:
    with pytest.raises(ValueError):
        validate_youtube_url("https://www.youtube.com/@somechannel")


def test_rejects_watch_url_missing_video_id() -> None:
    with pytest.raises(ValueError):
        validate_youtube_url("https://www.youtube.com/watch?list=PLxxxxxxxxxxxxxxxx")


def test_strips_playlist_context_from_watch_url() -> None:
    """A watch URL carrying playlist context (list=) resolves to just its video."""
    result = validate_youtube_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLxxxxxxxxxxxxxxxx")
    assert result == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
