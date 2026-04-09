"""ffprobe duration and ffmpeg encode progress (stderr time= vs stream DURATION)."""

from __future__ import annotations

import re
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

from video_finder.exceptions import OperationCancelled

# Stream metadata (e.g. "DURATION        : 00:03:54.067000000")
_DURATION_RE = re.compile(
    r"DURATION\s*:\s*(\d+):(\d{2}):(\d{2}\.\d+)",
    re.IGNORECASE,
)
# Encoding stats line (e.g. "time=00:00:35.44 bitrate=...")
_TIME_STAT_RE = re.compile(r"\btime=(\d{1,2}):(\d{2}):(\d{2}\.\d+)\b")


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


def ffprobe_stream_durations_max_ms(path: Path) -> int | None:
    """Largest positive stream ``duration`` from ffprobe (seconds), or None."""
    r = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=duration",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    best: int | None = None
    for line in (r.stdout or "").splitlines():
        part = line.strip()
        if not part or part.upper() == "N/A":
            continue
        try:
            sec = float(part)
            if sec > 0:
                ms = int(sec * 1000)
                if best is None or ms > best:
                    best = ms
        except ValueError:
            continue
    return best


def ffprobe_duration_ms_best(path: Path) -> int | None:
    """Best-effort duration: max of container format and per-stream durations."""
    a = ffprobe_duration_ms(path)
    b = ffprobe_stream_durations_max_ms(path)
    vals = [x for x in (a, b) if x is not None and x > 0]
    return max(vals) if vals else None


def _hms_to_ms(h: str, m: str, s: str) -> int:
    sec = int(h) * 3600 + int(m) * 60 + float(s)
    return int(sec * 1000)


def update_ffmpeg_progress_from_stderr_line(state: dict[str, int], line: str) -> None:
    """Update ``state`` keys ``total_ms`` (max seen) and ``out_ms`` from one ffmpeg stderr line."""
    for m in _DURATION_RE.finditer(line):
        ms = _hms_to_ms(m.group(1), m.group(2), m.group(3))
        prev = state.get("total_ms", 0)
        if ms > prev:
            state["total_ms"] = ms
    m = _TIME_STAT_RE.search(line)
    if m:
        state["out_ms"] = _hms_to_ms(m.group(1), m.group(2), m.group(3))


def last_out_time_ms_from_progress_file(progress_path: Path) -> int | None:
    """Parse ffmpeg -progress output for the last ``out_time_ms`` value (legacy helper)."""
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
    """Legacy: insert ``-nostats -loglevel error -progress <path>`` (prefer stderr-based :func:`run_ffmpeg_with_progress`)."""
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
    """Run ffmpeg; progress from stderr ``time=`` vs stream ``DURATION`` (and ffprobe fallback).

    Matches on-screen ffmpeg stats (same as ``time=`` / metadata duration). Emits at most **99%**
    on success so callers can reserve **100%** for follow-up work (e.g. album art).
    """
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=False,
    )
    stderr_chunks: list[bytes] = []
    state: dict[str, int] = {}
    if duration_ms and duration_ms > 0:
        state["total_ms"] = duration_ms

    lock = threading.Lock()

    def _stderr_reader() -> None:
        if proc.stderr is None:
            return
        try:
            for raw in iter(proc.stderr.readline, b""):
                stderr_chunks.append(raw)
                line = raw.decode("utf-8", errors="replace")
                with lock:
                    update_ffmpeg_progress_from_stderr_line(state, line)
        finally:
            try:
                proc.stderr.close()
            except OSError:
                pass

    reader = threading.Thread(target=_stderr_reader, daemon=True)
    reader.start()

    last_reported = -1.0
    total_hint = duration_ms if duration_ms and duration_ms > 0 else 0
    if total_hint:
        on_progress(0.0)
        last_reported = 0.0
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
            with lock:
                total = state.get("total_ms", 0) or total_hint
                cur = state.get("out_ms", 0)
            if total > 0 and last_reported < 0:
                on_progress(0.0)
                last_reported = 0.0
            if total > 0 and cur >= 0:
                pct = min(99.0, max(0.0, (cur / total) * 100.0))
                if pct > last_reported + 0.2 or rc is not None:
                    on_progress(pct)
                    last_reported = pct
            elif cur > 0 and total <= 0:
                on_progress(None)
            if rc is not None:
                break
            time.sleep(0.1)
    finally:
        rc_final = proc.wait()
        reader.join(timeout=30.0)

    if rc_final != 0:
        err = b"".join(stderr_chunks).decode("utf-8", errors="replace")
        raise subprocess.CalledProcessError(rc_final, cmd, stderr=err.encode())
    on_progress(99.0)
