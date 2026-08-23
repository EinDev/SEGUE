"""FastAPI app for SEGUE's api service: routes, static file serving,
websocket fan-out, and startup wiring of the state machine + telnet client.

Identity/auth model: this service sits entirely behind a Coolify/Traefik
proxy that runs Authentik forward-auth in front of it, so there is no login
form or session cookie here at all -- every request that reaches this app
is already an authenticated human, identified by a trusted request header
(ONAIR_AUTH_USERNAME_HEADER, default "X-authentik-username"). The app's own
job is only role differentiation: the one username in ONAIR_ADMIN_USERNAME
gets the admin API; everyone else is treated as a DJ, who self-registers on
first visit and starts out not-ready until the admin flips them on.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Set

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .db import Database, NoFreeSlotError
from .state import FILLER, StateManager
from .telnet_client import TelnetClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("segue.api")

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"


def _env(name: str, default: Optional[str] = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise RuntimeError(f"missing required environment variable: {name}")
    return value


app = FastAPI(title="SEGUE api")


# ---------------------------------------------------------------------------
# Identity (trusted header from the Authentik forward-auth proxy)
# ---------------------------------------------------------------------------

def get_identity(request: Request) -> str:
    username = request.headers.get(app.state.auth_header)
    if not username:
        raise HTTPException(
            status_code=401,
            detail=f"no {app.state.auth_header!r} header -- is this behind the Authentik proxy?",
        )
    return username


def require_admin(request: Request) -> str:
    username = get_identity(request)
    if username != app.state.admin_username:
        raise HTTPException(status_code=403, detail="not the admin")
    return username


def _ws_identity(websocket: WebSocket) -> Optional[str]:
    return websocket.headers.get(app.state.auth_header)


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
        for ws, username in list(self.dj_sockets.items()):
            dj_state = state_manager.get_dj_state(username)
            if dj_state is None:
                dead.append(ws)
                continue
            dj_state["dj"]["credentials"] = _dj_credentials(username)
            try:
                await ws.send_json(dj_state)
            except Exception:  # noqa: BLE001
                dead.append(ws)
        for ws in dead:
            self.dj_sockets.pop(ws, None)


manager = ConnectionManager()


# ---------------------------------------------------------------------------
# Slot <-> username translation (the only place that needs both the DB and
# the telnet client's raw slot-shaped payloads)
# ---------------------------------------------------------------------------

def _connected_usernames(slots: dict) -> Set[str]:
    return {info["user"] for info in slots.values() if info.get("connected") and info.get("user")}


# ---------------------------------------------------------------------------
# Startup / shutdown
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def on_startup() -> None:
    app.state.admin_username = _env("ONAIR_ADMIN_USERNAME")
    app.state.auth_header = os.environ.get("ONAIR_AUTH_USERNAME_HEADER", "X-authentik-username")
    app.state.internal_secret = _env("ONAIR_INTERNAL_SECRET")
    app.state.max_djs = int(os.environ.get("ONAIR_MAX_DJS", "6"))
    app.state.log_debounce = {}
    harbor_host = _env("ONAIR_HARBOR_PUBLIC_HOST", "")
    harbor_port = int(os.environ.get("ONAIR_HARBOR_PUBLIC_PORT", "8005"))
    debounce_seconds = float(os.environ.get("ONAIR_DEBOUNCE_SECONDS", "2"))
    db_path = os.environ.get("ONAIR_DB_PATH", "/data/onair.db")
    liquidsoap_host = os.environ.get("ONAIR_LIQUIDSOAP_HOST", "liquidsoap")
    liquidsoap_port = int(os.environ.get("ONAIR_LIQUIDSOAP_TELNET_PORT", "1234"))

    db = Database(db_path)
    app.state.db = db
    app.state.harbor_host = harbor_host
    app.state.harbor_port = harbor_port

    telnet = TelnetClient(liquidsoap_host, liquidsoap_port)
    app.state.telnet = telnet

    mode, pinned = db.load_settings()

    async def telnet_set_target(dj_id: str) -> None:
        if dj_id == FILLER:
            await telnet.set_target(FILLER)
            return
        slot = db.get_slot(dj_id)
        if slot is None:
            raise RuntimeError(f"{dj_id} has no assigned slot (not ready?)")
        await telnet.set_target(slot)

    state_manager = StateManager(
        db=db,
        debounce_seconds=debounce_seconds,
        telnet_set_target=telnet_set_target,
        on_broadcast=manager.broadcast,
        log_event=db.log_event,
        save_settings=db.save_settings,
    )
    state_manager.load_intentions(mode, pinned)
    app.state.state_manager = state_manager

    async def on_connect(is_first: bool) -> None:
        slots, target_slot = await telnet.status()
        connected = _connected_usernames(slots)
        if is_first:
            if target_slot == FILLER:
                target_username = FILLER
            else:
                target_username = db.username_for_slot(target_slot) or FILLER
            await state_manager.startup_sync(connected, target_username)
        else:
            await state_manager.reconcile(connected)

    async def on_alive_change(alive: bool) -> None:
        await state_manager.set_telnet_alive(alive)

    async def on_reconcile(slots: dict) -> None:
        await state_manager.reconcile(_connected_usernames(slots))

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
    username: str


@app.post("/api/pin")
async def post_pin(body: PinRequest, request: Request) -> dict:
    require_admin(request)
    try:
        await app.state.state_manager.set_pin(body.username)
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


# ---------------------------------------------------------------------------
# Admin: DJ roster management (registration + ready approval)
# ---------------------------------------------------------------------------

@app.get("/api/djs")
async def list_djs(request: Request) -> list:
    require_admin(request)
    connected = set(app.state.state_manager.connected_since)
    return [
        {
            "username": dj["username"],
            "ready": bool(dj["ready"]),
            "slot": dj["slot"],
            "connected": dj["username"] in connected,
            "created_at": dj["created_at"],
        }
        for dj in app.state.db.list_djs()
    ]


class ReadyRequest(BaseModel):
    ready: bool


@app.post("/api/djs/{username}/ready")
async def set_dj_ready(username: str, body: ReadyRequest, request: Request) -> dict:
    require_admin(request)
    if app.state.db.get_dj(username) is None:
        raise HTTPException(status_code=404, detail="unknown dj")
    try:
        slot = app.state.db.set_ready(username, body.ready, app.state.max_djs)
    except NoFreeSlotError:
        raise HTTPException(
            status_code=409,
            detail=f"Alle {app.state.max_djs} Slots sind belegt - zuerst einen anderen DJ deaktivieren.",
        )
    app.state.db.log_event(
        f"{username} freigeschaltet (Slot {slot})" if body.ready else f"{username} deaktiviert"
    )
    return {"username": username, "ready": body.ready, "slot": slot}


@app.websocket("/ws")
async def ws_admin(websocket: WebSocket) -> None:
    username = _ws_identity(websocket)
    if not username or username != app.state.admin_username:
        await websocket.close(code=4403)
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

def _dj_credentials(username: str) -> Optional[dict]:
    dj = app.state.db.get_dj(username)
    if dj is None or not dj["ready"] or not dj["slot"]:
        return None
    return {
        "host": app.state.harbor_host,
        "port": app.state.harbor_port,
        "mount": dj["slot"],
        "user": username,
        "password": dj["password"],
        "format_hint": "MP3 320kbps oder Ogg Vorbis",
    }


@app.get("/dj")
async def dj_view(request: Request) -> FileResponse:
    get_identity(request)  # just ensure the proxy actually authenticated someone
    path = STATIC_DIR / "dj" / "index.html"
    if not path.exists():
        raise HTTPException(status_code=404, detail="dj frontend not built yet")
    return FileResponse(path)


@app.get("/api/dj/me")
async def dj_state(request: Request) -> dict:
    username = get_identity(request)
    app.state.db.get_or_create_dj(username)  # self-register on first visit
    state = app.state.state_manager.get_dj_state(username)
    state["dj"]["credentials"] = _dj_credentials(username)
    return state


@app.websocket("/ws/dj")
async def ws_dj(websocket: WebSocket) -> None:
    username = _ws_identity(websocket)
    if not username:
        await websocket.close(code=4401)
        return
    app.state.db.get_or_create_dj(username)
    await websocket.accept()
    manager.dj_sockets[websocket] = username
    try:
        initial = app.state.state_manager.get_dj_state(username)
        if initial is not None:
            initial["dj"]["credentials"] = _dj_credentials(username)
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
# Internal: harbor webhook + auth-check (container-to-container only, never
# routed through the public domain / Authentik proxy)
# ---------------------------------------------------------------------------

def _log_event_debounced(key: str, message: str, cooldown: float = 30.0) -> None:
    """Log an eventlog entry, but at most once per `key` per `cooldown`
    seconds. A rejected connection (wrong password, mismatched internal
    secret) can retry every few seconds indefinitely from a misconfigured
    or stubborn encoder -- without this, that alone would drown out every
    other entry in the admin's event log."""
    now = asyncio.get_running_loop().time()
    last = app.state.log_debounce.get(key, 0.0)
    if now - last >= cooldown:
        app.state.log_debounce[key] = now
        app.state.db.log_event(message)


@app.post("/internal/harbor/event")
async def harbor_event(request: Request) -> dict:
    secret = request.headers.get("X-Onair-Secret")
    if secret != app.state.internal_secret:
        _log_event_debounced(
            "secret-mismatch-event",
            "Interner Aufruf mit falschem/fehlendem Secret abgelehnt (harbor/event) - "
            "ONAIR_INTERNAL_SECRET zwischen liquidsoap und api prüfen.",
        )
        raise HTTPException(status_code=403, detail="forbidden")
    body = await request.json()
    username = body.get("user")
    slot = body.get("slot")
    event = body.get("event")
    ts = body.get("ts")
    received_at = datetime.now(timezone.utc)
    logger.info("harbor event slot=%s user=%s event=%s liquidsoap_ts=%s", slot, username, event, ts)
    # Never block the webhook response on the debounce/resolve pipeline.
    asyncio.create_task(
        app.state.state_manager.handle_webhook_event(username, event, received_at)
    )
    return {"ok": True}


@app.get("/internal/harbor/auth", response_class=PlainTextResponse)
async def harbor_auth(request: Request, user: str = "", password: str = "", address: str = "") -> str:
    secret = request.headers.get("X-Onair-Secret")
    if secret != app.state.internal_secret:
        _log_event_debounced(
            "secret-mismatch-auth",
            "Interner Aufruf mit falschem/fehlendem Secret abgelehnt (harbor/auth) - "
            "ONAIR_INTERNAL_SECRET zwischen liquidsoap und api prüfen.",
        )
        raise HTTPException(status_code=403, detail="forbidden")
    ok = app.state.db.check_credentials(user, password)
    if not ok:
        # CONCEPT.md §10: "häufigster Supportfall am Abend" -- make wrong
        # credentials visible in the admin log with who and where from.
        reason = "unbekannter Benutzer" if not app.state.db.dj_exists(user) else (
            "nicht freigeschaltet" if not app.state.db.is_ready(user) else "falsches Passwort"
        )
        _log_event_debounced(
            f"auth-fail-{user}-{address}",
            f"Harbor-Login fehlgeschlagen: {user!r} von {address} ({reason})",
        )
    return "true" if ok else "false"


# ---------------------------------------------------------------------------
# Frontend serving
# ---------------------------------------------------------------------------

@app.get("/")
async def admin_view() -> FileResponse:
    path = STATIC_DIR / "admin" / "index.html"
    if not path.exists():
        raise HTTPException(status_code=404, detail="admin frontend not built yet")
    return FileResponse(path)


app.mount("/static", StaticFiles(directory=str(STATIC_DIR), check_dir=False), name="static")
