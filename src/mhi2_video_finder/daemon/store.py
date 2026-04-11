"""SQLite persistence for daemon jobs (survive restarts)."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from mhi2_video_finder.daemon.models import DaemonJobRow, JobPhase, JobStatus


class DaemonJobStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or Path("/var/lib/mhi2-video-finder/state/jobs.sqlite")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def close(self) -> None:
        self._conn.close()

    def _init_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS daemon_jobs (
                job_id TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                subdir TEXT NOT NULL DEFAULT '',
                output_stem TEXT NOT NULL DEFAULT '',
                video_id TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                channel TEXT NOT NULL DEFAULT '',
                no_embed INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                phase TEXT,
                download_percent REAL NOT NULL DEFAULT -1,
                convert_percent REAL NOT NULL DEFAULT -1,
                speed TEXT NOT NULL DEFAULT '',
                eta TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                error_phase TEXT,
                raw_path TEXT,
                output_path TEXT,
                ytdlp_info_json TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                finished_at REAL
            )
            """
        )
        valid_statuses = tuple(s.value for s in JobStatus)
        placeholders = ", ".join("?" for _ in valid_statuses)
        self._conn.execute(
            f"""
            UPDATE daemon_jobs
            SET
                status = ?,
                error = CASE
                    WHEN COALESCE(error, '') = '' THEN ?
                    ELSE error
                END,
                error_phase = CASE
                    WHEN COALESCE(error_phase, '') = '' THEN ?
                    ELSE error_phase
                END,
                finished_at = COALESCE(finished_at, ?),
                updated_at = ?
            WHERE COALESCE(TRIM(status), '') = '' OR status NOT IN ({placeholders})
            """,
            (
                JobStatus.FAILED.value,
                "invalid persisted job status; row was auto-repaired",
                JobPhase.DOWNLOAD.value,
                time.time(),
                time.time(),
                *valid_statuses,
            ),
        )
        self._conn.commit()

    def insert(self, row: DaemonJobRow) -> None:
        now = time.time()
        if not row.created_at:
            row.created_at = now
        row.updated_at = now
        self._conn.execute(
            """
            INSERT INTO daemon_jobs (
                job_id, url, subdir, output_stem, video_id, title, channel, no_embed,
                status, phase, download_percent, convert_percent, speed, eta,
                error, error_phase, raw_path, output_path, ytdlp_info_json,
                created_at, updated_at, finished_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row.job_id,
                row.url,
                row.subdir,
                row.output_stem,
                row.video_id,
                row.title,
                row.channel,
                1 if row.no_embed else 0,
                row.status.value,
                row.phase.value if row.phase else None,
                row.download_percent,
                row.convert_percent,
                row.speed,
                row.eta,
                row.error,
                row.error_phase.value if row.error_phase else None,
                row.raw_path,
                row.output_path,
                row.ytdlp_info_json,
                row.created_at,
                row.updated_at,
                row.finished_at,
            ),
        )
        self._conn.commit()

    def update(
        self,
        job_id: str,
        *,
        status: JobStatus | None = None,
        phase: JobPhase | None = None,
        clear_phase: bool = False,
        download_percent: float | None = None,
        convert_percent: float | None = None,
        speed: str | None = None,
        eta: str | None = None,
        error: str | None = None,
        error_phase: JobPhase | None = None,
        clear_error_phase: bool = False,
        raw_path: str | None = None,
        output_path: str | None = None,
        ytdlp_info_json: str | None = None,
        finished_at: float | None = None,
        clear_finished: bool = False,
    ) -> None:
        cur = self._conn.execute("SELECT * FROM daemon_jobs WHERE job_id = ?", (job_id,))
        base = cur.fetchone()
        if not base:
            return
        d = dict(base)
        if status is not None:
            d["status"] = status.value
        if clear_phase:
            d["phase"] = None
        elif phase is not None:
            d["phase"] = phase.value
        if download_percent is not None:
            d["download_percent"] = download_percent
        if convert_percent is not None:
            d["convert_percent"] = convert_percent
        if speed is not None:
            d["speed"] = speed
        if eta is not None:
            d["eta"] = eta
        if error is not None:
            d["error"] = error
        if clear_error_phase:
            d["error_phase"] = None
        elif error_phase is not None:
            d["error_phase"] = error_phase.value
        if raw_path is not None:
            d["raw_path"] = raw_path
        if output_path is not None:
            d["output_path"] = output_path
        if ytdlp_info_json is not None:
            d["ytdlp_info_json"] = ytdlp_info_json
        if finished_at is not None:
            d["finished_at"] = finished_at
        if clear_finished:
            d["finished_at"] = None
        d["updated_at"] = time.time()
        self._conn.execute(
            """
            UPDATE daemon_jobs SET
                status = ?, phase = ?, download_percent = ?, convert_percent = ?,
                speed = ?, eta = ?, error = ?, error_phase = ?,
                raw_path = ?, output_path = ?, ytdlp_info_json = ?,
                updated_at = ?, finished_at = ?
            WHERE job_id = ?
            """,
            (
                d["status"],
                d["phase"],
                d["download_percent"],
                d["convert_percent"],
                d["speed"],
                d["eta"],
                d["error"],
                d["error_phase"],
                d["raw_path"],
                d["output_path"],
                d["ytdlp_info_json"],
                d["updated_at"],
                d["finished_at"],
                job_id,
            ),
        )
        self._conn.commit()

    def get(self, job_id: str) -> DaemonJobRow | None:
        cur = self._conn.execute("SELECT * FROM daemon_jobs WHERE job_id = ?", (job_id,))
        row = cur.fetchone()
        if not row:
            return None
        return self._row_from_sql(row)

    def list_recent(self, limit: int = 50) -> list[DaemonJobRow]:
        cur = self._conn.execute(
            "SELECT * FROM daemon_jobs ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        )
        return [self._row_from_sql(r) for r in cur.fetchall()]

    def list_non_terminal(self) -> list[DaemonJobRow]:
        cur = self._conn.execute(
            """
            SELECT * FROM daemon_jobs
            WHERE status NOT IN ('done', 'failed', 'cancelled')
            ORDER BY created_at ASC
            """
        )
        return [self._row_from_sql(r) for r in cur.fetchall()]

    def delete(self, job_id: str) -> None:
        self._conn.execute("DELETE FROM daemon_jobs WHERE job_id = ?", (job_id,))
        self._conn.commit()

    def update_meta(
        self,
        job_id: str,
        *,
        video_id: str | None = None,
        title: str | None = None,
        channel: str | None = None,
    ) -> None:
        parts: list[str] = []
        vals: list[Any] = []
        if video_id is not None:
            parts.append("video_id = ?")
            vals.append(video_id)
        if title is not None:
            parts.append("title = ?")
            vals.append(title)
        if channel is not None:
            parts.append("channel = ?")
            vals.append(channel)
        if not parts:
            return
        vals.append(time.time())
        vals.append(job_id)
        self._conn.execute(
            f"UPDATE daemon_jobs SET {', '.join(parts)}, updated_at = ? WHERE job_id = ?",
            vals,
        )
        self._conn.commit()

    def _row_from_sql(self, row: sqlite3.Row) -> DaemonJobRow:
        d = dict(row)
        try:
            st = JobStatus(str(d.get("status") or "").strip())
        except ValueError:
            st = JobStatus.FAILED
            if not (d.get("error") or "").strip():
                d["error"] = "invalid persisted job status"
            if not (d.get("error_phase") or "").strip():
                d["error_phase"] = JobPhase.DOWNLOAD.value
        try:
            ph = JobPhase(str(d.get("phase") or "").strip()) if d.get("phase") else None
        except ValueError:
            ph = None
        try:
            eph = JobPhase(str(d.get("error_phase") or "").strip()) if d.get("error_phase") else None
        except ValueError:
            eph = None
        yjson = d["ytdlp_info_json"]
        return DaemonJobRow(
            job_id=d["job_id"],
            url=d["url"],
            subdir=d["subdir"] or "",
            output_stem=d["output_stem"] or "",
            video_id=d["video_id"] or "",
            title=d["title"] or "",
            channel=d["channel"] or "",
            no_embed=bool(d["no_embed"]),
            status=st,
            phase=ph,
            download_percent=float(d["download_percent"]),
            convert_percent=float(d["convert_percent"]),
            speed=d["speed"] or "",
            eta=d["eta"] or "",
            error=d["error"] or "",
            error_phase=eph,
            raw_path=d["raw_path"],
            output_path=d["output_path"],
            ytdlp_info_json=yjson,
            created_at=float(d["created_at"]),
            updated_at=float(d["updated_at"]),
            finished_at=float(d["finished_at"]) if d["finished_at"] is not None else None,
        )


def ytdlp_info_to_json(info: dict[str, Any] | None) -> str | None:
    if info is None:
        return None
    try:
        return json.dumps(info, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        return None


def ytdlp_info_from_json(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        o = json.loads(raw)
        return o if isinstance(o, dict) else None
    except json.JSONDecodeError:
        return None
