"""FastAPI app for SEGUE's api service: routes, static file serving,
websocket fan-out, and startup wiring of the state machine + MediaMTX
polling client.

Identity/auth model: this service sits entirely behind a Coolify/Traefik
proxy that runs Authentik forward-auth in front of it, so there is no login
form or session cookie here at all -- every request that reaches this app
is already an authenticated human, identified by a trusted request header
(ONAIR_AUTH_USERNAME_HEADER, default "X-authentik-username"). The app's own
job is only role differentiation: the one username in ONAIR_ADMIN_USERNAME
gets the admin API; everyone else is treated as a DJ, who self-registers on
first visit and starts out not-ready until the admin flips them on.

A third identity kind exists alongside admin/DJ: the LJ controller (see
lj-controller/), a bare script running on the event operator's own machine,
outside Authentik entirely. It authenticates with a static shared token
(ONAIR_LJ_TOKEN) instead of the proxy header.
"""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Set
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import mediamtx_client
from .db import Database, NoFreeSlotError
from .state import FILLER, StateManager

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


def require_lj(request: Request) -> None:
    token = request.headers.get("X-Onair-Lj-Token", "")
    if not secrets.compare_digest(token, app.state.lj_token):
        raise HTTPException(status_code=403, detail="forbidden")


def _ws_identity(websocket: WebSocket) -> Optional[str]:
    return websocket.headers.get(app.state.auth_header)


# ---------------------------------------------------------------------------
# Websocket connection registry + broadcast
# ---------------------------------------------------------------------------

class ConnectionManager:
    def __init__(self) -> None:
        self.admin_sockets: set[WebSocket] = set()
        self.dj_sockets: dict[WebSocket, str] = {}
        self.lj_sockets: set[WebSocket] = set()

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

        dead = []
        lj_state = _lj_state()
        for ws in list(self.lj_sockets):
            try:
                await ws.send_json(lj_state)
            except Exception:  # noqa: BLE001
                dead.append(ws)
        for ws in dead:
            self.lj_sockets.discard(ws)


manager = ConnectionManager()


# ---------------------------------------------------------------------------
# Slot <-> username translation (the only place that needs both the DB and
# MediaMTX's raw slot-name-shaped payloads)
# ---------------------------------------------------------------------------

def _connected_usernames(slot_names: Set[str]) -> Set[str]:
    """Translate a set of connected slot names (as reported by MediaMTX)
    into usernames. Prefers the in-memory slot_occupants map (populated at
    publish-auth time) over the DB, since a DJ revoked mid-set would no
    longer resolve via the DB's ready-only username_for_slot() lookup but
    should still register as "connected" until MediaMTX reports them gone
    -- mirrors how the old Liquidsoap closures captured the username at
    connect time rather than re-deriving it per lookup."""
    usernames = set()
    for slot in slot_names:
        user = app.state.slot_occupants.get(slot) or app.state.db.username_for_slot(slot)
        if user:
            usernames.add(user)
    return usernames


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
    app.state.slot_occupants = {}  # slot -> username, populated on publish auth

    app.state.rtmp_host = _env("ONAIR_RTMP_PUBLIC_HOST", "")
    app.state.rtmp_port = int(os.environ.get("ONAIR_RTMP_PUBLIC_PORT", "1935"))

    app.state.lj_token = _env("ONAIR_LJ_TOKEN")
    app.state.lj_read_username = _env("ONAIR_LJ_READ_USERNAME")
    app.state.lj_read_password = _env("ONAIR_LJ_READ_PASSWORD")
    app.state.rtsp_public_host = _env("ONAIR_RTSP_PUBLIC_HOST", "")
    app.state.rtsp_public_port = int(os.environ.get("ONAIR_RTSP_PUBLIC_PORT", "8554"))

    debounce_seconds = float(os.environ.get("ONAIR_DEBOUNCE_SECONDS", "2"))
    db_path = os.environ.get("ONAIR_DB_PATH", "/data/onair.db")
    mediamtx_host = os.environ.get("ONAIR_MEDIAMTX_HOST", "mediamtx")
    mediamtx_api_port = int(os.environ.get("ONAIR_MEDIAMTX_API_PORT", "9997"))
    app.state.mediamtx_base_url = f"http://{mediamtx_host}:{mediamtx_api_port}"

    db = Database(db_path)
    app.state.db = db

    mode, pinned = db.load_settings()

    state_manager = StateManager(
        db=db,
        debounce_seconds=debounce_seconds,
        on_broadcast=manager.broadcast,
        log_event=db.log_event,
        save_settings=db.save_settings,
    )
    state_manager.load_intentions(mode, pinned)
    app.state.state_manager = state_manager

    async def on_first_sync(slot_names: Set[str]) -> None:
        await state_manager.startup_sync(_connected_usernames(slot_names))

    async def on_alive_change(alive: bool) -> None:
        await state_manager.set_media_alive(alive)

    async def on_reconcile(slot_names: Set[str]) -> None:
        await state_manager.reconcile(_connected_usernames(slot_names))

    app.state.mediamtx_task = asyncio.create_task(
        mediamtx_client.run_forever(
            app.state.mediamtx_base_url, on_first_sync, on_alive_change, on_reconcile
        )
    )


@app.on_event("shutdown")
async def on_shutdown() -> None:
    task = getattr(app.state, "mediamtx_task", None)
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
    if not body.ready:
        # Free the slot's occupant record immediately -- otherwise a
        # revoked-but-still-transmitting encoder would keep translating to
        # this username via slot_occupants until MediaMTX notices the
        # publisher went away (it can't: our own auth check will reject the
        # next publish attempt, but an already-open connection isn't
        # retroactively kicked by this call).
        app.state.slot_occupants = {
            s: u for s, u in app.state.slot_occupants.items() if u != username
        }
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
    # MediaMTX takes RTMP publish credentials as query params on the
    # publish URL (there's no native username/password field in RTMP the
    # way RTSP has). This maps directly onto OBS's "Custom..." stream
    # service, which exposes exactly two fields: Server and Stream Key.
    stream_key = f"{dj['slot']}?user={quote(username)}&pass={quote(dj['password'])}"
    return {
        "rtmp_server": f"rtmp://{app.state.rtmp_host}:{app.state.rtmp_port}",
        "stream_key": stream_key,
        "format_hint": (
            "OBS: Einstellungen -> Stream -> Dienst 'Benutzerdefiniert...'. "
            "Server und Stream-Key oben eintragen. Video H.264, Audio AAC."
        ),
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
# LJ routes (the event operator's OBS-side controller -- see
# lj-controller/. Not an Authentik-authenticated human, so it carries its
# own static shared token instead of the trusted proxy header.)
# ---------------------------------------------------------------------------

def _lj_rtsp_url(slot: str) -> str:
    return (
        f"rtsp://{quote(app.state.lj_read_username)}:{quote(app.state.lj_read_password)}"
        f"@{app.state.rtsp_public_host}:{app.state.rtsp_public_port}/{slot}"
    )


def _lj_state() -> dict:
    state_manager: StateManager = app.state.state_manager
    full = state_manager.get_full_state()
    connected = set(state_manager.connected_since)
    djs = [
        {
            "username": dj["username"],
            "slot": dj["slot"],
            "connected": dj["username"] in connected,
            "rtsp_url": _lj_rtsp_url(dj["slot"]),
        }
        for dj in app.state.db.list_djs()
        if dj["ready"] and dj["slot"]
    ]
    return {
        "mode": full["mode"],
        "on_air": full["on_air"],
        "reason": full["reason"],
        "warning": full["warning"],
        "djs": djs,
        "server_time": full["server_time"],
    }


@app.get("/api/lj/state")
async def lj_state(request: Request) -> dict:
    require_lj(request)
    return _lj_state()


@app.websocket("/ws/lj")
async def ws_lj(websocket: WebSocket) -> None:
    token = websocket.headers.get("X-Onair-Lj-Token", "")
    if not secrets.compare_digest(token, app.state.lj_token):
        await websocket.close(code=4403)
        return
    await websocket.accept()
    manager.lj_sockets.add(websocket)
    try:
        await websocket.send_json(_lj_state())
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        pass
    finally:
        manager.lj_sockets.discard(websocket)


# ---------------------------------------------------------------------------
# Internal: MediaMTX webhook + auth-check (container-to-container only,
# never routed through the public domain / Authentik proxy)
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


@app.post("/internal/mediamtx/event")
async def mediamtx_event(request: Request) -> dict:
    secret = request.headers.get("X-Onair-Secret")
    if secret != app.state.internal_secret:
        _log_event_debounced(
            "secret-mismatch-event",
            "Interner Aufruf mit falschem/fehlendem Secret abgelehnt (mediamtx/event) - "
            "ONAIR_INTERNAL_SECRET zwischen mediamtx und api prüfen.",
        )
        raise HTTPException(status_code=403, detail="forbidden")
    body = await request.json()
    path = body.get("path")
    event = body.get("event")
    ts = body.get("ts")
    received_at = datetime.now(timezone.utc)
    # MediaMTX's hooks carry only the path, not a username (unlike
    # Liquidsoap's closures, which captured it at connect time) -- recover
    # it from the in-memory map populated during publish auth, falling back
    # to the DB for the case where `api` itself restarted mid-connection.
    username = app.state.slot_occupants.get(path) or app.state.db.username_for_slot(path)
    if event == "disconnect":
        app.state.slot_occupants.pop(path, None)
    logger.info("mediamtx event path=%s user=%s event=%s mediamtx_ts=%s", path, username, event, ts)
    # Never block the webhook response on the debounce/resolve pipeline.
    asyncio.create_task(
        app.state.state_manager.handle_webhook_event(username, event, received_at)
    )
    return {"ok": True}


@app.post("/internal/mediamtx/auth")
async def mediamtx_auth(request: Request) -> Response:
    secret = request.headers.get("X-Onair-Secret")
    if secret != app.state.internal_secret:
        _log_event_debounced(
            "secret-mismatch-auth",
            "Interner Aufruf mit falschem/fehlendem Secret abgelehnt (mediamtx/auth) - "
            "ONAIR_INTERNAL_SECRET zwischen mediamtx und api prüfen.",
        )
        raise HTTPException(status_code=403, detail="forbidden")

    body = await request.json()
    action = body.get("action", "")
    path = body.get("path", "")
    user = body.get("user", "")
    password = body.get("password", "")
    ip = body.get("ip", "")

    if action == "publish":
        # A DJ's own valid credentials must also match the slot they were
        # actually assigned -- unlike Liquidsoap's per-slot auth closures,
        # a single global endpoint now handles every slot, so nothing else
        # stops a valid DJ from being sent to (or guessing) someone else's
        # path.
        ok = app.state.db.check_credentials(user, password) and app.state.db.get_slot(user) == path
        if ok:
            app.state.slot_occupants[path] = user
        else:
            # CONCEPT.md §10: "häufigster Supportfall am Abend" -- make
            # wrong credentials visible in the admin log with who and
            # where from.
            reason = "unbekannter Benutzer" if not app.state.db.dj_exists(user) else (
                "nicht freigeschaltet" if not app.state.db.is_ready(user) else
                "falsches Passwort/Slot"
            )
            _log_event_debounced(
                f"auth-fail-{user}-{ip}",
                f"RTMP-Login fehlgeschlagen: {user!r} von {ip} auf {path!r} ({reason})",
            )
    elif action == "read":
        ok = secrets.compare_digest(user, app.state.lj_read_username) and secrets.compare_digest(
            password, app.state.lj_read_password
        )
        if not ok:
            _log_event_debounced(
                f"auth-fail-read-{ip}",
                f"RTSP-Lesezugriff abgelehnt: {ip} auf {path!r}",
            )
    else:
        ok = False

    return Response(status_code=200 if ok else 403)


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
