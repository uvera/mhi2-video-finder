#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
	echo "Usage: scripts/bump-version.sh <major|minor|patch>" >&2
	exit 1
fi

KIND="$1"
if [[ "$KIND" != "major" && "$KIND" != "minor" && "$KIND" != "patch" ]]; then
	echo "Invalid bump type: $KIND (expected: major, minor, patch)" >&2
	exit 1
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ -x "$ROOT/.venv/bin/python" ]]; then
	PY="$ROOT/.venv/bin/python"
else
	PY=python3
fi

"$PY" - "$KIND" "$ROOT" <<'PY'
from __future__ import annotations

import re
import sys
from pathlib import Path


def parse_semver(value: str) -> tuple[int, int, int]:
    m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", value.strip())
    if not m:
        raise SystemExit(f"Unsupported version format: {value!r} (expected X.Y.Z)")
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def format_semver(parts: tuple[int, int, int]) -> str:
    return f"{parts[0]}.{parts[1]}.{parts[2]}"


def bump(parts: tuple[int, int, int], kind: str) -> tuple[int, int, int]:
    major, minor, patch = parts
    if kind == "major":
        return major + 1, 0, 0
    if kind == "minor":
        return major, minor + 1, 0
    return major, minor, patch + 1


def replace_one(text: str, pattern: str, repl: str, file_label: str) -> str:
    updated, count = re.subn(pattern, repl, text, count=1, flags=re.M)
    if count != 1:
        raise SystemExit(f"Could not update version in {file_label}")
    return updated


kind = sys.argv[1]
root = Path(sys.argv[2])

paths = {
    "pyproject": root / "pyproject.toml",
    "init": root / "src" / "mhi2_video_finder" / "__init__.py",
    "root_pkgbuild": root / "PKGBUILD",
    "pacman": root / "pacman" / "PKGBUILD",
}

pyproject_text = paths["pyproject"].read_text()
init_text = paths["init"].read_text()
root_pkgbuild_text = paths["root_pkgbuild"].read_text()
pacman_text = paths["pacman"].read_text()

pyproject_match = re.search(r'^version\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"\s*$', pyproject_text, flags=re.M)
init_match = re.search(r'^__version__\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"\s*$', init_text, flags=re.M)
root_pkgbuild_match = re.search(r"^pkgver=([0-9]+\.[0-9]+\.[0-9]+)\s*$", root_pkgbuild_text, flags=re.M)
pacman_match = re.search(r"^pkgver=([0-9]+\.[0-9]+\.[0-9]+)\s*$", pacman_text, flags=re.M)

if not pyproject_match or not init_match or not root_pkgbuild_match or not pacman_match:
    raise SystemExit("Failed to read current versions from one or more files")

current_versions = {
    "pyproject.toml": pyproject_match.group(1),
    "src/mhi2_video_finder/__init__.py": init_match.group(1),
    "PKGBUILD": root_pkgbuild_match.group(1),
    "pacman/PKGBUILD": pacman_match.group(1),
}

if len(set(current_versions.values())) != 1:
    detail = ", ".join(f"{k}={v}" for k, v in current_versions.items())
    raise SystemExit(f"Version mismatch across files: {detail}")

old_version = current_versions["pyproject.toml"]
new_version = format_semver(bump(parse_semver(old_version), kind))

pyproject_text = replace_one(
    pyproject_text,
    r'^version\s*=\s*"[0-9]+\.[0-9]+\.[0-9]+"\s*$',
    f'version = "{new_version}"',
    "pyproject.toml",
)
init_text = replace_one(
    init_text,
    r'^__version__\s*=\s*"[0-9]+\.[0-9]+\.[0-9]+"\s*$',
    f'__version__ = "{new_version}"',
    "src/mhi2_video_finder/__init__.py",
)
root_pkgbuild_text = replace_one(
    root_pkgbuild_text,
    r"^pkgver=[0-9]+\.[0-9]+\.[0-9]+\s*$",
    f"pkgver={new_version}",
    "PKGBUILD",
)
pacman_text = replace_one(
    pacman_text,
    r"^pkgver=[0-9]+\.[0-9]+\.[0-9]+\s*$",
    f"pkgver={new_version}",
    "pacman/PKGBUILD",
)

paths["pyproject"].write_text(pyproject_text)
paths["init"].write_text(init_text)
paths["root_pkgbuild"].write_text(root_pkgbuild_text)
paths["pacman"].write_text(pacman_text)

print(f"Bumped version ({kind}): {old_version} -> {new_version}")
PY
