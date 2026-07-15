"""Tests for recursive video scan."""

from __future__ import annotations

import os
import time
from pathlib import Path

from mhi2_video_finder.local_library import iter_video_files_recursive


def test_iter_video_files_recursive_orders_newest_mtime_first(tmp_path: Path) -> None:
    oldest = tmp_path / "oldest.mp4"
    middle = tmp_path / "middle.mp4"
    newest = tmp_path / "newest.mp4"
    for p in (oldest, middle, newest):
        p.write_bytes(b"x")
    now = time.time()
    os.utime(oldest, (now - 200, now - 200))
    os.utime(middle, (now - 100, now - 100))
    os.utime(newest, (now, now))

    out = iter_video_files_recursive(tmp_path)

    assert [p.name for p in out] == ["newest.mp4", "middle.mp4", "oldest.mp4"]


def test_iter_video_files_recursive(tmp_path: Path) -> None:
    (tmp_path / "a.mp4").write_bytes(b"x")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.mkv").write_bytes(b"x")
    (tmp_path / "ignore.txt").write_text("n")
    out = iter_video_files_recursive(tmp_path)
    assert len(out) == 2
    assert {p.name for p in out} == {"a.mp4", "b.mkv"}


def test_iter_video_files_recursive_skips_metadata_temp(tmp_path: Path) -> None:
    (tmp_path / "real.mp4").write_bytes(b"x")
    (tmp_path / ".vf-meta-94eq87x8.mp4").write_bytes(b"x")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / ".vf-meta-argwj6vl.mkv").write_bytes(b"x")
    out = iter_video_files_recursive(tmp_path)
    assert len(out) == 1
    assert out[0].name == "real.mp4"
