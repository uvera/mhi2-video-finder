"""Tests for safe rename collision handling."""

from __future__ import annotations

from pathlib import Path

from mhi2_video_finder.local_tags import sanitize_filename_stem, unique_sibling_path


def test_unique_sibling_path_no_collision(tmp_path: Path) -> None:
    p = unique_sibling_path(tmp_path, "foo", ".mp4")
    assert p == tmp_path / "foo.mp4"


def test_unique_sibling_path_collision(tmp_path: Path) -> None:
    (tmp_path / "foo.mp4").write_bytes(b"a")
    p = unique_sibling_path(tmp_path, "foo", ".mp4")
    assert p == tmp_path / "foo_2.mp4"


def test_unique_sibling_path_exclude_same_file(tmp_path: Path) -> None:
    existing = tmp_path / "foo.mp4"
    existing.write_bytes(b"a")
    p = unique_sibling_path(tmp_path, "foo", ".mp4", exclude=existing)
    assert p == existing


def test_sanitize_filename_stem() -> None:
    assert sanitize_filename_stem("  hello / world  ", "fb") == "hello _ world"
