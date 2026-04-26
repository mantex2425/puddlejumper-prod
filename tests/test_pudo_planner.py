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
