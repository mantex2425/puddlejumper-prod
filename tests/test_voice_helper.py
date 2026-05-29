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
  - Priority: canceled > pickup_missed > normal dropoff > observation dropoff
    > pickup > observation pickup.

§XVIII LOST-MODE EXTENSION (2026-05-27):
  - FirePickupObservation and FireDropoffObservation also voice. The driver
    hears identical wording for narrative and observation variants because
    the architectural distinction is internal noise from the driver's
    perspective.
  - Voice utterances are deduped against the last-voiced (offer_id,
    action_type) tuple per driver. The same Observation refiring multiple
    times within a short window produces voice only once. Different
    offers, or different legs of the same offer, voice independently.

NEW RETURN CONTRACT (2026-05-27):
  _voice_for_actions returns a three-tuple
  (voice_string, voiced_offer_id, voiced_action_type). All three are None
  when no voice should be emitted. The non-None offer_id and action_type
  are persisted by the call site to driver_trip_state for the next
  heartbeat's dedup check.
"""

from driver_heartbeat import _voice_for_actions
from dispatch import (
    FirePickup,
    FireDropoff,
    FirePickupObservation,
    FireDropoffObservation,
    LogNoMatch,
    LogPickupRematch,
    LogAmbiguousMatch,
)


# -- Silent paths --------------------------------------------------------------

def test_empty_list_silent():
    """No executed actions → response omits voice field."""
    voice, offer_id, action_type = _voice_for_actions([])
    assert voice is None
    assert offer_id is None
    assert action_type is None


def test_log_no_match_silent():
    """LogNoMatch on every empty-queue heartbeat — must NOT spam voice."""
    voice, offer_id, action_type = _voice_for_actions([LogNoMatch()])
    assert voice is None
    assert offer_id is None
    assert action_type is None


def test_log_pickup_rematch_silent():
    """LogPickupRematch is DEBUG-level idempotency safety net — silent."""
    voice, offer_id, action_type = _voice_for_actions([LogPickupRematch("OFFER1")])
    assert voice is None
    assert offer_id is None
    assert action_type is None


def test_log_ambiguous_match_silent():
    """LogAmbiguousMatch repeats every 5s while driver sits in ambiguous zone.
    Voicing it would spam every heartbeat — surface via status endpoint instead.
    """
    actions = [LogAmbiguousMatch(candidates=(), reason="test")]
    voice, offer_id, action_type = _voice_for_actions(actions)
    assert voice is None
    assert offer_id is None
    assert action_type is None


# -- Single narrative state-change actions ------------------------------------

def test_fire_pickup_alone():
    voice, offer_id, action_type = _voice_for_actions([FirePickup("OFFER1")])
    assert voice == "Pickup confirmed, OFFER1"
    assert offer_id == "OFFER1"
    assert action_type == "pickup"


def test_fire_dropoff_normal():
    voice, offer_id, action_type = _voice_for_actions([FireDropoff("OFFER1")])
    assert voice == "Dropoff confirmed, OFFER1"
    assert offer_id == "OFFER1"
    assert action_type == "dropoff"


def test_fire_dropoff_canceled():
    actions = [FireDropoff("OFFER1", outcome="canceled")]
    voice, offer_id, action_type = _voice_for_actions(actions)
    assert voice == "Implicit cancel, new ride starting, OFFER1"
    assert offer_id == "OFFER1"
    assert action_type == "cancel"


def test_fire_dropoff_pickup_missed():
    actions = [FireDropoff("OFFER1", outcome="pickup_missed")]
    voice, offer_id, action_type = _voice_for_actions(actions)
    assert voice == "Dropoff confirmed, pickup was missed, OFFER1"
    assert offer_id == "OFFER1"
    assert action_type == "dropoff_missed_pickup"


# -- Observation variants (§XVIII lost-mode demotion path) --------------------

def test_fire_pickup_observation_voices():
    """§XVIII lost-mode demotion path: pickup observation produces voice
    using the same wording as the narrative variant. The driver does not
    distinguish observation from narrative.
    """
    voice, offer_id, action_type = _voice_for_actions(
        [FirePickupObservation("OFFER1")]
    )
    assert voice == "Pickup confirmed, OFFER1"
    assert offer_id == "OFFER1"
    assert action_type == "pickup"


def test_fire_dropoff_observation_voices():
    """§XVIII lost-mode demotion path: dropoff observation produces voice
    using the same wording as the narrative variant.
    """
    voice, offer_id, action_type = _voice_for_actions(
        [FireDropoffObservation("OFFER1")]
    )
    assert voice == "Dropoff confirmed, OFFER1"
    assert offer_id == "OFFER1"
    assert action_type == "dropoff"


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
    voice, offer_id, action_type = _voice_for_actions(actions)
    assert voice == "Implicit cancel, new ride starting, OLD_OFFER"
    assert offer_id == "OLD_OFFER"
    assert action_type == "cancel"


# -- Priority ordering (defensive — not real dispatch outputs but well-defined) -

def test_priority_canceled_beats_pickup_missed():
    """Two FireDropoffs in one list — canceled wins."""
    actions = [
        FireDropoff("A", outcome="pickup_missed"),
        FireDropoff("B", outcome="canceled"),
    ]
    voice, offer_id, action_type = _voice_for_actions(actions)
    assert voice == "Implicit cancel, new ride starting, B"
    assert offer_id == "B"
    assert action_type == "cancel"


def test_priority_dropoff_beats_pickup():
    """FireDropoff(None) + FirePickup — dropoff wins."""
    actions = [
        FireDropoff("A"),
        FirePickup("B"),
    ]
    voice, offer_id, action_type = _voice_for_actions(actions)
    assert voice == "Dropoff confirmed, A"
    assert offer_id == "A"
    assert action_type == "dropoff"


def test_priority_narrative_dropoff_beats_observation_dropoff():
    """FireDropoff + FireDropoffObservation — narrative wins (priority 3 vs 4)."""
    actions = [
        FireDropoff("A"),
        FireDropoffObservation("B"),
    ]
    voice, offer_id, action_type = _voice_for_actions(actions)
    assert voice == "Dropoff confirmed, A"
    assert offer_id == "A"
    assert action_type == "dropoff"


def test_priority_narrative_pickup_beats_observation_pickup():
    """FirePickup + FirePickupObservation — narrative wins (priority 5 vs 6)."""
    actions = [
        FirePickup("A"),
        FirePickupObservation("B"),
    ]
    voice, offer_id, action_type = _voice_for_actions(actions)
    assert voice == "Pickup confirmed, A"
    assert offer_id == "A"
    assert action_type == "pickup"


def test_silent_actions_alongside_state_change_dont_block_voice():
    """If a LogNoMatch is next to a FirePickup (defensive), pickup still voices."""
    actions = [LogNoMatch(), FirePickup("OFFER1")]
    voice, offer_id, action_type = _voice_for_actions(actions)
    assert voice == "Pickup confirmed, OFFER1"
    assert offer_id == "OFFER1"
    assert action_type == "pickup"


# -- Dedup gate (2026-05-27 §XVIII Observation storm protection) --------------

def test_dedup_same_offer_same_action_suppresses():
    """Observation firing again for the SAME (offer, action) tuple as the
    last voiced utterance is suppressed. This is the production case from
    2026-05-27: offer 8441 fired FirePickupObservation 3 times in 8 minutes
    — without dedup the driver would hear "Pickup confirmed" three times.
    """
    actions = [FirePickupObservation("OFFER1")]
    voice, offer_id, action_type = _voice_for_actions(
        actions,
        last_voiced_offer_id="OFFER1",
        last_voiced_action_type="pickup",
    )
    assert voice is None
    assert offer_id is None
    assert action_type is None


def test_dedup_different_offer_voices():
    """A new offer ID always voices, even with the same action type."""
    actions = [FirePickupObservation("OFFER2")]
    voice, offer_id, action_type = _voice_for_actions(
        actions,
        last_voiced_offer_id="OFFER1",
        last_voiced_action_type="pickup",
    )
    assert voice == "Pickup confirmed, OFFER2"
    assert offer_id == "OFFER2"
    assert action_type == "pickup"


def test_dedup_same_offer_different_action_voices():
    """The dropoff voices even when the pickup was the last voiced for the
    same offer. A pickup-then-dropoff cycle on one offer produces two
    distinct utterances.
    """
    actions = [FireDropoffObservation("OFFER1")]
    voice, offer_id, action_type = _voice_for_actions(
        actions,
        last_voiced_offer_id="OFFER1",
        last_voiced_action_type="pickup",
    )
    assert voice == "Dropoff confirmed, OFFER1"
    assert offer_id == "OFFER1"
    assert action_type == "dropoff"


def test_dedup_narrative_and_observation_share_key():
    """A FireDropoff (narrative) and FireDropoffObservation share the
    dedup key 'dropoff' for a given offer. After a narrative dropoff
    voices for OFFER1, an observation dropoff for OFFER1 should suppress.
    Same physical event from the driver's perspective.
    """
    # First: narrative dropoff fires. Voice produced.
    voice1, oid1, atype1 = _voice_for_actions([FireDropoff("OFFER1")])
    assert voice1 == "Dropoff confirmed, OFFER1"
    assert oid1 == "OFFER1"
    assert atype1 == "dropoff"

    # Second: observation dropoff fires with prior state from voice1.
    voice2, oid2, atype2 = _voice_for_actions(
        [FireDropoffObservation("OFFER1")],
        last_voiced_offer_id=oid1,
        last_voiced_action_type=atype1,
    )
    assert voice2 is None
    assert oid2 is None
    assert atype2 is None


def test_dedup_fresh_session_voices():
    """No prior voiced state (None, None) → first event voices normally.
    Models the post-deploy / post-restart case and a fresh driver session.
    """
    voice, offer_id, action_type = _voice_for_actions(
        [FirePickupObservation("OFFER1")],
        last_voiced_offer_id=None,
        last_voiced_action_type=None,
    )
    assert voice == "Pickup confirmed, OFFER1"
    assert offer_id == "OFFER1"
    assert action_type == "pickup"
