"""Configuration loading for the SEGUE api service.

Reads config/djs.yaml (path fixed at /config/djs.yaml inside the container,
overridable via ONAIR_DJS_CONFIG_PATH for local testing) and expands
``${VAR}`` placeholders against the process environment -- the same
convention used by the liquidsoap side's render_config.py.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import List

import yaml

ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

DEFAULT_DJS_CONFIG_PATH = "/config/djs.yaml"


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class DjConfig:
    id: str
    name: str
    mount: str
    password: str


def _expand_env(text: str) -> str:
    def replace(match: "re.Match[str]") -> str:
        name = match.group(1)
        value = os.environ.get(name)
        if value is None:
            raise ConfigError(f"environment variable {name} is not set")
        return value

    return ENV_PATTERN.sub(replace, text)


def djs_config_path() -> str:
    return os.environ.get("ONAIR_DJS_CONFIG_PATH", DEFAULT_DJS_CONFIG_PATH)


def load_djs(path: str | None = None) -> List[DjConfig]:
    """Load and validate config/djs.yaml, expanding ${VAR} placeholders."""
    path = path or djs_config_path()
    with open(path, "r", encoding="utf-8") as f:
        raw = _expand_env(f.read())
    data = yaml.safe_load(raw) or {}
    entries = data.get("djs", [])
    if not entries:
        raise ConfigError(f"{path}: no DJs configured")

    seen_ids = set()
    djs: List[DjConfig] = []
    for entry in entries:
        for field in ("id", "name", "mount", "password"):
            if not entry.get(field):
                raise ConfigError(f"{path}: DJ entry missing required field '{field}': {entry}")
        if entry["id"] in seen_ids:
            raise ConfigError(f"{path}: duplicate DJ id '{entry['id']}'")
        seen_ids.add(entry["id"])
        djs.append(
            DjConfig(
                id=str(entry["id"]),
                name=str(entry["name"]),
                mount=str(entry["mount"]),
                password=str(entry["password"]),
            )
        )
    return djs
