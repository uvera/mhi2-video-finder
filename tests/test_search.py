"""Tests for query formatting."""

from unittest.mock import MagicMock, patch

from video_finder.search import _yt_dlp_search_url, format_mv_query, video_from_url


def test_yt_dlp_search_url_limited_and_unlimited() -> None:
    assert _yt_dlp_search_url("hello world", 15) == "ytsearch15:hello world"
    assert _yt_dlp_search_url("hello world", 1) == "ytsearch1:hello world"
    assert _yt_dlp_search_url("hello world", None) == "ytsearchall:hello world"


def test_format_mv_query_with_title() -> None:
    q = format_mv_query(artist="Daft Punk", title="Harder Better Faster", template="music_video")
    assert "Daft Punk" in q and "Harder Better Faster" in q and "official music video" in q


def test_format_mv_query_artist_only() -> None:
    q = format_mv_query(artist="Radiohead", title=None, template="music_video")
    assert q.split() == "Radiohead official music video".split()


def test_video_from_url_single_video_mock() -> None:
    fake = {
        "id": "dQw4w9WgXcQ",
        "title": "Never Gonna Give You Up",
        "channel": "Rick Astley",
        "duration": 212,
        "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    }
    mock_ydl = MagicMock()
    mock_ydl.extract_info.return_value = fake
    mock_cm = MagicMock()
    mock_cm.__enter__.return_value = mock_ydl
    mock_cm.__exit__.return_value = None
    with patch("video_finder.search.yt_dlp.YoutubeDL", return_value=mock_cm):
        c = video_from_url("https://youtu.be/dQw4w9WgXcQ")
    assert c.video_id == "dQw4w9WgXcQ"
    assert "Never Gonna" in c.title
    assert c.channel == "Rick Astley"
    assert c.duration == 212
    assert c.url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def test_video_from_url_playlist_rejected_mock() -> None:
    fake = {
        "entries": [
            {"id": "aaaaaaaaaaa", "title": "A", "channel": "C", "duration": 1},
            {"id": "bbbbbbbbbbb", "title": "B", "channel": "C", "duration": 1},
        ],
    }
    mock_ydl = MagicMock()
    mock_ydl.extract_info.return_value = fake
    mock_cm = MagicMock()
    mock_cm.__enter__.return_value = mock_ydl
    mock_cm.__exit__.return_value = None
    with patch("video_finder.search.yt_dlp.YoutubeDL", return_value=mock_cm):
        try:
            video_from_url("https://www.youtube.com/playlist?list=PLx")
        except ValueError as e:
            assert "Playlist" in str(e) or "multiple" in str(e).lower()
        else:
            raise AssertionError("expected ValueError")
