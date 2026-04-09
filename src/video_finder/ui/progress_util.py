"""Map yt-dlp progress dicts to UI-friendly numbers/strings."""

from __future__ import annotations

from typing import Any


def format_bytes(n: float | int | None) -> str:
    if n is None:
        return ""
    x = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if x < 1024.0:
            return f"{x:.1f} {unit}" if unit != "B" else f"{int(x)} {unit}"
        x /= 1024.0
    return f"{x:.1f} TiB"


def ytdlp_progress_percent_and_labels(d: dict[str, Any]) -> tuple[float, str, str]:
    """Return (percent 0..100 or -1 if unknown, speed label, eta label)."""
    status = d.get("status")
    if status == "finished":
        return 100.0, "", ""
    if status != "downloading":
        return -1.0, "", ""

    total = d.get("total_bytes") or d.get("total_bytes_estimate")
    done = d.get("downloaded_bytes") or 0
    if total and int(total) > 0:
        pct = min(100.0, max(0.0, 100.0 * float(done) / float(total)))
    else:
        pct = -1.0

    speed = d.get("speed")
    speed_s = f"{format_bytes(speed)}/s" if speed else ""

    eta = d.get("eta")
    if eta is not None and isinstance(eta, int | float) and eta >= 0:
        m, s = divmod(int(eta), 60)
        h, m = divmod(m, 60)
        if h:
            eta_s = f"{h:d}:{m:02d}:{s:02d}"
        else:
            eta_s = f"{m:d}:{s:02d}"
    else:
        eta_s = ""

    return pct, speed_s, eta_s
