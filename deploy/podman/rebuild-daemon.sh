#!/usr/bin/env bash
# Recreate the daemon image and container from the repo root context (../../).
# Use this after `git pull` when /healthz is missing app_version or logs look stale.
set -euo pipefail
cd "$(dirname "$0")"

COMPOSE="${COMPOSE:-podman-compose}"
if ! command -v "$COMPOSE" >/dev/null 2>&1; then
  echo "Set COMPOSE=podman-compose or COMPOSE='docker compose' (command not found: $COMPOSE)" >&2
  exit 1
fi

echo "==> $COMPOSE down"
$COMPOSE down

echo "==> $COMPOSE build --no-cache"
$COMPOSE build --no-cache

echo "==> $COMPOSE up -d"
$COMPOSE up -d

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
