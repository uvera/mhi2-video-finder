# Podman / Docker image for mhi2-video-finder-daemon
# Root `Dockerfile` is a symlink to this file (podman-compose 1.0.x requires "Dockerfile" in build context).
FROM docker.io/library/python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin appuser

WORKDIR /app
COPY pyproject.toml README.md /app/
COPY src /app/src

RUN pip install --no-cache-dir -e ".[daemon]"

ENV DAEMON_HOST=0.0.0.0
ENV DAEMON_PORT=8765
# Persist job DB + media on volumes (see deploy/podman/README.md)
ENV DAEMON_STATE_DIR=/var/lib/mhi2-video-finder/state

# Pre-create volume mount points owned by the non-root user: Podman/Docker
# initialize a fresh named volume from the image directory it's mounted over
# (ownership included), so this is what makes the daemon able to write to
# state/cache/output volumes without running as root.
RUN mkdir -p /var/lib/mhi2-video-finder/state /var/lib/mhi2-video-finder/debug \
        /home/appuser/.cache/mhi2-video-finder/raw /home/appuser/Videos/mhi2-video-finder \
    && chown -R appuser:appuser /var/lib/mhi2-video-finder /home/appuser

USER appuser

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/healthz').read()" || exit 1

# python -m avoids setuptools console-script wrapper (stdio/logging quirks in some setups).
CMD ["python", "-u", "-m", "mhi2_video_finder.daemon.main"]
