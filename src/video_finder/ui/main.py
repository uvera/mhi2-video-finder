"""Entry point for the PyQt6 UI."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> None:
    try:
        from PyQt6.QtWidgets import QApplication
    except ImportError:
        print("PyQt6 is required for the UI. Install with: pip install 'video-finder[gui]'", file=sys.stderr)
        sys.exit(1)

    from video_finder.ui.window import MainWindow

    p = argparse.ArgumentParser(description="video-finder graphical UI")
    p.add_argument("--config", type=Path, default=None, help="Path to config.toml")
    args = p.parse_args()

    app = QApplication(sys.argv)
    win = MainWindow(config_path=args.config)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
