"""Best-effort build date for display in the UI (shown near the top tabs).

Packaging (``PKGBUILD``, ``scripts/package-release.sh``) stamps ``_build_stamp.py`` with the
exact build time. A plain ``pip install`` (no stamp file) falls back to the installed
dist-info timestamp; an editable/dev checkout falls back to this package's own file mtime.
"""

from __future__ import annotations

import importlib.metadata
from datetime import datetime
from pathlib import Path


def build_date_display() -> str:
    try:
        from mhi2_video_finder._build_stamp import BUILD_DATE

        if BUILD_DATE:
            return str(BUILD_DATE)
    except ImportError:
        pass

    try:
        dist = importlib.metadata.distribution("mhi2-video-finder")
        dist_path = Path(str(getattr(dist, "_path", "")))
        record = dist_path / "RECORD"
        stat_target = record if record.is_file() else dist_path
        ts = stat_target.stat().st_mtime
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except Exception:
        pass

    try:
        ts = Path(__file__).stat().st_mtime
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") + " (dev)"
    except OSError:
        return "unknown"
