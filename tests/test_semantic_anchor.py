"""Patch 5/5 — Regression test suite for §XVII Semantic Anchor.

Canonical reference: docs/CANONICAL_RULES_SECTION_XVII.md, §A-K.

Test architecture follows §XIV.J Live-PG Test Floor:
  - Cache round-trip (#3) and Iron Curtain (#7) use the db_cur fixture
    (SAVEPOINT-isolated real psycopg2 cursor).
  - Pure-Python signal tests (#1, #4, #5, #6) call _signal_semantic_anchor
    directly with constructed POI lists. No cursor, no monkeypatching.
  - HOU edge case (#2) is xskipped — fixture not yet captured. Tracked,
    non-blocking per the morning handoff's Patch 5 mandate.

Fixture provenance (test #1):
  tests/fixtures/iah_united_anchors.json was extracted from
  app_private.poi_cache.id=349 on 2026-05-14 via:

      psql -t -A -c "SELECT places::text FROM app_private.poi_cache
                     WHERE id = 349;" > tests/fixtures/iah_united_anchors.json

  The row was seeded by tmp/backtest_xvii_tier_a.py on 2026-05-14 ~13:49 CDT
  and confirmed by the IAH ride-7883 replay on 2026-05-14 ~23:50 CDT, which
  produced semantic_anchor_score=0.8241866559386112 against
  semantic_anchor:United/transportation_service (88m) for cluster
  (29.9869, -95.3350). See HANDOFF_2026-05-14_LATELATE_PM* for full
  validation evidence.

  The fixture is self-contained: pickling/serializing the anchor list
  decouples the test from the live cache row's TTL (expires 2027-05-14)
  and from any future poi_cache eviction policy.

The matcher's contract (per where_am_i.py:735 _signal_semantic_anchor
docstring): the caller is responsible for recomputing cluster-relative
dist_m before invoking the signal, because get_anchors_for_text returns
dist_m=0.0 by canonical §E (anchors carry venue identity, not spatial
relation to the car). Tests #1 and #5 honor this contract via
poi_service._flat_earth_m, the same distance function the matcher uses
in production.
"""

from __future__ import annotations

import json
import os
import types
import uuid

import pytest

import poi_service
from poi_service import (
    POI,
    SEMANTIC_CACHE_TTL_DAYS,
    SEMANTIC_DEFAULT_BIAS_RADIUS_M,
    _flat_earth_m,
    _read_cache,
    _read_text_cache,
    _write_text_cache,
)
from where_am_i import (
    _SEMANTIC_DEFAULT_HORIZON_M,
    _SEMANTIC_TYPE_HORIZON_MAP,
    _signal_semantic_anchor,
)


# ---------------------------------------------------------------------------
# Constants — the IAH validation event, recorded as code.
# ---------------------------------------------------------------------------

# Cluster centroid at canonical IAH arrest (the 6 contiguous frames in the
# 2026-05-14 replay, all within GPS-jitter of this point).
IAH_ARREST_LAT: float = 29.9869
IAH_ARREST_LNG: float = -95.3350

# The Houston market-center bias used for §XVII searchText calls per the
# canonical doc §C and where_am_i.py:_HOUSTON_BIAS_LAT/LNG.
HOUSTON_BIAS_LAT: float = 29.7604
HOUSTON_BIAS_LNG: float = -95.3698

# The expected peak score from the IAH replay, to four decimal places.
# Replay produced 0.8241866559386112; we assert within ±0.001 to absorb
# any future float-rounding drift in _flat_earth_m without losing
# regression value.
IAH_EXPECTED_PEAK_SCORE: float = 0.8242
IAH_PEAK_SCORE_TOLERANCE: float = 0.001

# Path to the IAH anchor fixture (relative to this test file's location).
_FIXTURE_DIR: str = os.path.join(os.path.dirname(__file__), "fixtures")
IAH_ANCHOR_FIXTURE: str = os.path.join(_FIXTURE_DIR, "iah_united_anchors.json")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_iah_anchor_places() -> list[dict]:
    """Load the 12-place IAH/United fixture as raw JSONB-shaped dicts.

    Returns the list exactly as it would appear in poi_cache.places.
    Each entry has keys: id, displayName.text, types, location.{lat,lng},
    formattedAddress.
    """
    with open(IAH_ANCHOR_FIXTURE) as f:
        data = json.load(f)
    assert isinstance(data, list), "Fixture must be a JSON array"
    assert len(data) == 12, f"Fixture expected 12 places, got {len(data)}"
    return data


def _places_to_pois_with_cluster_dist(
    places: list[dict],
    cluster_lat: float,
    cluster_lng: float,
) -> list[POI]:
    """Convert raw place dicts to POIs with cluster-relative dist_m.

    Mirrors what the production matcher does in evaluate() between
    get_anchors_for_text (which returns dist_m=0.0) and
    _signal_semantic_anchor (which scores against the recomputed dist_m).
    """
    pois: list[POI] = []
    for p in places:
        loc = p.get("location") or {}
        lat = float(loc.get("latitude"))
        lng = float(loc.get("longitude"))
        dist_m = _flat_earth_m(cluster_lat, cluster_lng, lat, lng)
        pois.append(
            POI(
                place_id=p.get("id") or "",
                name=(p.get("displayName") or {}).get("text") or "",
                types=list(p.get("types") or []),
                lat=lat,
                lng=lng,
                dist_m=dist_m,
            )
        )
    return pois


def _short_uuid() -> str:
    """12-char hex tag for test isolation within poi_cache (sentinel-style)."""
    return uuid.uuid4().hex[:12]


# ---------------------------------------------------------------------------
# Case 1 — The IAH regression. The point of this whole sprint.
# ---------------------------------------------------------------------------


def test_iah_united_anchors_score_above_floor_at_replay_value():
    """§XVII Head 5 must score >= WAI_CONFIDENCE_THRESHOLD (0.40) for the
    canonical IAH ride-7883 dropoff geometry.

    Hard truth: the IAH replay on 2026-05-14 produced
    semantic_anchor_score=0.8241866559386112 against the 12-place anchor
    set in poi_cache.id=349 with cluster (29.9869, -95.3350). This test
    recomputes that signal end-to-end against the pickled anchor list
    and asserts the peak score matches.
    """
    places = _load_iah_anchor_places()
    anchors = _places_to_pois_with_cluster_dist(
        places, IAH_ARREST_LAT, IAH_ARREST_LNG,
    )

    score, witness = _signal_semantic_anchor(anchors)

    # The floor — this is the regression guard. If §XVII Head 5 ever stops
    # firing for the canonical IAH case, this test catches it.
    assert score >= 0.40, (
        f"§XVII Head 5 must clear the 0.40 floor for IAH/United. "
        f"Got score={score!r}, witness={witness!r}. "
        f"This is the regression Patch 5 was authored to prevent."
    )

    # Stronger assertion — the score should match the replay-validated peak
    # to within float-rounding noise. Drift here means something changed
    # in either the horizon map, the linear-decay formula, or _flat_earth_m.
    assert abs(score - IAH_EXPECTED_PEAK_SCORE) < IAH_PEAK_SCORE_TOLERANCE, (
        f"§XVII Head 5 score drifted from the replay-validated peak. "
        f"Expected {IAH_EXPECTED_PEAK_SCORE} ± {IAH_PEAK_SCORE_TOLERANCE}, "
        f"got {score!r}. "
        f"Check horizon map, _flat_earth_m, or _signal_semantic_anchor."
    )

    # Witness must follow the canonical §F format. The peak anchor name
    # is data-driven (whichever of the 12 places sits closest to the
    # IAH arrest); we assert the prefix and shape, not the exact name.
    assert witness is not None
    assert witness.startswith("semantic_anchor:"), (
        f"Witness must follow canonical §F format. Got: {witness!r}"
    )
    assert "United" in witness, (
        f"Witness for the IAH/United case must mention 'United'. "
        f"Got: {witness!r}"
    )


# ---------------------------------------------------------------------------
# Case 2 — HOU edge case (xskipped, tracked).
# ---------------------------------------------------------------------------


@pytest.mark.skip(
    reason=(
        "HOU/Hobby Airport fixture not yet captured. William P. Hobby's "
        "centroid sits ~4428m from canonical passenger-drop coordinates, "
        "exceeding the §C 1000m airport horizon. Tracked as a recall-vs-"
        "precision tuning concern, non-blocking for §XVII regression suite."
    )
)
def test_hou_hobby_beyond_airport_horizon():  # pragma: no cover
    """Placeholder for the HOU/Hobby edge case.

    When the HOU fixture is captured, this test should:
      1. Load the fixture (probably tests/fixtures/hou_anchors.json).
      2. Score against an HOU-area arrest cluster.
      3. Assert score == 0.0 (or whatever ratified outcome the team
         reaches — possibly raising the airport horizon further, or
         leaving HOU as a known recall miss).
    """
    pytest.fail("Unreached — test is unconditionally skipped.")


# ---------------------------------------------------------------------------
# Case 3 — Cache round-trip with 365-day TTL (live-PG).
# ---------------------------------------------------------------------------


def test_text_cache_round_trip_writes_then_reads_with_365d_ttl(db_cur):
    """_write_text_cache must persist a row that _read_text_cache returns
    with the canonical §E TTL of 365 days.

    Validates:
      - INSERT path against the partial UNIQUE index
      - 365-day expires_at offset from cached_at
      - Read-back returns the exact place set written
      - Read-back POIs carry dist_m=0.0 per canonical §E
      - last_hit_at touched after the read

    The SAVEPOINT in db_cur rolls back at teardown; nothing persists.
    The text_query is uuid-tagged to avoid colliding with the canonical
    IAH row (poi_cache.id=349) even if rollback fails.
    """
    test_tag = _short_uuid()
    text_query = f"TEST_SEMANTIC_XVII_{test_tag}"
    bias_radius = SEMANTIC_DEFAULT_BIAS_RADIUS_M

    # Synthetic 2-place anchor set. Structure matches what
    # _parse_v1_anchors would produce from Google's v1 searchText response.
    fixture_places = [
        {
            "id": f"PLACE_A_{test_tag}",
            "displayName": {"text": "Test Anchor Alpha", "languageCode": "en"},
            "types": ["transportation_service", "point_of_interest"],
            "location": {"latitude": 29.9869, "longitude": -95.3350},
        },
        {
            "id": f"PLACE_B_{test_tag}",
            "displayName": {"text": "Test Anchor Beta", "languageCode": "en"},
            "types": ["establishment"],
            "location": {"latitude": 29.9870, "longitude": -95.3351},
        },
    ]

    # WRITE
    _write_text_cache(
        text_query,
        HOUSTON_BIAS_LAT,
        HOUSTON_BIAS_LNG,
        bias_radius,
        fixture_places,
        db_cur,
    )

    # Direct-SQL TTL verification — read the row's cached_at/expires_at
    # delta and confirm it matches SEMANTIC_CACHE_TTL_DAYS (365).
    db_cur.execute(
        """
        SELECT cached_at, expires_at,
               EXTRACT(EPOCH FROM (expires_at - cached_at)) AS ttl_seconds
          FROM app_private.poi_cache
         WHERE text_query = %s
           AND query_lat = %s
           AND query_lng = %s
           AND bias_radius_m = %s
        """,
        (text_query, HOUSTON_BIAS_LAT, HOUSTON_BIAS_LNG, bias_radius),
    )
    row = db_cur.fetchone()
    assert row is not None, "Expected exactly one row after _write_text_cache"
    expected_ttl_seconds = SEMANTIC_CACHE_TTL_DAYS * 86400
    # Tolerance ± 1 second to absorb cached_at default vs INTERVAL
    # interpolation timing.
    assert abs(row["ttl_seconds"] - expected_ttl_seconds) < 1.0, (
        f"§E mandates {SEMANTIC_CACHE_TTL_DAYS}-day TTL for searchText "
        f"cache rows. Got delta = {row['ttl_seconds']} seconds "
        f"(expected ~{expected_ttl_seconds})."
    )

    # READ-BACK
    pois = _read_text_cache(
        text_query,
        HOUSTON_BIAS_LAT,
        HOUSTON_BIAS_LNG,
        bias_radius,
        db_cur,
    )
    assert pois is not None, "Cache miss after successful write — bug in (text_query, bias) key shape?"
    assert len(pois) == 2, f"Expected 2 anchors round-tripped, got {len(pois)}"

    # Per canonical §E: anchor POIs from text-cache reads carry dist_m=0.0;
    # spatial relation is recomputed by the matcher.
    for poi in pois:
        assert poi.dist_m == 0.0, (
            f"Anchor POI from _read_text_cache must carry dist_m=0.0 per "
            f"canonical §E. Got poi.dist_m={poi.dist_m!r} for {poi.name!r}."
        )

    # Place-set fidelity — names and place_ids preserved through the
    # round-trip.
    names_back = {p.name for p in pois}
    assert names_back == {"Test Anchor Alpha", "Test Anchor Beta"}, (
        f"Round-trip name set mismatch. Got: {names_back!r}"
    )

    # last_hit_at touched. Read it via direct SQL since pois don't carry it.
    db_cur.execute(
        """
        SELECT last_hit_at
          FROM app_private.poi_cache
         WHERE text_query = %s
        """,
        (text_query,),
    )
    last_hit = db_cur.fetchone()["last_hit_at"]
    assert last_hit is not None, (
        "last_hit_at must be touched on the first cache hit (no throttle "
        "applies to NULL last_hit_at per _read_text_cache's UPDATE clause)."
    )


# ---------------------------------------------------------------------------
# Case 4 — Horizon map canonical values.
# ---------------------------------------------------------------------------


def test_horizon_map_carries_canonical_per_type_values():
    """The horizon map is the matcher's only category step. Drift here
    silently changes match recall for entire venue classes.

    Canonical values per docs/CANONICAL_RULES_SECTION_XVII.md §C, as
    ratified in the §XVII Patch 3 raise from 800m → 1000m for airport.
    Note: the canonical doc as written still says 800m; the code is the
    truth, and the doc will be updated post-Patch-5 per the morning
    handoff's "Canonical doc updates" item.
    """
    assert _SEMANTIC_TYPE_HORIZON_MAP["airport"] == 1000.0
    assert _SEMANTIC_TYPE_HORIZON_MAP["international_airport"] == 1000.0
    assert _SEMANTIC_TYPE_HORIZON_MAP["stadium"] == 600.0
    assert _SEMANTIC_TYPE_HORIZON_MAP["tourist_attraction"] == 600.0
    assert _SEMANTIC_TYPE_HORIZON_MAP["university"] == 500.0
    assert _SEMANTIC_TYPE_HORIZON_MAP["shopping_mall"] == 500.0
    assert _SEMANTIC_TYPE_HORIZON_MAP["hospital"] == 150.0
    assert _SEMANTIC_TYPE_HORIZON_MAP["medical_clinic"] == 150.0
    assert _SEMANTIC_TYPE_HORIZON_MAP["lodging"] == 150.0
    assert _SEMANTIC_DEFAULT_HORIZON_M == 500.0


# ---------------------------------------------------------------------------
# Case 5 — Negative score below floor.
# ---------------------------------------------------------------------------


def test_score_is_zero_when_anchor_beyond_type_horizon():
    """Linear decay clamps at zero — anchors past their horizon contribute
    nothing. This is what prevents §XVII from over-firing on far-away
    venues that share an offer's address text.

    Test geometry: hospital anchor at 600m from cluster, hospital horizon
    is 150m. Score must clamp to 0.0; witness must be None (no anchor
    produced a positive score).
    """
    far_hospital = POI(
        place_id="TEST_FAR_HOSPITAL",
        name="Test Hospital Far",
        types=["hospital", "establishment"],
        lat=29.0,  # arbitrary; dist_m is what the signal uses
        lng=-95.0,
        dist_m=600.0,  # 4x the 150m hospital horizon
    )

    score, witness = _signal_semantic_anchor([far_hospital])

    assert score == 0.0, (
        f"Anchor at 600m with 150m horizon must clamp to score=0.0. "
        f"Got score={score!r}."
    )
    assert witness is None, (
        f"Witness must be None when no anchor produces positive score. "
        f"Got witness={witness!r}."
    )


# ---------------------------------------------------------------------------
# Case 6 — Empty anchor list returns (0.0, None).
# ---------------------------------------------------------------------------


def test_empty_anchor_list_returns_zero_and_none():
    """The signal must gracefully handle empty input — this is the path
    taken when get_anchors_for_text returns POILookupResult(pois=[],
    source='semantic_api_error') or when a text query yields zero anchors
    (negative cache).

    The matcher's evaluate() falls through to Heads 1-4 when Head 5
    returns (0.0, None), so this contract is load-bearing for the
    multi-head composition.
    """
    score, witness = _signal_semantic_anchor([])
    assert score == 0.0
    assert witness is None


# ---------------------------------------------------------------------------
# Case 7 — Iron Curtain (live-PG).
# ---------------------------------------------------------------------------


def test_iron_curtain_spatial_cluster_read_excludes_searchtext_rows(db_cur):
    """_read_cache (the legacy searchNearby reader) must NOT return rows
    that were written by _write_text_cache (the §XVII searchText writer).

    The defensive predicate `AND pc.text_query IS NULL` in _read_cache
    (poi_service.py:281) is what enforces this separation. Without it,
    spatial cluster reads would surface §XVII semantic anchors as if
    they were nearby POIs, corrupting Head 1/2/3/4 inputs.

    Test geometry: insert a searchText row whose query_lat/lng are
    inside Houston, then probe _read_cache with a cluster at the same
    coordinates. The probe must return None (complete miss) because
    the text_query IS NULL predicate excludes our row.
    """
    test_tag = _short_uuid()
    text_query = f"TEST_IRON_CURTAIN_{test_tag}"
    bias_radius = SEMANTIC_DEFAULT_BIAS_RADIUS_M

    # Plant a searchText row at a quiet Houston coordinate. We use
    # (29.5800, -95.5500) — a SW Houston point unlikely to collide with
    # any live searchNearby cache row in the production table.
    quiet_lat = 29.5800
    quiet_lng = -95.5500
    fixture_places = [
        {
            "id": f"IRON_CURTAIN_{test_tag}",
            "displayName": {"text": "Iron Curtain Probe", "languageCode": "en"},
            "types": ["establishment"],
            "location": {"latitude": quiet_lat, "longitude": quiet_lng},
        }
    ]
    # NOTE: _write_text_cache writes the bias center as query_lat/lng,
    # NOT the place's coordinates. So we set the bias center to our
    # quiet coordinate so the row's query_geog sits there.
    _write_text_cache(
        text_query,
        quiet_lat,
        quiet_lng,
        bias_radius,
        fixture_places,
        db_cur,
    )

    # Verify the row landed where we expect.
    db_cur.execute(
        """
        SELECT id, text_query, query_lat, query_lng
          FROM app_private.poi_cache
         WHERE text_query = %s
        """,
        (text_query,),
    )
    planted = db_cur.fetchone()
    assert planted is not None, "Plant failed — Iron Curtain test cannot run"

    # Probe _read_cache at the same coordinates. The cluster needs only
    # median_lat / median_lng for _read_cache's purposes.
    cluster = types.SimpleNamespace(
        median_lat=quiet_lat,
        median_lng=quiet_lng,
    )
    # 80m radius matches the production default per
    # test_poi_service:test_cache_lookup_default_radius_is_80m_per_proposal_R3.
    result = _read_cache(cluster, db_cur, radius_m=80)

    # The defensive predicate must filter our row out. Result is None
    # (complete miss) because text_query IS NULL excludes our planted row
    # AND no legacy searchNearby rows exist at this quiet coordinate.
    assert result is None, (
        f"Iron Curtain breach: _read_cache returned {result!r} when probing "
        f"coordinates where ONLY a searchText row exists. The "
        f"`AND pc.text_query IS NULL` predicate at poi_service.py:281 is "
        f"missing or has regressed. This would surface §XVII semantic "
        f"anchors as searchNearby POIs and corrupt Heads 1/2/3/4."
    )
