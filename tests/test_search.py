"""Tests for query formatting."""

from video_finder.search import format_mv_query


def test_format_mv_query_with_title() -> None:
    q = format_mv_query(artist="Daft Punk", title="Harder Better Faster", template="music_video")
    assert "Daft Punk" in q and "Harder Better Faster" in q and "official music video" in q


def test_format_mv_query_artist_only() -> None:
    q = format_mv_query(artist="Radiohead", title=None, template="music_video")
    assert q.split() == "Radiohead official music video".split()
