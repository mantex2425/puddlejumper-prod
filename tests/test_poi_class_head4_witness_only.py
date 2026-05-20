"""
P16 sentinel: POI class Head 4 (poi_type_match) is witness-only, never a scorer.

Andrew + Gemini ratification 2026-05-20.

Guarantees that a synthetic cluster with a nearby POI of an accepted type
(e.g. bus_station within 100m) does NOT cause _match_poi_class to return
confidence >= WAI_CONFIDENCE_THRESHOLD when Head 5 (semantic anchor) and
Head 1 (fuzzy/branded/airport name) both score 0.

If this test ever fails post-merge, Head 4 has been re-promoted into the
confidence ensemble and urban POI noise will leak false positives across
every airport-class offer in the queue. See production drive 2026-05-20
forensic for the failure mode this test prevents.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from pudo_types import WAI_CONFIDENCE_THRESHOLD
from where_am_i import _match_poi_class


@dataclass(frozen=True)
class _StubCluster:
    median_lat: float = 29.6713
    median_lng: float = -95.5284
    spread_m: float = 5.0
    duration_s: float = 30.0


@dataclass(frozen=True)
class _StubTopo:
    on_target_road: bool = False


@dataclass(frozen=True)
class _StubTarget:
    address_class: str = "poi"
    address: str = "American Airlines, Houston, Texas"
    lat: float = 29.6546
    lng: float = -95.2760


@dataclass(frozen=True)
class _StubPOI:
    place_id: str
    name: str
    types: list
    lat: float
    lng: float
    dist_m: float


def test_head4_alone_cannot_clear_wai_floor():
    """Bus station 5m from cluster, no semantic anchor, no fuzzy name match.

    Pre-P16: Head 4 returned 1.0 (boolean coercion), max() picked it,
    confidence = 1.000 → fired wrong location.
    Post-P16: Head 4 excluded from max(). Head 5 = 0, Head 1 = 0,
    confidence = 0 → does NOT fire.
    """
    cluster = _StubCluster()
    topo = _StubTopo()
    target = _StubTarget()

    bus_stop = _StubPOI(
        place_id="ChIJ_test_bus_stop",
        name="METRO Bissonnet Park-and-Ride",
        types=["bus_station", "transit_station"],
        lat=29.6713,
        lng=-95.5284,
        dist_m=5.0,
    )

    outcome = _match_poi_class(
        cluster=cluster,
        topo=topo,
        target=target,
        pois=[bus_stop],
        anchors=[],
        semantic_lookup_source="cache_hit",
    )

    assert outcome.confidence < WAI_CONFIDENCE_THRESHOLD, (
        f"P16 violation: Head 4 alone produced confidence={outcome.confidence:.3f} "
        f"(must be < {WAI_CONFIDENCE_THRESHOLD}). Head 4 has been re-promoted "
        f"into the max() ensemble. Re-check _match_poi_class composition."
    )

    assert outcome.poi_type_match is True, (
        "P16 forensic: poi_type_match witness must remain True "
        "(witness signal preserved even when not contributing to score)."
    )
    assert outcome.poi_type_witness is not None, (
        "P16 forensic: poi_type_witness must be populated"
    )
    assert "bus_station" in outcome.poi_type_witness, (
        f"P16 forensic: witness string should name the matched type, "
        f"got {outcome.poi_type_witness!r}"
    )
