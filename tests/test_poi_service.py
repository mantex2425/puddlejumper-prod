"""Unit tests for poi_service.get_pois_near_cluster — Phase 2a cache-only path.

Mocking pattern follows production conventions: MagicMock cursor with
``fetchall.return_value`` set to dict rows (mirroring RealDictCursor output).
The cursor's ``execute`` calls are inspected to validate parameter binding
order and presence of the throttled UPDATE.

Coverage:
    1. Empty cache (cache miss) → empty list
    2. Single fresh row → single POI returned
    3. Single expired row → SQL filters it; empty list (test validates the
       SQL TTL guard semantically — the mock simulates the filtered result)
    4. Three rows, two share a place_id → Option C dedupe keeps the closer
       occurrence; result sorted by distance
    5. Negative-cache row (all-empty arrays) → empty list returned, BUT the
       last_hit_at UPDATE still fires (negative cache rows are useful and
       should stay warm in the heatmap)
    6. Boundary distance (cluster centroid 80m from cache row) → SQL accepts
       the row (ST_DWithin uses inclusive radius); test validates via mock
       that the radius parameter is passed correctly to ST_DWithin
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from poi_service import (
    DEFAULT_RADIUS_M,
    POI,
    TOUCH_THROTTLE_INTERVAL,
    get_pois_near_cluster,
)


# ─── HELPERS ─────────────────────────────────────────────────────────────────


def _make_cluster(median_lat: float = 29.5800, median_lng: float = -95.5500):
    """Build a Cluster-like object with the fields poi_service reads.

    Avoids importing the real Cluster dataclass to keep this test file
    isolated from cluster_detection's dependencies. The contract is
    duck-typed: poi_service only reads .median_lat and .median_lng.
    """
    cluster = MagicMock()
    cluster.median_lat = median_lat
    cluster.median_lng = median_lng
    return cluster


def _row(
    *,
    row_id: int,
    place_ids: list[str],
    names: list[str],
    types: list[str],
    dist_m: float,
) -> dict:
    """Build a RealDictCursor-style row dict matching poi_service's SELECT."""
    return {
        "id": row_id,
        "google_place_ids": place_ids,
        "business_names": names,
        "business_types": types,
        "dist_m": dist_m,
    }


def _executed_sqls(cur: MagicMock) -> list[str]:
    """Return the SQL string of every cur.execute() call, normalized."""
    return [call.args[0] for call in cur.execute.call_args_list]


# ─── TESTS ───────────────────────────────────────────────────────────────────


def test_empty_cache_returns_empty_list():
    """Cache miss → [] returned, no UPDATE issued (nothing to touch)."""
    cur = MagicMock()
    cur.fetchall.return_value = []

    result = get_pois_near_cluster(_make_cluster(), cur)

    assert result == []
    # Only the SELECT fired; no UPDATE because rows was empty.
    assert cur.execute.call_count == 1
    assert "SELECT" in _executed_sqls(cur)[0]


def test_single_fresh_row_returns_single_poi():
    """One in-range row with one POI → one POI in result, UPDATE fires."""
    cur = MagicMock()
    cur.fetchall.return_value = [
        _row(
            row_id=1,
            place_ids=["ChIJ_pepperonis"],
            names=["Pepperoni's"],
            types=["restaurant"],
            dist_m=14.7,
        )
    ]

    result = get_pois_near_cluster(_make_cluster(), cur)

    assert len(result) == 1
    assert result[0] == POI(
        place_id="ChIJ_pepperonis",
        name="Pepperoni's",
        business_type="restaurant",
        dist_m=14.7,
    )
    # SELECT plus UPDATE (last_hit_at touch).
    assert cur.execute.call_count == 2
    assert "UPDATE app_private.poi_cache" in _executed_sqls(cur)[1]
    # Throttle interval embedded in UPDATE per module constant.
    assert TOUCH_THROTTLE_INTERVAL in _executed_sqls(cur)[1]


def test_expired_row_filtered_by_sql_returns_empty():
    """Expired rows are filtered by the SQL TTL guard.

    The test simulates this by having fetchall return [] (the SQL's
    expires_at > NOW() AT TIME ZONE 'UTC' clause excluded the row).
    Validates the SELECT contains the TTL guard.
    """
    cur = MagicMock()
    cur.fetchall.return_value = []  # TTL guard filtered the expired row

    result = get_pois_near_cluster(_make_cluster(), cur)

    assert result == []
    select_sql = _executed_sqls(cur)[0]
    assert "expires_at > (NOW() AT TIME ZONE 'UTC')" in select_sql


def test_dedupe_keeps_closer_occurrence_of_shared_place_id():
    """Two rows reference the same place_id at different distances.

    Option C dedupe must keep the closer occurrence. The strip mall got
    queried twice from different cluster centroids; both cache rows landed
    in range of THIS cluster's lookup. We want the one whose query point
    was closer.
    """
    cur = MagicMock()
    cur.fetchall.return_value = [
        _row(  # Closer cache row, has Pepperoni's
            row_id=1,
            place_ids=["ChIJ_pepperonis", "ChIJ_shell"],
            names=["Pepperoni's", "Shell"],
            types=["restaurant", "gas_station"],
            dist_m=12.0,
        ),
        _row(  # Farther cache row, ALSO has Pepperoni's plus a unique POI
            row_id=2,
            place_ids=["ChIJ_pepperonis", "ChIJ_subway"],
            names=["Pepperoni's", "Subway"],
            types=["restaurant", "restaurant"],
            dist_m=45.0,
        ),
    ]

    result = get_pois_near_cluster(_make_cluster(), cur)

    # Three unique place_ids: pepperonis, shell, subway.
    assert len(result) == 3

    by_id = {poi.place_id: poi for poi in result}

    # Pepperoni's came from the closer row (12.0m), not the farther (45.0m).
    assert by_id["ChIJ_pepperonis"].dist_m == 12.0
    # Shell only appeared in the closer row.
    assert by_id["ChIJ_shell"].dist_m == 12.0
    # Subway only appeared in the farther row.
    assert by_id["ChIJ_subway"].dist_m == 45.0

    # Result is sorted ascending by distance: pepperonis & shell tie at 12.0,
    # subway at 45.0 trails. The two 12.0 entries can be in either order;
    # subway must be last.
    assert result[-1].place_id == "ChIJ_subway"
    assert {result[0].dist_m, result[1].dist_m} == {12.0, 12.0}


def test_negative_cache_row_returns_empty_but_touches_last_hit_at():
    """All-empty arrays → empty result, BUT UPDATE still fires.

    Negative cache rows record "Google was queried here, returned nothing"
    and PREVENT redundant API calls in 2b. They are useful and should stay
    warm in the Living Heatmap. last_hit_at must update even when the row
    contributes zero POIs.
    """
    cur = MagicMock()
    cur.fetchall.return_value = [
        _row(
            row_id=42,
            place_ids=[],
            names=[],
            types=[],
            dist_m=18.0,
        )
    ]

    result = get_pois_near_cluster(_make_cluster(), cur)

    assert result == []
    # Critical: SELECT plus UPDATE both fired — the row was touched even
    # though it produced no POIs. The empty arrays loop zero times in the
    # dedupe logic but the UPDATE is unconditional on rows being non-empty.
    assert cur.execute.call_count == 2
    update_sql = _executed_sqls(cur)[1]
    assert "UPDATE app_private.poi_cache" in update_sql
    # The UPDATE binds the row id 42 in its parameter tuple.
    update_params = cur.execute.call_args_list[1].args[1]
    assert 42 in update_params[0]


def test_radius_parameter_bound_to_st_dwithin():
    """Custom radius_m flows to the ST_DWithin call in SELECT.

    Validates the parameter binding contract: cluster centroid (lat, lng)
    appears twice (once for ST_Distance, once for ST_DWithin), then radius.
    """
    cur = MagicMock()
    cur.fetchall.return_value = []

    cluster = _make_cluster(median_lat=29.5800, median_lng=-95.5500)
    get_pois_near_cluster(cluster, cur, radius_m=120)

    select_call = cur.execute.call_args_list[0]
    select_params = select_call.args[1]
    # Order: lat, lng (ST_Distance), lat, lng (ST_DWithin), radius_m.
    assert select_params == (29.5800, -95.5500, 29.5800, -95.5500, 120)


def test_default_radius_is_80m_per_proposal_R3():
    """No explicit radius → DEFAULT_RADIUS_M used. Locked at 80m per R3."""
    assert DEFAULT_RADIUS_M == 80

    cur = MagicMock()
    cur.fetchall.return_value = []

    get_pois_near_cluster(_make_cluster(), cur)

    select_params = cur.execute.call_args_list[0].args[1]
    # Last bound parameter is the radius, defaulting to 80.
    assert select_params[-1] == 80