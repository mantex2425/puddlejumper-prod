"""
tests/test_voice_helper.py

Unit tests for driver_heartbeat._voice_for_actions().

Cut B3 voice spec:
  - State-changing actions (FirePickup, FireDropoff) get voice
  - Detection-only actions (LogNoMatch, LogAmbiguousMatch, LogPickupRematch)
    are silent — they surface via /driver/status.last_3_dispatch_actions
    for forensic review instead.
  - Implicit cancel pair: FireDropoff(outcome="canceled") wins over the
    paired FirePickup; produces "Implicit cancel, new ride starting".
  - Priority: canceled > pickup_missed > normal dropoff > pickup.
"""

from driver_heartbeat import _voice_for_actions
from dispatch import (
    FirePickup,
    FireDropoff,
    LogNoMatch,
    LogPickupRematch,
    LogAmbiguousMatch,
)


# -- Silent paths --------------------------------------------------------------

def test_empty_list_silent():
    """No executed actions → response omits voice field."""
    assert _voice_for_actions([]) is None


def test_log_no_match_silent():
    """LogNoMatch on every empty-queue heartbeat — must NOT spam voice."""
    assert _voice_for_actions([LogNoMatch()]) is None


def test_log_pickup_rematch_silent():
    """LogPickupRematch is DEBUG-level idempotency safety net — silent."""
    assert _voice_for_actions([LogPickupRematch("OFFER1")]) is None


def test_log_ambiguous_match_silent():
    """LogAmbiguousMatch repeats every 5s while driver sits in ambiguous zone.
    Voicing it would spam every heartbeat — surface via status endpoint instead.
    """
    actions = [LogAmbiguousMatch(candidates=(), reason="test")]
    assert _voice_for_actions(actions) is None


# -- Single state-change actions ----------------------------------------------

def test_fire_pickup_alone():
    assert _voice_for_actions([FirePickup("OFFER1")]) == "Pickup confirmed"


def test_fire_dropoff_normal():
    assert _voice_for_actions([FireDropoff("OFFER1")]) == "Dropoff confirmed"


def test_fire_dropoff_canceled():
    actions = [FireDropoff("OFFER1", outcome="canceled")]
    assert _voice_for_actions(actions) == "Implicit cancel, new ride starting"


def test_fire_dropoff_pickup_missed():
    actions = [FireDropoff("OFFER1", outcome="pickup_missed")]
    assert _voice_for_actions(actions) == "Dropoff confirmed, pickup was missed"


# -- The interesting one: implicit cancel pair (Case D) -----------------------

def test_implicit_cancel_pair_case_d():
    """Case D from §4: driver accepts new offer while ENROUTE to old one.
    dispatch() emits BOTH FireDropoff(canceled) and FirePickup; the canceled
    string wins to avoid two confusing voice utterances back-to-back.
    """
    actions = [
        FireDropoff("OLD_OFFER", outcome="canceled"),
        FirePickup("NEW_OFFER"),
    ]
    assert _voice_for_actions(actions) == "Implicit cancel, new ride starting"


# -- Priority ordering (defensive — not real dispatch outputs but well-defined) -

def test_priority_canceled_beats_pickup_missed():
    """Two FireDropoffs in one list — canceled wins."""
    actions = [
        FireDropoff("A", outcome="pickup_missed"),
        FireDropoff("B", outcome="canceled"),
    ]
    assert _voice_for_actions(actions) == "Implicit cancel, new ride starting"


def test_priority_dropoff_beats_pickup():
    """FireDropoff(None) + FirePickup (not a real dispatch case but priority defined) — dropoff wins."""
    actions = [
        FireDropoff("A"),
        FirePickup("B"),
    ]
    assert _voice_for_actions(actions) == "Dropoff confirmed"


def test_silent_actions_alongside_state_change_dont_block_voice():
    """If a LogNoMatch is somehow next to a FirePickup (defensive), pickup still voices."""
    actions = [LogNoMatch(), FirePickup("OFFER1")]
    assert _voice_for_actions(actions) == "Pickup confirmed"