"""Unit tests for dispatch() -- SIMPLIFIED_ARCHITECTURE.md §4 + §5.

Cut B1 stage 2 of 3.

Tests the pure-function case-resolution logic. dispatch() is a Pure Pipe;
these tests verify input-to-output mapping without any I/O fixtures.

Coverage: 7 test functions, 15 parametrize items.
  test_case_a_no_match (x2)              §4 Case A + border-filter silent drop
  test_case_b_single_pickup_no_active    §4 Case B
  test_case_c_dropoff_of_active          §4 Case C
  test_case_d_implicit_cancel_then_pickup §4 Case D (asserts ordering)
  test_case_e_disambiguation (x7)        §5.1, §5.2, §5.3 + 3+match unenumerated
  test_case_f_dropoff_missed_pickup (x2) §4 Case F (current=None / other_offer)
  test_case_g_pickup_rematch_while_active §4 Case G
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

import pytest

from dispatch import (
    FireDropoff,
    FirePickup,
    LogAmbiguousMatch,
    LogNoMatch,
    LogPickupRematch,
    dispatch,
)
from pudo_types import OfferMeta, WAIMatch


# ============================================================================
# Helpers
# ============================================================================


def _wm(
    offer_id: str,
    location_type: Literal["pickup", "dropoff"],
    confidence: float = 0.5,
) -> WAIMatch:
    """Build a WAIMatch with confidence above WAI_CONFIDENCE_THRESHOLD.

    dispatch() does not inspect confidence (WAI is responsible for the
    threshold filter per §3 step 3d), so the value is irrelevant to test
    outcomes. Default 0.5 is well above the 0.40 floor.
    """
    return WAIMatch(
        offer_id=offer_id,
        location_type=location_type,
        confidence=confidence,
    )


def _meta(created_at: datetime | None = None) -> OfferMeta:
    """Sentinel OfferMeta for test queue_metadata dicts.

    Phase 2 migration helper. Most dispatch tests don't exercise the
    recency tiebreaker, so the timestamp value is irrelevant — any
    UTC-aware datetime satisfies OfferMeta.__post_init__. Pass a
    specific created_at when the test needs to control ordering
    (e.g., the §5.3 recency tiebreaker test in Phase 5).
    """
    if created_at is None:
        created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return OfferMeta(created_at=created_at)


# Match shapes referenced by parametrize rows (keeps each row readable
# and lets the expected LogAmbiguousMatch.candidates tuple be built
# from the same source).
_M_ERRANDS = [_wm("123", "pickup"), _wm("123", "dropoff")]
_M_HOT_SWAP = [_wm("1", "dropoff"), _wm("2", "pickup")]
_M_TWO_PICKUPS = [_wm("A", "pickup"), _wm("B", "pickup")]
_M_THREE_MATCH = [
    _wm("X", "pickup"),
    _wm("Y", "pickup"),
    _wm("Z", "dropoff"),
]


# ============================================================================
# §4 Case A -- empty match list and border-filter silent drop
# ============================================================================


@pytest.mark.parametrize(
    "matches, queue_metadata",
    [
        pytest.param([], {}, id="empty_match_list"),
        pytest.param(
            [_wm("STALE_A", "pickup"), _wm("STALE_B", "dropoff")],
            {"123": _meta()},
            id="all_stale_border_filter",
        ),
    ],
)
def test_case_a_no_match(matches, queue_metadata):
    """§4 Case A: empty (or fully-filtered) match list -> LogNoMatch.

    Border filter coverage: matches whose offer_ids are absent from
    queue_metadata are silently dropped per §10 A8 belt-and-suspenders.
    With every match dropped, the function reaches Case A naturally.
    No Action is emitted for the dropped matches themselves.
    """
    actions = dispatch(matches, None, queue_metadata)
    assert actions == [LogNoMatch()]


# ============================================================================
# §4 Case B -- single pickup, no active ride
# ============================================================================


def test_case_b_single_pickup_no_active():
    """§4 Case B: pickup match, current_offer_id=None -> FirePickup."""
    matches = [_wm("123", "pickup")]
    actions = dispatch(matches, None, {"123": _meta()})
    assert actions == [FirePickup("123")]


# ============================================================================
# §4 Case C -- dropoff of the active ride
# ============================================================================


def test_case_c_dropoff_of_active():
    """§4 Case C: dropoff match, current_offer_id == matched_id."""
    matches = [_wm("123", "dropoff")]
    actions = dispatch(matches, "123", {"123": _meta()})
    assert actions == [FireDropoff("123")]
    # outcome=None means the wiring layer logs INFO (normal completion);
    # contrast with Case D's "canceled" and Case F's "pickup_missed".
    assert actions[0].outcome is None


# ============================================================================
# §4 Case D -- implicit cancel + new pickup (Ghost Ride)
# ============================================================================


def test_case_d_implicit_cancel_then_pickup():
    """§4 Case D: pickup of different offer while active -> cancel + pickup.

    Asserts ordering: FireDropoff(canceled) MUST precede FirePickup so the
    wiring layer clears current_offer_id before setting it anew. List
    equality in Python is positional, so == comparison verifies order.
    """
    matches = [_wm("NEW", "pickup")]
    actions = dispatch(matches, "OLD", {"NEW": _meta(), "OLD": _meta()})
    assert actions == [
        FireDropoff("OLD", outcome="canceled"),
        FirePickup("NEW"),
    ]


# ============================================================================
# §5 Disambiguation -- multi-match scenarios
# ============================================================================


@pytest.mark.parametrize(
    "matches, current_offer_id, expected",
    [
        # §5.1 errands -- ride not started
        pytest.param(
            _M_ERRANDS,
            None,
            [FirePickup("123")],
            id="51_errands_ride_not_started",
        ),
        # §5.1 errands -- ride active for the same offer
        pytest.param(
            _M_ERRANDS,
            "123",
            [FireDropoff("123")],
            id="51_errands_ride_active",
        ),
        # §5.1 errands -- a DIFFERENT ride is active. Unenumerated by
        # §5.1 (assumes current_offer_id in {None, offer_id}). Fail closed.
        pytest.param(
            _M_ERRANDS,
            "456",
            [LogAmbiguousMatch(
                candidates=tuple(_M_ERRANDS),
                reason="errands_with_unrelated_active",
            )],
            id="51_errands_unrelated_active_fail_closed",
        ),
        # §5.2 hot-swap -- clean (current matches the dropoff offer)
        pytest.param(
            _M_HOT_SWAP,
            "1",
            [FireDropoff("1"), FirePickup("2")],
            id="52_hot_swap_clean",
        ),
        # §5.2 hot-swap -- current matches neither side. Unenumerated.
        pytest.param(
            _M_HOT_SWAP,
            "999",
            [LogAmbiguousMatch(
                candidates=tuple(_M_HOT_SWAP),
                reason="hot_swap_without_matching_active",
            )],
            id="52_hot_swap_broken_fail_closed",
        ),
        # §5.3 two-pickups case removed in Phase 2 (2026-05-13). Old
        # assertion was LogAmbiguousMatch(reason="two_pickups"); new
        # behavior per CANONICAL_RULES.md §XIV.I is a recency tiebreaker
        # emitting [FirePickup(winner), FirePickupObservation(loser)].
        # Coverage restored by dedicated tests in Phase 5 per v3 plan
        # item 13 (test_dispatch_two_pickups_recency_winner +
        # test_dispatch_two_dropoffs_clears_narrative).
        # 3+ matches -- unenumerated by §4/§5. Fail closed.
        pytest.param(
            _M_THREE_MATCH,
            None,
            [LogAmbiguousMatch(
                candidates=tuple(_M_THREE_MATCH),
                reason="unenumerated_multi_match",
            )],
            id="3plus_match_unenumerated_fail_closed",
        ),
    ],
)
def test_case_e_disambiguation(matches, current_offer_id, expected):
    """§4 Case E + §5.1 / §5.2 / §5.3 disambiguation rules + fail-closed."""
    queue = {m.offer_id: _meta() for m in matches}
    if current_offer_id is not None:
        queue[current_offer_id] = _meta()
    actions = dispatch(matches, current_offer_id, queue)
    assert actions == expected


# ============================================================================
# §4 Case F -- dropoff with mismatched current_offer_id (S33 missed-pickup)
# ============================================================================


@pytest.mark.parametrize(
    "current_offer_id",
    [
        pytest.param(None, id="no_active_ride"),
        pytest.param("999", id="other_offer_active"),
    ],
)
def test_case_f_dropoff_missed_pickup(current_offer_id):
    """§4 Case F: dropoff match, current_offer_id != matched_id.

    Both shapes (None and a different offer) route to the same Action:
    FireDropoff(matched_id, outcome="pickup_missed"). Per the §4 amendment
    ratified 2026-04-30, this is the simplified-architecture analog of S33
    (Calhoun missed-pickup) surfaced synchronously at the dropoff
    heartbeat. The wiring layer logs WARNING for operational tracking of
    Uber-pin-quality drift.
    """
    matches = [_wm("123", "dropoff")]
    queue = {"123": _meta()}
    if current_offer_id is not None:
        queue[current_offer_id] = _meta()
    actions = dispatch(matches, current_offer_id, queue)
    assert actions == [FireDropoff("123", outcome="pickup_missed")]
    # Explicit assertion on the outcome flag -- central to Case F semantics
    # and the WARNING vs INFO log severity branch in the wiring layer.
    assert actions[0].outcome == "pickup_missed"


# ============================================================================
# §4 Case G -- pickup re-match while active (idempotency safety net)
# ============================================================================


def test_case_g_pickup_rematch_while_active():
    """§4 Case G: pickup match, current_offer_id == matched_id -> no-op log.

    Driver returned to pickup geocode mid-trip (circled the block,
    geocode overlaps dropoff path, etc.). Idempotency safety net --
    fire_pickup must NOT fire twice for the same offer. The wiring layer
    logs DEBUG; no state change.
    """
    matches = [_wm("123", "pickup")]
    actions = dispatch(matches, "123", {"123": _meta()})
    assert actions == [LogPickupRematch("123")]



# ============================================================================
# Step F: _derive_post_offer_id (Phase 1B Deterministic Fold)
# ============================================================================
#
# Test 5 from PHASE_1B_PROPOSAL_v2.md. Validates the post-dispatch
# current_offer_id derivation in isolation from the heartbeat handler.

from driver_heartbeat import _derive_post_offer_id


def test_derive_post_offer_id_no_actions_returns_pre():
    """Empty executed_actions -> post == pre."""
    assert _derive_post_offer_id([], None) is None
    assert _derive_post_offer_id([], "offer_X") == "offer_X"


def test_derive_post_offer_id_fire_pickup_binds():
    """FirePickup(N) -> post == N."""
    assert _derive_post_offer_id([FirePickup("offer_A")], None) == "offer_A"
    # Even when pre is set (re-pickup edge), FirePickup wins
    assert _derive_post_offer_id([FirePickup("offer_B")], "offer_A") == "offer_B"


def test_derive_post_offer_id_fire_dropoff_clears():
    """FireDropoff(...) -> post == None."""
    assert _derive_post_offer_id([FireDropoff("offer_A")], "offer_A") is None
    # Even when pre was None (defensive -- shouldn't occur in production)
    assert _derive_post_offer_id([FireDropoff("offer_X")], None) is None


def test_derive_post_offer_id_case_d_dropoff_then_pickup():
    """§5.2 Case D: FireDropoff(A) then FirePickup(B) -> post == B."""
    actions = [FireDropoff("offer_A"), FirePickup("offer_B")]
    assert _derive_post_offer_id(actions, "offer_A") == "offer_B"


def test_derive_post_offer_id_log_actions_unchanged():
    """LogNoMatch / LogPickupRematch / LogAmbiguousMatch -> no state change."""
    log_actions = [LogNoMatch(), LogPickupRematch("offer_A"), LogAmbiguousMatch((), "test_ambiguous")]
    assert _derive_post_offer_id(log_actions, None) is None
    assert _derive_post_offer_id(log_actions, "offer_A") == "offer_A"
