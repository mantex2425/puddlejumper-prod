"""Integration tests for motion_gate.filter_matches_by_gates +
evaluate_gates (top-level orchestration).

These tests exercise the post-WAI filtering surface that driver_heartbeat.py
will invoke. They use FakeMatch / FakeCluster / FakeOffer dataclasses to
stay independent of WAIMatch / Cluster / OfferRow imports.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import pytest

from motion_gate import (
    GateVerdict,
    MOTION_MAX_SPEED_MPH,
    MOTION_MIN_DURATION_S,
    OfferLegEligibility,
    LegResult,
    evaluate_gates,
    filter_matches_by_gates,
)


@dataclass
class FakeCluster:
    duration_s: float
    max_recent_speed_mph: float


@dataclass
class FakeOffer:
    id: int
    pickup_miles: Optional[float] = None
    trip_miles: Optional[float] = None
    leg_start_cumulative_miles_pickup: Optional[float] = None
    leg_start_cumulative_miles_dropoff: Optional[float] = None


@dataclass
class FakeMatch:
    """Duck-typed WAIMatch."""
    offer_id: int
    location_type: str
    confidence: float = 0.85


# =============================================================================
# evaluate_gates: top-level orchestration
# =============================================================================

class TestEvaluateGates:
    def test_returns_gate_verdict_with_motion_and_legs(self):
        cluster = FakeCluster(duration_s=30.0, max_recent_speed_mph=0.0)
        offer = FakeOffer(
            id=1, pickup_miles=2.0, trip_miles=5.0,
            leg_start_cumulative_miles_pickup=0.0,
            leg_start_cumulative_miles_dropoff=2.0,
        )
        verdict = evaluate_gates(cluster, cumulative_miles=5.0, queue_offers=[offer])
        assert isinstance(verdict, GateVerdict)
        assert verdict.motion_verdict == "closed"
        assert "1" in verdict.leg_eligibility

    def test_no_cluster_returns_no_cluster_verdict_with_legs(self):
        # Even with no cluster, leg verdicts are computed (forensic).
        offer = FakeOffer(
            id=1, pickup_miles=2.0, trip_miles=5.0,
            leg_start_cumulative_miles_pickup=0.0,
            leg_start_cumulative_miles_dropoff=2.0,
        )
        verdict = evaluate_gates(None, cumulative_miles=5.0, queue_offers=[offer])
        assert verdict.motion_verdict == "no_cluster"
        assert "1" in verdict.leg_eligibility

    def test_jsonb_payload_serialization(self):
        cluster = FakeCluster(duration_s=30.0, max_recent_speed_mph=0.0)
        offer = FakeOffer(
            id=7711, pickup_miles=3.0, trip_miles=5.5,
            leg_start_cumulative_miles_pickup=0.0,
            leg_start_cumulative_miles_dropoff=3.0,
        )
        verdict = evaluate_gates(cluster, cumulative_miles=8.5, queue_offers=[offer])
        payload = verdict.jsonb_payload()
        assert "7711" in payload
        assert payload["7711"]["pickup"]["eligible"] is True
        assert payload["7711"]["dropoff"]["eligible"] is True
        assert payload["7711"]["dropoff"]["odo"] == 8.5
        assert payload["7711"]["dropoff"]["target"] == 5.5

    def test_held_offer_ids_and_legs(self):
        # Mid-route stop: dropoff leg holds, pickup leg passes.
        cluster = FakeCluster(duration_s=120.0, max_recent_speed_mph=0.0)
        offer = FakeOffer(
            id=7711, pickup_miles=3.0, trip_miles=5.5,
            leg_start_cumulative_miles_pickup=0.0,
            leg_start_cumulative_miles_dropoff=3.0,
        )
        # cm=4.0: pickup progress 100% (anchor 0, target 3, delta 4.0+) -> pass
        # cm=4.0: dropoff progress 1/5.5 = 0.18 -> hold
        verdict = evaluate_gates(cluster, cumulative_miles=4.0, queue_offers=[offer])
        held_offers, held_legs = verdict.held_offer_ids_and_legs()
        assert "7711" in held_offers
        assert "dropoff" in held_legs
        assert "pickup" not in held_legs

    def test_held_lists_exclude_grandfathered_and_zero_target(self):
        # pre_gate_offer (anchor None) and zero_target are not "holds" for
        # forensic purposes — they're explicit pass-throughs.
        cluster = FakeCluster(duration_s=30.0, max_recent_speed_mph=0.0)
        offer = FakeOffer(
            id=99, pickup_miles=0.0, trip_miles=5.0,
            leg_start_cumulative_miles_pickup=None,    # grandfathered
            leg_start_cumulative_miles_dropoff=None,   # grandfathered
        )
        verdict = evaluate_gates(cluster, cumulative_miles=5.0, queue_offers=[offer])
        held_offers, held_legs = verdict.held_offer_ids_and_legs()
        assert held_offers == []
        assert held_legs == []


# =============================================================================
# filter_matches_by_gates: motion gate behavior
# =============================================================================

class TestFilterMatchesMotionGate:
    def _verdict(self, motion="closed", leg_eligibility=None):
        return GateVerdict(
            motion_verdict=motion,
            leg_eligibility=leg_eligibility or {},
        )

    def test_moving_drops_all_matches(self):
        verdict = self._verdict(motion="moving")
        matches = [FakeMatch(1, "pickup"), FakeMatch(2, "dropoff")]
        assert filter_matches_by_gates(matches, verdict) == []

    def test_transient_drops_all_matches(self):
        verdict = self._verdict(motion="transient")
        matches = [FakeMatch(1, "pickup")]
        assert filter_matches_by_gates(matches, verdict) == []

    def test_no_cluster_drops_all_matches(self):
        # Practically impossible in production (matcher needs a cluster
        # for most signals) but explicit for completeness.
        verdict = self._verdict(motion="no_cluster")
        matches = [FakeMatch(1, "pickup")]
        assert filter_matches_by_gates(matches, verdict) == []

    def test_closed_passes_when_legs_eligible(self):
        leg = OfferLegEligibility(
            pickup=LegResult(True, 1.0, 5.0, 0.0, 5.0, "passed"),
            dropoff=LegResult(True, 1.0, 5.0, 0.0, 5.0, "passed"),
        )
        verdict = self._verdict(motion="closed", leg_eligibility={"1": leg})
        matches = [FakeMatch(1, "pickup")]
        assert filter_matches_by_gates(matches, verdict) == matches


# =============================================================================
# filter_matches_by_gates: odometer gate behavior
# =============================================================================

class TestFilterMatchesOdometerGate:
    def test_pickup_match_dropped_when_pickup_leg_held(self):
        leg = OfferLegEligibility(
            pickup=LegResult(False, 0.2, 1.0, 0.0, 5.0, "below_floor"),
            dropoff=LegResult(True, 1.0, 5.0, 0.0, 5.0, "passed"),
        )
        verdict = GateVerdict(motion_verdict="closed", leg_eligibility={"1": leg})
        matches = [FakeMatch(1, "pickup"), FakeMatch(1, "dropoff")]
        survivors = filter_matches_by_gates(matches, verdict)
        # Pickup dropped, dropoff survives
        assert len(survivors) == 1
        assert survivors[0].location_type == "dropoff"

    def test_dropoff_match_dropped_when_dropoff_leg_held(self):
        # Drive 1 Stop 1A: dropoff leg held by odometer; matcher might
        # have surfaced a Walmart/Shell match — gate must drop it.
        leg = OfferLegEligibility(
            pickup=LegResult(True, 1.0, 5.0, 0.0, 5.0, "passed"),
            dropoff=LegResult(False, 0.18, 1.0, 0.0, 5.5, "below_floor"),
        )
        verdict = GateVerdict(motion_verdict="closed", leg_eligibility={"7711": leg})
        matches = [FakeMatch(7711, "dropoff")]
        assert filter_matches_by_gates(matches, verdict) == []

    def test_grandfathered_offer_passes(self):
        leg = OfferLegEligibility(
            pickup=LegResult(True, None, 5.0, None, 3.0, "pre_gate_offer"),
            dropoff=LegResult(True, None, 5.0, None, 5.0, "pre_gate_offer"),
        )
        verdict = GateVerdict(motion_verdict="closed", leg_eligibility={"1": leg})
        matches = [FakeMatch(1, "pickup"), FakeMatch(1, "dropoff")]
        assert filter_matches_by_gates(matches, verdict) == matches

    def test_match_for_unknown_offer_passes_through(self):
        # Defensive: if matcher returns a match for an offer not in the
        # queue snapshot (shouldn't happen but...), don't drop it.
        verdict = GateVerdict(motion_verdict="closed", leg_eligibility={})
        matches = [FakeMatch(99, "pickup")]
        assert filter_matches_by_gates(matches, verdict) == matches

    def test_match_with_unknown_location_type_passes_through(self):
        # Defensive: if matcher ever adds a third location_type, gate
        # doesn't accidentally hold it.
        leg = OfferLegEligibility(
            pickup=LegResult(False, 0.0, 0.0, 0.0, 1.0, "below_floor"),
            dropoff=LegResult(False, 0.0, 0.0, 0.0, 1.0, "below_floor"),
        )
        verdict = GateVerdict(motion_verdict="closed", leg_eligibility={"1": leg})
        matches = [FakeMatch(1, "intersection")]  # not pickup/dropoff
        assert filter_matches_by_gates(matches, verdict) == matches


# =============================================================================
# Empty inputs
# =============================================================================

class TestFilterEdgeCases:
    def test_empty_matches_yields_empty(self):
        verdict = GateVerdict(motion_verdict="closed", leg_eligibility={})
        assert filter_matches_by_gates([], verdict) == []

    def test_does_not_mutate_input(self):
        leg = OfferLegEligibility(
            pickup=LegResult(False, 0.0, 0.0, 0.0, 1.0, "below_floor"),
            dropoff=LegResult(True, 1.0, 1.0, 0.0, 1.0, "passed"),
        )
        verdict = GateVerdict(motion_verdict="closed", leg_eligibility={"1": leg})
        matches = [FakeMatch(1, "pickup"), FakeMatch(1, "dropoff")]
        original_len = len(matches)
        filter_matches_by_gates(matches, verdict)
        assert len(matches) == original_len  # untouched


# =============================================================================
# End-to-end: kickoff scenarios via top-level evaluate_gates + filter
# =============================================================================

class TestEndToEndScenarios:
    """Replays kickoff scenarios through evaluate_gates -> filter_matches.

    Goal: confirm the right answer falls out of the integrated pipeline,
    not just isolated leg math.
    """

    def test_drive_1_stop_1a_dropoff_match_dropped(self):
        # Mid-route 2-min light: cluster Closed, but odometer holds dropoff.
        cluster = FakeCluster(duration_s=120.0, max_recent_speed_mph=0.0)
        offer = FakeOffer(
            id=7711, pickup_miles=2.0, trip_miles=5.5,
            leg_start_cumulative_miles_pickup=0.0,
            leg_start_cumulative_miles_dropoff=2.0,
        )
        verdict = evaluate_gates(cluster, cumulative_miles=3.0, queue_offers=[offer])
        # The matcher might surface a Walmart/CVS dropoff candidate here.
        matches = [FakeMatch(7711, "dropoff", confidence=0.85)]
        survivors = filter_matches_by_gates(matches, verdict)
        assert survivors == []  # gate drops the false-positive dropoff

    def test_drive_1_stop_1c_dropoff_match_survives(self):
        # Real dropoff at Excel Dental: cluster Closed, odometer passes.
        cluster = FakeCluster(duration_s=180.0, max_recent_speed_mph=0.0)
        offer = FakeOffer(
            id=7711, pickup_miles=2.0, trip_miles=5.5,
            leg_start_cumulative_miles_pickup=0.0,
            leg_start_cumulative_miles_dropoff=2.0,
        )
        verdict = evaluate_gates(cluster, cumulative_miles=7.5, queue_offers=[offer])
        matches = [FakeMatch(7711, "dropoff", confidence=0.85)]
        survivors = filter_matches_by_gates(matches, verdict)
        assert len(survivors) == 1

    def test_drive_by_no_cluster_no_match_survives(self):
        # 30mph drive-by: no cluster (production cluster_detection won't
        # form one). Even if matcher somehow returned a match, gate drops.
        verdict = evaluate_gates(None, cumulative_miles=5.0, queue_offers=[])
        matches = [FakeMatch(7711, "dropoff", confidence=0.85)]
        assert filter_matches_by_gates(matches, verdict) == []

    def test_brief_5s_tap_match_dropped(self):
        # 5s stop: cluster transient (motion gate holds).
        cluster = FakeCluster(duration_s=5.0, max_recent_speed_mph=0.0)
        offer = FakeOffer(
            id=7711, pickup_miles=2.0, trip_miles=5.5,
            leg_start_cumulative_miles_pickup=0.0,
            leg_start_cumulative_miles_dropoff=0.0,
        )
        verdict = evaluate_gates(cluster, cumulative_miles=5.5, queue_offers=[offer])
        assert verdict.motion_verdict == "transient"
        matches = [FakeMatch(7711, "dropoff", confidence=0.85)]
        assert filter_matches_by_gates(matches, verdict) == []

    def test_rolling_5mph_match_dropped(self):
        # Rolling 5mph cluster: motion gate verdict "moving".
        cluster = FakeCluster(duration_s=30.0, max_recent_speed_mph=5.0)
        offer = FakeOffer(
            id=7711, pickup_miles=2.0, trip_miles=5.5,
            leg_start_cumulative_miles_pickup=0.0,
            leg_start_cumulative_miles_dropoff=0.0,
        )
        verdict = evaluate_gates(cluster, cumulative_miles=5.5, queue_offers=[offer])
        assert verdict.motion_verdict == "moving"
        matches = [FakeMatch(7711, "dropoff", confidence=0.85)]
        assert filter_matches_by_gates(matches, verdict) == []
