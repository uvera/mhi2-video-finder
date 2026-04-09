"""ffprobe duration and ffmpeg encode progress (via -progress file)."""

from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

from video_finder.exceptions import OperationCancelled


def ffprobe_duration_ms(path: Path) -> int | None:
    """Return container duration in milliseconds, or None if unknown."""
    r = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        return None
    try:
        sec = float((r.stdout or "").strip())
        if sec <= 0:
            return None
        return int(sec * 1000)
    except ValueError:
        return None


def last_out_time_ms_from_progress_file(progress_path: Path) -> int | None:
    """Parse ffmpeg -progress output for the last ``out_time_ms`` value."""
    if not progress_path.is_file():
        return None
    try:
        text = progress_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    last: int | None = None
    for line in text.splitlines():
        if line.startswith("out_time_ms="):
            try:
                last = int(line.split("=", 1)[1].strip())
            except ValueError:
                continue
    return last


def insert_ffmpeg_progress_args(cmd: list[str], progress_path: Path) -> list[str]:
    """Insert ``-nostats -loglevel error -progress <path>`` after ``ffmpeg -y``."""
    if len(cmd) < 2 or cmd[0] != "ffmpeg":
        raise ValueError("expected ffmpeg command list")
    extra = ["-nostats", "-loglevel", "error", "-progress", str(progress_path)]
    if cmd[1] == "-y":
        return [cmd[0], cmd[1], *extra, *cmd[2:]]
    return [cmd[0], *extra, *cmd[1:]]


def run_ffmpeg_with_progress(
    cmd: list[str],
    *,
    duration_ms: int | None,
    on_progress: Callable[[float | None], None],
    should_cancel: Callable[[], bool] | None = None,
) -> None:
    """Run ffmpeg, calling ``on_progress`` with percent 0..99 during encode, or ``None`` if duration unknown.

    Emits at most **99%** on success so callers can reserve **100%** for any follow-up work (e.g. album art).
    """
    fd, progress_tmp = tempfile.mkstemp(suffix=".ffprog")
    os.close(fd)
    progress_path = Path(progress_tmp)
    try:
        cmd2 = insert_ffmpeg_progress_args(cmd, progress_path)
        proc = subprocess.Popen(
            cmd2,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=False,
        )
        stderr_chunks: list[bytes] = []

        def _drain_stderr() -> None:
            if proc.stderr:
                stderr_chunks.append(proc.stderr.read())

        threading.Thread(target=_drain_stderr, daemon=True).start()

        last_reported = -1.0
        if duration_ms and duration_ms > 0:
            on_progress(0.0)
        try:
            while True:
                if should_cancel and should_cancel():
                    proc.terminate()
                    try:
                        proc.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=4)
                    raise OperationCancelled()
                rc = proc.poll()
                ot = last_out_time_ms_from_progress_file(progress_path)
                if duration_ms and duration_ms > 0 and ot is not None:
                    pct = min(99.0, max(0.0, (ot / duration_ms) * 100.0))
                    if pct > last_reported + 0.25 or rc is not None:
                        on_progress(pct)
                        last_reported = pct
                elif ot is not None and (duration_ms is None or duration_ms <= 0):
                    on_progress(None)
                if rc is not None:
                    break
                time.sleep(0.12)
        finally:
            rc_final = proc.wait()
        if rc_final != 0:
            err = b"".join(stderr_chunks).decode("utf-8", errors="replace")
            raise subprocess.CalledProcessError(rc_final, cmd2, stderr=err.encode())
        on_progress(99.0)
    finally:
        progress_path.unlink(missing_ok=True)
