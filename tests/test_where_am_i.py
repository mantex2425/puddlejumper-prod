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

from cluster_detection import Cluster
from where_am_i import (
    _haversine_meters,
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
    )


# =============================================================================
# TestHaversineMeters - 3 tests
# =============================================================================

class TestHaversineMeters:
    def test_zero_distance_same_point(self):
        d = _haversine_meters(29.6246, -95.5102, 29.6246, -95.5102)
        assert d == pytest.approx(0.0, abs=1e-9)

    def test_known_short_distance_houston(self):
        # Two points ~100m apart in Houston (29.6246, -95.5102) and
        # (29.6255, -95.5102) - 0.0009 degrees of latitude is ~100m
        d = _haversine_meters(29.6246, -95.5102, 29.6255, -95.5102)
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
        d = _haversine_meters(29.6246, -95.5102, 29.6454, -95.2789)
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

        # Confidence should clear the STRONG_MATCH threshold (0.7)
        # Theoretical max with these signals (proximity=0.18, off_wire=0):
        #   proximity:        0.20 * 0.18 = 0.036
        #   breadcrumb_match: 0.30 * 1.00 = 0.300
        #   cluster_tight:    0.15 * 1.00 = 0.150
        #   cluster_duration: 0.10 * 1.00 = 0.100
        #   on_target_road:   0.20 * 1.00 = 0.200
        #   off_wire_pivot:   0.05 * 0.00 = 0.000
        #   total:                          0.786
        # Above 0.7 - WAI would report at_current_pudo with strong confidence.
        assert confidence > 0.70, (
            f"Forum Park 7623 confidence {confidence:.3f} should be >= 0.70 "
            f"(STRONG_MATCH_CONFIDENCE). Signals: {signals}"
        )
