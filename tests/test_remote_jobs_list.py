"""Tests for remote daemon job list import (requires PyQt6 for RemoteJobController)."""

from __future__ import annotations

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QApplication

from mhi2_video_finder.config import Settings
from mhi2_video_finder.ui.backends.remote import RemoteJobController


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_fetch_recent_jobs_sync_parses_jobs(qapp: QApplication, monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeResp:
        status_code = 200

        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            return {
                "jobs": [
                    {
                        "job_id": "rid-1",
                        "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                        "title": "T",
                        "channel": "C",
                        "video_id": "dQw4w9WgXcQ",
                        "status": "done",
                    }
                ]
            }

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, params=None, headers=None):
            assert "/v1/jobs" in url
            assert params.get("limit") == 10
            return FakeResp()

    monkeypatch.setattr("mhi2_video_finder.ui.backends.remote.httpx.Client", FakeClient)

    s = Settings()
    s.remote_base_url = "http://127.0.0.1:8765"
    s.remote_bearer_token = "secret"
    ctrl = RemoteJobController(lambda: s)
    rows = ctrl._fetch_recent_jobs_sync(10)
    assert len(rows) == 1
    assert rows[0]["job_id"] == "rid-1"
