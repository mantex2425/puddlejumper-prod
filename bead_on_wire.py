"""
bead_on_wire.py — DIAGNOSE-layer primitives for target refinement via
driver trajectory, road network, and odometer.

The "Blind Man with his hand on a rail" model: the driver walks along named
road segments; the system tracks which rail the hand is touching; when YOLO
distance matches and a cluster forms, fire.

All functions in this module are PURE READ — they query but never mutate.
Writes happen through the existing PLAN → EXECUTE handoff in
nail_it_core.check_convergence().

Architecture: 4-Box Controller, DIAGNOSE box.

This module deliberately reuses primitives from:
  - superpower_geo.INTERSECTION_REGEX (intersection separator detection)
  - nail_it_core.is_vague_address / VAGUE_KEYWORDS (highway detection)
  - triangulation._HIGHWAY_RE (highway name pattern)
  - nail_it_core._haversine_m, median_centroid (math)
"""

import re
import logging
from typing import Optional

# Google geocode fallback — reuse existing triangulation primitives rather
# than reinvent. These already handle API timeout (2s), caching, and logging.
from geo_utils import (
    _google_geocode,
    _lookup_geocode_cache,
    _write_geocode_cache,
)

# Inlined from superpower_geo.py (that module imports `h3` which isn't
# available on all deploy targets — we copy the two constants we need).
INTERSECTION_REGEX = re.compile(r'\s*(?:&|and|/|@|\+)\s*', re.IGNORECASE)
POI_TYPE_MAP = {
    "park", "airport", "mall", "hospital", "hotel",
    "stadium", "zoo", "museum"
}


# ============================================================================
# OCR cleanup — used before any pattern matching
# ============================================================================

# "9 mins (2.8 mi)", "1l mins (4.6 mi)" — time/distance leakage from the
# OCR of the Uber offer card
_RE_OCR_TIME_DIST = re.compile(
    r"\b\w*\s*\d*\w?\s*mins?\s*\(\d+\.?\d*\s*mi\)",
    re.IGNORECASE
)


def _clean_ocr(text: str) -> str:
    """Strip known OCR pollution patterns, collapse whitespace."""
    cleaned = _RE_OCR_TIME_DIST.sub(" ", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = re.sub(r",\s*,", ",", cleaned)
    return cleaned


# ============================================================================
# Road suffix detection — broader than _is_vague_single_road
# ============================================================================

_ROAD_SUFFIXES = {
    # Standard abbreviations
    "st", "rd", "ave", "blvd", "dr", "ln", "ct", "pl", "pkwy", "fwy",
    "hwy", "trl", "cir", "way", "ter", "trce", "xing", "pt", "row",
    "run", "path", "loop", "bend", "cove", "walk", "sq", "grove",
    "glen", "park", "creek", "brook", "vista", "rise", "knoll",
    # Spelled-out
    "street", "road", "avenue", "boulevard", "drive", "lane", "court",
    "place", "parkway", "freeway", "highway", "trail", "circle", "terrace",
    "trace", "crossing", "point", "square", "plaza",
    # Directional (trailing a road name, e.g. "Dulles Ave N")
    "n", "s", "e", "w", "ne", "nw", "se", "sw",
}

# Houston-specific highway pattern (shared shape with triangulation.py)
_HIGHWAY_RE = re.compile(
    r"""^(?:
        [a-z]{1,3}[-\s]?\d+              # I-10, US-59, SR-6, FM 1960
      | (?:state\s+)?hwy\s*\d+           # Hwy 6, Hwy6, State Hwy 6
      | (?:state\s+)?highway\s*\d+       # Highway 6, Highway6
      | [nsew]?\s*loop\s+\d+             # Loop 610, N Loop 610, W Loop 610
      | beltway\s*\d*                     # Beltway 8
    )""",
    re.IGNORECASE | re.VERBOSE
)


# Compound-word road endings: "Riverway", "Broadway", "Fairway" etc.
# Single-word road names where the suffix is concatenated.
_COMPOUND_ROAD_ENDINGS = (
    "way", "street", "road", "avenue", "boulevard", "drive", "lane",
    "parkway", "freeway", "highway", "trail",
)


def _first_field_looks_like_road(first_field: str) -> bool:
    """True if first_field ends with a known road suffix, matches the
    highway pattern (I-10, FM 2234, Hwy 6, Loop 610), or is a single-word
    compound road name (Riverway, Broadway)."""
    if not first_field:
        return False
    stripped = first_field.strip()
    if _HIGHWAY_RE.match(stripped):
        return True
    tokens = stripped.lower().rstrip(".").split()
    if not tokens:
        return False
    last = tokens[-1].rstrip(".,")
    # "Dulles Ave N" → check second-to-last if last is directional
    if last in {"n", "s", "e", "w", "ne", "nw", "se", "sw"} and len(tokens) >= 2:
        last = tokens[-2].rstrip(".,")
    if last in _ROAD_SUFFIXES:
        return True
    # Compound-word fallback: "Riverway" ends with "way", "Broadway" with "way"
    for ending in _COMPOUND_ROAD_ENDINGS:
        if last.endswith(ending) and len(last) > len(ending):
            return True
    return False


# ============================================================================
# Extended POI tokens — superpower_geo's set is too narrow for Uber's output
# ============================================================================

_EXTENDED_POI_TOKENS = set(POI_TYPE_MAP) | {
    # Airlines (Uber uses bare airline names for IAH/HOU drop-offs)
    # Southwest is tricky — matches "Southwest Airlines" as a venue, but
    # we must not false-positive on "Southwest Fwy" etc. The bare word
    # "southwest" alone is ambiguous; we match only "southwest airlines".
    "united", "southwest airlines", "delta", "american airlines", "spirit",
    "frontier", "alaska", "jetblue", "lufthansa", "british", "klm",
    "emirates", "qatar",
    # Airports (Houston: IAH = Bush Intercontinental, HOU = Hobby)
    "iah", "hou", "hobby airport", "george bush",
    # Hotel brands
    "marriott", "hilton", "hyatt", "sheraton", "westin", "ritz", "marquis",
    "galleria",
    # Other venues
    "terminal", "arena", "coliseum", "music hall", "medical center",
    "plazamericas",
}


def _contains_poi_token(first_field: str) -> bool:
    if not first_field:
        return False
    lower = first_field.lower()
    return any(token in lower for token in _EXTENDED_POI_TOKENS)


# ============================================================================
# PRIMITIVE 1 — classify_address
# ============================================================================

def classify_address(text: str) -> dict:
    """
    Classify a YOLO'd address string into a bucket.

    Returns: {"bucket": str, "parts": dict}
    Buckets: "intersection", "street_number", "single_road", "poi", "garbage"
    """
    if not text or not text.strip():
        return {"bucket": "garbage", "parts": {}}

    cleaned = _clean_ocr(text.strip())
    first_field = cleaned.split(",")[0].strip()

    # --- Street number: first token is digits + followed by road ---
    # "2727 Allen Pkwy" / "401 Franklin St"
    # Guard: "I-10" and "Hwy 6" are NOT street numbers — they're highway names.
    if not _HIGHWAY_RE.match(first_field):
        m = re.match(r"^(\d+)\s+(.+)$", first_field)
        if m:
            rest_of_address = cleaned.split(",", 1)
            city = rest_of_address[1].split(",")[0].strip() if len(rest_of_address) > 1 else ""
            return {
                "bucket": "street_number",
                "parts": {
                    "number": m.group(1),
                    "road": m.group(2).strip(),
                    "city": city,
                }
            }

    # --- Intersection: first field contains & / and / @ / + ---
    if INTERSECTION_REGEX.search(first_field):
        parts = INTERSECTION_REGEX.split(first_field, maxsplit=1)
        if len(parts) == 2 and parts[0].strip() and parts[1].strip():
            rest = cleaned.split(",", 1)
            city = rest[1].split(",")[0].strip() if len(rest) > 1 else ""
            return {
                "bucket": "intersection",
                "parts": {
                    "road_a": parts[0].strip(),
                    "road_b": parts[1].strip(),
                    "city": city,
                }
            }

    # --- Single road: first field looks like a road name ---
    if _first_field_looks_like_road(first_field):
        rest = cleaned.split(",", 1)
        city = rest[1].split(",")[0].strip() if len(rest) > 1 else ""
        return {
            "bucket": "single_road",
            "parts": {
                "road": first_field,
                "city": city,
            }
        }

    # --- POI: first field matches known venue/brand/airline token ---
    if _contains_poi_token(first_field):
        return {
            "bucket": "poi",
            "parts": {"name": first_field, "full_text": text}
        }

    # --- Garbage fallback ---
    return {
        "bucket": "garbage",
        "parts": {"raw": text}
    }


# ============================================================================
# PRIMITIVE 2 — snap_to_intersection
# ============================================================================

# Cache-key canonicalization: map all spelled-out road suffixes to their
# abbreviated forms so "4th Street" and "4th St" hit the same cache entry.
# Ordered longest-first so "Boulevard" matches before "Blvd".
_SUFFIX_CANONICAL = [
    (r"\bstreet\b",    "st"),
    (r"\bavenue\b",    "ave"),
    (r"\bboulevard\b", "blvd"),
    (r"\bdrive\b",     "dr"),
    (r"\broad\b",      "rd"),
    (r"\blane\b",      "ln"),
    (r"\bcourt\b",     "ct"),
    (r"\bplace\b",     "pl"),
    (r"\bparkway\b",   "pkwy"),
    (r"\bfreeway\b",   "fwy"),
    (r"\bhighway\b",   "hwy"),
    (r"\btrail\b",     "trl"),
    (r"\bcircle\b",    "cir"),
    (r"\bterrace\b",   "ter"),
    (r"\btrace\b",     "trce"),
    (r"\bcrossing\b",  "xing"),
    (r"\bpoint\b",     "pt"),
]


def _canonicalize_address(addr: str) -> str:
    """Normalize an address for cache keying.

    Lowercases, collapses whitespace, and abbreviates spelled-out road
    suffixes. "4th Street & Orchard Street, Missouri City" and
    "4th St & Orchard St, Missouri City" produce identical canonical forms.
    """
    s = addr.lower().strip()
    s = re.sub(r"\s+", " ", s)
    for pattern, repl in _SUFFIX_CANONICAL:
        s = re.sub(pattern, repl, s)
    return s


def _google_intersection_fallback(road_a: str, road_b: str, city: str,
                                 cur) -> Optional[dict]:
    """When houston_ways doesn't have the intersection, try Google geocoding.

    Caches results (with canonicalized key) via triangulation's cache so
    subsequent lookups — even with variant suffix wording — are free.

    Returns: {lat, lng, source, confidence} or None.
    """
    full_address = f"{road_a} & {road_b}"
    if city:
        full_address = f"{full_address}, {city}, Texas"
    canonical_key = _canonicalize_address(full_address)

    import time
    # 1. Cache lookup (fast path — no API call)
    t0 = time.perf_counter()
    cached = _lookup_geocode_cache(canonical_key, cur)
    if cached:
        ms = (time.perf_counter() - t0) * 1000
        logging.info(
            f"[BEAD][timing] google_cache_hit {ms:.0f}ms for {full_address!r}"
        )
        return {
            "lat": float(cached[0]),
            "lng": float(cached[1]),
            "road_a_match": road_a,
            "road_b_match": road_b,
            "confidence": "medium",
            "source": "google_cache",
        }

    # 2. Live Google geocode (2s timeout inside _google_geocode)
    t_google = time.perf_counter()
    coords = _google_geocode(full_address)
    google_ms = (time.perf_counter() - t_google) * 1000
    if not coords:
        logging.info(
            f"[BEAD][timing] google_live_miss {google_ms:.0f}ms for {full_address!r}"
        )
        return None
    logging.info(
        f"[BEAD][timing] google_live_hit {google_ms:.0f}ms for {full_address!r}"
    )

    # 3. Cache the result so future calls are free
    try:
        _write_geocode_cache(canonical_key, coords[0], coords[1], cur)
        cur.connection.commit()
    except Exception as cache_err:
        logging.warning(f"[BEAD] cache write failed: {cache_err}")

    return {
        "lat": float(coords[0]),
        "lng": float(coords[1]),
        "road_a_match": road_a,
        "road_b_match": road_b,
        "confidence": "medium",
        "source": "google_live",
    }


def snap_to_intersection(road_a: str, road_b: str, cur,
                         city_hint: Optional[str] = None) -> Optional[dict]:
    """Find the geometric intersection of two named road segments.

    Primary path: routing.houston_ways via trigram + PostGIS intersection.
    Fallback path: Google geocode (cached).

    Returns: {lat, lng, road_a_match, road_b_match, confidence, source} or None.
      confidence="high" when OSM intersection math actually found a crossing.
      confidence="medium" when Google geocoding filled a data gap.

    Uses idx_houston_ways_name_trgm + idx_houston_ways_geog_gist.
    """
    if not road_a or not road_b:
        return None

    try:
        cur.execute("""
            WITH a_segs AS (
                SELECT gid, the_geom, name,
                       similarity(lower(name), lower(%s)) AS sim
                FROM routing.houston_ways
                WHERE name %% %s
                ORDER BY sim DESC
                LIMIT 50
            ),
            b_segs AS (
                SELECT gid, the_geom, name,
                       similarity(lower(name), lower(%s)) AS sim
                FROM routing.houston_ways
                WHERE name %% %s
                ORDER BY sim DESC
                LIMIT 50
            )
            SELECT
                ST_Y(ST_Intersection(a.the_geom, b.the_geom)) AS lat,
                ST_X(ST_Intersection(a.the_geom, b.the_geom)) AS lng,
                a.name AS a_name,
                b.name AS b_name,
                a.sim + b.sim AS total_sim
            FROM a_segs a, b_segs b
            WHERE ST_Intersects(a.the_geom, b.the_geom)
              AND ST_GeometryType(ST_Intersection(a.the_geom, b.the_geom)) = 'ST_Point'
            ORDER BY total_sim DESC
            LIMIT 1;
        """, (road_a, road_a, road_b, road_b))
        row = cur.fetchone()
        if row:
            return {
                "lat": float(row["lat"]),
                "lng": float(row["lng"]),
                "road_a_match": row["a_name"],
                "road_b_match": row["b_name"],
                "confidence": "high",
                "source": "osm_intersection",
            }
    except Exception as e:
        logging.warning(f"[BEAD] OSM intersection query failed: {e}")

    # OSM miss — fall through to Google
    logging.info(
        f"[BEAD] OSM miss for {road_a!r} & {road_b!r} — trying Google"
    )
    return _google_intersection_fallback(
        road_a, road_b, city_hint or "", cur
    )


# ============================================================================
# PRIMITIVE 3 — snap_to_road (the rail-under-the-hand)
# ============================================================================

def snap_to_road(lat: float, lng: float, cur,
                 max_distance_m: float = 40.0) -> Optional[dict]:
    """Find the nearest named road segment to (lat, lng) within max_distance_m.

    Returns: {road_name, segment_id, snapped_lat, snapped_lng, dist_m} or None.

    None means driver is off-wire — typically inside a parking lot, driveway,
    or unnamed access road. A None after a sequence of hits is the Terminal
    Pivot signal: driver has transitioned from named road to unnamed space.

    Uses idx_houston_ways_geog_gist. Expected runtime: sub-100ms.
    """
    if lat is None or lng is None:
        return None

    try:
        cur.execute("""
            WITH anchor AS (
                SELECT app_private.coords_to_point(%s, %s) AS pt
            )
            SELECT
                hw.gid AS segment_id,
                hw.name AS road_name,
                ST_Y(ST_ClosestPoint(hw.the_geom, a.pt)) AS snap_lat,
                ST_X(ST_ClosestPoint(hw.the_geom, a.pt)) AS snap_lng,
                ST_Distance(hw.the_geom::geography,
                            a.pt::geography) AS dist_m
            FROM routing.houston_ways hw, anchor a
            WHERE hw.name IS NOT NULL
              -- Exclude corrupt OSM segments (~857 segments >20km that
              -- sprawl diagonally across the metro and produce false snaps)
              AND hw.length_m < 20000
              AND ST_DWithin(hw.the_geom::geography,
                             a.pt::geography,
                             %s)
            ORDER BY hw.the_geom <-> a.pt
            LIMIT 1;
        """, (lat, lng, max_distance_m))
        row = cur.fetchone()
        if not row:
            return None

        return {
            "road_name": row["road_name"],
            "segment_id": row["segment_id"],
            "snapped_lat": float(row["snap_lat"]),
            "snapped_lng": float(row["snap_lng"]),
            "dist_m": float(row["dist_m"]),
        }
    except Exception as e:
        logging.warning(f"[BEAD] snap_to_road failed: {e}")
        return None


# ============================================================================
# PRIMITIVE 4 — odometer_miles_since
# ============================================================================

def odometer_miles_since(driver_id: str, since_time, cur,
                         jitter_speed_mph: float = 2.0) -> float:
    """Sum of heartbeat-to-heartbeat distances since since_time.

    Filters out samples at speed < jitter_speed_mph to reject GPS drift
    while stopped. Used by Blind Man to anchor YOLO trip_miles against
    actual driven distance.

    Returns: miles driven since anchor (float, >= 0.0).

    since_time is any Postgres-parseable timestamp (datetime or string).
    """
    if not driver_id or since_time is None:
        return 0.0

    try:
        cur.execute("""
            WITH ordered AS (
                SELECT
                    lat, lng, speed_mph, logged_at,
                    LAG(lat)  OVER (ORDER BY logged_at) AS prev_lat,
                    LAG(lng)  OVER (ORDER BY logged_at) AS prev_lng
                FROM app_private.heartbeat_log
                WHERE driver_id = %s
                  AND logged_at >= %s
            )
            SELECT COALESCE(
                SUM(app_private.distance_miles(lat, lng, prev_lat, prev_lng)),
                0
            )::float AS miles
            FROM ordered
            WHERE prev_lat IS NOT NULL
              AND speed_mph >= %s;
        """, (driver_id, since_time, jitter_speed_mph))
        row = cur.fetchone()
        return float(row["miles"]) if row and row["miles"] is not None else 0.0
    except Exception as e:
        logging.warning(f"[BEAD] odometer_miles_since failed: {e}")
        return 0.0


# ============================================================================
# PRIMITIVE 5 — get_pivot_context
# ============================================================================

def get_pivot_context(driver_id: str, cur,
                      anchor_time=None) -> dict:
    """Determine whether driver is on-wire, and the last named road touched.

    Implements the "Blind Man's hand" tracking. Looks back through heartbeat
    history (bounded by anchor_time to the current ride only) to find:

      - Whether the most recent heartbeat is on a named road (on_wire)
      - The name of that current road, if any
      - The name of the last named road the driver was on (could be current)
      - The time the driver pivoted off the last named road (None if still on it)

    Returns: {on_wire: bool, current_road: str|None,
              last_named_road: str|None, pivot_time: timestamp|None}

    anchor_time bounds the lookback to the current ride (e.g., the IN_TRIP
    transition). Without it, we could pick up the last named road from a
    prior ride. Required for correctness across stacked rides.
    """
    if not driver_id:
        return {"on_wire": False, "current_road": None,
                "last_named_road": None, "pivot_time": None}

    try:
        # Query: most recent N heartbeats in this ride, with each one's snap.
        # We do the snap inline as a LATERAL join so we get road_name per tick.
        anchor_clause = ""
        params = [driver_id]
        if anchor_time is not None:
            anchor_clause = "AND hb.logged_at >= %s::timestamptz"
            params.append(anchor_time)
        params_tuple = tuple(params)

        cur.execute(f"""
            WITH recent AS (
                SELECT
                    hb.logged_at,
                    hb.lat,
                    hb.lng
                FROM app_private.heartbeat_log hb
                WHERE hb.driver_id = %s
                  {anchor_clause}
                ORDER BY hb.logged_at DESC
                LIMIT 60
            ),
            snapped AS (
                SELECT
                    r.logged_at,
                    r.lat, r.lng,
                    (
                        SELECT hw.name
                        FROM routing.houston_ways hw
                        WHERE hw.name IS NOT NULL
                          AND hw.length_m < 20000
                          AND ST_DWithin(
                            hw.the_geom::geography,
                            app_private.coords_to_point(r.lat, r.lng)::geography,
                            40
                          )
                        ORDER BY hw.the_geom <-> app_private.coords_to_point(r.lat, r.lng)
                        LIMIT 1
                    ) AS road_name
                FROM recent r
            )
            SELECT logged_at, road_name FROM snapped
            ORDER BY logged_at DESC;
        """, params_tuple)
        rows = cur.fetchall()

        if not rows:
            return {"on_wire": False, "current_road": None,
                    "last_named_road": None, "pivot_time": None}

        # Most recent heartbeat
        current = rows[0]
        on_wire = current["road_name"] is not None
        current_road = current["road_name"]

        if on_wire:
            # Still on a named road — pivot hasn't happened
            return {
                "on_wire": True,
                "current_road": current_road,
                "last_named_road": current_road,
                "pivot_time": None,
            }

        # Off-wire — walk backwards to find the last named road and when we left it
        last_named_road = None
        pivot_time = None
        for i, row in enumerate(rows):
            if row["road_name"] is not None:
                last_named_road = row["road_name"]
                # pivot_time = time of the LATER heartbeat (just after leaving)
                if i > 0:
                    pivot_time = rows[i - 1]["logged_at"]
                else:
                    pivot_time = row["logged_at"]
                break

        return {
            "on_wire": False,
            "current_road": None,
            "last_named_road": last_named_road,
            "pivot_time": pivot_time,
        }
    except Exception as e:
        logging.warning(f"[BEAD] get_pivot_context failed: {e}")
        return {"on_wire": False, "current_road": None,
                "last_named_road": None, "pivot_time": None}


# ============================================================================
# PRIMITIVE 6 — detect_cluster
# ============================================================================

def detect_cluster(driver_id: str, cur,
                   window_sec: int = 60,
                   min_samples: int = 3,
                   max_speed_mph: float = 10.0,
                   max_spread_m: float = 25.0) -> Optional[dict]:
    """Check if driver has a tight low-speed cluster in the recent past.

    Cluster criteria (all must hold):
      - >= min_samples heartbeats in the last window_sec seconds
      - all at speed < max_speed_mph
      - all within max_spread_m of the cluster median

    Returns: {n, median_lat, median_lng, spread_m, duration_sec} or None.

    None means driver isn't in a stopped cluster — blind man should hold.
    """
    if not driver_id:
        return None

    try:
        # Find the most recent CONSECUTIVE run of low-speed heartbeats.
        # This is the "current stopped state" — not "everything slow in the
        # last minute" which can conflate a red light earlier with a curb
        # stop now.
        cur.execute("""
            WITH recent AS (
                SELECT lat, lng, speed_mph, logged_at
                FROM app_private.heartbeat_log
                WHERE driver_id = %s
                  AND logged_at >= NOW() - make_interval(secs => %s)
                ORDER BY logged_at DESC
            ),
            tagged AS (
                SELECT *,
                       SUM(CASE WHEN speed_mph >= %s THEN 1 ELSE 0 END)
                         OVER (ORDER BY logged_at DESC
                               ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
                         AS breaks_before
                FROM recent
            ),
            current_run AS (
                SELECT lat, lng, speed_mph, logged_at
                FROM tagged
                WHERE breaks_before = 0    -- zero high-speed samples between
                                            -- this row and the most recent one
                  AND speed_mph < %s
            )
            SELECT
                COUNT(*)::int AS n,
                percentile_cont(0.5) WITHIN GROUP (ORDER BY lat)  AS median_lat,
                percentile_cont(0.5) WITHIN GROUP (ORDER BY lng)  AS median_lng,
                MIN(logged_at) AS earliest,
                MAX(logged_at) AS latest
            FROM current_run;
        """, (driver_id, window_sec, max_speed_mph, max_speed_mph))
        row = cur.fetchone()
        if not row or row["n"] is None or row["n"] < min_samples:
            return None

        median_lat = float(row["median_lat"])
        median_lng = float(row["median_lng"])

        # Spread check — spread of CURRENT low-speed run only
        cur.execute("""
            WITH recent AS (
                SELECT lat, lng, speed_mph, logged_at
                FROM app_private.heartbeat_log
                WHERE driver_id = %s
                  AND logged_at >= NOW() - make_interval(secs => %s)
                ORDER BY logged_at DESC
            ),
            tagged AS (
                SELECT *,
                       SUM(CASE WHEN speed_mph >= %s THEN 1 ELSE 0 END)
                         OVER (ORDER BY logged_at DESC
                               ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
                         AS breaks_before
                FROM recent
            )
            SELECT MAX(
                app_private.distance_miles(lat, lng, %s, %s) * 1609.34
            ) AS max_dist_m
            FROM tagged
            WHERE breaks_before = 0 AND speed_mph < %s;
        """, (driver_id, window_sec, max_speed_mph,
              median_lat, median_lng, max_speed_mph))
        sr = cur.fetchone()
        if not sr or sr["max_dist_m"] is None:
            return None
        spread_m = float(sr["max_dist_m"])
        if spread_m > max_spread_m:
            return None

        duration_sec = (row["latest"] - row["earliest"]).total_seconds()
        return {
            "n": int(row["n"]),
            "median_lat": median_lat,
            "median_lng": median_lng,
            "spread_m": spread_m,
            "duration_sec": duration_sec,
        }
    except Exception as e:
        logging.warning(f"[BEAD] detect_cluster failed: {e}")
        return None


# ============================================================================
# PRIMITIVE 7 — compute_target (the orchestrator)
# ============================================================================

# Tortuosity lower bound — driver must have driven at least this fraction of
# the Uber-reported distance before Blind Man is eligible to fire. No upper
# bound: detours (trains, construction) can extend actual miles well beyond
# the reported trip_miles, and we should still fire when the cluster forms
# near the target road.
_BLIND_MAN_TORT_MIN = 0.9


def _road_names_match(snapped_name: str, address_road: str) -> bool:
    """Fuzzy match between an OSM road name and a YOLO'd address road name.

    Both get lowercased and abbreviation-normalized via _canonicalize_address.
    Match if either contains the other (to absorb OSM adding directional or
    county qualifiers that Uber's text doesn't include, and vice versa).
    """
    if not snapped_name or not address_road:
        return False
    s = _canonicalize_address(snapped_name).strip()
    a = _canonicalize_address(address_road).strip()
    if not s or not a:
        return False
    return s in a or a in s


def compute_target(
    address_text: str,
    anchor_time,
    expected_miles: float,
    driver_id: str,
    cur,
    city_hint: Optional[str] = None,
) -> Optional[dict]:
    """DIAGNOSE orchestrator. Produces a target lat/lng for check_convergence,
    or None (blue frog) if the system can't commit to a high-confidence answer.

    Args:
      address_text:   YOLO'd pickup or dropoff address string
      anchor_time:    timestamp the current phase of the ride started
                      (offer-accept for pickup, pickup-nail for dropoff).
                      Used for odometer reference and pivot-context bound.
      expected_miles: Uber-reported trip_miles or pickup_miles
      driver_id:      driver's UID
      cur:            DB cursor
      city_hint:      optional, improves Google fallback query quality

    Returns: {lat, lng, source, confidence, tier, reason} or None.
    """
    if not address_text:
        logging.info("[BEAD] compute_target: empty address_text → HOLD")
        return None

    classification = classify_address(address_text)
    bucket = classification["bucket"]
    parts = classification["parts"]

    logging.info(f"[BEAD] classify: {address_text!r} → {bucket}")

    # --- INTERSECTION bucket ---
    if bucket == "intersection":
        road_a = parts.get("road_a")
        road_b = parts.get("road_b")
        city = parts.get("city") or city_hint
        result = snap_to_intersection(road_a, road_b, cur, city_hint=city)
        if result:
            return {
                "lat": result["lat"],
                "lng": result["lng"],
                "source": result.get("source", "intersection_snap"),
                "confidence": result.get("confidence", "high"),
                "tier": "high",
                "reason": f"intersection {road_a} & {road_b}",
            }
        logging.info(f"[BEAD] intersection snap failed for {road_a!r} & {road_b!r} → HOLD")
        return None

    # --- STREET_NUMBER bucket: defer to upstream Google geocode ---
    # The caller already has (or can get) a Google-geocoded lat/lng for
    # numbered addresses. That's Google's sweet spot. Bead-on-wire doesn't
    # improve on it; returning None tells the caller to keep its existing
    # target_lat/target_lng.
    if bucket == "street_number":
        logging.info(f"[BEAD] street_number: {address_text!r} → defer to existing geocode")
        return None

    # --- POI bucket: run Blind Man without road-name gate ---
    # POI addresses (Walmart, hotels, airports) don't have a road name to
    # match against. But the odometer floor + off-wire cluster still give
    # us a strong signal that driver has reached the POI and stopped.
    # Tier is "medium" — we're skipping one gate compared to single_road.
    if bucket == "poi":
        odometer = odometer_miles_since(driver_id, anchor_time, cur)
        min_miles = expected_miles * _BLIND_MAN_TORT_MIN if expected_miles else 0.0
        if expected_miles and odometer < min_miles:
            logging.info(
                f"[BEAD] poi {address_text!r}: odometer {odometer:.2f}mi "
                f"< floor {min_miles:.2f}mi → HOLD"
            )
            return None

        pivot = get_pivot_context(driver_id, cur, anchor_time=anchor_time)
        # POI fires only when off-wire — on-wire means we're still driving
        # on a named road, not at the POI yet.
        if pivot.get("on_wire"):
            logging.info(
                f"[BEAD] poi {address_text!r}: still on-wire "
                f"({pivot.get('current_road')!r}) → HOLD"
            )
            return None

        # Off-wire + past odometer floor — check for cluster. Use relaxed
        # spread (same as off-wire single_road) since parking lots allow
        # drop-off-line-style creep.
        cluster = detect_cluster(driver_id, cur, max_spread_m=70.0)
        if not cluster:
            logging.info(
                f"[BEAD] poi {address_text!r}: no cluster yet → HOLD"
            )
            return None

        logging.info(
            f"[BEAD] ✅ poi FIRE: {address_text!r}, odometer {odometer:.2f}mi, "
            f"off-wire (pivot from {pivot.get('last_named_road')!r}), "
            f"cluster n={cluster['n']} spread={cluster['spread_m']:.0f}m"
        )
        return {
            "lat": cluster["median_lat"],
            "lng": cluster["median_lng"],
            "source": "blind_man_poi",
            "confidence": "medium",
            "tier": "medium",
            "reason": (
                f"poi {address_text[:40]}, odometer={odometer:.1f}mi, "
                f"off-wire cluster n={cluster['n']}"
            ),
        }

    # --- GARBAGE bucket: blue frog ---
    if bucket == "garbage":
        logging.info(f"[BEAD] garbage: {address_text!r} → HOLD")
        return None

    # --- SINGLE_ROAD bucket: full Blind Man logic ---
    if bucket == "single_road":
        address_road = parts.get("road")
        if not address_road:
            logging.info("[BEAD] single_road: no road name extracted → HOLD")
            return None

        # Gate 1: odometer floor. Must have driven >= 90% of reported miles.
        odometer = odometer_miles_since(driver_id, anchor_time, cur)
        min_miles = expected_miles * _BLIND_MAN_TORT_MIN if expected_miles else 0.0
        if expected_miles and odometer < min_miles:
            logging.info(
                f"[BEAD] single_road {address_road!r}: odometer {odometer:.2f}mi "
                f"< floor {min_miles:.2f}mi (trip={expected_miles}mi) → HOLD"
            )
            return None

        # Gate 2: pivot context. Either currently on matching road (curb
        # dropoff) OR last-named-road before pivot matches (parking lot).
        pivot = get_pivot_context(driver_id, cur, anchor_time=anchor_time)
        candidate_road = pivot.get("current_road") if pivot.get("on_wire")                          else pivot.get("last_named_road")
        if not candidate_road:
            logging.info(
                f"[BEAD] single_road {address_road!r}: no candidate_road "
                f"(on_wire={pivot.get('on_wire')}) → HOLD"
            )
            return None
        if not _road_names_match(candidate_road, address_road):
            logging.info(
                f"[BEAD] single_road {address_road!r}: candidate_road "
                f"{candidate_road!r} does not match → HOLD"
            )
            return None

        # Gate 3: cluster formed. Tight, low-speed, sustained.
        # Context-aware spread: tight (25m) when on-wire matching the target
        # road — curb stop. Relaxed (70m) when off-wire — parking lot creep
        # line, school drop-off queue, long apartment driveway.
        spread_limit = 25.0 if pivot.get("on_wire") else 70.0
        cluster = detect_cluster(driver_id, cur, max_spread_m=spread_limit)
        if not cluster:
            logging.info(
                f"[BEAD] single_road {address_road!r}: no cluster yet → HOLD"
            )
            return None

        # All three gates passed. Fire at cluster median.
        tier = "high" if pivot.get("on_wire") else "medium"
        logging.info(
            f"[BEAD] ✅ single_road FIRE: {address_road!r} matched "
            f"{candidate_road!r}, odometer {odometer:.2f}mi, "
            f"cluster n={cluster['n']} spread={cluster['spread_m']:.0f}m "
            f"(limit {spread_limit:.0f}m) pivot={'on_wire' if pivot.get('on_wire') else 'off_wire'}"
        )
        return {
            "lat": cluster["median_lat"],
            "lng": cluster["median_lng"],
            "source": "blind_man",
            "confidence": tier,
            "tier": tier,
            "reason": (
                f"single_road match {address_road}→{candidate_road}, "
                f"odometer={odometer:.1f}mi, cluster n={cluster['n']}"
            ),
        }

    # Unknown bucket — shouldn't happen, but fail safe
    logging.warning(f"[BEAD] unknown bucket {bucket!r} → HOLD")
    return None


# ============================================================================
# Additional primitives live below once written.
# ============================================================================
