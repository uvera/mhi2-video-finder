# mhi2-video-finder

**Repository:** [github.com/uvera/mhi2-video-finder](https://github.com/uvera/mhi2-video-finder) · clone: `git@github.com:uvera/mhi2-video-finder.git`

Search YouTube for music-video-style results, download with [yt-dlp](https://github.com/yt-dlp/yt-dlp), and transcode with **ffmpeg** to **H.264 / AAC MP4** tuned for **Audi USB “music interface”** limits (and similar MMI/MHI2-style players).

## Requirements

- **Python** 3.11+
- **ffmpeg** on your `PATH` (with libx264, aac, and optionally VAAPI/NVENC for hardware encode)
- **yt-dlp** is installed automatically as a Python dependency (CLI `yt-dlp` is also available via the same package)

Install ffmpeg on Arch:

```bash
sudo pacman -S ffmpeg
```

## Install

### Arch Linux (pacman, from this checkout)

From the `pacman/` directory so build artifacts stay out of the Python `src/` tree:

```bash
cd pacman
makepkg -si
```

This installs `/usr/bin/mhi2-video-finder`, `/usr/bin/mhi2-video-finder-ui`, and the desktop entry system-wide.

Use a virtual environment (recommended on Arch and other PEP 668–managed Pythons):

```bash
cd /path/to/mhi2-video-finder
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### Graphical UI (PyQt6)

The GUI adds a **Search** tab (search term, channel `/videos`, or playlist URL), multi-select to **queue downloads**, a **Downloads** tab with yt-dlp progress, and a **Convert** tab with ffmpeg encode progress (percent from stream duration when known).

```bash
pip install -e ".[gui]"    # or pip install "mhi2-video-finder[gui]"
mhi2-video-finder-ui
mhi2-video-finder-ui --config /path/to/config.toml
```

A `.desktop` entry is installed as `mhi2-video-finder` (launches `mhi2-video-finder-ui`) when you install from the wheel.

Incomplete download/convert jobs are saved under **`$XDG_CACHE_HOME/mhi2-video-finder/jobs.sqlite`** (default `~/.cache/mhi2-video-finder/jobs.sqlite`) so they survive app restarts; finished jobs are removed from that database. Rows with a missing raw file or an existing output MP4 are cleaned up on startup.

## Config

Optional file: `$XDG_CONFIG_HOME/mhi2-video-finder/config.toml` (default: `~/.config/mhi2-video-finder/config.toml`).

Defaults already match **Audi’s published USB video limits** (see below). Override only if your car plays higher specs or for home use.

```toml
# Audi USB video: max 720x576, 25 fps, video <= 2000 kbit/s
max_width = 720
max_height = 576
fps = "25"
video_bitrate_k = 1800
video_bitrate_peak_k = 2000
audio_bitrate_k = 320
audio_sample_rate = 48000
h264_profile = "baseline"
h264_level = "3.1"
preset = "medium"
# h264_tune = "zerolatency"
# ffmpeg_threads = 8
# ffmpeg_nice = 10                  # Unix: nicer ffmpeg (yield CPU to other apps)
# ffmpeg_cpu_limit_percent = 50     # needs `cpulimit` on PATH; 0 = off
# max_parallel_downloads = 2        # GUI default 4; concurrent yt-dlp jobs
# max_parallel_converts = 1           # concurrent ffmpeg encodes in the GUI
# Optional: NVIDIA — may produce streams some cars dislike; prefer libx264 if USB playback fails.
# video_encoder = "h264_nvenc"
# nvenc_preset = "p1"
# Optional: Intel/AMD VAAPI (e.g. /dev/dri/renderD128). baseline → ffmpeg -profile:v 578 (constrained_baseline).
# video_encoder = "h264_vaapi"
# vaapi_device = "/dev/dri/renderD128"
# vaapi_profile = 0   # 0 = from h264_profile; or set e.g. 77 (main), 100 (high)
# vaapi_cbr = true    # VAAPI constant bitrate (rc_mode 2); set false if you prefer encoder default RC
scale_flags = "bicubic"
raw_cache_dir = "~/.cache/mhi2-video-finder/raw"
output_dir = "~/Videos/mhi2-video-finder"
# Embed title / artist / album (from yt-dlp) and highest-res thumbnail as MP4 album art.
embed_metadata = true
embed_album_art = true
```

To **lift limits** for a TV or PC (not the car), raise `max_width` / `max_height`, `fps`, `video_bitrate_k`, and `video_bitrate_peak_k` (or set `video_bitrate_peak_k = 0` to disable the peak cap).

Environment variables override the same keys when prefixed with `MHI2_VIDEO_FINDER_`.

By default, finished MP4s go under **`~/Videos/mhi2-video-finder`**. Before each download/transcode, the tool **creates the output folder immediately** and prints `Output folder (ready): /absolute/path` on stderr.

## Audi USB / music interface (official limits)

For **MPEG-4 AVC (H.264)** in `.mp4` / `.m4v` / `.mov` / `.avi` on USB, Audi documents:

| Constraint | Maximum |
|------------|---------|
| Resolution | **720 x 576** px |
| Frame rate | **25** fps |
| Video bitrate | **2,000** kbit/s |

**Audio:** up to **320 kbit/s**, **48 kHz** (AAC `.m4a` / `.aac` is supported).

**Media:** USB 2.0 mass storage; file systems **FAT, FAT32, or NTFS**; **at most two partitions** on the USB device.

The tool’s defaults respect the video table and use **AAC-LC** stereo at 48 kHz / 320 kb/s. If the car still skips files, try **exFAT→FAT32**, fewer files per folder, and **libx264** (not NVENC).

**Picture quality and smooth playback:** At a fixed video bitrate (e.g. 1800 kbit/s under a 2000 peak cap), **slower `libx264` presets** pack more detail than `veryfast` / `ultrafast`. **VAAPI** is faster but often looks softer or less stable than **libx264** at the same numbers; for “best USB copy,” keep **`video_encoder` unset** (libx264) and use **`preset = "medium"`** or **`slow`** if encode time is fine. **VAAPI** defaults include **`vaapi_cbr = true`** (CBR) to reduce bitrate swings that some head units decode unevenly; if motion still looks uneven, try **`libx264`** for that job.

## Usage

```bash
mhi2-video-finder search "daft punk harder better"
mhi2-video-finder search --artist "Daft Punk" --title "Harder Better Faster"
mhi2-video-finder channel "@daftpunk"
mhi2-video-finder playlist "https://www.youtube.com/playlist?list=..."
mhi2-video-finder download "https://www.youtube.com/watch?v=..." -o ./out.mp4
mhi2-video-finder convert ./input.mkv -o ./out.mp4
mhi2-video-finder get "artist song"
mhi2-video-finder get "artist song" --auto-first
mhi2-video-finder get "artist song" --subdir my-album
mhi2-video-finder interactive
mhi2-video-finder interactive --subdir my-picks
mhi2-video-finder get "artist song" --no-embed   # transcode only; no tags or embedded art
export YOUTUBE_API_KEY=...
mhi2-video-finder search "query" --use-youtube-api
```

### Faster encodes

**Biggest wins (already supported)**

- **`preset = "veryfast"`** or **`"ultrafast"`** — faster **libx264** encodes with a bit less quality at the same target bitrate than the default **`medium`**.
- **`video_encoder = "h264_nvenc"`** — **NVIDIA GPU** encode is usually much faster than CPU; keep **libx264** if the car rejects NVENC files.

**Smaller tweaks (optional in `config.toml`)**

- **`h264_tune = "zerolatency"`** — tiny **libx264** speed-up, slightly worse compression.
- **`ffmpeg_threads = 8`** (or your core count) — sometimes helps if ffmpeg/x264 under-uses CPU; **`0`** leaves the default (often fine).
- **`ffmpeg_nice = 10`** (Unix) — lower scheduling priority so other apps stay responsive; does not cap peak CPU when the machine is idle.
- **`ffmpeg_cpu_limit_percent = 50`** — soft cap on average CPU via **`cpulimit`** (install on Arch: `pacman -S cpulimit`); **`0`** disables. Ignored if `cpulimit` is not on `PATH`.
- **`max_parallel_downloads`** / **`max_parallel_converts`** — cap how many downloads or encodes run at once in **`mhi2-video-finder-ui`** (defaults **4** / **4**). The **Settings** tab can change these and save to `config.toml`.

**Outside this tool**

- **Two encodes at once** — run a second terminal and convert another file in parallel (watch CPU thermals).
- **Hardware decode** — advanced: run your own `ffmpeg` with **`-hwaccel cuda`** / **`-hwaccel vaapi`** before `-i` to speed decoding when you still CPU-encode; not wired into `mhi2-video-finder` today.
- **Power / thermals** — laptop on AC + performance mode avoids throttling mid-encode.
- **Shorter pipeline** — you’re already downscaling to **720×576**; there’s little left to skip without breaking Audi limits.

In **`mhi2-video-finder interactive`**, each finished **MP4 path is printed to stdout** as that encode completes; progress goes to stderr.

## Headless daemon (server) + remote GUI

Run download + transcode on a machine that stays on (e.g. **Podman** on a home server) so your laptop only queues jobs and pulls finished MP4s.

- **Install (daemon):** `pip install "mhi2-video-finder[daemon]"` (needs **ffmpeg** on the server).
- **Run:** `mhi2-video-finder-daemon` — listens on `DAEMON_HOST` / `DAEMON_PORT` (default `127.0.0.1:8765`). Set **`DAEMON_BEARER_TOKEN`** for LAN auth (same value in the GUI bearer field).
- **State:** `DAEMON_STATE_DIR` (default `/var/lib/mhi2-video-finder/state`) holds the SQLite job DB; mount it on a volume for persistence.
- **API:** OpenAPI [docs/daemon-api.yaml](docs/daemon-api.yaml) and WebSocket events [docs/daemon-websocket.md](docs/daemon-websocket.md).
- **Container / Podman:** [deploy/podman/README.md](deploy/podman/README.md), [deploy/podman/compose.yaml](deploy/podman/compose.yaml), and [Containerfile](Containerfile).

**GUI:** In **Settings → Processing backend**, choose **Remote server (Podman daemon)**, set base URL and token, and a **local folder** where finished files are saved (**Save to PC** on the Convert tab, or enable **Auto-download**). Switching local ↔ remote requires **restarting** `mhi2-video-finder-ui`.

Optional `config.toml` keys (same as other settings, or `MHI2_VIDEO_FINDER_*` env):

```toml
processing_backend = "local"   # or "remote"
remote_base_url = "http://myserver:8765"
remote_bearer_token = "your-secret"
remote_download_dir = "~/Videos/mhi2-video-finder-remote"
remote_auto_download = false
```

## Arch Linux packaging (AUR)

Upstream ships an AUR-style recipe under [`aur/`](aur/):

- Edit [`aur/PKGBUILD`](aur/PKGBUILD): set `url=` and `source=` to your real GitHub repo (replace `YOUR_GITHUB_USER`), then run `makepkg --printsrcinfo > aur/.SRCINFO` (or use Docker as in the script below).
- [`scripts/release-github-aur.sh`](scripts/release-github-aur.sh) — after `gh auth login`, runs `python -m build` (wheel + sdist, same as [`scripts/package-release.sh`](scripts/package-release.sh)), pushes tag `v<version>`, downloads the **GitHub** tag tarball, writes `sha256sums` into `aur/PKGBUILD`, regenerates `aur/.SRCINFO`, commits and pushes those files when they change (and moves the tag), builds a **pacman** package from [`pacman/PKGBUILD`](pacman/PKGBUILD) (must match `pyproject.toml` `version`), then creates a GitHub release attaching the wheel, sdist, and `.pkg.tar.zst`. The repo uses [`.gitattributes`](.gitattributes) `aur/ export-ignore` so the tag archive hash stays stable when only AUR metadata changes (same idea as [hikvision-viewer](https://github.com/uvera/hikvision-viewer)).
- [`scripts/package-release.sh`](scripts/package-release.sh) — local `python -m build` (wheel + sdist) only.

## Legal

Use only for content you are allowed to download. YouTube’s terms apply.
