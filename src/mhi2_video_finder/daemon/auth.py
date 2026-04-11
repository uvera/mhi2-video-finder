"""Bearer token auth for REST and WebSocket (?token=)."""

from __future__ import annotations

import os
from typing import Annotated

from fastapi import Depends, HTTPException, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer


def expected_bearer_token() -> str | None:
    """Return configured bearer token, or None if auth is disabled."""
    t = (os.environ.get("DAEMON_BEARER_TOKEN") or "").strip()
    return t or None


security = HTTPBearer(auto_error=False)


async def require_bearer(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(security)],
) -> None:
    expected = expected_bearer_token()
    if not expected:
        return
    if creds is None or (creds.scheme or "").lower() != "bearer":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    if creds.credentials != expected:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid bearer token")


def ws_token_ok(token: str | None) -> bool:
    expected = expected_bearer_token()
    if not expected:
        return True
    return (token or "").strip() == expected


async def ws_token_query(token: Annotated[str | None, Query()] = None) -> None:
    if not ws_token_ok(token):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid or missing token")
