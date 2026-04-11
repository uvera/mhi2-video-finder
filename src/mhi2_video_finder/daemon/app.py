"""FastAPI application: REST + WebSocket."""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from fastapi.responses import FileResponse
from pydantic import BaseModel

from mhi2_video_finder.config import Settings, load_settings

from mhi2_video_finder.daemon.auth import require_bearer, ws_token_ok
from mhi2_video_finder.daemon.engine import JobEngine
from mhi2_video_finder.daemon.hub import WsHub
from mhi2_video_finder.daemon.models import JobStatus
from mhi2_video_finder.daemon.store import DaemonJobStore
from mhi2_video_finder.daemon.urlvalidate import validate_youtube_url
from mhi2_video_finder.workflow import safe_stem


class CreateJobBody(BaseModel):
    url: str
    subdir: str = ""
    output_stem: str = ""
    video_id: str = ""
    title: str = ""
    channel: str = ""
    no_embed: bool = False


def _state_path() -> Path:
    raw = (os.environ.get("DAEMON_STATE_DIR") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve() / "jobs.sqlite"
    return Path("/var/lib/mhi2-video-finder/state/jobs.sqlite")


def _config_path() -> Path | None:
    raw = (os.environ.get("MHI2_VIDEO_FINDER_CONFIG") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return None


hub = WsHub()
store: DaemonJobStore | None = None
engine: JobEngine | None = None
app_settings: Settings | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global store, engine, app_settings
    import asyncio

    loop = asyncio.get_running_loop()
    app_settings = load_settings(_config_path())
    store = DaemonJobStore(_state_path())
    engine = JobEngine(app_settings, store, hub, loop)
    engine.recover_non_terminal()
    yield
    if engine:
        engine.shutdown(wait=True)
    if store:
        store.close()


app = FastAPI(title="mhi2-video-finder daemon", lifespan=lifespan)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/jobs", dependencies=[Depends(require_bearer)])
def list_jobs(limit: int = 50) -> dict[str, Any]:
    assert store is not None
    lim = max(1, min(200, limit))
    jobs = [r.to_status_dict() for r in store.list_recent(lim)]
    return {"jobs": jobs}


@app.post("/v1/jobs", status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_bearer)])
def create_job(body: CreateJobBody) -> dict[str, Any]:
    assert engine is not None
    try:
        url = validate_youtube_url(body.url)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    stem = body.output_stem.strip() or "video"
    vid = body.video_id.strip()
    if not stem or stem == "video":
        # caller should pass title-based stem; keep minimal default
        stem = safe_stem(body.title, vid or "video")
    row = engine.create_job(
        url=url,
        subdir=body.subdir,
        output_stem=stem,
        video_id=vid,
        title=body.title,
        channel=body.channel,
        no_embed=body.no_embed,
    )
    return {
        "job_id": row.job_id,
        "status": row.status.value,
        "ws_url_hint": "/v1/ws",
    }


@app.get("/v1/jobs/{job_id}", dependencies=[Depends(require_bearer)])
def get_job(job_id: str) -> dict[str, Any]:
    assert store is not None
    row = store.get(job_id)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown job_id")
    return row.to_status_dict()


@app.post("/v1/jobs/{job_id}/cancel", dependencies=[Depends(require_bearer)])
def cancel_job(job_id: str) -> dict[str, Any]:
    assert engine is not None
    assert store is not None
    if not store.get(job_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown job_id")
    ok = engine.cancel_job(job_id)
    row = store.get(job_id)
    return {"job_id": job_id, "cancel_requested": ok, "status": row.status.value if row else "unknown"}


@app.get("/v1/jobs/{job_id}/download", dependencies=[Depends(require_bearer)])
def download_job(job_id: str):
    assert store is not None
    assert app_settings is not None
    row = store.get(job_id)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown job_id")
    if row.status != JobStatus.DONE:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "job not finished")
    if not row.output_path:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no output path")
    outp = Path(row.output_path).resolve()
    base = app_settings.merged_output_dir().resolve()
    try:
        outp.relative_to(base)
    except ValueError:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "invalid output path") from None
    if not outp.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "file missing")
    return FileResponse(
        path=str(outp),
        filename=outp.name,
        media_type="video/mp4",
    )


@app.websocket("/v1/ws")
async def websocket_endpoint(ws: WebSocket, token: str | None = Query(default=None)):
    if not ws_token_ok(token):
        await ws.close(code=4401)
        return
    await hub.connect(ws)
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send_text(json.dumps({"type": "error", "message": "invalid json"}))
                continue
            mtype = msg.get("type")
            if mtype == "ping":
                await ws.send_text(json.dumps({"type": "pong"}))
            elif mtype == "subscribe_job":
                jid = msg.get("job_id")
                if not isinstance(jid, str) or not jid:
                    await ws.send_text(json.dumps({"type": "error", "message": "missing job_id"}))
                else:
                    await hub.subscribe(ws, jid)
            elif mtype == "unsubscribe_job":
                jid = msg.get("job_id")
                if isinstance(jid, str) and jid:
                    await hub.unsubscribe(ws, jid)
            else:
                await ws.send_text(json.dumps({"type": "error", "message": f"unknown type: {mtype!r}"}))
    except WebSocketDisconnect:
        pass
    finally:
        await hub.disconnect(ws)
