pkgname=mhi2-video-finder
pkgver=0.14.2
pkgrel=1
pkgdesc="YouTube music-video search, download (yt-dlp), and MHI2-oriented MP4 transcode"
arch=('any')
url='https://github.com/uvera/mhi2-video-finder'
license=('MIT')
depends=('python' 'ffmpeg' 'python-pyqt6' 'python-httpx' 'python-typer' 'yt-dlp')
makedepends=('python-build' 'python-installer' 'python-setuptools' 'python-wheel')
install=mhi2-video-finder.install
_workdir="${startdir}/.makepkg/${pkgname}-${pkgver}"

prepare() {
	rm -rf "${_workdir}"
	mkdir -p "${_workdir}"
	cp -a "${startdir}/pyproject.toml" \
		"${startdir}/README.md" \
		"${startdir}/src" \
		"${startdir}/mhi2-video-finder.desktop" \
		"${_workdir}/"
}

build() {
	cd "${_workdir}"
	# Use system Python so a project .venv on PATH is not picked up.
	/usr/bin/python -m build --wheel --no-isolation
}

package() {
	cd "${_workdir}"
	/usr/bin/python -m installer --destdir="$pkgdir" --prefix=/usr dist/*.whl
}
