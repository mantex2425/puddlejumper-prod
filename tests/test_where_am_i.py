"""
test_where_am_i.py - Unit tests for where_am_i.py Section A (signal library).

Phase D Step 5.2. 34 tests across 7 test classes plus a sanity-check
forensic mini-test for the Forum Park 7623 canonical case.

Test classes:
  TestHaversineMeters         (3 tests)  - distance math, including PostGIS validation
  TestSignalProximity         (5 tests)  - linear decay across threshold
  TestSignalBreadcrumbMatch   (5 tests)  - step function, abbreviation match
  TestSignalClusterTightness  (5 tests)  - linear ramp 15m -> 50m
  TestSignalClusterDuration   (8 tests)  - triangle profile boundaries
  TestSignalOnTargetRoad      (4 tests)  - step function with abbreviation
  TestSignalOffWirePivot      (6 tests)  - apartment-pivot ramp 10s -> 30s

Plus:
  TestForumPark7623SanityCheck (1)        - confidence math for the canonical case

Total: 37 tests. Independent of any database connection - pure functions only.
"""
from __future__ import annotations

import math
import pytest
from unittest.mock import patch, MagicMock

from cluster_detection import Cluster
from datetime import datetime as _dt, timezone as _tz, timedelta
from where_am_i import (
    _compute_cluster_revisit,
    haversine_meters,
    _signal_proximity,
    _signal_breadcrumb_match,
    _signal_cluster_tightness,
    _signal_cluster_duration,
    _signal_on_target_road,
    _signal_off_wire_pivot,
    INTERSECTION_RADIUS_M,
    NUMBER_ON_STREET_RADIUS_M,
    _CONFIDENCE_WEIGHTS,
)


# =============================================================================
# DUMMY_ACCEPTED_AT — synthetic anchor for Offer construction (v2.6 amendment)
# =============================================================================

DUMMY_ACCEPTED_AT = _dt(2026, 4, 27, 8, 0, tzinfo=_tz.utc)
"""
Synthetic anchor for tests that require an Offer construction but do not
depend on temporal lookback logic. L-9 fixture provenance: declared,
not borrowed.

WARNING: This is a placeholder of record. Tests exercising temporal logic
(e.g., Houston Loop topology, T75-T79) must use locally-coherent timestamps
relative to their heartbeat data, not this constant.
"""


# =============================================================================
# Test fixture factory (Gemini Q1 ruling: factory over boilerplate)
# =============================================================================

def _cluster(
    spread_m: float = 15.0,
    duration_s: float = 30.0,
    median_lat: float = 29.6246,
    median_lng: float = -95.5102,
    n: int = 8,
) -> Cluster:
    """Construct a Cluster with sensible defaults for tests.

    Defaults represent the canonical Forum Park 7623 stop:
    8 heartbeats, 15m spread, 30s duration, at the Settemont Rd cluster median.
    Tests override individual fields as needed.
    """
    return Cluster(
        n=n,
        median_lat=median_lat,
        median_lng=median_lng,
        spread_m=spread_m,
        duration_s=duration_s,
        latest=_dt(2026, 4, 23, 20, 52, 16, tzinfo=_tz.utc),
    )


# =============================================================================
# _cluster_at - null-island-relative cluster factory for cluster_revisit tests
# =============================================================================
# L-9 provenance for null-island coordinate scheme:
#   Origin (0.0, 0.0) - null island.
#   1 deg latitude  ~= 111,320 m near equator.
#   1 deg longitude ~= 111,320 m at equator.
#   Within a few-hundred-meter neighborhood the haversine distance is well-
#   approximated by Euclidean * 111,320 m/deg.
#
# Reference offsets used in TestComputeClusterRevisit:
#   (0,        0)         - anchor "active" position
#   (0,        0.001)     ~ 111 m  - "near" (under 200m gate)
#   (0,        0.001800)  ~ 200.15 m  - just past gate (intermediate-side)
#   (0.005,    0.005)     ~ 786 m  - "far" (over 200m gate)
#   (0,        0.005)     ~ 556 m  - "far" along single axis

_BASE_LATEST = _dt(2026, 4, 27, 8, 0, tzinfo=_tz.utc)


def _cluster_at(
    lat: float,
    lng: float,
    latest_offset_s: int = 0,
    n: int = 8,
    spread_m: float = 15.0,
    duration_s: float = 30.0,
) -> Cluster:
    """Construct a synthetic Cluster for cluster_revisit topology tests.

    L-9 provenance: callers declare null-island-relative coords and the
    haversine distance to other clusters in the test docstring.

    latest_offset_s: integer seconds offset from _BASE_LATEST. Lets tests
    declare chronological ordering without absolute timestamps. oldest-first
    iteration in get_recent_clusters: smaller offsets appear earlier.
    """
    return Cluster(
        n=n,
        median_lat=lat,
        median_lng=lng,
        spread_m=spread_m,
        duration_s=duration_s,
        latest=_BASE_LATEST + timedelta(seconds=latest_offset_s),
    )


# =============================================================================
# TestHaversineMeters - 3 tests
# =============================================================================

class TestHaversineMeters:
    def test_zero_distance_same_point(self):
        d = haversine_meters(29.6246, -95.5102, 29.6246, -95.5102)
        assert d == pytest.approx(0.0, abs=1e-9)

    def test_known_short_distance_houston(self):
        # Two points ~100m apart in Houston (29.6246, -95.5102) and
        # (29.6255, -95.5102) - 0.0009 degrees of latitude is ~100m
        d = haversine_meters(29.6246, -95.5102, 29.6255, -95.5102)
        # 0.0009 deg latitude * 111139 m/deg = ~100m
        assert d == pytest.approx(100.0, abs=1.0)

    def test_known_long_distance_postgis_validated(self):
        """The "Sleep-Well-At-Night" test (Gemini Q2 ruling).

        Validates Python Haversine against a reference value computed by
        PostGIS for two points across Houston. If this passes, we trust
        the formula and don't pay CI cycles re-proving trigonometry.

        Reference points (lat, lng):
          Forum Park 7623:   29.6246, -95.5102
          Hobby Airport HQ:  29.6454, -95.2789

        PostGIS: ST_Distance(geog1, geog2) = 22405 meters (approx).
        Python Haversine should agree within 0.5% for this scale of distance.
        """
        d = haversine_meters(29.6246, -95.5102, 29.6454, -95.2789)
        # Allow 0.5% tolerance - Earth isn't a perfect sphere; PostGIS uses
        # the WGS84 ellipsoid, Haversine assumes a sphere. Below 0.5% delta
        # at this scale is well within "trust the formula" territory.
        expected_postgis = 22405.0
        assert d == pytest.approx(expected_postgis, rel=0.005)


# =============================================================================
# TestSignalProximity - 5 tests
# =============================================================================

class TestSignalProximity:
    def test_at_target_returns_1(self):
        c = _cluster(median_lat=29.6246, median_lng=-95.5102)
        assert _signal_proximity(c, 29.6246, -95.5102, 250.0) == 1.0

    def test_inside_threshold_linear(self):
        # Distance ~125m, threshold 250m -> 0.5
        # 125m of latitude shift = 0.001125 deg
        c = _cluster(median_lat=29.6246, median_lng=-95.5102)
        signal = _signal_proximity(c, 29.6246 + 0.001125, -95.5102, 250.0)
        assert signal == pytest.approx(0.5, abs=0.05)

    def test_at_threshold_returns_0(self):
        # Distance ~250m, threshold 250m -> exactly 0.0 (boundary)
        c = _cluster(median_lat=29.6246, median_lng=-95.5102)
        # 250m latitude = 0.00225 deg
        signal = _signal_proximity(c, 29.6246 + 0.00225, -95.5102, 250.0)
        assert signal == pytest.approx(0.0, abs=0.05)

    def test_beyond_threshold_returns_0(self):
        # Distance much > threshold, must clamp to 0.0 not negative
        c = _cluster(median_lat=29.6246, median_lng=-95.5102)
        signal = _signal_proximity(c, 29.6500, -95.5102, 250.0)
        assert signal == 0.0

    def test_zero_threshold_defensive(self):
        c = _cluster()
        assert _signal_proximity(c, 29.6246, -95.5102, 0.0) == 0.0


# =============================================================================
# TestSignalBreadcrumbMatch - 5 tests
# =============================================================================

class TestSignalBreadcrumbMatch:
    def test_target_in_breadcrumb(self):
        breadcrumb = ("Fondren Road", "Settemont Road")
        targets = ("Settemont Road",)
        assert _signal_breadcrumb_match(breadcrumb, targets) == 1.0

    def test_abbreviation_match(self):
        # Gemini Q3: one end-to-end test that confirms _road_names_match
        # is wired correctly. "Settemont Rd" should match "Settemont Road".
        breadcrumb = ("Settemont Rd",)
        targets = ("Settemont Road",)
        assert _signal_breadcrumb_match(breadcrumb, targets) == 1.0

    def test_no_match(self):
        breadcrumb = ("Fondren Road", "Bissonnet Street")
        targets = ("Settemont Road",)
        assert _signal_breadcrumb_match(breadcrumb, targets) == 0.0

    def test_empty_breadcrumb(self):
        assert _signal_breadcrumb_match((), ("Settemont Road",)) == 0.0

    def test_empty_target_road_names(self):
        assert _signal_breadcrumb_match(("Fondren Road",), ()) == 0.0


# =============================================================================
# TestSignalClusterTightness - 5 tests
# =============================================================================

class TestSignalClusterTightness:
    def test_tight_returns_1(self):
        # spread well below tight threshold (15m)
        assert _signal_cluster_tightness(_cluster(spread_m=10.0)) == 1.0

    def test_at_tight_threshold(self):
        # spread exactly at 15m boundary -> 1.0
        assert _signal_cluster_tightness(_cluster(spread_m=15.0)) == 1.0

    def test_at_loose_threshold(self):
        # spread exactly at 50m boundary -> 0.0
        assert _signal_cluster_tightness(_cluster(spread_m=50.0)) == 0.0

    def test_loose_returns_0(self):
        # spread well above loose threshold
        assert _signal_cluster_tightness(_cluster(spread_m=100.0)) == 0.0

    def test_midway_linear(self):
        # halfway between 15m and 50m is 32.5m -> ~0.5
        assert _signal_cluster_tightness(_cluster(spread_m=32.5)) == pytest.approx(0.5, abs=0.001)


# =============================================================================
# TestSignalClusterDuration - 8 tests (the triangle profile)
# =============================================================================

class TestSignalClusterDuration:
    def test_very_short(self):
        # 5s of 15s minimum: 0.5 * (5/15) = 0.1667
        assert _signal_cluster_duration(_cluster(duration_s=5.0)) == pytest.approx(1/6, abs=0.001)

    def test_at_minimum(self):
        # 15s -> 0.5 (boundary between ramp1 and ramp2)
        assert _signal_cluster_duration(_cluster(duration_s=15.0)) == pytest.approx(0.5, abs=0.001)

    def test_at_full(self):
        # 30s -> 1.0 (start of plateau)
        assert _signal_cluster_duration(_cluster(duration_s=30.0)) == pytest.approx(1.0, abs=0.001)

    def test_in_plateau(self):
        # 60s mid-plateau -> 1.0
        assert _signal_cluster_duration(_cluster(duration_s=60.0)) == 1.0

    def test_at_decay_start(self):
        # 180s -> 1.0 (last point of plateau)
        assert _signal_cluster_duration(_cluster(duration_s=180.0)) == 1.0

    def test_in_decay(self):
        # 270s mid-decay: 1.0 - 0.5 * ((270-180)/180) = 1.0 - 0.25 = 0.75
        assert _signal_cluster_duration(_cluster(duration_s=270.0)) == pytest.approx(0.75, abs=0.001)

    def test_at_decay_floor(self):
        # 360s end of decay -> 0.5
        assert _signal_cluster_duration(_cluster(duration_s=360.0)) == pytest.approx(0.5, abs=0.001)

    def test_long_meal_stop(self):
        # 600s well past decay -> 0.5 (clamped, never below)
        assert _signal_cluster_duration(_cluster(duration_s=600.0)) == 0.5


# =============================================================================
# TestSignalOnTargetRoad - 4 tests
# =============================================================================

class TestSignalOnTargetRoad:
    def test_match(self):
        assert _signal_on_target_road("Settemont Road", ("Settemont Road",)) == 1.0

    def test_abbreviation_match(self):
        # canonical abbreviation handling via _road_names_match
        assert _signal_on_target_road("Fondren Rd", ("Fondren Road",)) == 1.0

    def test_no_match(self):
        assert _signal_on_target_road("Fondren Road", ("Settemont Road",)) == 0.0

    def test_off_wire(self):
        # current_road=None when driver is off-wire
        assert _signal_on_target_road(None, ("Settemont Road",)) == 0.0


# =============================================================================
# TestSignalOffWirePivot - 6 tests
# =============================================================================

class TestSignalOffWirePivot:
    def test_on_wire_returns_0(self):
        # Currently on-wire = no pivot happened, regardless of other inputs
        assert _signal_off_wire_pivot(
            on_wire=True,
            last_named_road="Settemont Road",
            off_wire_duration_s=30,
            target_road_names=("Settemont Road",),
        ) == 0.0

    def test_no_last_named_road(self):
        # No last_named_road = nothing to pivot off of
        assert _signal_off_wire_pivot(
            on_wire=False,
            last_named_road=None,
            off_wire_duration_s=30,
            target_road_names=("Settemont Road",),
        ) == 0.0

    def test_below_min_duration(self):
        # off_wire_duration_s < 10s = transient, not a real pivot
        assert _signal_off_wire_pivot(
            on_wire=False,
            last_named_road="Settemont Road",
            off_wire_duration_s=5,
            target_road_names=("Settemont Road",),
        ) == 0.0

    def test_pivot_not_off_target(self):
        # Pivoted off Random Street, not the target road -> 0
        assert _signal_off_wire_pivot(
            on_wire=False,
            last_named_road="Random Street",
            off_wire_duration_s=30,
            target_road_names=("Settemont Road",),
        ) == 0.0

    def test_at_full_pivot(self):
        # 30s off-wire after target road = 1.0 (full apartment-arrival signal)
        assert _signal_off_wire_pivot(
            on_wire=False,
            last_named_road="Settemont Road",
            off_wire_duration_s=30,
            target_road_names=("Settemont Road",),
        ) == 1.0

    def test_in_ramp(self):
        # 20s mid-ramp: (20-10)/(30-10) = 0.5
        assert _signal_off_wire_pivot(
            on_wire=False,
            last_named_road="Settemont Road",
            off_wire_duration_s=20,
            target_road_names=("Settemont Road",),
        ) == pytest.approx(0.5, abs=0.001)


# =============================================================================
# TestForumPark7623SanityCheck - 1 forensic mini-test
# =============================================================================

class TestForumPark7623SanityCheck:
    """End-to-end signal sanity check for the canonical Forum Park 7623 case.

    Reproduces the 6 signal values we expect at the moment WAI should fire
    INITIAL_NAIL on the intersection-class match. Confirms the weighted sum
    against the intersection weights row clears the 0.7 STRONG_MATCH threshold.

    This is NOT the full S31 forensic replay (that's Step 5.7 with real
    heartbeat fixture data and the full evaluate() orchestrator). This is
    a "math sanity" test: do the locked weights + idealized signal values
    actually produce a fire-worthy confidence?
    """

    def test_intersection_signals_sum_above_strong_threshold(self):
        # Forum Park 7623: cluster 205m from intersection, on Settemont,
        # 30s stop, 15m spread, breadcrumb contains Settemont
        c = _cluster(
            spread_m=15.0,
            duration_s=30.0,
            median_lat=29.6246,
            median_lng=-95.5102,
        )
        # Intersection target ~205m from cluster (slightly inside threshold)
        # 205m latitude shift = 0.00184 deg
        target_lat = 29.6246 + 0.00184
        target_lng = -95.5102

        signals = {
            "proximity": _signal_proximity(c, target_lat, target_lng, INTERSECTION_RADIUS_M),
            "breadcrumb_match": _signal_breadcrumb_match(("Settemont Road",), ("Settemont Rd", "Joan St")),
            "cluster_tightness": _signal_cluster_tightness(c),
            "cluster_duration": _signal_cluster_duration(c),
            "on_target_road": _signal_on_target_road("Settemont Road", ("Settemont Rd", "Joan St")),
            "off_wire_pivot": _signal_off_wire_pivot(
                on_wire=True,
                last_named_road="Settemont Road",
                off_wire_duration_s=0,
                target_road_names=("Settemont Rd", "Joan St"),
            ),
            "adjacent_road_match": 0.0,  # no adjacent_roads established in this test
        }

        # Compute weighted confidence using the intersection weights
        weights = _CONFIDENCE_WEIGHTS["intersection"]
        confidence = sum(signals[k] * w for k, w in weights.items())

        # At minimum: signal values we expect for this scenario
        assert signals["breadcrumb_match"] == 1.0, "Settemont in breadcrumb"
        assert signals["on_target_road"] == 1.0, "currently on Settemont"
        assert signals["cluster_tightness"] == 1.0, "15m is tight"
        assert signals["cluster_duration"] == 1.0, "30s is full"
        assert signals["off_wire_pivot"] == 0.0, "still on-wire"
        assert 0.10 < signals["proximity"] < 0.25, (
            f"205m on 250m threshold should be ~0.18; got {signals['proximity']}"
        )

        # Post adjacency re-weight 2026-06-03 (intersection class, ×0.888889
        # renorm + adjacent_road_match 0.10->0.20): with adjacent_road_match=0
        # here, weight pulled toward the adjacency signal lands on a zero, so
        # this non-adjacent match drops below the 0.70 STRONG threshold while
        # still clearing the 0.40 match floor comfortably.
        #   proximity:           0.088889 * 0.18 = 0.016
        #   breadcrumb_match:    0.266667 * 1.00 = 0.267
        #   cluster_tight:       0.133333 * 1.00 = 0.133
        #   cluster_duration:    0.088889 * 1.00 = 0.089
        #   on_target_road:      0.177778 * 1.00 = 0.178
        #   off_wire_pivot:      0.044444 * 0.00 = 0.000
        #   adjacent_road_match: 0.200000 * 0.00 = 0.000
        #   total:                                 0.683
        # See docs/FINDING_residential_adjacent_pickup_below_floor_2026-06-03.md §4.4
        assert confidence > 0.65, (
            f"Forum Park 7623 confidence {confidence:.3f} should clear ~0.683 "
            f"(match floor 0.40; below STRONG 0.70 post-renorm). Signals: {signals}"
        )


# =============================================================================
# Step E: _signal_adjacent_road_match tests
# =============================================================================

def test_signal_adjacent_road_match_returns_one_when_target_in_adjacent():
    """Positive case: target's named_road is in adjacent_roads whitelist."""
    from where_am_i import _signal_adjacent_road_match
    result = _signal_adjacent_road_match(
        adjacent_roads=("Hollister", "Northwest Freeway"),
        target_road_names=("Hollister",),
    )
    assert result == 1.0


def test_signal_adjacent_road_match_returns_zero_when_no_overlap():
    """No target road appears in adjacent_roads."""
    from where_am_i import _signal_adjacent_road_match
    result = _signal_adjacent_road_match(
        adjacent_roads=("Hollister", "Northwest Freeway"),
        target_road_names=("Settemont Rd",),
    )
    assert result == 0.0


def test_signal_adjacent_road_match_returns_zero_for_empty_inputs():
    """Defensive: empty adjacent_roads OR empty target_road_names -> 0.0.
    Mirrors the early-return contract of _signal_on_target_road."""
    from where_am_i import _signal_adjacent_road_match
    assert _signal_adjacent_road_match((), ("Hollister",)) == 0.0
    assert _signal_adjacent_road_match(("Hollister",), ()) == 0.0
    assert _signal_adjacent_road_match((), ()) == 0.0


def test_signal_adjacent_road_match_normalizes_via_road_names_match():
    """Reuses pivot_context._road_names_match — 'Westheimer Rd' should
    match 'Westheimer Road' (canonical suffix normalization)."""
    from where_am_i import _signal_adjacent_road_match
    # Two strings differing only by Rd/Road suffix should still match.
    # If _road_names_match canonicalizes, this returns 1.0.
    result = _signal_adjacent_road_match(
        adjacent_roads=("Westheimer Rd",),
        target_road_names=("Westheimer Road",),
    )
    assert result == 1.0, "suffix normalization (Rd <-> Road) must work"


# =============================================================================
# =============================================================================
# Step 5.3 plumbing tests — MatchOutcome, _weighted_confidence,
# _render_reason, _validate_target
# =============================================================================

from where_am_i import (
    MatchOutcome,
    _weighted_confidence,
    _render_reason,
    _validate_target,
)
from pudo_types import TargetSpec
import dataclasses


# =============================================================================
# TestMatchOutcome - 3 tests
# =============================================================================

class TestMatchOutcome:
    def test_construction_with_all_fields(self):
        outcome = MatchOutcome(
            matched=True,
            confidence=0.78,
            corrected_lat=29.6246,
            corrected_lng=-95.5102,
            reason="intersection conf=0.78 [...]",
            pudo_type="pickup",
            target_address="Joan St & Settemont Rd",
            signals={"proximity": 0.18, "breadcrumb_match": 1.0},
        )
        assert outcome.matched is True
        assert outcome.confidence == 0.78
        assert outcome.signals["breadcrumb_match"] == 1.0

    def test_construction_with_minimal_fields(self):
        # All fields are non-default (consistent with WhereAmIResult style),
        # so "minimal" means populating optionals with None.
        outcome = MatchOutcome(
            matched=False,
            confidence=0.0,
            corrected_lat=None,
            corrected_lng=None,
            reason="not matched",
            pudo_type=None,
            target_address=None,
            signals=None,
        )
        assert outcome.matched is False
        assert outcome.signals is None

    def test_frozen(self):
        # Dataclass should be immutable post-construction
        outcome = MatchOutcome(
            matched=True, confidence=0.5,
            corrected_lat=None, corrected_lng=None,
            reason="test", pudo_type=None,
            target_address=None, signals=None,
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            outcome.matched = False  # type: ignore


# =============================================================================
# TestWeightedConfidence - 4 tests
# =============================================================================

class TestWeightedConfidence:
    def test_simple_sum(self):
        # All signals 1.0, weights sum to 1.0, expect 1.0
        signals = {"a": 1.0, "b": 1.0, "c": 1.0}
        weights = {"a": 0.5, "b": 0.3, "c": 0.2}
        assert _weighted_confidence(signals, weights) == pytest.approx(1.0)

    def test_zero_signals(self):
        # All signals 0.0 -> weighted sum is 0.0
        signals = {"a": 0.0, "b": 0.0, "c": 0.0}
        weights = {"a": 0.5, "b": 0.3, "c": 0.2}
        assert _weighted_confidence(signals, weights) == 0.0

    def test_partial_signals(self):
        # Mixed signals: 1.0*0.5 + 0.5*0.3 + 0.0*0.2 = 0.65
        signals = {"a": 1.0, "b": 0.5, "c": 0.0}
        weights = {"a": 0.5, "b": 0.3, "c": 0.2}
        assert _weighted_confidence(signals, weights) == pytest.approx(0.65)

    def test_missing_signal_raises_keyerror(self):
        # Defensive: missing signal key should raise loudly, not silently
        # substitute 0.0. A matcher that forgets to compute a signal is a
        # bug, not a tunable parameter.
        signals = {"a": 1.0}  # missing "b"
        weights = {"a": 0.5, "b": 0.5}
        with pytest.raises(KeyError):
            _weighted_confidence(signals, weights)


# =============================================================================
# TestRenderReason - 3 tests
# =============================================================================

class TestRenderReason:
    def test_basic_format(self):
        signals = {"a": 0.5, "b": 0.3}
        result = _render_reason("intersection", signals, 0.42)
        # Must contain class name, conf token, and signal values
        assert result.startswith("intersection conf=0.42 [")
        assert result.endswith("]")
        assert "a=0.50" in result
        assert "b=0.30" in result

    def test_signals_sorted_descending(self):
        # Dominant signal must appear first (Gemini Step 3 ruling)
        signals = {
            "low": 0.0,
            "high": 1.0,
            "mid": 0.5,
        }
        result = _render_reason("test", signals, 0.5)
        # Find positions of each signal in the output
        high_pos = result.index("high=")
        mid_pos = result.index("mid=")
        low_pos = result.index("low=")
        # high should come before mid, mid before low
        assert high_pos < mid_pos < low_pos

    def test_zero_signals(self):
        # 0.00 values should still be listed with 2-decimal precision
        signals = {"all_zero": 0.0}
        result = _render_reason("poi", signals, 0.0)
        assert "all_zero=0.00" in result
        assert "conf=0.00" in result


# =============================================================================
# TestValidateTarget - 4 tests
# =============================================================================

# Target factory for validation tests — a TargetSpec with address attribute.
# Real TargetSpec doesn't have `address`; this test uses a SimpleNamespace-style
# object with the fields _validate_target reads.

class _FakeTarget:
    """Test fixture mimicking TargetSpec's surface as _validate_target sees it."""
    def __init__(self, lat, lng, address=""):
        self.lat = lat
        self.lng = lng
        self.address = address


class TestValidateTarget:
    def test_valid_target_returns_none(self):
        # Valid lat/lng → matcher should proceed (returns None to signal "go")
        target = _FakeTarget(29.6246, -95.5102, "Joan St & Settemont Rd")
        assert _validate_target(target, "intersection") is None

    def test_null_lat_returns_failclosed(self):
        # NULL lat from triangulation failure → fail-closed outcome
        target = _FakeTarget(None, -95.5102, "Bad geocode")
        result = _validate_target(target, "single_road")
        assert result is not None
        assert result.matched is False
        assert result.confidence == 0.0
        assert "single_road skipped: NULL target coords" in result.reason
        assert result.target_address == "Bad geocode"

    def test_null_lng_returns_failclosed(self):
        # NULL lng → same fail-closed pattern
        target = _FakeTarget(29.6246, None, "Bad geocode 2")
        result = _validate_target(target, "intersection")
        assert result is not None
        assert result.matched is False
        assert "intersection skipped" in result.reason

    def test_both_null_returns_failclosed(self):
        # Both None → fail-closed (same as either alone)
        target = _FakeTarget(None, None, "Total fail")
        result = _validate_target(target, "apartment_complex")
        assert result is not None
        assert result.matched is False
        assert result.signals is None  # no signals computed for fail-closed


# =============================================================================
# Step 5.4 matcher tests — RoadTopology, _compute_signals, _build_outcome,
# _match_<class> functions, _CLASS_DISPATCH
# =============================================================================

from where_am_i import (
    RoadTopology,
    _compute_signals,
    _build_outcome,
    _match_intersection,
    _match_single_road,
    _match_number_on_street,
    _match_apartment_complex,
    _match_poi_class,
    _CLASS_DISPATCH,
    MIN_REPORT_THRESHOLD,
    STRONG_MATCH_CONFIDENCE,
)
import logging


def _topo(
    on_wire: bool = True,
    current_road: str | None = "Settemont Road",
    last_named_road: str | None = "Settemont Road",
    off_wire_duration_s: int = 0,
    breadcrumb: tuple = ("Settemont Road",),
) -> RoadTopology:
    """Test fixture factory for RoadTopology with sensible defaults.

    Defaults represent the canonical Forum Park 7623 topology: on Settemont
    Rd, just arrived, breadcrumb shows the road. Tests override individually.
    """
    return RoadTopology(
        on_wire=on_wire,
        current_road=current_road,
        last_named_road=last_named_road,
        off_wire_duration_s=off_wire_duration_s,
        breadcrumb=breadcrumb,
    )


def _target(
    lat: float = 29.6246,
    lng: float = -95.5102,
    address_class: str = "intersection",
    named_roads: tuple = ("Settemont Rd", "Joan St"),
) -> TargetSpec:
    """Test fixture factory for TargetSpec.

    Note: real TargetSpec doesn't currently have an `address` attribute, so
    target_address in matcher outcomes will be None in tests until Phase F
    adds the field. Tests assert on matched/confidence/reason which are
    independent of address attribution.
    """
    return TargetSpec(
        lat=lat,
        lng=lng,
        address_class=address_class,
        named_roads=named_roads,
    )


# =============================================================================
# TestComputeSignals - 2 tests
# =============================================================================

class TestComputeSignals:
    def test_all_six_keys_present(self):
        # Every signal name must be in the output dict — _weighted_confidence
        # raises KeyError if any are missing
        c = _cluster()
        signals = _compute_signals(c, _topo(), _target(), 250.0)
        expected_keys = {
            "proximity", "breadcrumb_match", "cluster_tightness",
            "cluster_duration", "on_target_road", "off_wire_pivot",
            "adjacent_road_match",
        }
        assert set(signals.keys()) == expected_keys

    def test_signal_values_pass_through(self):
        # Verify the helper isn't doing any transformation — values match
        # what calling the individual signals directly would produce
        c = _cluster(spread_m=15.0, duration_s=30.0)
        topo = _topo(current_road="Settemont Road", on_wire=True)
        tgt = _target()
        signals = _compute_signals(c, topo, tgt, 250.0)
        assert signals["cluster_tightness"] == 1.0  # tight
        assert signals["cluster_duration"] == 1.0   # full
        assert signals["on_target_road"] == 1.0     # match
        assert signals["off_wire_pivot"] == 0.0     # on-wire


# =============================================================================
# TestMatchIntersection - 4 tests (the Vindication Tests for Forum Park 7623)
# =============================================================================

class TestMatchIntersection:
    def test_perfect_match_high_confidence(self):
        """The Vindication Test: WAI achieves what BMOAR could not.

        Forum Park 7623 cluster sat 205m from the intersection. BMOAR's
        Path B proximity gate (200m hard cliff) couldn't fire. WAI's
        weighted confidence factors in the 1.0 breadcrumb_match,
        on_target_road, cluster_tightness, and cluster_duration, even
        with proximity at 0.18 — combined confidence > 0.7.
        """
        cluster = _cluster(
            spread_m=15.0, duration_s=30.0,
            median_lat=29.6246, median_lng=-95.5102,
        )
        topo = _topo(
            on_wire=True,
            current_road="Settemont Road",
            breadcrumb=("Settemont Road", "Fondren Road"),
        )
        # Target intersection at ~205m from cluster
        target = _target(
            lat=29.6246 + 0.00184,
            lng=-95.5102,
            named_roads=("Settemont Rd", "Joan St"),
        )

        outcome = _match_intersection(cluster, topo, target)

        assert outcome.matched is True, (
            f"Forum Park 7623 should match. Got: {outcome.reason}"
        )
        # Post adjacency re-weight 2026-06-03: non-adjacent intersection match
        # (adjacent_road_match=0) scaled ×0.888889 -> 0.683, below the dead STRONG
        # 0.70 (test-only constant); still clears the 0.40 commit floor.
        assert outcome.confidence > 0.65, (
            f"Confidence {outcome.confidence:.3f} should clear ~0.683 "
            f"(match floor 0.40; below STRONG 0.70 post-renorm). Reason: {outcome.reason}"
        )
        assert outcome.corrected_lat == 29.6246
        assert outcome.corrected_lng == -95.5102

    def test_proximity_only_low_confidence(self):
        # Right place geographically but driver was never on the target
        # road (no breadcrumb match), is currently off-wire (no on_target_road),
        # and only the cluster tightness/duration carry signal. Should NOT match.
        cluster = _cluster(spread_m=15.0, duration_s=30.0)
        topo = _topo(
            on_wire=False,
            current_road=None,
            last_named_road=None,
            breadcrumb=("Random St", "Other Ave"),
        )
        target = _target(
            lat=29.6246, lng=-95.5102,  # proximity = 1.0
            named_roads=("Settemont Rd", "Joan St"),
        )
        outcome = _match_intersection(cluster, topo, target)
        # Per Step E: intersection requires road-name evidence to fire.
        # With no breadcrumb_match, on_target_road, off_wire_pivot, or
        # adjacent_road_match, only spatial signals (proximity, tightness,
        # duration) contribute. Their weighted sum is below MIN_REPORT_THRESHOLD,
        # honoring the test's stated intent ("Should NOT match").
        #   proximity     1.0 * 0.10 = 0.10
        #   tightness     1.0 * 0.15 = 0.15
        #   duration      1.0 * 0.10 = 0.10
        #   total                    = 0.35  (below 0.40)
        assert outcome.matched is False
        assert outcome.confidence < 0.4

    def test_null_target_coords(self):
        # S27 case: triangulation failed, target.lat is None — fail closed
        cluster = _cluster()
        target = _target(lat=None, lng=-95.5102)
        outcome = _match_intersection(cluster, _topo(), target)
        assert outcome.matched is False
        assert outcome.confidence == 0.0
        assert "intersection skipped: NULL target coords" in outcome.reason

    def test_returns_match_outcome(self):
        # Type check — every matcher returns MatchOutcome
        outcome = _match_intersection(_cluster(), _topo(), _target())
        assert isinstance(outcome, MatchOutcome)


# =============================================================================
# TestMatchSingleRoad - 2 tests
# =============================================================================

class TestMatchSingleRoad:
    def test_breadcrumb_dominates(self):
        # Cluster on the right road but 400m from geocoded point.
        # single_road weights breadcrumb at 0.40 — breadcrumb match alone
        # contributes 0.40 to confidence, plus cluster_tightness+duration
        # signals. Should match.
        cluster = _cluster(spread_m=15.0, duration_s=30.0)
        topo = _topo(
            current_road="Fondren Road",
            breadcrumb=("Fondren Road",),
        )
        # ~400m north of geocoded point
        target = _target(
            lat=29.6246 + 0.0036, lng=-95.5102,
            address_class="single_road",
            named_roads=("Fondren Rd",),
        )
        outcome = _match_single_road(cluster, topo, target)
        # breadcrumb=1.0*0.40 + cluster_tight=1.0*0.15 + duration=1.0*0.15
        # + on_target_road=1.0*0.15 = 0.85 minimum
        assert outcome.matched is True
        assert outcome.confidence > MIN_REPORT_THRESHOLD

    def test_wrong_road_low(self):
        # No breadcrumb match, no on_target_road — even with proximity,
        # confidence stays low for single_road class
        cluster = _cluster(spread_m=15.0, duration_s=30.0)
        topo = _topo(current_road="Wrong Road", breadcrumb=("Wrong Road",))
        target = _target(
            lat=29.6246, lng=-95.5102,
            address_class="single_road",
            named_roads=("Fondren Rd",),
        )
        outcome = _match_single_road(cluster, topo, target)
        # proximity=1.0*0.10 + tight=1.0*0.15 + duration=1.0*0.15 = 0.40
        # Right at MIN_REPORT_THRESHOLD boundary
        # Test the spirit: confidence well below STRONG
        assert outcome.confidence < STRONG_MATCH_CONFIDENCE


# =============================================================================
# TestMatchNumberOnStreet - 2 tests
# =============================================================================

class TestMatchNumberOnStreet:
    def test_close_proximity_dominates(self):
        # 30m from geocoded point, 50m threshold — proximity = 1 - 30/50 = 0.4
        # number_on_street weights proximity at 0.40
        cluster = _cluster(spread_m=15.0, duration_s=30.0)
        topo = _topo(current_road="Main Street", breadcrumb=("Main Street",))
        # ~30m north
        target = _target(
            lat=29.6246 + 0.00027, lng=-95.5102,
            address_class="number_on_street",
            named_roads=("Main St",),
        )
        outcome = _match_number_on_street(cluster, topo, target)
        # Should match (high proximity * high weight + supporting signals)
        assert outcome.matched is True

    def test_far_proximity_low(self):
        # 60m from geocoded point — beyond 50m threshold, proximity = 0
        cluster = _cluster(spread_m=15.0, duration_s=30.0)
        topo = _topo(current_road="Main Street", breadcrumb=("Main Street",))
        # ~60m north — beyond 50m threshold
        target = _target(
            lat=29.6246 + 0.00054, lng=-95.5102,
            address_class="number_on_street",
            named_roads=("Main St",),
        )
        outcome = _match_number_on_street(cluster, topo, target)
        # proximity=0; remaining: breadcrumb=1.0*0.20 + tight=1.0*0.15 +
        # duration=1.0*0.10 + on_target_road=1.0*0.15 = 0.60 — still matches
        # but proximity contribution is missing entirely
        assert outcome.signals["proximity"] == 0.0
        # Documenting actual production behavior: even far-from-geocode matches
        # if all OTHER signals are perfect. PLAN may add stricter gates later.


# =============================================================================
# TestMatchApartmentComplex - 2 tests
# =============================================================================

class TestMatchApartmentComplex:
    def test_pivot_dominates(self):
        # Driver pivoted off Settemont 30s ago into a parking lot.
        # apartment_complex weights off_wire_pivot at 0.40 — pivot alone
        # contributes 0.40 to confidence.
        cluster = _cluster(spread_m=15.0, duration_s=30.0)
        topo = _topo(
            on_wire=False,
            current_road=None,
            last_named_road="Settemont Road",
            off_wire_duration_s=30,
            breadcrumb=("Settemont Road",),
        )
        target = _target(
            lat=29.6246, lng=-95.5102,  # proximity ~ 1.0
            address_class="apartment_complex",
            named_roads=("Settemont Rd",),
        )
        outcome = _match_apartment_complex(cluster, topo, target)
        # off_wire_pivot=1.0*0.40 + breadcrumb=1.0*0.10 + tight=1.0*0.20 +
        # duration=1.0*0.15 + proximity=1.0*0.10 = 0.95 (on_target=0 because
        # off-wire)
        assert outcome.matched is True
        assert outcome.confidence > STRONG_MATCH_CONFIDENCE

    def test_no_pivot_low(self):
        # Driver was never on target road. off_wire_pivot=0, no on_target_road.
        # Proximity matters for apartment but doesn't carry alone.
        cluster = _cluster(spread_m=15.0, duration_s=30.0)
        topo = _topo(
            on_wire=False,
            current_road=None,
            last_named_road="Different Road",
            off_wire_duration_s=30,
            breadcrumb=("Different Road",),
        )
        target = _target(
            lat=29.6246, lng=-95.5102,
            address_class="apartment_complex",
            named_roads=("Settemont Rd",),
        )
        outcome = _match_apartment_complex(cluster, topo, target)
        # proximity=1.0*0.10 + tight=1.0*0.20 + duration=1.0*0.15 = 0.45
        # Just above MIN_REPORT_THRESHOLD, well below STRONG
        assert outcome.confidence < STRONG_MATCH_CONFIDENCE


# =============================================================================
# TestClassDispatchContract - 1 test (L-6 corollary regression)
# =============================================================================
#
# Production bug 2026-05-09 16:00-17:15 UTC: _match_poi_stub missing
# `pois` kwarg, causing TypeError on every heartbeat post-resurrection
# (commit fb56175).
#
# Pre-resurrection: cluster_pois was always [] (POI service swallowed
# exceptions). _match_poi_stub's missing pois kwarg was latent — never
# triggered because pois=[] was never actually passed to the matcher
# (the dispatch site WAS already passing pois=cluster_pois, but every
# heartbeat returned cluster=None earlier in the pipeline so dispatch
# was unreachable).
#
# Post-resurrection: cluster_pois populates with real values. Dispatch
# hits _match_poi_stub. TypeError. 50+ errors in 75 minutes. Android
# backoff, cluster starvation, no PUDO commits.
#
# This class locks in the dispatch-call contract: every matcher in
# _CLASS_DISPATCH MUST accept (cluster, topo, target, pois=..., anchors=...).
#
# CRITICAL: do NOT "simplify" this test by using MagicMock for
# cluster/topo/target. MagicMock accepts any signature and would let
# this regression class slip through silently — same trap that motivated
# the real-Cluster fixture in tests/test_poi_service.py contract tests
# (resurrection commit fb56175). Real types only.


# =============================================================================
# TestMatchPOIClassGeocodeSignal — PR-A behavioral tests
#
# Verifies _match_poi_class behavior under the "geocode as signal, not gate"
# architecture (commit 9c3ea17, ratified Andrew + Gemini Rev 4 2026-05-21).
# Null-coord targets and "garbage" address-class targets must still match
# when coord-independent signal heads fire: Head 6 geofence membership at
# score 1.0, Head 5 semantic anchor at score >= MIN_REPORT_THRESHOLD.
#
# These tests are the 8094 regression's structural proof. Pre-PR-A,
# _validate_target inside _match_poi_class would fail-closed any null-coord
# target. PR-A removed _validate_target from this matcher only, letting
# Head 5 and Head 6 (both coord-independent on the target side) rescue
# offers whose geocode failed upstream.
# =============================================================================

from unittest.mock import patch
from where_am_i import _match_poi_class


class TestMatchPOIClassGeocodeSignal:
    """PR-A behavioral tests for _match_poi_class with null-coord and garbage targets."""

    def test_null_coords_geofence_hit_fires(self):
        """Head 6 = 1.0, all other heads silent, null coords -> fires at confidence=1.0.

        Canonical PR-A case: 8094-class airport pickup where the only matching
        signal is geofence containment with IATA word-boundary match. Pre-PR-A:
        _validate_target fail-closed. Post-PR-A: geofence rescues at ground truth.
        """
        with patch("where_am_i._signal_semantic_anchor") as ms, \
             patch("where_am_i._signal_poi_match") as m1, \
             patch("where_am_i._signal_poi_type_match") as m4:
            ms.return_value = (0.0, None)
            m1.return_value = (0.0, None)
            m4.return_value = (False, None)

            target = TargetSpec(
                lat=None, lng=None,
                address_class="poi",
                named_roads=(),
                address="Main Terminal, Arrivals (Baggage, Texas",
            )
            outcome = _match_poi_class(
                _cluster(), _topo(), target,
                geofence_score=1.0,
                geofence_witness="hou_iata_match",
            )

            assert outcome.matched is True
            assert outcome.confidence == 1.0
            # geofence_witness is not stored on MatchOutcome (line 1511-1520);
            # absence of semantic_anchor_witness confirms Head 6 took it, not Head 5.
            assert outcome.semantic_anchor_witness is None
            # PR-A: address text preserved through the null-coord pipeline
            assert outcome.target_address == "Main Terminal, Arrivals (Baggage, Texas"

    def test_null_coords_anchor_hit_fires(self):
        """Head 5 = 0.82, geofence silent, null coords -> fires at confidence=0.82.

        IAH United dropoff class: text resolves to semantic anchor via §XVII
        even though pickup_lat is null. Driver en route, hasn't entered the
        polygon yet. Head 5 takes the match.
        """
        with patch("where_am_i._signal_semantic_anchor") as ms, \
             patch("where_am_i._signal_poi_match") as m1, \
             patch("where_am_i._signal_poi_type_match") as m4:
            ms.return_value = (0.82, "semantic_anchor:united_terminal/airport (45m)")
            m1.return_value = (0.0, None)
            m4.return_value = (False, None)

            target = TargetSpec(
                lat=None, lng=None,
                address_class="poi",
                named_roads=(),
                address="United, Houston, Texas",
            )
            outcome = _match_poi_class(
                _cluster(), _topo(), target,
                geofence_score=0.0,
                geofence_witness=None,
            )

            assert outcome.matched is True
            assert outcome.confidence == 0.82
            assert outcome.semantic_anchor_witness == "semantic_anchor:united_terminal/airport (45m)"
            assert outcome.semantic_anchor_score == 0.82

    def test_null_coords_both_hit_geofence_wins_tie(self):
        """Both heads at 1.0 -> Head 6 wins via list-order tie-break (line 1483-1490).

        Tie-break is observable only via INFO log; MatchOutcome stores both
        witnesses. Test confirms the outcome's confidence and that both
        heads' contributions made it through.
        """
        with patch("where_am_i._signal_semantic_anchor") as ms, \
             patch("where_am_i._signal_poi_match") as m1, \
             patch("where_am_i._signal_poi_type_match") as m4:
            ms.return_value = (1.0, "semantic_anchor:hobby_arrivals/airport (12m)")
            m1.return_value = (0.0, None)
            m4.return_value = (False, None)

            target = TargetSpec(
                lat=None, lng=None,
                address_class="poi",
                named_roads=(),
                address="William P. Hobby Airport",
            )
            outcome = _match_poi_class(
                _cluster(), _topo(), target,
                geofence_score=1.0,
                geofence_witness="hou_iata_match",
            )

            assert outcome.matched is True
            assert outcome.confidence == 1.0
            # Both heads scored; both witnesses survive _build_outcome.
            # The winning_head label ("head6_geofence") is visible only in
            # the INFO log; tie-break is documented at where_am_i.py:1475.
            assert outcome.semantic_anchor_witness == "semantic_anchor:hobby_arrivals/airport (12m)"

    def test_null_coords_no_heads_score_skips_honestly(self):
        """All heads silent + null coords -> matched=False, confidence=0.0.

        §0.D.4: skip beats pollute. When no head can identify the venue,
        the system honestly reports no match. No false fire risk.
        """
        with patch("where_am_i._signal_semantic_anchor") as ms, \
             patch("where_am_i._signal_poi_match") as m1, \
             patch("where_am_i._signal_poi_type_match") as m4:
            ms.return_value = (0.0, None)
            m1.return_value = (0.0, None)
            m4.return_value = (False, None)

            target = TargetSpec(
                lat=None, lng=None,
                address_class="poi",
                named_roads=(),
                address="Unknown POI",
            )
            outcome = _match_poi_class(
                _cluster(), _topo(), target,
                geofence_score=0.0,
                geofence_witness=None,
            )

            assert outcome.matched is False
            assert outcome.confidence == 0.0

    def test_garbage_class_geofence_hit_fires(self):
        """Garbage-class target + geofence hit -> fires.

        OCR-shredded text past classify_address keywords lands in the
        "garbage" bucket per PR-A's driver_heartbeat.py change.
        _CLASS_DISPATCH routes "garbage" to _match_poi_class. Driver
        arrests inside a known polygon. Head 6 fires at 1.0.
        """
        with patch("where_am_i._signal_semantic_anchor") as ms, \
             patch("where_am_i._signal_poi_match") as m1, \
             patch("where_am_i._signal_poi_type_match") as m4:
            ms.return_value = (0.0, None)
            m1.return_value = (0.0, None)
            m4.return_value = (False, None)

            target = TargetSpec(
                lat=None, lng=None,
                address_class="garbage",
                named_roads=(),
                address="x9$z totally shredded",
            )
            outcome = _match_poi_class(
                _cluster(), _topo(), target,
                geofence_score=1.0,
                geofence_witness="hou_iata_match",
            )

            assert outcome.matched is True
            assert outcome.confidence == 1.0

    def test_regression_8094_main_terminal_at_hobby_fires(self):
        """8094 regression backfill: 2026-05-20 production failure now passes.

        Original failure: offer 8094 created with pickup_address
        "Main Terminal, Arrivals (Baggage, Texas" — OCR truncation of the
        real Uber-rendered "Main Terminal, Arrivals (Baggage Claim level)
        Zone 5". HALLUCINATION_GUARD correctly nulled pickup_lat/lng
        (Google returned state-centroid 317mi away). _bucket_to_target_spec
        returned None at line 89-90. Offer dropped from queue. Matcher blind.

        Post-PR-A: offer survives queue (driver_heartbeat.py:89 strip),
        reaches _match_poi_class via "poi" routing (Terminal is a POI token),
        Head 6 fires at 1.0 via HOU IATA word-boundary match when driver
        arrests inside Hobby polygon at (29.6479, -95.2773).

        End-to-end production validation: tomorrow's drive.
        """
        with patch("where_am_i._signal_semantic_anchor") as ms, \
             patch("where_am_i._signal_poi_match") as m1, \
             patch("where_am_i._signal_poi_type_match") as m4:
            # Realistic head returns for the 8094 input shape.
            ms.return_value = (0.78, "semantic_anchor:hobby_terminal/airport (28m)")
            m1.return_value = (0.0, None)
            m4.return_value = (False, None)

            # Reproduce 8094's offer_history row state at the moment
            # _bucket_to_target_spec would have run post-PR-A.
            target = TargetSpec(
                lat=None,
                lng=None,
                address_class="poi",
                named_roads=(),
                address="Main Terminal, Arrivals (Baggage, Texas",
            )
            # Cluster at Hobby airport curb (8094's real arrest location).
            hobby_cluster = _cluster(
                median_lat=29.6479,
                median_lng=-95.2773,
            )

            outcome = _match_poi_class(
                hobby_cluster, _topo(), target,
                geofence_score=1.0,
                geofence_witness="hou_iata_match",
            )

            assert outcome.matched is True
            assert outcome.confidence == 1.0
            # Address text survived the null-coord queue admission unchanged.
            assert outcome.target_address == "Main Terminal, Arrivals (Baggage, Texas"


class TestClassDispatchContract:
    """L-6 corollary regression: dispatch-call signature contract.

    where_am_i.py:1700 calls every matcher in _CLASS_DISPATCH with the
    signature (cluster, topo, target, pois=cluster_pois). This test
    asserts every dispatch entry accepts that contract. Failure here
    catches the regression class that hit production 2026-05-09 BEFORE
    the bug ships.
    """

    def test_every_dispatch_matcher_accepts_pois_anchors_and_source_kwargs(self):
        """Every _CLASS_DISPATCH entry must accept pois=... AND anchors=...
        per the dispatch contract. TypeError here means a matcher signature
        has drifted from the where_am_i.py call site — same regression
        class as the 2026-05-09 _match_poi_stub production bug.

        Contract evolution:
          - 2026-05-09: pois=... mandatory (Item 2 / _match_poi_stub bug)
          - 2026-05-14a: anchors=... added (§XVII Patch 3a, Head 5 wiring)
          - 2026-05-14b: semantic_lookup_source=... added (§XVII Patch 4,
                         POILookupResult.source plumbed through matcher pipeline)

        We pass pois=[] and anchors=[] (empty lists) because those are
        the common production cases (no POIs near cluster, no semantic
        anchors for the target text) and they let matchers either consume
        or ignore the kwargs without depending on POI/anchor-handling
        correctness — this test is about the signature contract, not
        signal logic.
        """
        cluster = _cluster()
        topo = _topo()
        target = _target()

        for address_class, matcher in _CLASS_DISPATCH.items():
            try:
                outcome = matcher(
                    cluster, topo, target,
                    pois=[], anchors=[],
                    semantic_lookup_source=None,
                )
            except TypeError as e:
                raise AssertionError(
                    f"_CLASS_DISPATCH[{address_class!r}] -> "
                    f"{matcher.__name__} does not accept dispatch contract "
                    f"(cluster, topo, target, pois=..., anchors=..., "
                    f"semantic_lookup_source=...): {e}. "
                    f"L-6 corollary regression — see "
                    f"TestClassDispatchContract module docstring."
                )
            assert isinstance(outcome, MatchOutcome), (
                f"_CLASS_DISPATCH[{address_class!r}] -> "
                f"{matcher.__name__} returned {type(outcome).__name__}, "
                f"expected MatchOutcome"
            )


# =============================================================================
# TestClassDispatch - 3 tests
# =============================================================================

class TestClassDispatch:
    def test_all_six_classes_present(self):
        # §PR-A: "garbage" added 2026-05-21 — routes to _match_poi_class
        # so OCR-shredded text can be rescued via Head 5 (semantic anchor)
        # and Head 6 (geofence membership). See test_geocode_signal_admission.py
        # for the queue-admission contract.
        expected = {"intersection", "single_road", "number_on_street",
                    "apartment_complex", "poi", "garbage"}
        assert set(_CLASS_DISPATCH.keys()) == expected

    def test_dispatch_returns_matcher_function(self):
        # Each entry should be a callable matching the (cluster, topo, target)
        # signature
        cluster = _cluster()
        topo = _topo()
        for class_name, matcher in _CLASS_DISPATCH.items():
            target = _target(address_class=class_name, named_roads=())
            outcome = matcher(cluster, topo, target)
            assert isinstance(outcome, MatchOutcome), (
                f"{class_name} matcher returned {type(outcome)}"
            )

    def test_unknown_class_returns_none(self):
        # .get() returns None for unknown classes; orchestrator handles this
        assert _CLASS_DISPATCH.get("unknown_class") is None
        assert _CLASS_DISPATCH.get("") is None


# =============================================================================
# Step 5.5 orchestrator tests — WhereAmI class, evaluate(), helpers
# =============================================================================

from where_am_i import WhereAmI
from pudo_types import Offer
import datetime


# Lightweight fake cursor (Gemini Q1 ruling: hand-rolled over MagicMock)
class _FakeCursor:
    """Test fixture for ghost-cache SELECT.

    Records the last execute() call and returns a predetermined row from
    fetchone(). Hand-rolled rather than MagicMock-based per Gemini Q1
    ruling — explicit fakes are easier to reason about than mocks.

    Per Step 5.7.1 audit: production cursors use psycopg2.extras.RealDictCursor
    everywhere (pickup_confirm.py, geo.py, etc.), so fetchone() returns
    dict-shaped rows, not tuples. Ghost-row fixtures in tests below
    follow the same dict shape.
    """
    def __init__(self, ghost_row=None):
        self.ghost_row = ghost_row
        self.last_query = None
        self.last_params = None

    def execute(self, sql, params):
        self.last_query = sql
        self.last_params = params

    def fetchone(self):
        return self.ghost_row

    def fetchall(self):
        """Default empty list. get_recent_clusters() consumes via fetchall;
        without this, the bare except in cluster_detection would swallow an
        AttributeError silently. Tests that need specific recent_clusters
        results inject _recent_clusters_fn directly rather than mock SQL.
        """
        return []


# Test fixture builders for evaluate() inputs

def _fake_pivot(
    on_wire: bool = True,
    current_road: str | None = "Settemont Road",
    last_named_road: str | None = "Settemont Road",
    pivot_time=None,
    breadcrumb: list = None,
):
    """Build a fake pivot_context return dict for tests.

    The breadcrumb argument accepts a convenience shape — a list of road-name
    strings — and coerces it to the production segment-dict shape produced by
    pivot_context._build_breadcrumb ({road_name, entered_at, exited_at}).
    The coercion uses placeholder UTC timestamps because no test currently
    exercises entered_at / exited_at semantics; if a future test needs real
    timestamps, pass pre-built segment dicts directly and the coercion is
    skipped (isinstance dict pass-through).
    """
    if breadcrumb is None:
        breadcrumb = ["Settemont Road"]
    # Coerce string-shape convenience input to production segment-dict shape.
    # Pass-through for entries that are already dicts (lets future tests
    # supply real-shape segments when timing matters).
    _now = datetime.datetime.now(datetime.timezone.utc)
    breadcrumb_segments = [
        seg if isinstance(seg, dict)
        else {"road_name": seg, "entered_at": _now, "exited_at": _now}
        for seg in breadcrumb
    ]
    def _pivot_fn(driver_id, cur, anchor_time=None):
        return {
            "on_wire": on_wire,
            "current_road": current_road,
            "last_named_road": last_named_road,
            "pivot_time": pivot_time,
            "breadcrumb": breadcrumb_segments,
        }
    return _pivot_fn


def _fake_cluster_fn(cluster_to_return):
    """Build a fake cluster_fn that always returns the given cluster (or None)."""
    def _cluster_fn(driver_id, cur):
        return cluster_to_return
    return _cluster_fn


def _fake_recent_clusters_fn(clusters_to_return):
    """Build a fake recent_clusters_fn that returns the given list verbatim.

    Mirrors get_recent_clusters' real signature: (driver_id, cur,
    accepted_at_anchor=None, **kwargs). The kwargs catch-all lets tests
    pass through without caring about defaulted args (preroll_sec etc).

    For Block B tests that need to assert on the call args (e.g. that
    accepted_at_anchor was forwarded from offer.accepted_at), use a
    closure that captures into a list rather than this factory.
    """
    def _rc_fn(driver_id, cur, accepted_at_anchor=None, **kwargs):
        return clusters_to_return
    return _rc_fn


def _offer(
    offer_id: str = "test_offer",
    pickup: TargetSpec | None = None,
    dropoff: TargetSpec | None = None,
) -> Offer:
    """Build an Offer with sensible defaults.

    Cut B2: secondary_dropoff parameter removed. Stacked rides are
    represented as multiple Offers in the queue, not a single Offer with
    a secondary slot.
    """
    if pickup is None:
        pickup = _target(named_roads=("Settemont Rd", "Joan St"))
    if dropoff is None:
        dropoff = _target(
            lat=29.7000, lng=-95.4000,
            address_class="number_on_street",
            named_roads=("Main St",),
        )
    return Offer(
        offer_id=offer_id,
        accepted_at=DUMMY_ACCEPTED_AT,
        pickup=pickup,
        dropoff=dropoff,
    )


# =============================================================================
# TestComputeRoadTopology - 4 tests
# =============================================================================

class TestComputeRoadTopology:
    def test_on_wire_zero_duration(self):
        # When on_wire=True, off_wire_duration_s should be 0 regardless of pivot_time
        cur = _FakeCursor()
        wai = WhereAmI(
            cur,
            _cluster_fn=_fake_cluster_fn(None),
            _pivot_fn=_fake_pivot(on_wire=True),
        )
        topo = wai._compute_road_topology("driver1", None)
        assert topo.on_wire is True
        assert topo.off_wire_duration_s == 0
        assert topo.current_road == "Settemont Road"

    def test_off_wire_with_pivot_time(self):
        # off_wire with pivot_time 30s ago -> duration ~30
        ago_30s = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=30)
        cur = _FakeCursor()
        wai = WhereAmI(
            cur,
            _cluster_fn=_fake_cluster_fn(None),
            _pivot_fn=_fake_pivot(on_wire=False, pivot_time=ago_30s),
        )
        topo = wai._compute_road_topology("driver1", None)
        assert topo.on_wire is False
        assert 28 <= topo.off_wire_duration_s <= 32  # allow test latency

    def test_off_wire_no_pivot_time_fails_closed(self):
        # off_wire with pivot_time=None -> duration=0 (fail-closed apartment matchers)
        cur = _FakeCursor()
        wai = WhereAmI(
            cur,
            _cluster_fn=_fake_cluster_fn(None),
            _pivot_fn=_fake_pivot(on_wire=False, pivot_time=None),
        )
        topo = wai._compute_road_topology("driver1", None)
        assert topo.off_wire_duration_s == 0

    def test_negative_duration_clamps(self):
        # If pivot_time is in the future (clock skew), max(0, ...) clamps to 0
        future = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=60)
        cur = _FakeCursor()
        wai = WhereAmI(
            cur,
            _cluster_fn=_fake_cluster_fn(None),
            _pivot_fn=_fake_pivot(on_wire=False, pivot_time=future),
        )
        topo = wai._compute_road_topology("driver1", None)
        assert topo.off_wire_duration_s == 0


# =============================================================================
# TestTargetsForState - 5 tests
# =============================================================================

class TestEvaluate:
    """Cut B2: queue-aware evaluate() returns list[WAIMatch].

    These tests cover the public Pure-Sensor surface: evaluate() returns
    a naked match list; evaluate_with_diagnostics() returns the same plus
    a DiagnosticContext. Per SIMPLIFIED_ARCHITECTURE.md §3 Map-Reduce.
    """

    def test_no_cluster_returns_empty_matches(self):
        cur = _FakeCursor()
        wai = WhereAmI(cur, _cluster_fn=_fake_cluster_fn(None),
                       _pivot_fn=_fake_pivot())
        matches = wai.evaluate("driver1", [_offer()])
        assert matches == []

    def test_cluster_at_target_returns_pickup_match(self):
        # Forum Park 7623 — full evaluate() path. Pickup at the canonical
        # 205m intersection target; default dropoff is far enough away
        # that only the pickup target should match.
        cluster = _cluster()
        cur = _FakeCursor()
        wai = WhereAmI(
            cur,
            _cluster_fn=_fake_cluster_fn(cluster),
            _pivot_fn=_fake_pivot(
                on_wire=True,
                current_road="Settemont Road",
                breadcrumb=["Settemont Road", "Fondren Road"],
            ),
        )
        pickup = _target(
            lat=29.6246 + 0.00184,
            lng=-95.5102,
            named_roads=("Settemont Rd", "Joan St"),
        )
        offer = _offer(pickup=pickup)
        matches, diagnostics = wai.evaluate_with_diagnostics("driver1", [offer])
        # Single decisive winner per §3 step 3d Reduce
        assert len(matches) == 1
        assert matches[0].offer_id == offer.offer_id
        assert matches[0].location_type == "pickup"
        assert matches[0].confidence > 0.65  # post adjacency re-weight 2026-06-03: 0.683, below dead STRONG 0.70, above 0.40 commit floor
        # Default _FakeCursor.fetchall() returns [] -> no recent_clusters ->
        # cluster_revisit must be False on the standard "first arrival" case.
        assert diagnostics.cluster_revisit is False

    def test_empty_queue_returns_empty_matches(self):
        # Empty queue (driver idle, no offers) -> Map step generates zero
        # candidates -> empty matches. Diagnostics still carries the cluster
        # per §3 step 8 (cluster data logged independently of WAI outcome).
        cluster = _cluster()
        cur = _FakeCursor()
        wai = WhereAmI(cur, _cluster_fn=_fake_cluster_fn(cluster),
                       _pivot_fn=_fake_pivot())
        matches, diagnostics = wai.evaluate_with_diagnostics("driver1", [])
        assert matches == []
        assert diagnostics.cluster is cluster

    def test_far_targets_return_empty_matches(self):
        # Cluster + queue with offer whose targets are nowhere near the
        # cluster -> all candidates filter out -> empty matches.
        cluster = _cluster()
        cur = _FakeCursor()
        wai = WhereAmI(cur, _cluster_fn=_fake_cluster_fn(cluster),
                       _pivot_fn=_fake_pivot())
        far_target = _target(lat=30.0, lng=-96.0, named_roads=("Nowhere Blvd",))
        offer = _offer(pickup=far_target, dropoff=far_target)
        matches = wai.evaluate("driver1", [offer])
        assert matches == []


# =============================================================================
# TestWhereAmIInjection - 3 tests
# =============================================================================

class TestWhereAmIInjection:
    def test_default_uses_real_dependencies(self):
        # No injection -> uses production cluster_detection.detect_cluster
        # and pivot_context.get_pivot_context
        from cluster_detection import detect_cluster
        from pivot_context import get_pivot_context
        cur = _FakeCursor()
        wai = WhereAmI(cur)
        assert wai._cluster_fn is detect_cluster
        assert wai._pivot_fn is get_pivot_context

    def test_cluster_fn_injection(self):
        # Custom _cluster_fn should be used
        marker = _cluster(spread_m=99.0)  # distinct fingerprint
        wai = WhereAmI(_FakeCursor(), _cluster_fn=_fake_cluster_fn(marker))
        # Direct check — call through evaluate with a no-cluster path is too indirect
        assert wai._cluster_fn("driver1", None) is marker

    def test_pivot_fn_injection(self):
        sentinel_pivot = _fake_pivot(current_road="Sentinel Road")
        wai = WhereAmI(_FakeCursor(), _pivot_fn=sentinel_pivot)
        result = wai._pivot_fn("driver1", None)
        assert result["current_road"] == "Sentinel Road"


# =============================================================================
# TestComputeClusterRevisit - Block A (12 pure-function unit tests)
# =============================================================================

class TestComputeClusterRevisit:
    """Direct unit tests for _compute_cluster_revisit() helper.

    Coordinate scheme (L-9 provenance, see _cluster_at module docstring):
      active   at (0, 0)
      near     at (0, 0.001)        ~ 111 m  (under 200m gate)
      gate     at (0, 0.001797)     ~ 200 m  (exactly at gate)
      far      at (0.005, 0.005)    ~ 786 m  (over 200m gate)
      far_axis at (0, 0.005)        ~ 556 m  (over 200m gate, single axis)
    """

    def test_empty_history_returns_false(self):
        """recent=[active] only -> history len 0 -> False."""
        active = _cluster_at(0.0, 0.0, latest_offset_s=100)
        assert _compute_cluster_revisit(active, [active]) is False

    def test_single_history_cluster_returns_false(self):
        """recent=[A_near, active] -> history len 1 -> False."""
        active = _cluster_at(0.0, 0.0, latest_offset_s=100)
        a = _cluster_at(0.0, 0.001, latest_offset_s=10)  # 111m away
        assert _compute_cluster_revisit(active, [a, active]) is False

    def test_no_prior_pudo_within_gap_returns_false(self):
        """All history clusters >200m from active -> no candidate prior_pudo -> False."""
        active = _cluster_at(0.0, 0.0, latest_offset_s=100)
        far_1 = _cluster_at(0.005, 0.005, latest_offset_s=10)  # 786m
        far_2 = _cluster_at(0.0, 0.005, latest_offset_s=20)    # 556m
        assert _compute_cluster_revisit(active, [far_1, far_2, active]) is False

    def test_prior_near_no_intermediate_far_from_both(self):
        """prior near active, intermediate also near both -> False (Q3 from-both guard)."""
        active = _cluster_at(0.0, 0.0, latest_offset_s=100)
        prior = _cluster_at(0.0, 0.001, latest_offset_s=10)    # 111m from active
        # Intermediate at (0, 0.0005) - 56m from active, ~56m from prior.
        # Near both -> fails the from-both gate.
        mid = _cluster_at(0.0, 0.0005, latest_offset_s=20)
        assert _compute_cluster_revisit(active, [prior, mid, active]) is False

    def test_houston_loop_classic_returns_true(self):
        """[near, far, active] chronologically -> True (canonical case)."""
        active = _cluster_at(0.0, 0.0, latest_offset_s=100)
        near = _cluster_at(0.0, 0.001, latest_offset_s=10)     # 111m from active
        far = _cluster_at(0.005, 0.005, latest_offset_s=20)    # 786m from active and near
        assert _compute_cluster_revisit(active, [near, far, active]) is True

    def test_intermediate_close_to_prior_only(self):
        """mid near prior fails the active-gate (from-both guard, geometric edge).

        GPS-multipath protection: when prior is parked ~11m from active, any
        intermediate within 200m of prior is also within ~211m of active.
        Picking mid at (0, 0.0011) ~ 122m from active produces:
          d_mid_to_active = 122m  (FAILS active gate, < 200m)
          d_mid_to_prior  = 111m  (would also fail prior gate)
        Either failure mode triggers the from-both guard. Helper returns False.
        """
        active = _cluster_at(0.0, 0.0, latest_offset_s=100)
        prior = _cluster_at(0.0, 0.0001, latest_offset_s=10)   # 11m from active
        mid = _cluster_at(0.0, 0.0011, latest_offset_s=20)     # 122m from active
        assert _compute_cluster_revisit(active, [prior, mid, active]) is False

    def test_boundary_prior_at_exactly_min_gap(self):
        """prior at exactly 200m -> continue (>= excludes), no candidate, False.

        Tests asymmetric semantics on line 827 of where_am_i.py:
          if d_prior_to_active >= CLUSTER_REVISIT_MIN_GAP_M: continue
        At d ~= 200.04m, 'continue' fires -> prior is NOT a candidate.
        """
        active = _cluster_at(0.0, 0.0, latest_offset_s=100)
        # 0.001800 deg lng ~= 200.15m. Just past gate -> excluded as prior_pudo.
        prior_at_gate = _cluster_at(0.0, 0.001800, latest_offset_s=10)
        far = _cluster_at(0.005, 0.005, latest_offset_s=20)
        # No other near cluster -> no Houston Loop possible -> False.
        assert _compute_cluster_revisit(
            active, [prior_at_gate, far, active]
        ) is False

    def test_boundary_intermediate_at_exactly_min_gap(self):
        """intermediate at exactly 200m from both -> True (>= includes).

        Tests asymmetric semantics on lines 840-841 of where_am_i.py:
          if d_mid_to_active >= 200 AND d_mid_to_prior >= 200: return True
        At d ~= 200m, both conditions fire -> True.

        Setup:
          active = (0, 0)
          prior  = (0.0001, 0)        ~ 11m from active (passes prior gate)
          mid    = (0, 0.001800)      ~ 200.15m from active, ~200.46m from prior
        Both intermediate-gate conditions satisfied -> True.
        """
        active = _cluster_at(0.0, 0.0, latest_offset_s=100)
        prior = _cluster_at(0.0001, 0.0, latest_offset_s=10)    # ~11m from active
        mid_at_gate = _cluster_at(0.0, 0.001800, latest_offset_s=20)
        assert _compute_cluster_revisit(
            active, [prior, mid_at_gate, active]
        ) is True

    def test_chronological_ordering_intermediate_before_prior(self):
        """history [mid_far, prior_near, active] -> mid is BEFORE prior -> False.

        The algorithm only looks for intermediates AFTER prior_pudo via
        history[i+1:]. If the only "far" cluster appears before the only
        "near" cluster chronologically, no valid pair exists.
        """
        active = _cluster_at(0.0, 0.0, latest_offset_s=100)
        mid_far = _cluster_at(0.005, 0.005, latest_offset_s=10)    # earlier
        prior_near = _cluster_at(0.0, 0.001, latest_offset_s=20)   # later
        # mid_far at i=0: >200m from active, fails prior gate (continue).
        # prior_near at i=1: passes prior gate, but history[2:] is empty
        # (active filtered by tuple key) -> no intermediate -> False.
        assert _compute_cluster_revisit(
            active, [mid_far, prior_near, active]
        ) is False

    def test_multiple_priors_one_valid_pair(self):
        """[A_near, B_near, C_far, active] -> A finds C as intermediate -> True.

        Tests algorithm finds first valid pair across multiple priors.
        Iteration order: A first, examines history[1:] = [B, C].
        B near both -> fails. C far from both -> True.
        """
        active = _cluster_at(0.0, 0.0, latest_offset_s=100)
        a_near = _cluster_at(0.0, 0.001, latest_offset_s=10)      # 111m
        b_near = _cluster_at(0.0001, 0.001, latest_offset_s=20)   # ~112m
        c_far = _cluster_at(0.005, 0.005, latest_offset_s=30)     # 786m
        assert _compute_cluster_revisit(
            active, [a_near, b_near, c_far, active]
        ) is True

    def test_active_filtered_by_tuple_key(self):
        """Freshly-constructed Cluster with same key as recent's active -> filtered.

        Verifies equality-by-content filter on lines 810-815 of where_am_i.py:
          active_key = (median_lat, median_lng, latest)
          history = [c for c in recent_clusters if (...) != active_key]

        Critical when callers construct active_cluster fresh and pass alongside
        a recent_clusters list containing a "duplicate" - the duplicate must
        be removed so it doesn't accidentally serve as a prior_pudo candidate.
        """
        active_orig = _cluster_at(0.0, 0.0, latest_offset_s=100)
        active_dup = _cluster_at(0.0, 0.0, latest_offset_s=100)
        assert active_orig is not active_dup  # sanity: different objects
        prior = _cluster_at(0.0, 0.001, latest_offset_s=10)       # 111m
        far = _cluster_at(0.005, 0.005, latest_offset_s=20)       # 786m
        # active_dup filtered by tuple-key equality -> history = [prior, far]
        # -> Houston Loop -> True.
        assert _compute_cluster_revisit(
            active_orig, [prior, far, active_dup]
        ) is True

    def test_same_coords_different_time_not_filtered(self):
        """Same (lat, lng), different latest -> NOT filtered (A12, Q4 ratification).

        Inverse of test_active_filtered_by_tuple_key: a previous visit to
        the SAME location as active should remain in history if its `latest`
        differs. The tuple-key filter uses (lat, lng, latest), so distinct
        timestamps preserve the previous visit.

        Setup: active and prior_visit at identical coords (0,0). But:
          - active.latest = base + 100s (the current cluster)
          - prior_visit.latest = base + 10s (an earlier visit, same spot)
        prior_visit must NOT be filtered. With a far intermediate between,
        this constitutes a true return-to-same-spot Houston Loop -> True.
        """
        active = _cluster_at(0.0, 0.0, latest_offset_s=100)
        prior_visit = _cluster_at(0.0, 0.0, latest_offset_s=10)   # same coords, earlier
        far = _cluster_at(0.005, 0.005, latest_offset_s=20)       # 786m from active
        # prior_visit has 0m distance to active (same coords) -> passes prior gate.
        # far is >200m from both -> passes intermediate gate.
        # Order: prior_visit (10s) -> far (20s) -> active (100s). Valid.
        assert _compute_cluster_revisit(
            active, [prior_visit, far, active]
        ) is True


# =============================================================================
# TestEvaluateClusterRevisit - Block B (7 integration tests via injected fakes)
# =============================================================================

class TestEvaluateClusterRevisit:
    """Cut B2: cluster_revisit lives on DiagnosticContext, not on the match.

    Verifies the Memory Eye lookback behavior:
      - empty queue -> no lookback fetch, cluster_revisit=False
      - non-empty queue -> _recent_clusters_fn called with min(accepted_at)
      - cluster_revisit propagates to diagnostics regardless of match outcome
    """

    def test_empty_queue_revisit_false(self):
        """Empty queue -> Memory Eye skipped, cluster_revisit unconditionally False."""
        cluster = _cluster()
        cur = _FakeCursor()
        # Even injecting a Houston Loop list, an empty queue means Step 3.5
        # doesn't fire — cluster_revisit is workload-scoped, not driver-scoped.
        houston_loop = [
            _cluster_at(0.0, 0.001, latest_offset_s=10),
            _cluster_at(0.005, 0.005, latest_offset_s=20),
            cluster,
        ]
        wai = WhereAmI(
            cur,
            _cluster_fn=_fake_cluster_fn(cluster),
            _pivot_fn=_fake_pivot(),
            _recent_clusters_fn=_fake_recent_clusters_fn(houston_loop),
        )
        _, diagnostics = wai.evaluate_with_diagnostics("driver1", [])
        assert diagnostics.cluster_revisit is False

    def test_no_cluster_revisit_false(self):
        """_cluster_fn returns None -> diagnostics.cluster=None, revisit=False."""
        cur = _FakeCursor()
        wai = WhereAmI(
            cur,
            _cluster_fn=_fake_cluster_fn(None),
            _pivot_fn=_fake_pivot(),
        )
        _, diagnostics = wai.evaluate_with_diagnostics("driver1", [_offer()])
        assert diagnostics.cluster is None
        assert diagnostics.cluster_revisit is False

    def test_offer_no_history_revisit_false(self):
        """Single-offer queue + history without Houston Loop -> revisit False."""
        cluster = _cluster()
        cur = _FakeCursor()
        wai = WhereAmI(
            cur,
            _cluster_fn=_fake_cluster_fn(cluster),
            _pivot_fn=_fake_pivot(),
            _recent_clusters_fn=_fake_recent_clusters_fn([cluster]),
        )
        far_pickup = _target(lat=30.0, lng=-96.0, named_roads=("Nowhere Blvd",))
        offer = _offer(pickup=far_pickup, dropoff=far_pickup)
        _, diagnostics = wai.evaluate_with_diagnostics("driver1", [offer])
        assert diagnostics.cluster_revisit is False

    def test_offer_houston_loop_revisit_true(self):
        """Single-offer queue + Houston Loop history -> revisit True."""
        active = _cluster()
        near = _cluster_at(29.6246, -95.5102 + 0.001, latest_offset_s=10)
        far = _cluster_at(29.6246 + 0.005, -95.5102 + 0.005, latest_offset_s=20)
        history = [near, far, active]

        cur = _FakeCursor()
        wai = WhereAmI(
            cur,
            _cluster_fn=_fake_cluster_fn(active),
            _pivot_fn=_fake_pivot(),
            _recent_clusters_fn=_fake_recent_clusters_fn(history),
        )
        far_pickup = _target(lat=30.0, lng=-96.0, named_roads=("Nowhere Blvd",))
        offer = _offer(pickup=far_pickup, dropoff=far_pickup)
        _, diagnostics = wai.evaluate_with_diagnostics("driver1", [offer])
        assert diagnostics.cluster_revisit is True

    def test_recent_clusters_fn_called_with_min_accepted_at(self):
        """Memory Eye anchors on min(accepted_at) across the queue.

        Two assertions: single-offer (trivial) and multi-offer (proves min).
        Cut B2's Memory Eye extension of Amendment 1's Offer-Anchor Lookback.
        """
        from dataclasses import replace as _dc_replace

        cluster = _cluster()
        captured: list = []

        def _capturing_rc_fn(driver_id, cur, accepted_at_anchor=None, **kwargs):
            captured.append(accepted_at_anchor)
            return [cluster]

        cur = _FakeCursor()
        wai = WhereAmI(
            cur,
            _cluster_fn=_fake_cluster_fn(cluster),
            _pivot_fn=_fake_pivot(),
            _recent_clusters_fn=_capturing_rc_fn,
        )

        # Single-offer queue: anchor is trivially that offer's accepted_at.
        offer = _offer()
        wai.evaluate_with_diagnostics("driver1", [offer])
        assert captured[-1] == offer.accepted_at

        # Multi-offer queue: anchor is the min (oldest accepted_at).
        oldest = _dc_replace(
            offer, offer_id="oldest",
            accepted_at=DUMMY_ACCEPTED_AT - timedelta(hours=2),
        )
        middle = offer
        newest = _dc_replace(
            offer, offer_id="newest",
            accepted_at=DUMMY_ACCEPTED_AT + timedelta(hours=1),
        )
        # Pass in non-sorted order to prove min() handles ordering.
        wai.evaluate_with_diagnostics("driver1", [newest, middle, oldest])
        assert captured[-1] == oldest.accepted_at

    def test_diagnostics_carry_cluster_revisit_when_no_match(self):
        """Far targets -> empty matches, but diagnostics still reports revisit."""
        active = _cluster()
        near = _cluster_at(29.6246, -95.5102 + 0.001, latest_offset_s=10)
        far = _cluster_at(29.6246 + 0.005, -95.5102 + 0.005, latest_offset_s=20)
        history = [near, far, active]

        cur = _FakeCursor()
        wai = WhereAmI(
            cur,
            _cluster_fn=_fake_cluster_fn(active),
            _pivot_fn=_fake_pivot(),
            _recent_clusters_fn=_fake_recent_clusters_fn(history),
        )
        far_pickup = _target(lat=30.0, lng=-96.0, named_roads=("Nowhere Blvd",))
        offer = _offer(pickup=far_pickup, dropoff=far_pickup)
        matches, diagnostics = wai.evaluate_with_diagnostics("driver1", [offer])
        assert matches == []
        assert diagnostics.cluster_revisit is True



# ============================================================================
# Phase 1A: transit-class adjacency gate tests
# (Operation Strip Mall, 2026-05-04)
# ============================================================================

def test_signal_adjacent_road_match_transit_gate_blocks_tertiary():
    """McKeever Rd traffic light regression (offer 8336, 2026-05-04).

    Driver on tertiary road (OSM tag_id 109, mapped to 'transit' class)
    must NOT receive adjacency rescue toward a target road that happens
    to be within ADJACENCY_BUFFER_M (150m). Without the gate, the
    pre-2026-05-04 behavior fired wai_confidence ~0.41 at a McKeever
    traffic light because Sienna Pkwy ran 32m away.
    """
    from where_am_i import _signal_adjacent_road_match
    # adjacent_roads contains the target — pre-gate this would return 1.0
    result = _signal_adjacent_road_match(
        adjacent_roads=("Sienna Pkwy",),
        target_road_names=("Sienna Pkwy",),
        current_road_class="transit",
    )
    assert result == 0.0, (
        f"Transit gate failed: tertiary-road driver should not match "
        f"target via adjacency. Got {result}, expected 0.0."
    )


def test_signal_adjacent_road_match_transit_gate_blocks_secondary():
    """Driver on secondary road (tag_id 108) — same gate applies.

    Generalization of the tertiary case: any 'transit' class
    (motorway/trunk/primary/secondary/tertiary) suppresses adjacency.
    """
    from where_am_i import _signal_adjacent_road_match
    result = _signal_adjacent_road_match(
        adjacent_roads=("Westheimer Rd",),
        target_road_names=("Westheimer Rd",),
        current_road_class="transit",
    )
    assert result == 0.0


def test_signal_adjacent_road_match_transit_gate_blocks_motorway():
    """Driver on motorway (tag_id 101) — gate covers the full transit range.

    Worst-case false positive scenario: driver is on a freeway, a parallel
    surface street with the target name passes within 150m. Adjacency
    must not rescue — the driver is in transit on a different class of
    road entirely.
    """
    from where_am_i import _signal_adjacent_road_match
    result = _signal_adjacent_road_match(
        adjacent_roads=("Some Surface Rd",),
        target_road_names=("Some Surface Rd",),
        current_road_class="transit",
    )
    assert result == 0.0


def test_signal_adjacent_road_match_residential_allows_adjacency():
    """Bees Passage Road (tag_id 110, 'residential' class) preserved.

    The strip-mall frontage road case from offer 8336 ride 1 actual
    dropoff: driver snapped to Bees Passage (residential), Sienna Pkwy
    32m away. Pre-gate this fired with conf 0.42; post-gate it must
    still fire because residential class is the legitimate frontage-road
    case. The gate is class-specific, not blanket.
    """
    from where_am_i import _signal_adjacent_road_match
    result = _signal_adjacent_road_match(
        adjacent_roads=("Sienna Pkwy",),
        target_road_names=("Sienna Pkwy",),
        current_road_class="residential",
    )
    assert result == 1.0, (
        f"Residential-class adjacency should still fire. Got {result}, "
        f"expected 1.0."
    )


def test_signal_adjacent_road_match_off_wire_allows_adjacency():
    """Off-wire / NULL class — original Planet Fitness use case preserved.

    `FORENSIC_2026_04_27_HOUSTON_SHIFT.md` Case B: driver in a strip-mall
    parking lot is off-wire (no road snap within 40m), pivot_context
    returns current_road=None and current_road_class=None. Adjacency
    rescue is the WHOLE POINT of this signal for this case — it must
    NOT be suppressed.
    """
    from where_am_i import _signal_adjacent_road_match

    # NULL (off-wire) — no class set
    result_null = _signal_adjacent_road_match(
        adjacent_roads=("Hollister Rd",),
        target_road_names=("Hollister Rd",),
        current_road_class=None,
    )
    assert result_null == 1.0, "NULL class must allow adjacency"

    # Explicit 'off_wire' class (snapped to a parking-lot service road,
    # tag_id 112/113/117)
    result_off_wire = _signal_adjacent_road_match(
        adjacent_roads=("Hollister Rd",),
        target_road_names=("Hollister Rd",),
        current_road_class="off_wire",
    )
    assert result_off_wire == 1.0, "off_wire class must allow adjacency"

    # 'unknown' class — fail-open
    result_unknown = _signal_adjacent_road_match(
        adjacent_roads=("Hollister Rd",),
        target_road_names=("Hollister Rd",),
        current_road_class="unknown",
    )
    assert result_unknown == 1.0, "unknown class must allow adjacency"



# =============================================================================
# Step F: outcome_for tests
# =============================================================================

def test_outcome_for_returns_matching_outcome():
    """Positive case: outcome_for returns the MatchOutcome whose
    (offer_id, location_type) matches the given WAIMatch.
    """
    from where_am_i import DiagnosticContext, MatchOutcome
    from pudo_types import WAIMatch

    outcome_a_pickup = MatchOutcome(
        matched=True, confidence=0.50, corrected_lat=None, corrected_lng=None,
        reason="prox_hit", pudo_type="pickup", target_address="123 Main St",
        signals={"on_target_road": 1.0},
    )
    outcome_a_dropoff = MatchOutcome(
        matched=False, confidence=0.20, corrected_lat=None, corrected_lng=None,
        reason="no_match", pudo_type=None, target_address="456 Oak Ave",
        signals=None,
    )
    outcome_b_pickup = MatchOutcome(
        matched=True, confidence=0.45, corrected_lat=None, corrected_lng=None,
        reason="prox_partial", pudo_type="pickup", target_address="789 Elm Dr",
        signals={"on_target_road": 0.0},
    )

    diag = DiagnosticContext(
        cluster=None,
        topology=None,
        cluster_revisit=False,
        stop_context="unknown_stop",
        per_target_outcomes=[
            ("offer_A", "pickup", outcome_a_pickup),
            ("offer_A", "dropoff", outcome_a_dropoff),
            ("offer_B", "pickup", outcome_b_pickup),
        ],
    )

    match = WAIMatch(offer_id="offer_A", location_type="pickup", confidence=0.50)
    result = diag.outcome_for(match)

    assert result is outcome_a_pickup, (
        "outcome_for should return the (offer_A, pickup) outcome by identity"
    )


def test_outcome_for_disambiguates_pickup_vs_dropoff():
    """Same offer_id with different location_types must be distinguished."""
    from where_am_i import DiagnosticContext, MatchOutcome
    from pudo_types import WAIMatch

    pickup_outcome = MatchOutcome(
        matched=True, confidence=0.50, corrected_lat=None, corrected_lng=None,
        reason="pickup_match", pudo_type="pickup", target_address="P-addr",
        signals={"on_target_road": 1.0},
    )
    dropoff_outcome = MatchOutcome(
        matched=True, confidence=0.55, corrected_lat=None, corrected_lng=None,
        reason="dropoff_match", pudo_type="dropoff", target_address="D-addr",
        signals={"on_target_road": 1.0},
    )

    diag = DiagnosticContext(
        cluster=None, topology=None, cluster_revisit=False,
        stop_context="unknown_stop",
        per_target_outcomes=[
            ("offer_X", "pickup", pickup_outcome),
            ("offer_X", "dropoff", dropoff_outcome),
        ],
    )

    pickup_match = WAIMatch(offer_id="offer_X", location_type="pickup", confidence=0.50)
    dropoff_match = WAIMatch(offer_id="offer_X", location_type="dropoff", confidence=0.55)

    assert diag.outcome_for(pickup_match) is pickup_outcome
    assert diag.outcome_for(dropoff_match) is dropoff_outcome


def test_outcome_for_returns_none_when_no_match():
    """Defensive: returns None for an offer_id absent from per_target_outcomes."""
    from where_am_i import DiagnosticContext, MatchOutcome
    from pudo_types import WAIMatch

    outcome = MatchOutcome(
        matched=True, confidence=0.50, corrected_lat=None, corrected_lng=None,
        reason="match", pudo_type="pickup", target_address="addr",
        signals=None,
    )

    diag = DiagnosticContext(
        cluster=None, topology=None, cluster_revisit=False,
        stop_context="unknown_stop",
        per_target_outcomes=[("offer_known", "pickup", outcome)],
    )

    match = WAIMatch(offer_id="offer_unknown", location_type="pickup", confidence=0.50)

    assert diag.outcome_for(match) is None


def test_outcome_for_empty_per_target_outcomes_returns_none():
    """Edge case: empty per_target_outcomes returns None for any match."""
    from where_am_i import DiagnosticContext
    from pudo_types import WAIMatch

    diag = DiagnosticContext(
        cluster=None, topology=None, cluster_revisit=False,
        stop_context="unknown_stop",
        per_target_outcomes=[],
    )

    match = WAIMatch(offer_id="any", location_type="pickup", confidence=0.0)

    assert diag.outcome_for(match) is None


# ============================================================================
# Item 2 (Phase 2c.2, 2026-05-08): witness signal integration tests.
#
# Proves that per-class matchers correctly thread `pois` through to
# _signal_poi_match and _signal_poi_type_match, populating MatchOutcome's
# witness fields. This is the integration gate between the unit-tested
# witness signals (test_bead_on_wire.py TestSignalPoi*) and the matcher
# dispatch path (Step 5 of _evaluate()).
#
# Rapidfuzz note: existing _signal_poi_match uses Head 1 fuzzy via rapidfuzz.
# Bible Rule 2 locks both witness signals as-is for this sprint.
# ============================================================================


class TestWitnessWiring:
    """Integration tests proving _build_outcome populates witness fields
    when matchers receive POIs.

    Dormant-wiring reality (Item 2, Phase 2c.2, 2026-05-08):
    poi_match (Patch 2c name-witness) is currently dormant in production
    because TargetSpec lacks an `address` field -- production code
    short-circuits _signal_poi_match to (0.0, None) at function entry.
    Wiring is confirmed dormant; full activation requires TargetSpec
    expansion in a future sprint (Phase 2c.3+). These integration tests
    therefore assert poi_match == 0.0 / poi_witness is None as the
    expected dormant state, while poi_type_match (Head 4) is the live
    witness signal verified by the test assertions.

    Item 3 handoff note: when implementing Rule 3b (Lost Mode commit),
    poi_type_match is the only active semantic witness until the
    TargetSpec.address plumbing lands.
    """

    def _build_target(self, address_class="single_road"):
        """Construct a TargetSpec for matcher tests.

        TargetSpec has 4 fields: lat, lng, address_class, named_roads.
        It does NOT have an `address` field - production code uses
        getattr(target, "address", None) defensively in 7 sites,
        anticipating future `TargetSpec.address` plumbing (Phase 2c.3+).
        Until then, _signal_poi_match receives empty string and
        short-circuits to (0.0, None) (dormant wiring).
        """
        from pudo_types import TargetSpec
        return TargetSpec(
            lat=29.7604,
            lng=-95.3698,
            address_class=address_class,
            named_roads=("Forum Park Dr",),
        )

    def _build_topo(self):
        """Construct a minimal RoadTopology for matcher tests."""
        from where_am_i import RoadTopology
        return RoadTopology(
            on_wire=True,
            current_road="Forum Park Dr",
            last_named_road="Forum Park Dr",
            off_wire_duration_s=0,
            breadcrumb=("Forum Park Dr",),
            adjacent_roads=(),
            current_road_class="residential",
        )

    def _build_cluster(self):
        """Construct a Cluster centered at the target.

        Honors v2.1 Section III (UTC mandatory) for the `latest` field.
        """
        from cluster_detection import Cluster
        from datetime import datetime, timezone
        return Cluster(
            n=10,
            median_lat=29.7604,
            median_lng=-95.3698,
            spread_m=20.0,
            duration_s=60.0,
            latest=datetime.now(timezone.utc),
        )

    def _build_poi(self, name, types, dist_m=50.0):
        """Construct a POI for integration tests."""
        from poi_service import POI
        return POI(
            place_id=f"ChIJ_test_{name.replace(' ', '_').lower()}",
            name=name,
            types=list(types),
            lat=29.7604,
            lng=-95.3698,
            dist_m=dist_m,
        )

    def test_intersection_matcher_populates_witnesses_when_pois_supplied(self):
        from where_am_i import _match_intersection
        cluster = self._build_cluster()
        topo = self._build_topo()
        target = self._build_target(address_class="intersection")
        pois = [self._build_poi("Quick Tire Shop", ["car_repair"])]

        outcome = _match_intersection(cluster, topo, target, pois=pois)

        # poi_type_match should fire TRUE (car_repair is in intersection's accepted set)
        assert outcome.poi_type_match is True
        assert outcome.poi_type_witness == "poi_type:car_repair/Quick Tire Shop"
        # poi_match is dormant (TargetSpec lacks `address` field, short-circuits to 0.0/None)
        assert outcome.poi_match == 0.0
        assert outcome.poi_witness is None

    def test_single_road_matcher_populates_witnesses(self):
        from where_am_i import _match_single_road
        cluster = self._build_cluster()
        topo = self._build_topo()
        target = self._build_target(address_class="single_road")
        pois = [self._build_poi("Excel Dental", ["dentist"])]

        outcome = _match_single_road(cluster, topo, target, pois=pois)

        assert outcome.poi_type_match is True
        assert outcome.poi_type_witness == "poi_type:dentist/Excel Dental"
        # poi_match is dormant (see class docstring)
        assert outcome.poi_match == 0.0
        assert outcome.poi_witness is None

    def test_number_on_street_matcher_populates_witnesses(self):
        from where_am_i import _match_number_on_street
        cluster = self._build_cluster()
        topo = self._build_topo()
        target = self._build_target(address_class="number_on_street")
        pois = [self._build_poi("Pappasito's Cantina", ["restaurant"])]

        outcome = _match_number_on_street(cluster, topo, target, pois=pois)

        assert outcome.poi_type_match is True
        assert outcome.poi_type_witness == "poi_type:restaurant/Pappasito's Cantina"
        # poi_match is dormant (see class docstring)
        assert outcome.poi_match == 0.0
        assert outcome.poi_witness is None

    def test_apartment_complex_matcher_populates_witnesses(self):
        from where_am_i import _match_apartment_complex
        cluster = self._build_cluster()
        topo = self._build_topo()
        target = self._build_target(address_class="apartment_complex")
        pois = [self._build_poi("The Enclave at Sienna", ["lodging"])]

        outcome = _match_apartment_complex(cluster, topo, target, pois=pois)

        assert outcome.poi_type_match is True
        assert outcome.poi_type_witness == "poi_type:lodging/The Enclave at Sienna"

    def test_matcher_with_pois_none_keeps_witnesses_default(self):
        # Backward compat: pois=None (legacy 3-arg call) -> no witnesses populated.
        # Both witness fields default None/None per Bible Rule 2.
        from where_am_i import _match_intersection
        cluster = self._build_cluster()
        topo = self._build_topo()
        target = self._build_target(address_class="intersection")

        outcome = _match_intersection(cluster, topo, target, pois=None)

        # No POIs supplied -> witnesses don't testify.
        assert outcome.poi_type_match is False  # function returns False, not None, for empty pois
        assert outcome.poi_type_witness is None
        assert outcome.poi_match == 0.0
        assert outcome.poi_witness is None

    def test_matchoutcome_has_15_fields(self):
        # Arithmetic gate: MatchOutcome should have exactly 15 dataclass fields.
        # Provenance:
        #   - Originally 10.
        #   - Item 2 (Phase 2c.2, 2026-05-08): +2 for poi_type_match +
        #     poi_type_witness (Head 4 wiring).
        #   - §XVII Patch 2/5 (2026-05-14): +2 for semantic_anchor_score +
        #     semantic_anchor_witness (Head 5 wiring).
        #   - §XVII Patch 4/5 (2026-05-14): +1 for semantic_lookup_source
        #     (POILookupResult.source plumbed through matcher pipeline).
        from where_am_i import MatchOutcome
        fields = list(MatchOutcome.__dataclass_fields__.keys())
        assert len(fields) == 15
        assert "poi_type_match" in fields
        assert "poi_type_witness" in fields
        assert "semantic_anchor_score" in fields
        assert "semantic_anchor_witness" in fields
        assert "semantic_lookup_source" in fields


# =============================================================================
# Phase 2c.2 Item 3 — dual commit rule tests
# =============================================================================
#
# These tests cover the three pieces of Item 3 that landed in commit 07a003e:
#   - Item 3a: Step 4.5 TAD bouncer integration in _evaluate
#   - Item 3c: Per-leg dispatch + dual commit rule
#   - Item 3e: Forensic blob assembly (in-memory, via DiagnosticContext.tad_verdicts)
#
# Items 3b (caller-layer Lost Mode trigger) and 3d (inferred-dropoff anchor
# recovery) are deferred to a separate session and live in driver_heartbeat.py
# rather than where_am_i.py — out of scope for this test suite.
#
# Mocking strategy:
#   - evaluate_tad_gate is patched via unittest.mock.patch — it's a pure
#     module-level function, ideal target for patch.
#   - Per-class matchers are patched via patch.dict(_CLASS_DISPATCH, ...) —
#     swaps in a fake matcher returning a hand-crafted MatchOutcome. This
#     gives deterministic confidence/poi_type_match values without depending
#     on real spatial scoring math.
#   - Cluster, topology, recent_clusters, POI service all use the existing
#     _fake_*_fn injection patterns from earlier in this file.


def _make_outcome(
    matched: bool = True,
    confidence: float = 0.95,
    poi_type_match=None,
    pudo_type: str = "pickup",
    target_address: str = "test target",
):
    """Construct a MatchOutcome for test fakes.

    Defaults: matched=True, confidence=0.95, poi_type_match=None (default).
    Tests override per scenario. The four required-non-defaulted fields
    (corrected_lat/lng, reason, signals) get sensible test values.
    """
    from where_am_i import MatchOutcome
    return MatchOutcome(
        matched=matched,
        confidence=confidence,
        corrected_lat=29.6246 if matched else None,
        corrected_lng=-95.5102 if matched else None,
        reason="test_outcome",
        pudo_type=pudo_type,
        target_address=target_address,
        signals={"proximity": confidence},
        poi_type_match=poi_type_match,
    )


def _make_verdict(passed, leg: str = "pickup", lost_mode_reason: str = None):
    """Construct a TadVerdict for test fakes.

    passed: tristate Optional[bool]. Other fields default to test-friendly
    values. Tests override per scenario.
    """
    from tad import TadVerdict
    return TadVerdict(
        passed=passed,
        time_boost=0.0,
        leg_evaluated=leg,
        lost_mode_reason=lost_mode_reason,
        distance_gate={"mode": "test", "passed": passed},
        time_signal=None,
    )


def _wai_with_fakes(cluster, topo):
    """Build a WhereAmI with fake dependencies for _evaluate exercises.

    Mirrors the kwargs accepted by the existing _fake_pivot() factory in
    this file (does NOT pass current_road_class — that field is on the
    real RoadTopology dataclass but not in the fake_pivot dict signature).
    The _adjacency_fn returns an empty tuple — tests don't exercise the
    adjacent-road signal.
    """
    from where_am_i import WhereAmI
    return WhereAmI(
        cur=MagicMock(),  # not actually used — POI fetch fails-closed to []
        _cluster_fn=_fake_cluster_fn(cluster),
        _pivot_fn=_fake_pivot(
            on_wire=topo.on_wire,
            current_road=topo.current_road,
            last_named_road=topo.last_named_road,
            pivot_time=None,
            breadcrumb=list(topo.breadcrumb),
        ),
        _recent_clusters_fn=_fake_recent_clusters_fn([]),
        _adjacency_fn=lambda cur, c: (),
    )


class TestCommitsHelper:
    """Pure-function tests for _commits() — Fix B (POI as lifter, 2026-05-11)."""

    def test_unmatched_outcome_never_commits(self):
        from where_am_i import _commits
        outcome = _make_outcome(matched=False, confidence=0.99)
        # Even a 0.99 confidence with a True verdict can't override matched=False
        verdict = _make_verdict(passed=True)
        assert _commits(outcome, verdict) is False

    def test_bridge_state_uses_legacy_floor_above(self):
        from where_am_i import _commits, WAI_CONFIDENCE_THRESHOLD
        # Bridge state: verdict=None, confidence above 0.40 floor -> commits
        outcome = _make_outcome(confidence=0.50)  # above WAI_CONFIDENCE_THRESHOLD
        assert WAI_CONFIDENCE_THRESHOLD == 0.40
        assert _commits(outcome, None) is True

    def test_bridge_state_uses_legacy_floor_below(self):
        from where_am_i import _commits
        # Bridge state: verdict=None, confidence below 0.40 floor -> skip
        outcome = _make_outcome(confidence=0.30)
        assert _commits(outcome, None) is False

    def test_normal_mode_at_floor_commits(self):
        from where_am_i import _commits, COMMIT_NORMAL_FLOOR
        # Normal Mode (passed=True): conf >= COMMIT_NORMAL_FLOOR -> commits.
        # TAD's three-signal corroboration is sufficient; POI not required.
        verdict = _make_verdict(passed=True)
        assert COMMIT_NORMAL_FLOOR == 0.40
        outcome_above = _make_outcome(confidence=0.50, poi_type_match=None)
        outcome_at = _make_outcome(confidence=0.40, poi_type_match=None)
        outcome_real = _make_outcome(confidence=0.41, poi_type_match=None)  # §10 A1 empirical
        outcome_below = _make_outcome(confidence=0.39, poi_type_match=None)
        assert _commits(outcome_above, verdict) is True
        assert _commits(outcome_at, verdict) is True
        assert _commits(outcome_real, verdict) is True  # restores the 4224-row drive
        assert _commits(outcome_below, verdict) is False

    def test_normal_mode_poi_lift_clears_borderline(self):
        from where_am_i import _commits, POI_ELEVATOR_LIFT
        # POI lift: poi_type_match=True adds POI_ELEVATOR_LIFT (0.10) to conf.
        # A 0.31 outcome below the 0.40 floor clears with POI lift (0.31 + 0.10 = 0.41).
        verdict = _make_verdict(passed=True)
        assert POI_ELEVATOR_LIFT == 0.10
        # Below floor, no POI -> skip
        assert _commits(_make_outcome(confidence=0.31, poi_type_match=None), verdict) is False
        assert _commits(_make_outcome(confidence=0.31, poi_type_match=False), verdict) is False
        # Below floor, POI True -> commits via lift
        assert _commits(_make_outcome(confidence=0.31, poi_type_match=True), verdict) is True
        # Far below floor, POI True insufficient -> skip
        assert _commits(_make_outcome(confidence=0.20, poi_type_match=True), verdict) is False

    def test_poi_lift_requires_true_not_truthy(self):
        from where_am_i import _commits
        # POI lift only fires when poi_type_match is specifically True.
        # None and False give no lift. A 0.31 outcome stays below floor for both.
        verdict = _make_verdict(passed=True)
        assert _commits(_make_outcome(confidence=0.31, poi_type_match=None), verdict) is False
        assert _commits(_make_outcome(confidence=0.31, poi_type_match=False), verdict) is False
        assert _commits(_make_outcome(confidence=0.31, poi_type_match=True), verdict) is True

    def test_lost_mode_floor(self):
        from where_am_i import _commits, COMMIT_LOST_FLOOR
        # Lost Mode (passed=None): conf >= COMMIT_LOST_FLOOR (0.55).
        # Stricter than Normal Mode because narrative is broken.
        verdict = _make_verdict(passed=None, lost_mode_reason="narrative_blindness")
        assert COMMIT_LOST_FLOOR == 0.55
        # Above floor, no POI -> commits (POI not mandatory under Fix B)
        assert _commits(_make_outcome(confidence=0.60, poi_type_match=None), verdict) is True
        assert _commits(_make_outcome(confidence=0.55, poi_type_match=None), verdict) is True
        # Below floor, no POI -> skip
        assert _commits(_make_outcome(confidence=0.54, poi_type_match=None), verdict) is False
        # POI lift applies in Lost Mode too: 0.46 + 0.10 = 0.56 >= 0.55
        assert _commits(_make_outcome(confidence=0.46, poi_type_match=True), verdict) is True
        # POI lift insufficient if confidence too far below floor
        assert _commits(_make_outcome(confidence=0.40, poi_type_match=True), verdict) is False


class TestClassifyCommitRule:
    """Pure-function tests for classify_commit_rule() — Fix B labels (2026-05-11)."""

    def test_legacy_floor_when_no_verdict(self):
        from where_am_i import classify_commit_rule
        outcome = _make_outcome(confidence=0.50)
        assert classify_commit_rule(outcome, None) == "legacy_floor"

    def test_normal_floor_without_poi(self):
        from where_am_i import classify_commit_rule
        # Normal Mode, no POI lift -> "normal_floor"
        verdict = _make_verdict(passed=True)
        assert classify_commit_rule(_make_outcome(confidence=0.50, poi_type_match=None), verdict) == "normal_floor"
        assert classify_commit_rule(_make_outcome(confidence=0.41, poi_type_match=False), verdict) == "normal_floor"

    def test_normal_floor_with_poi_lift(self):
        from where_am_i import classify_commit_rule
        # Normal Mode, POI True -> "normal_floor_with_poi_lift"
        verdict = _make_verdict(passed=True)
        assert classify_commit_rule(_make_outcome(confidence=0.50, poi_type_match=True), verdict) == "normal_floor_with_poi_lift"
        assert classify_commit_rule(_make_outcome(confidence=0.31, poi_type_match=True), verdict) == "normal_floor_with_poi_lift"

    def test_lost_floor_without_poi(self):
        from where_am_i import classify_commit_rule
        # Lost Mode, no POI lift -> "lost_floor"
        verdict = _make_verdict(passed=None, lost_mode_reason="narrative_violation")
        assert classify_commit_rule(_make_outcome(confidence=0.60, poi_type_match=None), verdict) == "lost_floor"
        assert classify_commit_rule(_make_outcome(confidence=0.55, poi_type_match=False), verdict) == "lost_floor"

    def test_lost_floor_with_poi_lift(self):
        from where_am_i import classify_commit_rule
        # Lost Mode, POI True -> "lost_floor_with_poi_lift"
        verdict = _make_verdict(passed=None, lost_mode_reason="narrative_violation")
        assert classify_commit_rule(_make_outcome(confidence=0.60, poi_type_match=True), verdict) == "lost_floor_with_poi_lift"
        assert classify_commit_rule(_make_outcome(confidence=0.46, poi_type_match=True), verdict) == "lost_floor_with_poi_lift"

    def test_unknown_when_passed_is_false(self):
        from where_am_i import classify_commit_rule
        # Defensive: passed=False shouldn't reach classify_commit_rule in
        # production (Step 5 skips dispatch), but if it does, label as unknown.
        verdict = _make_verdict(passed=False)
        assert classify_commit_rule(_make_outcome(confidence=0.95), verdict) == "unknown"

    def test_classify_matches_commits_decision(self):
        """Cross-check: every classification corresponds to a True _commits result.

        Table-driven sanity check that classify_commit_rule and _commits stay
        in sync. If the dual rule changes in one but not the other, this fires.

        Fix B (2026-05-11): cases cover bridge state, Normal Mode (with and
        without POI lift), and Lost Mode (with and without POI lift). Bridge-
        state semantics (verdict is None) remain distinct from Lost Mode
        (verdict.passed is None) — the two None values mean different things.
        """
        from where_am_i import _commits, classify_commit_rule
        cases = [
            # (outcome_kwargs, verdict, expected_label)
            (dict(confidence=0.50), None, "legacy_floor"),
            (dict(confidence=0.41, poi_type_match=None), _make_verdict(passed=True), "normal_floor"),
            (dict(confidence=0.31, poi_type_match=True), _make_verdict(passed=True), "normal_floor_with_poi_lift"),
            (dict(confidence=0.60, poi_type_match=None), _make_verdict(passed=None), "lost_floor"),
            (dict(confidence=0.46, poi_type_match=True), _make_verdict(passed=None), "lost_floor_with_poi_lift"),
        ]
        for outcome_kwargs, verdict, expected_label in cases:
            outcome = _make_outcome(**outcome_kwargs)
            assert _commits(outcome, verdict) is True, (
                f"_commits returned False for case expected to commit: "
                f"outcome={outcome_kwargs}, verdict={verdict}, label={expected_label}"
            )
            assert classify_commit_rule(outcome, verdict) == expected_label


class TestItem3DualCommitRule:
    """Integration tests for _evaluate with TAD context (Items 3a, 3c, 3e)."""

    def _build_cluster(self):
        return _cluster()

    def _build_topo(self):
        return _topo()

    def _build_offer(self, offer_id: str = "offer_a", address_class: str = "single_road"):
        # _offer() builds an Offer; _target() builds the TargetSpec.
        # Use single_road as default address_class for predictable dispatch.
        # Note: TargetSpec has no `address` field (Bible Finding 3 — Patch 2c
        # is dormant until that field lands). _target() accepts only
        # lat/lng/address_class/named_roads.
        pickup = _target(address_class=address_class)
        dropoff = _target(address_class=address_class, lat=29.7000, lng=-95.4000)
        return _offer(offer_id=offer_id, pickup=pickup, dropoff=dropoff)

    def test_constants_exist_with_correct_values(self):
        """Module-level constants for the dual commit rule are exported correctly."""
        from where_am_i import COMMIT_NORMAL_FLOOR, COMMIT_LOST_FLOOR, POI_ELEVATOR_LIFT
        assert COMMIT_NORMAL_FLOOR == 0.40
        assert COMMIT_LOST_FLOOR == 0.55
        assert POI_ELEVATOR_LIFT == 0.10

    def test_bridge_state_preserves_legacy_behavior(self):
        """No TAD context supplied -> legacy floor, evaluate_tad_gate not called."""
        cluster = self._build_cluster()
        topo = self._build_topo()
        wai = _wai_with_fakes(cluster, topo)
        offer = self._build_offer()

        with patch("where_am_i.evaluate_tad_gate") as mock_tad,              patch("where_am_i._CLASS_DISPATCH", {"single_road": lambda c, t, tg, pois, **kwargs: _make_outcome(confidence=0.50)}):
            matches, diag = wai.evaluate_with_diagnostics("driver1", [offer])

        # TAD gate NOT called in bridge state
        mock_tad.assert_not_called()
        # Legacy floor (0.40) is cleared by 0.50 confidence -> match commits
        assert len(matches) >= 1
        # Forensic field present and empty in bridge state
        assert diag.tad_verdicts == {}

    def test_step_4_5_calls_evaluate_tad_gate_when_context_supplied(self):
        """TAD context supplied -> evaluate_tad_gate called once with expected kwargs."""
        cluster = self._build_cluster()
        topo = self._build_topo()
        wai = _wai_with_fakes(cluster, topo)
        offer = self._build_offer()

        from tad import OfferTadState
        from datetime import datetime, timezone

        per_offer_state = {
            offer.offer_id: OfferTadState(
                offer_id=offer.offer_id,
                miles_at_offer_receipt=1.0,
                accepted_at=DUMMY_ACCEPTED_AT,
                expected_pickup_arrival_time=datetime(2026, 4, 27, 8, 5, tzinfo=timezone.utc),
                expected_pickup_distance=2.0,
                actual_pickup_at=None,
                cumulative_miles_at_pickup_fire=None,
                pickup_exit_time=None,
                exit_velocity_timeout=False,
            )
        }

        fake_verdicts = {offer.offer_id: _make_verdict(passed=True)}

        with patch("where_am_i.evaluate_tad_gate", return_value=fake_verdicts) as mock_tad,              patch("where_am_i._CLASS_DISPATCH", {"single_road": lambda c, t, tg, pois, **kwargs: _make_outcome(confidence=0.95)}):
            matches, diag = wai.evaluate_with_diagnostics(
                "driver1", [offer],
                per_offer_state=per_offer_state,
                current_odometer=1.5,
            )

        mock_tad.assert_called_once()
        # Diagnostics carry the verdicts dict for forensic JSONB serialization
        assert diag.tad_verdicts == fake_verdicts

    def test_step_5_skips_dispatch_for_passed_false_verdict(self):
        """passed=False verdict -> per-class matcher NOT invoked for that offer."""
        cluster = self._build_cluster()
        topo = self._build_topo()
        wai = _wai_with_fakes(cluster, topo)
        offer = self._build_offer()

        from tad import OfferTadState
        from datetime import datetime, timezone

        per_offer_state = {
            offer.offer_id: OfferTadState(
                offer_id=offer.offer_id,
                miles_at_offer_receipt=1.0,
                accepted_at=DUMMY_ACCEPTED_AT,
                expected_pickup_arrival_time=datetime(2026, 4, 27, 8, 5, tzinfo=timezone.utc),
                expected_pickup_distance=2.0,
                actual_pickup_at=None,
                cumulative_miles_at_pickup_fire=None,
                pickup_exit_time=None,
                exit_velocity_timeout=False,
            )
        }

        fake_verdicts = {offer.offer_id: _make_verdict(passed=False)}

        # Track whether the matcher gets called
        matcher_calls = []
        def tracking_matcher(c, t, tg, pois, **kwargs):
            matcher_calls.append((c, tg))
            return _make_outcome(confidence=0.99)

        with patch("where_am_i.evaluate_tad_gate", return_value=fake_verdicts),              patch("where_am_i._CLASS_DISPATCH", {"single_road": tracking_matcher}):
            matches, diag = wai.evaluate_with_diagnostics(
                "driver1", [offer],
                per_offer_state=per_offer_state,
                current_odometer=1.5,
            )

        # Matcher NOT called: TAD bouncer skipped the offer entirely
        assert len(matcher_calls) == 0, (
            f"Matcher was called {len(matcher_calls)} times; expected 0 for passed=False verdict"
        )
        # No matches produced
        assert matches == []
        # Forensic context still carries the verdict for the JSONB blob
        assert diag.tad_verdicts == fake_verdicts

    def test_step_6_normal_mode_commits_at_floor_without_poi(self):
        """Fix B (2026-05-11): Normal Mode (passed=True), confidence at the
        empirical §10 A1 boundary (0.41) commits without poi_type_match.

        Replaces the legacy 'high floor' test which asserted 0.91 commits
        standalone — under the dead Rule 3a contract, 0.91 was the boundary
        between 'standalone commit' and 'elevator-with-POI commit'. Under
        Fix B, the meaningful boundary is COMMIT_NORMAL_FLOOR (0.40) where
        TAD verdict.passed=True provides three-signal corroboration that
        is sufficient for commit. Empirical: §10 A1 documents 13 heartbeats
        scoring 0.404-0.409 against a real PUDO; this test asserts they
        commit under Fix B.
        """
        cluster = self._build_cluster()
        topo = self._build_topo()
        wai = _wai_with_fakes(cluster, topo)
        offer = self._build_offer()

        from tad import OfferTadState
        from datetime import datetime, timezone

        per_offer_state = {
            offer.offer_id: OfferTadState(
                offer_id=offer.offer_id,
                miles_at_offer_receipt=1.0,
                accepted_at=DUMMY_ACCEPTED_AT,
                expected_pickup_arrival_time=datetime(2026, 4, 27, 8, 5, tzinfo=timezone.utc),
                expected_pickup_distance=2.0,
                actual_pickup_at=None,
                cumulative_miles_at_pickup_fire=None,
                pickup_exit_time=None,
                exit_velocity_timeout=False,
            )
        }

        fake_verdicts = {offer.offer_id: _make_verdict(passed=True)}

        with patch("where_am_i.evaluate_tad_gate", return_value=fake_verdicts),              patch("where_am_i._CLASS_DISPATCH", {"single_road": lambda c, t, tg, pois, **kwargs: _make_outcome(confidence=0.41, poi_type_match=None)}):
            matches, _ = wai.evaluate_with_diagnostics(
                "driver1", [offer],
                per_offer_state=per_offer_state,
                current_odometer=1.5,
            )

        assert len(matches) >= 1, (
            "Normal Mode at §10 A1 empirical confidence (0.41) should commit "
            "without POI lift under Fix B; TAD passed=True is sufficient."
        )

    def test_step_6_lost_mode_commits_above_floor_without_poi(self):
        """Fix B (2026-05-11): Lost Mode (passed=None), confidence 0.86
        above COMMIT_LOST_FLOOR (0.55) commits without poi_type_match.

        INVERTED from the legacy test that asserted Lost Mode required POI.
        Under the dead Rule 3b contract, Lost Mode demanded mandatory POI
        corroboration to compensate for broken narrative. Fix B replaces
        this with a stricter Lost Mode floor (0.55 vs Normal Mode's 0.40)
        that itself compensates for the broken narrative — POI is no longer
        mandatory in any mode.

        A separate test (test_step_6_lost_mode_below_floor_skips) covers
        the below-floor skip case.
        """
        cluster = self._build_cluster()
        topo = self._build_topo()
        wai = _wai_with_fakes(cluster, topo)
        offer = self._build_offer()

        from tad import OfferTadState
        from datetime import datetime, timezone

        per_offer_state = {
            offer.offer_id: OfferTadState(
                offer_id=offer.offer_id,
                miles_at_offer_receipt=1.0,
                accepted_at=DUMMY_ACCEPTED_AT,
                expected_pickup_arrival_time=datetime(2026, 4, 27, 8, 5, tzinfo=timezone.utc),
                expected_pickup_distance=2.0,
                actual_pickup_at=None,
                cumulative_miles_at_pickup_fire=None,
                pickup_exit_time=None,
                exit_velocity_timeout=False,
            )
        }

        fake_verdicts = {offer.offer_id: _make_verdict(passed=None, lost_mode_reason="narrative_blindness")}

        with patch("where_am_i.evaluate_tad_gate", return_value=fake_verdicts),              patch("where_am_i._CLASS_DISPATCH", {"single_road": lambda c, t, tg, pois, **kwargs: _make_outcome(confidence=0.86, poi_type_match=None)}):
            matches, _ = wai.evaluate_with_diagnostics(
                "driver1", [offer],
                per_offer_state=per_offer_state,
                current_odometer=1.5,
                lost_mode=True,
            )

        # Fix B: 0.86 >= 0.55 Lost Floor; commits without requiring POI
        assert len(matches) >= 1, (
            "Lost Mode at 0.86 confidence (well above 0.55 floor) should commit "
            "without POI lift under Fix B; stricter floor compensates for broken narrative."
        )

    def test_step_6_lost_mode_with_poi_lift_commits(self):
        """Fix B (2026-05-11): Lost Mode (passed=None), confidence 0.86 +
        poi_type_match=True -> commits with POI lift contributing.

        Renamed from test_step_6_lost_mode_commits_with_poi_type_match.
        Under Fix B, POI is no longer required to commit in Lost Mode;
        this test now demonstrates that POI lift (the +0.10 bonus to
        effective confidence) is captured when present — forensic visibility
        into POI contribution. The match still commits without POI per
        test_step_6_lost_mode_commits_above_floor_without_poi; the
        difference under Fix B is whether classify_commit_rule labels the
        match as 'lost_floor' or 'lost_floor_with_poi_lift'.
        """
        cluster = self._build_cluster()
        topo = self._build_topo()
        wai = _wai_with_fakes(cluster, topo)
        offer = self._build_offer()

        from tad import OfferTadState
        from datetime import datetime, timezone

        per_offer_state = {
            offer.offer_id: OfferTadState(
                offer_id=offer.offer_id,
                miles_at_offer_receipt=1.0,
                accepted_at=DUMMY_ACCEPTED_AT,
                expected_pickup_arrival_time=datetime(2026, 4, 27, 8, 5, tzinfo=timezone.utc),
                expected_pickup_distance=2.0,
                actual_pickup_at=None,
                cumulative_miles_at_pickup_fire=None,
                pickup_exit_time=None,
                exit_velocity_timeout=False,
            )
        }

        fake_verdicts = {offer.offer_id: _make_verdict(passed=None, lost_mode_reason="narrative_blindness")}

        with patch("where_am_i.evaluate_tad_gate", return_value=fake_verdicts),              patch("where_am_i._CLASS_DISPATCH", {"single_road": lambda c, t, tg, pois, **kwargs: _make_outcome(confidence=0.86, poi_type_match=True)}):
            matches, _ = wai.evaluate_with_diagnostics(
                "driver1", [offer],
                per_offer_state=per_offer_state,
                current_odometer=1.5,
                lost_mode=True,
            )

        assert len(matches) >= 1, (
            "Lost Mode at 0.86 + POI lift should commit under Fix B "
            "(0.86 + 0.10 = 0.96 effective, well above 0.55 floor)."
        )

    def test_empty_queue_with_lost_mode_does_not_crash(self):
        """Empty queue + lost_mode=True -> no verdicts, no exception."""
        cluster = self._build_cluster()
        topo = self._build_topo()
        wai = _wai_with_fakes(cluster, topo)

        with patch("where_am_i.evaluate_tad_gate") as mock_tad:
            matches, diag = wai.evaluate_with_diagnostics(
                "driver1", [],
                per_offer_state={},
                current_odometer=1.5,
                lost_mode=True,
            )

        # TAD gate NOT called for empty queue (the `if queue and ...` guard)
        mock_tad.assert_not_called()
        assert matches == []
        assert diag.tad_verdicts == {}

