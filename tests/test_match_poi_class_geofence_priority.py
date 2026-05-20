"""P18 sentinel tests: _match_poi_class ensemble priority."""
from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import patch

from where_am_i import _match_poi_class


@dataclass(frozen=True)
class _StubCluster:
    median_lat: float = 29.65479
    median_lng: float = -95.27736
    spread_m: float = 5.0
    duration_s: float = 60.0


@dataclass(frozen=True)
class _StubTopo:
    current_road: str = ""
    breadcrumb_roads: tuple = ()
    adjacent_roads: tuple = ()
    road_membership: bool = False


@dataclass(frozen=True)
class _StubTarget:
    address: str
    address_class: str = "poi"
    lat: float = 29.65479
    lng: float = -95.27736
    target_name: str = ""


CLUSTER = _StubCluster()
TOPO = _StubTopo()


def test_geofence_definitive_overrides_legacy():
    """h6 = 1.0 wins even if h5 and h1 also have non-zero scores."""
    target = _StubTarget(address="William P. Hobby Airport (HOU), Houston, Texas")
    # Spike h5 to 0.5 to prove geofence still wins.
    with patch("where_am_i._signal_semantic_anchor", return_value=(0.5, "h5_witness")):
        with patch("where_am_i._signal_poi_match", return_value=(0.44, "h1_witness")):
            with patch("where_am_i._signal_poi_type_match", return_value=(False, "h4_witness")):
                outcome = _match_poi_class(
                    CLUSTER, TOPO, target,
                    pois=[], anchors=[],
                    geofence_score=1.0, geofence_witness="geofence:Hobby/airport [iata:HOU]",
                )
    assert outcome.confidence == 1.0, f"got {outcome.confidence}"


def test_no_containment_falls_to_h5():
    """h6 = 0.0 (no containment) → confidence = h5."""
    target = _StubTarget(address="American Airlines, Houston, Texas")
    with patch("where_am_i._signal_semantic_anchor", return_value=(0.7, "h5_witness")):
        with patch("where_am_i._signal_poi_match", return_value=(0.44, "h1_witness")):
            with patch("where_am_i._signal_poi_type_match", return_value=(False, "h4_witness")):
                outcome = _match_poi_class(
                    CLUSTER, TOPO, target,
                    pois=[], anchors=[],
                    geofence_score=0.0, geofence_witness=None,
                )
    assert outcome.confidence == 0.7, f"got {outcome.confidence}"


def test_partial_containment_h1_excluded_from_max():
    """h6 = 0.30 (contained, no name match) → h1 still excluded; h5 wins.

    This is the 8082-class regression test: if h1 = 0.444 (the Braeburn Liquor
    false fire we are eliminating) and h5 = 0.0, ensemble must NOT pick h1.
    """
    target = _StubTarget(address="American Airlines, Houston, Texas")
    with patch("where_am_i._signal_semantic_anchor", return_value=(0.0, None)):
        with patch("where_am_i._signal_poi_match", return_value=(0.444, "h1_braeburn")):
            with patch("where_am_i._signal_poi_type_match", return_value=(False, "h4_witness")):
                outcome = _match_poi_class(
                    CLUSTER, TOPO, target,
                    pois=[], anchors=[],
                    geofence_score=0.30, geofence_witness="geofence:contained-no-name-match",
                )
    # Neither geofence (0.30 below the 1.0 gate) nor semantic (0.0) clears anything.
    # Head 1's 0.444 must NOT win — that was the 8082 wrong-fire source.
    assert outcome.confidence < 0.4, f"P18 must block h1 0.444 wrong-fire; got {outcome.confidence}"
