"""Tests for yt-dlp → MediaTags mapping and ffmpeg metadata argv."""

from pathlib import Path

from mhi2_video_finder.config import Settings, build_ffmpeg_command
from mhi2_video_finder.metadata import MediaTags, best_thumbnail_url, tags_from_ytdlp


def test_tags_from_ytdlp_uses_track_and_channel() -> None:
    tags = tags_from_ytdlp(
        {
            "title": "Official Video HD",
            "track": "Harder Better",
            "channel": "Daft Punk",
            "upload_date": "20090101",
        }
    )
    assert tags.title == "Harder Better"
    assert tags.artist == "Daft Punk"
    assert tags.date == "2009"


def test_tags_from_ytdlp_release_year() -> None:
    tags = tags_from_ytdlp({"title": "X", "artist": "Y", "release_year": 2020})
    assert tags.date == "2020"


def test_best_thumbnail_prefers_largest() -> None:
    url = best_thumbnail_url(
        {
            "thumbnails": [
                {"url": "https://small", "width": 120, "height": 90},
                {"url": "https://big", "width": 1920, "height": 1080},
            ]
        }
    )
    assert url == "https://big"


def test_best_thumbnail_fallback_single_field() -> None:
    assert best_thumbnail_url({"thumbnail": "https://one"}) == "https://one"


def test_media_tags_ffmpeg_args_skips_empty() -> None:
    t = MediaTags(title="A", artist="", album="B")
    args = t.ffmpeg_args()
    assert args == ["-metadata", "title=A", "-metadata", "album=B"]


def test_build_ffmpeg_command_includes_metadata_args() -> None:
    s = Settings()
    meta = ["-metadata", "title=Test", "-metadata", "artist=Me"]
    cmd = build_ffmpeg_command(Path("/in.mkv"), Path("/out.mp4"), s, metadata_args=meta)
    assert "-metadata" in cmd
    i = cmd.index("-metadata")
    assert cmd[i + 1] == "title=Test"
    assert cmd[-1] == "/out.mp4"
