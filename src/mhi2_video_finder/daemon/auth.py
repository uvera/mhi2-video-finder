"""Bearer token auth for REST and WebSocket (?token=)."""

from __future__ import annotations

import hmac
import logging
import os
from typing import Annotated

from fastapi import Depends, HTTPException, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

_log = logging.getLogger(__name__)


def expected_bearer_token() -> str | None:
    """Return configured bearer token, or None if auth is disabled."""
    t = (os.environ.get("DAEMON_BEARER_TOKEN") or "").strip()
    return t or None


def warn_if_auth_disabled() -> None:
    """Log a warning at startup when the daemon is running with no bearer token."""
    if expected_bearer_token() is None:
        _log.warning(
            "DAEMON_BEARER_TOKEN is not set; the daemon API and WebSocket are "
            "running with authentication disabled and are open to any client "
            "that can reach them."
        )


security = HTTPBearer(auto_error=False)


async def require_bearer(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(security)],
) -> None:
    expected = expected_bearer_token()
    if not expected:
        return
    if creds is None or (creds.scheme or "").lower() != "bearer":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    if not hmac.compare_digest(creds.credentials.encode(), expected.encode()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid bearer token")


def ws_token_ok(token: str | None) -> bool:
    expected = expected_bearer_token()
    if not expected:
        return True
    return hmac.compare_digest((token or "").strip().encode(), expected.encode())


async def ws_token_query(token: Annotated[str | None, Query()] = None) -> None:
    if not ws_token_ok(token):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid or missing token")
