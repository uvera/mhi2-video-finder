#!/usr/bin/env bash
# Build wheel + sdist under dist/. For AUR metadata aligned with the GitHub tag tarball, use
# scripts/release-github-aur.sh (local tarball hashes differ from GitHub's generated archive).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ -x "$ROOT/.venv/bin/python" ]]; then
	PY="$ROOT/.venv/bin/python"
else
	PY=python3
fi

"$PY" -m pip install -q build setuptools wheel 2>/dev/null || true
"$PY" -m build --wheel --sdist --no-isolation
echo "Built: dist/"
