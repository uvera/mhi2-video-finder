"""Tests for chat / messy multi-line YouTube paste parsing."""

from __future__ import annotations

from mhi2_video_finder.paste_urls import parse_pasted_video_urls


def test_parse_chat_log_youtube_watch_strips_list_and_timestamp() -> None:
    line = (
        "[13 04 2026 09:34] Slađa: https://www.youtube.com/watch?v=47bJs521ixs"
        "&list=RD47bJs521ixs&start_radio=1&t=1834s"
    )
    assert parse_pasted_video_urls(line) == ["https://www.youtube.com/watch?v=47bJs521ixs"]


def test_parse_without_scheme_normalizes() -> None:
    line = "www.youtube.com/watch?v=atb0mUKQgiY&list=RDatb0mUKQgiY&start_radio=1&pp=ygUJbmlrb2xpamEgoAcB"
    assert parse_pasted_video_urls(line) == ["https://www.youtube.com/watch?v=atb0mUKQgiY"]


def test_parse_encoded_pp_param() -> None:
    line = (
        "https://www.youtube.com/watch?v=KzCIULteklo&list=RDKzCIULteklo&start_radio=1&pp=ygUEY29ieaAHAQ%3D%3D"
    )
    assert parse_pasted_video_urls(line) == ["https://www.youtube.com/watch?v=KzCIULteklo"]


def test_parse_youtu_be() -> None:
    assert parse_pasted_video_urls("https://youtu.be/dQw4w9WgXcQ?t=42") == [
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    ]


def test_parse_multiple_lines_dedupes() -> None:
    text = (
        "https://www.youtube.com/watch?v=abc12345678&list=foo\n"
        "https://www.youtube.com/watch?v=abc12345678&list=bar\n"
    )
    assert parse_pasted_video_urls(text) == ["https://www.youtube.com/watch?v=abc12345678"]


def test_skips_hash_lines() -> None:
    text = "# https://www.youtube.com/watch?v=abc12345678\nhttps://www.youtube.com/watch?v=def45678901\n"
    assert parse_pasted_video_urls(text) == ["https://www.youtube.com/watch?v=def45678901"]
