"""Unit tests for motion_gate.evaluate_odometer_gate (dual-leg gate).

Covers:
  - Per-leg eligibility math
  - Threshold boundary (exactly 0.9)
  - Divide-by-zero (target == 0) guard
  - NULL handling: cumulative_miles, target, anchor
  - Forensic raw-math correctness in LegResult
  - Pickup-vs-dropoff asymmetry (different anchors per leg)
  - The kickoff's 7 canonical scenarios applied to odometer leg verdicts

Uses FakeOffer dataclass duck-typed to OfferRow. No real OfferRow import,
keeping these tests independent of driver_queue.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import pytest

from motion_gate import (
    ODOMETER_FLOOR_FRACTION,
    LegResult,
    OfferLegEligibility,
    _evaluate_leg,
    evaluate_odometer_gate,
)


@dataclass
class FakeOffer:
    """Duck-typed OfferRow for gate testing."""
    id: int
    pickup_miles: Optional[float] = None
    trip_miles: Optional[float] = None
    leg_start_cumulative_miles_pickup: Optional[float] = None
    leg_start_cumulative_miles_dropoff: Optional[float] = None


# =============================================================================
# Single-leg primitive: _evaluate_leg
# =============================================================================

class TestSingleLegPasses:
    def test_at_floor_exactly(self):
        # progress = 1.0 / 5.5 ... no, let's pick math that hits exactly 0.9
        # cm=4.5, anchor=0, target=5.0 -> progress=0.9, eligible
        r = _evaluate_leg(4.5, 0.0, 5.0, ODOMETER_FLOOR_FRACTION)
        assert r.eligible is True
        assert r.progress == pytest.approx(0.9)
        assert r.reason == "passed"

    def test_just_above_floor(self):
        r = _evaluate_leg(5.0, 0.0, 5.0, ODOMETER_FLOOR_FRACTION)
        assert r.eligible is True
        assert r.progress == pytest.approx(1.0)
        assert r.reason == "passed"

    def test_drive_2_real_dropoff(self):
        # Drive 2 final stop: cm=30.5, anchor=0, target=30.8
        r = _evaluate_leg(30.5, 0.0, 30.8, ODOMETER_FLOOR_FRACTION)
        assert r.eligible is True
        assert r.progress > 0.99
        assert r.reason == "passed"


class TestSingleLegHolds:
    def test_well_below_floor(self):
        # Drive 1 Stop 1A: cm=1.0, anchor=0, target=5.5 -> progress=0.18
        r = _evaluate_leg(1.0, 0.0, 5.5, ODOMETER_FLOOR_FRACTION)
        assert r.eligible is False
        assert r.progress == pytest.approx(1.0 / 5.5)
        assert r.reason == "below_floor"

    def test_just_below_floor(self):
        # progress = 0.89 / 1.0 = 0.89 < 0.9
        r = _evaluate_leg(0.89, 0.0, 1.0, ODOMETER_FLOOR_FRACTION)
        assert r.eligible is False
        assert r.reason == "below_floor"

    def test_with_nonzero_anchor(self):
        # Stacked ride: pickup leg started after dropoff of prior ride
        # cm=12.0, anchor=10.0, target=3.0 -> delta=2.0, progress=0.667
        r = _evaluate_leg(12.0, 10.0, 3.0, ODOMETER_FLOOR_FRACTION)
        assert r.eligible is False
        assert r.progress == pytest.approx(2.0 / 3.0)
        assert r.reason == "below_floor"


# =============================================================================
# NULL / degenerate handling
# =============================================================================

class TestNullHandling:
    def test_cumulative_miles_none_holds(self):
        r = _evaluate_leg(None, 0.0, 5.0, ODOMETER_FLOOR_FRACTION)
        assert r.eligible is False
        assert r.progress is None
        assert r.odo is None
        assert r.reason == "missing_cumulative_miles"

    def test_target_none_holds(self):
        # Data integrity issue: should never happen on production rows
        # (pickup_miles is non-null on all 2199 rows in current data)
        r = _evaluate_leg(5.0, 0.0, None, ODOMETER_FLOOR_FRACTION)
        assert r.eligible is False
        assert r.progress is None
        assert r.target is None
        assert r.reason == "missing_target_distance"

    def test_target_zero_passes(self):
        # Errands scenario (§5.1) or driver standing at pickup at acceptance
        # 43/936 ACCEPTed offers have pickup_miles = 0 in production data
        r = _evaluate_leg(5.0, 0.0, 0.0, ODOMETER_FLOOR_FRACTION)
        assert r.eligible is True
        assert r.progress is None  # avoid divide-by-zero
        assert r.target == 0.0
        assert r.reason == "zero_target"

    def test_anchor_none_passes(self):
        # In-flight offer accepted before this ship; grandfathered.
        r = _evaluate_leg(5.0, None, 10.0, ODOMETER_FLOOR_FRACTION)
        assert r.eligible is True
        assert r.progress is None
        assert r.anchor is None
        assert r.reason == "pre_gate_offer"


class TestNullPriority:
    """Order of NULL checks matters for clarity of forensic reasons."""

    def test_cm_none_beats_target_none(self):
        # Both are None; cm None wins (we can't reason at all without cm).
        r = _evaluate_leg(None, None, None, ODOMETER_FLOOR_FRACTION)
        assert r.reason == "missing_cumulative_miles"

    def test_target_none_beats_anchor_none(self):
        # cm present, target None, anchor None: target_none wins.
        r = _evaluate_leg(5.0, None, None, ODOMETER_FLOOR_FRACTION)
        assert r.reason == "missing_target_distance"

    def test_target_zero_beats_anchor_none(self):
        # cm present, target=0, anchor None: zero_target wins (passes).
        r = _evaluate_leg(5.0, None, 0.0, ODOMETER_FLOOR_FRACTION)
        assert r.reason == "zero_target"
        assert r.eligible is True


# =============================================================================
# Forensic raw-math fidelity (Gemini round-2 "audit from couch" requirement)
# =============================================================================

class TestForensicMath:
    def test_passed_leg_carries_full_math(self):
        r = _evaluate_leg(5.0, 0.0, 5.0, ODOMETER_FLOOR_FRACTION)
        assert r.odo == 5.0
        assert r.anchor == 0.0
        assert r.target == 5.0
        assert r.progress == pytest.approx(1.0)

    def test_held_leg_carries_full_math(self):
        # Drive 1 Stop 1A: must show "1.2 of 5.5 (21%)"
        r = _evaluate_leg(1.2, 0.0, 5.5, ODOMETER_FLOOR_FRACTION)
        assert r.odo == 1.2
        assert r.anchor == 0.0
        assert r.target == 5.5
        assert r.progress == pytest.approx(0.218, abs=0.01)
        assert r.reason == "below_floor"

    def test_jsonable_serialization(self):
        r = _evaluate_leg(1.2, 0.0, 5.5, ODOMETER_FLOOR_FRACTION)
        j = r.to_jsonable()
        assert j["eligible"] is False
        assert j["odo"] == 1.2
        assert j["anchor"] == 0.0
        assert j["target"] == 5.5
        assert j["reason"] == "below_floor"
        assert "progress" in j


# =============================================================================
# Top-level evaluate_odometer_gate (queue evaluation)
# =============================================================================

class TestEvaluateOdometerGate:
    def test_empty_queue_returns_empty_dict(self):
        result = evaluate_odometer_gate(5.0, [])
        assert result == {}

    def test_single_offer_both_legs(self):
        offer = FakeOffer(
            id=7711,
            pickup_miles=3.0, trip_miles=5.5,
            leg_start_cumulative_miles_pickup=0.0,
            leg_start_cumulative_miles_dropoff=3.0,
        )
        # cm=1.0: pickup progress 0.33 (hold), dropoff anchor not yet active
        # but anchor is set so progress = -2.0/5.5 = -0.36 (hold)
        result = evaluate_odometer_gate(1.0, [offer])
        assert "7711" in result
        assert result["7711"].pickup.eligible is False
        assert result["7711"].dropoff.eligible is False

    def test_pickup_passes_dropoff_holds(self):
        # cm=2.8 of 3.0 pickup target -> 93%, passes
        # cm=2.8 of 5.5 dropoff target (anchor 3.0) -> -0.04, holds
        offer = FakeOffer(
            id=7711,
            pickup_miles=3.0, trip_miles=5.5,
            leg_start_cumulative_miles_pickup=0.0,
            leg_start_cumulative_miles_dropoff=3.0,
        )
        result = evaluate_odometer_gate(2.8, [offer])
        assert result["7711"].pickup.eligible is True
        assert result["7711"].dropoff.eligible is False

    def test_both_legs_pass_at_dropoff(self):
        # cm=8.5 of 5.5 dropoff target (anchor 3.0) -> 100%, passes
        offer = FakeOffer(
            id=7711,
            pickup_miles=3.0, trip_miles=5.5,
            leg_start_cumulative_miles_pickup=0.0,
            leg_start_cumulative_miles_dropoff=3.0,
        )
        result = evaluate_odometer_gate(8.5, [offer])
        assert result["7711"].pickup.eligible is True
        assert result["7711"].dropoff.eligible is True

    def test_multiple_offers_independent_evaluation(self):
        # Stacked queue: A on dropoff leg, B on pickup leg. Different anchors.
        offer_a = FakeOffer(
            id=7711,
            pickup_miles=3.0, trip_miles=5.5,
            leg_start_cumulative_miles_pickup=0.0,
            leg_start_cumulative_miles_dropoff=3.0,
        )
        offer_b = FakeOffer(
            id=7712,
            pickup_miles=2.0, trip_miles=4.0,
            leg_start_cumulative_miles_pickup=8.0,
            leg_start_cumulative_miles_dropoff=None,  # not yet picked up
        )
        # cm=8.5: A.dropoff progress = 5.5/5.5 = 1.0 (pass)
        #         B.pickup progress = 0.5/2.0 = 0.25 (hold)
        result = evaluate_odometer_gate(8.5, [offer_a, offer_b])
        assert result["7711"].dropoff.eligible is True
        assert result["7712"].pickup.eligible is False
        # B.dropoff: anchor None -> grandfathered pass
        assert result["7712"].dropoff.eligible is True
        assert result["7712"].dropoff.reason == "pre_gate_offer"

    def test_offer_without_id_skipped(self):
        # Defensive: malformed offer must not crash the gate
        @dataclass
        class NoIdOffer:
            pickup_miles: float = 1.0
            trip_miles: float = 2.0

        result = evaluate_odometer_gate(5.0, [NoIdOffer()])
        assert result == {}

    def test_offer_id_falls_back_to_offer_id_attr(self):
        @dataclass
        class WeirdOffer:
            offer_id: int = 99
            pickup_miles: float = 1.0
            trip_miles: float = 2.0
            leg_start_cumulative_miles_pickup: float = 0.0
            leg_start_cumulative_miles_dropoff: float = 1.0

        result = evaluate_odometer_gate(2.0, [WeirdOffer()])
        assert "99" in result


# =============================================================================
# Kickoff canonical scenarios — odometer leg verdicts
# =============================================================================

class TestKickoffScenarios:
    """The kickoff doc lists 7 validation scenarios. This class covers the
    odometer-gate aspect of the 4 that have meaningful odometer math.
    The motion-gate-only scenarios (drive-by, brief tap, rolling 5mph)
    are covered in test_motion_gate.py.
    """

    def test_drive_1_stop_1a_held(self):
        """Drive 1 Stop 1A (mid-route 2-min light) — odometer must hold.

        Coords 29.4975076, -95.518549 — ~1mi into 5.5mi trip.
        Speed=0, duration=120s, would PASS motion gate.
        Odometer (pickup leg, post-pickup-fire so dropoff anchor is set):
          cm=1.0, anchor=0.0, target=5.5 -> progress=0.18
        """
        # Assume pickup already fired; dropoff leg evaluated.
        offer = FakeOffer(
            id=1,
            pickup_miles=2.0, trip_miles=5.5,
            leg_start_cumulative_miles_pickup=0.0,
            leg_start_cumulative_miles_dropoff=0.0,
        )
        result = evaluate_odometer_gate(1.0, [offer])
        assert result["1"].dropoff.eligible is False
        assert result["1"].dropoff.reason == "below_floor"
        # Forensic readout
        assert result["1"].dropoff.progress < 0.2

    def test_drive_1_stop_1c_passes(self):
        """Drive 1 Stop 1C (real dropoff at Excel Dental) — both pass.

        cm=5.5 of 5.5 dropoff target.
        """
        offer = FakeOffer(
            id=1,
            pickup_miles=2.0, trip_miles=5.5,
            leg_start_cumulative_miles_pickup=0.0,
            leg_start_cumulative_miles_dropoff=0.0,
        )
        result = evaluate_odometer_gate(5.5, [offer])
        assert result["1"].dropoff.eligible is True

    def test_drive_2_final_passes(self):
        """Drive 2 final stop — both pass."""
        offer = FakeOffer(
            id=2,
            pickup_miles=2.0, trip_miles=30.8,
            leg_start_cumulative_miles_pickup=0.0,
            leg_start_cumulative_miles_dropoff=0.0,
        )
        result = evaluate_odometer_gate(30.5, [offer])
        assert result["2"].dropoff.eligible is True

    def test_dentist_dropoff_passes(self):
        """2026-05-05 dentist drive Stop 2 (88s stationary at outbound destination).

        Trip ~1.9mi each way; cm at dropoff arrival ~1.9 of 1.9 target.
        """
        offer = FakeOffer(
            id=3,
            pickup_miles=0.5, trip_miles=1.9,
            leg_start_cumulative_miles_pickup=0.0,
            leg_start_cumulative_miles_dropoff=0.5,
        )
        result = evaluate_odometer_gate(2.4, [offer])
        # delta=1.9, target=1.9 -> 1.0 progress
        assert result["3"].dropoff.eligible is True
