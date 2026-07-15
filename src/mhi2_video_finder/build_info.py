"""Best-effort build metadata for display in the UI and daemon diagnostics.

Packaging (``PKGBUILD``, ``scripts/package-release.sh``) stamps ``_build_stamp.py`` with the
exact build time and git commit. A plain ``pip install`` (no stamp file) falls back to the
installed dist-info timestamp; an editable/dev checkout falls back to this package's own file
mtime / the local git checkout.
"""

from __future__ import annotations

import importlib.metadata
import subprocess
from datetime import datetime
from pathlib import Path


def _stamp_attr(name: str) -> str | None:
    try:
        import mhi2_video_finder._build_stamp as stamp
    except ImportError:
        return None
    val = getattr(stamp, name, None)
    return str(val) if val else None


def build_date_display() -> str:
    stamped = _stamp_attr("BUILD_DATE")
    if stamped:
        return stamped

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


def build_commit_display() -> str:
    stamped = _stamp_attr("BUILD_COMMIT")
    if stamped:
        return stamped

    # Dev/editable checkout: ask the local git repo directly (best-effort).
    repo_root = Path(__file__).resolve().parents[2]
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=2.0,
            check=True,
        )
        sha = out.stdout.strip()
        if sha:
            return f"{sha} (dev)"
    except Exception:
        pass
    return "unknown"
