"""Optional Telegram bot (long polling) running alongside the daemon process."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Any

from mhi2_video_finder.daemon.engine import JobEngine
from mhi2_video_finder.paste_urls import parse_pasted_video_urls
from mhi2_video_finder.workflow import safe_stem

_log = logging.getLogger(__name__)


def parse_allowed_telegram_user_ids(raw: str) -> set[int]:
    """Parse ``TELEGRAM_ALLOWED_USER_IDS`` (comma-separated integers)."""
    out: set[int] = set()
    for part in (raw or "").replace(";", ",").split(","):
        p = part.strip()
        if not p:
            continue
        try:
            out.add(int(p))
        except ValueError:
            _log.warning("Ignoring invalid Telegram user id fragment: %r", p)
    return out


def _video_id_from_canonical_watch_url(url: str) -> str:
    """Extract 11-char id from ``https://www.youtube.com/watch?v=...``."""
    from urllib.parse import parse_qs, urlparse

    p = urlparse(url.strip())
    if p.netloc.lower().endswith("youtube.com"):
        q = parse_qs(p.query)
        for v in q.get("v", []):
            s = v.strip()
            if len(s) == 11:
                return s
    return ""


class TelegramBotRunner:
    """Long-poll Telegram updates in a background thread; queue jobs via ``JobEngine``."""

    def __init__(
        self,
        *,
        token: str,
        allowed_user_ids: set[int],
        engine: JobEngine,
        poll_timeout: int = 50,
    ) -> None:
        self._token = token.strip()
        self._allowed = allowed_user_ids
        self._engine = engine
        self._poll_timeout = max(1, min(60, int(poll_timeout)))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._offset: int | None = None

    @classmethod
    def maybe_start(cls, engine: JobEngine) -> TelegramBotRunner | None:
        enabled = (os.environ.get("TELEGRAM_ENABLED") or "1").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        if not enabled:
            _log.info("Telegram bot disabled (TELEGRAM_ENABLED).")
            return None
        token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
        if not token:
            return None
        allow = parse_allowed_telegram_user_ids(os.environ.get("TELEGRAM_ALLOWED_USER_IDS") or "")
        if not allow:
            _log.warning(
                "TELEGRAM_BOT_TOKEN is set but TELEGRAM_ALLOWED_USER_IDS is empty; "
                "refusing to start Telegram bot (no allowlist)."
            )
            return None
        try:
            poll_timeout = int((os.environ.get("TELEGRAM_POLL_TIMEOUT") or "50").strip())
        except ValueError:
            poll_timeout = 50
        runner = cls(
            token=token,
            allowed_user_ids=allow,
            engine=engine,
            poll_timeout=poll_timeout,
        )
        runner.start()
        _log.info("Telegram bot started (long polling, %d allowed user id(s)).", len(allow))
        return runner

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_loop, name="vf-telegram", daemon=True)
        self._thread.start()

    def stop(self, *, join_timeout: float = 8.0) -> None:
        self._stop.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=join_timeout)
        self._thread = None

    def _api(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"https://api.telegram.org/bot{self._token}/{method}"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self._poll_timeout + 15) as resp:
            body = resp.read().decode("utf-8")
        out = json.loads(body)
        if not out.get("ok"):
            raise RuntimeError(f"Telegram API {method} not ok: {out!r}")
        return out

    def _send_message(self, chat_id: int, text: str) -> None:
        try:
            self._api("sendMessage", {"chat_id": chat_id, "text": text[:4000]})
        except Exception:
            _log.exception("sendMessage failed for chat_id=%s", chat_id)

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            try:
                params: dict[str, Any] = {
                    "timeout": self._poll_timeout,
                    "allowed_updates": ["message"],
                }
                if self._offset is not None:
                    params["offset"] = self._offset
                res = self._api("getUpdates", params)
                for u in res.get("result") or []:
                    self._dispatch_update(u)
            except urllib.error.HTTPError as e:
                _log.warning("Telegram getUpdates HTTP error: %s %s", e.code, e.reason)
                time.sleep(3.0)
            except urllib.error.URLError as e:
                if self._stop.is_set():
                    break
                _log.warning("Telegram getUpdates network error: %s", e)
                time.sleep(3.0)
            except Exception:
                if self._stop.is_set():
                    break
                _log.exception("Telegram poll loop error")
                time.sleep(2.0)

    def _dispatch_update(self, update: dict[str, Any]) -> None:
        uid = update.get("update_id")
        if isinstance(uid, int):
            self._offset = uid + 1
        msg = update.get("message")
        if not isinstance(msg, dict):
            return
        from_user = msg.get("from")
        if not isinstance(from_user, dict):
            return
        user_id = from_user.get("id")
        if not isinstance(user_id, int):
            return
        chat = msg.get("chat")
        chat_id = chat.get("id") if isinstance(chat, dict) else user_id
        if not isinstance(chat_id, int):
            return

        if user_id not in self._allowed:
            _log.info("Telegram message from non-allowed user_id=%s; ignoring.", user_id)
            self._send_message(chat_id, "You are not authorized to use this bot.")
            return

        text = msg.get("text") or msg.get("caption") or ""
        if not isinstance(text, str) or not text.strip():
            self._send_message(chat_id, "Send one or more YouTube links (text or caption).")
            return

        urls = parse_pasted_video_urls(text)
        if not urls:
            self._send_message(chat_id, "No YouTube URLs found in your message.")
            return

        lines: list[str] = []
        for u in urls:
            try:
                vid = _video_id_from_canonical_watch_url(u)
                stem = safe_stem("", vid or "video")
                row = self._engine.create_job(
                    url=u,
                    subdir="",
                    output_stem=stem,
                    video_id=vid,
                    title="",
                    channel="",
                    no_embed=False,
                )
                lines.append(f"Queued: {u}\njob_id: {row.job_id}")
            except Exception as e:
                _log.exception("Failed to queue URL from Telegram: %s", u)
                lines.append(f"Failed: {u}\n{e!s}")

        self._send_message(chat_id, "\n\n".join(lines))
