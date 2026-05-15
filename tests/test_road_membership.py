"""road_membership tests — Google reverse geocode + H3 cache layer.

Coverage (10 tests):

Live PG (5) — exercises the actual app_private.coords_to_h3 call and
the road_membership_cache table I/O. Google call is mocked so tests
don't hit the network or consume quota.

  1. Ride 1 cluster + "Watts Plantation Dr" + simulated probe response
     (Henry Watts + 5× Watts Plantation Road) -> True; cache row written
     with on_road=True, google_route_canonical='Watts Plantation Road'.
  2. Cache hit: pre-insert a row, call is_cluster_on_road, assert
     Google was NOT called and verdict matches cached value.
  3. Disambiguation: same probe response, target "Henry Watts Dr" ->
     True via Henry Watts (NOT Watts Plantation).
  4. Negative match: probe response with no matching route -> False,
     row written with on_road=False (cache the negative verdict too).
  5. Short-name path (closes directional gap): Google response has
     long_name="North Shepherd Drive", short_name="N Shepherd Dr";
     target "N Shepherd Dr" -> True via short_name comparison.

Pure-Python (5) — logic-only per §XIV.J. MagicMock acceptable.

  6. _canonicalize_road_text truth table.
  7. _extract_route_candidates skips plus codes / localities.
  8. _pick_matching_route first-wins semantics.
  9. is_cluster_on_road(None, cluster, "...") -> False, no Google call.
 10. is_cluster_on_road(cur, cluster, "") -> False, no Google call.
"""
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

from road_membership import (
    is_cluster_on_road,
    _canonicalize_road_text,
    _extract_route_candidates,
    _pick_matching_route,
    ROAD_MEMBERSHIP_H3_RESOLUTION,
)


# Ride 1 forensic anchor (apartment arrest cluster).
RIDE_1_LAT = 29.525473
RIDE_1_LNG = -95.528919


def _cluster(lat: float, lng: float):
    """Minimal Cluster stand-in: only .median_lat / .median_lng read."""
    return SimpleNamespace(median_lat=lat, median_lng=lng)


# Synthetic Google response mirroring the actual probe output (2026-05-15).
# Henry Watts Rd appears as the closest street_address; Watts Plantation
# Road appears multiple times with different place_ids. A real production
# call would return more variants — this captures the disambiguation case.
PROBE_RESPONSE_RIDE_1 = {
    "status": "OK",
    "results": [
        {
            "place_id": "ChIJCwaQva_vQIYRJ40BFKp4gew",
            "types": ["street_address"],
            "formatted_address":
                "4800 Henry Watts Rd, Missouri City, TX 77459, USA",
            "address_components": [
                {"long_name": "4800", "short_name": "4800",
                 "types": ["street_number"]},
                {"long_name": "Henry Watts Rd",
                 "short_name": "Henry Watts Rd",
                 "types": ["route"]},
                {"long_name": "Missouri City",
                 "short_name": "Missouri City",
                 "types": ["locality", "political"]},
            ],
        },
        {
            "place_id": "ChIJL_ecC5jvQIYRtk_N-VKLJrQ",
            "types": ["establishment", "point_of_interest"],
            "formatted_address":
                "4800 Watts Plantation Rd, Missouri City, TX 77459, USA",
            "address_components": [
                {"long_name": "4800", "short_name": "4800",
                 "types": ["street_number"]},
                {"long_name": "Watts Plantation Road",
                 "short_name": "Watts Plantation Rd",
                 "types": ["route"]},
            ],
        },
        {
            "place_id": "ChIJXaqc6q_vQIYRno63g0aPl6M",
            "types": ["route"],
            "formatted_address":
                "4823-4813 Watts Plantation Rd, Missouri City, TX 77459, USA",
            "address_components": [
                {"long_name": "Watts Plantation Road",
                 "short_name": "Watts Plantation Rd",
                 "types": ["route"]},
            ],
        },
        # Plus code — no route component, must be ignored.
        {
            "place_id": "GhIJVe4FZoWGPUAR6s4Tz9nhV8A",
            "types": ["plus_code"],
            "formatted_address": "GFGC+5C Missouri City, TX, USA",
            "address_components": [
                {"long_name": "GFGC+5C", "short_name": "GFGC+5C",
                 "types": ["plus_code"]},
            ],
        },
    ],
}


# Synthetic Google response with directional prefix mismatch between
# long_name and short_name — exercises the short_name match path.
DIRECTIONAL_RESPONSE = {
    "status": "OK",
    "results": [
        {
            "place_id": "ChIJDirectionalShepherd",
            "types": ["street_address"],
            "formatted_address":
                "1234 N Shepherd Dr, Houston, TX 77008, USA",
            "address_components": [
                {"long_name": "1234", "short_name": "1234",
                 "types": ["street_number"]},
                {"long_name": "North Shepherd Drive",
                 "short_name": "N Shepherd Dr",
                 "types": ["route"]},
            ],
        },
    ],
}


# Synthetic response where no route matches the target. Single result,
# road called "Calhoun Road"; offer asks about "Westheimer Road".
NO_MATCH_RESPONSE = {
    "status": "OK",
    "results": [
        {
            "place_id": "ChIJCalhoun",
            "types": ["street_address"],
            "formatted_address":
                "1234 Calhoun Rd, Houston, TX 77004, USA",
            "address_components": [
                {"long_name": "1234", "short_name": "1234",
                 "types": ["street_number"]},
                {"long_name": "Calhoun Road",
                 "short_name": "Calhoun Rd",
                 "types": ["route"]},
            ],
        },
    ],
}


# ============================================================================
# Live-PG tests (§XIV.J): exercise app_private.coords_to_h3 + cache I/O
# ============================================================================

class TestRoadMembershipLivePG:
    """Real-cursor tests. Cache I/O and the canonical H3 function are
    exercised against production schema; Google call is mocked so we
    don't burn quota or depend on network in CI."""

    def test_ride_1_watts_plantation_caches_true_verdict(self, db_cur):
        """The regression pin: ride 1 cluster + offer 'Watts Plantation Dr'
        + the actual probe response → True, cache row created with the
        right canonical, the right Google form, and on_road=True.
        """
        cluster = _cluster(RIDE_1_LAT, RIDE_1_LNG)

        with patch("road_membership._get_api_key", return_value="test-key"), \
             patch("road_membership._reverse_geocode",
                   return_value=PROBE_RESPONSE_RIDE_1) as mock_geocode:
            result = is_cluster_on_road(
                db_cur, cluster, "Watts Plantation Dr",
            )

        assert result is True, (
            "Ride 1 regression: expected True for cluster on "
            "Watts Plantation Dr."
        )
        assert mock_geocode.call_count == 1, (
            "Expected exactly one Google call on cache miss."
        )

        # Verify the cache row landed correctly.
        db_cur.execute("""
            SELECT
              on_road,
              target_road_canonical,
              target_road_raw,
              google_route_canonical,
              google_place_id,
              cluster_lat,
              cluster_lng
            FROM app_private.road_membership_cache
            WHERE target_road_canonical = %s
        """, ("watts plantation",))
        row = db_cur.fetchone()
        assert row is not None, "Cache row was not written."
        assert row["on_road"] is True
        assert row["target_road_canonical"] == "watts plantation"
        assert row["target_road_raw"] == "Watts Plantation Dr"
        assert row["google_route_canonical"] == "Watts Plantation Road"
        assert row["google_place_id"] == "ChIJL_ecC5jvQIYRtk_N-VKLJrQ"
        assert abs(row["cluster_lat"] - RIDE_1_LAT) < 1e-9
        assert abs(row["cluster_lng"] - RIDE_1_LNG) < 1e-9

    def test_cache_hit_skips_google_call(self, db_cur):
        """Pre-insert a cache row, call is_cluster_on_road, assert
        Google was NOT consulted. This is the whole point of the cache —
        once verified, never re-pay."""
        cluster = _cluster(RIDE_1_LAT, RIDE_1_LNG)

        # Compute the H3 cell the resolver will use, pre-insert a row.
        db_cur.execute(
            "SELECT app_private.coords_to_h3(%s, %s, %s) AS h3",
            (RIDE_1_LAT, RIDE_1_LNG, ROAD_MEMBERSHIP_H3_RESOLUTION),
        )
        h3 = db_cur.fetchone()["h3"]
        assert h3 is not None

        db_cur.execute("""
            INSERT INTO app_private.road_membership_cache (
                cluster_h3, target_road_canonical, on_road,
                google_route_canonical, google_place_id,
                cluster_lat, cluster_lng, target_road_raw,
                cached_at, expires_at
            )
            VALUES (
                %s, %s, TRUE,
                'Pre-Inserted Road', 'pre-place-id',
                %s, %s, 'Pre-Inserted Dr',
                (NOW() AT TIME ZONE 'UTC'),
                (NOW() AT TIME ZONE 'UTC') + INTERVAL '365 days'
            )
        """, (h3, "watts plantation", RIDE_1_LAT, RIDE_1_LNG))

        with patch("road_membership._get_api_key",
                   return_value="test-key") as mock_key, \
             patch("road_membership._reverse_geocode") as mock_geocode:
            result = is_cluster_on_road(
                db_cur, cluster, "Watts Plantation Dr",
            )

        assert result is True, "Cache hit verdict should match cached value."
        mock_geocode.assert_not_called()
        # _get_api_key is only consulted on cache miss; assert not called
        # confirms the cache short-circuited before the Google branch.
        mock_key.assert_not_called()

    def test_disambiguation_henry_watts_target(self, db_cur):
        """Same Google response, different offer text: 'Henry Watts Dr'.
        Resolver must pick Henry Watts (NOT Watts Plantation) via
        base-name comparison. This pins the disambiguation that the
        probe showed working."""
        cluster = _cluster(RIDE_1_LAT, RIDE_1_LNG)

        with patch("road_membership._get_api_key", return_value="test-key"), \
             patch("road_membership._reverse_geocode",
                   return_value=PROBE_RESPONSE_RIDE_1):
            result = is_cluster_on_road(db_cur, cluster, "Henry Watts Dr")

        assert result is True

        db_cur.execute("""
            SELECT google_route_canonical, google_place_id
            FROM app_private.road_membership_cache
            WHERE target_road_canonical = 'henry watts'
        """)
        row = db_cur.fetchone()
        assert row is not None
        assert row["google_route_canonical"] == "Henry Watts Rd"
        assert row["google_place_id"] == "ChIJCwaQva_vQIYRJ40BFKp4gew"

    def test_negative_match_caches_false_verdict(self, db_cur):
        """Google response contains no road matching the target. Verdict
        is False; row IS written (negative caching saves the Google call
        next time the same question is asked)."""
        cluster = _cluster(RIDE_1_LAT, RIDE_1_LNG)

        with patch("road_membership._get_api_key", return_value="test-key"), \
             patch("road_membership._reverse_geocode",
                   return_value=NO_MATCH_RESPONSE):
            result = is_cluster_on_road(db_cur, cluster, "Westheimer Road")

        assert result is False

        db_cur.execute("""
            SELECT on_road, google_route_canonical
            FROM app_private.road_membership_cache
            WHERE target_road_canonical = 'westheimer'
        """)
        row = db_cur.fetchone()
        assert row is not None, (
            "Negative verdicts MUST be cached to avoid re-querying Google."
        )
        assert row["on_road"] is False
        assert row["google_route_canonical"] is None

    def test_short_name_path_closes_directional_gap(self, db_cur):
        """Google returns long='North Shepherd Drive' / short='N Shepherd Dr'
        for a residential point. Uber offer text is 'N Shepherd Dr'.
        Canonical of long is 'north shepherd' (no match); canonical of
        short is 'n shepherd' (match). Match comes via short_name path."""
        # Synthetic point — using ride 1 coords just to have a valid
        # H3 cell; the Google response is what matters for this test.
        cluster = _cluster(RIDE_1_LAT, RIDE_1_LNG)

        with patch("road_membership._get_api_key", return_value="test-key"), \
             patch("road_membership._reverse_geocode",
                   return_value=DIRECTIONAL_RESPONSE):
            result = is_cluster_on_road(db_cur, cluster, "N Shepherd Dr")

        assert result is True, (
            "Directional gap test: short_name 'N Shepherd Dr' should "
            "match offer 'N Shepherd Dr' via base-name comparison."
        )

        db_cur.execute("""
            SELECT google_route_canonical
            FROM app_private.road_membership_cache
            WHERE target_road_canonical = 'n shepherd'
        """)
        row = db_cur.fetchone()
        assert row is not None
        # The matched form recorded is the one that produced the hit,
        # i.e. the short_name (since long form's canonical was different).
        assert row["google_route_canonical"] == "N Shepherd Dr"


# ============================================================================
# Pure-Python tests — internal helpers and defensive paths
# ============================================================================

class TestCanonicalize:
    """_canonicalize_road_text truth table. Pins the suffix-strip
    semantics; new entries to _SUFFIX_TOKENS should not break these."""

    @pytest.mark.parametrize("raw, expected", [
        ("Watts Plantation Dr",     "watts plantation"),
        ("Watts Plantation Drive",  "watts plantation"),
        ("Watts Plantation Road",   "watts plantation"),
        ("WATTS PLANTATION DR.",    "watts plantation"),
        ("Henry Watts Rd",          "henry watts"),
        ("N Shepherd Dr",           "n shepherd"),
        ("North Shepherd Drive",    "north shepherd"),
        ("West Bellfort Blvd",      "west bellfort"),
        ("W Bellfort Boulevard",    "w bellfort"),
        ("FM 1960",                 "fm 1960"),   # no recognized suffix
        ("Highway 6",               "highway 6"),  # leading classifier; route-designator canonicalization is a separate gap
        ("",                        ""),
        ("   ",                     ""),
    ])
    def test_canonicalize(self, raw, expected):
        assert _canonicalize_road_text(raw) == expected


class TestRouteCandidates:
    """_extract_route_candidates skips non-route results."""

    def test_skips_plus_code_and_locality_results(self):
        candidates = _extract_route_candidates(PROBE_RESPONSE_RIDE_1)
        # 3 results have route components; 1 is a plus code (skipped).
        assert len(candidates) == 3
        route_names = {(long_, short_) for long_, short_, _ in candidates}
        assert ("Henry Watts Rd", "Henry Watts Rd") in route_names
        assert ("Watts Plantation Road",
                "Watts Plantation Rd") in route_names

    def test_picks_first_match_in_relevance_order(self):
        """Google sorts by relevance. First-match-wins is intentional."""
        candidates = _extract_route_candidates(PROBE_RESPONSE_RIDE_1)
        # 'watts plantation' appears in positions 2 and 3 (1-indexed);
        # the first one (position 2) should win.
        match = _pick_matching_route(candidates, "watts plantation")
        assert match is not None
        matched_form, place_id = match
        assert matched_form == "Watts Plantation Road"
        # The 2nd-result place_id, not the 3rd.
        assert place_id == "ChIJL_ecC5jvQIYRtk_N-VKLJrQ"


class TestDefensivePaths:
    """Defensive returns. Each test asserts no Google call AND no SQL
    side effects on early-return paths."""

    def test_none_cluster_returns_false_no_google(self):
        cur = MagicMock()
        with patch("road_membership._reverse_geocode") as mock_geocode:
            result = is_cluster_on_road(cur, None, "Watts Plantation Dr")
        assert result is False
        mock_geocode.assert_not_called()
        cur.execute.assert_not_called()

    def test_empty_target_returns_false_no_google(self):
        cur = MagicMock()
        cluster = _cluster(RIDE_1_LAT, RIDE_1_LNG)
        with patch("road_membership._reverse_geocode") as mock_geocode:
            result = is_cluster_on_road(cur, cluster, "")
        assert result is False
        mock_geocode.assert_not_called()
        cur.execute.assert_not_called()
