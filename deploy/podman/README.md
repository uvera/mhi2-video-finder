# Podman deployment (daemon)

## Podman Compose (recommended)

### If `curl http://127.0.0.1:8765/healthz` shows only `{"status":"ok"}`

That means the **container is still an old image**. Editing files under `src/` on the host does **nothing** until you **rebuild the image** and **recreate** the container. `podman-compose up -d --build` often **skips** the build when it thinks nothing changed.

From **`deploy/podman/`** after `git pull`:

```bash
./rebuild-daemon.sh
```

(Same script: **`./rebuild-daemons.sh`** if you typed an extra `s`.)

The script runs **`sudo podman-compose`** by default (rootful Podman). For rootless: `USE_SUDO=0 ./rebuild-daemon.sh`.

If healthz still has no `app_version`, the script prints **Diagnostics** (host vs in-container checks via **Python** inside the slim image, `podman inspect` **Config.Cmd**, `podman ps`). Use that output to see whether the image was built from the wrong `Containerfile` (CMD stuck on `mhi2-video-finder-daemon` instead of `python -m …`).

**podman-compose 1.0.x:** `compose build` only checks for **`Dockerfile`** inside `context` (see `OSError: Dockerfile not found in ../..`). The repo root **`Dockerfile`** is a **symlink to `Containerfile`**. Do not delete it if you use `podman-compose`; modern `docker compose` / Podman 5 still work the same.

**Rootless build / slirp4netns:** If `podman-compose build` dies on `RUN pip install …` with `failed to read from slirp4netns sync pipe: EOF`, rootless networking for build steps failed. **`./rebuild-daemon.sh` defaults to `podman build --network=host`** (build-only; the running container still uses normal compose networking). To use plain `podman-compose build` again: `PODMAN_BUILD_NETWORK=default ./rebuild-daemon.sh`. Manual one-off: from repo root, `podman build --network=host --no-cache -f Dockerfile -t mhi2-video-finder-daemon .` then `podman-compose up -d` from `deploy/podman/`.

To force dropping the old tagged image before build: `PRUNE_IMAGE=1 ./rebuild-daemon.sh`.

Or manually:

```bash
sudo podman-compose down
sudo podman-compose build --no-cache
sudo podman-compose up -d
curl -sS http://127.0.0.1:8765/healthz   # expect "app_version" in JSON
```

---

From **`deploy/podman/`** (first-time):

```bash
cd deploy/podman
cp -n config.toml.example config.toml
cp -n .env.example .env
# Edit config.toml and .env (set DAEMON_BEARER_TOKEN)
podman compose up -d --build
```

**Compose command** — use whichever your install provides:

| Install | Typical command |
|--------|------------------|
| Podman 5+ / compose plugin | `podman compose up -d --build` |
| Older Podman (e.g. 4.3) without `compose` subcommand | `podman-compose up -d --build` (install `podman-compose` package) |
| Docker | `docker compose up -d --build` |

**Container logs** — to see daemon stderr (including `mhi2-vf[daemon]: NDJSON debug …` lines), this always works when `container_name` is `mhi2-vf`:

```bash
podman logs mhi2-vf 2>&1
```

Some `podman-compose` versions print their own noise; `podman logs` shows only the container stream.

- **`config.toml`** must exist before `up` (compose mounts it read-only). It sets `raw_cache_dir` and `output_dir` to the in-container paths used by the named volumes in [compose.yaml](compose.yaml).
- **`.env`** holds `DAEMON_BEARER_TOKEN` (gitignored) and is loaded via `env_file` (no compose interpolation); use the same token in the GUI remote settings.
- **Telegram bot (optional):** set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_ALLOWED_USER_IDS` (comma-separated numeric Telegram user IDs) in `.env`. The daemon starts a long-polling worker in the same process and queues YouTube URLs as normal daemon jobs. Without an allowlist, the bot does not start. See comments in [`.env.example`](.env.example).
- **`MHI2_VF_PORT`** in `.env` changes the published host port (default `8765`).
- **Debug NDJSON logs** (daemon instrumentation via `emit_debug_log`) are written to **`debug/debug-runtime.log`** next to `compose.yaml` (bind-mounted into the container). The `debug/` folder is a normal directory on disk, not a Git repository (there is no `debug/.git`).
- If the bind mount is not writable (permissions / SELinux), the daemon falls back to **`/tmp/mhi2-video-finder/debug-runtime.log`** inside the container and prints errors to **container logs** (`podman logs mhi2-vf`). Check there if `debug/` stays empty.

**If `debug/` is empty after jobs run**

1. Rebuild so the image includes current code: `podman-compose up -d --build` (or `podman compose ...`). If logs still look unchanged, force a clean build: `podman-compose build --no-cache` then `podman-compose up -d`.
2. Confirm the running code version (no auth): `curl -sS http://127.0.0.1:8765/healthz` — expect **`app_version`** matching your checkout. If it is missing or wrong, recreate the container after **`podman-compose build --no-cache`**.
3. The first lines of **`podman logs mhi2-vf`** should include **`mhi2-vf: mhi2-video-finder <version> daemon (pre-uvicorn)`** (container starts via `python -u -m mhi2_video_finder.daemon.main`). If not, you are not running the image built from this tree.
4. Confirm the variable inside the container: `podman exec mhi2-vf env | grep MHI2_VF_DEBUG_LOG_PATH`
5. After the pre-uvicorn line you should see **`mhi2-vf[daemon]: NDJSON debug paths`** and **`NDJSON debug active -> ...`** in **`podman logs mhi2-vf 2>&1`** (plain print lines, not only `INFO:` from uvicorn).
6. Watch for `emit_debug_log: could not write` in the same logs if every path fails.
7. Optional: `podman exec mhi2-vf sh -c 'touch /var/lib/mhi2-video-finder/debug/.write-test && ls -la /var/lib/mhi2-video-finder/debug/'`

`/tmp/mhi2-video-finder/debug-runtime.log` only appears after a successful fallback write (when the bind-mounted path is not used or failed first). Prefer checking the path printed after **`NDJSON debug active ->`**.

Do not set `MHI2_VF_DEBUG_LOG_PATH` to an empty value in `.env` (it would override the compose default in some setups).

```bash
podman logs -f mhi2-vf
# or: podman compose logs -f mhi2-vf   /   podman-compose logs -f mhi2-vf

podman compose down
# or: podman-compose down
```

---

Build manually (from repository root):

```bash
podman build -f Containerfile -t mhi2-video-finder-daemon .
```

Create host directories for state and media:

```bash
sudo mkdir -p /var/lib/mhi2-video-finder/state \
  /var/lib/mhi2-video-finder/raw \
  /var/lib/mhi2-video-finder/output
sudo chown -R "$UID:$GID" /var/lib/mhi2-video-finder
```

Run (example):

```bash
podman run -d --name mhi2-vf \
  -p 8765:8765 \
  -e DAEMON_BEARER_TOKEN='choose-a-long-random-secret' \
  -e DAEMON_STATE_DIR=/var/lib/mhi2-video-finder/state \
  -e MHI2_VIDEO_FINDER_CONFIG=/config/config.toml \
  -v /path/to/config.toml:/config/config.toml:ro \
  -v /var/lib/mhi2-video-finder/state:/var/lib/mhi2-video-finder/state:Z \
  -v /var/lib/mhi2-video-finder/raw:/root/.cache/mhi2-video-finder/raw:Z \
  -v /var/lib/mhi2-video-finder/output:/root/Videos/mhi2-video-finder:Z \
  mhi2-video-finder-daemon
```

- Set `raw_cache_dir` and `output_dir` in `config.toml` to match the two media volume mount targets (defaults inside the container are under `/root/...` as above).
- API contract: [docs/daemon-api.yaml](../../docs/daemon-api.yaml) and [docs/daemon-websocket.md](../../docs/daemon-websocket.md).

### systemd (quadlet)

Generate a unit from the container:

```bash
podman generate systemd --new --name mhi2-vf > ~/.config/systemd/user/mhi2-vf.service
systemctl --user daemon-reload
systemctl --user enable --now mhi2-vf.service
```

Adjust volumes and environment in the generated file or use a `.container` quadlet file.
