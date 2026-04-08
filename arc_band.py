# backend/arc_band.py
# VERSION: 2.0 - Arc Band Geocoding Correction
#
# Two-layer defense against geocoder errors:
#   Layer 1: Distance gate — catches Class A hallucinations (>2mi error)
#            Pure math, microseconds, no external calls.
#   Layer 2: Street sweep — resolves actual dropoff position on street geometry
#            Used for BOTH red zone drift (Class B) AND hallucination recovery.
#
# v2.0 CHANGE: Removed snap-to-pickup on hallucination. Layer 1 no longer
#   short-circuits — it falls through to Layer 2 to resolve the dropoff
#   using street geometry + arc band intersection. If street can't be
#   resolved, returns None coordinates for OCR-only evaluation.

import math
import json
import logging
import urllib.request
import urllib.parse
from typing import Optional, Dict, List, Tuple

# Tortuosity range: driving distance / straight-line distance
TORT_MIN = 0.9   # p01 — near-straight freeway routes (data-driven from 554 rides)
TORT_MAX = 2.5   # p95 — covers 95% of Houston route shapes (data-driven)

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
OVERPASS_TIMEOUT = 5  # seconds

SAMPLES_PER_SEGMENT = 50  # Balance between precision and speed


# ======================================================================
# MATH HELPERS
# ======================================================================

def haversine(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Straight-line distance in miles between two GPS points."""
    R = 3958.8  # Earth radius in miles
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlng / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(a))


# ======================================================================
# LAYER 1: DISTANCE GATE
# ======================================================================

def check_distance_gate(
    pickup_lat: float, pickup_lng: float,
    dropoff_lat: float, dropoff_lng: float,
    trip_miles: float
) -> Dict:
    """
    Fast pre-check: is the geocoded dropoff plausibly reachable
    in trip_miles driving distance?

    Returns dict with:
      - is_hallucination: bool
      - geocoded_distance: float (straight-line mi from pickup to dropoff)
      - arc_inner: float
      - arc_outer: float
      - ratio: float (how far outside band, 1.0 = on edge)
    """
    if trip_miles <= 0 or (dropoff_lat == 0 and dropoff_lng == 0):
        return {"is_hallucination": False, "reason": "no_data"}

    geocoded_distance = haversine(pickup_lat, pickup_lng, dropoff_lat, dropoff_lng)
    arc_inner = trip_miles / TORT_MAX
    arc_outer = trip_miles / TORT_MIN

    ratio = geocoded_distance / arc_outer if arc_outer > 0 else 999

    is_hallucination = geocoded_distance > arc_outer * 1.5  # 50% margin beyond max

    return {
        "is_hallucination": is_hallucination,
        "geocoded_distance": round(geocoded_distance, 3),
        "arc_inner": round(arc_inner, 3),
        "arc_outer": round(arc_outer, 3),
        "ratio": round(ratio, 2),
        "reason": "distance_exceeds_band" if is_hallucination else "within_band"
    }


# ======================================================================
# STREET GEOMETRY: CACHE + OVERPASS
# ======================================================================

def _normalize_street_name(name: str) -> str:
    """Normalize street name for cache lookups."""
    if not name:
        return ""
    # Strip common suffixes for matching, keep original for Overpass
    return name.strip().lower()


def _get_region_h3(lat: float, lng: float, cur) -> str:
    """Get H3 resolution-4 hex for regional cache grouping."""
    try:
        cur.execute(
            "SELECT app_private.coords_to_h3(%s, %s, 4)",
            (lat, lng)
        )
        row = cur.fetchone()
        if row:
            return row[0] if isinstance(row, tuple) else row.get('h3_latlng_to_cell', '')
        return ""
    except Exception:
        pass
    return ""


def _fetch_from_cache(street_name: str, region_h3: str, cur) -> Optional[List]:
    """Try to get street geometry from cache. Returns list of segments or None."""
    try:
        cur.execute("""
            SELECT geometry FROM app_private.street_geometry_cache
            WHERE street_name = %s AND region_h3 = %s AND expires_at > NOW()
        """, (_normalize_street_name(street_name), region_h3))
        row = cur.fetchone()
        if row:
            geom = row[0] if isinstance(row, tuple) else row.get('geometry')
            if isinstance(geom, str):
                geom = json.loads(geom)
            return geom  # List of segments: [[lat,lng], [lat,lng], ...]
    except Exception as e:
        logging.warning(f"Cache lookup failed: {e}")
    return None


def _store_in_cache(street_name: str, region_h3: str, segments: List,
                    osm_way_ids: List[int], cur, conn) -> None:
    """Store street geometry in cache."""
    try:
        cur.execute("""
            INSERT INTO app_private.street_geometry_cache
                (street_name, region_h3, geometry, osm_way_ids, node_count)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (street_name, region_h3) DO UPDATE SET
                geometry = EXCLUDED.geometry,
                osm_way_ids = EXCLUDED.osm_way_ids,
                node_count = EXCLUDED.node_count,
                fetched_at = NOW(),
                expires_at = NOW() + INTERVAL '90 days'
        """, (
            _normalize_street_name(street_name),
            region_h3,
            json.dumps(segments),
            osm_way_ids,
            sum(len(s) for s in segments)
        ))
        conn.commit()
    except Exception as e:
        logging.warning(f"Cache store failed: {e}")
        try:
            conn.rollback()
        except Exception:
            pass


def _fetch_from_overpass(street_name: str, center_lat: float, center_lng: float,
                         bbox_margin: float = 0.03) -> Optional[Tuple[List, List[int]]]:
    """
    Fetch street geometry from Overpass API.
    Returns (segments, osm_way_ids) or None on failure.
    Segments = list of lists of [lat, lng] pairs.
    """
    south = center_lat - bbox_margin
    north = center_lat + bbox_margin
    west = center_lng - bbox_margin
    east = center_lng + bbox_margin

    # Extract the core street name for fuzzy matching
    # e.g., "Creston Dr" -> search for "Creston"
    search_name = street_name.strip()
    # Remove common suffixes for broader matching
    for suffix in [' St', ' Street', ' Rd', ' Road', ' Dr', ' Drive',
                   ' Blvd', ' Boulevard', ' Ave', ' Avenue', ' Ln', ' Lane',
                   ' Ct', ' Court', ' Pl', ' Place', ' Way', ' Pkwy',
                   ' Parkway', ' Fwy', ' Freeway', ' Hwy', ' Highway']:
        if search_name.lower().endswith(suffix.lower()):
            search_name = search_name[:len(search_name) - len(suffix)]
            break

    query = f"""
[out:json][timeout:{OVERPASS_TIMEOUT}];
(
  way["name"~"{search_name}",i]({south},{west},{north},{east});
  way["alt_name"~"{search_name}",i]({south},{west},{north},{east});
  way["ref"~"{search_name}",i]({south},{west},{north},{east});
);
out geom;
"""

    try:
        req_data = urllib.parse.urlencode({"data": query}).encode("utf-8")
        req = urllib.request.Request(OVERPASS_URL, data=req_data, method="POST")
        with urllib.request.urlopen(req, timeout=OVERPASS_TIMEOUT + 2) as response:
            raw = response.read()
            data = json.loads(raw)

        elements = data.get("elements", [])
        if not elements:
            return None

        all_segments = []
        osm_ids = []
        aliases = set()

        for way in elements:
            osm_ids.append(way.get("id", 0))
            tags = way.get("tags", {})
            for tag in ["name", "alt_name", "ref", "short_name", "old_name"]:
                val = tags.get(tag, "")
                if val:
                    aliases.add(val.strip().lower())
            geom = way.get("geometry", [])
            if len(geom) >= 2:
                segment = [[pt["lat"], pt["lon"]] for pt in geom]
                all_segments.append(segment)

        if not all_segments:
            return None

        return (all_segments, osm_ids, aliases)

    except Exception as e:
        logging.warning(f"Overpass fetch failed for '{street_name}': {e}")
        return None


def get_street_geometry(street_name: str, center_lat: float, center_lng: float,
                        cur, conn, bbox_margin: float = 0.03) -> Optional[List]:
    """
    Get street geometry — cache first, Overpass fallback.
    Returns list of segments (each segment = list of [lat, lng] pairs), or None.

    Args:
        bbox_margin: Overpass bounding box margin in degrees.
                     Default 0.03 (~2mi). Use 0.08 (~5.5mi) for hallucination recovery.
    """
    if not street_name:
        return None

    region_h3 = _get_region_h3(center_lat, center_lng, cur)

    # Try cache
    cached = _fetch_from_cache(street_name, region_h3, cur)
    if cached is not None:
        logging.info(f"🗺️ Cache hit for '{street_name}' in region {region_h3}")
        return cached

    # houston_ways exact name match (1M+ edges, GiST indexed, already used for pickup)
    try:
        cur.execute("""
            SELECT ST_AsGeoJSON(the_geom) AS geojson
            FROM routing.houston_ways
            WHERE lower(name) = lower(%s)
              AND ST_DWithin(the_geom::geography,
                             app_private.coords_to_geography(%s, %s),
                             10000)
              AND the_geom IS NOT NULL
            LIMIT 100
        """, (_normalize_street_name(street_name), center_lat, center_lng))
        rows = cur.fetchall()
        if rows:
            segments = [
                [[coord[1], coord[0]] for coord in
                 json.loads(row[0] if isinstance(row, tuple) else row['geojson'])['coordinates']]
                for row in rows
            ]
            logging.info(f"🏙️ houston_ways hit for '{street_name}' — {len(segments)} segments")
            _store_in_cache(street_name, region_h3, segments, [], cur, conn)
            return segments
    except Exception as e:
        logging.warning(f"houston_ways exact lookup failed: {e}")

    # houston_ways proximity fallback (replaces street_network proximity)
    try:
        cur.execute("""
            SELECT ST_AsGeoJSON(the_geom) AS geojson
            FROM routing.houston_ways
            WHERE ST_DWithin(the_geom::geography,
                             app_private.coords_to_geography(%s, %s),
                             2000)
              AND the_geom IS NOT NULL
            ORDER BY the_geom::geography <-> app_private.coords_to_geography(%s, %s)
            LIMIT 50
        """, (center_lat, center_lng, center_lat, center_lng))
        rows = cur.fetchall()
        if rows:
            segments = [
                [[coord[1], coord[0]] for coord in
                 json.loads(row[0] if isinstance(row, tuple) else row['geojson'])['coordinates']]
                for row in rows
            ]
            logging.info(f"📍 houston_ways proximity hit — {len(segments)} segments")
            _store_in_cache(street_name, region_h3, segments, [], cur, conn)
            return segments
    except Exception as e:
        logging.warning(f"houston_ways proximity lookup failed: {e}")

    # Overpass disabled — houston_ways covers all of Houston
    # External API calls are not permitted in the 3-second decision window
    logging.info(f"🚫 houston_ways miss for '{street_name}' — Overpass disabled, returning None")
    return None

    # Store in cache under the queried name
    _store_in_cache(street_name, region_h3, segments, osm_ids, cur, conn)

    # Also store under all aliases (Westheimer/FM 1093/etc)
    for alias in aliases:
        if alias != _normalize_street_name(street_name):
            _store_in_cache(alias, region_h3, segments, osm_ids, cur, conn)

    return segments


# ======================================================================
# LAYER 2: STREET SWEEP (Red Zone Check)
# ======================================================================

def sweep_arc_band(
    arc_center_lat: float, arc_center_lng: float,
    distance_miles: float,
    segments: List,
    red_zone_set: set
) -> Dict:
    """
    Sweep the arc band along street geometry and check for red zone hits.

    Args:
        arc_center_lat/lng: Arc center point. Pass driver GPS for pickup triangulation,
            triangulated pickup coords for dropoff triangulation.
        distance_miles: Arc radius. Pass YOLO pickup_miles for pickup, trip_miles for dropoff.
        segments: Street geometry from cache/Overpass
        red_zone_set: Set of red zone H3 hex strings

    Returns dict with:
        - band_points: list of (lat, lng, distance) in band
        - red_hits: list of (lat, lng, hex) that hit red zones
        - best_guess: (lat, lng) closest band point to geometric center
        - any_red: bool
    """
    arc_inner = distance_miles / TORT_MAX
    arc_outer = distance_miles / TORT_MIN

    band_points = []

    for segment in segments:
        for i in range(len(segment) - 1):
            lat1, lng1 = segment[i]
            lat2, lng2 = segment[i + 1]

            for s in range(SAMPLES_PER_SEGMENT + 1):
                t = s / SAMPLES_PER_SEGMENT
                plat = lat1 + t * (lat2 - lat1)
                plng = lng1 + t * (lng2 - lng1)
                dist = haversine(arc_center_lat, arc_center_lng, plat, plng)

                if arc_inner <= dist <= arc_outer:
                    band_points.append((plat, plng, dist))

    if not band_points:
        return {
            "band_points": [],
            "red_hits": [],
            "best_guess": None,
            "any_red": False,
            "band_size": 0
        }

    return {
        "band_points": band_points,
        "band_size": len(band_points),
        "arc_inner": arc_inner,
        "arc_outer": arc_outer
    }


def check_band_red_zones(band_points: List[Tuple], red_zone_set: set, cur) -> Dict:
    """
    Check if any band points fall in red zone hexes.
    Samples up to 30 evenly-spaced points to avoid excessive H3 calls.
    """
    if not band_points:
        return {"any_red": False, "red_hits": [], "hexes_checked": []}

    # Sample evenly
    step = max(1, len(band_points) // 30)
    samples = band_points[::step]

    red_hits = []
    hexes_checked = []

    for plat, plng, dist in samples:
        try:
            cur.execute(
                "SELECT app_private.coords_to_h3(%s, %s)",
                (plat, plng)
            )
            row = cur.fetchone()
            hex_id = row[0] if isinstance(row, tuple) else list(row.values())[0]
        except Exception:
            continue

        hexes_checked.append(hex_id)
        if hex_id in red_zone_set:
            red_hits.append({"lat": plat, "lng": plng, "hex": hex_id})

    return {
        "any_red": len(red_hits) > 0,
        "red_hits": red_hits,
        "red_hit_count": len(red_hits),
        "hexes_checked": list(set(hexes_checked)),
        "samples_checked": len(samples)
    }


# ======================================================================
# DROPOFF PROXIMITY CHECK: Is dropoff near a red zone?
# ======================================================================

def is_near_red_zone(dropoff_hex: str, red_zone_set: set, cur, max_hops: int = 2) -> bool:
    """
    Check if a dropoff hex is within max_hops of any red zone.
    Uses H3 grid distance. Returns True if near red.
    """
    if not dropoff_hex or not red_zone_set:
        return False

    # If already in red zone, definitely near
    if dropoff_hex in red_zone_set:
        return True

    # Check grid distance to each red zone hex
    # For efficiency, only check a sample if there are many red zones
    red_list = list(red_zone_set)
    if len(red_list) > 50:
        red_list = red_list[:50]  # Sample — could optimize with spatial index later

    for rz_hex in red_list:
        try:
            cur.execute(
                "SELECT h3_grid_distance(%s::h3index, %s::h3index)",
                (dropoff_hex, rz_hex)
            )
            row = cur.fetchone()
            dist = row[0] if isinstance(row, tuple) else list(row.values())[0]
            if dist is not None and dist <= max_hops:
                return True
        except Exception:
            continue  # h3_grid_distance can fail for distant hexes

    return False


# ======================================================================
# MAIN ENTRY POINT
# ======================================================================

def correct_dropoff(
    pickup_lat: float, pickup_lng: float,
    dropoff_lat: float, dropoff_lng: float,
    trip_miles: float,
    dropoff_address: Optional[str],
    red_zone_set: set,
    cur, conn,
    green_zone_set: set = None,
    is_puddle_jump: bool = False
) -> Dict:
    """
    Main arc band correction entry point. Called from decisions.py.

    Returns dict with:
        - corrected_lat, corrected_lng: Use these instead of original dropoff
              (None if hallucination and street geometry couldn't resolve)
        - use_ocr_distance: If True, ignore geocoded distance, use OCR trip_miles
        - is_red_zone_risk: If True, dropoff may be in red zone
        - arc_band_triggered: Whether the arc band check was run
        - trace: Dict of debugging info for trace_data
    """
    result = {
        "corrected_lat": dropoff_lat,
        "corrected_lng": dropoff_lng,
        "use_ocr_distance": False,
        "is_red_zone_risk": False,
        "arc_band_triggered": False,
        "trace": {}
    }

    # Skip if no valid data
    if trip_miles <= 0 or (dropoff_lat == 0 and dropoff_lng == 0):
        result["trace"]["skipped"] = "no_valid_data"
        return result

    # ── LAYER 1: Distance Gate ───────────────────────────────────
    gate = check_distance_gate(pickup_lat, pickup_lng, dropoff_lat, dropoff_lng, trip_miles)
    result["trace"]["distance_gate"] = gate
    is_hallucination = gate.get("is_hallucination", False)

    if is_hallucination:
        logging.warning(
            f"🚨 Arc Band Layer 1: HALLUCINATION — geocoded {gate['geocoded_distance']}mi "
            f"vs band max {gate['arc_outer']}mi (ratio: {gate['ratio']}x)"
        )
        # Flag OCR distance — geocoded coordinates are wrong
        result["use_ocr_distance"] = True
        # DON'T return — fall through to Layer 2 to try resolving
        # the actual dropoff from street geometry

    # ── LAYER 2 ──────────────────────────────────────────────────
    #
    # Two paths arrive here:
    #   A) Normal ride — geocoded coords are plausible, check for
    #      red zone drift (street segment in wrong hex)
    #   B) Hallucination — geocoded coords are garbage, use street
    #      geometry + arc band to find the REAL dropoff
    #

    if not is_hallucination:
        # ── PATH A: Normal — red zone proximity gate ─────────────
        # Only run the expensive street sweep if dropoff is near a red zone
        dropoff_hex = None
        try:
            cur.execute(
                "SELECT app_private.coords_to_h3(%s, %s)",
                (dropoff_lat, dropoff_lng)
            )
            row = cur.fetchone()
            dropoff_hex = row[0] if isinstance(row, tuple) else list(row.values())[0]
        except Exception:
            pass

        result["trace"]["dropoff_hex"] = dropoff_hex

        # Trigger: Outside green zone in PUDDLE_JUMP mode
        if is_puddle_jump and green_zone_set and dropoff_hex and dropoff_hex not in green_zone_set:
            result["arc_band_triggered"] = True
            result["trace"]["trigger"] = "outside_green_zone"
            return result

        # Trigger: Long street detection
        if dropoff_hex:
            try:
                cur.execute("""
                    SELECT COUNT(DISTINCT s.id) > 3
                    FROM app_private.street_network s
                    WHERE s.h3_indices_res8 @> ARRAY[%s]
                    AND s.highway_type IN ('motorway','trunk','primary')
                """, (dropoff_hex,))
                row = cur.fetchone()
                is_long_street = row[0] if row else False
            except Exception:
                is_long_street = False
            if is_long_street:
                result["arc_band_triggered"] = True
                result["trace"]["trigger"] = "long_street"
                return result

        # Already in red zone? Flag it directly
        if dropoff_hex and dropoff_hex in red_zone_set:
            result["is_red_zone_risk"] = True
            result["trace"]["red_zone_direct"] = True
            return result

        # Check proximity
        near_red = is_near_red_zone(dropoff_hex, red_zone_set, cur, max_hops=2)
        result["trace"]["near_red_zone"] = near_red

        if not near_red:
            # Not near any red zone — no need for street sweep
            result["trace"]["layer2_skipped"] = "not_near_red"
            return result

    # ── STREET SWEEP (both paths) ────────────────────────────────
    # Path A: near a red zone, need to check if street segment is actually red
    # Path B: hallucination, need to find where the ride actually goes

    if not dropoff_address:
        if is_hallucination:
            # Can't resolve without a street name — OCR-only fallback
            result["corrected_lat"] = None
            result["corrected_lng"] = None
            result["trace"]["correction"] = "hallucination_no_street"
        else:
            result["trace"]["layer2_skipped"] = "no_street_name"
        return result

    result["arc_band_triggered"] = True

    # For hallucinations: center search on PICKUP (geocoded coords are wrong)
    #   and widen bbox to cover the full arc band radius
    # For normal: center on geocoded dropoff (close enough)
    if is_hallucination:
        search_lat = pickup_lat
        search_lng = pickup_lng
        search_bbox = 0.08  # ~5.5 miles — covers arc band
        logging.info(
            f"🔍 Arc Band: Hallucination recovery — sweeping '{dropoff_address}' "
            f"centered on pickup ({pickup_lat:.4f}, {pickup_lng:.4f})"
        )
    else:
        search_lat = dropoff_lat
        search_lng = dropoff_lng
        search_bbox = 0.03  # ~2 miles — default
        logging.info(f"🔍 Arc Band Layer 2: Sweeping '{dropoff_address}' near red zone")

    # Get street geometry (cache or Overpass)
    segments = get_street_geometry(
        dropoff_address, search_lat, search_lng, cur, conn,
        bbox_margin=search_bbox
    )

    if not segments:
        if is_hallucination:
            result["corrected_lat"] = None
            result["corrected_lng"] = None
            result["trace"]["correction"] = "hallucination_no_geometry"
        else:
            result["trace"]["layer2_result"] = "no_geometry"
        return result

    # Sweep the band
    band = sweep_arc_band(pickup_lat, pickup_lng, trip_miles, segments, red_zone_set)
    result["trace"]["band_size"] = band.get("band_size", 0)

    if not band.get("band_points"):
        if is_hallucination:
            result["corrected_lat"] = None
            result["corrected_lng"] = None
            result["trace"]["correction"] = "hallucination_no_band_points"
        else:
            result["trace"]["layer2_result"] = "no_band_points"
        return result

    # ── BAND RESOLVED — check red zones ──────────────────────────
    red_check = check_band_red_zones(band["band_points"], red_zone_set, cur)
    result["trace"]["red_check"] = {
        "any_red": red_check["any_red"],
        "red_hit_count": red_check["red_hit_count"],
        "samples_checked": red_check["samples_checked"],
        "hexes_found": red_check["hexes_checked"]
    }

    if red_check["any_red"]:
        logging.warning(
            f"🔴 Arc Band Layer 2: RED ZONE — {red_check['red_hit_count']} hits "
            f"in band for '{dropoff_address}'"
        )
        result["is_red_zone_risk"] = True
        result["trace"]["correction"] = "red_zone_band_hit"

    # ── PICK BEST DROPOFF COORDINATES ────────────────────────────
    if is_hallucination:
        # Geocoded coords are meaningless — pick band midpoint
        mid_idx = len(band["band_points"]) // 2
        best = band["band_points"][mid_idx]
        if not red_check["any_red"]:
            result["trace"]["correction"] = "hallucination_resolved"
    else:
        # Geocoded coords are close — pick nearest band point
        best = min(band["band_points"],
                   key=lambda pt: haversine(pt[0], pt[1], dropoff_lat, dropoff_lng))
        if not red_check["any_red"]:
            result["trace"]["correction"] = "band_clear_best_guess"

    result["corrected_lat"] = best[0]
    result["corrected_lng"] = best[1]

    return result