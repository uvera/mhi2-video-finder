"""Bearer-token auth tests: correctness and constant-time comparison."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from mhi2_video_finder.daemon import auth as auth_mod


def _creds(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def test_require_bearer_accepts_correct_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAEMON_BEARER_TOKEN", "secret")
    asyncio.run(auth_mod.require_bearer(_creds("secret")))


def test_require_bearer_rejects_wrong_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAEMON_BEARER_TOKEN", "secret")
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(auth_mod.require_bearer(_creds("wrong")))
    assert exc_info.value.status_code == 401


def test_ws_token_ok_accepts_correct_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAEMON_BEARER_TOKEN", "secret")
    assert auth_mod.ws_token_ok("secret") is True


def test_ws_token_ok_rejects_wrong_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAEMON_BEARER_TOKEN", "secret")
    assert auth_mod.ws_token_ok("wrong") is False


def test_require_bearer_uses_constant_time_comparison(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAEMON_BEARER_TOKEN", "secret")
    calls: list[tuple[bytes, bytes]] = []
    real_compare_digest = auth_mod.hmac.compare_digest

    def spy(a: bytes, b: bytes) -> bool:
        calls.append((a, b))
        return real_compare_digest(a, b)

    monkeypatch.setattr(auth_mod.hmac, "compare_digest", spy)
    with pytest.raises(HTTPException):
        asyncio.run(auth_mod.require_bearer(_creds("wrong")))
    assert calls, "require_bearer must compare tokens via hmac.compare_digest"


def test_warn_if_auth_disabled_logs_warning_when_token_unset(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("DAEMON_BEARER_TOKEN", raising=False)
    with caplog.at_level("WARNING", logger="mhi2_video_finder.daemon.auth"):
        auth_mod.warn_if_auth_disabled()
    assert any("DAEMON_BEARER_TOKEN" in r.message for r in caplog.records)


def test_warn_if_auth_disabled_silent_when_token_set(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("DAEMON_BEARER_TOKEN", "secret")
    with caplog.at_level("WARNING", logger="mhi2_video_finder.daemon.auth"):
        auth_mod.warn_if_auth_disabled()
    assert not caplog.records


def test_ws_token_ok_uses_constant_time_comparison(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAEMON_BEARER_TOKEN", "secret")
    calls: list[tuple[bytes, bytes]] = []
    real_compare_digest = auth_mod.hmac.compare_digest

    def spy(a: bytes, b: bytes) -> bool:
        calls.append((a, b))
        return real_compare_digest(a, b)

    monkeypatch.setattr(auth_mod.hmac, "compare_digest", spy)
    auth_mod.ws_token_ok("wrong")
    assert calls, "ws_token_ok must compare tokens via hmac.compare_digest"
