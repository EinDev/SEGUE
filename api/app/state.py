"""State resolution (CONCEPT.md §4) and debounce orchestration.

``resolve()`` is a pure function with zero I/O -- it is the thing
tests/test_state.py exercises directly. Everything else in this module
(``StateManager``) wires that pure function up to the telnet client, SQLite
persistence, and websocket broadcast, including the 2s debounce for
connect/disconnect events described in CONCEPT.md §4.4 / the task prompt §5.

FILLER is represented as the literal string "FILLER" throughout, matching
the wire protocol used by both the telnet contract and the REST API.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable, Dict, List, Optional, Set

FILLER = "FILLER"

logger = logging.getLogger("segue.state")


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
    dj_names: Dict[str, str],
) -> str:
    """Human-readable German sentence per CONCEPT.md §6.4."""

    def name(dj_id: str) -> str:
        return dj_names.get(dj_id, dj_id)

    if mode == "MANUAL":
        if pinned is None:
            if on_air == FILLER:
                return "Manuell: kein Pin gesetzt, Filler läuft"
            return f"Manuell auf {name(on_air)}"
        if on_air == pinned:
            return f"Manuell gepinnt auf {name(pinned)}"
        return "Gepinnter DJ offline, Filler läuft"

    # AUTO
    if on_air == FILLER:
        if len(connected) == 0:
            return "Auto: niemand verbunden, Filler läuft"
        return "Auto: mehrere verbunden, keine eindeutige Auswahl, Filler läuft"
    if len(connected) == 1:
        return f"Auto: nur {name(on_air)} verbunden"
    return f"Auto: {name(on_air)} on air"


def iso_z(dt: datetime) -> str:
    dt = dt.astimezone(timezone.utc)
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


TelnetSetTarget = Callable[[str], Awaitable[None]]
BroadcastCb = Callable[[], Awaitable[None]]
LogFn = Callable[[str], None]


@dataclass
class StateManager:
    djs: List  # List[DjConfig] from app.config
    debounce_seconds: float
    telnet_set_target: TelnetSetTarget
    on_broadcast: BroadcastCb
    log_event: LogFn
    save_settings: Callable[[str, Optional[str]], None]

    mode: str = "AUTO"
    pinned: Optional[str] = None
    on_air: str = FILLER
    telnet_alive: bool = False

    connected_since: Dict[str, datetime] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _debounce_task: Optional[asyncio.Task] = None

    def __post_init__(self) -> None:
        self._dj_ids = {dj.id for dj in self.djs}
        self._dj_names = {dj.id: dj.name for dj in self.djs}

    # -- helpers -----------------------------------------------------

    def _resolve_warning(self) -> Optional[str]:
        if not self.telnet_alive:
            return "Telnet-Verbindung zu Liquidsoap tot"
        _, warning = resolve(self.mode, self.pinned, set(self.connected_since), self.on_air)
        return warning

    def get_full_state(self) -> dict:
        connected = set(self.connected_since)
        reason = compute_reason(self.mode, self.on_air, self.pinned, connected, self._dj_names)
        warning = self._resolve_warning()
        djs = [
            {
                "id": dj.id,
                "name": dj.name,
                "connected": dj.id in connected,
                "since": iso_z(self.connected_since[dj.id]) if dj.id in connected else None,
            }
            for dj in self.djs
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

    def get_dj_state(self, dj_id: str) -> Optional[dict]:
        if dj_id not in self._dj_ids:
            return None
        connected = set(self.connected_since)
        reason = compute_reason(self.mode, self.on_air, self.pinned, connected, self._dj_names)
        dj = next(d for d in self.djs if d.id == dj_id)
        djs = [{"id": d.id, "name": d.name, "connected": d.id in connected} for d in self.djs]
        return {
            "dj": {
                "id": dj.id,
                "name": dj.name,
                "connected": dj.id in connected,
                "since": iso_z(self.connected_since[dj.id]) if dj.id in connected else None,
                "on_air": self.on_air == dj.id,
            },
            "mode": self.mode,
            "on_air": self.on_air,
            "reason": reason,
            "djs": djs,
            "server_time": iso_z(datetime.now(timezone.utc)),
        }

    # -- lifecycle -----------------------------------------------------

    def load_intentions(self, mode: str, pinned: Optional[str]) -> None:
        """Load mode/pinned from SQLite at startup, before telnet connects."""
        self.mode = mode
        self.pinned = pinned

    async def startup_sync(self, ready_map: Dict[str, bool], remote_target: str) -> None:
        """One-shot immediate sync on process startup (no debounce).

        Seeds ``connected`` from Liquidsoap's status, resolves once using
        Liquidsoap's current target as the baseline on_air, and pushes the
        result via onair.set if it differs.
        """
        async with self._lock:
            now = datetime.now(timezone.utc)
            self.connected_since = {dj_id: now for dj_id, ready in ready_map.items() if ready}
            self.on_air = remote_target if remote_target else FILLER
            new_on_air, _ = resolve(self.mode, self.pinned, set(self.connected_since), self.on_air)
            self.on_air = new_on_air
            self.telnet_alive = True
            if new_on_air != remote_target:
                await self._push_target(new_on_air)
            self.log_event("Startup-Synchronisation abgeschlossen")
            await self.on_broadcast()

    async def set_telnet_alive(self, alive: bool) -> None:
        async with self._lock:
            if alive == self.telnet_alive:
                return
            self.telnet_alive = alive
            self.log_event(
                "Telnet-Verbindung zu Liquidsoap wiederhergestellt"
                if alive
                else "Telnet-Verbindung zu Liquidsoap verloren"
            )
            await self.on_broadcast()

    # -- reconciliation (safety net for missed webhooks) ---------------

    async def reconcile(self, ready_map: Dict[str, bool]) -> None:
        """Treat any discrepancy vs in-memory `connected` like a fresh
        connect/disconnect event, through the same debounced pipeline."""
        mismatches = []
        async with self._lock:
            for dj_id, ready in ready_map.items():
                if dj_id not in self._dj_ids:
                    continue
                local = dj_id in self.connected_since
                if ready != local:
                    mismatches.append((dj_id, ready))
        for dj_id, ready in mismatches:
            self.log_event(
                f"Reconciliation: {dj_id} {'verbunden' if ready else 'getrennt'} "
                "(Status-Poll, evtl. verpasster Webhook)"
            )
            await self._apply_connected_change(dj_id, ready)

    # -- webhook-driven events ------------------------------------------

    async def handle_webhook_event(self, dj_id: str, event: str, received_at: datetime) -> None:
        if dj_id not in self._dj_ids:
            self.log_event(f"Unbekannte dj_id im Webhook ignoriert: {dj_id!r}")
            return
        if event not in ("connect", "disconnect"):
            self.log_event(f"Unbekanntes Event im Webhook ignoriert: {event!r}")
            return
        name = self._dj_names.get(dj_id, dj_id)
        self.log_event(f"{name} {'verbunden' if event == 'connect' else 'getrennt'}")
        await self._apply_connected_change(dj_id, event == "connect", at=received_at)

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
            await self._push_target(new_on_air)
        await self.on_broadcast()

    async def _push_target(self, value: str) -> None:
        try:
            await self.telnet_set_target(value)
        except Exception as exc:  # noqa: BLE001 - must never crash the resolver
            logger.warning("telnet onair.set failed: %s", exc)
            self.log_event(f"Telnet-Befehl onair.set {value} fehlgeschlagen: {exc}")

    # -- operator-driven events (immediate, no debounce) -----------------

    async def set_mode(self, new_mode: str) -> None:
        if new_mode not in ("AUTO", "MANUAL"):
            raise ValueError(f"invalid mode: {new_mode!r}")
        async with self._lock:
            old_mode = self.mode
            if new_mode == "MANUAL":
                if self.pinned is None and self.on_air != FILLER and self.on_air in self._dj_ids:
                    self.pinned = self.on_air
            elif new_mode == "AUTO":
                self.pinned = None
            self.mode = new_mode
            self.save_settings(self.mode, self.pinned)
            if old_mode != new_mode:
                self.log_event(f"Modus geändert: {old_mode} -> {new_mode}")
            self._cancel_pending_resolve()
            await self._resolve_and_apply_locked()

    async def set_pin(self, dj_id: str) -> None:
        if dj_id not in self._dj_ids:
            raise ValueError(f"unknown dj_id: {dj_id!r}")
        async with self._lock:
            self.mode = "MANUAL"
            self.pinned = dj_id
            self.save_settings(self.mode, self.pinned)
            self.log_event(f"Pin gesetzt auf {self._dj_names.get(dj_id, dj_id)}")
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
