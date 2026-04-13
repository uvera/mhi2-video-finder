"""Tests for query formatting."""

from unittest.mock import MagicMock, patch

from mhi2_video_finder.search import _yt_dlp_search_url, format_mv_query, video_from_url


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
    with patch("mhi2_video_finder.search.yt_dlp.YoutubeDL", return_value=mock_cm):
        c = video_from_url("https://youtu.be/dQw4w9WgXcQ")
    assert c.video_id == "dQw4w9WgXcQ"
    assert "Never Gonna" in c.title
    assert c.channel == "Rick Astley"
    assert c.duration == 212
    assert c.url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def test_video_from_url_watch_with_list_and_radio_params_is_canonicalized() -> None:
    fake = {
        "id": "47bJs521ixs",
        "title": "Example Song",
        "channel": "Example Channel",
        "duration": 180,
        "url": "https://www.youtube.com/watch?v=47bJs521ixs",
    }
    mock_ydl = MagicMock()
    mock_ydl.extract_info.return_value = fake
    mock_cm = MagicMock()
    mock_cm.__enter__.return_value = mock_ydl
    mock_cm.__exit__.return_value = None
    with patch("mhi2_video_finder.search.yt_dlp.YoutubeDL", return_value=mock_cm):
        c = video_from_url(
            "https://www.youtube.com/watch?v=47bJs521ixs&list=RD47bJs521ixs&start_radio=1&t=1834s"
        )
    mock_ydl.extract_info.assert_called_once_with(
        "https://www.youtube.com/watch?v=47bJs521ixs",
        download=False,
    )
    assert c.video_id == "47bJs521ixs"


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
    with patch("mhi2_video_finder.search.yt_dlp.YoutubeDL", return_value=mock_cm):
        try:
            video_from_url("https://www.youtube.com/playlist?list=PLx")
        except ValueError as e:
            assert "Playlist" in str(e) or "multiple" in str(e).lower()
        else:
            raise AssertionError("expected ValueError")
