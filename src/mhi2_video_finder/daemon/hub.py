"""WebSocket fan-out hub (async)."""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from typing import Any

from fastapi import WebSocket


class WsHub:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._subs: dict[str, set[WebSocket]] = defaultdict(set)

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()

    async def subscribe(self, ws: WebSocket, job_id: str) -> None:
        async with self._lock:
            self._subs[job_id].add(ws)

    async def unsubscribe(self, ws: WebSocket, job_id: str) -> None:
        async with self._lock:
            subs = self._subs.get(job_id)
            if not subs:
                return
            subs.discard(ws)
            if not subs:
                del self._subs[job_id]

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            empty: list[str] = []
            for jid, subs in self._subs.items():
                subs.discard(ws)
                if not subs:
                    empty.append(jid)
            for jid in empty:
                del self._subs[jid]

    async def broadcast_job(self, job_id: str, message: dict[str, Any]) -> None:
        text = json.dumps(message, default=str)
        async with self._lock:
            targets = list(self._subs.get(job_id, ()))
        dead: list[WebSocket] = []
        for ws in targets:
            try:
                await ws.send_text(text)
            except Exception:
                dead.append(ws)
        for ws in dead:
            await self.disconnect(ws)
