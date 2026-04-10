"""Typer CLI for mhi2-video-finder."""

from __future__ import annotations

import sys
from pathlib import Path

import typer

from mhi2_video_finder import __version__
from mhi2_video_finder.config import Settings, load_settings
from mhi2_video_finder.download import download_to_cache
from mhi2_video_finder.search import VideoCandidate, list_channel_videos, list_playlist_videos
from mhi2_video_finder.transcode import transcode
from mhi2_video_finder.workflow import (
    ensure_and_print_output_dir,
    fmt_duration,
    gather_search_rows,
    parse_multi_pick,
    safe_stem,
    slug_dir_name,
    unique_out_path,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


app = typer.Typer(no_args_is_help=True, help="YouTube music-video search, download, and MHI2-oriented transcode.")
settings_opt = typer.Option(None, "--config", help="Path to config.toml (default: XDG config path)")


def _load(path: Path | None) -> Settings:
    return load_settings(path)


def _gather_rows_cli(
    s: Settings,
    *,
    n: int | None,
    mf: str | None,
    use_youtube_api: bool,
    query: str,
    artist: str | None,
    title: str | None,
    template: str,
    channel_id: str | None,
) -> tuple[str, list[VideoCandidate]]:
    try:
        return gather_search_rows(
            s,
            n=n,
            mf=mf,
            use_youtube_api=use_youtube_api,
            query=query,
            artist=artist,
            title=title,
            template=template,
            channel_id=channel_id,
        )
    except ValueError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e


def _print_candidates(rows: list[VideoCandidate]) -> None:
    for i, c in enumerate(rows, 1):
        dur = fmt_duration(c.duration)
        typer.echo(f"{i:3}. [{dur}] {c.title}\n     {c.channel}\n     {c.url}")


def _pick_index(n: int, choice: int | None) -> int:
    if choice is not None:
        if choice < 1 or choice > n:
            raise typer.BadParameter(f"index must be 1..{n}")
        return choice - 1
    try:
        line = input(f"Enter number (1–{n}) or 0 to cancel: ")
    except EOFError:
        raise typer.Abort()
    try:
        idx = int(line.strip())
    except ValueError:
        raise typer.BadParameter("invalid number")
    if idx == 0:
        raise typer.Abort()
    if idx < 1 or idx > n:
        raise typer.BadParameter(f"expected 1..{n}")
    return idx - 1


@app.callback()
def _main(
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show version and exit.",
    ),
) -> None:
    """mhi2-video-finder: YouTube → MHI2-oriented MP4."""
    pass


@app.command("search")
def cmd_search(
    query: str = typer.Argument(..., help="Search query or use --artist/--title for templates"),
    config: Path | None = settings_opt,
    limit: int = typer.Option(None, "--limit", "-n", help="Max results"),
    artist: str | None = typer.Option(None, "--artist", "-a"),
    title: str | None = typer.Option(None, "--title", "-t"),
    template: str = typer.Option("music_video", "--template", help="music_video, vevo, official, plain"),
    match_filter: str | None = typer.Option(None, "--match-filter", help="yt-dlp match filter expression"),
    use_youtube_api: bool = typer.Option(False, "--use-youtube-api", help="Use YouTube Data API (Music category)"),
    channel_id: str | None = typer.Option(None, "--channel-id", help="With API: restrict to channel ID"),
) -> None:
    """Search YouTube for videos (heuristic music-video query or Data API)."""
    s = _load(config)
    n = limit if limit is not None else s.search_limit
    mf = match_filter or s.match_filter

    if use_youtube_api:
        key = s.youtube_api_key
        if not key:
            typer.echo("YOUTUBE_API_KEY or youtube_api_key in config is required for --use-youtube-api", err=True)
            raise typer.Exit(1)

    _, rows = _gather_rows_cli(
        s,
        n=n,
        mf=mf,
        use_youtube_api=use_youtube_api,
        query=query,
        artist=artist,
        title=title,
        template=template,
        channel_id=channel_id,
    )
    _print_candidates(rows)


@app.command("playlist")
def cmd_playlist(
    url: str = typer.Argument(..., help="Playlist URL"),
    config: Path | None = settings_opt,
    limit: int = typer.Option(None, "--limit", "-n"),
    match_filter: str | None = typer.Option(None, "--match-filter"),
) -> None:
    """List videos from a YouTube playlist."""
    s = _load(config)
    n = limit if limit is not None else s.search_limit
    mf = match_filter or s.match_filter
    rows = list_playlist_videos(url, limit=n, match_filter=mf)
    _print_candidates(rows)


@app.command("channel")
def cmd_channel(
    channel: str = typer.Argument(..., help="Channel @handle, channel URL, or ID URL"),
    config: Path | None = settings_opt,
    limit: int = typer.Option(None, "--limit", "-n"),
    match_filter: str | None = typer.Option(None, "--match-filter"),
) -> None:
    """List recent videos from a channel /videos tab."""
    s = _load(config)
    n = limit if limit is not None else s.search_limit
    mf = match_filter or s.match_filter
    rows = list_channel_videos(channel, limit=n, match_filter=mf)
    _print_candidates(rows)


@app.command("download")
def cmd_download(
    url_or_id: str = typer.Argument(..., help="Watch URL or 11-char video ID"),
    config: Path | None = settings_opt,
    output: Path | None = typer.Option(None, "--output", "-o", help="Output .mp4 path"),
    no_transcode: bool = typer.Option(False, "--no-transcode", help="Keep raw merged file only"),
    no_embed: bool = typer.Option(
        False,
        "--no-embed",
        help="Do not embed title/artist/album tags or album art from YouTube.",
    ),
) -> None:
    """Download a video and transcode to MHI2-oriented MP4 (default)."""
    s = _load(config)
    out_path: Path | None = None
    if output is not None:
        out_path = output.expanduser().resolve()
        ensure_and_print_output_dir(out_path.parent)
    else:
        ensure_and_print_output_dir(s.merged_output_dir())

    typer.echo("Downloading…", err=True)
    sys.stderr.flush()
    raw, yinfo = download_to_cache(url_or_id, s.merged_raw_cache_dir())
    if no_transcode:
        typer.echo(str(raw))
        return
    if out_path is None:
        stem = raw.stem
        out_path = s.output_dir / f"{stem}.mp4"
    transcode(raw, out_path, s, ytdlp_info=None if no_embed else yinfo)
    typer.echo(str(out_path))


@app.command("convert")
def cmd_convert(
    input_path: Path = typer.Argument(..., exists=True, readable=True, help="Input media file"),
    config: Path | None = settings_opt,
    output: Path | None = typer.Option(None, "--output", "-o", help="Output .mp4"),
) -> None:
    """Transcode a local file to MHI2-oriented MP4."""
    s = _load(config)
    if output is not None:
        out = output.expanduser().resolve()
        ensure_and_print_output_dir(out.parent)
    else:
        s.merged_output_dir()
        ensure_and_print_output_dir(s.output_dir.resolve())
        out = s.output_dir / f"{input_path.stem}.mp4"
    transcode(input_path, out, s)
    typer.echo(str(out))


@app.command("get")
def cmd_get(
    query: str = typer.Argument(..., help="Search query"),
    config: Path | None = settings_opt,
    limit: int = typer.Option(None, "--limit", "-n"),
    auto_first: bool = typer.Option(False, "--auto-first", help="Pick first search result"),
    pick: int | None = typer.Option(None, "--pick", "-p", help="1-based index from search"),
    match_filter: str | None = typer.Option(None, "--match-filter"),
    artist: str | None = typer.Option(None, "--artist", "-a"),
    title: str | None = typer.Option(None, "--title", "-t"),
    template: str = typer.Option("music_video", "--template"),
    output: Path | None = typer.Option(None, "--output", "-o"),
    subdir: str | None = typer.Option(
        None,
        "--subdir",
        "-d",
        help="Save under this subfolder of output_dir (ignored if -o is set).",
    ),
    use_youtube_api: bool = typer.Option(False, "--use-youtube-api"),
    no_embed: bool = typer.Option(
        False,
        "--no-embed",
        help="Do not embed tags or album art from YouTube.",
    ),
) -> None:
    """Search, pick, download, and transcode in one step."""
    s = _load(config)
    n = limit if limit is not None else s.search_limit
    mf = match_filter or s.match_filter

    _, rows = _gather_rows_cli(
        s,
        n=n,
        mf=mf,
        use_youtube_api=use_youtube_api,
        query=query,
        artist=artist,
        title=title,
        template=template,
        channel_id=None,
    )

    if not rows:
        typer.echo("No results.", err=True)
        raise typer.Exit(1)

    if auto_first:
        chosen = rows[0]
    elif pick is not None:
        chosen = rows[_pick_index(len(rows), pick)]
    else:
        _print_candidates(rows)
        chosen = rows[_pick_index(len(rows), None)]

    typer.echo(f"Selected: {chosen.title}", err=True)

    out_path: Path
    if output is not None:
        out_path = output.expanduser().resolve()
        ensure_and_print_output_dir(out_path.parent)
    else:
        s.merged_output_dir()
        stem = safe_stem(chosen.title, chosen.video_id)
        if subdir:
            sub_folder = ensure_and_print_output_dir(s.output_dir / subdir)
            out_path = unique_out_path(sub_folder, stem, chosen.video_id)
        else:
            base = ensure_and_print_output_dir(s.output_dir.resolve())
            out_path = unique_out_path(base, stem, chosen.video_id)

    typer.echo("Downloading…", err=True)
    sys.stderr.flush()
    raw, yinfo = download_to_cache(chosen.url, s.merged_raw_cache_dir())
    transcode(raw, out_path, s, ytdlp_info=None if no_embed else yinfo)
    typer.echo(str(out_path))


@app.command("interactive")
def cmd_interactive(
    config: Path | None = settings_opt,
    limit: int | None = typer.Option(
        None,
        "--limit",
        "-n",
        help="Max search/channel results (default: all pages yt-dlp/API return).",
    ),
    match_filter: str | None = typer.Option(None, "--match-filter"),
    template: str = typer.Option("music_video", "--template"),
    use_youtube_api: bool = typer.Option(False, "--use-youtube-api"),
    subdir: str | None = typer.Option(
        None,
        "--subdir",
        "-d",
        help="Output subfolder under config output_dir (skips prompt if set).",
    ),
    no_embed: bool = typer.Option(
        False,
        "--no-embed",
        help="Do not embed tags or album art from YouTube.",
    ),
) -> None:
    """Prompted flow: search or channel list, pick one or more videos, save MP4s in a subfolder."""
    s = _load(config)
    n: int | None = limit
    mf = match_filter or s.match_filter
    base_out = s.merged_output_dir()

    typer.echo("Interactive mode — follow the prompts (empty line uses defaults where shown).\n")

    try:
        src = input("Source  [1] YouTube search  [2] Channel videos [1]: ").strip() or "1"
    except EOFError:
        raise typer.Abort()

    rows: list[VideoCandidate] = []
    subdir_default = "interactive"

    if src == "2":
        try:
            ch = input("Channel (@handle or full URL): ").strip()
        except EOFError:
            raise typer.Abort()
        if not ch:
            typer.echo("Channel required.", err=True)
            raise typer.Exit(1)
        typer.echo("Fetching videos…", err=True)
        rows = list_channel_videos(ch, limit=n, match_filter=mf)
        subdir_default = slug_dir_name(ch, "channel")
    else:
        if use_youtube_api:
            try:
                q = input("Search query: ").strip()
            except EOFError:
                raise typer.Abort()
            if not q:
                typer.echo("Query required.", err=True)
                raise typer.Exit(1)
            try:
                artist = input("Artist (optional, for template — Enter to skip): ").strip() or None
                title = input("Song title (optional): ").strip() or None
            except EOFError:
                raise typer.Abort()
            typer.echo("Searching (YouTube Data API)…", err=True)
            _, rows = _gather_rows_cli(
                s,
                n=n,
                mf=mf,
                use_youtube_api=True,
                query=q,
                artist=artist,
                title=title,
                template=template,
                channel_id=None,
            )
            subdir_default = slug_dir_name(q, "search")
        else:
            try:
                artist = input("Artist (optional — if set, uses music-video template): ").strip() or None
                if artist:
                    title = input("Song / title (optional): ").strip() or None
                    q = ""
                else:
                    q = input("Search query: ").strip()
                    title = None
            except EOFError:
                raise typer.Abort()
            if artist:
                typer.echo("Searching…", err=True)
                _, rows = _gather_rows_cli(
                    s,
                    n=n,
                    mf=mf,
                    use_youtube_api=False,
                    query="",
                    artist=artist,
                    title=title,
                    template=template,
                    channel_id=None,
                )
                subdir_default = slug_dir_name(f"{artist} {title or ''}", "search")
            else:
                if not q:
                    typer.echo("Search query or artist required.", err=True)
                    raise typer.Exit(1)
                typer.echo("Searching…", err=True)
                _, rows = _gather_rows_cli(
                    s,
                    n=n,
                    mf=mf,
                    use_youtube_api=False,
                    query=q,
                    artist=None,
                    title=None,
                    template=template,
                    channel_id=None,
                )
                subdir_default = slug_dir_name(q, "search")

    if not rows:
        typer.echo("No results.", err=True)
        raise typer.Exit(1)

    _print_candidates(rows)
    try:
        pick_line = input(
            f"Pick video(s) to download — one number, comma list (1,3), range (2-4), or 'all' — 0 to cancel: "
        ).strip()
    except EOFError:
        raise typer.Abort()
    picked = parse_multi_pick(pick_line, len(rows))
    if not picked:
        typer.echo("Cancelled.", err=True)
        raise typer.Abort()

    folder_name = subdir
    if not folder_name:
        try:
            prompt = f"Subfolder under {base_out} [{subdir_default}]: "
            folder_name = input(prompt).strip() or subdir_default
        except EOFError:
            raise typer.Abort()

    out_folder = ensure_and_print_output_dir(base_out / folder_name)

    n_pick = len(picked)
    for j, i in enumerate(picked, 1):
        chosen = rows[i]
        typer.echo(f"\n[{j}/{n_pick}] {chosen.title}\nDownloading…", err=True)
        sys.stderr.flush()
        raw, yinfo = download_to_cache(chosen.url, s.merged_raw_cache_dir())
        stem = safe_stem(chosen.title, chosen.video_id)
        out_path = unique_out_path(out_folder, stem, chosen.video_id)
        typer.echo(f"Transcoding to {out_path.name} …", err=True)
        sys.stderr.flush()
        transcode(raw, out_path, s, ytdlp_info=None if no_embed else yinfo)
        # One path per completed file on stdout (flush immediately for scripts / pipelines).
        print(str(out_path.resolve()), flush=True)
        typer.echo(f"Finished [{j}/{n_pick}]: {out_path.name}", err=True)
        sys.stderr.flush()


if __name__ == "__main__":
    app()
