"""road_membership — Google reverse geocode → cluster road membership, cached.

The "Path B.2" pattern (ratified 2026-05-15): defer road-name resolution
to Google (the authoritative source for road canonicalization globally),
cache verdicts by (cluster_h3_r10, target_road_canonical) so we pay the
Google tax once per spatial-cell × offer-road pair.

Replaces the locally-maintained suffix/directional canonicalization
approach (Path B.1 / Geometric Handshake against houston_ways) that
required ongoing maintenance of Houston-specific abbreviation tables.
Google already canonicalizes globally; we just need to ask once per
(H3 cell, offer text) tuple and remember the answer.

Public API:
    is_cluster_on_road(cur, cluster, target_road_text) -> bool

Defensive return False on: None cluster, NULL coords, empty road text,
API errors, ZERO_RESULTS, REQUEST_DENIED. Never raises (caller does
not need to wrap in try/except).

Coverage:
    Suffix mismatches    (Dr/Drive/Road)         — handled via base-name strip
    Directional prefixes (W/West, N/North)       — handled via short_name path
    Abbreviation forms   (Fwy/Freeway, Hwy/Highway) — handled via short_name
    Numbered routes      (FM/Hwy/Interstate)     — partial; depends on Google
                                                   returning matching short_name

Forensic columns persisted to road_membership_cache:
    cluster_lat/lng           — which actual cluster centroid drove this row
    google_route_canonical    — the form Google returned that matched
    google_place_id           — route-typed place_id (durable road identity)
    target_road_raw           — original offer text (for "did Uber typo this?"
                                diagnostics)
"""
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional


log = logging.getLogger(__name__)


# ============================================================================
# Constants (canonical configuration)
# ============================================================================

# H3 resolution for cache keying. R10 = ~175m hex diameter, ~65m edge.
# Empirically matches the geometric tolerance Path B targeted before the
# switch to Google. GPS jitter across visits usually lands in the same
# hex; centroids 100m apart land in adjacent hexes (cache miss the first
# time at each, then both hit).
ROAD_MEMBERSHIP_H3_RESOLUTION = 10

# Cache TTL. Roads are durable infrastructure; matches §XVII Semantic
# Anchor cache TTL (365 days) for operational consistency.
ROAD_MEMBERSHIP_CACHE_TTL_DAYS = 365

# Google Geocoding API endpoint and call parameters.
_GOOGLE_GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
_GOOGLE_API_TIMEOUT_S = 15
_GOOGLE_USER_AGENT = "puddlejumper-road-membership/1.0"

# API key environment variables, checked in priority order. Mirrors the
# probe script and the existing poi_service convention.
_API_KEY_ENV_VARS = (
    "GOOGLE_PLACES_API_KEY",
    "GOOGLE_MAPS_API_KEY",
    "GOOGLE_GEOCODING_API_KEY",
    "GOOGLE_API_KEY",
)

# Road-suffix tokens we strip for canonical base-name comparison.
# Single source of truth: Google is the authority once we get a hit;
# we only need to strip suffixes well enough to recognize when "Drive"
# and "Dr" refer to the same road.
_SUFFIX_TOKENS = frozenset({
    "dr", "drive",
    "rd", "road",
    "st", "street",
    "ave", "avenue", "av",
    "blvd", "boulevard",
    "ln", "lane",
    "ct", "court",
    "pkwy", "parkway",
    "fwy", "freeway",
    "hwy", "highway",
    "way", "trl", "trail",
    "cir", "circle",
    "pl", "place",
    "ter", "terrace",
    "loop", "row",
})


# ============================================================================
# Pure helpers — canonicalization and route extraction
# ============================================================================

def _canonicalize_road_text(road: str) -> str:
    """Lowercase, strip trailing suffix tokens, collapse whitespace.

    Examples:
        "Watts Plantation Dr"     -> "watts plantation"
        "Watts Plantation Drive"  -> "watts plantation"
        "Watts Plantation Road"   -> "watts plantation"
        "WATTS PLANTATION DR."    -> "watts plantation"
        "FM 1960"                 -> "fm 1960"   (no recognized suffix)
        ""                        -> ""
        None                      -> ""

    The trailing strip iterates so chained suffixes like "Watts Plantation
    Dr Rd" (unusual but possible in user-typed data) reduce correctly.
    """
    if not road:
        return ""
    tokens = road.lower().strip().split()
    while tokens and tokens[-1].rstrip(".,") in _SUFFIX_TOKENS:
        tokens.pop()
    return " ".join(tokens)


def _extract_route_candidates(response: dict) -> list:
    """Pull (long_name, short_name, place_id) tuples from a Google
    geocode response.

    Skips results without a route component (plus codes, localities,
    postal codes). Empty route names suppressed. Both long_name and
    short_name preserved so the matcher can check either form — this
    closes the directional gap ("North Shepherd Drive" / "N Shepherd
    Dr") at zero additional API cost since Google returns both in the
    same response.
    """
    out = []
    for result in response.get("results", []):
        place_id = result.get("place_id", "")
        long_name = ""
        short_name = ""
        for comp in result.get("address_components", []):
            if "route" in comp.get("types", []):
                long_name = comp.get("long_name", "")
                short_name = comp.get("short_name", "")
                break
        if long_name or short_name:
            out.append((long_name, short_name, place_id))
    return out


def _pick_matching_route(
    candidates: list,
    target_canonical: str,
) -> Optional[tuple]:
    """Find the first candidate whose canonical long OR short name equals
    target_canonical.

    Returns (matched_form, place_id) on hit; None on miss. Google returns
    results in relevance order, so first-match-wins gives the most
    relevant route.

    Checking BOTH long_name and short_name closes the directional gap:
        long_name="North Shepherd Drive" + short_name="N Shepherd Dr"
        target "N Shepherd Dr" -> canonical "n shepherd" matches short_name
                                  (long form's canonical is "north shepherd").
    """
    for long_name, short_name, place_id in candidates:
        for form in (long_name, short_name):
            if form and _canonicalize_road_text(form) == target_canonical:
                return (form, place_id)
    return None


# ============================================================================
# Environment / API
# ============================================================================

def _get_api_key() -> Optional[str]:
    """Return the Google API key from the environment, or None.

    Checks env vars in priority order. None propagates to
    is_cluster_on_road returning False (with WARNING log).
    """
    for var in _API_KEY_ENV_VARS:
        key = os.environ.get(var)
        if key:
            return key
    return None


def _reverse_geocode(
    lat: float,
    lng: float,
    api_key: str,
) -> Optional[dict]:
    """Call Google Geocoding reverse endpoint. Returns parsed dict or
    None on network failure.

    Status-level errors (REQUEST_DENIED, OVER_QUERY_LIMIT, ZERO_RESULTS)
    are returned INSIDE the dict so the caller can inspect the specific
    status string. Caller treats any non-OK status as "miss, don't cache".
    """
    params = {
        "latlng": "{},{}".format(lat, lng),
        "key": api_key,
    }
    url = "{}?{}".format(
        _GOOGLE_GEOCODE_URL, urllib.parse.urlencode(params),
    )
    req = urllib.request.Request(
        url, headers={"User-Agent": _GOOGLE_USER_AGENT},
    )
    try:
        with urllib.request.urlopen(
            req, timeout=_GOOGLE_API_TIMEOUT_S,
        ) as resp:
            body = resp.read().decode("utf-8")
        return json.loads(body)
    except urllib.error.HTTPError as e:
        log.warning(
            "[road_membership] HTTP %d from Geocoding API for (%s, %s)",
            e.code, lat, lng,
        )
        return None
    except Exception:
        log.warning(
            "[road_membership] Geocoding call failed for (%s, %s)",
            lat, lng, exc_info=True,
        )
        return None


# ============================================================================
# Cache I/O
# ============================================================================

_H3_SQL = "SELECT app_private.coords_to_h3(%s, %s, %s) AS h3"

_CACHE_READ_SQL = """
    SELECT on_road
    FROM app_private.road_membership_cache
    WHERE cluster_h3 = %s
      AND target_road_canonical = %s
      AND expires_at > (NOW() AT TIME ZONE 'UTC')
"""

# Throttle last_hit_at updates to once per hour per row. Matches
# poi_service convention; avoids index churn on a hot cell.
_CACHE_HIT_TOUCH_SQL = """
    UPDATE app_private.road_membership_cache
    SET last_hit_at = (NOW() AT TIME ZONE 'UTC')
    WHERE cluster_h3 = %s
      AND target_road_canonical = %s
      AND (
          last_hit_at IS NULL
          OR last_hit_at < (NOW() AT TIME ZONE 'UTC') - INTERVAL '1 hour'
      )
"""

_CACHE_WRITE_SQL = """
    INSERT INTO app_private.road_membership_cache (
        cluster_h3, target_road_canonical, on_road,
        google_route_canonical, google_place_id,
        cluster_lat, cluster_lng, target_road_raw,
        cached_at, expires_at
    )
    VALUES (
        %s, %s, %s,
        %s, %s,
        %s, %s, %s,
        (NOW() AT TIME ZONE 'UTC'),
        (NOW() AT TIME ZONE 'UTC') + INTERVAL '365 days'
    )
    ON CONFLICT (cluster_h3, target_road_canonical) DO NOTHING
"""


def _row_value(row, key: str, fallback_index: int = 0):
    """Read a value from a psycopg cursor row, handling both RealDictRow
    (dict-like) and the default tuple cursor.
    """
    if isinstance(row, dict):
        if key in row:
            return row[key]
        # Fallback for queries without explicit AS alias.
        return next(iter(row.values()), None)
    return row[fallback_index]


def _compute_cluster_h3(cur, lat: float, lng: float) -> Optional[str]:
    """Compute the R10 H3 cell for a cluster centroid via canonical fn.

    Returns text representation. None on SQL failure (which propagates
    to is_cluster_on_road returning False).
    """
    try:
        cur.execute(
            _H3_SQL,
            (float(lat), float(lng), ROAD_MEMBERSHIP_H3_RESOLUTION),
        )
        row = cur.fetchone()
    except Exception:
        log.warning(
            "[road_membership] coords_to_h3 failed for (%s, %s)",
            lat, lng, exc_info=True,
        )
        return None
    if not row:
        return None
    cell = _row_value(row, "h3")
    return cell if cell else None


def _read_cache(
    cur, cluster_h3: str, target_canonical: str,
) -> Optional[bool]:
    """Return cached verdict, or None on miss/expired.

    True/False are real verdicts; None means "not cached" and caller
    must call Google. Hit-time touch is fire-and-forget — a failure
    there does not affect the returned verdict.
    """
    try:
        cur.execute(_CACHE_READ_SQL, (cluster_h3, target_canonical))
        row = cur.fetchone()
    except Exception:
        log.warning(
            "[road_membership] cache read failed for h3=%s target=%r",
            cluster_h3, target_canonical, exc_info=True,
        )
        return None
    if not row:
        return None
    on_road = _row_value(row, "on_road")
    try:
        cur.execute(_CACHE_HIT_TOUCH_SQL, (cluster_h3, target_canonical))
    except Exception:
        log.warning(
            "[road_membership] cache hit-touch failed", exc_info=True,
        )
    return bool(on_road)


def _write_cache(
    cur,
    cluster_h3: str,
    target_canonical: str,
    target_raw: str,
    on_road: bool,
    google_route: Optional[str],
    google_place_id: Optional[str],
    cluster_lat: float,
    cluster_lng: float,
) -> None:
    """Idempotent INSERT. Conflict on (cluster_h3, target_canonical)
    is silent — first writer wins; concurrent writers see DO NOTHING.
    """
    try:
        cur.execute(_CACHE_WRITE_SQL, (
            cluster_h3, target_canonical, on_road,
            google_route, google_place_id,
            cluster_lat, cluster_lng, target_raw,
        ))
    except Exception:
        log.warning(
            "[road_membership] cache write failed for h3=%s target=%r",
            cluster_h3, target_canonical, exc_info=True,
        )


# ============================================================================
# Public API
# ============================================================================

def is_cluster_on_road(cur, cluster, target_road_text: str) -> bool:
    """Is the cluster's centroid on the road named in the offer text?

    Authoritative answer via Google reverse geocoding, cached at
    (H3 R10 cell × suffix-stripped offer road). Defers to cached
    verdicts when present; otherwise calls Google once and caches the
    result for ROAD_MEMBERSHIP_CACHE_TTL_DAYS.

    Args:
        cur: psycopg cursor (caller-injected, request-scoped).
        cluster: object with .median_lat and .median_lng attributes
                 (production Cluster dataclass or any compatible shape).
        target_road_text: the offer's road text (e.g. "Watts Plantation Dr").

    Returns:
        True  if Google's reverse-geocode of cluster centroid produces
              a route whose canonical base-name matches the offer's.
        False otherwise. Includes all defensive paths (None cluster,
              NULL coords, empty road, API failure, ZERO_RESULTS, no
              matching route). Never raises.

    Failure mode: conservative miss. If we can't verify, we don't claim
    membership, and the matcher proceeds on the other signals — same
    behavior as before this primitive existed.
    """
    # Defensive validation. None checks first so we never call SQL with
    # garbage inputs.
    if cluster is None or not target_road_text:
        return False
    lat = getattr(cluster, "median_lat", None)
    lng = getattr(cluster, "median_lng", None)
    if lat is None or lng is None:
        return False

    target_canonical = _canonicalize_road_text(target_road_text)
    if not target_canonical:
        return False

    # Step 1: H3 cell for cache keying.
    cluster_h3 = _compute_cluster_h3(cur, lat, lng)
    if cluster_h3 is None:
        return False

    # Step 2: cache lookup.
    cached = _read_cache(cur, cluster_h3, target_canonical)
    if cached is not None:
        return cached

    # Step 3: cache miss → call Google.
    api_key = _get_api_key()
    if not api_key:
        log.warning(
            "[road_membership] no Google API key in environment; "
            "returning False without verification "
            "(tried %s)", ", ".join(_API_KEY_ENV_VARS),
        )
        return False

    response = _reverse_geocode(lat, lng, api_key)
    if response is None:
        return False  # network/HTTP error already logged

    status = response.get("status", "UNKNOWN")
    if status != "OK":
        log.warning(
            "[road_membership] Google status=%s for (%s, %s) target=%r",
            status, lat, lng, target_road_text,
        )
        # Don't cache failure responses — quota/permission errors are
        # transient; we want to retry next time, not stale-cache them.
        return False

    # Step 4: extract candidates, find match (long_name OR short_name).
    candidates = _extract_route_candidates(response)
    match = _pick_matching_route(candidates, target_canonical)
    on_road = match is not None
    google_route = match[0] if match else None
    google_place_id = match[1] if match else None

    # Step 5: persist verdict. Even False verdicts cache — they save the
    # Google call next time the same question is asked at the same cell.
    _write_cache(
        cur, cluster_h3, target_canonical, target_road_text,
        on_road, google_route, google_place_id,
        float(lat), float(lng),
    )

    return on_road
