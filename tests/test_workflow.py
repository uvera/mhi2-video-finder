"""Tests for workflow helpers (filenames, stems)."""

from mhi2_video_finder.workflow import safe_stem


def test_safe_stem_no_title_placeholder_uses_fallback() -> None:
    assert safe_stem("(no title)", "dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert safe_stem("(No Title)", "abc") == "abc"
    assert safe_stem("_no title_", "xyz") == "xyz"
    assert safe_stem("_no_title_", "xyz") == "xyz"


def test_safe_stem_real_title_unchanged() -> None:
    assert safe_stem("Harder Better Faster", "vid") == "Harder Better Faster"
    assert safe_stem("Song - live", "vid") == "Song - live"
