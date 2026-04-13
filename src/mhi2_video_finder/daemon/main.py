"""CLI entry: run uvicorn for the daemon."""

from __future__ import annotations

import os

from mhi2_video_finder import __version__


def main() -> None:
    import uvicorn

    # Unbuffered marker: if this never appears in `podman logs`, the image is stale.
    print(f"mhi2-vf: mhi2-video-finder {__version__} daemon (pre-uvicorn)", flush=True)

    host = (os.environ.get("DAEMON_HOST") or "127.0.0.1").strip()
    port = int((os.environ.get("DAEMON_PORT") or "8765").strip())
    uvicorn.run(
        "mhi2_video_finder.daemon.app:app",
        host=host,
        port=port,
        factory=False,
        log_level=(os.environ.get("DAEMON_LOG_LEVEL") or "info").lower(),
    )


if __name__ == "__main__":
    main()
