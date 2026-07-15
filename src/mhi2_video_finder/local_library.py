"""Scan local folders for video files (Library tab)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# Temp files created during metadata remux (`apply_metadata_inplace`); keep in sync with local_tags.
METADATA_REMUX_TEMP_PREFIX = ".vf-meta-"

# Common container extensions; user may add more later.
VIDEO_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".mp4",
        ".mkv",
        ".webm",
        ".avi",
        ".mov",
        ".m4v",
        ".wmv",
        ".flv",
        ".ogv",
        ".mpeg",
        ".mpg",
    }
)


@dataclass
class LibraryFileRow:
    """One row in the library table (mutable while editing)."""

    path: Path
    author: str = ""
    song_name: str = ""
    filename_stem: str = ""
    probe_summary: str = ""
    ai_guess_status: str = "None"
    _dirty: bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        if not self.filename_stem:
            self.filename_stem = self.path.stem


def iter_video_files_recursive(root: Path) -> list[Path]:
    """Return video paths under ``root`` (recursive), newest file first (mtime)."""
    root = root.expanduser().resolve()
    if not root.is_dir():
        return []
    out: list[Path] = []
    for p in root.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in VIDEO_EXTENSIONS:
            continue
        if p.name.startswith(METADATA_REMUX_TEMP_PREFIX):
            continue
        out.append(p)

    def _sort_key(p: Path) -> tuple[float, str]:
        try:
            mtime = p.stat().st_mtime
        except OSError:
            mtime = 0.0
        return (-mtime, str(p).lower())

    out.sort(key=_sort_key)
    return out
