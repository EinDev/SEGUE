"""Persistent asyncio telnet client for the Liquidsoap onair.* commands.

Protocol (verified live against the real container): plain-text, line
based. A command is a single line terminated with "\n". The server replies
with zero or more payload lines followed by a line containing exactly
"END". There is no other framing.

Two custom commands are registered on the Liquidsoap side:

  onair.set <slot-id|FILLER>  -> one line with the new target, then END
  onair.status                -> one line of compact JSON, then END
      {"target": "slot2", "slots": {"slot1": {"connected": false, "user": ""},
                                     "slot2": {"connected": true, "user": "eindev"}}}

This module is deliberately protocol-only and knows nothing about DJs or
usernames as a *roster* concept -- it just reports what Liquidsoap itself
reports (which slots are live and which username each captured at auth
time). Translating slot ids to/from usernames is app.main's job, since
that requires the DB (the slot pool is generic; only the DB knows which
username currently owns a given slot).
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable, Dict, Optional, Tuple

logger = logging.getLogger("segue.telnet")

OnConnectCb = Callable[[bool], Awaitable[None]]  # arg: is_first_connect
OnAliveChangeCb = Callable[[bool], Awaitable[None]]
OnReconcileCb = Callable[[Dict[str, dict]], Awaitable[None]]  # arg: raw `slots` map


class TelnetClient:
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._io_lock = asyncio.Lock()
        self.alive = False

    async def _connect(self) -> None:
        self._reader, self._writer = await asyncio.open_connection(self.host, self.port)

    async def _close(self) -> None:
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:  # noqa: BLE001
                pass
        self._reader = None
        self._writer = None

    async def _read_until_end(self) -> list[str]:
        assert self._reader is not None
        lines: list[str] = []
        while True:
            raw = await self._reader.readline()
            if not raw:
                raise ConnectionError("telnet connection closed by peer")
            text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if text == "END":
                break
            lines.append(text)
        return lines

    async def _send(self, command: str) -> list[str]:
        async with self._io_lock:
            if self._writer is None or self._reader is None:
                raise ConnectionError("telnet not connected")
            self._writer.write((command + "\n").encode("utf-8"))
            await self._writer.drain()
            return await self._read_until_end()

    async def set_target(self, value: str) -> str:
        lines = await self._send(f"onair.set {value}")
        return lines[0].strip() if lines else value

    async def status(self) -> Tuple[Dict[str, dict], str]:
        lines = await self._send("onair.status")
        if not lines:
            raise ConnectionError("onair.status returned no payload")
        payload = json.loads(lines[0])
        slots = payload.get("slots") or {}
        target = payload.get("target", "FILLER")
        return slots, target

    async def run_forever(
        self,
        on_connect: OnConnectCb,
        on_alive_change: OnAliveChangeCb,
        on_reconcile: OnReconcileCb,
        poll_interval: float = 5.0,
        backoff_start: float = 1.0,
        backoff_cap: float = 15.0,
    ) -> None:
        """Connect/reconnect loop with exponential backoff.

        On every successful (re)connect, calls on_connect(is_first) so the
        caller can perform its reconciliation pass before normal operation
        resumes, then polls onair.status every `poll_interval` seconds,
        routing results through on_reconcile as a safety net for any
        webhook that might have been dropped.
        """
        backoff = backoff_start
        first = True
        while True:
            try:
                await self._connect()
                self.alive = True
                await on_alive_change(True)
                await on_connect(first)
                first = False
                backoff = backoff_start
                while True:
                    await asyncio.sleep(poll_interval)
                    slots, _target = await self.status()
                    await on_reconcile(slots)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect loop must never die
                logger.warning("telnet connection lost/failed: %s", exc)
                self.alive = False
                await self._close()
                try:
                    await on_alive_change(False)
                except Exception:  # noqa: BLE001
                    logger.exception("on_alive_change callback failed")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, backoff_cap)
