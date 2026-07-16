"""Entry point for the PyQt6 UI."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path


def _configure_logging(verbose: bool) -> None:
    env = (os.environ.get("MHI2_VIDEO_FINDER_REMOTE_DEBUG") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    if not verbose and not env:
        return
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
        force=True,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("httpcore.connection").setLevel(logging.WARNING)
    logging.getLogger("httpcore.http11").setLevel(logging.WARNING)


def main() -> None:
    try:
        from PyQt6.QtWidgets import QApplication
    except ImportError:
        print(
            "PyQt6 is required for the UI. Install with: pip install 'mhi2-video-finder[gui]'",
            file=sys.stderr,
        )
        sys.exit(1)

    from mhi2_video_finder.ui.window import MainWindow

    p = argparse.ArgumentParser(description="mhi2-video-finder graphical UI")
    p.add_argument("--config", type=Path, default=None, help="Path to config.toml")
    p.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Log remote HTTP/WebSocket diagnostics to stderr (or set MHI2_VIDEO_FINDER_REMOTE_DEBUG=1).",
    )
    args = p.parse_args()

    _configure_logging(args.verbose)

    app = QApplication(sys.argv)
    win = MainWindow(config_path=args.config)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
