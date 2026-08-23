"""FastAPI app for SEGUE's api service: routes, static/template serving,
websocket fan-out, and startup wiring of the state machine + telnet client.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config as config_mod
from .db import Database
from .state import FILLER, StateManager
from .telnet_client import TelnetClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("segue.api")

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"

ADMIN_COOKIE = "segue_admin"


def _env(name: str, default: Optional[str] = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise RuntimeError(f"missing required environment variable: {name}")
    return value


app = FastAPI(title="SEGUE api")


# ---------------------------------------------------------------------------
# Admin auth
# ---------------------------------------------------------------------------

def require_admin(request: Request) -> None:
    token = request.cookies.get(ADMIN_COOKIE)
    expected = app.state.admin_token
    if not token or token != expected:
        raise HTTPException(status_code=401, detail="unauthorized")


def _admin_ws_ok(websocket: WebSocket) -> bool:
    token = websocket.cookies.get(ADMIN_COOKIE)
    return bool(token) and token == app.state.admin_token


# ---------------------------------------------------------------------------
# Websocket connection registry + broadcast
# ---------------------------------------------------------------------------

class ConnectionManager:
    def __init__(self) -> None:
        self.admin_sockets: set[WebSocket] = set()
        self.dj_sockets: dict[WebSocket, str] = {}

    async def broadcast(self) -> None:
        state_manager: StateManager = app.state.state_manager
        full_state = state_manager.get_full_state()
        dead = []
        for ws in list(self.admin_sockets):
            try:
                await ws.send_json(full_state)
            except Exception:  # noqa: BLE001
                dead.append(ws)
        for ws in dead:
            self.admin_sockets.discard(ws)

        dead = []
        for ws, dj_id in list(self.dj_sockets.items()):
            dj_state = state_manager.get_dj_state(dj_id)
            if dj_state is None:
                dead.append(ws)
                continue
            try:
                await ws.send_json(dj_state)
            except Exception:  # noqa: BLE001
                dead.append(ws)
        for ws in dead:
            self.dj_sockets.pop(ws, None)


manager = ConnectionManager()


# ---------------------------------------------------------------------------
# Startup / shutdown
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def on_startup() -> None:
    app.state.admin_token = _env("ONAIR_ADMIN_TOKEN")
    app.state.internal_secret = _env("ONAIR_INTERNAL_SECRET")
    harbor_host = _env("ONAIR_HARBOR_PUBLIC_HOST", "")
    harbor_port = int(os.environ.get("ONAIR_HARBOR_PUBLIC_PORT", "8005"))
    debounce_seconds = float(os.environ.get("ONAIR_DEBOUNCE_SECONDS", "2"))
    db_path = os.environ.get("ONAIR_DB_PATH", "/data/onair.db")
    liquidsoap_host = os.environ.get("ONAIR_LIQUIDSOAP_HOST", "liquidsoap")
    liquidsoap_port = int(os.environ.get("ONAIR_LIQUIDSOAP_TELNET_PORT", "1234"))

    djs = config_mod.load_djs()
    db = Database(db_path)
    for dj in djs:
        db.get_or_create_token(dj.id)

    app.state.djs = djs
    app.state.db = db
    app.state.harbor_host = harbor_host
    app.state.harbor_port = harbor_port

    telnet = TelnetClient(liquidsoap_host, liquidsoap_port)
    app.state.telnet = telnet

    mode, pinned = db.load_settings()

    state_manager = StateManager(
        djs=djs,
        debounce_seconds=debounce_seconds,
        telnet_set_target=telnet.set_target,
        on_broadcast=manager.broadcast,
        log_event=db.log_event,
        save_settings=db.save_settings,
    )
    state_manager.load_intentions(mode, pinned)
    app.state.state_manager = state_manager

    async def on_connect(is_first: bool) -> None:
        ready, target = await telnet.status()
        if is_first:
            await state_manager.startup_sync(ready, target)
        else:
            await state_manager.reconcile(ready)

    async def on_alive_change(alive: bool) -> None:
        await state_manager.set_telnet_alive(alive)

    async def on_reconcile(ready: dict) -> None:
        await state_manager.reconcile(ready)

    app.state.telnet_task = asyncio.create_task(
        telnet.run_forever(on_connect, on_alive_change, on_reconcile)
    )


@app.on_event("shutdown")
async def on_shutdown() -> None:
    task = getattr(app.state, "telnet_task", None)
    if task is not None:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Admin session
# ---------------------------------------------------------------------------

class SessionRequest(BaseModel):
    token: str


@app.post("/api/session")
async def create_session(body: SessionRequest, response: Response) -> dict:
    if body.token != app.state.admin_token:
        raise HTTPException(status_code=401, detail="invalid token")
    response.set_cookie(
        ADMIN_COOKIE,
        body.token,
        httponly=True,
        samesite="lax",
    )
    return {"ok": True}


# ---------------------------------------------------------------------------
# Admin REST
# ---------------------------------------------------------------------------

@app.get("/api/state")
async def get_state(request: Request) -> dict:
    require_admin(request)
    return app.state.state_manager.get_full_state()


class ModeRequest(BaseModel):
    mode: str


@app.post("/api/mode")
async def post_mode(body: ModeRequest, request: Request) -> dict:
    require_admin(request)
    if body.mode not in ("AUTO", "MANUAL"):
        raise HTTPException(status_code=400, detail="mode must be AUTO or MANUAL")
    await app.state.state_manager.set_mode(body.mode)
    return app.state.state_manager.get_full_state()


class PinRequest(BaseModel):
    dj_id: str


@app.post("/api/pin")
async def post_pin(body: PinRequest, request: Request) -> dict:
    require_admin(request)
    try:
        await app.state.state_manager.set_pin(body.dj_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return app.state.state_manager.get_full_state()


@app.post("/api/filler")
async def post_filler(request: Request) -> dict:
    require_admin(request)
    await app.state.state_manager.force_filler()
    return app.state.state_manager.get_full_state()


@app.get("/api/log")
async def get_log(request: Request, limit: int = 100) -> list:
    require_admin(request)
    return app.state.db.get_log(limit=limit)


@app.websocket("/ws")
async def ws_admin(websocket: WebSocket) -> None:
    if not _admin_ws_ok(websocket):
        await websocket.close(code=4401)
        return
    await websocket.accept()
    manager.admin_sockets.add(websocket)
    try:
        await websocket.send_json(app.state.state_manager.get_full_state())
        while True:
            # Admin socket is push-only from the server's perspective; just
            # drain whatever the client sends (pings etc.) to detect drops.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        pass
    finally:
        manager.admin_sockets.discard(websocket)


# ---------------------------------------------------------------------------
# DJ routes
# ---------------------------------------------------------------------------

def _dj_credentials(dj_id: str) -> dict:
    dj = next(d for d in app.state.djs if d.id == dj_id)
    return {
        "host": app.state.harbor_host,
        "port": app.state.harbor_port,
        "mount": dj.mount,
        "user": dj.id,
        "password": dj.password,
        "format_hint": "MP3 320kbps oder Ogg Vorbis",
    }


@app.get("/dj/{token}")
async def dj_view(token: str) -> FileResponse:
    dj_id = app.state.db.dj_id_for_token(token)
    if dj_id is None:
        raise HTTPException(status_code=404, detail="unknown token")
    path = STATIC_DIR / "dj" / "index.html"
    if not path.exists():
        raise HTTPException(status_code=404, detail="dj frontend not built yet")
    return FileResponse(path)


@app.get("/api/dj/{token}/state")
async def dj_state(token: str) -> dict:
    dj_id = app.state.db.dj_id_for_token(token)
    if dj_id is None:
        raise HTTPException(status_code=404, detail="unknown token")
    state = app.state.state_manager.get_dj_state(dj_id)
    if state is None:
        raise HTTPException(status_code=404, detail="unknown dj")
    state["dj"]["credentials"] = _dj_credentials(dj_id)
    return state


@app.websocket("/ws/dj/{token}")
async def ws_dj(websocket: WebSocket, token: str) -> None:
    dj_id = app.state.db.dj_id_for_token(token)
    if dj_id is None:
        await websocket.close(code=4404)
        return
    await websocket.accept()
    manager.dj_sockets[websocket] = dj_id
    try:
        initial = app.state.state_manager.get_dj_state(dj_id)
        if initial is not None:
            initial["dj"]["credentials"] = _dj_credentials(dj_id)
            await websocket.send_json(initial)
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        pass
    finally:
        manager.dj_sockets.pop(websocket, None)


# ---------------------------------------------------------------------------
# Internal webhook
# ---------------------------------------------------------------------------

@app.post("/internal/harbor/event")
async def harbor_event(request: Request) -> dict:
    secret = request.headers.get("X-Onair-Secret")
    if secret != app.state.internal_secret:
        raise HTTPException(status_code=403, detail="forbidden")
    body = await request.json()
    dj_id = body.get("dj_id")
    event = body.get("event")
    ts = body.get("ts")
    received_at = datetime.now(timezone.utc)
    logger.info("harbor event dj_id=%s event=%s liquidsoap_ts=%s", dj_id, event, ts)
    # Never block the webhook response on the debounce/resolve pipeline.
    asyncio.create_task(
        app.state.state_manager.handle_webhook_event(dj_id, event, received_at)
    )
    return {"ok": True}


# ---------------------------------------------------------------------------
# Frontend serving
# ---------------------------------------------------------------------------

@app.get("/")
async def admin_view() -> FileResponse:
    path = STATIC_DIR / "admin" / "index.html"
    if not path.exists():
        raise HTTPException(status_code=404, detail="admin frontend not built yet")
    return FileResponse(path)


# check_dir=False: the parallel frontend agent may not have populated
# api/static/ yet when this service starts; StaticFiles must not crash on
# mount, it should just 404 individual requests until files show up.
app.mount("/static", StaticFiles(directory=str(STATIC_DIR), check_dir=False), name="static")
