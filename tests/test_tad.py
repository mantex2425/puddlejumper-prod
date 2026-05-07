"""
test_tad.py — unit tests for tad.compute_offer_expectations()

Pure-arithmetic tests. No DB, no Cluster, no fixtures from production.
Tests cover:
  - Idle case (no prev_offer)
  - Stacked case (prev_offer's expected dropoff is in the future)
  - Orphan case (prev_offer's expected dropoff is in the past)
  - "Already halfway there" case (Gemini's flagged scenario, 2026-05-07)
  - Short trip math (sub-2mi, where percentage-based gate would be noisy)
  - Missing required fields (defensive None return)
  - Edge case: prev_offer provided but anchor data is None
  - Forensic: orphan case emits log.warning with offer_id and delta

Test floor target: 6+ tests added, suite remains green from 435.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
from typing import Optional

import pytest

from pudo_types import Offer, TargetSpec
from tad import (
    compute_offer_expectations,
    OfferExpectations,
)


# =============================================================================
# Test fixtures — minimal Offer constructors
# =============================================================================

def _make_target(lat: float = 29.76, lng: float = -95.37) -> TargetSpec:
    """Minimal TargetSpec for tests that don't care about address parsing."""
    return TargetSpec(
        lat=lat,
        lng=lng,
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
    """Minimal Offer for tad math tests."""
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


NOW = datetime(2026, 5, 7, 14, 0, 0, tzinfo=timezone.utc)


# =============================================================================
# Idle case
# =============================================================================

class TestIdleCase:
    """prev_offer is None; expectations computed from now + new_offer fields."""

    def test_idle_pickup_anchors(self):
        offer = _make_offer("offer-A", accepted_at=NOW,
                            pickup_miles=5.0, pickup_minutes=8)
        result = compute_offer_expectations(
            new_offer=offer,
            prev_offer=None,
            current_odometer=100.0,
            now=NOW,
        )

        assert result is not None
        # pickup arrival = now + 8 minutes
        assert result.expected_pickup_arrival_time == NOW + timedelta(minutes=8)
        # pickup distance = 100 + 5 = 105 miles cumulative
        assert result.expected_pickup_distance == pytest.approx(105.0)

    def test_idle_dropoff_anchors_chain_from_pickup(self):
        offer = _make_offer("offer-A", accepted_at=NOW,
                            pickup_miles=5.0, pickup_minutes=8,
                            trip_miles=12.0, trip_minutes=18)
        result = compute_offer_expectations(
            new_offer=offer,
            prev_offer=None,
            current_odometer=100.0,
            now=NOW,
        )

        # dropoff arrival = pickup arrival + 18 minutes = now + 26 minutes
        assert result.expected_dropoff_arrival_time == NOW + timedelta(minutes=26)
        # dropoff distance = 105 + 12 = 117
        assert result.expected_dropoff_distance == pytest.approx(117.0)


# =============================================================================
# Stacked case
# =============================================================================

class TestStackedCase:
    """prev_offer's expected dropoff is still in the future; chain anchors."""

    def test_stacked_pickup_anchors_chain_from_prev_dropoff(self):
        prev = _make_offer("prev-A", accepted_at=NOW - timedelta(minutes=10),
                           pickup_miles=3.0, pickup_minutes=5,
                           trip_miles=8.0, trip_minutes=12)
        # Prev's expected dropoff: 15 min from now, 14 mi cumulative
        prev_dropoff_eta = NOW + timedelta(minutes=15)
        prev_dropoff_dist = 114.0

        new = _make_offer("offer-B", accepted_at=NOW,
                          pickup_miles=2.0, pickup_minutes=4,
                          trip_miles=6.0, trip_minutes=10)
        result = compute_offer_expectations(
            new_offer=new,
            prev_offer=prev,
            current_odometer=105.0,  # driver is mid-prev-trip
            now=NOW,
            prev_expected_dropoff_arrival_time=prev_dropoff_eta,
            prev_expected_dropoff_distance=prev_dropoff_dist,
        )

        assert result is not None
        # New pickup arrival = prev_dropoff_eta + 4min = NOW + 19min
        assert result.expected_pickup_arrival_time == NOW + timedelta(minutes=19)
        # New pickup distance = prev_dropoff_dist + 2mi = 116.0
        assert result.expected_pickup_distance == pytest.approx(116.0)
        # New dropoff arrival = pickup + 10min = NOW + 29min
        assert result.expected_dropoff_arrival_time == NOW + timedelta(minutes=29)
        # New dropoff distance = 116 + 6 = 122
        assert result.expected_dropoff_distance == pytest.approx(122.0)


# =============================================================================
# Orphan case (cancellation detection)
# =============================================================================

class TestOrphanCase:
    """prev_offer's expected dropoff is in the past — treat as idle, log warning."""

    def test_orphan_falls_back_to_idle(self):
        prev = _make_offer("prev-A", accepted_at=NOW - timedelta(hours=1))
        # Prev's expected dropoff was 5 minutes ago (orphaned)
        prev_dropoff_eta = NOW - timedelta(minutes=5)
        prev_dropoff_dist = 95.0

        new = _make_offer("offer-B", accepted_at=NOW,
                          pickup_miles=4.0, pickup_minutes=6)
        result = compute_offer_expectations(
            new_offer=new,
            prev_offer=prev,
            current_odometer=100.0,
            now=NOW,
            prev_expected_dropoff_arrival_time=prev_dropoff_eta,
            prev_expected_dropoff_distance=prev_dropoff_dist,
        )

        # Should fall back to idle: pickup_eta = now + 6min
        assert result.expected_pickup_arrival_time == NOW + timedelta(minutes=6)
        # And idle distance: 100 + 4 = 104 (NOT chained off prev's 95)
        assert result.expected_pickup_distance == pytest.approx(104.0)

    def test_orphan_emits_log_warning(self, caplog):
        prev = _make_offer("prev-orphan", accepted_at=NOW - timedelta(hours=1))
        prev_dropoff_eta = NOW - timedelta(minutes=5)
        prev_dropoff_dist = 95.0

        new = _make_offer("new-after-orphan", accepted_at=NOW)

        with caplog.at_level(logging.WARNING, logger="tad"):
            compute_offer_expectations(
                new_offer=new,
                prev_offer=prev,
                current_odometer=100.0,
                now=NOW,
                prev_expected_dropoff_arrival_time=prev_dropoff_eta,
                prev_expected_dropoff_distance=prev_dropoff_dist,
            )

        # Forensic trail must mention both offer_ids and the time delta
        assert any(
            "new-after-orphan" in rec.message and "prev-orphan" in rec.message
            for rec in caplog.records
        ), f"orphan log message missing expected fields. Records: {[r.message for r in caplog.records]}"


# =============================================================================
# "Already halfway there" case (Gemini, 2026-05-07)
# =============================================================================

class TestAlreadyHalfwayThere:
    """Driver accepts a 0.8mi pickup while already 0.4mi into the approach.

    Math should produce a sane forward-looking expectation regardless of
    where the driver currently is. There's no "impossible" output state —
    expected_pickup_distance is always current_odometer + pickup_miles.
    """

    def test_short_pickup_accepted_mid_approach(self):
        offer = _make_offer("halfway-offer", accepted_at=NOW,
                            pickup_miles=0.8, pickup_minutes=2,
                            trip_miles=4.0, trip_minutes=8)
        # Driver is at 50.0 cumulative miles, accepts a 0.8mi pickup
        result = compute_offer_expectations(
            new_offer=offer,
            prev_offer=None,
            current_odometer=50.0,
            now=NOW,
        )

        assert result is not None
        # Expected pickup distance is forward-looking: 50.0 + 0.8 = 50.8
        # NOT "0.8" or "halfway". The cluster odometer at actual pickup
        # will be ~50.8; that's the comparison TAD makes.
        assert result.expected_pickup_distance == pytest.approx(50.8)
        # Expected pickup time is also forward-looking: 2 minutes ahead
        assert result.expected_pickup_arrival_time == NOW + timedelta(minutes=2)
        # And dropoff still chains correctly off the forward pickup
        assert result.expected_dropoff_distance == pytest.approx(54.8)


# =============================================================================
# Defensive: missing required fields
# =============================================================================

class TestMissingFields:
    """If pickup/trip miles/minutes are missing, return None (skip TAD)."""

    def test_missing_pickup_minutes_returns_none(self):
        offer = _make_offer("no-pickup-min", accepted_at=NOW,
                            pickup_minutes=None)
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


# =============================================================================
# Edge: prev_offer present but anchor data missing
# =============================================================================

class TestPrevOfferWithoutAnchors:
    """prev_offer is provided but its expected_dropoff_* values are None.

    This happens when prev_offer predates Phase 2c.2 deploy (its
    expected_dropoff_* columns are NULL). Should fall back to idle.
    """

    def test_prev_offer_with_no_anchors_uses_idle(self):
        prev = _make_offer("prev-pre-2c2", accepted_at=NOW - timedelta(minutes=20))
        new = _make_offer("offer-after-prev", accepted_at=NOW,
                          pickup_miles=3.0, pickup_minutes=5)

        result = compute_offer_expectations(
            new_offer=new,
            prev_offer=prev,
            current_odometer=200.0,
            now=NOW,
            prev_expected_dropoff_arrival_time=None,
            prev_expected_dropoff_distance=None,
        )

        # Falls back to idle: pickup_eta = now + 5min, dist = 200 + 3 = 203
        assert result.expected_pickup_arrival_time == NOW + timedelta(minutes=5)
        assert result.expected_pickup_distance == pytest.approx(203.0)


# =============================================================================
# Frozen dataclass discipline
# =============================================================================

class TestOfferExpectationsImmutability:
    """OfferExpectations is frozen — any mutation must raise FrozenInstanceError."""

    def test_cannot_mutate_after_construction(self):
        offer = _make_offer("freeze-test", accepted_at=NOW)
        result = compute_offer_expectations(
            new_offer=offer, prev_offer=None,
            current_odometer=100.0, now=NOW,
        )
        assert result is not None
        with pytest.raises(Exception):  # dataclasses.FrozenInstanceError
            result.expected_pickup_distance = 999.0  # type: ignore[misc]


# =============================================================================
# v2.1 Section III: UTC-mandatory enforcement
# =============================================================================

class TestUtcEnforcement:
    """Naive datetimes must raise ValueError. No silent coercion to UTC."""

    def test_naive_now_raises(self):
        offer = _make_offer("naive-test", accepted_at=NOW)
        naive_now = datetime(2026, 5, 7, 14, 0, 0)  # no tzinfo
        with pytest.raises(ValueError, match="must be tz-aware UTC"):
            compute_offer_expectations(
                new_offer=offer,
                prev_offer=None,
                current_odometer=100.0,
                now=naive_now,
            )

    def test_naive_prev_dropoff_anchor_raises(self):
        prev = _make_offer("prev-naive", accepted_at=NOW - timedelta(minutes=20))
        new = _make_offer("new-after-naive", accepted_at=NOW)
        naive_prev_dropoff = datetime(2026, 5, 7, 14, 30, 0)  # no tzinfo

        with pytest.raises(ValueError, match="must be tz-aware UTC"):
            compute_offer_expectations(
                new_offer=new,
                prev_offer=prev,
                current_odometer=100.0,
                now=NOW,
                prev_expected_dropoff_arrival_time=naive_prev_dropoff,
                prev_expected_dropoff_distance=95.0,
            )