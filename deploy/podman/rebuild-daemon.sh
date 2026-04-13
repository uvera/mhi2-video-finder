#!/usr/bin/env bash
# Recreate the daemon image and container from the repo root context (../../).
# Use this after `git pull` when /healthz is missing app_version or logs look stale.
set -euo pipefail
cd "$(dirname "$0")"

# Rootful Podman: use sudo (set USE_SUDO=0 for rootless podman-compose).
USE_SUDO="${USE_SUDO:-1}"

compose() {
  if [[ "$USE_SUDO" == "1" ]]; then
    sudo podman-compose "$@"
  else
    podman-compose "$@"
  fi
}

pman() {
  if [[ "$USE_SUDO" == "1" ]]; then
    sudo podman "$@"
  else
    podman "$@"
  fi
}

diag() {
  echo "" >&2
  echo "========== Diagnostics ==========" >&2
  echo "Script dir: $(pwd -P)" >&2
  echo "Host healthz (127.0.0.1:${PORT}): $(curl -sS --connect-timeout 2 "http://127.0.0.1:${PORT}/healthz" 2>/dev/null || echo '(failed)')" >&2
  echo "--- podman ps (name mhi2-vf) ---" >&2
  pman ps -a --filter name=mhi2-vf 2>&2 || true
  echo "--- image for container mhi2-vf ---" >&2
  pman inspect mhi2-vf --format 'ImageID={{.Image}} Created={{.Created}}' 2>&2 || echo "(no container mhi2-vf)" >&2
  echo "--- app.py on disk (host) has app_version? ---" >&2
  grep -n "app_version" ../../src/mhi2_video_finder/daemon/app.py 2>/dev/null | head -3 >&2 \
    || echo "(no match or wrong cwd — must run from deploy/podman)" >&2
  echo "--- same check inside container /app/src ---" >&2
  pman exec mhi2-vf grep -n "app_version" /app/src/mhi2_video_finder/daemon/app.py 2>&2 || echo "(podman exec or grep failed)" >&2
  echo "--- healthz from INSIDE container (bypasses host port confusion) ---" >&2
  pman exec mhi2-vf curl -sS --connect-timeout 2 http://127.0.0.1:8765/healthz 2>&2 || echo "(curl inside container failed)" >&2
  echo "--- NDJSON debug dir in container ---" >&2
  pman exec mhi2-vf ls -la /var/lib/mhi2-video-finder/debug 2>&2 || true
  echo "=================================" >&2
}

if ! command -v podman-compose >/dev/null 2>&1; then
  echo "podman-compose not found on PATH" >&2
  exit 1
fi
if [[ "$USE_SUDO" == "1" ]] && ! command -v sudo >/dev/null 2>&1; then
  echo "sudo not found; set USE_SUDO=0 for rootless podman" >&2
  exit 1
fi

echo "==> compose down (USE_SUDO=$USE_SUDO)"
compose down || echo "Note: compose down exited non-zero (ok if stack was already stopped)." >&2

echo "==> compose build --no-cache"
compose build --no-cache

echo "==> compose up -d"
compose up -d

PORT=8765
if [[ -f .env ]]; then
  v=$(grep -E '^MHI2_VF_PORT=' .env | tail -1 | cut -d= -f2- | tr -d ' "' || true)
  [[ -n "${v:-}" ]] && PORT="$v"
fi

echo "==> Waiting for healthz on port $PORT ..."
ok=0
for _ in $(seq 1 40); do
  if out=$(curl -sS --connect-timeout 1 "http://127.0.0.1:${PORT}/healthz" 2>/dev/null); then
    echo "$out"
    if echo "$out" | grep -q app_version; then
      echo "OK: healthz includes app_version."
      ok=1
      break
    fi
    echo "WARN: healthz replied but no app_version: $out" >&2
    diag
    exit 1
  fi
  sleep 1
done

if [[ "$ok" != "1" ]]; then
  echo "ERROR: healthz did not become ready on port $PORT." >&2
  diag
  exit 1
fi

echo "==> Optional: in-container healthz"
pman exec mhi2-vf curl -sS http://127.0.0.1:8765/healthz || true
echo ""
exit 0
