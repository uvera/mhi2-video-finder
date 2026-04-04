"""Transcode to MHI2-oriented H.264/AAC MP4 via ffmpeg."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from video_finder.config import Settings, build_ffmpeg_command
from video_finder.metadata import (
    attach_album_art_to_mp4,
    best_thumbnail_url,
    download_thumbnail,
    tags_from_ytdlp,
)


def transcode(
    input_path: Path,
    output_path: Path,
    settings: Settings,
    *,
    ytdlp_info: dict[str, Any] | None = None,
) -> None:
    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    metadata_args: list[str] | None = None
    if ytdlp_info and settings.embed_metadata:
        tags = tags_from_ytdlp(ytdlp_info)
        if tags.any_set():
            metadata_args = tags.ffmpeg_args()

    cover_path: Path | None = None
    if ytdlp_info and settings.embed_album_art:
        turl = best_thumbnail_url(ytdlp_info)
        if turl:
            fd, ctmp = tempfile.mkstemp(suffix=".thumb", dir=str(output_path.parent))
            os.close(fd)
            cp = Path(ctmp)
            if download_thumbnail(turl, cp):
                cover_path = cp
            else:
                cp.unlink(missing_ok=True)

    encode_target = output_path
    tmp_encode: Path | None = None
    if cover_path is not None:
        fd, etmp = tempfile.mkstemp(suffix=".mp4", prefix="vf-enc-", dir=str(output_path.parent))
        os.close(fd)
        tmp_encode = Path(etmp)
        encode_target = tmp_encode

    try:
        cmd = build_ffmpeg_command(
            input_path.resolve(),
            encode_target,
            settings,
            metadata_args=metadata_args,
        )
        subprocess.run(cmd, check=True)
        if cover_path is not None:
            assert tmp_encode is not None
            try:
                attach_album_art_to_mp4(tmp_encode, cover_path, output_path)
            except subprocess.CalledProcessError:
                tmp_encode.replace(output_path)
                raise
    finally:
        if tmp_encode is not None:
            tmp_encode.unlink(missing_ok=True)
        if cover_path is not None:
            cover_path.unlink(missing_ok=True)
