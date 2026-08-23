"""SQLite persistence for SEGUE's api service.

Plain synchronous stdlib sqlite3 -- this tool has very low concurrency (one
admin, a handful of DJs), so a lightweight synchronous wrapper is plenty.

Tables:
  settings  -- single row holding the persisted "intentions": mode, pinned
  djs       -- self-registered DJs: username -> password/ready/slot. Rows
               are created lazily on first login (see app.main's identity
               dependency), never by a static config file.
  eventlog  -- append-only log of connect/disconnect, mode/pin changes,
               on_air switches, etc.

Slot assignment: only a `ready` DJ ever holds a non-null `slot`. Flipping a
DJ to ready picks the lowest-numbered free slot among `slot1..slot{max}`;
flipping to not-ready always frees it. This is what makes a DJ's
(username, password) independent of which physical harbor mount they end
up on -- the credentials never encode a slot number.
"""
from __future__ import annotations

import os
import secrets
import sqlite3
from datetime import datetime, timezone
from typing import List, Optional


def _iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class NoFreeSlotError(RuntimeError):
    """Raised by set_ready(..., ready=True) when every slot is taken."""


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
        conn.row_factory = sqlite3.Row
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
                CREATE TABLE IF NOT EXISTS djs (
                    username TEXT PRIMARY KEY,
                    password TEXT NOT NULL,
                    ready INTEGER NOT NULL DEFAULT 0,
                    slot TEXT,
                    created_at TEXT NOT NULL
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

    def load_settings(self) -> tuple[str, Optional[str]]:
        with self._connect() as conn:
            row = conn.execute("SELECT mode, pinned FROM settings WHERE id = 1").fetchone()
            if row is None:
                return "AUTO", None
            return row["mode"], row["pinned"]

    def save_settings(self, mode: str, pinned: Optional[str]) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE settings SET mode = ?, pinned = ? WHERE id = 1", (mode, pinned)
            )
            conn.commit()

    # -- djs --------------------------------------------------------------

    def get_or_create_dj(self, username: str) -> dict:
        """Look up a DJ by username, self-registering them on first login.

        Credentials (password) are generated exactly once and never
        rotated by this call -- revisiting the dashboard just returns the
        same row.
        """
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM djs WHERE username = ?", (username,)).fetchone()
            if row is not None:
                return dict(row)
            password = secrets.token_urlsafe(16)
            conn.execute(
                "INSERT INTO djs (username, password, ready, slot, created_at) "
                "VALUES (?, ?, 0, NULL, ?)",
                (username, password, _iso_now()),
            )
            conn.commit()
            return {
                "username": username,
                "password": password,
                "ready": 0,
                "slot": None,
                "created_at": _iso_now(),
            }

    def get_dj(self, username: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM djs WHERE username = ?", (username,)).fetchone()
            return dict(row) if row is not None else None

    def dj_exists(self, username: str) -> bool:
        return self.get_dj(username) is not None

    def is_ready(self, username: str) -> bool:
        dj = self.get_dj(username)
        return bool(dj and dj["ready"])

    def list_djs(self) -> List[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM djs ORDER BY created_at ASC").fetchall()
            return [dict(r) for r in rows]

    def list_ready_usernames(self) -> List[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT username FROM djs WHERE ready = 1 ORDER BY created_at ASC"
            ).fetchall()
            return [r["username"] for r in rows]

    def get_slot(self, username: str) -> Optional[str]:
        dj = self.get_dj(username)
        return dj["slot"] if dj else None

    def username_for_slot(self, slot: str) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT username FROM djs WHERE slot = ? AND ready = 1", (slot,)
            ).fetchone()
            return row["username"] if row else None

    def check_credentials(self, username: str, password: str) -> bool:
        dj = self.get_dj(username)
        return bool(dj and dj["ready"] and secrets.compare_digest(dj["password"], password))

    def set_ready(self, username: str, ready: bool, max_slots: int) -> Optional[str]:
        """Toggle a DJ's ready flag, (de)assigning a slot as needed.

        Returns the assigned slot (or None when turning ready off).
        Raises NoFreeSlotError if turning ready on and every slot in
        slot1..slot{max_slots} is already held by another ready DJ.
        """
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM djs WHERE username = ?", (username,)).fetchone()
            if row is None:
                raise ValueError(f"unknown dj: {username!r}")

            if not ready:
                conn.execute(
                    "UPDATE djs SET ready = 0, slot = NULL WHERE username = ?", (username,)
                )
                conn.commit()
                return None

            if row["ready"] and row["slot"]:
                return row["slot"]  # already ready, idempotent

            taken = {
                r["slot"]
                for r in conn.execute(
                    "SELECT slot FROM djs WHERE ready = 1 AND slot IS NOT NULL"
                ).fetchall()
            }
            free_slot = next(
                (f"slot{i}" for i in range(1, max_slots + 1) if f"slot{i}" not in taken), None
            )
            if free_slot is None:
                raise NoFreeSlotError(f"all {max_slots} slots are taken")

            conn.execute(
                "UPDATE djs SET ready = 1, slot = ? WHERE username = ?", (free_slot, username)
            )
            conn.commit()
            return free_slot

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
            return [{"ts": r["ts"], "message": r["message"]} for r in rows]
