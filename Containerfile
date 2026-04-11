# Podman / Docker image for mhi2-video-finder-daemon
FROM docker.io/library/python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md /app/
COPY src /app/src

RUN pip install --no-cache-dir -e ".[daemon]"

ENV DAEMON_HOST=0.0.0.0
ENV DAEMON_PORT=8765
# Persist job DB + media on volumes (see deploy/podman/README.md)
ENV DAEMON_STATE_DIR=/var/lib/mhi2-video-finder/state

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/healthz').read()" || exit 1

CMD ["mhi2-video-finder-daemon"]
