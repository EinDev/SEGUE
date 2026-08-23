"""Polling client for MediaMTX's control API.

Unlike the old telnet_client.TelnetClient, this module never *commands*
anything - there is no "set target" push anymore, because switching now
happens entirely in the LJ's OBS, driven by broadcasting `on_air` over
/ws/lj (see app.main). MediaMTX itself doesn't know or care which slot is
"on air"; it's a dumb relay. So this client's only job is the same
reconciliation role telnet_client's polling loop played: periodically ask
"what's actually connected right now?" as a safety net against a missed
runOnAvailable/runOnUnavailable webhook, via GET /v3/paths/list on
MediaMTX's internal control API (never exposed to the host - same
treatment the old telnet port got).

This module is deliberately protocol-only and knows nothing about DJs or
usernames as a *roster* concept, same separation of concerns telnet_client
had - translating slot ids to/from usernames is app.main's job.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Set

import httpx

logger = logging.getLogger("segue.mediamtx")

OnFirstSyncCb = Callable[[Set[str]], Awaitable[None]]  # arg: connected slot names
OnReconcileCb = Callable[[Set[str]], Awaitable[None]]  # arg: connected slot names
OnAliveChangeCb = Callable[[bool], Awaitable[None]]


async def list_connected_slots(client: httpx.AsyncClient, base_url: str) -> Set[str]:
    """GET {base_url}/v3/paths/list, return the set of slot names that
    currently have a ready publisher (i.e. a connected DJ)."""
    resp = await client.get(f"{base_url}/v3/paths/list", timeout=5.0)
    resp.raise_for_status()
    payload = resp.json()
    items = payload.get("items") or []
    return {item["name"] for item in items if item.get("ready") and item.get("name")}


async def run_forever(
    base_url: str,
    on_first_sync: OnFirstSyncCb,
    on_alive_change: OnAliveChangeCb,
    on_reconcile: OnReconcileCb,
    poll_interval: float = 5.0,
    backoff_start: float = 1.0,
    backoff_cap: float = 15.0,
) -> None:
    """Poll/reconnect loop with exponential backoff, mirroring
    telnet_client.TelnetClient.run_forever's shape exactly (same backoff
    constants, same is_first-sync/alive-change/reconcile split) so the
    startup/reconnection behavior documented in CONCEPT.md/README.md's
    error-handling table carries over unchanged in spirit, just polling
    HTTP instead of holding a persistent telnet socket.
    """
    backoff = backoff_start
    first = True
    alive = False
    async with httpx.AsyncClient() as client:
        while True:
            try:
                connected = await list_connected_slots(client, base_url)
                if not alive:
                    alive = True
                    await on_alive_change(True)
                if first:
                    first = False
                    await on_first_sync(connected)
                else:
                    await on_reconcile(connected)
                backoff = backoff_start
                await asyncio.sleep(poll_interval)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - poll loop must never die
                logger.warning("mediamtx control API poll failed: %s", exc)
                if alive:
                    alive = False
                    try:
                        await on_alive_change(False)
                    except Exception:  # noqa: BLE001
                        logger.exception("on_alive_change callback failed")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, backoff_cap)
