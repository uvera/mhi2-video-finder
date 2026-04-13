"""Shared NDJSON debug logger used by runtime instrumentation."""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from tempfile import gettempdir
from typing import Any


def _session_id() -> str | None:
    raw = (os.environ.get("MHI2_VF_DEBUG_SESSION_ID") or os.environ.get("X_DEBUG_SESSION_ID") or "").strip()
    return raw or None


def _run_id() -> str:
    raw = (os.environ.get("MHI2_VF_DEBUG_RUN_ID") or "").strip()
    return raw or "pre-fix"


def _default_log_name() -> str:
    session = _session_id()
    if session:
        return f"debug-{session}.log"
    return "debug-runtime.log"


def _fallback_log_paths(name: str) -> list[Path]:
    xdg_state = (os.environ.get("XDG_STATE_HOME") or "").strip()
    user_state = Path(xdg_state).expanduser().resolve() if xdg_state else (Path.home() / ".local" / "state")
    return [
        (user_state / "mhi2-video-finder" / "debug" / name).resolve(),
        (Path(gettempdir()) / "mhi2-video-finder" / name).resolve(),
    ]


def _candidate_log_paths() -> list[Path]:
    """Prefer MHI2_VF_DEBUG_LOG_PATH, then state dir, then tempdir (deduped)."""
    name = _default_log_name()
    seen: set[str] = set()
    out: list[Path] = []
    explicit = (os.environ.get("MHI2_VF_DEBUG_LOG_PATH") or "").strip()
    if explicit:
        p = Path(explicit).expanduser().resolve()
        k = str(p)
        if k not in seen:
            seen.add(k)
            out.append(p)
    for p in _fallback_log_paths(name):
        k = str(p)
        if k not in seen:
            seen.add(k)
            out.append(p)
    return out


def debug_log_candidate_paths() -> list[Path]:
    """Resolved paths tried for NDJSON logs (for diagnostics)."""
    return _candidate_log_paths()


def log_daemon_visible(message: str, *, level: int = logging.INFO) -> None:
    """Write to stderr and to uvicorn's logger (shows in podman/docker logs reliably)."""
    line = message.rstrip("\n")
    try:
        sys.stderr.write(line + "\n")
        sys.stderr.flush()
    except Exception:
        pass
    try:
        logging.getLogger("uvicorn.error").log(level, "%s", line)
    except Exception:
        pass


def debug_startup_stderr_banner(component: str = "daemon") -> None:
    """Announce NDJSON paths at startup (stderr + uvicorn INFO)."""
    try:
        paths = _candidate_log_paths()
        explicit = (os.environ.get("MHI2_VF_DEBUG_LOG_PATH") or "").strip()
        msg = (
            f"mhi2-vf[{component}]: NDJSON debug paths (explicit={explicit!r}): "
            + " -> ".join(str(p) for p in paths)
        )
        log_daemon_visible(msg)
    except Exception:
        pass


def emit_debug_log(hypothesis_id: str, location: str, message: str, data: dict[str, Any]) -> Path | None:
    payload: dict[str, Any] = {
        "runId": _run_id(),
        "hypothesisId": hypothesis_id,
        "location": location,
        "message": message,
        "data": data,
        "timestamp": int(time.time() * 1000),
    }
    session = _session_id()
    if session:
        payload["sessionId"] = session
    line = json.dumps(payload, ensure_ascii=False, default=str) + "\n"
    errors: list[str] = []
    for path in _candidate_log_paths():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
            return path
        except OSError as e:
            errors.append(f"{path}: {e}")
        except Exception as e:
            errors.append(f"{path}: {e!r}")
    msg = "mhi2-vf emit_debug_log: could not write to any path; tried:\n  " + "\n  ".join(errors)
    log_daemon_visible(msg, level=logging.ERROR)
    return None
