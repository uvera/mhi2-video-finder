"""Tests for the best-effort build-date display shown near the top tabs."""

from __future__ import annotations

import sys
import types
from pathlib import Path

from mhi2_video_finder import build_info


def test_build_date_display_uses_packaged_stamp_when_present(monkeypatch) -> None:
    stamp_mod = types.ModuleType("mhi2_video_finder._build_stamp")
    stamp_mod.BUILD_DATE = "2026-07-15 14:00 UTC"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mhi2_video_finder._build_stamp", stamp_mod)

    assert build_info.build_date_display() == "2026-07-15 14:00 UTC"


def test_build_date_display_falls_back_to_dist_info_mtime(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setitem(sys.modules, "mhi2_video_finder._build_stamp", None)
    dist_dir = tmp_path / "mhi2_video_finder-9.9.9.dist-info"
    dist_dir.mkdir()
    record = dist_dir / "RECORD"
    record.write_text("x")
    import os
    import time

    stamped = time.time() - 3600
    os.utime(record, (stamped, stamped))

    class _FakeDist:
        _path = dist_dir

    monkeypatch.setattr(build_info.importlib.metadata, "distribution", lambda _name: _FakeDist())

    out = build_info.build_date_display()
    assert out == build_info.datetime.fromtimestamp(stamped).strftime("%Y-%m-%d %H:%M")


def test_build_date_display_falls_back_to_source_mtime_when_uninstalled(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "mhi2_video_finder._build_stamp", None)

    def _raise(_name: str):
        raise ModuleNotFoundError("not installed")

    monkeypatch.setattr(build_info.importlib.metadata, "distribution", _raise)

    out = build_info.build_date_display()
    assert out.endswith(" (dev)")
