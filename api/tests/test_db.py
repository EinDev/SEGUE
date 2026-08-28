"""Unit tests for the new (issue #2) persistence surface in app.db.Database:
event name, per-DJ schedule, and the admin<->DJ chat table.

Uses a fresh sqlite file per test via pytest's tmp_path fixture -- real I/O,
but cheap and isolated (same spirit as the rest of this module: low
concurrency, plain sqlite, no mocking needed).
"""
import pytest

from app.db import Database


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "onair.db"))


# ---------------------------------------------------------------------------
# Event name
# ---------------------------------------------------------------------------

def test_event_name_defaults_to_none(db):
    assert db.get_event_name() is None


def test_event_name_round_trips(db):
    db.set_event_name("Summer Bash 2026")
    assert db.get_event_name() == "Summer Bash 2026"


def test_event_name_blank_clears_it(db):
    db.set_event_name("Something")
    db.set_event_name("   ")
    assert db.get_event_name() is None


# ---------------------------------------------------------------------------
# Per-DJ schedule (purely informational)
# ---------------------------------------------------------------------------

def test_schedule_round_trips(db):
    db.get_or_create_dj("dj1")
    db.set_schedule("dj1", "2026-08-28T20:00:00Z", "2026-08-28T20:30:00Z")
    dj = db.get_dj("dj1")
    assert dj["scheduled_start"] == "2026-08-28T20:00:00Z"
    assert dj["scheduled_end"] == "2026-08-28T20:30:00Z"


def test_schedule_unknown_dj_raises(db):
    with pytest.raises(ValueError):
        db.set_schedule("nobody", "2026-08-28T20:00:00Z", None)


def test_new_dj_has_no_schedule(db):
    dj = db.get_or_create_dj("dj1")
    assert dj["scheduled_start"] is None
    assert dj["scheduled_end"] is None


# ---------------------------------------------------------------------------
# Messages (admin<->DJ chat)
# ---------------------------------------------------------------------------

def test_add_message_unknown_dj_raises(db):
    with pytest.raises(ValueError):
        db.add_message("nobody", "admin", "hi")


def test_add_message_invalid_sender_raises(db):
    db.get_or_create_dj("dj1")
    with pytest.raises(ValueError):
        db.add_message("dj1", "operator", "hi")


def test_add_message_empty_text_raises(db):
    db.get_or_create_dj("dj1")
    with pytest.raises(ValueError):
        db.add_message("dj1", "admin", "   ")


def test_admin_message_is_unacked_until_dj_acks(db):
    db.get_or_create_dj("dj1")
    msg = db.add_message("dj1", "admin", "bitte kurz melden")
    assert [m["id"] for m in db.unacked_admin_messages("dj1")] == [msg["id"]]

    ok = db.ack_admin_message(msg["id"], "dj1")
    assert ok is True
    assert db.unacked_admin_messages("dj1") == []


def test_ack_is_idempotent_and_scoped_to_the_right_dj(db):
    db.get_or_create_dj("dj1")
    db.get_or_create_dj("dj2")
    msg = db.add_message("dj1", "admin", "hi dj1")

    # dj2 can't ack a message addressed to dj1.
    assert db.ack_admin_message(msg["id"], "dj2") is False
    assert db.unacked_admin_messages("dj1") != []

    assert db.ack_admin_message(msg["id"], "dj1") is True
    # Second ack of the same message is a no-op, not an error.
    assert db.ack_admin_message(msg["id"], "dj1") is False


def test_dj_cannot_ack_their_own_outgoing_message(db):
    db.get_or_create_dj("dj1")
    msg = db.add_message("dj1", "dj", "frage an admin")
    assert db.ack_admin_message(msg["id"], "dj1") is False


def test_unread_dj_message_counts_and_mark_read(db):
    db.get_or_create_dj("dj1")
    db.get_or_create_dj("dj2")
    db.add_message("dj1", "dj", "frage 1")
    db.add_message("dj1", "dj", "frage 2")
    db.add_message("dj2", "dj", "andere frage")
    # An admin->dj message must not count as unread-from-dj.
    db.add_message("dj1", "admin", "antwort")

    assert db.unread_dj_message_counts() == {"dj1": 2, "dj2": 1}

    db.mark_dj_messages_read("dj1")
    assert db.unread_dj_message_counts() == {"dj2": 1}


def test_list_messages_is_chronological_and_scoped_per_dj(db):
    db.get_or_create_dj("dj1")
    db.get_or_create_dj("dj2")
    db.add_message("dj1", "admin", "eins")
    db.add_message("dj2", "admin", "für dj2")
    db.add_message("dj1", "dj", "zwei")

    texts = [m["text"] for m in db.list_messages("dj1")]
    assert texts == ["eins", "zwei"]


def test_delete_dj_drops_their_messages(db):
    db.get_or_create_dj("dj1")
    db.add_message("dj1", "admin", "hi")
    db.delete_dj("dj1")
    assert db.list_messages("dj1") == []
