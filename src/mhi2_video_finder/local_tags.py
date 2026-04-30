"""Apply MP4/Matroska metadata and safe file renames for local library files."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path

from mhi2_video_finder.ffmpeg_progress import prepare_ffmpeg_subprocess_argv
from mhi2_video_finder.local_library import METADATA_REMUX_TEMP_PREFIX
from mhi2_video_finder.metadata import MediaTags
from mhi2_video_finder.workflow import safe_stem


def unique_sibling_path(folder: Path, stem: str, ext: str, exclude: Path | None = None) -> Path:
    """Return ``folder / (stem + ext)`` or ``stem_2``, ``stem_3``, … if that name exists."""
    ext = ext if ext.startswith(".") else f".{ext}"
    candidate = folder / f"{stem}{ext}"
    if exclude is not None and candidate.resolve() == exclude.resolve():
        return candidate
    if not candidate.exists():
        return candidate
    n = 2
    while True:
        alt = folder / f"{stem}_{n}{ext}"
        if exclude is not None and alt.resolve() == exclude.resolve():
            return alt
        if not alt.exists():
            return alt
        n += 1


def sanitize_filename_stem(stem: str, fallback: str) -> str:
    """Slug a user-entered stem for filesystem use (preserve readability)."""
    s = safe_stem(stem, fallback)
    s = re.sub(r"[\x00-\x1f\x7f]", "", s)
    return s.strip() or fallback


def remux_copy_with_metadata(
    input_path: Path,
    output_path: Path,
    *,
    artist: str,
    title: str,
    nice_delta: int = 0,
    cpu_limit_percent: int = 0,
) -> None:
    """Copy all streams; set iTunes-style artist/title metadata."""
    tags = MediaTags(artist=artist.strip(), title=title.strip())
    meta = tags.ffmpeg_args()
    input_path = input_path.expanduser().resolve()
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    cmd: list[str] = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_path),
        "-map",
        "0",
        "-c",
        "copy",
    ]
    cmd.extend(meta)
    ext = input_path.suffix.lower()
    if ext in (".mp4", ".m4v", ".mov"):
        cmd.extend(["-movflags", "+faststart"])
    cmd.append(str(output_path))

    argv, pre = prepare_ffmpeg_subprocess_argv(
        cmd,
        nice_delta=nice_delta,
        cpu_limit_percent=cpu_limit_percent,
    )
    run_kw: dict[str, object] = {"check": True, "capture_output": True, "text": True}
    if pre is not None:
        run_kw["preexec_fn"] = pre
    proc = subprocess.run(argv, **run_kw)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(err or "ffmpeg metadata remux failed")


def apply_metadata_inplace(
    path: Path,
    *,
    artist: str,
    title: str,
    nice_delta: int = 0,
    cpu_limit_percent: int = 0,
) -> None:
    """Rewrite metadata in-place via a temp file in the same directory."""
    path = path.expanduser().resolve()
    parent = path.parent
    fd, tmp_name = tempfile.mkstemp(suffix=path.suffix, prefix=METADATA_REMUX_TEMP_PREFIX, dir=str(parent))
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        remux_copy_with_metadata(
            path,
            tmp,
            artist=artist,
            title=title,
            nice_delta=nice_delta,
            cpu_limit_percent=cpu_limit_percent,
        )
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def rename_if_needed(path: Path, new_stem: str) -> Path:
    """Rename ``path`` to ``new_stem`` + same suffix if different; avoid collisions."""
    path = path.expanduser().resolve()
    folder = path.parent
    ext = path.suffix
    fb = path.stem or "video"
    stem = sanitize_filename_stem(new_stem, fb)
    target = unique_sibling_path(folder, stem, ext, exclude=path)
    if target.resolve() == path.resolve():
        return path
    path.rename(target)
    return target


def apply_author_song_and_filename(
    path: Path,
    *,
    author: str,
    song_name: str,
    filename_stem: str,
    settings_nice: int = 0,
    settings_cpu_limit: int = 0,
) -> Path:
    """Update tags and optionally rename. Returns final path."""
    current = path.expanduser().resolve()
    apply_metadata_inplace(
        current,
        artist=author,
        title=song_name,
        nice_delta=settings_nice,
        cpu_limit_percent=settings_cpu_limit,
    )
    return rename_if_needed(current, filename_stem)
