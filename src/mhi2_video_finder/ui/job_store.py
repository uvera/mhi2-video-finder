"""SQLite persistence for GUI download/convert jobs (resume after restart)."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from mhi2_video_finder.search import VideoCandidate

from .models import UiJob


def default_job_db_path() -> Path:
    base = os.environ.get("XDG_CACHE_HOME")
    if base:
        p = Path(base).expanduser().resolve() / "mhi2-video-finder"
    else:
        p = Path.home() / ".cache" / "mhi2-video-finder"
    p.mkdir(parents=True, exist_ok=True)
    return p / "jobs.sqlite"


class JobStore:
    """Stores incomplete jobs so the UI can restore them after an app restart."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_job_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def close(self) -> None:
        self._conn.close()

    def _init_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                seq INTEGER NOT NULL,
                video_id TEXT NOT NULL,
                title TEXT NOT NULL,
                channel TEXT NOT NULL,
                url TEXT NOT NULL,
                duration INTEGER,
                out_path TEXT NOT NULL,
                raw_path TEXT,
                download_status TEXT NOT NULL,
                convert_status TEXT NOT NULL,
                download_error TEXT,
                convert_error TEXT,
                no_embed INTEGER NOT NULL DEFAULT 0,
                ytdlp_info_json TEXT,
                updated_at REAL NOT NULL
            )
            """
        )
        self._conn.commit()

    def upsert(self, job: UiJob, seq: int) -> None:
        if job.convert_status == "done" and job.download_status == "done":
            self.delete(job.job_id)
            return
        yjson: str | None
        if job.ytdlp_info is None:
            yjson = None
        else:
            try:
                yjson = json.dumps(job.ytdlp_info, default=str, ensure_ascii=False)
            except (TypeError, ValueError):
                yjson = None
        now = time.time()
        self._conn.execute(
            """
            INSERT INTO jobs (
                job_id, seq, video_id, title, channel, url, duration, out_path, raw_path,
                download_status, convert_status, download_error, convert_error,
                no_embed, ytdlp_info_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                seq = excluded.seq,
                video_id = excluded.video_id,
                title = excluded.title,
                channel = excluded.channel,
                url = excluded.url,
                duration = excluded.duration,
                out_path = excluded.out_path,
                raw_path = excluded.raw_path,
                download_status = excluded.download_status,
                convert_status = excluded.convert_status,
                download_error = excluded.download_error,
                convert_error = excluded.convert_error,
                no_embed = excluded.no_embed,
                ytdlp_info_json = excluded.ytdlp_info_json,
                updated_at = excluded.updated_at
            """,
            (
                job.job_id,
                seq,
                job.candidate.video_id,
                job.candidate.title,
                job.candidate.channel,
                job.candidate.url,
                job.candidate.duration,
                str(job.out_path),
                str(job.raw_path) if job.raw_path else None,
                job.download_status,
                job.convert_status,
                job.download_error or None,
                job.convert_error or None,
                1 if job.no_embed else 0,
                yjson,
                now,
            ),
        )
        self._conn.commit()

    def delete(self, job_id: str) -> None:
        self._conn.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
        self._conn.commit()

    def prune_not_in(self, keep_ids: set[str]) -> None:
        """Remove DB rows for jobs no longer in memory (e.g. cleared session)."""
        if not keep_ids:
            self._conn.execute("DELETE FROM jobs")
        else:
            ph = ",".join("?" * len(keep_ids))
            self._conn.execute(f"DELETE FROM jobs WHERE job_id NOT IN ({ph})", tuple(keep_ids))
        self._conn.commit()

    def load_all(self) -> list[UiJob]:
        cur = self._conn.execute("SELECT * FROM jobs ORDER BY seq ASC, job_id ASC")
        out: list[UiJob] = []
        for row in cur.fetchall():
            yinfo: dict[str, Any] | None = None
            raw_j = row["ytdlp_info_json"]
            if raw_j:
                try:
                    yinfo = json.loads(raw_j)
                except json.JSONDecodeError:
                    yinfo = None
            cand = VideoCandidate(
                video_id=row["video_id"],
                title=row["title"],
                duration=row["duration"],
                channel=row["channel"],
                url=row["url"],
            )
            rp = Path(row["raw_path"]) if row["raw_path"] else None
            job = UiJob(
                job_id=row["job_id"],
                candidate=cand,
                out_path=Path(row["out_path"]),
                download_status=row["download_status"],
                convert_status=row["convert_status"],
                download_error=row["download_error"] or "",
                convert_error=row["convert_error"] or "",
                raw_path=rp,
                ytdlp_info=yinfo,
                no_embed=bool(row["no_embed"]),
            )
            out.append(job)
        return out
