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
import json
import logging
import os
import re
import secrets
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Set
from urllib.parse import quote

import httpx
from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import lj_package, mediamtx_client, mediamtx_stats
from .db import Database, NoFreeSlotError
from .state import FILLER, StateManager, iso_z

# Server-side rolling history for the admin/DJ stats charts (5 min of
# trend at a glance). Deliberately server-side and shared, not
# accumulated per-browser-tab: multiple viewers (admin + the DJ
# themself) see the same backfilled window immediately on open, rather
# than each starting from an empty chart. The tradeoff is explicit and
# accepted: this runs continuously for every currently-connected slot
# regardless of whether anyone has a panel open, unlike every other
# per-slot stats call in this file (those are on-demand,
# request-triggered only).
HISTORY_SAMPLE_INTERVAL_SECONDS = 5.0
HISTORY_WINDOW_SAMPLES = 60  # 60 * 5s = 5 minutes

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
    app.state.stat_history = {}  # slot -> deque[{"ts", "bitrate_kbps", "delay_seconds"}]

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
    mediamtx_hls_port = int(os.environ.get("ONAIR_MEDIAMTX_HLS_PORT", "8888"))
    app.state.mediamtx_base_url = f"http://{mediamtx_host}:{mediamtx_api_port}"
    app.state.mediamtx_hls_base_url = f"http://{mediamtx_host}:{mediamtx_hls_port}"
    # Shared client for the stats/preview paths (mediamtx_client.py's own
    # reconciliation loop keeps its own, shorter-lived client) - reused
    # across requests so connection pooling/keep-alive actually helps
    # during a burst of admin-panel polling.
    app.state.stats_client = httpx.AsyncClient()

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
    app.state.history_task = asyncio.create_task(_history_collector_loop())


async def _history_collector_loop() -> None:
    """Samples ingest bitrate + HLS delay for every currently-connected
    slot every HISTORY_SAMPLE_INTERVAL_SECONDS, appending into
    app.state.stat_history (bounded per-slot deques -- see
    HISTORY_WINDOW_SAMPLES). Powers the 5-minute trend charts on the
    admin/DJ stats views.

    A slot's history is dropped the moment it's no longer connected,
    rather than left to age out on its own: a disconnected DJ's stale
    trend isn't useful, and explicit deletion keeps memory bounded to
    currently-connected slots only instead of accumulating buffers for
    every DJ who has ever connected during the event.
    """
    while True:
        try:
            connected_usernames = set(app.state.state_manager.connected_since)
            slots_in_use: Set[str] = set()
            for username in connected_usernames:
                dj = app.state.db.get_dj(username)
                if dj and dj["slot"]:
                    slots_in_use.add(dj["slot"])

            for slot in slots_in_use:
                ingest = await mediamtx_stats.get_ingest_stats(
                    app.state.stats_client, app.state.mediamtx_base_url, slot
                )
                delay = await mediamtx_stats.get_hls_delay_seconds(
                    app.state.stats_client,
                    app.state.mediamtx_hls_base_url,
                    slot,
                    app.state.lj_read_username,
                    app.state.lj_read_password,
                    # Longer than the 6s default: nobody's waiting on a
                    # page load for this background tick, but a slot's
                    # on-demand HLS muxer (created lazily on first HLS
                    # request - see mediamtx_stats.py) can genuinely take
                    # longer than 6s to spin up and produce its first
                    # segment right after a DJ connects. Confirmed via a
                    # real RTMP publish: the 6s default intermittently
                    # timed out on the first couple of collector ticks
                    # for a freshly-connected slot even though the same
                    # call succeeds moments later once the muxer is warm.
                    timeout=15.0,
                )
                sample = {
                    "ts": iso_z(datetime.now(timezone.utc)),
                    "bitrate_kbps": ingest.get("bitrate_kbps") if ingest else None,
                    "delay_seconds": delay,
                }
                buf = app.state.stat_history.setdefault(slot, deque(maxlen=HISTORY_WINDOW_SAMPLES))
                buf.append(sample)

            for slot in list(app.state.stat_history.keys()):
                if slot not in slots_in_use:
                    del app.state.stat_history[slot]
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - this loop must never die
            logger.exception("history collector iteration failed")
        await asyncio.sleep(HISTORY_SAMPLE_INTERVAL_SECONDS)


@app.on_event("shutdown")
async def on_shutdown() -> None:
    task = getattr(app.state, "mediamtx_task", None)
    if task is not None:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    history_task = getattr(app.state, "history_task", None)
    if history_task is not None:
        history_task.cancel()
        try:
            await history_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    client = getattr(app.state, "stats_client", None)
    if client is not None:
        await client.aclose()


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


@app.get("/api/admin/info")
async def admin_info(request: Request) -> dict:
    # Static (never changes at runtime), so it's its own tiny endpoint
    # fetched once at boot rather than folded into /api/state - that dict
    # is polled/pushed every few seconds via /ws, and there's no reason to
    # carry a constant string along on every one of those messages.
    require_admin(request)
    return {"rtmp_server": f"rtmp://{app.state.rtmp_host}:{app.state.rtmp_port}"}


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
# Admin: per-connected-DJ stream stats + live preview
#
# Two separate things, both admin-only, both sourced from MediaMTX's
# control API/HLS output (see mediamtx_stats.py for exactly what's real
# and what isn't - in particular, no claimed end-to-end delay to VRCDN):
#   - a lightweight JSON stats endpoint (codec/resolution/bitrate/remote
#     address), cheap enough to poll periodically for every connected DJ
#     row at once;
#   - an HLS proxy for an actual live-preview <video>, which the admin
#     frontend only attaches on demand (per DJ, on click) since each open
#     preview keeps an on-demand HLS muxer running on mediamtx for as
#     long as it's open.
# ---------------------------------------------------------------------------

def _slot_history(slot: Optional[str]) -> list:
    # list(...) copies out of the live deque so the JSON encoder isn't
    # racing _history_collector_loop mutating it mid-request.
    if not slot:
        return []
    return list(app.state.stat_history.get(slot, []))


@app.get("/api/admin/stream/{username}")
async def admin_stream_stats(username: str, request: Request) -> dict:
    require_admin(request)
    dj = app.state.db.get_dj(username)
    if dj is None or not dj["ready"] or not dj["slot"]:
        return {"connected": False, "history": []}
    stats = await mediamtx_stats.get_ingest_stats(app.state.stats_client, app.state.mediamtx_base_url, dj["slot"])
    history = _slot_history(dj["slot"])
    if stats is None:
        return {"connected": False, "history": history}
    # Same "Verzoegerung DJ -> Server" figure shown on the DJ's own
    # dashboard - the admin needs this per-DJ to judge stream health
    # before switching, not just the DJ themself.
    stats["delay_seconds"] = await mediamtx_stats.get_hls_delay_seconds(
        app.state.stats_client,
        app.state.mediamtx_hls_base_url,
        dj["slot"],
        app.state.lj_read_username,
        app.state.lj_read_password,
    )
    # 5-minute trend, server-side sampled/stored - see
    # _history_collector_loop and HISTORY_SAMPLE_INTERVAL_SECONDS above.
    # Each sample: {"ts": "...Z", "bitrate_kbps": float|None,
    # "delay_seconds": float|None}.
    stats["history"] = history
    return stats


_SLOT_RE = re.compile(r"^slot[0-9]+$")


@app.get("/api/admin/preview/{slot}/{filename:path}")
async def admin_preview_proxy(slot: str, filename: str, request: Request) -> Response:
    require_admin(request)
    if not _SLOT_RE.match(slot):
        raise HTTPException(status_code=404, detail="not found")
    url = f"{app.state.mediamtx_hls_base_url}/{slot}/{filename}"
    query = request.url.query
    if query:
        url = f"{url}?{query}"
    try:
        resp = await app.state.stats_client.get(
            url,
            auth=(app.state.lj_read_username, app.state.lj_read_password),
            follow_redirects=True,
            timeout=10.0,
        )
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="preview unreachable")
    media_type = resp.headers.get("content-type", "application/octet-stream")
    return Response(content=resp.content, status_code=resp.status_code, media_type=media_type)


# ---------------------------------------------------------------------------
# Admin: LJ controller onboarding kit (pre-filled config, one-click zip, and
# a ready-to-import OBS scene collection) - see lj_package.py for exactly
# what's generated and, critically, the confidence-level breakdown on the
# reverse-engineered OBS scene collection format there.
# ---------------------------------------------------------------------------

def _public_api_base_url(request: Request) -> str:
    # Deliberately NOT request.base_url: this app's Dockerfile runs plain
    # `uvicorn` with no --proxy-headers/--forwarded-allow-ips, so Starlette
    # has no way to know the real public scheme/host Traefik terminated -
    # request.base_url would report the container-internal http://host:8080
    # seen on this side of the proxy, not the real https:// domain. The
    # raw Host header IS still the real public host, though (Traefik
    # forwards it unchanged) - and every documented deployment path for
    # this app already assumes Coolify terminates TLS on the public domain
    # (same assumption the README/CONCEPT.md make throughout), so https is
    # hardcoded rather than sniffed.
    host = request.headers.get("host") or request.url.hostname or "your-domain.example"
    return f"https://{host}/"


@app.get("/api/admin/lj/package.zip")
async def admin_lj_package(request: Request) -> Response:
    require_admin(request)
    api_base_url = _public_api_base_url(request)
    data = lj_package.build_lj_zip(api_base_url, app.state.lj_token, app.state.max_djs, _lj_rtsp_url)
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="segue-lj-controller.zip"'},
    )


@app.get("/api/admin/lj/obs-scene.json")
async def admin_lj_obs_scene(request: Request) -> Response:
    require_admin(request)
    data = lj_package.build_obs_scene_json(_lj_rtsp_url, app.state.max_djs)
    return Response(
        content=json.dumps(data, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="segue-obs-scene.json"'},
    )


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


@app.get("/api/dj/me/stream")
async def dj_own_stream_stats(request: Request) -> dict:
    # Own-slot connection quality for the "Verbindungsqualitaet" card. See
    # mediamtx_stats.py's module docstring for exactly what "Verzoegerung
    # DJ -> Server" does and does not measure - deliberately not marketed
    # as end-to-end/glass-to-glass, since this server can't see the LJ's
    # OBS or the push to VRCDN.
    username = get_identity(request)
    dj = app.state.db.get_dj(username)
    if dj is None or not dj["ready"] or not dj["slot"]:
        return {"connected": False, "history": []}
    stats = await mediamtx_stats.get_ingest_stats(app.state.stats_client, app.state.mediamtx_base_url, dj["slot"])
    history = _slot_history(dj["slot"])
    if stats is None:
        return {"connected": False, "history": history}
    stats["delay_seconds"] = await mediamtx_stats.get_hls_delay_seconds(
        app.state.stats_client,
        app.state.mediamtx_hls_base_url,
        dj["slot"],
        app.state.lj_read_username,
        app.state.lj_read_password,
    )
    stats["history"] = history
    return stats


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
    secret = request.headers.get("X-Onair-Secret", "")
    if not secrets.compare_digest(secret, app.state.internal_secret):
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
    # Unlike /internal/mediamtx/event (fired by our own runOnAvailable/
    # runOnUnavailable curl commands, where we control every header),
    # MediaMTX's built-in HTTP client makes *this* call itself and has no
    # config option to attach a custom header to it - confirmed against
    # the config reference. So the secret has to travel as a query param
    # baked into the configured authHTTPAddress URL instead (see
    # docker-compose.yaml's MTX_AUTHHTTPADDRESS override).
    secret = request.query_params.get("secret", "")
    if not secrets.compare_digest(secret, app.state.internal_secret):
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
