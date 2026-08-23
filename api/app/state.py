"""State resolution (CONCEPT.md §4) and debounce orchestration.

``resolve()`` is a pure function with zero I/O -- it is the thing
tests/test_state.py exercises directly. Everything else in this module
(``StateManager``) wires that pure function up to the MediaMTX polling
client, SQLite persistence, and websocket broadcast (admin, DJ, and LJ
controller sockets), including the 2s debounce for connect/disconnect
events described in CONCEPT.md §4.4 / the task prompt §5.

Unlike the old Liquidsoap-backed version, StateManager never pushes a
decision anywhere -- MediaMTX is a dumb relay with no "on air" concept of
its own, so switching happens entirely client-side in the LJ's OBS, driven
by broadcasting on_air over /ws/lj. This module's only output is that
broadcast.

FILLER is represented as the literal string "FILLER" throughout, matching
the wire protocol used by the REST/WS API.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable, Dict, Optional, Set

FILLER = "FILLER"


def resolve(
    mode: str,
    pinned: Optional[str],
    connected: Set[str],
    current_on_air: str,
) -> tuple[str, Optional[str]]:
    """Pure resolution function per CONCEPT.md §4.2.

    Returns (new_on_air, warning_or_None). No I/O, no side effects, fully
    deterministic given its inputs -- safe to call as often as needed.
    """
    connected = set(connected)

    if mode == "MANUAL":
        if pinned is not None and pinned in connected:
            return pinned, None
        if pinned is not None:
            # Pinned DJ is configured but not currently connected.
            return FILLER, "Gepinnter DJ offline, Filler läuft"
        # No pin set yet -- Filler plays until the operator pins someone.
        return FILLER, None

    if mode == "AUTO":
        if len(connected) == 0:
            return FILLER, None
        if len(connected) == 1:
            return next(iter(connected)), None
        # Multiple connected.
        if current_on_air in connected:
            return current_on_air, None
        return FILLER, "Mehrere verbunden, keine eindeutige Auswahl im Auto-Modus"

    raise ValueError(f"unknown mode: {mode!r}")


def compute_reason(
    mode: str,
    on_air: str,
    pinned: Optional[str],
    connected: Set[str],
) -> str:
    """Human-readable German sentence per CONCEPT.md §6.4.

    Usernames double as their own display name (no separate "name" field
    since identities now come from Authentik, not a hand-authored roster).
    """
    if mode == "MANUAL":
        if pinned is None:
            if on_air == FILLER:
                return "Manuell: kein Pin gesetzt, Filler läuft"
            return f"Manuell auf {on_air}"
        if on_air == pinned:
            return f"Manuell gepinnt auf {pinned}"
        return "Gepinnter DJ offline, Filler läuft"

    # AUTO
    if on_air == FILLER:
        if len(connected) == 0:
            return "Auto: niemand verbunden, Filler läuft"
        return "Auto: mehrere verbunden, keine eindeutige Auswahl, Filler läuft"
    if len(connected) == 1:
        return f"Auto: nur {on_air} verbunden"
    return f"Auto: {on_air} on air"


def iso_z(dt: datetime) -> str:
    dt = dt.astimezone(timezone.utc)
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


BroadcastCb = Callable[[], Awaitable[None]]
LogFn = Callable[[str], None]


@dataclass
class StateManager:
    db: object  # app.db.Database -- duck-typed here to avoid a circular import
    debounce_seconds: float
    on_broadcast: BroadcastCb
    log_event: LogFn
    save_settings: Callable[[str, Optional[str]], None]

    mode: str = "AUTO"
    pinned: Optional[str] = None
    on_air: str = FILLER
    media_alive: bool = False

    connected_since: Dict[str, datetime] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _debounce_task: Optional[asyncio.Task] = None

    # -- helpers -----------------------------------------------------

    def _resolve_warning(self) -> Optional[str]:
        if not self.media_alive:
            return "Verbindung zu MediaMTX tot"
        _, warning = resolve(self.mode, self.pinned, set(self.connected_since), self.on_air)
        return warning

    def get_full_state(self) -> dict:
        connected = set(self.connected_since)
        reason = compute_reason(self.mode, self.on_air, self.pinned, connected)
        warning = self._resolve_warning()
        djs = [
            {
                "username": u,
                "connected": u in connected,
                "since": iso_z(self.connected_since[u]) if u in connected else None,
            }
            for u in self.db.list_ready_usernames()
        ]
        return {
            "mode": self.mode,
            "pinned": self.pinned,
            "on_air": self.on_air,
            "reason": reason,
            "warning": warning,
            "djs": djs,
            "server_time": iso_z(datetime.now(timezone.utc)),
        }

    def get_dj_state(self, username: str) -> Optional[dict]:
        dj = self.db.get_dj(username)
        if dj is None:
            return None
        connected = set(self.connected_since)
        reason = compute_reason(self.mode, self.on_air, self.pinned, connected)
        djs = [
            {"username": u, "connected": u in connected} for u in self.db.list_ready_usernames()
        ]
        return {
            "dj": {
                "username": username,
                "ready": bool(dj["ready"]),
                "connected": username in connected,
                "since": iso_z(self.connected_since[username]) if username in connected else None,
                "on_air": self.on_air == username,
            },
            "mode": self.mode,
            "on_air": self.on_air,
            "reason": reason,
            "djs": djs,
            "server_time": iso_z(datetime.now(timezone.utc)),
        }

    # -- lifecycle -----------------------------------------------------

    def load_intentions(self, mode: str, pinned: Optional[str]) -> None:
        """Load mode/pinned from SQLite at startup, before MediaMTX connects."""
        self.mode = mode
        self.pinned = pinned

    async def startup_sync(self, connected: Set[str]) -> None:
        """One-shot immediate sync on process startup (no debounce).

        Seeds ``connected`` from MediaMTX's control API (already translated
        from slot ids to usernames by the caller) and resolves once against
        the FILLER baseline -- MediaMTX has no "current target" concept to
        sync from the way Liquidsoap did (it never performs switching), so
        there is nothing to push back: resolve()'s existing "multiple
        connected, ambiguous -- don't guess" rule already does the right
        thing starting from FILLER.
        """
        async with self._lock:
            now = datetime.now(timezone.utc)
            self.connected_since = {u: now for u in connected}
            self.on_air = FILLER
            new_on_air, _ = resolve(self.mode, self.pinned, set(self.connected_since), self.on_air)
            self.on_air = new_on_air
            self.media_alive = True
            self.log_event("Startup-Synchronisation abgeschlossen")
            await self.on_broadcast()

    async def set_media_alive(self, alive: bool) -> None:
        async with self._lock:
            if alive == self.media_alive:
                return
            self.media_alive = alive
            self.log_event(
                "Verbindung zu MediaMTX wiederhergestellt"
                if alive
                else "Verbindung zu MediaMTX verloren"
            )
            await self.on_broadcast()

    # -- reconciliation (safety net for missed webhooks) ---------------

    async def reconcile(self, connected: Set[str]) -> None:
        """Treat any discrepancy vs in-memory `connected` like a fresh
        connect/disconnect event, through the same debounced pipeline.

        `connected` is the full set of usernames MediaMTX currently reports
        as live on some slot (already translated by the caller).
        """
        mismatches = []
        async with self._lock:
            all_known = connected | set(self.connected_since)
            for username in all_known:
                remote = username in connected
                local = username in self.connected_since
                if remote != local:
                    mismatches.append((username, remote))
        for username, remote in mismatches:
            self.log_event(
                f"Reconciliation: {username} {'verbunden' if remote else 'getrennt'} "
                "(Status-Poll, evtl. verpasster Webhook)"
            )
            await self._apply_connected_change(username, remote)

    # -- webhook-driven events ------------------------------------------

    async def handle_webhook_event(self, username: str, event: str, received_at: datetime) -> None:
        if not username:
            self.log_event("Webhook ohne Benutzername ignoriert (Slot war nicht zugeordnet)")
            return
        if not self.db.dj_exists(username):
            # Shouldn't normally happen -- Liquidsoap's auth callback already
            # required a ready DB row to accept the connection in the first
            # place. Defensive only (e.g. a DJ row deleted mid-connection).
            self.log_event(f"Unbekannter Benutzer im Webhook ignoriert: {username!r}")
            return
        if event not in ("connect", "disconnect"):
            self.log_event(f"Unbekanntes Event im Webhook ignoriert: {event!r}")
            return
        self.log_event(f"{username} {'verbunden' if event == 'connect' else 'getrennt'}")
        await self._apply_connected_change(username, event == "connect", at=received_at)

    async def _apply_connected_change(
        self, dj_id: str, connected: bool, at: Optional[datetime] = None
    ) -> None:
        at = at or datetime.now(timezone.utc)
        async with self._lock:
            if connected:
                self.connected_since[dj_id] = at
            else:
                self.connected_since.pop(dj_id, None)
            await self.on_broadcast()  # raw connected state must reflect immediately

            prospective, _ = resolve(self.mode, self.pinned, set(self.connected_since), self.on_air)
            bypass_debounce = connected and self.on_air == FILLER and prospective != FILLER
            if bypass_debounce:
                self._cancel_pending_resolve()
                await self._resolve_and_apply_locked()
            else:
                self._schedule_debounced_resolve()

    def _cancel_pending_resolve(self) -> None:
        if self._debounce_task is not None and not self._debounce_task.done():
            self._debounce_task.cancel()
        self._debounce_task = None

    def _schedule_debounced_resolve(self) -> None:
        self._cancel_pending_resolve()
        self._debounce_task = asyncio.create_task(self._debounced_resolve_worker())

    async def _debounced_resolve_worker(self) -> None:
        try:
            await asyncio.sleep(self.debounce_seconds)
        except asyncio.CancelledError:
            return
        async with self._lock:
            await self._resolve_and_apply_locked()

    async def _resolve_and_apply_locked(self) -> None:
        """Must be called with self._lock held."""
        new_on_air, _ = resolve(self.mode, self.pinned, set(self.connected_since), self.on_air)
        if new_on_air != self.on_air:
            old = self.on_air
            self.on_air = new_on_air
            self.log_event(f"On Air: {old} -> {new_on_air}")
            # Nothing to push anywhere -- switching now happens client-side
            # in the LJ's OBS, driven by broadcasting the new on_air value
            # over /ws/lj (see app.main). on_broadcast() below is the whole
            # notification.
        await self.on_broadcast()

    # -- operator-driven events (immediate, no debounce) -----------------

    async def set_mode(self, new_mode: str) -> None:
        if new_mode not in ("AUTO", "MANUAL"):
            raise ValueError(f"invalid mode: {new_mode!r}")
        async with self._lock:
            old_mode = self.mode
            if new_mode == "MANUAL":
                if self.pinned is None and self.on_air != FILLER and self.db.is_ready(self.on_air):
                    self.pinned = self.on_air
            elif new_mode == "AUTO":
                self.pinned = None
            self.mode = new_mode
            self.save_settings(self.mode, self.pinned)
            if old_mode != new_mode:
                self.log_event(f"Modus geändert: {old_mode} -> {new_mode}")
            self._cancel_pending_resolve()
            await self._resolve_and_apply_locked()

    async def set_pin(self, username: str) -> None:
        if not self.db.is_ready(username):
            raise ValueError(f"{username} is not a ready/enabled DJ")
        async with self._lock:
            self.mode = "MANUAL"
            self.pinned = username
            self.save_settings(self.mode, self.pinned)
            self.log_event(f"Pin gesetzt auf {username}")
            self._cancel_pending_resolve()
            await self._resolve_and_apply_locked()

    async def force_filler(self) -> None:
        async with self._lock:
            self.mode = "MANUAL"
            self.pinned = None
            self.save_settings(self.mode, self.pinned)
            self.log_event("Filler erzwungen")
            self._cancel_pending_resolve()
            await self._resolve_and_apply_locked()
