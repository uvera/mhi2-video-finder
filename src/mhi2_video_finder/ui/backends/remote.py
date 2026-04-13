"""Remote daemon client: REST + WebSocket, mirrors local worker signals."""

from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

import httpx
import websocket
from PyQt6.QtCore import QObject, pyqtSignal

from mhi2_video_finder.config import Settings
from mhi2_video_finder.debug_runtime_log import emit_debug_log as _debug_log

_log = logging.getLogger(__name__)


def _http_base_to_ws_base(base: str) -> str:
    b = base.rstrip("/")
    if b.startswith("https://"):
        return "wss://" + b[len("https://") :]
    if b.startswith("http://"):
        return "ws://" + b[len("http://") :]
    return "ws://" + b


class RemoteDownloadFacade(QObject):
    progress = pyqtSignal(str, float, str, str)
    item_done = pyqtSignal(str, str, object)
    item_failed = pyqtSignal(str, str)
    download_cancelled = pyqtSignal(str)

    def __init__(self, ctrl: RemoteJobController) -> None:
        super().__init__(ctrl)
        self._ctrl = ctrl

    def set_settings(self, _settings: Settings) -> None:
        pass

    def set_max_workers(self, _n: int) -> None:
        pass

    def cancel_download(self, job_id: str) -> None:
        self._ctrl.cancel(job_id)

    def enqueue(self, job_id: str, url: str) -> None:
        self._ctrl.enqueue(job_id, url)

    def stop(self) -> None:
        self._ctrl.stop()


class RemoteConvertFacade(QObject):
    progress = pyqtSignal(str, object)
    item_done = pyqtSignal(str)
    item_failed = pyqtSignal(str, str)
    convert_cancelled = pyqtSignal(str)

    def __init__(self, ctrl: RemoteJobController) -> None:
        super().__init__(ctrl)
        self._ctrl = ctrl

    def set_settings(self, _settings: Settings) -> None:
        pass

    def set_max_workers(self, _n: int) -> None:
        pass

    def set_no_embed(self, _v: bool) -> None:
        pass

    def cancel_convert(self, job_id: str) -> None:
        self._ctrl.cancel(job_id)

    def enqueue(
        self,
        _job_id: str,
        _raw_path: Path,
        _out_path: Path,
        _yinfo: dict[str, Any] | None,
        *,
        _no_embed: bool,
    ) -> None:
        pass

    def stop(self) -> None:
        self._ctrl.stop()


class RemoteJobController(QObject):
    """Bridges daemon events to the same signals MainWindow expects from local workers."""

    connection_error = pyqtSignal(str)
    remote_registered = pyqtSignal(str, str)  # local_job_id, remote_job_id (after POST)
    remote_missing = pyqtSignal(str, str)  # local_job_id, reason (remote job vanished)
    # Emitted on GUI thread after ``GET /v1/jobs`` (used to import daemon-created jobs).
    recent_jobs_ready = pyqtSignal(list)
    # Short status text for the main window (emitted from worker threads; Qt queues to GUI thread).
    user_status = pyqtSignal(str)
    # title/channel/video_id from REST or WebSocket when daemon fills them (e.g. after yt-dlp).
    # Empty string for a field means "leave existing UI value unchanged".
    remote_meta_updated = pyqtSignal(str, str, str, str)

    def __init__(self, get_settings: Callable[[], Settings]) -> None:
        super().__init__()
        self._get_settings = get_settings
        self.dl = RemoteDownloadFacade(self)
        self.cv = RemoteConvertFacade(self)
        self._lock = threading.Lock()
        self._l2r: dict[str, str] = {}
        self._r2l: dict[str, str] = {}
        self._pending: dict[str, dict[str, Any]] = {}
        self._dl_done_sent: set[str] = set()
        self._ws_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._ws_app: websocket.WebSocketApp | None = None
        self._need_ws_restart = threading.Event()
        self._stopped_once = threading.Event()
        # Bound status-sync concurrency to avoid startup thread storms when many
        # remote jobs are restored/imported at once.
        self._sync_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="vf-remote-sync")
        self._sync_inflight: set[tuple[str, str]] = set()

    def register_existing(self, local_id: str, remote_id: str) -> None:
        with self._lock:
            self._l2r[local_id] = remote_id
            self._r2l[remote_id] = local_id
        self._subscribe_remote(remote_id)

    def enqueue(self, local_id: str, url: str) -> None:
        p = self._pending.get(local_id)
        if not p:
            # region agent log
            _debug_log(
                "H1",
                "ui/backends/remote.py:150",
                "enqueue rejected due missing pending payload",
                {"local_id": local_id},
            )
            # endregion
            self.dl.item_failed.emit(local_id, "internal: missing pending job payload")
            return
        # region agent log
        _debug_log(
            "H1",
            "ui/backends/remote.py:159",
            "enqueue accepted and post thread starting",
            {"local_id": local_id, "url": url},
        )
        # endregion
        threading.Thread(target=self._post_job, args=(local_id, url, p), daemon=True).start()

    def set_pending_job(
        self,
        local_id: str,
        *,
        subdir: str,
        output_stem: str,
        video_id: str,
        title: str,
        channel: str,
        no_embed: bool,
    ) -> None:
        self._pending[local_id] = {
            "subdir": subdir,
            "output_stem": output_stem,
            "video_id": video_id,
            "title": title,
            "channel": channel,
            "no_embed": no_embed,
        }

    def clear_pending(self, local_id: str) -> None:
        self._pending.pop(local_id, None)

    def reset_local_tracking(self, local_id: str) -> None:
        """Forget server mapping and download-bridge flags (e.g. before re-queueing a remote job)."""
        self._dl_done_sent.discard(local_id)
        with self._lock:
            rid = self._l2r.pop(local_id, None)
            if rid:
                self._r2l.pop(rid, None)

    def cancel(self, local_id: str) -> None:
        rid: str | None
        with self._lock:
            rid = self._l2r.get(local_id)
        if not rid:
            return
        threading.Thread(target=self._post_cancel, args=(local_id, rid), daemon=True).start()

    def delete_server_job_sync(self, remote_id: str) -> bool:
        """``DELETE /v1/jobs/{id}`` so the daemon drops the row (stops re-import after UI restart)."""
        base = self._base()
        if not base:
            self.connection_error.emit("Remote: base URL not set; cannot delete job on server.")
            return False
        url = f"{base.rstrip('/')}/v1/jobs/{quote(remote_id, safe='')}"
        try:
            with httpx.Client(timeout=60.0) as c:
                r = c.delete(url, headers=self._headers())
            if r.status_code == 404:
                return True
            r.raise_for_status()
            return True
        except Exception as e:
            _log.warning("DELETE %s failed: %s", url, e)
            self.connection_error.emit(f"Remote delete failed: {e}")
            return False

    def stop(self) -> None:
        if self._stopped_once.is_set():
            return
        self._stopped_once.set()
        self._stop.set()
        self._need_ws_restart.set()
        app = self._ws_app
        if app is not None:
            try:
                app.close()
            except Exception:
                pass
        try:
            self._sync_pool.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass

    def start_ws(self) -> None:
        if self._ws_thread and self._ws_thread.is_alive():
            return
        self._stop.clear()
        self._ws_thread = threading.Thread(target=self._ws_loop, daemon=True)
        self._ws_thread.start()

    def sync_job_from_server(self, local_id: str, remote_id: str) -> None:
        if self._stop.is_set():
            return
        key = (local_id, remote_id)
        with self._lock:
            if key in self._sync_inflight:
                return
            self._sync_inflight.add(key)
        fut = self._sync_pool.submit(self._get_status_and_reconcile, local_id, remote_id)

        def _done(_f: object) -> None:
            with self._lock:
                self._sync_inflight.discard(key)

        fut.add_done_callback(_done)

    def fetch_recent_jobs_async(self, limit: int = 200) -> None:
        """Fetch ``GET /v1/jobs`` in a worker thread; emit ``recent_jobs_ready`` on the GUI thread."""

        def _run() -> None:
            rows = self._fetch_recent_jobs_sync(limit)
            self.recent_jobs_ready.emit(rows)

        threading.Thread(target=_run, daemon=True).start()

    def _fetch_recent_jobs_sync(self, limit: int) -> list[dict[str, Any]]:
        base = self._base()
        if not base:
            return []
        lim = max(1, min(200, int(limit)))
        try:
            with httpx.Client(timeout=httpx.Timeout(60.0, connect=15.0)) as c:
                r = c.get(f"{base}/v1/jobs", params={"limit": lim}, headers=self._headers())
            if r.status_code == 401:
                self.connection_error.emit("Import jobs: unauthorized (check remote bearer token).")
                return []
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            _log.warning("GET /v1/jobs failed: %s", e)
            self.connection_error.emit(f"Import jobs failed: {e}")
            return []
        jobs = data.get("jobs")
        if not isinstance(jobs, list):
            return []
        return [j for j in jobs if isinstance(j, dict)]

    def _headers(self) -> dict[str, str]:
        s = self._get_settings()
        h: dict[str, str] = {}
        t = (s.remote_bearer_token or "").strip()
        if t:
            h["Authorization"] = f"Bearer {t}"
        return h

    def _base(self) -> str:
        return (self._get_settings().remote_base_url or "").strip().rstrip("/")

    def _ws_url(self) -> str:
        s = self._get_settings()
        base = _http_base_to_ws_base(self._base())
        tok = (s.remote_bearer_token or "").strip()
        q = f"?token={quote(tok)}" if tok else ""
        return f"{base}/v1/ws{q}"

    def _post_job(self, local_id: str, url: str, p: dict[str, Any]) -> None:
        base = self._base()
        if not base:
            self.dl.item_failed.emit(
                local_id,
                "Remote base URL is empty; set it in Settings and Apply or Save.",
            )
            return
        post_url = f"{base}/v1/jobs"
        req_started = time.time()
        # region agent log
        _debug_log(
            "H2",
            "ui/backends/remote.py:260",
            "starting POST /v1/jobs",
            {"local_id": local_id, "post_url": post_url},
        )
        # endregion
        self.user_status.emit(f"Remote: POST {post_url} …")
        _log.info("POST %s local_job_id=%s body_url=%s", post_url, local_id, url)
        try:
            body = {
                "url": url,
                "subdir": p.get("subdir") or "",
                "output_stem": p.get("output_stem") or "",
                "video_id": p.get("video_id") or "",
                "title": p.get("title") or "",
                "channel": p.get("channel") or "",
                "no_embed": bool(p.get("no_embed")),
            }
            timeout = httpx.Timeout(120.0, connect=15.0)
            with httpx.Client(timeout=timeout) as c:
                r = c.post(post_url, json=body, headers=self._headers())
            # region agent log
            _debug_log(
                "H2",
                "ui/backends/remote.py:279",
                "POST /v1/jobs finished",
                {
                    "local_id": local_id,
                    "status_code": r.status_code,
                    "elapsed_ms": int((time.time() - req_started) * 1000),
                },
            )
            # endregion
            _log.info("POST response %s %s", r.status_code, r.headers.get("content-type", ""))
            try:
                r.raise_for_status()
            except httpx.HTTPStatusError as e:
                detail = (e.response.text or "")[:800].strip()
                msg = f"HTTP {e.response.status_code} from {post_url}"
                if detail:
                    msg += f": {detail}"
                _log.warning("POST failed: %s", msg)
                self.dl.item_failed.emit(local_id, msg)
                self.user_status.emit(f"Remote: failed ({e.response.status_code})")
                return
            try:
                data = r.json()
            except json.JSONDecodeError:
                snippet = (r.text or "")[:300]
                msg = f"Server did not return JSON (status {r.status_code}): {snippet!r}"
                _log.warning(msg)
                self.dl.item_failed.emit(local_id, msg)
                self.user_status.emit("Remote: invalid response (not JSON)")
                return
            if "job_id" not in data:
                self.dl.item_failed.emit(local_id, f"Unexpected JSON from server: {data!r}")
                self.user_status.emit("Remote: bad response (no job_id)")
                return
            rid = str(data["job_id"])
            with self._lock:
                self._l2r[local_id] = rid
                self._r2l[rid] = local_id
            # region agent log
            _debug_log(
                "H1",
                "ui/backends/remote.py:318",
                "local to remote mapping registered",
                {"local_id": local_id, "remote_id": rid},
            )
            # endregion
            _log.info("remote job id %s for local %s", rid, local_id)
            self.remote_registered.emit(local_id, rid)
            self._subscribe_remote(rid)
            self.start_ws()
            self.user_status.emit(f"Remote: job accepted ({rid[:8]}…)")
        except httpx.ConnectError as e:
            msg = f"Cannot connect to {base}: {e}"
            _log.warning(msg)
            self.dl.item_failed.emit(local_id, msg)
            self.user_status.emit("Remote: connection refused / unreachable")
        except httpx.ConnectTimeout as e:
            msg = f"Connect timed out to {base}: {e}"
            _log.warning(msg)
            self.dl.item_failed.emit(local_id, msg)
            self.user_status.emit("Remote: connect timeout")
        except httpx.ReadTimeout as e:
            msg = f"Server accepted the connection but did not respond in time: {base} — {e}"
            _log.warning(msg)
            self.dl.item_failed.emit(local_id, msg)
            self.user_status.emit("Remote: read timeout")
        except httpx.TimeoutException as e:
            msg = f"Timeout talking to {base}: {e}"
            _log.warning(msg)
            self.dl.item_failed.emit(local_id, msg)
            self.user_status.emit("Remote: timeout")
        except Exception as e:
            _log.exception("POST /v1/jobs")
            self.dl.item_failed.emit(local_id, str(e))
            self.user_status.emit("Remote: error (see Downloads row or terminal)")

    def _post_cancel(self, local_id: str, remote_id: str) -> None:
        try:
            with httpx.Client(timeout=60.0) as c:
                r = c.post(
                    f"{self._base()}/v1/jobs/{remote_id}/cancel",
                    headers=self._headers(),
                )
            if r.status_code == 404:
                self._emit_remote_missing(
                    local_id,
                    remote_id,
                    "Cancel failed because the server no longer has this job id.",
                )
                return
            r.raise_for_status()
            # Some servers/links can drop the WS cancel event; reconcile over REST so
            # the UI row still reflects cancellation after a successful cancel request.
            self._reconcile_after_cancel(local_id, remote_id)
        except Exception as e:
            self.connection_error.emit(f"Cancel failed: {e}")

    def _reconcile_after_cancel(self, local_id: str, remote_id: str) -> None:
        last_err: Exception | None = None
        for _ in range(8):
            try:
                with httpx.Client(timeout=10.0) as c:
                    r = c.get(f"{self._base()}/v1/jobs/{remote_id}", headers=self._headers())
                if r.status_code == 404:
                    self._emit_remote_missing(
                        local_id,
                        remote_id,
                        "Status sync after cancel failed because the server no longer has this job id.",
                    )
                    return
                r.raise_for_status()
                row = r.json()
                self._apply_status_snapshot(local_id, row)
                if row.get("status") in ("cancelled", "failed", "done"):
                    return
            except Exception as e:
                last_err = e
            time.sleep(0.35)
        if last_err is not None:
            self.connection_error.emit(f"Cancel sync failed: {last_err}")

    def _get_status_and_reconcile(self, local_id: str, remote_id: str) -> None:
        try:
            with httpx.Client(timeout=30.0) as c:
                r = c.get(f"{self._base()}/v1/jobs/{remote_id}", headers=self._headers())
            if r.status_code == 404:
                self._emit_remote_missing(
                    local_id,
                    remote_id,
                    "Status sync failed because the server no longer has this job id.",
                )
                return
            r.raise_for_status()
            row = r.json()
            self._apply_status_snapshot(local_id, row)
        except Exception as e:
            self.connection_error.emit(f"Sync failed: {e}")

    def _emit_remote_missing(self, local_id: str, remote_id: str, detail: str) -> None:
        with self._lock:
            mapped_remote = self._l2r.get(local_id)
            if mapped_remote == remote_id:
                self._l2r.pop(local_id, None)
            self._r2l.pop(remote_id, None)
        self._dl_done_sent.discard(local_id)
        self.remote_missing.emit(local_id, f"Remote job {remote_id} is missing. {detail}")

    def _emit_remote_meta_from_row(self, local_id: str, row: dict[str, Any]) -> None:
        t = str(row.get("title") or "").strip()
        ch = str(row.get("channel") or "").strip()
        v = str(row.get("video_id") or "").strip()
        if len(v) != 11:
            v = ""
        if t or ch or v:
            self.remote_meta_updated.emit(local_id, t, ch, v)

    def _apply_status_snapshot(self, local_id: str, row: dict[str, Any]) -> None:
        st = row.get("status")
        phase = row.get("phase")
        if st in ("queued", "downloading"):
            self.dl.progress.emit(
                local_id,
                float(row.get("download_percent") or -1.0),
                row.get("speed") or "",
                row.get("eta") or "",
            )
        if st == "downloading" and phase == "convert":
            self._emit_dl_done_bridge(local_id)
        if st == "converting":
            self._emit_dl_done_bridge(local_id)
            cp = row.get("convert_percent")
            cp_f = float(cp) if isinstance(cp, (int, float)) else -1.0
            self.cv.progress.emit(local_id, None if cp_f < 0.0 else cp_f)
        if st == "done":
            self._emit_dl_done_bridge(local_id)
            self.cv.item_done.emit(local_id)
        if st == "failed":
            ep = row.get("error_phase")
            err = row.get("error") or "failed"
            if ep == "convert":
                self.cv.item_failed.emit(local_id, err)
            else:
                self.dl.item_failed.emit(local_id, err)
        if st == "cancelled":
            ph = row.get("phase")
            if ph == "convert":
                self._emit_dl_done_bridge(local_id)
                self.cv.convert_cancelled.emit(local_id)
            else:
                self.dl.download_cancelled.emit(local_id)
        self._emit_remote_meta_from_row(local_id, row)

    def _emit_dl_done_bridge(self, local_id: str) -> None:
        if local_id in self._dl_done_sent:
            return
        self._dl_done_sent.add(local_id)
        self.dl.item_done.emit(local_id, "", None)

    def _subscribe_remote(self, remote_id: str) -> None:
        self.start_ws()
        try:
            app = self._ws_app
            if app and app.sock and app.sock.connected:
                app.send(json.dumps({"type": "subscribe_job", "job_id": remote_id}))
        except Exception:
            self._need_ws_restart.set()

    def _ws_loop(self) -> None:
        while not self._stop.is_set():
            try:
                def on_open(ws: Any) -> None:
                    # Read current subscriptions at open-time (not loop-time): jobs can be
                    # re-registered while the socket is still connecting during client restart.
                    with self._lock:
                        subs = list(self._r2l.keys())
                    for rid in subs:
                        try:
                            ws.send(json.dumps({"type": "subscribe_job", "job_id": rid}))
                        except Exception:
                            pass

                def on_message(_ws: Any, message: str) -> None:
                    self._handle_ws_message(message)

                def on_error(_ws: Any, err: Any) -> None:
                    if err and not self._stop.is_set():
                        self.connection_error.emit(str(err))

                app = websocket.WebSocketApp(
                    self._ws_url(),
                    on_open=on_open,
                    on_message=on_message,
                    on_error=on_error,
                )
                self._ws_app = app
                app.run_forever(ping_interval=25, ping_timeout=20)
            except Exception as e:
                if not self._stop.is_set():
                    self.connection_error.emit(str(e))
            if self._stop.is_set():
                break
            time.sleep(2.0)

    def _handle_ws_message(self, message: str) -> None:
        try:
            o = json.loads(message)
        except json.JSONDecodeError:
            return
        mtype = o.get("type")
        rid = o.get("job_id")
        if not isinstance(rid, str):
            return
        with self._lock:
            lid = self._r2l.get(rid)
        if not lid:
            return

        if mtype == "job_state_changed":
            st = o.get("status")
            ph = o.get("phase")
            if st in ("queued", "downloading", "converting"):
                # region agent log
                _debug_log(
                    "H4",
                    "ui/backends/remote.py:533",
                    "ws state changed received",
                    {"local_id": lid, "remote_id": rid, "status": st, "phase": ph},
                )
                # endregion
            if any(k in o for k in ("title", "channel", "video_id")):
                self._emit_remote_meta_from_row(lid, o)
            if st == "converting" or (st == "downloading" and ph == "convert"):
                self._emit_dl_done_bridge(lid)
        elif mtype == "job_progress":
            phase = o.get("phase")
            pct = float(o.get("percent") if o.get("percent") is not None else -1.0)
            speed = o.get("speed") or ""
            eta = o.get("eta") or ""
            indet = bool(o.get("indeterminate"))
            if phase == "download":
                self.dl.progress.emit(lid, pct, str(speed), str(eta))
            elif phase == "convert":
                self._emit_dl_done_bridge(lid)
                self.cv.progress.emit(lid, None if indet or pct < 0.0 else pct)
        elif mtype == "job_done":
            self._emit_dl_done_bridge(lid)
            self.cv.item_done.emit(lid)
        elif mtype == "job_failed":
            phase = o.get("phase")
            err = str(o.get("error") or "failed")
            if phase == "convert":
                self.cv.item_failed.emit(lid, err)
            else:
                self.dl.item_failed.emit(lid, err)
        elif mtype == "job_cancelled":
            ph = o.get("phase")
            if ph == "convert":
                self._emit_dl_done_bridge(lid)
                self.cv.convert_cancelled.emit(lid)
            else:
                self.dl.download_cancelled.emit(lid)
