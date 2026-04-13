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


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    cur = conn.execute(f"PRAGMA table_info({table})")
    return {str(r[1]) for r in cur.fetchall()}


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
        cols = _table_columns(self._conn, "jobs")
        if "backend" not in cols:
            self._conn.execute("ALTER TABLE jobs ADD COLUMN backend TEXT NOT NULL DEFAULT 'local'")
        if "remote_saved" not in cols:
            self._conn.execute("ALTER TABLE jobs ADD COLUMN remote_saved INTEGER NOT NULL DEFAULT 1")
        if "remote_job_id" not in cols:
            self._conn.execute("ALTER TABLE jobs ADD COLUMN remote_job_id TEXT")
        if "remote_job_origin" not in cols:
            self._conn.execute(
                "ALTER TABLE jobs ADD COLUMN remote_job_origin TEXT NOT NULL DEFAULT 'desktop'"
            )
        self._conn.commit()

    def upsert(self, job: UiJob, seq: int) -> None:
        self._upsert_no_commit(job, seq)
        self._conn.commit()

    def _upsert_no_commit(self, job: UiJob, seq: int) -> None:
        if job.download_status == "done" and job.convert_status == "done":
            if job.backend == "remote":
                # Keep rows until the user removes them (pending or completed Save to PC).
                pass
            else:
                self._conn.execute("DELETE FROM jobs WHERE job_id = ?", (job.job_id,))
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
                no_embed, ytdlp_info_json, updated_at, backend, remote_saved, remote_job_id,
                remote_job_origin
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                updated_at = excluded.updated_at,
                backend = excluded.backend,
                remote_saved = excluded.remote_saved,
                remote_job_id = excluded.remote_job_id,
                remote_job_origin = excluded.remote_job_origin
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
                job.backend,
                1 if job.remote_saved_locally else 0,
                job.remote_job_id,
                (job.remote_job_origin or "desktop").strip() or "desktop",
            ),
        )

    def upsert_many(self, jobs_with_seq: list[tuple[UiJob, int]]) -> None:
        for job, seq in jobs_with_seq:
            self._upsert_no_commit(job, seq)
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
            d = dict(row)
            yinfo: dict[str, Any] | None = None
            raw_j = d.get("ytdlp_info_json")
            if raw_j:
                try:
                    yinfo = json.loads(raw_j)
                except json.JSONDecodeError:
                    yinfo = None
            cand = VideoCandidate(
                video_id=d["video_id"],
                title=d["title"],
                duration=d["duration"],
                channel=d["channel"],
                url=d["url"],
            )
            rp = Path(d["raw_path"]) if d.get("raw_path") else None
            backend = (d.get("backend") or "local").strip() or "local"
            remote_saved = bool(d.get("remote_saved", 1))
            rjid = d.get("remote_job_id")
            rjo = (d.get("remote_job_origin") or "desktop").strip() or "desktop"
            job = UiJob(
                job_id=d["job_id"],
                candidate=cand,
                out_path=Path(d["out_path"]),
                download_status=d["download_status"],
                convert_status=d["convert_status"],
                download_error=d.get("download_error") or "",
                convert_error=d.get("convert_error") or "",
                raw_path=rp,
                ytdlp_info=yinfo,
                no_embed=bool(d.get("no_embed", 0)),
                backend=backend,
                remote_saved_locally=remote_saved,
                remote_job_id=str(rjid) if rjid else None,
                remote_job_origin=rjo,
            )
            out.append(job)
        return out
