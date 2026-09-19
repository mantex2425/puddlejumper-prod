"""POI cache + Google Places v1 service — Operation Strip Mall Phase 2b.

Cache-first reads from app_private.poi_cache (places jsonb column); on
cache miss, calls Google Places API v1 (places.googleapis.com/v1/places:
searchNearby) with a tight field mask, writes the response (including
empty array for negative cache), returns the POI list.

Source of truth:
    OPERATION_STRIP_MALL_PROPOSAL.md REVISIONS R1-R7.
    Phase 2b iron-fist mandate 2026-05-05 (Gemini-ratified):
      - V1 endpoint, not legacy
      - 50m API search radius (tighter than 80m cache lookup)
      - Field mask: places.id, places.displayName, places.types, places.location
      - Negative cache writes on zero-result API responses
      - Fail-closed on API error (POILookupResult source='api_error', pois=[])

Canonical Standards v2.0:
    §I  Recon-first — Cluster.median_lat/median_lng confirmed in
        cluster_detection.py:42-43 (frozen dataclass).
    §II Coordinate functions — distance via app_private.distance_miles;
        spatial filter via app_private.coords_to_geography. No raw
        ST_MakePoint, no Python haversine.
    §III UTC mandatory — TTL filter, cache writes, last_hit_at update all
        use (NOW() AT TIME ZONE 'UTC').
    §V  Binding integrity — last_hit_at touched on cache hits (1h
        throttled); POILookupResult.source bound on every code path so
        heartbeat callers populate pudo_decision_context.poi_lookup_source
        unambiguously.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import requests

if TYPE_CHECKING:
    # Imported only for type hints to avoid circular imports at runtime.
    from cluster_detection import Cluster
    from psycopg2.extensions import cursor as _Cursor


# ─── DATA SHAPE ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class POI:
    """Immutable POI record returned to the matcher.

    Phase 2c's _signal_poi_match consumes this with rapidfuzz weighting
    (80% business-name, 20% street-number per R4). The full ``types`` list
    is preserved — Phase 2c uses it for type-aware confidence weighting
    (gas_station vs dentist signal differently in different contexts).

    Fields:
      place_id  -- Google Places v1 stable identifier (ChIJ...)
      name      -- displayName.text (the business's display label)
      types     -- full Google types array (primary + secondaries)
      lat       -- location.latitude (place's actual lat, not cluster's)
      lng       -- location.longitude (place's actual lng)
      dist_m    -- meters from cluster centroid to this place's location,
                   computed in Postgres via app_private.distance_miles
                   for cache reads, or in-flight after API parse.
    """

    place_id: str
    name: str
    types: list[str]
    lat: float
    lng: float
    dist_m: float


@dataclass(frozen=True)
class POILookupResult:
    """Forensic-grade return type from get_pois_near_cluster.

    Phase 1B's pudo_decision_context.poi_lookup_source column expects one
    of: 'cache_hit', 'api_call', 'api_error', 'skipped'. Phase 2b binds
    source on every code path so the heartbeat caller (Phase 2d) populates
    that column unambiguously.

    Source semantics:
      cache_hit -- one or more in-range cache rows found with TTL valid;
                   no API call made. pois may be empty if all matched
                   cache rows are negative-cache entries.
      api_call  -- cache miss; Google API returned 200 OK. Response may
                   have empty places list (negative-cache write); pois
                   reflects whatever was returned.
      api_error -- cache miss; API failed (timeout, non-200, malformed
                   JSON, missing key); no cache write attempted; pois=[].
      skipped   -- not used by this module; reserved for heartbeat
                   callers indicating they bypassed POI lookup entirely.
    """

    pois: list[POI]
    source: str


# ─── CONSTANTS ───────────────────────────────────────────────────────────────


# Cache lookup ST_DWithin radius. 80m per OPERATION_STRIP_MALL_PROPOSAL R3.
DEFAULT_RADIUS_M: int = 80


# Google API search radius. 50m — tighter than cache lookup so cache-write
# coverage stays well-bounded; iron-fist mandate 2026-05-05.
API_SEARCH_RADIUS_M: float = 50.0


# 30-day TTL on cache writes per OPERATION_STRIP_MALL_PROPOSAL R5.
CACHE_TTL_DAYS: int = 30


# Throttle for last_hit_at write-back. 1 hour suppresses heartbeat write
# storms on hot cache rows while still surfacing daily/weekly usage signal.
TOUCH_THROTTLE_INTERVAL: str = "1 hour"


# Mile→meter conversion for app_private.distance_miles results.
_METERS_PER_MILE: float = 1609.344


# Google Places v1 endpoint. POST with JSON body; field mask in header.
PLACES_V1_URL: str = "https://places.googleapis.com/v1/places:searchNearby"

# Field mask scopes the response to exactly the fields the matcher uses.
# Every additional field is paid for in v1's pricing model — keep tight.
PLACES_V1_FIELD_MASK: str = (
    "places.id,places.displayName,places.types,places.location"
)


# §XVII: Google Places v1 searchText endpoint. POST with JSON body; field
# mask in header (same as searchNearby). Used to resolve offer dropoff
# text to semantic anchors per canonical §XVII.
PLACES_V1_TEXT_URL: str = "https://places.googleapis.com/v1/places:searchText"


# Max anchors returned per searchText call. Captures dense venues (IAH
# returns ~12 anchors for "United, Houston, Texas") without inflating
# per-call cost.
PLACES_V1_TEXT_MAX_RESULTS: int = 20


# §XVII TTL on searchText cache writes per canonical §E. Venue identity
# is stable for years; 365 days is conservative.
SEMANTIC_CACHE_TTL_DAYS: int = 365


# §XVII default locationBias radius. 50km wide enough to cover the Houston
# metro for any text query; soft hint, not a hard restriction (Google may
# return results outside this radius if relevance is high).
SEMANTIC_DEFAULT_BIAS_RADIUS_M: float = 50000.0

# v1 hard cap is 20 results; we request the max so tight clusters in dense
# strip malls don't get truncated. The 50m radius keeps this manageable.
PLACES_V1_MAX_RESULTS: int = 20

# 5s timeout matches tools/places_tools.py convention. Heartbeat path can
# tolerate this — fail-closed below shorts cache write on slow responses.
PLACES_V1_TIMEOUT_S: float = 5.0


# Env-var name. Reused from puddles_brain.py / superpower_geo.py. Pre-flight
# curl 2026-05-05 verified Places API access on this key.
_API_KEY_ENV_VAR: str = "GOOGLE_MAPS_API_KEY"


logger = logging.getLogger(__name__)


# ─── PUBLIC API ──────────────────────────────────────────────────────────────


def get_pois_near_cluster(
    cluster: "Cluster",
    cur: "_Cursor",
    *,
    radius_m: int = DEFAULT_RADIUS_M,
) -> POILookupResult:
    """Return POIs near the cluster centroid; cache-first, API on miss.

    Behavior:
      1. Read cache for in-range, non-expired rows.
      2. Cache hit (one or more rows, regardless of place count) → touch
         last_hit_at on those rows, dedupe places by place_id (Option C
         keeps closer occurrence), return sorted by dist_m ASC.
         source = 'cache_hit'. Empty pois possible if all rows are
         negative-cache entries — that's still a hit; we already paid
         Google to learn there's nothing here.
      3. Cache miss (zero rows) → call Google Places v1 searchNearby at
         the cluster centroid with API_SEARCH_RADIUS_M. On 200, write the
         response (including empty places for negative cache) to a new
         poi_cache row with 30-day TTL. Return parsed POIs sorted ASC.
         source = 'api_call'.
      4. API failure (timeout, non-200, malformed JSON, missing key) →
         no cache write. Return POILookupResult(pois=[], source='api_error').
         The system reverts to road-only matching for this cluster.

    Args:
        cluster: a frozen Cluster with median_lat, median_lng (and other
            fields, ignored by this function).
        cur: psycopg2 cursor with RealDictCursor factory; the function
            relies on dict-style row access.
        radius_m: cache-lookup ST_DWithin radius. Default 80m per R3.

    Returns:
        POILookupResult with pois sorted ASC by per-place dist_m and
        source tag for forensic logging in pudo_decision_context.
    """
    cache_pois = _read_cache(cluster, cur, radius_m)
    if cache_pois is not None:
        return POILookupResult(pois=cache_pois, source="cache_hit")

    # Cache miss → API call.
    api_response, source = _call_google_places_v1(
        cluster.median_lat, cluster.median_lng
    )
    if api_response is None:
        # api_error: fail-closed, no cache write, empty result.
        return POILookupResult(pois=[], source=source)

    # 200 OK (places may be empty for negative cache). Persist before
    # returning so the next driver here gets a cache hit.
    _write_cache(cluster, cur, api_response.get("places", []))

    pois = _parse_v1_to_pois(
        api_response, cluster.median_lat, cluster.median_lng
    )
    return POILookupResult(pois=pois, source=source)


# ─── PRIVATE: CACHE READ ─────────────────────────────────────────────────────


def _read_cache(
    cluster: "Cluster", cur: "_Cursor", radius_m: int
) -> list[POI] | None:
    """Read in-range cache rows. Returns None on complete miss.

    The SELECT uses LEFT JOIN LATERAL jsonb_array_elements so cache rows
    with empty places (negative-cache entries) still yield a row, with
    NULL place fields. This distinguishes:

        zero rows  -> complete miss (caller invokes API)
        rows with all-NULL place_id -> negative-cache hits (caller skips API)
        rows with non-NULL place_id -> positive cache hits

    Distance is computed via app_private.distance_miles (canonical) and
    converted to meters; no Python geometry math, per Standard §II.

    Side effect: matched rows have last_hit_at touched, throttled to once
    per hour per row, per Standard §V.
    """
    cur.execute(
        """
        SELECT
            pc.id              AS cache_id,
            p->>'id'           AS place_id,
            p->'displayName'->>'text' AS name,
            p->'types'         AS types,
            (p->'location'->>'latitude')::float  AS poi_lat,
            (p->'location'->>'longitude')::float AS poi_lng,
            CASE
                WHEN p IS NULL THEN NULL
                ELSE app_private.distance_miles(
                    %s, %s,
                    (p->'location'->>'latitude')::float,
                    (p->'location'->>'longitude')::float
                ) * %s
            END AS dist_m
        FROM app_private.poi_cache pc
        LEFT JOIN LATERAL jsonb_array_elements(pc.places) AS p ON TRUE
        WHERE pc.expires_at > (NOW() AT TIME ZONE 'UTC')
          AND pc.text_query IS NULL
          AND ST_DWithin(
              pc.query_geog,
              app_private.coords_to_geography(%s, %s),
              %s
          )
        ORDER BY dist_m ASC NULLS LAST
        """,
        (
            cluster.median_lat, cluster.median_lng,  # distance_miles probe
            _METERS_PER_MILE,                         # mile->meter conversion
            cluster.median_lat, cluster.median_lng,  # ST_DWithin probe
            radius_m,
        ),
    )

    rows = cur.fetchall()
    if not rows:
        return None  # complete miss; caller invokes API

    # Touch last_hit_at on every distinct matched cache row, throttled to
    # 1h/row to avoid heartbeat write storms. Standard §V.
    cache_ids = list({row["cache_id"] for row in rows})
    cur.execute(
        f"""
        UPDATE app_private.poi_cache
        SET last_hit_at = (NOW() AT TIME ZONE 'UTC')
        WHERE id = ANY(%s)
          AND (
              last_hit_at IS NULL
              OR last_hit_at < (NOW() AT TIME ZONE 'UTC')
                               - INTERVAL '{TOUCH_THROTTLE_INTERVAL}'
          )
        """,
        (cache_ids,),
    )

    # Build POIs from non-sentinel rows. NULL place_id rows are negative-
    # cache sentinels — the cache row exists but has zero places.
    candidates: list[POI] = []
    for row in rows:
        if row["place_id"] is None:
            continue
        candidates.append(
            POI(
                place_id=row["place_id"],
                name=row["name"] or "",
                types=list(row["types"] or []),
                lat=float(row["poi_lat"]),
                lng=float(row["poi_lng"]),
                dist_m=float(row["dist_m"]),
            )
        )

    # Option C dedupe: for each unique place_id across all matched rows,
    # keep the closest occurrence. Sort ASC by dist_m so the matcher's
    # fuzzy scan hits the nearest candidate first.
    best: dict[str, POI] = {}
    for poi in candidates:
        existing = best.get(poi.place_id)
        if existing is None or poi.dist_m < existing.dist_m:
            best[poi.place_id] = poi

    return sorted(best.values(), key=lambda p: p.dist_m)


# ─── PRIVATE: GOOGLE PLACES V1 CALL ──────────────────────────────────────────


def _call_google_places_v1(
    lat: float, lng: float
) -> tuple[dict[str, Any] | None, str]:
    """POST to Google Places v1 searchNearby. Returns (response_dict, source).

    On 200 OK (including empty places list): returns (response, 'api_call').
    On any failure: returns (None, 'api_error') and logs a warning.

    Fail-closed: caller treats None as "no API data, no cache write,
    empty POI list." System reverts to road-only matching for this cluster.
    """
    api_key = os.environ.get(_API_KEY_ENV_VAR)
    if not api_key:
        logger.warning(
            "Google Places v1 call skipped: %s not set in environment",
            _API_KEY_ENV_VAR,
        )
        return None, "api_error"

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": PLACES_V1_FIELD_MASK,
    }
    body = {
        "maxResultCount": PLACES_V1_MAX_RESULTS,
        "locationRestriction": {
            "circle": {
                "center": {"latitude": lat, "longitude": lng},
                "radius": API_SEARCH_RADIUS_M,
            }
        },
    }

    try:
        response = requests.post(
            PLACES_V1_URL,
            json=body,
            headers=headers,
            timeout=PLACES_V1_TIMEOUT_S,
        )
    except requests.RequestException as exc:
        logger.warning("Google Places v1 request exception: %s", exc)
        return None, "api_error"

    if response.status_code != 200:
        logger.warning(
            "Google Places v1 returned %d: %s",
            response.status_code,
            response.text[:200],
        )
        return None, "api_error"

    try:
        data = response.json()
    except ValueError as exc:
        logger.warning("Google Places v1 returned malformed JSON: %s", exc)
        return None, "api_error"

    # Empty places is a valid 200 response (negative-cache scenario).
    # Normalize: ensure 'places' key exists for downstream parsing/write.
    if "places" not in data:
        data["places"] = []

    return data, "api_call"


# ─── PRIVATE: CACHE WRITE ────────────────────────────────────────────────────


def _write_cache(
    cluster: "Cluster", cur: "_Cursor", places: list[dict[str, Any]]
) -> None:
    """INSERT a new cache row holding the v1 places array verbatim.

    Empty places list is permitted — it produces a negative-cache row that
    suppresses redundant API calls within TTL. Schema's
    poi_cache_places_is_array CHECK passes for both [] and [{...}, ...].
    """
    cur.execute(
        f"""
        INSERT INTO app_private.poi_cache
            (expires_at, query_lat, query_lng, places)
        VALUES (
            (NOW() AT TIME ZONE 'UTC') + INTERVAL '{CACHE_TTL_DAYS} days',
            %s, %s, %s::jsonb
        )
        """,
        (
            cluster.median_lat,
            cluster.median_lng,
            json.dumps(places),
        ),
    )


# ─── PRIVATE: V1 → POI PARSING ───────────────────────────────────────────────


def _parse_v1_to_pois(
    api_response: dict[str, Any], cluster_lat: float, cluster_lng: float
) -> list[POI]:
    """Map a v1 response.places array to a sorted POI list.

    Used after a successful API call (the cache-read path computes
    distances in Postgres and constructs POIs from flat rows directly).
    Distance here is computed via _api_distance_m which calls Postgres
    NOT — we don't have a cursor here. Instead, we use a small in-Python
    distance approximation that's close enough for sort-order; the
    matcher in 2c re-computes distance against driver-actual GPS anyway.

    The defensive parser skips records missing required fields rather
    than crashing — the heartbeat path must not fail on a partial Google
    response.
    """
    pois: list[POI] = []
    for place in api_response.get("places", []):
        poi = _v1_place_to_poi(place, cluster_lat, cluster_lng)
        if poi is not None:
            pois.append(poi)
    return sorted(pois, key=lambda p: p.dist_m)


def _v1_place_to_poi(
    place: dict[str, Any], cluster_lat: float, cluster_lng: float
) -> POI | None:
    """Map a single v1 place dict to a POI. Returns None on missing fields.

    Distance: we compute a flat-earth approximation here because this
    code path only runs on a fresh API response (1-20 places) where we
    don't have a DB cursor. The approximation is accurate to within ~1m
    at 50m radius — sort-order-correct, which is all the matcher needs.
    The cache-read path computes canonical distance via Postgres.
    """
    place_id = place.get("id")
    display_name = place.get("displayName") or {}
    name = display_name.get("text")
    types = place.get("types") or []
    location = place.get("location") or {}
    lat = location.get("latitude")
    lng = location.get("longitude")

    if not place_id or not name or lat is None or lng is None:
        return None

    return POI(
        place_id=place_id,
        name=name,
        types=list(types),
        lat=float(lat),
        lng=float(lng),
        dist_m=_flat_earth_m(cluster_lat, cluster_lng, float(lat), float(lng)),
    )


def _flat_earth_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Flat-earth distance approximation in meters.

    Used only for ranking POIs returned in a single API response (50m
    radius, ≤20 places). At this scale, flat-earth error is well under
    1m — the matcher in Phase 2c re-computes against driver-actual GPS
    using canonical Postgres functions. Sort-order accuracy is the
    contract here, not absolute precision.

    For cache reads, distance is computed in Postgres via
    app_private.distance_miles per Standard §II — see _read_cache.
    """
    # 1° latitude ≈ 111,111m everywhere.
    # 1° longitude ≈ 111,111m * cos(latitude).
    import math

    avg_lat_rad = math.radians((lat1 + lat2) / 2.0)
    dy = (lat2 - lat1) * 111_111.0
    dx = (lng2 - lng1) * 111_111.0 * math.cos(avg_lat_rad)
    return math.sqrt(dx * dx + dy * dy)

# ─── §XVII: SEMANTIC ANCHOR (searchText) ─────────────────────────────────────


def _call_google_places_text_v1(
    text_query: str,
    bias_lat: float,
    bias_lng: float,
    bias_radius_m: float,
) -> tuple[dict[str, Any] | None, str]:
    """POST to Google Places v1 searchText. Returns (response_dict, source).

    Sibling of _call_google_places_v1. Differs in two ways:
      (1) Endpoint: places:searchText, not places:searchNearby.
      (2) Body uses locationBias.circle (soft hint), not locationRestriction.
          Google may return results outside the bias radius if relevance is high.

    On 200 OK (including empty places list): returns (response, 'semantic_api_call').
    On any failure: returns (None, 'semantic_api_error') and logs a warning.

    Fail-closed: caller treats None as "no API data, no cache write,
    empty anchor list." System falls through to other matcher heads for
    this cluster.
    """
    api_key = os.environ.get(_API_KEY_ENV_VAR)
    if not api_key:
        logger.warning(
            "Google Places v1 searchText call skipped: %s not set in environment",
            _API_KEY_ENV_VAR,
        )
        return None, "semantic_api_error"

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": PLACES_V1_FIELD_MASK,
    }
    body = {
        "textQuery": text_query,
        "maxResultCount": PLACES_V1_TEXT_MAX_RESULTS,
        "locationBias": {
            "circle": {
                "center": {"latitude": bias_lat, "longitude": bias_lng},
                "radius": bias_radius_m,
            }
        },
    }

    try:
        response = requests.post(
            PLACES_V1_TEXT_URL,
            json=body,
            headers=headers,
            timeout=PLACES_V1_TIMEOUT_S,
        )
    except requests.RequestException as exc:
        logger.warning("Google Places v1 searchText request exception: %s", exc)
        return None, "semantic_api_error"

    if response.status_code != 200:
        logger.warning(
            "Google Places v1 searchText returned %d: %s",
            response.status_code,
            response.text[:200],
        )
        return None, "semantic_api_error"

    try:
        data = response.json()
    except ValueError as exc:
        logger.warning("Google Places v1 searchText returned malformed JSON: %s", exc)
        return None, "semantic_api_error"

    # Empty places is a valid 200 response (negative-cache scenario; rare for
    # well-formed text queries, common for gibberish/dead venues). Normalize
    # 'places' key for downstream parsing/write.
    if "places" not in data:
        data["places"] = []

    return data, "semantic_api_call"


def _read_text_cache(
    text_query: str,
    bias_lat: float,
    bias_lng: float,
    bias_radius_m: float,
    cur: "_Cursor",
) -> list[POI] | None:
    """Read searchText cache rows by exact (text_query, bias) tuple.

    Sibling of _read_cache. Differs in two ways:
      (1) Lookup is by partial UNIQUE key (text_query, query_lat, query_lng,
          bias_radius_m), not by spatial proximity.
      (2) dist_m on returned POIs is 0.0 per Gemini directive 1 — these are
          semantic anchors whose spatial relationship to the cluster is
          recomputed by Patch 2's _signal_semantic_anchor against the
          cluster centroid, not the bias center.

    Returns None on miss (caller invokes API), or a list of POIs on hit
    (possibly empty for negative-cache rows).

    Side effect: matched row has last_hit_at touched, throttled to once
    per hour per row.
    """
    cur.execute(
        """
        SELECT
            pc.id              AS cache_id,
            p->>'id'           AS place_id,
            p->'displayName'->>'text' AS name,
            p->'types'         AS types,
            (p->'location'->>'latitude')::float  AS poi_lat,
            (p->'location'->>'longitude')::float AS poi_lng
        FROM app_private.poi_cache pc
        LEFT JOIN LATERAL jsonb_array_elements(pc.places) AS p ON TRUE
        WHERE pc.expires_at > (NOW() AT TIME ZONE 'UTC')
          AND pc.text_query = %s
          AND pc.query_lat = %s
          AND pc.query_lng = %s
          AND pc.bias_radius_m = %s
        """,
        (text_query, bias_lat, bias_lng, bias_radius_m),
    )

    rows = cur.fetchall()
    if not rows:
        return None  # complete miss; caller invokes API

    # Touch last_hit_at, throttled to 1h per row.
    cache_ids = list({row["cache_id"] for row in rows})
    cur.execute(
        f"""
        UPDATE app_private.poi_cache
        SET last_hit_at = (NOW() AT TIME ZONE 'UTC')
        WHERE id = ANY(%s)
          AND (
              last_hit_at IS NULL
              OR last_hit_at < (NOW() AT TIME ZONE 'UTC')
                               - INTERVAL '{TOUCH_THROTTLE_INTERVAL}'
          )
        """,
        (cache_ids,),
    )

    # Build POIs from non-sentinel rows. NULL place_id rows are negative-
    # cache sentinels (cache row exists but places is []).
    pois: list[POI] = []
    for row in rows:
        if row["place_id"] is None:
            continue
        pois.append(
            POI(
                place_id=row["place_id"],
                name=row["name"] or "",
                types=list(row["types"] or []),
                lat=float(row["poi_lat"]),
                lng=float(row["poi_lng"]),
                dist_m=0.0,  # §XVII: spatial relation deferred to matcher
            )
        )

    return pois


def _write_text_cache(
    text_query: str,
    bias_lat: float,
    bias_lng: float,
    bias_radius_m: float,
    places: list[dict[str, Any]],
    cur: "_Cursor",
) -> None:
    """INSERT a new searchText cache row. Idempotent via partial UNIQUE.

    Sibling of _write_cache. Differs in three ways:
      (1) Sets text_query and bias_radius_m columns.
      (2) 365-day TTL (SEMANTIC_CACHE_TTL_DAYS) vs 30-day legacy TTL.
      (3) ON CONFLICT DO NOTHING against the partial UNIQUE index per
          canonical §E. Concurrent writers with the same (text_query, bias)
          tuple collapse to one row.

    Empty places list is permitted (negative-cache row; suppresses API
    retries for 365 days for gibberish queries).
    """
    cur.execute(
        f"""
        INSERT INTO app_private.poi_cache
            (expires_at, query_lat, query_lng, places, text_query, bias_radius_m)
        VALUES (
            (NOW() AT TIME ZONE 'UTC') + INTERVAL '{SEMANTIC_CACHE_TTL_DAYS} days',
            %s, %s, %s::jsonb, %s, %s
        )
        ON CONFLICT (text_query, query_lat, query_lng, bias_radius_m)
            WHERE text_query IS NOT NULL DO NOTHING
        """,
        (
            bias_lat,
            bias_lng,
            json.dumps(places),
            text_query,
            bias_radius_m,
        ),
    )


def _parse_v1_anchors(
    api_response: dict[str, Any],
) -> list[POI]:
    """Map a v1 searchText response to a POI list with dist_m = 0.0.

    Wraps _parse_v1_to_pois but discards its bias-center-relative distances.
    Per Gemini directive 1: setting dist_m to 0.0 signals these are semantic
    anchors whose spatial relationship to the car is pending Phase 2b
    re-calculation in _signal_semantic_anchor (Patch 2).
    """
    # Pass zeros for the reference point — _parse_v1_to_pois computes
    # dist_m relative to that point, but we immediately overwrite dist_m
    # to 0.0 below for forensic clarity.
    raw_pois = _parse_v1_to_pois(api_response, 0.0, 0.0)
    return [
        POI(
            place_id=p.place_id,
            name=p.name,
            types=p.types,
            lat=p.lat,
            lng=p.lng,
            dist_m=0.0,
        )
        for p in raw_pois
    ]


def get_anchors_for_text(
    text_query: str,
    cur: "_Cursor",
    *,
    bias_lat: float,
    bias_lng: float,
    bias_radius_m: float = SEMANTIC_DEFAULT_BIAS_RADIUS_M,
) -> POILookupResult:
    """Return semantic anchors for an offer text query; cache-first, API on miss.

    Public sibling of get_pois_near_cluster. Implements §XVII canonical §A-E.

    Behavior:
      1. Read cache by (text_query, bias_lat, bias_lng, bias_radius_m).
      2. Cache hit → touch last_hit_at (1h throttled), return
         POILookupResult(pois=..., source='semantic_cache_hit'). Empty pois
         possible if the row is a negative-cache entry.
      3. Cache miss → call Google Places v1 searchText with locationBias.
         On 200, write to cache (365-day TTL, including empty places for
         negative cache). Return POILookupResult(pois=..., source='semantic_api_call').
      4. API failure → no cache write. Return POILookupResult(pois=[],
         source='semantic_api_error'). Matcher falls through to other heads.

    Args:
        text_query: the offer's address text (dropoff_address or pickup_address),
            passed verbatim to Google Text Search. No tokenization, no
            canonicalization.
        cur: psycopg2 cursor with RealDictCursor factory.
        bias_lat, bias_lng: locationBias circle center. Market-specific
            (Houston: 29.7604, -95.3698).
        bias_radius_m: locationBias circle radius. Default 50km
            (SEMANTIC_DEFAULT_BIAS_RADIUS_M).

    Returns:
        POILookupResult with anchor POIs (dist_m=0.0; spatial relation
        deferred to matcher) and source ∈ {semantic_cache_hit,
        semantic_api_call, semantic_api_error}.
    """
    cache_anchors = _read_text_cache(
        text_query, bias_lat, bias_lng, bias_radius_m, cur,
    )
    if cache_anchors is not None:
        return POILookupResult(pois=cache_anchors, source="semantic_cache_hit")

    # Cache miss → API call.
    api_response, source = _call_google_places_text_v1(
        text_query, bias_lat, bias_lng, bias_radius_m,
    )
    if api_response is None:
        # semantic_api_error: fail-closed, no cache write, empty result.
        return POILookupResult(pois=[], source=source)

    # 200 OK (places may be empty for negative cache). Persist first so
    # the next caller with the same text gets a cache hit.
    _write_text_cache(
        text_query, bias_lat, bias_lng, bias_radius_m,
        api_response.get("places", []), cur,
    )

    anchors = _parse_v1_anchors(api_response)
    return POILookupResult(pois=anchors, source=source)

