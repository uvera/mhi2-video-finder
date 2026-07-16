"""CliRunner-based characterization tests for `interactive`, ahead of splitting
cmd_interactive into named phases."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from mhi2_video_finder.cli import app
from mhi2_video_finder.search import VideoCandidate

runner = CliRunner()


def _candidate(video_id: str = "dQw4w9WgXcQ", title: str = "Song") -> VideoCandidate:
    return VideoCandidate(
        video_id=video_id,
        title=title,
        duration=125,
        channel="Some Channel",
        url=f"https://www.youtube.com/watch?v={video_id}",
    )


def _config(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'output_dir = "{tmp_path / "out"}"\nraw_cache_dir = "{tmp_path / "cache"}"\n')
    return cfg


def test_interactive_search_downloads_picked_video(tmp_path: Path, monkeypatch) -> None:
    import mhi2_video_finder.cli as cli_mod

    cand = _candidate()
    monkeypatch.setattr(cli_mod, "_gather_rows_cli", lambda *a, **k: ("q", [cand]))

    raw = tmp_path / "cache" / "raw.mkv"

    def fake_download(url, cache_dir, **kwargs):
        del url, kwargs
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_bytes(b"x")
        return raw, {"id": cand.video_id}

    def fake_transcode(raw_path, out_path, settings, **kwargs):
        del raw_path, settings, kwargs
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"y")

    monkeypatch.setattr(cli_mod, "download_to_cache", fake_download)
    monkeypatch.setattr(cli_mod, "transcode", fake_transcode)

    result = runner.invoke(
        app,
        ["interactive", "--config", str(_config(tmp_path))],
        input="1\n\nsome query\n1\nmyfolder\n",
    )

    assert result.exit_code == 0, result.output
    assert (tmp_path / "out" / "myfolder").exists()


def test_interactive_channel_mode_uses_list_channel_videos(tmp_path: Path, monkeypatch) -> None:
    import mhi2_video_finder.cli as cli_mod

    cand = _candidate()
    monkeypatch.setattr(cli_mod, "list_channel_videos", lambda *a, **k: [cand])

    raw = tmp_path / "cache" / "raw.mkv"

    def fake_download(url, cache_dir, **kwargs):
        del url, kwargs
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_bytes(b"x")
        return raw, {"id": cand.video_id}

    def fake_transcode(raw_path, out_path, settings, **kwargs):
        del raw_path, settings, kwargs
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"y")

    monkeypatch.setattr(cli_mod, "download_to_cache", fake_download)
    monkeypatch.setattr(cli_mod, "transcode", fake_transcode)

    result = runner.invoke(
        app,
        ["interactive", "--config", str(_config(tmp_path)), "--infer-subdir"],
        input="2\n@somechannel\n1\n",
    )

    assert result.exit_code == 0, result.output


def test_interactive_no_results_exits_nonzero(tmp_path: Path, monkeypatch) -> None:
    import mhi2_video_finder.cli as cli_mod

    monkeypatch.setattr(cli_mod, "_gather_rows_cli", lambda *a, **k: ("q", []))

    result = runner.invoke(
        app,
        ["interactive", "--config", str(_config(tmp_path))],
        input="1\n\nsome query\n",
    )

    assert result.exit_code != 0


def test_interactive_empty_pick_aborts(tmp_path: Path, monkeypatch) -> None:
    import mhi2_video_finder.cli as cli_mod

    cand = _candidate()
    monkeypatch.setattr(cli_mod, "_gather_rows_cli", lambda *a, **k: ("q", [cand]))

    result = runner.invoke(
        app,
        ["interactive", "--config", str(_config(tmp_path))],
        input="1\n\nsome query\n0\n",
    )

    assert result.exit_code != 0


def test_interactive_subdir_option_skips_prompt(tmp_path: Path, monkeypatch) -> None:
    import mhi2_video_finder.cli as cli_mod

    cand = _candidate()
    monkeypatch.setattr(cli_mod, "_gather_rows_cli", lambda *a, **k: ("q", [cand]))

    raw = tmp_path / "cache" / "raw.mkv"

    def fake_download(url, cache_dir, **kwargs):
        del url, kwargs
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_bytes(b"x")
        return raw, {"id": cand.video_id}

    def fake_transcode(raw_path, out_path, settings, **kwargs):
        del raw_path, settings, kwargs
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"y")

    monkeypatch.setattr(cli_mod, "download_to_cache", fake_download)
    monkeypatch.setattr(cli_mod, "transcode", fake_transcode)

    result = runner.invoke(
        app,
        ["interactive", "--config", str(_config(tmp_path)), "--subdir", "explicit"],
        input="1\n\nsome query\n1\n",
    )

    assert result.exit_code == 0, result.output
    assert (tmp_path / "out" / "explicit").exists()
