"""Daemon SQLite store robustness tests."""

from __future__ import annotations

import sqlite3
import time
import uuid
from pathlib import Path

from mhi2_video_finder.daemon.models import DaemonJobRow, JobPhase, JobStatus
from mhi2_video_finder.daemon.store import DaemonJobStore


def _create_jobs_table(db: Path) -> None:
    conn = sqlite3.connect(str(db))
    conn.execute(
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
    conn.commit()
    conn.close()


def test_store_repairs_invalid_status_rows_on_open(tmp_path: Path) -> None:
    db = tmp_path / "jobs.sqlite"
    _create_jobs_table(db)
    now = time.time()
    conn = sqlite3.connect(str(db))
    conn.execute(
        """
        INSERT INTO daemon_jobs (
            job_id, url, subdir, output_stem, video_id, title, channel, no_embed,
            status, phase, download_percent, convert_percent, speed, eta,
            error, error_phase, raw_path, output_path, ytdlp_info_json,
            created_at, updated_at, finished_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "bad-row",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "",
            "",
            "",
            "",
            "",
            0,
            "",
            None,
            -1.0,
            -1.0,
            "",
            "",
            "",
            None,
            None,
            None,
            None,
            now,
            now,
            None,
        ),
    )
    conn.commit()
    conn.close()

    store = DaemonJobStore(db)
    row = store.get("bad-row")
    assert row is not None
    assert row.status == JobStatus.FAILED
    assert row.error_phase == JobPhase.DOWNLOAD
    assert "auto-repaired" in row.error
    assert row.finished_at is not None
    store.close()


def test_count_by_status(tmp_path: Path) -> None:
    store = DaemonJobStore(tmp_path / "jobs.sqlite")
    store.insert(
        DaemonJobRow(job_id="a", url="https://www.youtube.com/watch?v=dQw4w9WgXcQ", status=JobStatus.DONE)
    )
    store.insert(
        DaemonJobRow(job_id="b", url="https://www.youtube.com/watch?v=dQw4w9WgXcQ", status=JobStatus.DONE)
    )
    store.insert(
        DaemonJobRow(
            job_id="c", url="https://www.youtube.com/watch?v=dQw4w9WgXcQ", status=JobStatus.QUEUED
        )
    )

    counts = store.count_by_status()

    assert counts == {"done": 2, "queued": 1}
    store.close()


def test_list_stale_done_returns_only_old_finished_jobs(tmp_path: Path) -> None:
    store = DaemonJobStore(tmp_path / "jobs.sqlite")
    now = time.time()
    store.insert(
        DaemonJobRow(
            job_id="old-done",
            url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            status=JobStatus.DONE,
            finished_at=now - 4 * 86400,
        )
    )
    store.insert(
        DaemonJobRow(
            job_id="recent-done",
            url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            status=JobStatus.DONE,
            finished_at=now - 1 * 86400,
        )
    )
    store.insert(
        DaemonJobRow(
            job_id="old-failed",
            url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            status=JobStatus.FAILED,
            finished_at=now - 4 * 86400,
        )
    )

    stale = store.list_stale_done(now - 3 * 86400)

    assert [r.job_id for r in stale] == ["old-done"]
    store.close()


def test_store_get_normalizes_non_string_job_id(tmp_path: Path) -> None:
    db = tmp_path / "jobs.sqlite"
    store = DaemonJobStore(db)
    job_id = "11111111-1111-1111-1111-111111111111"
    store.insert(
        DaemonJobRow(
            job_id=job_id,
            url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            status=JobStatus.QUEUED,
        )
    )
    row = store.get(uuid.UUID(job_id))  # type: ignore[arg-type]
    assert row is not None
    assert row.job_id == job_id
    store.close()
