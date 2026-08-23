"""Unit tests for the pure resolution function (CONCEPT.md §4).

No I/O: these call app.state.resolve() directly with plain Python values.
"""
from app.state import FILLER, resolve


# ---------------------------------------------------------------------------
# MANUAL mode
# ---------------------------------------------------------------------------

def test_manual_pinned_and_connected_goes_on_air():
    on_air, warning = resolve("MANUAL", "dj1", {"dj1", "dj2"}, FILLER)
    assert on_air == "dj1"
    assert warning is None


def test_manual_pinned_dj_disconnects_falls_back_to_filler():
    # CONCEPT.md §11.3: pinned DJ disconnects -> Filler.
    on_air, warning = resolve("MANUAL", "dj1", set(), "dj1")
    assert on_air == FILLER
    assert warning is not None


def test_manual_pinned_dj_reconnects_is_immediately_on_air_again():
    # CONCEPT.md §11.3: pinned DJ reconnects -> immediately on air again.
    # The pin never expires, so once dj1 shows up in `connected` again,
    # resolve() puts them straight back on air with no extra state needed.
    on_air, warning = resolve("MANUAL", "dj1", {"dj1"}, FILLER)
    assert on_air == "dj1"
    assert warning is None


def test_manual_no_pin_set_stays_on_filler():
    on_air, warning = resolve("MANUAL", None, {"dj1", "dj2"}, FILLER)
    assert on_air == FILLER
    assert warning is None


def test_manual_pinned_dj_never_connected_is_filler_with_warning():
    on_air, warning = resolve("MANUAL", "dj3", {"dj1", "dj2"}, FILLER)
    assert on_air == FILLER
    assert warning is not None


# ---------------------------------------------------------------------------
# AUTO mode
# ---------------------------------------------------------------------------

def test_auto_single_connected_goes_on_air():
    on_air, warning = resolve("AUTO", None, {"dj1"}, FILLER)
    assert on_air == "dj1"
    assert warning is None


def test_auto_none_connected_is_filler():
    on_air, warning = resolve("AUTO", None, set(), "dj1")
    assert on_air == FILLER
    assert warning is None


def test_auto_multiple_connected_does_not_spontaneously_jump():
    # CONCEPT.md §11.3: AUTO with multiple connected does not switch on its
    # own as long as the currently on-air DJ is still among them.
    on_air, warning = resolve("AUTO", None, {"dj1", "dj2"}, "dj1")
    assert on_air == "dj1"
    assert warning is None

    on_air, warning = resolve("AUTO", None, {"dj1", "dj2", "dj3"}, "dj2")
    assert on_air == "dj2"
    assert warning is None


def test_auto_multiple_connected_current_on_air_drops_falls_back_to_filler_with_warning():
    on_air, warning = resolve("AUTO", None, {"dj2", "dj3"}, "dj1")
    assert on_air == FILLER
    assert warning is not None


def test_auto_does_not_guess_which_of_several_should_take_over():
    # Coming from Filler (nobody was on air) with several already
    # connected: AUTO must not guess -- stay on Filler with a warning.
    on_air, warning = resolve("AUTO", None, {"dj1", "dj2"}, FILLER)
    assert on_air == FILLER
    assert warning is not None


# ---------------------------------------------------------------------------
# Mode transitions (these are orchestrated in StateManager.set_mode, but the
# pin-capture/discard behavior itself is easy to verify against resolve()
# once the transition has been applied).
# ---------------------------------------------------------------------------

def test_after_switch_to_manual_capturing_current_dj_as_pin_keeps_them_on_air():
    # Simulates: AUTO with dj2 on air -> operator flips to MANUAL.
    # StateManager.set_mode captures pinned = "dj2" (the current on_air).
    on_air, warning = resolve("MANUAL", "dj2", {"dj1", "dj2"}, "dj2")
    assert on_air == "dj2"
    assert warning is None


def test_after_switch_to_manual_while_filler_on_air_pin_stays_null():
    # If Filler was on air, the pin stays null and Filler keeps playing
    # until the operator explicitly pins someone.
    on_air, warning = resolve("MANUAL", None, {"dj1", "dj2"}, FILLER)
    assert on_air == FILLER
    assert warning is None


def test_after_switch_to_auto_pin_is_discarded_and_auto_rules_apply():
    # Simulates: MANUAL pinned=dj1 (dj1 offline, so Filler was playing)
    # -> operator flips to AUTO. StateManager.set_mode discards the pin.
    # AUTO rules apply immediately against whatever is connected now.
    on_air, warning = resolve("AUTO", None, {"dj2"}, FILLER)
    assert on_air == "dj2"
    assert warning is None


def test_switch_to_auto_with_multiple_connected_and_none_previously_on_air():
    on_air, warning = resolve("AUTO", None, {"dj1", "dj2"}, FILLER)
    assert on_air == FILLER
    assert warning is not None
