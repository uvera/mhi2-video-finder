"""Scan local folders for video files (Library tab)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

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
    _dirty: bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        if not self.filename_stem:
            self.filename_stem = self.path.stem


def iter_video_files_recursive(root: Path) -> list[Path]:
    """Return sorted unique video paths under ``root`` (recursive)."""
    root = root.expanduser().resolve()
    if not root.is_dir():
        return []
    out: list[Path] = []
    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS:
            out.append(p)
    out.sort(key=lambda x: str(x).lower())
    return out
