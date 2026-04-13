"""Tests for daemon Telegram bot helpers."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from mhi2_video_finder.daemon.telegram_bot import TelegramBotRunner, parse_allowed_telegram_user_ids


def test_parse_allowed_telegram_user_ids() -> None:
    raw = " 111 , 222; bad , 333 "
    assert parse_allowed_telegram_user_ids(raw) == {111, 222, 333}


def test_parse_allowed_telegram_user_ids_empty() -> None:
    assert parse_allowed_telegram_user_ids("") == set()
    assert parse_allowed_telegram_user_ids("  , ; ") == set()


def test_telegram_dispatch_queues_for_allowed_user(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = MagicMock()
    engine.create_job.return_value = MagicMock(job_id="job-uuid-1")
    runner = TelegramBotRunner(
        token="dummy",
        allowed_user_ids={42},
        engine=engine,
        poll_timeout=1,
    )
    sent: list[tuple[int, str]] = []

    def fake_send(chat_id: int, text: str) -> None:
        sent.append((chat_id, text))

    monkeypatch.setattr(runner, "_send_message", fake_send)
    runner._dispatch_update(
        {
            "update_id": 9,
            "message": {
                "message_id": 1,
                "from": {"id": 42, "is_bot": False, "first_name": "A"},
                "chat": {"id": 42, "type": "private"},
                "text": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            },
        }
    )
    assert runner._offset == 10
    engine.create_job.assert_called_once()
    call_kw = engine.create_job.call_args.kwargs
    assert "youtube.com/watch?v=dQw4w9WgXcQ" in call_kw["url"]
    assert call_kw["video_id"] == "dQw4w9WgXcQ"
    assert len(sent) == 1
    assert "Queued:" in sent[0][1]
    assert "job-uuid-1" in sent[0][1]


def test_telegram_dispatch_rejects_disallowed_user(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = MagicMock()
    runner = TelegramBotRunner(
        token="dummy",
        allowed_user_ids={1},
        engine=engine,
        poll_timeout=1,
    )
    sent: list[str] = []
    monkeypatch.setattr(runner, "_send_message", lambda cid, t: sent.append(t))
    runner._dispatch_update(
        {
            "update_id": 3,
            "message": {
                "message_id": 1,
                "from": {"id": 99, "is_bot": False},
                "chat": {"id": 99, "type": "private"},
                "text": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            },
        }
    )
    engine.create_job.assert_not_called()
    assert any("not authorized" in m.lower() for m in sent)
