#!/usr/bin/env python3
"""SEGUE LJ controller.

Runs on the event operator's own machine, alongside the OBS instance that
already pulls SEGUE's output and forwards it to VRCDN. Its only job:
mirror the api service's resolved `on_air` value into OBS by toggling
scene-item *visibility* within one fixed scene (never by changing the
active scene, never by rewriting a source's URL - both of those force a
reconnect/buffer glitch on a Media Source, which is exactly the kind of
switch-time glitch this whole project exists to avoid; see this
directory's README for the OBS-side "Close file when inactive" setting
that visibility-toggling depends on).

Two independent reconnect-with-backoff loops run concurrently:
  - api_ws_loop: holds /ws/lj open, falling back to HTTP polling of
    /api/lj/state while it's down. Mirrors the exact backoff (1s -> 15s
    cap) + poll-fallback idiom already used by api/static/dj/dj.js and
    api/static/admin/admin.js against the same api service.
  - obs_loop: holds an obs-websocket connection open, with its own
    backoff and a periodic heartbeat call (simpleobsws has no built-in
    auto-reconnect or connection-loss callback to rely on).

Neither loop blocks the other: if OBS is unreachable when a state change
arrives, it's remembered as `pending_target` and applied on next OBS
(re)connect, so a restarted controller or a momentarily-down OBS
self-heals to the correct on-air source rather than staying stuck.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

import simpleobsws
import websockets
import yaml

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("lj-controller")

WS_BACKOFF_START = 1.0
WS_BACKOFF_MAX = 15.0
OBS_BACKOFF_START = 1.0
OBS_BACKOFF_MAX = 15.0
POLL_INTERVAL = 3.0
OBS_HEARTBEAT_INTERVAL = 5.0


def load_config(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _ws_url(api_base_url: str) -> str:
    if api_base_url.startswith("https://"):
        return "wss://" + api_base_url[len("https://"):].rstrip("/") + "/ws/lj"
    if api_base_url.startswith("http://"):
        return "ws://" + api_base_url[len("http://"):].rstrip("/") + "/ws/lj"
    raise ValueError(f"api_base_url must start with http:// or https://: {api_base_url!r}")


def _state_url(api_base_url: str) -> str:
    return api_base_url.rstrip("/") + "/api/lj/state"


class LjController:
    def __init__(self, config: dict):
        self.config = config
        self.obs: Optional[simpleobsws.WebSocketClient] = None
        self.obs_connected = False
        self.scene_items: dict[str, int] = {}  # OBS source name -> sceneItemId
        self.last_applied: Optional[str] = None
        self.pending_target: Optional[str] = None

    # ---- OBS side -----------------------------------------------------

    async def obs_loop(self) -> None:
        backoff = OBS_BACKOFF_START
        while True:
            try:
                self.obs = simpleobsws.WebSocketClient(
                    url=self.config["obs_ws_url"],
                    password=self.config.get("obs_ws_password", ""),
                )
                await self.obs.connect()
                await self.obs.wait_until_identified()
                logger.info("connected to OBS")
                await self._refresh_scene_items()
                self.obs_connected = True
                backoff = OBS_BACKOFF_START
                if self.pending_target is not None:
                    await self._apply_target(self.pending_target)
                await self._heartbeat_until_disconnected()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - this loop must never die
                logger.warning("OBS connection lost/failed: %s", exc)
            self.obs_connected = False
            try:
                if self.obs is not None:
                    await self.obs.disconnect()
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, OBS_BACKOFF_MAX)

    async def _heartbeat_until_disconnected(self) -> None:
        # simpleobsws has no connection-loss callback to await on, so a
        # periodic no-op request doubles as a liveness check - any
        # exception (including a closed connection) falls through to
        # obs_loop's reconnect handling.
        while True:
            await asyncio.sleep(OBS_HEARTBEAT_INTERVAL)
            resp = await self.obs.call(simpleobsws.Request("GetVersion"))
            if not resp.ok():
                raise RuntimeError("OBS heartbeat request failed")

    async def _refresh_scene_items(self) -> None:
        scene_name = self.config["scene_name"]
        resp = await self.obs.call(simpleobsws.Request("GetSceneItemList", {"sceneName": scene_name}))
        if not resp.ok():
            raise RuntimeError(f"GetSceneItemList failed: {resp.requestStatus.comment}")
        items = resp.responseData.get("sceneItems", [])
        self.scene_items = {item["sourceName"]: item["sceneItemId"] for item in items}
        wanted = set(self.config["slot_sources"].values()) | {self.config["standby_source"]}
        missing = wanted - set(self.scene_items)
        if missing:
            logger.warning(
                "scene %r is missing configured source(s): %s - check config.yaml against OBS",
                scene_name,
                sorted(missing),
            )

    async def _apply_target(self, source_name: str) -> None:
        if source_name == self.last_applied:
            return
        if source_name not in self.scene_items:
            logger.warning("target source %r not found in scene, staying on %r", source_name, self.last_applied)
            return
        scene_name = self.config["scene_name"]
        requests = [
            simpleobsws.Request(
                "SetSceneItemEnabled",
                {
                    "sceneName": scene_name,
                    "sceneItemId": item_id,
                    "sceneItemEnabled": name == source_name,
                },
            )
            for name, item_id in self.scene_items.items()
        ]
        await self.obs.call_batch(requests)
        self.last_applied = source_name
        logger.info("switched to %r", source_name)

    async def apply_state(self, state: dict) -> None:
        on_air = state.get("on_air")
        slot_sources = self.config["slot_sources"]
        standby = self.config["standby_source"]
        target = standby
        if on_air and on_air != "FILLER":
            for dj in state.get("djs", []):
                if dj.get("username") == on_air:
                    target = slot_sources.get(dj.get("slot"), standby)
                    break
        if not self.obs_connected:
            self.pending_target = target
            return
        self.pending_target = None
        await self._apply_target(target)

    # ---- api side -------------------------------------------------------

    async def api_ws_loop(self) -> None:
        backoff = WS_BACKOFF_START
        url = _ws_url(self.config["api_base_url"])
        headers = {"X-Onair-Lj-Token": self.config["lj_token"]}
        while True:
            poll_task = asyncio.create_task(self._poll_fallback())
            try:
                async with websockets.connect(url, additional_headers=headers) as ws:
                    logger.info("connected to api")
                    poll_task.cancel()
                    backoff = WS_BACKOFF_START
                    async for message in ws:
                        try:
                            state = json.loads(message)
                        except json.JSONDecodeError:
                            continue
                        await self.apply_state(state)
            except asyncio.CancelledError:
                poll_task.cancel()
                raise
            except Exception as exc:  # noqa: BLE001 - this loop must never die
                logger.warning("api connection lost/failed: %s", exc)
            finally:
                if not poll_task.done():
                    poll_task.cancel()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, WS_BACKOFF_MAX)

    async def _poll_fallback(self) -> None:
        """Runs only while /ws/lj is down (cancelled the instant it
        (re)connects) -- keeps OBS roughly in sync via plain HTTP polling
        during a longer api outage, the same WS-down-so-poll idiom
        dj.js/admin.js use against this same api service."""
        url = _state_url(self.config["api_base_url"])
        headers = {"X-Onair-Lj-Token": self.config["lj_token"]}
        while True:
            try:
                state = await asyncio.to_thread(self._http_get_json, url, headers)
                await self.apply_state(state)
            except Exception as exc:  # noqa: BLE001
                logger.debug("poll fallback failed: %s", exc)
            await asyncio.sleep(POLL_INTERVAL)

    @staticmethod
    def _http_get_json(url: str, headers: dict) -> dict:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))

    async def run(self) -> None:
        await asyncio.gather(self.obs_loop(), self.api_ws_loop())


def main() -> None:
    config_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "config.yaml"
    if not config_path.exists():
        raise SystemExit(
            f"config file not found: {config_path} - copy config.example.yaml to config.yaml first"
        )
    controller = LjController(load_config(config_path))
    try:
        asyncio.run(controller.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
