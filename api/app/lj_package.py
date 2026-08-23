"""Generates the two artifacts the admin panel hands to the event operator
so they don't have to hand-assemble the LJ controller themselves - see
api/app/main.py's /api/admin/lj/package.zip and /api/admin/lj/obs-scene.json.

Both artifacts share ONE naming scheme for OBS sources (slot1 -> "DJ Source
1", ..., plus a "Standby" source) via _slot_source_name()/STANDBY_SOURCE -
the generated config.yaml's slot_sources map and the generated OBS scene's
actual source names MUST stay byte-identical, since lj_controller.py looks
OBS sources up by name (see lj-controller/lj_controller.py's apply_state()).
Never let these drift apart.

--- Confidence level on the OBS scene collection JSON, read before trusting
    this blindly ---

OBS's scene collection file format is NOT officially documented - it's
whatever libobs happens to serialize, reverse-engineered here by reading
the actual save/load C++ source in github.com/obsproject/obs-studio
(frontend/widgets/OBSBasic_SceneCollections.cpp's Save()/Load(),
libobs/obs-scene.c's scene_save_item()/scene_load_item()), not guessed and
not copied from an unverified example. No real OBS install was available
to actually test-import the generated file, so treat confidence as tiered:

  HIGH - verified directly against current libobs/frontend source:
    - Top-level keys (name, sources, groups, scene_order, current_scene,
      current_program_scene, current_transition, transition_duration,
      transitions, quick_transitions, saved_projectors, preview_locked,
      scaling_enabled/level/off_x/off_y) and their exact load-time
      defaulting behavior for anything left empty/omitted here.
    - "sources" is a flat array mixing every non-scene source AND every
      scene object together (confirmed by reading the Save() function
      end-to-end, NOT a name-keyed object as some third-party scene-builder
      example files elsewhere on the web suggested - those are a
      different tool's own intermediate format, not OBS's real output).
    - Scene items resolve their source by "source_uuid" first, falling
      back to plain "name" lookup whenever source_uuid is absent/unset/
      unresolved (scene_load_item(), verbatim comment: "Fall back to name
      if UUID was not found or is not set") - so omitting source_uuid
      entirely and relying on exact name matches, as this module does, is
      confirmed-supported legacy-format behavior, not a guess.
    - ffmpeg_source's relevant setting keys (is_local_file, input,
      close_when_inactive, reconnect_delay_sec, buffering_mb, hw_decode,
      clear_on_media_end, restart_on_activate, looping, linear_alpha,
      speed_percent) - confirmed against obs-ffmpeg-source.c's own
      defaults function.

  MEDIUM - standard/widely-documented but not re-derived from source here:
    - "color_source_v3" as the built-in solid-color source type/settings
      shape for the generated standby placeholder.
    - bounds_type=2 (OBS_BOUNDS_SCALE_INNER) with a 1920x1080 bounds box on
      every scene item, so a DJ's actual stream resolution auto-fits/
      letterboxes into the canvas instead of rendering at 1:1 native size
      top-left - meaningfully nicer on first import, but if this specific
      field turns out wrong, the practical fallback is trivial: right-click
      the source in OBS -> Transform -> Fit to Screen. Nothing else in the
      file depends on this being correct.

Given that tiering, this is offered as "try importing this first," not as
a guaranteed substitute for the manual setup steps in
lj-controller/README.md and the admin page's own instructions - both stay
fully documented rather than being replaced by "just import this file."
"""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Callable, List

# Baked into the image at build time (api/Dockerfile: COPY lj-controller
# /srv/lj-controller, with a repo-root build context) - NOT bind-mounted.
# A bind mount was tried first and broke on Coolify: relative `volumes:`
# paths get provisioned as independent persistent-storage directories,
# not synced from the git checkout, so the mount came up empty in
# production despite working fine in local `docker compose` testing
# (which does reflect the real host directory). See docker-compose.yaml's
# `api.build` comment for the full story.
LJ_CONTROLLER_DIR = Path(__file__).resolve().parent.parent / "lj-controller"

SCENE_NAME = "Live"
STANDBY_SOURCE = "Standby"
_CANVAS_WIDTH = 1920
_CANVAS_HEIGHT = 1080


def _slot_source_name(slot: str) -> str:
    """"slot3" -> "DJ Source 3" - matches lj-controller/config.example.yaml's
    existing convention exactly, so a manually-set-up operator and a
    downloaded-package operator end up with the same names either way."""
    n = slot[len("slot") :]
    return f"DJ Source {n}"


def build_config_yaml(api_base_url: str, lj_token: str, max_djs: int) -> str:
    """A ready-to-run config.yaml, not a template - every value except
    obs_ws_password is already correct for this deployment. Deliberately
    hand-built as plain text rather than via a YAML library + dict dump:
    this shape is small and stable enough that matching
    config.example.yaml's own comment style (which a dict dump can't
    produce) is worth more than using a library for something this
    simple."""
    lines = [
        "# Generated by the SEGUE admin panel (Betreiber -> LJ-Setup) -",
        "# ready to run as-is, except obs_ws_password below, which only you",
        "# know (set under OBS's Tools -> WebSocket Server Settings).",
        "# See lj-controller/README.md in this package for full setup steps.",
        "",
        f'api_base_url: "{api_base_url}"',
        f'lj_token: "{lj_token}"',
        "",
        'obs_ws_url: "ws://localhost:4455"',
        'obs_ws_password: "CHANGE-ME"',
        "",
        f'scene_name: "{SCENE_NAME}"',
        f'standby_source: "{STANDBY_SOURCE}"',
        "",
        "slot_sources:",
    ]
    for i in range(1, max_djs + 1):
        slot = f"slot{i}"
        lines.append(f'  {slot}: "{_slot_source_name(slot)}"')
    lines.append("")
    return "\n".join(lines)


def build_lj_zip(api_base_url: str, lj_token: str, max_djs: int, rtsp_url_fn: Callable[[str], str]) -> bytes:
    """lj_controller.py + requirements.txt + README.md, copied as-is from
    lj-controller/ (never duplicated/reformatted - one source of truth),
    plus the generated config.yaml and the same OBS scene collection
    build_obs_scene_json() produces, so the zip is self-sufficient without
    also visiting the separate scene-only download."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in ("lj_controller.py", "requirements.txt", "README.md"):
            zf.write(LJ_CONTROLLER_DIR / name, arcname=name)
        zf.writestr("config.yaml", build_config_yaml(api_base_url, lj_token, max_djs))
        zf.writestr(
            "segue-obs-scene.json",
            json.dumps(build_obs_scene_json(rtsp_url_fn, max_djs), indent=2),
        )
    return buf.getvalue()


# ---------------------------------------------------------------------------
# OBS scene collection JSON
# ---------------------------------------------------------------------------


def _source_envelope(source_id: str, name: str, settings: dict) -> dict:
    """The generic wrapper every obs_save_source() call produces regardless
    of source type - confirmed against libobs's real save function and a
    real captured example (this project's own earlier research this
    session pulled a genuine exported audio-device source using this exact
    shape). "uuid"/"source_uuid" are deliberately omitted throughout this
    module - see the module docstring for why that's confirmed-safe rather
    than an oversight."""
    return {
        "prev_ver": 469829634,
        "name": name,
        "id": source_id,
        "versioned_id": source_id,
        "settings": settings,
        "mixers": 0,
        "sync": 0,
        "flags": 0,
        "volume": 1.0,
        "balance": 0.5,
        "enabled": True,
        "muted": False,
        "push-to-mute": False,
        "push-to-mute-delay": 0,
        "push-to-talk": False,
        "push-to-talk-delay": 0,
        "hotkeys": {},
        "deinterlace_mode": 0,
        "deinterlace_field_order": 0,
        "monitoring_type": 0,
        "private_settings": {},
    }


def _rtsp_source_settings(rtsp_url: str) -> dict:
    return {
        "is_local_file": False,
        "input": rtsp_url,
        "input_format": "",
        # TCP transport trades a little latency for reliability over the
        # network path to the venue's server - matches this whole
        # project's bias toward "never glitch" over "lowest possible
        # latency". Change to "-rtsp_transport udp" if your network proves
        # more reliable than TCP's retransmit behavior in practice.
        "ffmpeg_options": "-rtsp_transport tcp",
        "reconnect_delay_sec": 2,
        "buffering_mb": 2,
        "hw_decode": False,
        "clear_on_media_end": False,
        "restart_on_activate": True,
        # THE field this whole feature exists to get right - see
        # lj-controller/README.md for the full "why". Unchecked/false is
        # mandatory: leaving this true silently defeats glitch-free
        # switching.
        "close_when_inactive": False,
        "looping": False,
        "linear_alpha": False,
        "speed_percent": 100,
        "seekable": False,
    }


def _standby_source_settings() -> dict:
    return {"color": 0xFF202020, "width": _CANVAS_WIDTH, "height": _CANVAS_HEIGHT}


def _scene_item(name: str, item_id: int, *, visible: bool) -> dict:
    return {
        "name": name,
        "visible": visible,
        "locked": False,
        "rot": 0.0,
        "align": 5,
        # Scale-to-fit (letterboxed, aspect preserved) into a 1920x1080
        # box rather than native 1:1 top-left placement - a DJ's actual
        # stream resolution isn't known until they're actually connected,
        # so this is the one field in this file with only medium
        # confidence (see module docstring). Harmless if wrong: right-click
        # the source in OBS -> Transform -> Fit to Screen fixes it by hand
        # in a few seconds, same as any manually-added source would need.
        "bounds_type": 2,
        "bounds_align": 0,
        "bounds_crop": False,
        "crop_left": 0,
        "crop_top": 0,
        "crop_right": 0,
        "crop_bottom": 0,
        "id": item_id,
        "group_item_backup": False,
        "pos": {"x": 0.0, "y": 0.0},
        "scale": {"x": 1.0, "y": 1.0},
        "bounds": {"x": float(_CANVAS_WIDTH), "y": float(_CANVAS_HEIGHT)},
        "scale_filter": "disable",
        "blend_method": "default",
        "blend_type": "normal",
        "private_settings": {},
    }


def build_obs_scene_json(rtsp_url_fn: Callable[[str], str], max_djs: int) -> dict:
    """One importable OBS Scene Collection: one RTSP Media Source per slot
    (source names matching build_config_yaml()'s slot_sources exactly),
    one Standby color source, all inside a single scene named SCENE_NAME -
    matching lj_controller.py's design of toggling visibility within one
    scene rather than switching between scenes or rewriting URLs."""
    sources: List[dict] = []
    item_names: List[str] = []

    for i in range(1, max_djs + 1):
        slot = f"slot{i}"
        name = _slot_source_name(slot)
        sources.append(_source_envelope("ffmpeg_source", name, _rtsp_source_settings(rtsp_url_fn(slot))))
        item_names.append(name)

    sources.append(_source_envelope("color_source_v3", STANDBY_SOURCE, _standby_source_settings()))
    item_names.append(STANDBY_SOURCE)

    scene_items = [
        _scene_item(name, item_id, visible=(name == STANDBY_SOURCE))
        for item_id, name in enumerate(item_names, start=1)
    ]
    scene_settings = {
        "id_counter": len(scene_items),
        "custom_size": False,
        "items": scene_items,
    }
    sources.append(_source_envelope("scene", SCENE_NAME, scene_settings))

    return {
        "name": "SEGUE",
        "sources": sources,
        "groups": [],
        "scene_order": [{"name": SCENE_NAME}],
        "current_scene": SCENE_NAME,
        "current_program_scene": SCENE_NAME,
        "current_transition": "Fade",
        "transition_duration": 300,
        "transitions": [],
        "quick_transitions": [],
        "saved_projectors": [],
        "preview_locked": False,
        "scaling_enabled": False,
        "scaling_level": 0,
        "scaling_off_x": 0.0,
        "scaling_off_y": 0.0,
    }
