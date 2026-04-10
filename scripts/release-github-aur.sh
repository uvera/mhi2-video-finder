#!/usr/bin/env bash
# Build wheel+sdist, push git tag v<version>, fetch GitHub source tarball SHA-256, refresh
# aur/PKGBUILD + aur/.SRCINFO, commit/push AUR metadata when it changes, create GitHub release.
# Requires: git, gh (authenticated), curl, sha256sum, Python build deps (see scripts/package-release.sh).
# Regenerates .SRCINFO with makepkg or Docker.
#
# The tag may be force-moved to the AUR checksum commit; keep aur/ in .gitattributes export-ignore
# so the GitHub archive content/hash does not change when only aur/ metadata is updated.
#
# Environment (optional):
#   SKIP_BUILD=1          — skip python -m build (wheel + sdist)
#   SKIP_TAG_PUSH=1       — do not create/push tag
#   SKIP_AUR_REFRESH=1    — only tag push / release
#   SKIP_GH_RELEASE=1     — do not create GitHub release
#   ALLOW_DIRTY=1         — allow uncommitted changes
#   GITHUB_REPOSITORY=owner/repo — override GitHub slug
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ -x "$ROOT/.venv/bin/python" ]]; then
	PY="$ROOT/.venv/bin/python"
else
	PY=python3
fi

VERSION="$("$PY" -c "import tomllib; print(tomllib.load(open('pyproject.toml', 'rb'))['project']['version'])")"
TAG="v${VERSION}"

resolve_github_slug() {
	if [[ -n "${GITHUB_REPOSITORY:-}" ]]; then
		echo "${GITHUB_REPOSITORY}"
		return
	fi
	if command -v gh >/dev/null 2>&1; then
		local o
		o="$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null || true)"
		if [[ -n "$o" ]]; then
			echo "$o"
			return
		fi
	fi
	local origin
	origin="$(git remote get-url origin 2>/dev/null || true)"
	if [[ "$origin" =~ github\.com[:/]([^/]+)/([^/.]+)(\.git)?$ ]]; then
		echo "${BASH_REMATCH[1]}/${BASH_REMATCH[2]}"
		return
	fi
	echo ""
}

die() {
	echo "release-github-aur: $*" >&2
	exit 1
}

regenerate_srcinfo() {
	local aurdir="$ROOT/aur"
	if command -v makepkg >/dev/null 2>&1; then
		(cd "$aurdir" && makepkg --printsrcinfo >.SRCINFO)
		return 0
	fi
	if command -v docker >/dev/null 2>&1; then
		echo "makepkg not found; regenerating .SRCINFO via Docker (archlinux)…"
		docker run --rm \
			-v "${aurdir}:/aur:rw" \
			-w /aur \
			archlinux/archlinux:latest \
			bash -lc 'pacman-key --init >/dev/null 2>&1 || true; pacman -Sy --noconfirm archlinux-keyring pacman >/dev/null && makepkg --printsrcinfo' \
			>"${aurdir}/.SRCINFO.new" && mv -f "${aurdir}/.SRCINFO.new" "${aurdir}/.SRCINFO"
		return 0
	fi
	die "Need makepkg (Arch) or Docker to regenerate aur/.SRCINFO."
}

refresh_aur_pkgbuild() {
	local slug="$1" sum="$2"

	local old_pkgver pkgrel
	old_pkgver="$(grep -m1 '^pkgver=' "$ROOT/aur/PKGBUILD" | sed -E "s/^pkgver=([0-9.]+).*/\1/")"
	if [[ "$old_pkgver" == "$VERSION" ]]; then
		pkgrel="$(grep -m1 '^pkgrel=' "$ROOT/aur/PKGBUILD" | sed -E 's/^pkgrel=([0-9]+).*/\1/')"
		pkgrel=$((pkgrel + 1))
	else
		pkgrel=1
	fi

	ROOT="$ROOT" SLUG="$slug" SUM="$sum" VERSION="$VERSION" TAG="$TAG" PKGREL="$pkgrel" "$PY" <<'PY'
import os
import re
from pathlib import Path

root = Path(os.environ["ROOT"])
slug = os.environ["SLUG"]
owner, _, repo = slug.partition("/")
if not repo:
    raise SystemExit("bad SLUG")
url = f"https://github.com/{owner}/{repo}"
archive = f"{url}/archive/refs/tags/{os.environ['TAG']}.tar.gz"
ver = os.environ["VERSION"]
pkgrel = os.environ["PKGREL"]
sumh = os.environ["SUM"]
pb = root / "aur" / "PKGBUILD"
text = pb.read_text()
text = re.sub(r"^url=.*$", f"url='{url}'", text, count=1, flags=re.M)
text = re.sub(r"^pkgver=.*$", f"pkgver={ver}", text, count=1, flags=re.M)
text = re.sub(r"^pkgrel=.*$", f"pkgrel={pkgrel}", text, count=1, flags=re.M)
src = 'source=("${pkgname}-${pkgver}.tar.gz::%s")' % archive
text = re.sub(r"^source=\(.*\)$", src, text, count=1, flags=re.M)
text = re.sub(
    r"^sha256sums=\([^)]*\).*$",
    f"sha256sums=('{sumh}')",
    text,
    count=1,
    flags=re.M,
)
pb.write_text(text)
PY

	regenerate_srcinfo
	echo "Updated aur/PKGBUILD and aur/.SRCINFO (pkgrel=${pkgrel}, sha256=${sum})."
}

if ! command -v gh >/dev/null 2>&1; then
	die "install GitHub CLI (gh) and run: gh auth login"
fi
gh auth status >/dev/null 2>&1 || die "gh is not authenticated (gh auth login)"

SLUG="$(resolve_github_slug)"
[[ -n "$SLUG" ]] || die "could not resolve owner/repo; set GITHUB_REPOSITORY=owner/repo"

if [[ "${ALLOW_DIRTY:-0}" != "1" ]] && [[ -n "$(git status --porcelain)" ]]; then
	die "working tree is dirty; commit or stash, or set ALLOW_DIRTY=1"
fi

if [[ "${SKIP_AUR_REFRESH:-0}" != "1" ]]; then
	if git archive --format=tar HEAD 2>/dev/null | tar tf - | grep -qE '^aur/'; then
		die "aur/ is included in git archive — add 'aur/ export-ignore' to .gitattributes"
	fi
fi

if [[ "${SKIP_BUILD:-0}" != "1" ]]; then
	echo "Building wheel and sdist…"
	bash "$ROOT/scripts/package-release.sh"
fi

if [[ "${SKIP_TAG_PUSH:-0}" != "1" ]]; then
	if git rev-parse -q --verify "refs/tags/${TAG}" >/dev/null; then
		echo "Tag ${TAG} already exists locally."
	else
		git tag "${TAG}"
		echo "Created tag ${TAG}."
	fi
	git push origin "${TAG}"
	echo "Pushed ${TAG} to origin."
	sleep 2
fi

if [[ "${SKIP_AUR_REFRESH:-0}" != "1" ]]; then
	ARCHIVE_URL="https://github.com/${SLUG}/archive/refs/tags/${TAG}.tar.gz"
	SUM="$(curl -fsSL "$ARCHIVE_URL" | sha256sum | awk '{print $1}')"
	[[ -n "$SUM" ]] || die "empty sha256 (fetch failed: is ${TAG} on GitHub?)"
	refresh_aur_pkgbuild "$SLUG" "$SUM"

	if [[ -n "$(git status --porcelain aur/PKGBUILD aur/.SRCINFO 2>/dev/null || true)" ]]; then
		git add aur/PKGBUILD aur/.SRCINFO
		git commit -m "aur: source checksum for ${TAG}"
		BR="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo HEAD)"
		if [[ "${SKIP_TAG_PUSH:-0}" != "1" ]]; then
			if [[ "$BR" != "HEAD" ]]; then
				git push origin "$BR"
			fi
			git tag -f "${TAG}"
			git push -f origin "refs/tags/${TAG}"
			echo "Committed AUR metadata and moved tag ${TAG} to this commit."
			sleep 2
		else
			echo "Committed AUR metadata locally (SKIP_TAG_PUSH=1: push branch/tag yourself)."
		fi
	fi
fi

if [[ "${SKIP_GH_RELEASE:-0}" != "1" ]]; then
	if gh release view "${TAG}" >/dev/null 2>&1; then
		echo "Release ${TAG} already exists."
	else
		gh release create "${TAG}" --title "mhi2-video-finder ${VERSION}" --generate-notes
		echo "Published GitHub release ${TAG}."
	fi
fi

echo ""
echo "Done."
if [[ "${SKIP_AUR_REFRESH:-0}" != "1" ]]; then
	echo "AUR: copy aur/PKGBUILD and aur/.SRCINFO into ssh://aur@aur.archlinux.org/mhi2-video-finder.git (or your package name), then push."
fi
