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

if ! command -v podman-compose >/dev/null 2>&1; then
  echo "podman-compose not found on PATH" >&2
  exit 1
fi
if [[ "$USE_SUDO" == "1" ]] && ! command -v sudo >/dev/null 2>&1; then
  echo "sudo not found; set USE_SUDO=0 for rootless podman" >&2
  exit 1
fi

echo "==> compose down (USE_SUDO=$USE_SUDO)"
compose down

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
for _ in $(seq 1 30); do
  if out=$(curl -sS --connect-timeout 1 "http://127.0.0.1:${PORT}/healthz" 2>/dev/null); then
    echo "$out"
    if echo "$out" | grep -q app_version; then
      echo "OK: healthz includes app_version (container matches current image)."
      exit 0
    fi
    echo "WARN: healthz responded but has no app_version — wrong image or old layer." >&2
    exit 1
  fi
  sleep 1
done
echo "ERROR: healthz did not become ready." >&2
exit 1
