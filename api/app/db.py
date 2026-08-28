"""SQLite persistence for SEGUE's api service.

Plain synchronous stdlib sqlite3 -- this tool has very low concurrency (one
admin, a handful of DJs), so a lightweight synchronous wrapper is plenty.

Tables:
  settings  -- single row holding the persisted "intentions": mode, pinned,
               event_name (purely cosmetic, shown on both dashboards).
  djs       -- self-registered DJs: username -> password/ready/slot. Rows
               are created lazily on first login (see app.main's identity
               dependency), never by a static config file. Also carries an
               optional scheduled_start/scheduled_end (ISO timestamps) the
               admin can set for a running-order display -- purely
               informational, never enforced against who's actually live.
  eventlog  -- append-only log of connect/disconnect, mode/pin changes,
               on_air switches, etc.
  messages  -- admin<->DJ chat, one row per message. `sender` is which side
               *sent* it ('admin' or 'dj'); `acked_at` is set once the
               *other* side has acknowledged/read it -- for an admin
               message that means the DJ explicitly tapped "verstanden"
               (CONCEPT: this needs to be hard to miss, not just another
               feed entry); for a DJ message it just means the admin has
               opened that DJ's thread.

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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    dj_username TEXT NOT NULL,
                    sender TEXT NOT NULL CHECK (sender IN ('admin', 'dj')),
                    text TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    acked_at TEXT
                )
                """
            )
            # Additive migrations for columns introduced after the tables
            # above first shipped -- plain ALTER TABLE ADD COLUMN has no
            # "IF NOT EXISTS" in sqlite, so check PRAGMA table_info first.
            # Existing deployments' onair.db gets these transparently on
            # next startup; a fresh DB gets them via INSERT/UPDATE below
            # doing nothing (columns are NULL by default already).
            self._ensure_columns(conn, "settings", [("event_name", "TEXT")])
            self._ensure_columns(
                conn, "djs", [("scheduled_start", "TEXT"), ("scheduled_end", "TEXT")]
            )
            conn.commit()

    @staticmethod
    def _ensure_columns(conn: sqlite3.Connection, table: str, columns: List[tuple]) -> None:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, coltype in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {coltype}")

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

    def get_event_name(self) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute("SELECT event_name FROM settings WHERE id = 1").fetchone()
            return row["event_name"] if row else None

    def set_event_name(self, name: Optional[str]) -> None:
        name = (name or "").strip() or None
        with self._connect() as conn:
            conn.execute("UPDATE settings SET event_name = ? WHERE id = 1", (name,))
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
                "scheduled_start": None,
                "scheduled_end": None,
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

    def set_schedule(
        self, username: str, scheduled_start: Optional[str], scheduled_end: Optional[str]
    ) -> None:
        """Purely informational running-order times for the DJ dashboard's
        "live in XY minutes" / end-of-set display -- never read by
        state.resolve() or anything else that decides who's actually on
        air. `None` clears a field."""
        if self.get_dj(username) is None:
            raise ValueError(f"unknown dj: {username!r}")
        with self._connect() as conn:
            conn.execute(
                "UPDATE djs SET scheduled_start = ?, scheduled_end = ? WHERE username = ?",
                (scheduled_start, scheduled_end, username),
            )
            conn.commit()

    def delete_dj(self, username: str) -> bool:
        """Remove a DJ's row entirely, freeing their username/slot.

        Not just "not ready" -- this drops their password too, so a stale
        stream key stops working. Revisiting the DJ dashboard afterwards
        self-registers them again from scratch (see get_or_create_dj), now
        unapproved. Their chat history goes with them -- a re-registration
        under the same username starts a fresh thread rather than
        resurrecting old messages.
        """
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM djs WHERE username = ?", (username,))
            conn.execute("DELETE FROM messages WHERE dj_username = ?", (username,))
            conn.commit()
            return cur.rowcount > 0

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

    # -- messages (admin<->DJ chat) -----------------------------------------
    #
    # Deliberately simple: one flat table, no "conversation" object, no
    # read-vs-delivered distinction beyond acked_at. `sender` is whichever
    # side wrote the row; the *other* side is the one that acks it.
    #   - admin -> dj: the DJ must explicitly acknowledge (CONCEPT: "make it
    #     very annoying until acknowledged" -- see ack_admin_message).
    #   - dj -> admin: acked_at is set when the admin opens that DJ's thread
    #     (mark_dj_messages_read), just clearing the unread badge -- no
    #     forced interaction required on the admin's own dashboard.

    def add_message(self, dj_username: str, sender: str, text: str) -> dict:
        if sender not in ("admin", "dj"):
            raise ValueError(f"invalid sender: {sender!r}")
        text = (text or "").strip()
        if not text:
            raise ValueError("message text must not be empty")
        if self.get_dj(dj_username) is None:
            raise ValueError(f"unknown dj: {dj_username!r}")
        ts = _iso_now()
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO messages (dj_username, sender, text, created_at, acked_at) "
                "VALUES (?, ?, ?, ?, NULL)",
                (dj_username, sender, text, ts),
            )
            conn.commit()
            return {
                "id": cur.lastrowid,
                "dj_username": dj_username,
                "sender": sender,
                "text": text,
                "created_at": ts,
                "acked_at": None,
            }

    def list_messages(self, dj_username: str, limit: int = 200) -> List[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, dj_username, sender, text, created_at, acked_at FROM messages "
                "WHERE dj_username = ? ORDER BY id ASC LIMIT ?",
                (dj_username, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def unacked_admin_messages(self, dj_username: str) -> List[dict]:
        """Admin->DJ messages this DJ has not yet acknowledged -- backs the
        DJ dashboard's forced-acknowledgment banner."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, dj_username, sender, text, created_at, acked_at FROM messages "
                "WHERE dj_username = ? AND sender = 'admin' AND acked_at IS NULL "
                "ORDER BY id ASC",
                (dj_username,),
            ).fetchall()
            return [dict(r) for r in rows]

    def ack_admin_message(self, message_id: int, dj_username: str) -> bool:
        """The DJ acknowledging one of their own unacked admin messages.
        Scoped to (id, dj_username, sender='admin') so a DJ can only ack
        messages addressed to them, never someone else's or their own
        outgoing ones."""
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE messages SET acked_at = ? "
                "WHERE id = ? AND dj_username = ? AND sender = 'admin' AND acked_at IS NULL",
                (_iso_now(), message_id, dj_username),
            )
            conn.commit()
            return cur.rowcount > 0

    def mark_dj_messages_read(self, dj_username: str) -> None:
        """The admin opening a DJ's thread clears that DJ's unread badge --
        no forced interaction, unlike ack_admin_message above."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE messages SET acked_at = ? "
                "WHERE dj_username = ? AND sender = 'dj' AND acked_at IS NULL",
                (_iso_now(), dj_username),
            )
            conn.commit()

    def unread_dj_message_counts(self) -> dict:
        """{username: count} of un-read (from the admin's perspective)
        DJ->admin messages, for the roster's unread badge. Only usernames
        with at least one unread message are present."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT dj_username, COUNT(*) AS n FROM messages "
                "WHERE sender = 'dj' AND acked_at IS NULL GROUP BY dj_username"
            ).fetchall()
            return {r["dj_username"]: r["n"] for r in rows}
