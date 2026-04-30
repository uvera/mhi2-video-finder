"""Tests for recursive video scan."""

from __future__ import annotations

from pathlib import Path

from mhi2_video_finder.local_library import iter_video_files_recursive


def test_iter_video_files_recursive(tmp_path: Path) -> None:
    (tmp_path / "a.mp4").write_bytes(b"x")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.mkv").write_bytes(b"x")
    (tmp_path / "ignore.txt").write_text("n")
    out = iter_video_files_recursive(tmp_path)
    assert len(out) == 2
    assert {p.name for p in out} == {"a.mp4", "b.mkv"}
