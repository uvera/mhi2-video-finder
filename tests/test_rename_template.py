"""Filename templates for assisted library guessing."""

from __future__ import annotations

from mhi2_video_finder.rename_template import render_filename_stem


def test_render_filename_stem_both_fields() -> None:
    stem = render_filename_stem(
        "{song} - {artist}",
        song="Around the World",
        artist="Daft Punk",
        fallback="original",
    )
    assert stem == "Around the World - Daft Punk"


def test_render_filename_stem_missing_artist_drops_separator() -> None:
    stem = render_filename_stem(
        "{song} - {artist}",
        song="Song name",
        artist="",
        fallback="original",
    )
    assert stem == "Song name"


def test_render_filename_stem_missing_song_drops_separator() -> None:
    stem = render_filename_stem(
        "{song} - {artist}",
        song="",
        artist="Adele",
        fallback="original",
    )
    assert stem == "Adele"


def test_render_filename_stem_both_empty_keeps_fallback() -> None:
    stem = render_filename_stem(
        "{song} - {artist}",
        song="  ",
        artist="",
        fallback="original-name",
    )
    assert stem == "original-name"


def test_render_filename_stem_parentheses_around_artist() -> None:
    both = render_filename_stem(
        "{song} ({artist})",
        song="Song",
        artist="Adele",
        fallback="original",
    )
    assert both == "Song _Adele_"
    missing = render_filename_stem(
        "{song} ({artist})",
        song="Song",
        artist="",
        fallback="original",
    )
    assert missing == "Song"


def test_render_filename_stem_keeps_comma_in_song() -> None:
    stem = render_filename_stem(
        "{song} - {artist}",
        song="Alo, devoykite",
        artist="Ivana",
        fallback="original",
    )
    assert stem == "Alo, devoykite - Ivana"


def test_render_filename_stem_fixed_text_without_placeholders() -> None:
    stem = render_filename_stem(
        "My Video",
        song="Ignored",
        artist="Ignored",
        fallback="original",
    )
    assert stem == "My Video"
