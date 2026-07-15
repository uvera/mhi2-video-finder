"""Tests for workflow helpers (filenames, stems)."""

from pathlib import Path

import pytest

from mhi2_video_finder.workflow import safe_join, safe_stem


def test_safe_stem_no_title_placeholder_uses_fallback() -> None:
    assert safe_stem("(no title)", "dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert safe_stem("(No Title)", "abc") == "abc"
    assert safe_stem("_no title_", "xyz") == "xyz"
    assert safe_stem("_no_title_", "xyz") == "xyz"


def test_safe_stem_real_title_unchanged() -> None:
    assert safe_stem("Harder Better Faster", "vid") == "Harder Better Faster"
    assert safe_stem("Song - live", "vid") == "Song - live"


def test_safe_join_normal_relative_parts(tmp_path: Path) -> None:
    assert safe_join(tmp_path, "sub", "file.mp4") == tmp_path / "sub" / "file.mp4"


def test_safe_join_no_parts_returns_base(tmp_path: Path) -> None:
    assert safe_join(tmp_path) == tmp_path.resolve()


def test_safe_join_rejects_dotdot_traversal(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        safe_join(tmp_path, "../../etc/passwd")


def test_safe_join_rejects_absolute_path_override(tmp_path: Path) -> None:
    # Path("/a") / "/etc/passwd" discards the base entirely (pathlib semantics);
    # safe_join must still catch this via the final relative_to check.
    with pytest.raises(ValueError):
        safe_join(tmp_path, "/etc/passwd")


def test_safe_join_rejects_dotdot_embedded_in_single_part(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        safe_join(tmp_path, "sub", "../../../tmp/evil")
