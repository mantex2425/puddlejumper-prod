"""
test_tad.py — unit tests for the TAD primitive.

Coverage:
  Part 1: compute_offer_expectations
    - Idle, stacked, orphan
    - "Already halfway there" (Gemini scenario)
    - Missing fields, anchor-less prev offer
    - UTC enforcement (v2.1 Section III)
    - Frozen dataclass discipline

  Part 2: evaluate_tad_gate
    - Pickup leg pass / fail / lost-mode-overshoot
    - Dropoff leg pass / fail / lost-mode-overshoot (long-way-round)
    - Short-trip absolute gate (in window / overshoot)
    - Time signal tight / loose / zero boost
    - Exit velocity timeout forces time signal off
    - Caller-driven Lost Mode (narrative_blindness)
    - Per-offer Lost Mode (narrative_violation)
    - Cluster.latest UTC enforcement
    - Missing per_offer_state entry
    - XOR routing by actual_pickup_at
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
from typing import Optional

import pytest

from cluster_detection import Cluster
from pudo_types import Offer, TargetSpec
from tad import (
    DISTANCE_GATE_COMPLETION_THRESHOLD,
    DISTANCE_GATE_OVERSHOOT_THRESHOLD,
    SHORT_TRIP_ABSOLUTE_TOLERANCE_MILES,
    OfferExpectations,
    OfferTadState,
    TadVerdict,
    compute_offer_expectations,
    evaluate_tad_gate,
)


NOW = datetime(2026, 5, 7, 14, 0, 0, tzinfo=timezone.utc)


# =============================================================================
# Test fixtures
# =============================================================================

def _make_target(lat: float = 29.76, lng: float = -95.37) -> TargetSpec:
    return TargetSpec(
        lat=lat, lng=lng,
        address_class="single_road",
        named_roads=("test road",),
    )


def _make_offer(
    offer_id: str,
    accepted_at: datetime,
    pickup_miles: Optional[float] = 5.0,
    pickup_minutes: Optional[int] = 8,
    trip_miles: Optional[float] = 12.0,
    trip_minutes: Optional[int] = 18,
) -> Offer:
    return Offer(
        offer_id=offer_id,
        accepted_at=accepted_at,
        pickup=_make_target(),
        dropoff=_make_target(lat=29.80, lng=-95.40),
        pickup_miles=pickup_miles,
        trip_miles=trip_miles,
        pickup_minutes=pickup_minutes,
        trip_minutes=trip_minutes,
    )


def _make_cluster(
    latest: datetime = NOW,
    median_lat: float = 29.76,
    median_lng: float = -95.37,
) -> Cluster:
    """Minimal Cluster for TAD tests. Spatial position is irrelevant to TAD —
    only cluster.latest matters for time signal scoring."""
    return Cluster(
        n=4,
        median_lat=median_lat,
        median_lng=median_lng,
        spread_m=0.0,
        duration_s=20.0,
        latest=latest,
    )


def _make_state(
    offer_id: str,
    miles_at_offer_receipt: float = 100.0,
    accepted_at: datetime = NOW - timedelta(minutes=10),
    expected_pickup_arrival_time: Optional[datetime] = None,
    expected_pickup_distance: float = 105.0,
    actual_pickup_at: Optional[datetime] = None,
    cumulative_miles_at_pickup_fire: Optional[float] = None,
    pickup_exit_time: Optional[datetime] = None,
    exit_velocity_timeout: bool = False,
) -> OfferTadState:
    if expected_pickup_arrival_time is None:
        expected_pickup_arrival_time = accepted_at + timedelta(minutes=8)
    return OfferTadState(
        offer_id=offer_id,
        miles_at_offer_receipt=miles_at_offer_receipt,
        accepted_at=accepted_at,
        expected_pickup_arrival_time=expected_pickup_arrival_time,
        expected_pickup_distance=expected_pickup_distance,
        actual_pickup_at=actual_pickup_at,
        cumulative_miles_at_pickup_fire=cumulative_miles_at_pickup_fire,
        pickup_exit_time=pickup_exit_time,
        exit_velocity_timeout=exit_velocity_timeout,
    )


# =============================================================================
# Part 1: compute_offer_expectations
# =============================================================================

class TestIdleCase:
    def test_idle_pickup_anchors(self):
        offer = _make_offer("offer-A", accepted_at=NOW,
                            pickup_miles=5.0, pickup_minutes=8)
        result = compute_offer_expectations(
            new_offer=offer, prev_offer=None,
            current_odometer=100.0, now=NOW,
        )
        assert result is not None
        assert result.expected_pickup_arrival_time == NOW + timedelta(minutes=8)
        assert result.expected_pickup_distance == pytest.approx(105.0)

    def test_idle_dropoff_anchors_chain_from_pickup(self):
        offer = _make_offer("offer-A", accepted_at=NOW,
                            pickup_miles=5.0, pickup_minutes=8,
                            trip_miles=12.0, trip_minutes=18)
        result = compute_offer_expectations(
            new_offer=offer, prev_offer=None,
            current_odometer=100.0, now=NOW,
        )
        assert result.expected_dropoff_arrival_time == NOW + timedelta(minutes=26)
        assert result.expected_dropoff_distance == pytest.approx(117.0)


class TestStackedCase:
    def test_stacked_pickup_anchors_chain_from_prev_dropoff(self):
        prev = _make_offer("prev-A", accepted_at=NOW - timedelta(minutes=10),
                           pickup_miles=3.0, pickup_minutes=5,
                           trip_miles=8.0, trip_minutes=12)
        prev_dropoff_eta = NOW + timedelta(minutes=15)
        prev_dropoff_dist = 114.0

        new = _make_offer("offer-B", accepted_at=NOW,
                          pickup_miles=2.0, pickup_minutes=4,
                          trip_miles=6.0, trip_minutes=10)
        result = compute_offer_expectations(
            new_offer=new, prev_offer=prev,
            current_odometer=105.0, now=NOW,
            prev_expected_dropoff_arrival_time=prev_dropoff_eta,
            prev_expected_dropoff_distance=prev_dropoff_dist,
        )
        assert result.expected_pickup_arrival_time == NOW + timedelta(minutes=19)
        assert result.expected_pickup_distance == pytest.approx(116.0)
        assert result.expected_dropoff_arrival_time == NOW + timedelta(minutes=29)
        assert result.expected_dropoff_distance == pytest.approx(122.0)


class TestOrphanCase:
    def test_orphan_falls_back_to_idle(self):
        prev = _make_offer("prev-A", accepted_at=NOW - timedelta(hours=1))
        prev_dropoff_eta = NOW - timedelta(minutes=5)
        prev_dropoff_dist = 95.0

        new = _make_offer("offer-B", accepted_at=NOW,
                          pickup_miles=4.0, pickup_minutes=6)
        result = compute_offer_expectations(
            new_offer=new, prev_offer=prev,
            current_odometer=100.0, now=NOW,
            prev_expected_dropoff_arrival_time=prev_dropoff_eta,
            prev_expected_dropoff_distance=prev_dropoff_dist,
        )
        assert result.expected_pickup_arrival_time == NOW + timedelta(minutes=6)
        assert result.expected_pickup_distance == pytest.approx(104.0)

    def test_orphan_emits_log_warning(self, caplog):
        prev = _make_offer("prev-orphan", accepted_at=NOW - timedelta(hours=1))
        prev_dropoff_eta = NOW - timedelta(minutes=5)
        new = _make_offer("new-after-orphan", accepted_at=NOW)

        with caplog.at_level(logging.WARNING, logger="tad"):
            compute_offer_expectations(
                new_offer=new, prev_offer=prev,
                current_odometer=100.0, now=NOW,
                prev_expected_dropoff_arrival_time=prev_dropoff_eta,
                prev_expected_dropoff_distance=95.0,
            )

        assert any(
            "new-after-orphan" in rec.message and "prev-orphan" in rec.message
            for rec in caplog.records
        )


class TestAlreadyHalfwayThere:
    def test_short_pickup_accepted_mid_approach(self):
        offer = _make_offer("halfway-offer", accepted_at=NOW,
                            pickup_miles=0.8, pickup_minutes=2,
                            trip_miles=4.0, trip_minutes=8)
        result = compute_offer_expectations(
            new_offer=offer, prev_offer=None,
            current_odometer=50.0, now=NOW,
        )
        assert result.expected_pickup_distance == pytest.approx(50.8)
        assert result.expected_pickup_arrival_time == NOW + timedelta(minutes=2)
        assert result.expected_dropoff_distance == pytest.approx(54.8)


class TestMissingFields:
    def test_missing_pickup_minutes_returns_none(self):
        offer = _make_offer("no-pickup-min", accepted_at=NOW, pickup_minutes=None)
        result = compute_offer_expectations(
            new_offer=offer, prev_offer=None,
            current_odometer=100.0, now=NOW,
        )
        assert result is None

    def test_missing_trip_miles_returns_none(self):
        offer = _make_offer("no-trip-mi", accepted_at=NOW, trip_miles=None)
        result = compute_offer_expectations(
            new_offer=offer, prev_offer=None,
            current_odometer=100.0, now=NOW,
        )
        assert result is None


class TestPrevOfferWithoutAnchors:
    def test_prev_offer_with_no_anchors_uses_idle(self):
        prev = _make_offer("prev-pre-2c2", accepted_at=NOW - timedelta(minutes=20))
        new = _make_offer("offer-after-prev", accepted_at=NOW,
                          pickup_miles=3.0, pickup_minutes=5)
        result = compute_offer_expectations(
            new_offer=new, prev_offer=prev,
            current_odometer=200.0, now=NOW,
            prev_expected_dropoff_arrival_time=None,
            prev_expected_dropoff_distance=None,
        )
        assert result.expected_pickup_arrival_time == NOW + timedelta(minutes=5)
        assert result.expected_pickup_distance == pytest.approx(203.0)


class TestOfferExpectationsImmutability:
    def test_cannot_mutate_after_construction(self):
        offer = _make_offer("freeze-test", accepted_at=NOW)
        result = compute_offer_expectations(
            new_offer=offer, prev_offer=None,
            current_odometer=100.0, now=NOW,
        )
        with pytest.raises(Exception):
            result.expected_pickup_distance = 999.0  # type: ignore[misc]


class TestUtcEnforcement:
    def test_naive_now_raises(self):
        offer = _make_offer("naive-test", accepted_at=NOW)
        naive_now = datetime(2026, 5, 7, 14, 0, 0)
        with pytest.raises(ValueError, match="must be tz-aware UTC"):
            compute_offer_expectations(
                new_offer=offer, prev_offer=None,
                current_odometer=100.0, now=naive_now,
            )

    def test_naive_prev_dropoff_anchor_raises(self):
        prev = _make_offer("prev-naive", accepted_at=NOW - timedelta(minutes=20))
        new = _make_offer("new-after-naive", accepted_at=NOW)
        naive_prev_dropoff = datetime(2026, 5, 7, 14, 30, 0)
        with pytest.raises(ValueError, match="must be tz-aware UTC"):
            compute_offer_expectations(
                new_offer=new, prev_offer=prev,
                current_odometer=100.0, now=NOW,
                prev_expected_dropoff_arrival_time=naive_prev_dropoff,
                prev_expected_dropoff_distance=95.0,
            )


# =============================================================================
# Part 2: evaluate_tad_gate — pickup leg
# =============================================================================

class TestPickupLegInWindow:
    """Pickup leg: distance gate passes (85%-115%), normal-mode verdict."""

    def test_at_85_percent_completion_passes(self):
        offer = _make_offer("p-85", accepted_at=NOW - timedelta(minutes=8),
                            pickup_miles=10.0, pickup_minutes=15)
        state = _make_state(
            "p-85",
            miles_at_offer_receipt=100.0,
            expected_pickup_distance=110.0,  # 100 + 10
            expected_pickup_arrival_time=NOW,
        )
        cluster = _make_cluster(latest=NOW)
        verdicts = evaluate_tad_gate(
            cluster=cluster,
            queue_offers=(offer,),
            current_odometer=108.5,  # 8.5/10 = 85% completion
            per_offer_state={"p-85": state},
        )
        v = verdicts["p-85"]
        assert v.passed is True
        assert v.leg_evaluated == "pickup"
        assert v.lost_mode_reason is None
        assert v.distance_gate["passed"] is True
        assert v.distance_gate["completion_pct"] == pytest.approx(0.85, rel=1e-6)

    def test_at_100_percent_completion_passes(self):
        offer = _make_offer("p-100", accepted_at=NOW - timedelta(minutes=8),
                            pickup_miles=10.0, pickup_minutes=15)
        state = _make_state(
            "p-100", miles_at_offer_receipt=100.0,
            expected_pickup_distance=110.0,
            expected_pickup_arrival_time=NOW,
        )
        cluster = _make_cluster(latest=NOW)
        verdicts = evaluate_tad_gate(
            cluster=cluster, queue_offers=(offer,),
            current_odometer=110.0,
            per_offer_state={"p-100": state},
        )
        assert verdicts["p-100"].passed is True
        assert verdicts["p-100"].time_boost == 0.15  # arrived right on time


class TestPickupLegBelowThreshold:
    """Pickup leg: distance gate fails (driver hasn't traveled enough)."""

    def test_below_85_percent_fails(self):
        offer = _make_offer("p-low", accepted_at=NOW - timedelta(minutes=4),
                            pickup_miles=10.0, pickup_minutes=15)
        state = _make_state(
            "p-low", miles_at_offer_receipt=100.0,
            expected_pickup_distance=110.0,
        )
        cluster = _make_cluster(latest=NOW)
        verdicts = evaluate_tad_gate(
            cluster=cluster, queue_offers=(offer,),
            current_odometer=103.0,  # 3/10 = 30%
            per_offer_state={"p-low": state},
        )
        v = verdicts["p-low"]
        assert v.passed is False
        assert v.lost_mode_reason is None
        assert v.distance_gate["passed"] is False
        assert v.time_signal is None  # not computed when distance fails


class TestPickupLegOvershoot:
    """Pickup leg: distance > 115% triggers Lost Mode (narrative_violation)."""

    def test_above_115_percent_triggers_lost_mode(self):
        offer = _make_offer("p-overshoot", accepted_at=NOW - timedelta(minutes=20),
                            pickup_miles=10.0, pickup_minutes=15)
        state = _make_state(
            "p-overshoot", miles_at_offer_receipt=100.0,
            expected_pickup_distance=110.0,
        )
        cluster = _make_cluster(latest=NOW)
        verdicts = evaluate_tad_gate(
            cluster=cluster, queue_offers=(offer,),
            current_odometer=112.0,  # 12/10 = 120% = overshoot
            per_offer_state={"p-overshoot": state},
        )
        v = verdicts["p-overshoot"]
        assert v.passed is False  # §XVIII: bistate verdict at leg-evaluator boundary
        assert v.lost_mode_reason == "narrative_violation"  # §XVIII.D.3: label preserved
        assert v.distance_gate["passed"] is None  # gate stays tristate (private helper)
        assert "lost mode" in v.distance_gate["fail_reason"].lower()


# =============================================================================
# Part 2: evaluate_tad_gate — dropoff leg (Long Way Round)
# =============================================================================

class TestDropoffLegInWindow:
    def test_dropoff_leg_at_100_percent_passes(self):
        offer = _make_offer("d-100", accepted_at=NOW - timedelta(minutes=30),
                            trip_miles=10.0, trip_minutes=18)
        state = _make_state(
            "d-100",
            actual_pickup_at=NOW - timedelta(minutes=20),
            cumulative_miles_at_pickup_fire=100.0,
            pickup_exit_time=NOW - timedelta(minutes=18),
        )
        cluster = _make_cluster(latest=NOW)
        verdicts = evaluate_tad_gate(
            cluster=cluster, queue_offers=(offer,),
            current_odometer=110.0,  # 10mi delta from pickup-fire = 100%
            per_offer_state={"d-100": state},
        )
        v = verdicts["d-100"]
        assert v.passed is True
        assert v.leg_evaluated == "dropoff"
        assert v.distance_gate["completion_pct"] == pytest.approx(1.0)


class TestDropoffLegLongWayRound:
    """Andrew's actual scenario: rider takes detour, distance budget blown by >15%."""

    def test_dropoff_at_120_percent_returns_passed_false(self):
        """§XVIII: long-way-round detour > 115% → bistate verdict
        (passed=False) at leg-evaluator boundary, forensic label
        preserved per §XVIII.D.3.
        """
        offer = _make_offer("long-way", accepted_at=NOW - timedelta(minutes=45),
                            trip_miles=10.0, trip_minutes=18)
        state = _make_state(
            "long-way",
            actual_pickup_at=NOW - timedelta(minutes=30),
            cumulative_miles_at_pickup_fire=100.0,
            pickup_exit_time=NOW - timedelta(minutes=28),
        )
        cluster = _make_cluster(latest=NOW)
        verdicts = evaluate_tad_gate(
            cluster=cluster, queue_offers=(offer,),
            current_odometer=112.0,  # 12mi delta = 120% of 10mi expected
            per_offer_state={"long-way": state},
        )
        v = verdicts["long-way"]
        assert v.passed is False  # §XVIII bistate verdict
        assert v.lost_mode_reason == "narrative_violation"  # §XVIII.D.3 label preserved
        assert v.leg_evaluated == "dropoff"
        assert v.distance_gate["passed"] is None  # gate stays tristate (canary)


# =============================================================================
# Part 2: evaluate_tad_gate — short-trip absolute mode
# =============================================================================

class TestShortTripAbsoluteMode:
    def test_short_trip_in_tolerance_passes(self):
        offer = _make_offer("short-ok", accepted_at=NOW - timedelta(minutes=2),
                            pickup_miles=0.8, pickup_minutes=2)
        state = _make_state(
            "short-ok", miles_at_offer_receipt=50.0,
            expected_pickup_distance=50.8,
            expected_pickup_arrival_time=NOW,
        )
        cluster = _make_cluster(latest=NOW)
        verdicts = evaluate_tad_gate(
            cluster=cluster, queue_offers=(offer,),
            current_odometer=51.0,  # 0.2mi above expected, within +-0.5
            per_offer_state={"short-ok": state},
        )
        v = verdicts["short-ok"]
        assert v.passed is True
        assert v.distance_gate["mode"] == "absolute_short_trip"

    def test_short_trip_overshoot_returns_passed_false(self):
        """§XVIII: short-trip overshoot > +0.5mi → bistate verdict
        (passed=False), forensic label preserved per §XVIII.D.3.
        """
        offer = _make_offer("short-over", accepted_at=NOW - timedelta(minutes=10),
                            pickup_miles=0.8, pickup_minutes=2)
        state = _make_state(
            "short-over", miles_at_offer_receipt=50.0,
            expected_pickup_distance=50.8,
        )
        cluster = _make_cluster(latest=NOW)
        verdicts = evaluate_tad_gate(
            cluster=cluster, queue_offers=(offer,),
            current_odometer=52.0,  # 1.2mi above expected, > +0.5 tolerance
            per_offer_state={"short-over": state},
        )
        v = verdicts["short-over"]
        assert v.passed is False  # §XVIII bistate verdict
        assert v.lost_mode_reason == "narrative_violation"  # §XVIII.D.3 label preserved
        assert v.distance_gate["passed"] is None  # gate stays tristate (canary)


# =============================================================================
# Part 2: evaluate_tad_gate — time signal asymmetry
# =============================================================================

class TestTimeSignalBoost:
    def test_arrived_on_time_gets_tight_boost(self):
        offer = _make_offer("on-time", accepted_at=NOW - timedelta(minutes=8),
                            pickup_miles=10.0, pickup_minutes=15)
        state = _make_state(
            "on-time", miles_at_offer_receipt=100.0,
            expected_pickup_distance=110.0,
            expected_pickup_arrival_time=NOW,
        )
        cluster = _make_cluster(latest=NOW)
        verdicts = evaluate_tad_gate(
            cluster=cluster, queue_offers=(offer,),
            current_odometer=109.0,
            per_offer_state={"on-time": state},
        )
        assert verdicts["on-time"].time_boost == 0.15

    def test_modestly_late_gets_loose_boost(self):
        # ~30% time error → in (15%, 50%] range
        offer = _make_offer("modest-late", accepted_at=NOW - timedelta(minutes=14),
                            pickup_miles=10.0, pickup_minutes=10)
        state = _make_state(
            "modest-late", miles_at_offer_receipt=100.0,
            expected_pickup_distance=110.0,
            # expected was NOW - 4min; cluster arrived NOW = 4 min late on 10min leg = 40%
            expected_pickup_arrival_time=NOW - timedelta(minutes=4),
        )
        cluster = _make_cluster(latest=NOW)
        verdicts = evaluate_tad_gate(
            cluster=cluster, queue_offers=(offer,),
            current_odometer=109.0,
            per_offer_state={"modest-late": state},
        )
        assert verdicts["modest-late"].time_boost == 0.05

    def test_very_late_gets_zero_boost_no_penalty(self):
        # >50% time error → 0 boost (no penalty)
        offer = _make_offer("very-late", accepted_at=NOW - timedelta(minutes=20),
                            pickup_miles=10.0, pickup_minutes=10)
        state = _make_state(
            "very-late", miles_at_offer_receipt=100.0,
            expected_pickup_distance=110.0,
            expected_pickup_arrival_time=NOW - timedelta(minutes=10),
        )
        cluster = _make_cluster(latest=NOW)
        verdicts = evaluate_tad_gate(
            cluster=cluster, queue_offers=(offer,),
            current_odometer=109.0,
            per_offer_state={"very-late": state},
        )
        assert verdicts["very-late"].time_boost == 0.0
        assert verdicts["very-late"].passed is True  # distance gate still passed


class TestExitVelocityTimeout:
    def test_dropoff_with_timeout_forces_time_signal_off(self):
        offer = _make_offer("festival", accepted_at=NOW - timedelta(minutes=60),
                            trip_miles=10.0, trip_minutes=18)
        state = _make_state(
            "festival",
            actual_pickup_at=NOW - timedelta(minutes=45),
            cumulative_miles_at_pickup_fire=100.0,
            pickup_exit_time=None,
            exit_velocity_timeout=True,
        )
        cluster = _make_cluster(latest=NOW)
        verdicts = evaluate_tad_gate(
            cluster=cluster, queue_offers=(offer,),
            current_odometer=109.0,  # in window
            per_offer_state={"festival": state},
        )
        v = verdicts["festival"]
        assert v.passed is True
        assert v.time_boost == 0.0
        assert v.time_signal["applied"] is False
        assert v.time_signal["reason"] == "exit_velocity_timeout"


# =============================================================================
# Part 2: evaluate_tad_gate — Lost Mode (caller-driven)
# =============================================================================

class TestLostModeNarrativeBlindness:
    def test_caller_driven_lost_mode_returns_none_verdicts(self):
        offer = _make_offer("blind-1", accepted_at=NOW - timedelta(minutes=8),
                            pickup_miles=10.0, pickup_minutes=15)
        state = _make_state(
            "blind-1", miles_at_offer_receipt=100.0,
            expected_pickup_distance=110.0,
        )
        cluster = _make_cluster(latest=NOW)
        verdicts = evaluate_tad_gate(
            cluster=cluster, queue_offers=(offer,),
            current_odometer=109.0,
            per_offer_state={"blind-1": state},
            lost_mode=True,
            last_known_anchor_id="prev-confirmed-offer",
        )
        v = verdicts["blind-1"]
        assert v.passed is None
        assert v.lost_mode_reason == "narrative_blindness"
        assert v.time_boost == 0.0
        assert v.distance_gate["last_known_anchor_id"] == "prev-confirmed-offer"


# =============================================================================
# Part 2: evaluate_tad_gate — XOR routing + edge cases
# =============================================================================

class TestXorRouting:
    def test_pickup_pending_routes_to_pickup_leg(self):
        offer = _make_offer("xor-pickup", accepted_at=NOW - timedelta(minutes=8))
        state = _make_state(
            "xor-pickup", actual_pickup_at=None,
            expected_pickup_distance=105.0,
        )
        cluster = _make_cluster(latest=NOW)
        verdicts = evaluate_tad_gate(
            cluster=cluster, queue_offers=(offer,),
            current_odometer=104.0,
            per_offer_state={"xor-pickup": state},
        )
        assert verdicts["xor-pickup"].leg_evaluated == "pickup"

    def test_pickup_confirmed_routes_to_dropoff_leg(self):
        offer = _make_offer("xor-dropoff", accepted_at=NOW - timedelta(minutes=30),
                            trip_miles=10.0, trip_minutes=18)
        state = _make_state(
            "xor-dropoff",
            actual_pickup_at=NOW - timedelta(minutes=20),
            cumulative_miles_at_pickup_fire=100.0,
            pickup_exit_time=NOW - timedelta(minutes=18),
        )
        cluster = _make_cluster(latest=NOW)
        verdicts = evaluate_tad_gate(
            cluster=cluster, queue_offers=(offer,),
            current_odometer=110.0,
            per_offer_state={"xor-dropoff": state},
        )
        assert verdicts["xor-dropoff"].leg_evaluated == "dropoff"


class TestMissingState:
    def test_offer_without_state_records_failed_verdict(self, caplog):
        offer = _make_offer("no-state", accepted_at=NOW)
        cluster = _make_cluster(latest=NOW)
        with caplog.at_level(logging.WARNING, logger="tad"):
            verdicts = evaluate_tad_gate(
                cluster=cluster, queue_offers=(offer,),
                current_odometer=100.0,
                per_offer_state={},
            )
        v = verdicts["no-state"]
        assert v.passed is False
        assert v.distance_gate["mode"] == "missing_state"


class TestClusterUtcEnforcement:
    def test_naive_cluster_latest_raises(self):
        cluster = Cluster(
            n=4, median_lat=29.76, median_lng=-95.37,
            spread_m=0.0, duration_s=20.0,
            latest=datetime(2026, 5, 7, 14, 0, 0),  # naive
        )
        with pytest.raises(ValueError, match="must be tz-aware UTC"):
            evaluate_tad_gate(
                cluster=cluster, queue_offers=(),
                current_odometer=100.0,
                per_offer_state={},
            )


class TestEmptyQueue:
    def test_empty_queue_returns_empty_dict(self):
        cluster = _make_cluster(latest=NOW)
        verdicts = evaluate_tad_gate(
            cluster=cluster, queue_offers=(),
            current_odometer=100.0,
            per_offer_state={},
        )
        assert verdicts == {}


class TestTadVerdictImmutability:
    def test_cannot_mutate_after_construction(self):
        offer = _make_offer("freeze-v", accepted_at=NOW)
        state = _make_state("freeze-v", expected_pickup_distance=105.0)
        cluster = _make_cluster(latest=NOW)
        verdicts = evaluate_tad_gate(
            cluster=cluster, queue_offers=(offer,),
            current_odometer=104.0,
            per_offer_state={"freeze-v": state},
        )
        with pytest.raises(Exception):
            verdicts["freeze-v"].passed = False  # type: ignore[misc]