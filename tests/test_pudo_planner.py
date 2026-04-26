"""
test_pudo_planner.py - Unit tests for pudo_planner.py PLAN consumer.

Phase E Step 5.2. Skeleton with shared fixtures + 5 smoke tests.

The 64-test build is staged across subsequent sub-steps:
  Step 5.3 - Section A (temporal pattern detection):  24 tests
  Step 5.4 - Section C (decision builders):           13 tests
  Step 5.5 - Section B (STACKED disambiguation):       8 tests
  Step 5.6 - Section D (consume() dispatch):          16 tests
  Step 5.7 - Contract introspection:                   3 tests

Locked floor at end of Step 5: 246 (Gemini ratification 2026-04-26).
Step 5.2 ships 5 smoke tests so collection works and CI floor advances.

Database-blind throughout. Planner does not read or write the DB; tests
inject a fake clock and a plain dict for state storage.
"""
from __future__ import annotations

import datetime
from datetime import timezone
from typing import Optional

import pytest

from cluster_detection import Cluster
from pudo_planner import (
    PudoPlanner,
    _DriverTemporalState,
)
from pudo_types import (
    DriverStateSnapshot,
    PlannerDecision,
    States,
    WhereAmIResult,
)


# ============================================================================
# Sentinel values - shared defaults for factories
# ============================================================================

# Forum Park 7623 cluster median (canonical motivating case). Same coords as
# test_where_am_i.py defaults so reasoning carries across both files.
_DEFAULT_LAT = 29.6246
_DEFAULT_LNG = -95.5102

# Sentinel "now" for any factory not given an explicit clock. Hardcoded so
# test runs are reproducible across machines and CI.
_DEFAULT_NOW = datetime.datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc)


# ============================================================================
# _FakeClock - deterministic time injection
# ============================================================================

class _FakeClock:
    """Deterministic clock for planner tests. Class form, not closure.

    Why a class: PudoPlanner expects _now_fn to be callable. The class's
    __call__ method satisfies that. The mutator advance() is a separate
    public method so tests express heartbeat cadence as:

        clock = _FakeClock()
        planner = PudoPlanner(_now_fn=clock, _state_store={})
        planner.consume(...)            # heartbeat 1
        clock.advance(5.0)
        planner.consume(...)            # heartbeat 2 (5s later)

    A closure would have to expose advance() via closure cells, harder to
    inspect when a test fails.
    """

    def __init__(self, start: Optional[datetime.datetime] = None):
        self._now = start or _DEFAULT_NOW

    def __call__(self) -> datetime.datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        """Roll the internal time forward by `seconds`."""
        self._now = self._now + datetime.timedelta(seconds=seconds)


# ============================================================================
# Factories - WhereAmIResult, DriverStateSnapshot, _DriverTemporalState
# ============================================================================

def _wai(
    *,
    status: str = "at_current_pudo",
    pudo_type: Optional[str] = "pickup",
    offer_id: Optional[str] = "offer_7623",
    corrected_lat: Optional[float] = _DEFAULT_LAT,
    corrected_lng: Optional[float] = _DEFAULT_LNG,
    on_wire: bool = True,
    current_road: Optional[str] = "Settemont Road",
    on_target_road: bool = True,
    off_wire_duration_s: int = 0,
    stop_context: Optional[str] = "unknown_stop",
    confidence: float = 0.78,
    reason: str = "intersection conf=0.78 [test default]",
    target_address: Optional[str] = None,
    ghost_id: Optional[int] = None,
    cluster: Optional[Cluster] = None,
) -> WhereAmIResult:
    """Build a WhereAmIResult with Forum-Park-7623 success defaults.

    Default = at_current_pudo, pickup, confidence above STRONG_MATCH (0.7).
    Override per test:
      _wai(status="at_unknown_pudo", offer_id=None, pudo_type=None)
      _wai(status="not_at_pudo", confidence=0.0, cluster=None)
      _wai(offer_id="offer_OTHER")        # different-offer rearm

    target_address defaults to None per backlog B-15: TargetSpec has no
    `address` field yet, so where_am_i._build_outcome's
    getattr(target, "address", None) always returns None today. Step 5.5
    STACKED tests assert identity via offer_id, not target_address.
    """
    if cluster is None:
        cluster = Cluster(
            n=8,
            median_lat=corrected_lat if corrected_lat is not None else _DEFAULT_LAT,
            median_lng=corrected_lng if corrected_lng is not None else _DEFAULT_LNG,
            spread_m=15.0,
            duration_s=30.0,
        )
    return WhereAmIResult(
        status=status,
        pudo_type=pudo_type,
        offer_id=offer_id,
        corrected_lat=corrected_lat,
        corrected_lng=corrected_lng,
        on_wire=on_wire,
        current_road=current_road,
        on_target_road=on_target_road,
        off_wire_duration_s=off_wire_duration_s,
        stop_context=stop_context,
        confidence=confidence,
        reason=reason,
        target_address=target_address,
        ghost_id=ghost_id,
        cluster=cluster,
    )


def _snapshot(
    *,
    state: str = States.ENROUTE,
    current_offer_id: Optional[str] = "offer_7623",
    primary_offer_id: Optional[str] = "offer_7623",
    pickup_lat: Optional[float] = _DEFAULT_LAT,
    pickup_lng: Optional[float] = _DEFAULT_LNG,
    dropoff_lat: Optional[float] = 29.7000,
    dropoff_lng: Optional[float] = -95.4000,
    secondary_pickup_lat: Optional[float] = None,
    secondary_pickup_lng: Optional[float] = None,
    secondary_dropoff_lat: Optional[float] = None,
    secondary_dropoff_lng: Optional[float] = None,
) -> DriverStateSnapshot:
    """Build a DriverStateSnapshot. Default = ENROUTE single-offer.

    STACKED tests override:
      state=States.STACKED,
      primary_offer_id="primary",
      current_offer_id="secondary",
      secondary_pickup_lat=..., secondary_pickup_lng=...
    """
    return DriverStateSnapshot(
        state=state,
        current_offer_id=current_offer_id,
        primary_offer_id=primary_offer_id,
        pickup_lat=pickup_lat,
        pickup_lng=pickup_lng,
        dropoff_lat=dropoff_lat,
        dropoff_lng=dropoff_lng,
        secondary_pickup_lat=secondary_pickup_lat,
        secondary_pickup_lng=secondary_pickup_lng,
        secondary_dropoff_lat=secondary_dropoff_lat,
        secondary_dropoff_lng=secondary_dropoff_lng,
    )


def _state(
    *,
    armed_at: Optional[datetime.datetime] = None,
    armed_offer_id: str = "offer_7623",
    armed_pudo_type: str = "pickup",
    heartbeat_count: int = 1,
    last_seen_status: str = "at_current_pudo",
    last_seen_at: Optional[datetime.datetime] = None,
    recent_observations: tuple = (),
) -> _DriverTemporalState:
    """Build a _DriverTemporalState. Default = freshly armed (1 heartbeat).

    armed_at and last_seen_at default to _DEFAULT_NOW so tests can
    construct a state and reason about gap durations without a clock.
    """
    if armed_at is None:
        armed_at = _DEFAULT_NOW
    if last_seen_at is None:
        last_seen_at = armed_at
    return _DriverTemporalState(
        armed_at=armed_at,
        armed_offer_id=armed_offer_id,
        armed_pudo_type=armed_pudo_type,
        heartbeat_count=heartbeat_count,
        last_seen_status=last_seen_status,
        last_seen_at=last_seen_at,
        recent_observations=recent_observations,
    )


# ============================================================================
# Step 5.2 smoke tests - skeleton sanity
# ============================================================================
#
# Five tests, intentionally minimal. They prove:
#   1. Module imports resolve (file compiles, all 8 imports valid)
#   2-4. Each factory constructs a valid dataclass instance
#   5. End-to-end: assembled PudoPlanner.consume() returns a PlannerDecision
#      with default fixtures (no behavioral assertion - that's Step 5.6)
#
# Behavioral test coverage for consume() dispatch arrives in Step 5.6.

class TestStep52Smoke:
    """Skeleton smoke - prove the assembly compiles and connects."""

    def test_wai_factory_returns_valid_result(self):
        result = _wai()
        assert isinstance(result, WhereAmIResult)
        assert result.status == "at_current_pudo"
        assert result.cluster is not None

    def test_snapshot_factory_returns_valid_snapshot(self):
        snap = _snapshot()
        assert isinstance(snap, DriverStateSnapshot)
        assert snap.state == States.ENROUTE
        assert snap.current_offer_id == "offer_7623"

    def test_state_factory_returns_valid_state(self):
        state = _state()
        assert isinstance(state, _DriverTemporalState)
        assert state.heartbeat_count == 1
        assert state.armed_pudo_type == "pickup"

    def test_fake_clock_advances(self):
        clock = _FakeClock()
        t0 = clock()
        clock.advance(5.0)
        t1 = clock()
        assert (t1 - t0).total_seconds() == pytest.approx(5.0)

    def test_planner_consume_smoke_returns_decision(self):
        """End-to-end skeleton smoke: assembled planner returns a
        PlannerDecision. No behavioral assertion - that's Step 5.6.
        """
        clock = _FakeClock()
        store: dict = {}
        planner = PudoPlanner(_now_fn=clock, _state_store=store)
        decision = planner.consume(
            "driver_smoke",
            _wai(),
            driver_state_snapshot=_snapshot(),
        )
        assert isinstance(decision, PlannerDecision)


# ============================================================================
# Step 5.3 - Section A temporal pattern detection (24 tests)
# ============================================================================
#
# Coverage of the pure-function helpers used by consume() to advance the
# armed-candidate state machine across heartbeats.
#
# _make_observation              - 1 test  (TestMakeObservation)
# _is_stable_match               - 5 tests (TestIsStableMatch)
# _is_brief_disappearance        - 4 tests (TestIsBriefDisappearance)
# _is_long_stop_then_departure   - 5 tests (TestIsLongStopThenDeparture)
# _advance_armed_state           - 8 tests (TestAdvanceArmedState)
# OBSERVATION_WINDOW invariant   - 1 test  (TestObservationWindow)

from pudo_planner import (
    _make_observation,
    _is_stable_match,
    _is_brief_disappearance,
    _is_long_stop_then_departure,
    _advance_armed_state,
    _LastWAIObservation,
    CANDIDATE_STALE_SECONDS,
    OBSERVATION_WINDOW,
)


# ============================================================================
# TestMakeObservation - 1 test
# ============================================================================

class TestMakeObservation:
    def test_projects_wai_to_observation(self):
        # _make_observation copies the 5 fields it cares about + observed_at.
        wai = _wai(
            status="at_current_pudo",
            confidence=0.82,
            offer_id="offer_xyz",
            corrected_lat=29.62,
            corrected_lng=-95.51,
        )
        obs = _make_observation(wai, now=_DEFAULT_NOW)
        assert isinstance(obs, _LastWAIObservation)
        assert obs.status == "at_current_pudo"
        assert obs.confidence == 0.82
        assert obs.offer_id == "offer_xyz"
        assert obs.corrected_lat == 29.62
        assert obs.corrected_lng == -95.51
        assert obs.observed_at == _DEFAULT_NOW


# ============================================================================
# TestIsStableMatch - 5 tests
# ============================================================================

class TestIsStableMatch:
    def test_status_not_at_current_pudo_false(self):
        # Status guard short-circuits before count check.
        state = _state(heartbeat_count=10)
        wai = _wai(status="not_at_pudo")
        assert _is_stable_match(state, wai, n_required=3) is False

    def test_offer_id_mismatch_false(self):
        state = _state(armed_offer_id="A", heartbeat_count=10)
        wai = _wai(offer_id="B")
        assert _is_stable_match(state, wai, n_required=3) is False

    def test_pudo_type_mismatch_false(self):
        state = _state(armed_pudo_type="pickup", heartbeat_count=10)
        wai = _wai(pudo_type="dropoff")
        assert _is_stable_match(state, wai, n_required=3) is False

    def test_below_count_false(self):
        state = _state(heartbeat_count=2)
        wai = _wai()
        assert _is_stable_match(state, wai, n_required=3) is False

    def test_at_count_true(self):
        # >=N: boundary returns True.
        state = _state(heartbeat_count=3)
        wai = _wai()
        assert _is_stable_match(state, wai, n_required=3) is True


# ============================================================================
# TestIsBriefDisappearance - 4 tests
# ============================================================================

class TestIsBriefDisappearance:
    def test_at_current_pudo_returns_false(self):
        # Hit case is the disqualifier - this helper only fires on misses.
        state = _state(last_seen_at=_DEFAULT_NOW)
        wai = _wai(status="at_current_pudo")
        assert _is_brief_disappearance(state, wai, now=_DEFAULT_NOW) is False

    def test_inside_window_returns_true(self):
        state = _state(last_seen_at=_DEFAULT_NOW)
        wai = _wai(status="not_at_pudo")
        later = _DEFAULT_NOW + datetime.timedelta(seconds=10)
        assert _is_brief_disappearance(state, wai, now=later) is True

    def test_at_window_boundary_returns_false(self):
        # Strict < CANDIDATE_STALE_SECONDS - boundary exactly is False.
        state = _state(last_seen_at=_DEFAULT_NOW)
        wai = _wai(status="not_at_pudo")
        later = _DEFAULT_NOW + datetime.timedelta(
            seconds=CANDIDATE_STALE_SECONDS
        )
        assert _is_brief_disappearance(state, wai, now=later) is False

    def test_above_window_returns_false(self):
        state = _state(last_seen_at=_DEFAULT_NOW)
        wai = _wai(status="not_at_pudo")
        later = _DEFAULT_NOW + datetime.timedelta(seconds=20)
        assert _is_brief_disappearance(state, wai, now=later) is False


# ============================================================================
# TestIsLongStopThenDeparture - 5 tests
# ============================================================================

class TestIsLongStopThenDeparture:
    def test_at_current_pudo_returns_false(self):
        # Even with massive gap, hit case disqualifies (driver still here).
        state = _state(heartbeat_count=5, last_seen_at=_DEFAULT_NOW)
        wai = _wai(status="at_current_pudo")
        later = _DEFAULT_NOW + datetime.timedelta(seconds=300)
        assert _is_long_stop_then_departure(state, wai, now=later) is False

    def test_count_zero_returns_false(self):
        # heartbeat_count<1: never armed in earnest, ignore.
        state = _state(heartbeat_count=0, last_seen_at=_DEFAULT_NOW)
        wai = _wai(status="not_at_pudo")
        later = _DEFAULT_NOW + datetime.timedelta(seconds=30)
        assert _is_long_stop_then_departure(state, wai, now=later) is False

    def test_below_window_returns_false(self):
        # gap < CANDIDATE_STALE_SECONDS: brief, not departure.
        state = _state(heartbeat_count=5, last_seen_at=_DEFAULT_NOW)
        wai = _wai(status="not_at_pudo")
        later = _DEFAULT_NOW + datetime.timedelta(seconds=10)
        assert _is_long_stop_then_departure(state, wai, now=later) is False

    def test_at_window_boundary_returns_true(self):
        # >= CANDIDATE_STALE_SECONDS: boundary IS departure (mirror of brief).
        state = _state(heartbeat_count=5, last_seen_at=_DEFAULT_NOW)
        wai = _wai(status="not_at_pudo")
        later = _DEFAULT_NOW + datetime.timedelta(
            seconds=CANDIDATE_STALE_SECONDS
        )
        assert _is_long_stop_then_departure(state, wai, now=later) is True

    def test_above_window_returns_true(self):
        state = _state(heartbeat_count=5, last_seen_at=_DEFAULT_NOW)
        wai = _wai(status="not_at_pudo")
        later = _DEFAULT_NOW + datetime.timedelta(seconds=30)
        assert _is_long_stop_then_departure(state, wai, now=later) is True


# ============================================================================
# TestAdvanceArmedState - 8 tests
# ============================================================================

class TestAdvanceArmedState:
    def test_case1_cold_start_arms_fresh(self):
        # state=None, wai=at_current_pudo -> fresh _DriverTemporalState.
        result = _advance_armed_state(None, _wai(), now=_DEFAULT_NOW)
        assert result is not None
        assert result.heartbeat_count == 1
        assert result.armed_offer_id == "offer_7623"
        assert result.armed_pudo_type == "pickup"
        assert result.armed_at == _DEFAULT_NOW
        assert len(result.recent_observations) == 1

    def test_case1a_cold_start_no_pudo_returns_none(self):
        # state=None, wai=not_at_pudo: nothing to arm.
        wai = _wai(status="not_at_pudo", offer_id=None, pudo_type=None)
        result = _advance_armed_state(None, wai, now=_DEFAULT_NOW)
        assert result is None

    def test_case1b_cold_start_missing_offer_id_returns_none(self):
        # Defensive guard: status="at_current_pudo" with offer_id=None
        # is malformed WAI - refuse to arm.
        wai = _wai(status="at_current_pudo", offer_id=None)
        result = _advance_armed_state(None, wai, now=_DEFAULT_NOW)
        assert result is None

    def test_case2_different_offer_rearms_fresh(self):
        # state had offer_A, wai matches offer_B -> fresh state, count=1.
        prev = _state(armed_offer_id="A", heartbeat_count=2)
        wai = _wai(offer_id="B")
        result = _advance_armed_state(prev, wai, now=_DEFAULT_NOW)
        assert result is not None
        assert result.armed_offer_id == "B"
        assert result.heartbeat_count == 1
        assert result.armed_at == _DEFAULT_NOW

    def test_case2b_different_pudo_type_rearms_fresh(self):
        # state armed pickup, wai matches dropoff for same offer -> rearm.
        prev = _state(
            armed_offer_id="X", armed_pudo_type="pickup", heartbeat_count=2
        )
        wai = _wai(offer_id="X", pudo_type="dropoff")
        result = _advance_armed_state(prev, wai, now=_DEFAULT_NOW)
        assert result is not None
        assert result.armed_pudo_type == "dropoff"
        assert result.heartbeat_count == 1

    def test_case3_reaffirm_increments(self):
        # state matches, wai matches: count++ and observation appended.
        prev = _state(heartbeat_count=2, recent_observations=())
        later = _DEFAULT_NOW + datetime.timedelta(seconds=5)
        result = _advance_armed_state(prev, _wai(), now=later)
        assert result is not None
        assert result.heartbeat_count == 3
        assert result.last_seen_at == later
        assert len(result.recent_observations) == 1

    def test_case4_brief_dip_preserves_last_seen_at(self):
        # state existed, wai not_at_pudo within window:
        # state preserved BUT last_seen_at NOT advanced. The gap-measurement
        # reference must stay at the last hit, not advance to the dissent -
        # otherwise brief-dip would silently extend forever as long as
        # heartbeats kept arriving below threshold.
        prev = _state(heartbeat_count=3, last_seen_at=_DEFAULT_NOW)
        wai = _wai(status="not_at_pudo", offer_id=None, pudo_type=None)
        later = _DEFAULT_NOW + datetime.timedelta(seconds=10)
        result = _advance_armed_state(prev, wai, now=later)
        assert result is not None
        assert result.heartbeat_count == 3
        assert result.last_seen_at == _DEFAULT_NOW
        assert result.last_seen_status == "not_at_pudo"
        assert len(result.recent_observations) == 1

    def test_case5_long_gap_clears(self):
        # state existed, wai not_at_pudo beyond window: candidate dies.
        prev = _state(heartbeat_count=3, last_seen_at=_DEFAULT_NOW)
        wai = _wai(status="not_at_pudo", offer_id=None, pudo_type=None)
        later = _DEFAULT_NOW + datetime.timedelta(seconds=30)
        result = _advance_armed_state(prev, wai, now=later)
        assert result is None


# ============================================================================
# TestObservationWindow - 1 test
# ============================================================================

class TestObservationWindow:
    def test_observations_capped_at_window(self):
        # recent_observations slice keeps at most OBSERVATION_WINDOW entries.
        # Drive 5 reaffirmations through _advance_armed_state and confirm
        # the tuple stays bounded.
        state = None
        wai = _wai()
        clock_t = _DEFAULT_NOW
        for _ in range(5):
            state = _advance_armed_state(state, wai, now=clock_t)
            clock_t += datetime.timedelta(seconds=5)
        assert state is not None
        assert state.heartbeat_count == 5
        assert len(state.recent_observations) == OBSERVATION_WINDOW


# ============================================================================
# Step 5.4 - Section C decision builders (13 tests)
# ============================================================================
#
# One happy-path test per builder (11 builders), plus:
#   - TestBuildCacheGhost::test_null_cluster_defensive
#   - TestAllBuildersReturnFrozen::test_frozen_instance_error_on_mutation
#
# R1 enforcement (no coordinate synthesis on reconcile) is verified
# inline in the two reconcile tests via explicit None assertions.
#
# Tests assert action literal correctness, field population correctness,
# and that fields meant to be None for the action are in fact None. This
# catches a builder accidentally setting a payload it shouldn't or
# omitting a payload it should set.

import dataclasses
from cluster_detection import Cluster
from pudo_planner import (
    _build_noop,
    _build_arm_candidate,
    _build_cancel_candidate,
    _build_fire_pickup,
    _build_fire_dropoff,
    _build_fire_retroactive,
    _build_fire_stacked_swap,
    _build_fire_stacked_revert,
    _build_cache_ghost,
    _build_reconcile_missed_pickup,
    _build_reconcile_missed_dropoff,
)


# ============================================================================
# TestBuildNoop - 1 test
# ============================================================================

class TestBuildNoop:
    def test_returns_correct_shape(self):
        d = _build_noop("nothing to do")
        assert d.action == "noop"
        assert d.offer_id is None
        assert d.target_state is None
        assert d.corrected_lat is None
        assert d.corrected_lng is None
        assert d.ghost_insert_payload is None
        assert d.reconciliation_payload is None
        assert d.reason == "nothing to do"


# ============================================================================
# TestBuildArmCandidate - 1 test
# ============================================================================

class TestBuildArmCandidate:
    def test_returns_correct_shape(self):
        d = _build_arm_candidate(
            offer_id="offer_xyz",
            pudo_type="pickup",
            heartbeat_count=2,
            reason="armed conf=0.55",
        )
        assert d.action == "arm_candidate"
        assert d.offer_id == "offer_xyz"
        assert d.target_state is None
        assert d.corrected_lat is None
        assert d.corrected_lng is None
        assert d.ghost_insert_payload is None
        assert d.reconciliation_payload is None
        # Reason includes the heartbeat count and type per builder docstring.
        assert "armed conf=0.55" in d.reason
        assert "heartbeat 2" in d.reason
        assert "type=pickup" in d.reason


# ============================================================================
# TestBuildCancelCandidate - 1 test
# ============================================================================

class TestBuildCancelCandidate:
    def test_returns_correct_shape(self):
        d = _build_cancel_candidate("candidate cleared (was armed for 3hb)")
        assert d.action == "cancel_candidate"
        assert d.offer_id is None
        assert d.target_state is None
        assert d.corrected_lat is None
        assert d.corrected_lng is None
        assert d.ghost_insert_payload is None
        assert d.reconciliation_payload is None
        assert "cleared" in d.reason


# ============================================================================
# TestBuildFirePickup - 1 test
# ============================================================================

class TestBuildFirePickup:
    def test_returns_correct_shape(self):
        d = _build_fire_pickup(
            offer_id="offer_7623",
            corrected_lat=29.6246,
            corrected_lng=-95.5102,
            target_state="IN_TRIP",
            reason="stable match 3hb conf=0.82",
        )
        assert d.action == "fire_pickup"
        assert d.offer_id == "offer_7623"
        assert d.target_state == "IN_TRIP"
        assert d.corrected_lat == 29.6246
        assert d.corrected_lng == -95.5102
        assert d.ghost_insert_payload is None
        assert d.reconciliation_payload is None
        assert "stable match" in d.reason


# ============================================================================
# TestBuildFireDropoff - 1 test
# ============================================================================

class TestBuildFireDropoff:
    def test_returns_correct_shape(self):
        d = _build_fire_dropoff(
            offer_id="offer_7623",
            corrected_lat=29.7000,
            corrected_lng=-95.4000,
            target_state="UNCOMMITTED",
            reason="stable match 3hb conf=0.78",
        )
        assert d.action == "fire_dropoff"
        assert d.offer_id == "offer_7623"
        assert d.target_state == "UNCOMMITTED"
        assert d.corrected_lat == 29.7000
        assert d.corrected_lng == -95.4000
        assert d.ghost_insert_payload is None
        assert d.reconciliation_payload is None


# ============================================================================
# TestBuildFireRetroactive - 1 test
# ============================================================================

class TestBuildFireRetroactive:
    def test_returns_correct_shape(self):
        d = _build_fire_retroactive(
            offer_id="offer_7623",
            pudo_type="pickup",
            corrected_lat=29.6246,
            corrected_lng=-95.5102,
            target_state="IN_TRIP",
            reason="long stop then departure: armed for 4hb",
        )
        assert d.action == "fire_retroactive"
        assert d.offer_id == "offer_7623"
        assert d.target_state == "IN_TRIP"
        assert d.corrected_lat == 29.6246
        assert d.corrected_lng == -95.5102
        assert d.ghost_insert_payload is None
        assert d.reconciliation_payload is None
        # Reason annotated with the retroactive type per builder.
        assert "retroactive pickup" in d.reason


# ============================================================================
# TestBuildFireStackedSwap - 1 test
# ============================================================================

class TestBuildFireStackedSwap:
    def test_returns_correct_shape(self):
        d = _build_fire_stacked_swap(
            primary_offer_id="offer_PRIMARY",
            secondary_offer_id="offer_SECONDARY",
            corrected_lat=29.6246,
            corrected_lng=-95.5102,
            reason="S32 implicit STACKED cancel",
        )
        assert d.action == "fire_stacked_swap"
        # Secondary becomes the new active offer per swap semantics.
        assert d.offer_id == "offer_SECONDARY"
        assert d.target_state == "IN_TRIP"
        assert d.corrected_lat == 29.6246
        assert d.corrected_lng == -95.5102
        assert d.ghost_insert_payload is None
        # Payload carries both ids and the swap direction.
        assert d.reconciliation_payload is not None
        assert d.reconciliation_payload["swap_kind"] == "primary_to_secondary"
        assert d.reconciliation_payload["primary_offer_id"] == "offer_PRIMARY"
        assert (
            d.reconciliation_payload["secondary_offer_id"] == "offer_SECONDARY"
        )


# ============================================================================
# TestBuildFireStackedRevert - 1 test
# ============================================================================

class TestBuildFireStackedRevert:
    def test_returns_correct_shape(self):
        d = _build_fire_stacked_revert(
            primary_offer_id="offer_PRIMARY",
            secondary_offer_id="offer_SECONDARY",
            corrected_lat=29.6246,
            corrected_lng=-95.5102,
            reason="S35 Uber re-award",
        )
        assert d.action == "fire_stacked_revert"
        # Primary restored as the active offer per revert semantics.
        assert d.offer_id == "offer_PRIMARY"
        assert d.target_state == "ENROUTE"
        assert d.corrected_lat == 29.6246
        assert d.corrected_lng == -95.5102
        assert d.ghost_insert_payload is None
        assert d.reconciliation_payload is not None
        assert d.reconciliation_payload["swap_kind"] == "secondary_to_primary"
        assert d.reconciliation_payload["primary_offer_id"] == "offer_PRIMARY"
        assert (
            d.reconciliation_payload["secondary_offer_id"] == "offer_SECONDARY"
        )


# ============================================================================
# TestBuildCacheGhost - 2 tests (happy + null-cluster defensive)
# ============================================================================

class TestBuildCacheGhost:
    def test_with_cluster(self):
        cluster = Cluster(
            n=8,
            median_lat=29.6246,
            median_lng=-95.5102,
            spread_m=18.0,
            duration_s=42.0,
        )
        d = _build_cache_ghost(
            cluster_lat=29.6246,
            cluster_lng=-95.5102,
            cluster=cluster,
            state_at_time="ENROUTE",
            confidence=0.55,
            reason="at_unknown_pudo conf=0.55",
        )
        assert d.action == "cache_ghost"
        assert d.offer_id is None
        assert d.target_state is None
        assert d.corrected_lat == 29.6246
        assert d.corrected_lng == -95.5102
        assert d.reconciliation_payload is None
        # Payload carries cluster geometry + state + confidence.
        assert d.ghost_insert_payload is not None
        p = d.ghost_insert_payload
        assert p["lat"] == 29.6246
        assert p["lng"] == -95.5102
        assert p["cluster_spread_m"] == 18.0
        assert p["cluster_duration_s"] == 42.0
        assert p["state_at_time"] == "ENROUTE"
        assert p["confidence"] == 0.55

    def test_null_cluster_defensive(self):
        # Defensive: cluster=None should produce a payload with spread/duration
        # as None rather than raising AttributeError on .spread_m access.
        d = _build_cache_ghost(
            cluster_lat=29.6246,
            cluster_lng=-95.5102,
            cluster=None,
            state_at_time="UNCOMMITTED",
            confidence=0.0,
            reason="defensive null cluster",
        )
        assert d.action == "cache_ghost"
        assert d.ghost_insert_payload is not None
        assert d.ghost_insert_payload["cluster_spread_m"] is None
        assert d.ghost_insert_payload["cluster_duration_s"] is None


# ============================================================================
# TestBuildReconcileMissedPickup - 1 test (R1 enforcement)
# ============================================================================

class TestBuildReconcileMissedPickup:
    def test_r1_no_coordinate_synthesis(self):
        # R1 (state-correction over coordinate-synthesis): the reconcile
        # builder MUST emit corrected_lat/lng as None. Coordinates from the
        # cluster median are ignored on purpose - audit-trail honesty wins
        # over inferred location data.
        d = _build_reconcile_missed_pickup(
            offer_id="offer_calhoun",
            target_state="UNCOMMITTED",
            suspected_pudo_id=42,
            reason="dropoff fired without prior pickup confirmation",
        )
        assert d.action == "reconcile_missed_pickup"
        assert d.offer_id == "offer_calhoun"
        assert d.target_state == "UNCOMMITTED"
        # R1 enforcement - the load-bearing assertions of this test.
        assert d.corrected_lat is None
        assert d.corrected_lng is None
        assert d.ghost_insert_payload is None
        # Payload carries the reconciliation instructions for EXECUTE.
        assert d.reconciliation_payload is not None
        p = d.reconciliation_payload
        assert p["missed_pudo_type"] == "pickup"
        assert p["offer_id"] == "offer_calhoun"
        assert p["suspected_pudo_id"] == 42
        assert p["offer_history_updates"]["pickup_missed"] is True
        assert (
            p["offer_history_updates"]["pickup_inference_source"]
            == "dropoff_completed"
        )


# ============================================================================
# TestBuildReconcileMissedDropoff - 1 test (R1 enforcement, symmetric)
# ============================================================================

class TestBuildReconcileMissedDropoff:
    def test_r1_no_coordinate_synthesis(self):
        # Symmetric to test_r1_no_coordinate_synthesis above. Same R1 policy:
        # the next-ride pickup fired successfully, but the prior ride's
        # dropoff was never confirmed. EXECUTE must update offer_history
        # honestly without synthesizing the missed dropoff coords.
        d = _build_reconcile_missed_dropoff(
            offer_id="offer_prior",
            target_state="ENROUTE",
            suspected_pudo_id=None,
            reason="next pickup fired without prior dropoff confirmation",
        )
        assert d.action == "reconcile_missed_dropoff"
        assert d.offer_id == "offer_prior"
        assert d.target_state == "ENROUTE"
        # R1 enforcement.
        assert d.corrected_lat is None
        assert d.corrected_lng is None
        assert d.ghost_insert_payload is None
        assert d.reconciliation_payload is not None
        p = d.reconciliation_payload
        assert p["missed_pudo_type"] == "dropoff"
        assert p["offer_id"] == "offer_prior"
        assert p["suspected_pudo_id"] is None
        assert p["offer_history_updates"]["dropoff_missed"] is True
        assert (
            p["offer_history_updates"]["dropoff_inference_source"]
            == "next_pickup_completed"
        )


# ============================================================================
# TestAllBuildersReturnFrozen - 1 test
# ============================================================================

class TestAllBuildersReturnFrozen:
    def test_frozen_instance_error_on_mutation(self):
        # PlannerDecision is frozen=True. Any builder's output must raise
        # FrozenInstanceError on attribute mutation. _build_noop is the
        # cheapest sentinel; the frozen-ness is a property of the dataclass
        # not the builder, so one sentinel proves the contract.
        d = _build_noop("sentinel")
        with pytest.raises(dataclasses.FrozenInstanceError):
            d.action = "fire_pickup"  # type: ignore
