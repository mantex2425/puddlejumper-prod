"""Path B.2 extension tests — _apply_road_membership_override.

Tests for the dispatch-site helper that seeds three road-related
signal inputs (current_road, breadcrumb, adjacent_roads) when
road_membership confirms cluster on target road.

Pre-extension (just current_road seeded) ride 1 confidence was 0.42 —
just over floor. Post-extension (all three seeded) predicted 0.866 —
well over §XVI dual-commit strict gate (0.80). This file pins both the
helper's behavior and the integration math.

Coverage (6 tests):
  1. Helper seeds all three fields on hit.
  2. Helper preserves existing breadcrumb/adjacent entries (prepends).
  3. Helper skips non-road address classes (no SQL traffic).
  4. Helper returns unchanged topo on no-match.
  5. Helper handles multi-road intersection (first hit wins).
  6. Integration: ride 1 cluster + all signals seeded -> confidence >= 0.80.
"""
import datetime
from unittest.mock import patch

import pytest

from where_am_i import (
    _apply_road_membership_override,
    _match_single_road,
    RoadTopology,
)
from cluster_detection import Cluster
from pudo_types import TargetSpec


# Ride 1 forensic anchor — replay's apartment cluster (n=6, dur=28s,
# spread=20.6m, centroid 29.525473/-95.528919).
RIDE_1_LAT = 29.525473
RIDE_1_LNG = -95.528919


def _ride_1_cluster() -> Cluster:
    return Cluster(
        n=6,
        median_lat=RIDE_1_LAT,
        median_lng=RIDE_1_LNG,
        spread_m=20.6,
        duration_s=28.0,
        latest=datetime.datetime.now(datetime.timezone.utc),
    )


def _empty_topo() -> RoadTopology:
    return RoadTopology(
        on_wire=False,
        current_road=None,
        last_named_road=None,
        off_wire_duration_s=0,
        breadcrumb=(),
        adjacent_roads=(),
        current_road_class=None,
    )


def _single_road_target(road: str = "Watts Plantation Dr") -> TargetSpec:
    return TargetSpec(
        address=f"{road}, Missouri City, Texas",
        lat=29.5253574,    # Google geocoded — 3.2km off, per ride 1 forensics
        lng=-95.4955346,
        named_roads=(road,),
        address_class="single_road",
    )


def _intersection_target(
    roads: tuple = ("Joan St", "Settemont Rd"),
) -> TargetSpec:
    return TargetSpec(
        address=f"{' & '.join(roads)}, Houston, Texas",
        lat=29.7604,
        lng=-95.3698,
        named_roads=roads,
        address_class="intersection",
    )


class TestHelperOverride:
    """Helper extracts the dispatch-site override logic. Tests directly
    exercise the helper without running evaluate() end-to-end."""

    def test_seeds_all_three_fields_on_hit(self):
        cluster = _ride_1_cluster()
        topo = _empty_topo()
        target = _single_road_target("Watts Plantation Dr")

        with patch("where_am_i.is_cluster_on_road", return_value=True):
            new_topo = _apply_road_membership_override(
                object(), cluster, topo, target,
            )

        assert new_topo is not topo, "helper must return a new topo on hit"
        assert new_topo.current_road == "Watts Plantation Dr"
        assert new_topo.breadcrumb[0] == "Watts Plantation Dr"
        assert new_topo.adjacent_roads[0] == "Watts Plantation Dr"
        # Other fields preserved unchanged.
        assert new_topo.on_wire == topo.on_wire
        assert new_topo.last_named_road == topo.last_named_road
        assert new_topo.off_wire_duration_s == topo.off_wire_duration_s
        assert new_topo.current_road_class == topo.current_road_class

    def test_preserves_existing_breadcrumb_and_adjacent(self):
        """Helper prepends the offer-form road; existing entries remain."""
        cluster = _ride_1_cluster()
        topo = RoadTopology(
            on_wire=False,
            current_road=None,
            last_named_road="Some Pkwy",
            off_wire_duration_s=60,
            breadcrumb=("Some Pkwy", "Old Rd"),
            adjacent_roads=("Some Pkwy",),
            current_road_class=None,
        )
        target = _single_road_target("Watts Plantation Dr")

        with patch("where_am_i.is_cluster_on_road", return_value=True):
            new_topo = _apply_road_membership_override(
                object(), cluster, topo, target,
            )

        assert new_topo.breadcrumb == (
            "Watts Plantation Dr", "Some Pkwy", "Old Rd",
        )
        assert new_topo.adjacent_roads == (
            "Watts Plantation Dr", "Some Pkwy",
        )

    def test_skips_non_road_address_classes(self):
        """apartment_complex, poi, number_on_street → pass through, no
        cursor traffic. Path B.2 is scoped to road-naming classes only."""
        cluster = _ride_1_cluster()
        topo = _empty_topo()

        for ac in ("apartment_complex", "poi", "number_on_street"):
            target = TargetSpec(
                address="x", lat=29.0, lng=-95.0,
                named_roads=("X Dr",), address_class=ac,
            )
            with patch("where_am_i.is_cluster_on_road") as mock_check:
                new_topo = _apply_road_membership_override(
                    object(), cluster, topo, target,
                )
            assert new_topo is topo, (
                f"{ac} should pass through unchanged"
            )
            mock_check.assert_not_called()

    def test_no_match_returns_unchanged_topo(self):
        """When is_cluster_on_road returns False for every target road,
        helper returns the original topo (object identity preserved)."""
        cluster = _ride_1_cluster()
        topo = _empty_topo()
        target = _single_road_target("Westheimer Rd")

        with patch("where_am_i.is_cluster_on_road", return_value=False):
            new_topo = _apply_road_membership_override(
                object(), cluster, topo, target,
            )

        assert new_topo is topo

    def test_intersection_first_hit_wins(self):
        """For intersection class with multiple named_roads, helper
        iterates in order and seeds on the FIRST hit. The losing roads
        don't get their own cache calls after the winner — implementation
        breaks out of the loop on first success."""
        cluster = _ride_1_cluster()
        topo = _empty_topo()
        target = _intersection_target(("Joan St", "Settemont Rd"))

        # First road misses, second hits.
        with patch(
            "where_am_i.is_cluster_on_road",
            side_effect=[False, True],
        ) as mock_check:
            new_topo = _apply_road_membership_override(
                object(), cluster, topo, target,
            )

        assert new_topo.current_road == "Settemont Rd"
        assert new_topo.breadcrumb[0] == "Settemont Rd"
        assert new_topo.adjacent_roads[0] == "Settemont Rd"
        assert mock_check.call_count == 2


class TestRide1Math:
    """Integration: with all three signals seeded by the helper,
    ride 1 confidence math should comfortably clear floor — and clear
    the §XVI dual-commit strict gate (0.80) too."""

    def test_ride_1_apartment_confidence_strong(self):
        """The 'hell yes' assertion: post-extension confidence should
        be >= 0.80 (§XVI strict commit gate), not just >= 0.40 (floor).

        Math reconstruction (with weights from _CONFIDENCE_WEIGHTS):
            on_target_road    1.00 × 0.15 = 0.150
            breadcrumb_match  1.00 × 0.35 = 0.350
            adj_road_match    1.00 × 0.10 = 0.100
            cluster_tightness 0.84 × 0.15 = 0.126
            cluster_duration  0.93 × 0.15 = 0.140
            proximity         0.00 × 0.05 = 0
            off_wire_pivot    0.00 × 0.05 = 0
            TOTAL                          = 0.866
        """
        cluster = _ride_1_cluster()
        topo = _empty_topo()
        target = _single_road_target("Watts Plantation Dr")

        with patch("where_am_i.is_cluster_on_road", return_value=True):
            matcher_topo = _apply_road_membership_override(
                object(), cluster, topo, target,
            )

        outcome = _match_single_road(
            cluster, matcher_topo, target, pois=[], anchors=[],
        )

        # All three road signals must fire at 1.0 (this is what Path B.2
        # extension is FOR; if any of them are 0, the helper isn't
        # seeding what we think it's seeding).
        assert outcome.signals["on_target_road"] == 1.0, (
            f"on_target_road expected 1.0, got "
            f"{outcome.signals['on_target_road']}"
        )
        assert outcome.signals["breadcrumb_match"] == 1.0, (
            f"breadcrumb_match expected 1.0, got "
            f"{outcome.signals['breadcrumb_match']}"
        )
        assert outcome.signals["adjacent_road_match"] == 1.0, (
            f"adjacent_road_match expected 1.0, got "
            f"{outcome.signals['adjacent_road_match']}"
        )

        # Total confidence must clear the strict commit gate.
        assert outcome.matched is True
        assert outcome.confidence >= 0.80, (
            f"Ride 1 with all three signals seeded should clear §XVI strict "
            f"commit gate (>= 0.80); got {outcome.confidence:.4f}. Check "
            f"signal contributions: {outcome.signals}"
        )
