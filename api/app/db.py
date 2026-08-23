"""SQLite persistence for SEGUE's api service.

Plain synchronous stdlib sqlite3 -- this tool has very low concurrency (one
admin, a few DJs), so a lightweight synchronous wrapper is plenty. Queries
are tiny and local; callers that want to keep the event loop perfectly free
may wrap calls in asyncio.to_thread, but it isn't required.

Tables:
  settings   -- single row holding the persisted "intentions": mode, pinned
  dj_tokens  -- dj_id -> token, generated once and never regenerated
  eventlog   -- append-only log of connect/disconnect, mode/pin changes,
                on_air switches, etc.
"""
from __future__ import annotations

import os
import secrets
import sqlite3
from datetime import datetime, timezone
from typing import List, Optional, Tuple


def _iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class Database:
    def __init__(self, path: str):
        self.path = path
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    mode TEXT NOT NULL DEFAULT 'AUTO',
                    pinned TEXT
                )
                """
            )
            conn.execute(
                "INSERT OR IGNORE INTO settings (id, mode, pinned) VALUES (1, 'AUTO', NULL)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS dj_tokens (
                    dj_id TEXT PRIMARY KEY,
                    token TEXT UNIQUE NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS eventlog (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    message TEXT NOT NULL
                )
                """
            )
            conn.commit()

    # -- settings (mode / pinned) ---------------------------------------

    def load_settings(self) -> Tuple[str, Optional[str]]:
        with self._connect() as conn:
            row = conn.execute("SELECT mode, pinned FROM settings WHERE id = 1").fetchone()
            if row is None:
                return "AUTO", None
            return row[0], row[1]

    def save_settings(self, mode: str, pinned: Optional[str]) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE settings SET mode = ?, pinned = ? WHERE id = 1", (mode, pinned)
            )
            conn.commit()

    # -- dj tokens --------------------------------------------------------

    def get_or_create_token(self, dj_id: str) -> str:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT token FROM dj_tokens WHERE dj_id = ?", (dj_id,)
            ).fetchone()
            if row is not None:
                return row[0]
            token = secrets.token_urlsafe(24)
            conn.execute(
                "INSERT INTO dj_tokens (dj_id, token) VALUES (?, ?)", (dj_id, token)
            )
            conn.commit()
            return token

    def dj_id_for_token(self, token: str) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT dj_id FROM dj_tokens WHERE token = ?", (token,)
            ).fetchone()
            return row[0] if row else None

    def all_tokens(self) -> dict[str, str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT dj_id, token FROM dj_tokens").fetchall()
            return {dj_id: token for dj_id, token in rows}

    # -- eventlog -----------------------------------------------------------

    def log_event(self, message: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO eventlog (ts, message) VALUES (?, ?)", (_iso_now(), message)
            )
            conn.commit()

    def get_log(self, limit: int = 100) -> List[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT ts, message FROM eventlog ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [{"ts": ts, "message": message} for ts, message in rows]
