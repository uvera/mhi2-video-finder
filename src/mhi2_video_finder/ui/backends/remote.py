"""Remote daemon client: REST + WebSocket, mirrors local worker signals."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

import httpx
import websocket
from PyQt6.QtCore import QObject, pyqtSignal

from mhi2_video_finder.config import Settings


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

    def register_existing(self, local_id: str, remote_id: str) -> None:
        with self._lock:
            self._l2r[local_id] = remote_id
            self._r2l[remote_id] = local_id
        self._subscribe_remote(remote_id)

    def enqueue(self, local_id: str, url: str) -> None:
        p = self._pending.get(local_id)
        if not p:
            self.dl.item_failed.emit(local_id, "internal: missing pending job payload")
            return
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

    def start_ws(self) -> None:
        if self._ws_thread and self._ws_thread.is_alive():
            return
        self._stop.clear()
        self._ws_thread = threading.Thread(target=self._ws_loop, daemon=True)
        self._ws_thread.start()

    def sync_job_from_server(self, local_id: str, remote_id: str) -> None:
        threading.Thread(target=self._get_status_and_reconcile, args=(local_id, remote_id), daemon=True).start()

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
            with httpx.Client(timeout=120.0) as c:
                r = c.post(f"{self._base()}/v1/jobs", json=body, headers=self._headers())
            r.raise_for_status()
            data = r.json()
            rid = str(data["job_id"])
            with self._lock:
                self._l2r[local_id] = rid
                self._r2l[rid] = local_id
            self.remote_registered.emit(local_id, rid)
            self._subscribe_remote(rid)
            self.start_ws()
        except Exception as e:
            self.dl.item_failed.emit(local_id, str(e))

    def _post_cancel(self, local_id: str, remote_id: str) -> None:
        try:
            with httpx.Client(timeout=60.0) as c:
                r = c.post(
                    f"{self._base()}/v1/jobs/{remote_id}/cancel",
                    headers=self._headers(),
                )
            if r.status_code == 404:
                return
            r.raise_for_status()
        except Exception as e:
            self.connection_error.emit(f"Cancel failed: {e}")

    def _get_status_and_reconcile(self, local_id: str, remote_id: str) -> None:
        try:
            with httpx.Client(timeout=30.0) as c:
                r = c.get(f"{self._base()}/v1/jobs/{remote_id}", headers=self._headers())
            if r.status_code == 404:
                return
            r.raise_for_status()
            row = r.json()
            self._apply_status_snapshot(local_id, row)
        except Exception as e:
            self.connection_error.emit(f"Sync failed: {e}")

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
        if st == "converting" or (st == "downloading" and phase == "convert"):
            self._emit_dl_done_bridge(local_id)
            self.cv.progress.emit(local_id, float(row.get("convert_percent") or 0.0))
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
                subs: list[str] = []
                with self._lock:
                    subs = list(self._r2l.keys())

                def on_open(ws: Any) -> None:
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
            if st == "converting":
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
                self.cv.progress.emit(lid, None if indet and pct < 0 else pct)
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
