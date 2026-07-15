"""HTTP API tests for the headless daemon (no real yt-dlp/ffmpeg runs)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from starlette.testclient import TestClient


@pytest.fixture
def daemon_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("DAEMON_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("DAEMON_BEARER_TOKEN", raising=False)
    # Fresh module state per test
    import importlib

    import mhi2_video_finder.daemon.app as app_mod

    importlib.reload(app_mod)
    with TestClient(app_mod.app) as client:
        yield client


def test_healthz(daemon_client: TestClient) -> None:
    r = daemon_client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_create_job_rejects_non_youtube(daemon_client: TestClient) -> None:
    r = daemon_client.post("/v1/jobs", json={"url": "https://example.com/video"})
    assert r.status_code == 400


def test_bearer_required_when_configured(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DAEMON_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("DAEMON_BEARER_TOKEN", "secret")
    import importlib

    import mhi2_video_finder.daemon.app as app_mod

    importlib.reload(app_mod)
    with TestClient(app_mod.app) as client:
        r = client.get("/v1/jobs")
        assert r.status_code == 401
        r2 = client.get("/v1/jobs", headers={"Authorization": "Bearer secret"})
        assert r2.status_code == 200


def test_create_job_mocked_engine(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DAEMON_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("DAEMON_BEARER_TOKEN", raising=False)
    import importlib

    import mhi2_video_finder.daemon.app as app_mod
    import mhi2_video_finder.daemon.models as models

    importlib.reload(app_mod)

    row = models.DaemonJobRow(
        job_id="11111111-1111-1111-1111-111111111111",
        url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        status=models.JobStatus.QUEUED,
    )

    with TestClient(app_mod.app) as client:
        assert app_mod.engine is not None
        app_mod.engine.create_job = MagicMock(return_value=row)  # type: ignore[union-attr]
        r = client.post(
            "/v1/jobs",
            json={
                "url": "https://youtu.be/dQw4w9WgXcQ",
                "subdir": "t",
                "output_stem": "Rick",
                "video_id": "dQw4w9WgXcQ",
                "title": "Never",
                "channel": "Ch",
                "no_embed": True,
            },
        )
        assert r.status_code == 201
        data = r.json()
        assert data["job_id"] == row.job_id
        assert data["status"] == "queued"


def test_diagnostics_endpoint(daemon_client: TestClient) -> None:
    import mhi2_video_finder.daemon.app as app_mod
    from mhi2_video_finder.daemon.models import DaemonJobRow, JobStatus

    assert app_mod.store is not None
    app_mod.store.insert(
        DaemonJobRow(
            job_id="diag-1",
            url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            status=JobStatus.DONE,
        )
    )

    r = daemon_client.get("/v1/diagnostics")
    assert r.status_code == 200
    data = r.json()
    assert data["app_version"]
    assert "build_commit" in data
    assert "build_date" in data
    assert data["uptime_seconds"] >= 0
    assert data["job_counts"]["done"] == 1
    assert data["job_counts"]["total"] == 1


def test_diagnostics_requires_bearer_when_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DAEMON_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("DAEMON_BEARER_TOKEN", "secret")
    import importlib

    import mhi2_video_finder.daemon.app as app_mod

    importlib.reload(app_mod)
    with TestClient(app_mod.app) as client:
        r = client.get("/v1/diagnostics")
        assert r.status_code == 401
        r2 = client.get("/v1/diagnostics", headers={"Authorization": "Bearer secret"})
        assert r2.status_code == 200


def test_delete_job_endpoint(daemon_client: TestClient) -> None:
    import mhi2_video_finder.daemon.app as app_mod
    from mhi2_video_finder.daemon.models import DaemonJobRow, JobStatus

    assert app_mod.store is not None
    app_mod.store.insert(
        DaemonJobRow(
            job_id="del-me",
            url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            status=JobStatus.CANCELLED,
        )
    )
    r = daemon_client.delete("/v1/jobs/del-me")
    assert r.status_code == 200
    assert r.json().get("deleted") is True
    assert daemon_client.get("/v1/jobs/del-me").status_code == 404


def test_daemon_job_store_roundtrip(tmp_path: Path) -> None:
    from mhi2_video_finder.daemon.models import DaemonJobRow, JobStatus
    from mhi2_video_finder.daemon.store import DaemonJobStore

    db = tmp_path / "d.sqlite"
    store = DaemonJobStore(db)
    row = DaemonJobRow(
        job_id="abc",
        url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        subdir="s",
        output_stem="x",
        video_id="dQw4w9WgXcQ",
        title="T",
        channel="C",
        status=JobStatus.DOWNLOADING,
    )
    store.insert(row)
    got = store.get("abc")
    assert got is not None
    assert got.url == row.url
    assert got.status == JobStatus.DOWNLOADING
    store.close()


def test_daemon_restart_lists_non_terminal(tmp_path: Path) -> None:
    from mhi2_video_finder.daemon.models import DaemonJobRow, JobStatus
    from mhi2_video_finder.daemon.store import DaemonJobStore

    db = tmp_path / "d.sqlite"
    store = DaemonJobStore(db)
    store.insert(
        DaemonJobRow(
            job_id="j1",
            url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            status=JobStatus.CONVERTING,
            raw_path=str(tmp_path / "raw.mkv"),
        )
    )
    store.insert(
        DaemonJobRow(
            job_id="j2",
            url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            status=JobStatus.DONE,
        )
    )
    nt = store.list_non_terminal()
    assert len(nt) == 1
    assert nt[0].job_id == "j1"
    store.close()
