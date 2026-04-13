#!/usr/bin/env bash
# Recreate the daemon image and container from the repo root context (../../).
# Use this after `git pull` when /healthz is missing app_version or logs look stale.
set -euo pipefail
cd "$(dirname "$0")"

# Rootful Podman: use sudo (set USE_SUDO=0 for rootless podman-compose).
USE_SUDO="${USE_SUDO:-1}"

# Must match services.mhi2-vf.image in compose.yaml (podman-compose tags the build with this).
MHI2_VF_IMAGE="${MHI2_VF_IMAGE:-mhi2-video-finder-daemon}"

# Rootless `podman build` often runs RUN instructions behind slirp4netns; it can fail with:
#   failed to read from slirp4netns sync pipe: EOF
# Using the host network for the build avoids that. podman-compose 1.0.x does not pass compose
# `build.network`, so this script calls `podman build` directly unless PODMAN_BUILD_NETWORK=default.
PODMAN_BUILD_NETWORK="${PODMAN_BUILD_NETWORK:-host}"

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

# python:3.12-slim has no curl; use stdlib for in-container checks.
exec_py() {
  pman exec mhi2-vf python -c "$1"
}

diag() {
  echo "" >&2
  echo "========== Diagnostics ==========" >&2
  echo "Script dir: $(pwd -P)" >&2
  echo "Host healthz (127.0.0.1:${PORT}): $(curl -sS --connect-timeout 2 "http://127.0.0.1:${PORT}/healthz" 2>/dev/null || echo '(failed)')" >&2
  echo "--- podman ps (name mhi2-vf) ---" >&2
  pman ps -a --filter name=mhi2-vf 2>&2 || true
  echo "--- container Config.Cmd (expect python -m mhi2_video_finder.daemon.main) ---" >&2
  pman inspect mhi2-vf --format '{{json .Config.Cmd}}' 2>&2 || true
  echo "--- image ${MHI2_VF_IMAGE} Config.Cmd ---" >&2
  pman inspect "${MHI2_VF_IMAGE}" --format '{{json .Config.Cmd}}' 2>&2 || true
  echo "--- app.py on disk (host) ---" >&2
  grep -n "app_version" ../../src/mhi2_video_finder/daemon/app.py 2>/dev/null | head -3 >&2 \
    || echo "(no match or wrong cwd — run from deploy/podman)" >&2
  echo "--- app_version string inside container /app/src (python read) ---" >&2
  exec_py "
from pathlib import Path
p = Path('/app/src/mhi2_video_finder/daemon/app.py')
t = p.read_text(encoding='utf-8') if p.is_file() else ''
print('file_exists', p.is_file(), 'has_app_version', 'app_version' in t)
" 2>&2 || echo "(podman exec failed)" >&2
  echo "--- healthz from INSIDE container (python urllib) ---" >&2
  exec_py "
import urllib.request
print(urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=3).read().decode())
" 2>&2 || echo "(in-container healthz failed)" >&2
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

ROOT="$(cd ../.. && pwd -P)"
echo "==> Build context (repo root): $ROOT"
if [[ ! -f "$ROOT/Containerfile" ]]; then
  echo "ERROR: missing $ROOT/Containerfile" >&2
  exit 1
fi
if [[ ! -e "$ROOT/Dockerfile" ]]; then
  echo "ERROR: missing $ROOT/Dockerfile (symlink to Containerfile — required for podman-compose 1.0.x)" >&2
  exit 1
fi
if ! grep -q "mhi2_video_finder.daemon.main" "$ROOT/Containerfile"; then
  echo "ERROR: $ROOT/Containerfile does not CMD python -m mhi2_video_finder.daemon.main (git pull?)" >&2
  exit 1
fi
if ! grep -q "app_version" "$ROOT/src/mhi2_video_finder/daemon/app.py"; then
  echo "ERROR: host app.py has no app_version (wrong tree?)" >&2
  exit 1
fi

if [[ "${PRUNE_IMAGE:-0}" == "1" ]]; then
  echo "==> PRUNE_IMAGE=1: removing old image tag(s)"
  pman rmi -f "${MHI2_VF_IMAGE}" 2>/dev/null || true
  pman rmi -f "localhost/${MHI2_VF_IMAGE}:latest" 2>/dev/null || true
fi

echo "==> compose down (USE_SUDO=$USE_SUDO)"
compose down || echo "Note: compose down exited non-zero (ok if stack was already stopped)." >&2

if [[ "$PODMAN_BUILD_NETWORK" == "default" ]]; then
  echo "==> compose build --no-cache (PODMAN_BUILD_NETWORK=default)"
  compose build --no-cache
else
  echo "==> podman build --network=${PODMAN_BUILD_NETWORK} --no-cache (not via podman-compose)"
  echo "    Image: ${MHI2_VF_IMAGE}  Context: ${ROOT}" >&2
  pman build --network="${PODMAN_BUILD_NETWORK}" --no-cache -f "${ROOT}/Dockerfile" -t "${MHI2_VF_IMAGE}" "${ROOT}"
fi

echo "==> Built image CMD (expect python -m ...):" >&2
pman inspect "${MHI2_VF_IMAGE}" --format '{{json .Config.Cmd}}' 2>&2 || true

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

echo "==> In-container healthz (sanity):"
exec_py "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8765/healthz').read().decode())" || true
echo ""
exit 0
