"""Unit tests for poi_service — Phase 2b (JSONB cache + Google Places v1).

Mocking pattern follows production conventions: MagicMock cursor with
``fetchall.return_value`` set to dict rows (mirroring RealDictCursor output).
The cursor's ``execute`` calls are inspected to validate parameter binding,
SQL shape (regression guards on canonical functions and community-cache
invariants), and presence of the throttled UPDATE on cache hits.

The Google Places v1 API path is exercised by monkeypatching
``poi_service.requests.post`` and ``poi_service.os.environ`` per test —
no real network calls.

Coverage:
    Cache-hit path (8):
      1. Single-place cache hit returns one POI, source='cache_hit'
      2. Negative-cache row only — pois=[], source='cache_hit', no API call
      3. last_hit_at UPDATE fires with throttle interval embedded
      4. Option C dedupe keeps closer occurrence of shared place_id
      5. Default radius is 80m per OPERATION_STRIP_MALL_PROPOSAL R3
      6. Custom radius_m passed to ST_DWithin
      7. SELECT uses canonical distance_miles + coords_to_geography
      8. Result list sorted ASC by per-place dist_m

    Cache-miss → API path (6):
      9. Complete miss → 200 OK with results → INSERT fires, source='api_call'
      10. Complete miss → 200 OK with empty places → negative-cache INSERT,
          source='api_call', pois=[]
      11. Complete miss → requests.RequestException → source='api_error',
          no INSERT
      12. Complete miss → non-200 status → source='api_error', no INSERT
      13. Complete miss → malformed JSON response → source='api_error'
      14. Complete miss → API key missing from env → source='api_error',
          no requests.post call

    API request shape (1):
      15. Request body has 50m radius; header contains X-Goog-FieldMask
          and X-Goog-Api-Key per iron-fist mandate

    Community-cache invariants (2 — regression guards):
      16. Cache-read SELECT contains no driver_id filter
      17. Cache-write INSERT contains no driver_id column
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
import requests

from poi_service import (
    API_SEARCH_RADIUS_M,
    CACHE_TTL_DAYS,
    DEFAULT_RADIUS_M,
    PLACES_V1_FIELD_MASK,
    PLACES_V1_URL,
    POI,
    POILookupResult,
    TOUCH_THROTTLE_INTERVAL,
    get_pois_near_cluster,
)


# ─── HELPERS ─────────────────────────────────────────────────────────────────


def _make_cluster(median_lat: float = 29.5800, median_lng: float = -95.5500):
    """Build a Cluster-like object with the fields poi_service reads.

    Avoids importing the real Cluster dataclass to keep this test file
    isolated from cluster_detection's dependencies. Contract is duck-typed:
    poi_service only reads .median_lat and .median_lng.
    """
    cluster = MagicMock()
    cluster.median_lat = median_lat
    cluster.median_lng = median_lng
    return cluster


def _row(
    *,
    cache_id: int,
    place_id: str,
    name: str,
    types: list[str],
    poi_lat: float,
    poi_lng: float,
    dist_m: float,
) -> dict:
    """Build a flat per-place row matching the LATERAL unnest SELECT output."""
    return {
        "cache_id": cache_id,
        "place_id": place_id,
        "name": name,
        "types": types,
        "poi_lat": poi_lat,
        "poi_lng": poi_lng,
        "dist_m": dist_m,
    }


def _negative_row(cache_id: int) -> dict:
    """A LEFT JOIN LATERAL row from a cache row whose places jsonb is [].

    Postgres yields one row with NULL on every place-derived column for a
    cache row whose places array is empty. poi_service treats these as
    negative-cache sentinels and skips them during POI construction while
    still touching last_hit_at on the cache row.
    """
    return {
        "cache_id": cache_id,
        "place_id": None,
        "name": None,
        "types": None,
        "poi_lat": None,
        "poi_lng": None,
        "dist_m": None,
    }


def _executed_sqls(cur: MagicMock) -> list[str]:
    """Return the SQL string of every cur.execute() call."""
    return [call.args[0] for call in cur.execute.call_args_list]


def _mock_v1_response(places: list[dict], status_code: int = 200) -> MagicMock:
    """Build a requests.Response-like mock for the v1 API."""
    mock = MagicMock()
    mock.status_code = status_code
    mock.json.return_value = {"places": places}
    mock.text = json.dumps({"places": places})
    return mock


def _v1_place(
    place_id: str,
    name: str,
    types: list[str],
    lat: float,
    lng: float,
) -> dict:
    """Build a v1 places[i] dict matching the searchNearby field-mask shape."""
    return {
        "id": place_id,
        "displayName": {"text": name, "languageCode": "en"},
        "types": types,
        "location": {"latitude": lat, "longitude": lng},
    }


@pytest.fixture
def with_api_key(monkeypatch):
    """Set the API key env var for tests that exercise the API path."""
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "test_key_for_unit_tests")
    return "test_key_for_unit_tests"


# ─── CACHE-HIT PATH ──────────────────────────────────────────────────────────


def test_cache_hit_single_place_returns_one_poi_source_cache_hit(with_api_key):
    """One in-range cache row with one POI → POILookupResult(1 POI, 'cache_hit')."""
    cur = MagicMock()
    cur.fetchall.return_value = [
        _row(
            cache_id=1,
            place_id="ChIJ_pepperonis",
            name="Pepperoni's",
            types=["restaurant", "point_of_interest", "establishment"],
            poi_lat=29.5801,
            poi_lng=-95.5501,
            dist_m=14.7,
        )
    ]

    result = get_pois_near_cluster(_make_cluster(), cur)

    assert isinstance(result, POILookupResult)
    assert result.source == "cache_hit"
    assert len(result.pois) == 1
    assert result.pois[0] == POI(
        place_id="ChIJ_pepperonis",
        name="Pepperoni's",
        types=["restaurant", "point_of_interest", "establishment"],
        lat=29.5801,
        lng=-95.5501,
        dist_m=14.7,
    )
    # SELECT + UPDATE; no API call.
    assert cur.execute.call_count == 2


def test_cache_hit_negative_cache_only_returns_empty_pois_source_cache_hit(
    with_api_key,
):
    """Negative-cache row only → pois=[] but source='cache_hit'.

    The Negative Cache Dividend: the cache says 'no businesses here'
    so we do NOT call Google again. We DO touch last_hit_at to keep
    the negative cache warm in the Living Heatmap.
    """
    cur = MagicMock()
    cur.fetchall.return_value = [_negative_row(cache_id=42)]

    result = get_pois_near_cluster(_make_cluster(), cur)

    assert result.source == "cache_hit"
    assert result.pois == []
    # Critical: SELECT + UPDATE both fired (cache row touched), no API call.
    assert cur.execute.call_count == 2
    update_sql = _executed_sqls(cur)[1]
    assert "UPDATE app_private.poi_cache" in update_sql
    update_params = cur.execute.call_args_list[1].args[1]
    assert 42 in update_params[0]


def test_cache_hit_touches_last_hit_at_with_throttle_interval(with_api_key):
    """The UPDATE embeds the module-level TOUCH_THROTTLE_INTERVAL constant."""
    cur = MagicMock()
    cur.fetchall.return_value = [
        _row(
            cache_id=1,
            place_id="ChIJ_test",
            name="Test",
            types=["restaurant"],
            poi_lat=29.58,
            poi_lng=-95.55,
            dist_m=10.0,
        )
    ]

    get_pois_near_cluster(_make_cluster(), cur)

    update_sql = _executed_sqls(cur)[1]
    assert "UPDATE app_private.poi_cache" in update_sql
    assert "last_hit_at" in update_sql
    assert TOUCH_THROTTLE_INTERVAL in update_sql


def test_cache_hit_dedupe_keeps_closer_occurrence_of_shared_place_id(
    with_api_key,
):
    """Two cache rows reference the same place_id at different distances.

    Option C dedupe must keep the closer occurrence. A strip mall hit by
    two different drivers' clusters might produce overlapping cache rows;
    we collapse to one entry per place_id by the smallest dist_m.
    """
    cur = MagicMock()
    cur.fetchall.return_value = [
        _row(  # closer cache row, has Pepperoni's
            cache_id=1,
            place_id="ChIJ_pepperonis",
            name="Pepperoni's",
            types=["restaurant"],
            poi_lat=29.58,
            poi_lng=-95.55,
            dist_m=12.0,
        ),
        _row(  # closer cache row, has Shell
            cache_id=1,
            place_id="ChIJ_shell",
            name="Shell",
            types=["gas_station"],
            poi_lat=29.5805,
            poi_lng=-95.5505,
            dist_m=12.0,
        ),
        _row(  # farther cache row, ALSO has Pepperoni's
            cache_id=2,
            place_id="ChIJ_pepperonis",
            name="Pepperoni's",
            types=["restaurant"],
            poi_lat=29.58,
            poi_lng=-95.55,
            dist_m=45.0,
        ),
        _row(  # farther cache row, has unique Subway
            cache_id=2,
            place_id="ChIJ_subway",
            name="Subway",
            types=["restaurant"],
            poi_lat=29.5810,
            poi_lng=-95.5510,
            dist_m=45.0,
        ),
    ]

    result = get_pois_near_cluster(_make_cluster(), cur)

    by_id = {poi.place_id: poi for poi in result.pois}
    assert len(result.pois) == 3
    assert by_id["ChIJ_pepperonis"].dist_m == 12.0  # closer occurrence won
    assert by_id["ChIJ_shell"].dist_m == 12.0
    assert by_id["ChIJ_subway"].dist_m == 45.0


def test_cache_lookup_default_radius_is_80m_per_proposal_R3(with_api_key):
    """No explicit radius → DEFAULT_RADIUS_M (80) bound to ST_DWithin."""
    assert DEFAULT_RADIUS_M == 80

    cur = MagicMock()
    cur.fetchall.return_value = [_negative_row(cache_id=1)]

    get_pois_near_cluster(_make_cluster(), cur)

    select_params = cur.execute.call_args_list[0].args[1]
    # Last bound parameter is the ST_DWithin radius.
    assert select_params[-1] == 80


def test_cache_lookup_custom_radius_passed_to_st_dwithin(with_api_key):
    """Custom radius_m flows through to the ST_DWithin parameter slot."""
    cur = MagicMock()
    cur.fetchall.return_value = [_negative_row(cache_id=1)]

    get_pois_near_cluster(_make_cluster(), cur, radius_m=120)

    select_params = cur.execute.call_args_list[0].args[1]
    assert select_params[-1] == 120


def test_cache_lookup_uses_canonical_geometry_functions(with_api_key):
    """SELECT uses app_private.distance_miles + coords_to_geography (Std §II)."""
    cur = MagicMock()
    cur.fetchall.return_value = []  # cache miss; we only inspect the SELECT

    # Set the API key and stub out requests.post to avoid hitting the wire
    # on this cache-miss path; we don't care about the API result here.
    import poi_service

    original_post = poi_service.requests.post
    poi_service.requests.post = lambda *a, **kw: _mock_v1_response([])
    try:
        get_pois_near_cluster(_make_cluster(), cur)
    finally:
        poi_service.requests.post = original_post

    select_sql = _executed_sqls(cur)[0]
    assert "app_private.distance_miles" in select_sql
    assert "app_private.coords_to_geography" in select_sql
    # And explicitly NOT the blacklisted raw GIS calls.
    assert "ST_MakePoint" not in select_sql


def test_cache_hit_results_sorted_ascending_by_per_place_dist_m(with_api_key):
    """Returned POI list is sorted ASC by dist_m."""
    cur = MagicMock()
    # Mock the cursor to return rows in non-sorted order (Postgres ORDER BY
    # would have sorted them, but defense-in-depth: Python sort is the
    # final guarantee the matcher relies on).
    cur.fetchall.return_value = [
        _row(
            cache_id=1, place_id="ChIJ_far", name="Far",
            types=["restaurant"], poi_lat=29.59, poi_lng=-95.56, dist_m=70.0,
        ),
        _row(
            cache_id=1, place_id="ChIJ_near", name="Near",
            types=["restaurant"], poi_lat=29.58, poi_lng=-95.55, dist_m=10.0,
        ),
        _row(
            cache_id=1, place_id="ChIJ_mid", name="Mid",
            types=["restaurant"], poi_lat=29.585, poi_lng=-95.555, dist_m=40.0,
        ),
    ]

    result = get_pois_near_cluster(_make_cluster(), cur)

    distances = [poi.dist_m for poi in result.pois]
    assert distances == sorted(distances)
    assert distances == [10.0, 40.0, 70.0]


# ─── CACHE-MISS → API PATH ───────────────────────────────────────────────────


def test_cache_miss_api_success_writes_row_returns_api_call(
    monkeypatch, with_api_key
):
    """Complete miss → 200 OK with 3 places → INSERT fires, pois has 3."""
    cur = MagicMock()
    cur.fetchall.return_value = []  # complete cache miss

    posted_url: list[str] = []
    posted_body: list[dict] = []
    posted_headers: list[dict] = []

    def fake_post(url, json=None, headers=None, timeout=None):
        posted_url.append(url)
        posted_body.append(json)
        posted_headers.append(headers)
        return _mock_v1_response(
            [
                _v1_place("ChIJ_shell", "Shell", ["gas_station"], 29.5103, -95.5271),
                _v1_place(
                    "ChIJ_stomps", "Stomp's Burger Joint",
                    ["restaurant"], 29.5104, -95.5272,
                ),
                _v1_place(
                    "ChIJ_excel", "Missouri City Dentist - Excel Dental",
                    ["dentist"], 29.5105, -95.5273,
                ),
            ]
        )

    monkeypatch.setattr("poi_service.requests.post", fake_post)

    result = get_pois_near_cluster(
        _make_cluster(median_lat=29.510316, median_lng=-95.527151), cur
    )

    assert result.source == "api_call"
    assert len(result.pois) == 3
    place_ids = {poi.place_id for poi in result.pois}
    assert place_ids == {"ChIJ_shell", "ChIJ_stomps", "ChIJ_excel"}
    # Excel Dental must be in there with its full name.
    by_id = {poi.place_id: poi for poi in result.pois}
    assert by_id["ChIJ_excel"].name == "Missouri City Dentist - Excel Dental"
    assert "dentist" in by_id["ChIJ_excel"].types

    # API was called exactly once with the v1 endpoint.
    assert len(posted_url) == 1
    assert posted_url[0] == PLACES_V1_URL

    # SELECT + INSERT both fired on the cursor (UPDATE skipped — cache miss
    # has nothing to touch).
    sqls = _executed_sqls(cur)
    assert any("SELECT" in s for s in sqls)
    assert any("INSERT INTO app_private.poi_cache" in s for s in sqls)


def test_cache_miss_api_zero_results_writes_negative_cache_row(
    monkeypatch, with_api_key
):
    """API returns 200 OK with empty places → negative cache INSERT."""
    cur = MagicMock()
    cur.fetchall.return_value = []  # complete miss

    monkeypatch.setattr(
        "poi_service.requests.post",
        lambda *a, **kw: _mock_v1_response([]),
    )

    result = get_pois_near_cluster(_make_cluster(), cur)

    assert result.source == "api_call"
    assert result.pois == []

    # INSERT was called with an empty JSON array for the places column.
    insert_calls = [
        call for call in cur.execute.call_args_list
        if "INSERT INTO app_private.poi_cache" in call.args[0]
    ]
    assert len(insert_calls) == 1
    insert_params = insert_calls[0].args[1]
    # Param order: lat, lng, places_json
    assert json.loads(insert_params[2]) == []


def test_cache_miss_api_request_exception_returns_api_error_no_write(
    monkeypatch, with_api_key
):
    """Network failure → source='api_error', no cache write."""
    cur = MagicMock()
    cur.fetchall.return_value = []

    def boom(*a, **kw):
        raise requests.exceptions.Timeout("simulated timeout")

    monkeypatch.setattr("poi_service.requests.post", boom)

    result = get_pois_near_cluster(_make_cluster(), cur)

    assert result.source == "api_error"
    assert result.pois == []
    # No INSERT — fail-closed.
    assert not any(
        "INSERT INTO app_private.poi_cache" in s
        for s in _executed_sqls(cur)
    )


def test_cache_miss_api_500_returns_api_error_no_write(
    monkeypatch, with_api_key
):
    """Non-200 response → source='api_error', no cache write."""
    cur = MagicMock()
    cur.fetchall.return_value = []

    monkeypatch.setattr(
        "poi_service.requests.post",
        lambda *a, **kw: _mock_v1_response([], status_code=500),
    )

    result = get_pois_near_cluster(_make_cluster(), cur)

    assert result.source == "api_error"
    assert result.pois == []
    assert not any(
        "INSERT INTO app_private.poi_cache" in s
        for s in _executed_sqls(cur)
    )


def test_cache_miss_api_malformed_json_returns_api_error(
    monkeypatch, with_api_key
):
    """response.json() raises ValueError → source='api_error'."""
    cur = MagicMock()
    cur.fetchall.return_value = []

    bad_response = MagicMock()
    bad_response.status_code = 200
    bad_response.json.side_effect = ValueError("malformed JSON")
    bad_response.text = "<html>not json</html>"

    monkeypatch.setattr(
        "poi_service.requests.post", lambda *a, **kw: bad_response
    )

    result = get_pois_near_cluster(_make_cluster(), cur)

    assert result.source == "api_error"
    assert result.pois == []


def test_cache_miss_api_key_missing_returns_api_error_no_post_call(monkeypatch):
    """Env var unset → source='api_error', requests.post never called.

    No with_api_key fixture here — env var is intentionally missing.
    """
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)

    cur = MagicMock()
    cur.fetchall.return_value = []

    posted: list = []
    monkeypatch.setattr(
        "poi_service.requests.post",
        lambda *a, **kw: posted.append((a, kw)) or _mock_v1_response([]),
    )

    result = get_pois_near_cluster(_make_cluster(), cur)

    assert result.source == "api_error"
    assert result.pois == []
    # Critical: requests.post was NOT called — we shorted out before the wire.
    assert posted == []


# ─── API REQUEST SHAPE ───────────────────────────────────────────────────────


def test_api_call_uses_50m_radius_and_field_mask_and_api_key(
    monkeypatch, with_api_key
):
    """The POST body has 50m radius; headers carry the field mask + API key."""
    cur = MagicMock()
    cur.fetchall.return_value = []

    captured: dict = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        captured["timeout"] = timeout
        return _mock_v1_response([])

    monkeypatch.setattr("poi_service.requests.post", fake_post)

    get_pois_near_cluster(
        _make_cluster(median_lat=29.510316, median_lng=-95.527151), cur
    )

    # Endpoint
    assert captured["url"] == PLACES_V1_URL

    # Body shape: 50m radius at the cluster centroid.
    body = captured["json"]
    assert body["locationRestriction"]["circle"]["radius"] == API_SEARCH_RADIUS_M
    assert API_SEARCH_RADIUS_M == 50.0
    assert body["locationRestriction"]["circle"]["center"] == {
        "latitude": 29.510316,
        "longitude": -95.527151,
    }

    # Headers carry the field mask and API key.
    headers = captured["headers"]
    assert headers["X-Goog-FieldMask"] == PLACES_V1_FIELD_MASK
    assert headers["X-Goog-Api-Key"] == "test_key_for_unit_tests"
    assert headers["Content-Type"] == "application/json"


# ─── COMMUNITY-CACHE INVARIANTS (REGRESSION GUARDS) ──────────────────────────


def test_cache_lookup_sql_has_no_driver_id_filter(with_api_key, monkeypatch):
    """The cache lookup is fleet-shared by coordinate, not per-driver.

    Operation Strip Mall thesis: one driver pays the Google tax, every
    driver in 80m for the next 30 days reads it free. Adding a driver_id
    filter would silently break the community-cache economics. This test
    locks the invariant in code.
    """
    cur = MagicMock()
    cur.fetchall.return_value = []
    monkeypatch.setattr(
        "poi_service.requests.post", lambda *a, **kw: _mock_v1_response([])
    )

    get_pois_near_cluster(_make_cluster(), cur)

    select_sql = _executed_sqls(cur)[0]
    assert "driver_id" not in select_sql.lower()


def test_cache_write_sql_has_no_driver_id_column(monkeypatch, with_api_key):
    """The cache write attributes no driver — same community-cache invariant."""
    cur = MagicMock()
    cur.fetchall.return_value = []
    monkeypatch.setattr(
        "poi_service.requests.post",
        lambda *a, **kw: _mock_v1_response(
            [_v1_place("ChIJ_x", "X", ["restaurant"], 29.58, -95.55)]
        ),
    )

    get_pois_near_cluster(_make_cluster(), cur)

    insert_sqls = [
        s for s in _executed_sqls(cur)
        if "INSERT INTO app_private.poi_cache" in s
    ]
    assert len(insert_sqls) == 1
    assert "driver_id" not in insert_sqls[0].lower()


# ─── BONUS: CACHE-WRITE TTL CONTRACT ─────────────────────────────────────────


def test_cache_write_uses_30_day_ttl_per_proposal_R5(monkeypatch, with_api_key):
    """The INSERT embeds the 30-day TTL constant from CACHE_TTL_DAYS."""
    assert CACHE_TTL_DAYS == 30

    cur = MagicMock()
    cur.fetchall.return_value = []
    monkeypatch.setattr(
        "poi_service.requests.post", lambda *a, **kw: _mock_v1_response([])
    )

    get_pois_near_cluster(_make_cluster(), cur)

    insert_sql = next(
        s for s in _executed_sqls(cur)
        if "INSERT INTO app_private.poi_cache" in s
    )
    # The TTL is interpolated into the SQL string at module-eval time.
    assert "INTERVAL '30 days'" in insert_sql
    assert "(NOW() AT TIME ZONE 'UTC')" in insert_sql


# ─── CALL CONTRACT REGRESSION (Resurrection 2026-05-09) ──────────────────────
#
# Tests 18-19 lock in the call signature of get_pois_near_cluster against
# the argument-swap bug discovered 2026-05-09. The function signature is
# (cluster, cur, *, radius_m=...) — passing them in reverse order silently
# produced an AttributeError swallowed by where_am_i.py's try/except,
# leaving 69,944 PUDO contexts in 7 days without POI witnesses.
#
# These tests deliberately import the REAL Cluster dataclass rather than
# using the file's _make_cluster() MagicMock fixture. A MagicMock returns
# Mock objects for any attribute access (including .execute()), so a
# MagicMock-based test would NOT catch the swap. The frozen Cluster has
# no .execute attribute, which is exactly what makes the swap detectable.

from datetime import datetime, timezone

from cluster_detection import Cluster


def _real_cluster(median_lat: float = 29.5800, median_lng: float = -95.5500) -> Cluster:
    """Construct a real Cluster dataclass for contract testing.

    Required to detect the (cur, cluster) argument swap -- see comment
    block above. Do NOT replace with _make_cluster() (MagicMock).

    Fields per cluster_detection.py:42 (frozen dataclass, 6 fields):
      n, median_lat, median_lng, spread_m, duration_s, latest
    """
    return Cluster(
        n=5,
        median_lat=median_lat,
        median_lng=median_lng,
        spread_m=10.0,
        duration_s=60.0,
        latest=datetime.now(timezone.utc),
    )


def test_call_contract_correct_order_does_not_raise(with_api_key):
    """Calling get_pois_near_cluster(cluster, cur) — the documented order —
    must not raise. Cache miss + API error path returns POILookupResult
    without raising; that's sufficient to exercise argument unpacking."""
    cluster = _real_cluster()
    cur = MagicMock()
    cur.fetchall.return_value = []  # cache miss

    # API path will fail because no monkeypatch is set; that's fine —
    # we're testing the call-contract, not the API. A successful call
    # contract returns POILookupResult(source='api_error') without raising.
    result = get_pois_near_cluster(cluster, cur)

    assert result is not None
    assert hasattr(result, "source")
    assert result.source in ("cache_hit", "api_call", "api_error")


def test_call_contract_swapped_order_raises():
    """Calling get_pois_near_cluster(cur, cluster) — the buggy swapped
    order from the 2026-05-09 resurrection — MUST raise. This is the
    direct regression coverage for the bug.

    The frozen Cluster dataclass has no .execute attribute, so when
    _read_cache tries to call cluster.execute(...), AttributeError fires.
    If this test ever passes silently, someone has broken the regression
    coverage and the swap can recur undetected in production.
    """
    cluster = _real_cluster()
    cur = MagicMock()

    with pytest.raises(AttributeError):
        # Deliberately swapped — this is what where_am_i.py:1630 was
        # doing before the resurrection fix. The frozen Cluster lacks
        # .execute, so _read_cache's first cur.execute(...) call fails.
        get_pois_near_cluster(cur, cluster)
