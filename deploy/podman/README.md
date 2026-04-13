# Podman deployment (daemon)

## Podman Compose (recommended)

From **`deploy/podman/`**:

```bash
cd deploy/podman
cp -n config.toml.example config.toml
cp -n .env.example .env
# Edit config.toml and .env (set DAEMON_BEARER_TOKEN)
podman compose up -d --build
```

- **`config.toml`** must exist before `up` (compose mounts it read-only). It sets `raw_cache_dir` and `output_dir` to the in-container paths used by the named volumes in [compose.yaml](compose.yaml).
- **`.env`** holds `DAEMON_BEARER_TOKEN` (gitignored) and is loaded via `env_file` (no compose interpolation); use the same token in the GUI remote settings.
- **`MHI2_VF_PORT`** in `.env` changes the published host port (default `8765`).
- **Debug NDJSON logs** (daemon instrumentation via `emit_debug_log`) are written to **`debug/debug-runtime.log`** next to `compose.yaml` (bind-mounted into the container). Override with `MHI2_VF_DEBUG_LOG_PATH` in compose if you need a different path.

```bash
podman compose logs -f
podman compose down
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
