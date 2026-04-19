"""
triangulation.py - PuddleJumper Triangulation Engine

Owns all pickup and dropoff triangulation logic.
Extracted from decisions.py to keep concerns separated.

Canonical coordinate rule: ALL calls use (lat, lng) order.
NEVER call ST_MakePoint, h3_latlng_to_cell, h3_cell_to_latlng directly.
Use only app_private canonical functions.

v6 - April 2 2026
Tier 0: Enhanced arc-banding for vague single-road addresses.
         Uses our cached initial estimate as arc center when hallucination detected.
         Filled ST_Buffer (not ExteriorRing) — empirically better on
         fragmented Houston OSM data.
Tier 1: Google geocode → direct H3 (intersections, POIs)
Tier 2: Arc-banding fallback (GPS fresh)
Tier 3: Initial-estimate snap (last resort)

Confirmed working changes vs original v6:
- YOLO=0 fallback: uses geocoded_miles as arc radius
- Geocoded distance retry in Tier 2 when YOLO undercounts
- Stronger name variants (freeway frontage, suffix expansion)
- DWithin tolerance 0.4 → 0.5 in Tier 0
- arc_band.py uses routing.houston_ways for dropoff geometry
"""

# ═══════════════════════════════════════════════════════════════════
# CRITICAL MENTAL MODEL — READ FIRST
# ═══════════════════════════════════════════════════════════════════
# Uber does NOT provide lat/lng coordinates. Any coordinate passed to
# this engine as an "initial_estimate" originates from our OWN earlier
# work — a Google Geocoding API call, a cache hit on a prior Google
# lookup, or the output of a prior triangulation stage. Never reason
# about initial_estimate values as if they came from Uber; they are
# our prior guess being refined by this engine using address string +
# driver GPS + OCR-reported driven distance + houston_ways geometry.
# ═══════════════════════════════════════════════════════════════════


import logging
import os
import requests

GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY")

# Tortuosity constants imported from arc_band.py (single source of truth).
# Houston-specific empirical values — see arc_band.py for data provenance.
from arc_band import TORT_MIN_HOUSTON, TORT_MAX_HOUSTON


def _google_geocode(address: str, bias_lat: float = None, bias_lng: float = None) -> tuple | None:
    if not GOOGLE_MAPS_API_KEY or not address:
        return None
    try:
        params = {"address": address, "key": GOOGLE_MAPS_API_KEY}
        if bias_lat is not None and bias_lng is not None:
            params["location"] = f"{bias_lat},{bias_lng}"
            params["radius"] = "50000"
        r = requests.get(
            "https://maps.googleapis.com/maps/api/geocode/json",
            params=params,
            timeout=2.0
        )
        data = r.json()
        if data["status"] == "OK":
            loc = data["results"][0]["geometry"]["location"]
            logging.info(
                f"[Google] '{address}' → ({loc['lat']:.5f}, {loc['lng']:.5f}) "
                f"[{data['results'][0]['formatted_address']}]"
            )
            return loc["lat"], loc["lng"]
        logging.warning(f"[Google] Geocode failed for '{address}': {data['status']}")
    except requests.Timeout:
        logging.warning(f"[Google] Geocode timeout for '{address}'")
    except Exception as e:
        logging.warning(f"[Google] Geocode exception: {e}")
    return None


def clean_street_name(name: str) -> str:
    if not name:
        return ""
    name = name.split(',')[0].strip()
    expansions = [
        (' Fwy', ' Freeway'), (' Hwy', ' Highway'), (' Pkwy', ' Parkway'),
        (' Blvd', ' Boulevard'), (' Ave', ' Avenue'), (' Rd', ' Road'),
        (' St', ' Street'), (' Dr', ' Drive'), (' Ln', ' Lane'),
        (' Ct', ' Court'), (' Pl', ' Place'), (' Trl', ' Trail'),
        (' Cir', ' Circle'), (' Trce', ' Trace'),
    ]
    for abbr, full in expansions:
        if name.lower().endswith(abbr.lower()):
            return name[:len(name) - len(abbr)] + full
    return name


import re
_HIGHWAY_RE = re.compile(r'^[a-z]{1,3}[-\s]\d', re.IGNORECASE)

def _is_vague_single_road(street_name: str) -> bool:
    if not street_name:
        return False
    # Evaluate the street portion only — city/state suffixes (e.g.,
    # "Lexington Blvd, Sugar Land, Texas") don't contribute to complexity
    # and shouldn't disqualify the Tier 0 arc-banding path.
    street_only = street_name.split(',')[0].strip()
    if not street_only:
        return False
    if '&' in street_only:
        return False
    lower = street_only.lower()
    if any(x in lower for x in [' and ', ' at ', '/', 'near']):
        return False
    is_highway = bool(_HIGHWAY_RE.match(street_only))
    if not is_highway and (any(c.isdigit() for c in street_only[:10]) or '#' in lower):
        return False
    if len(street_only.split()) > 4:  # too long = likely intersection or POI
        return False
    road_keywords = [
        'fwy', 'freeway', 'hwy', 'highway', 'pkwy', 'parkway',
        'blvd', 'boulevard', 'ave', 'avenue', 'rd', 'road',
        'dr', 'drive', 'st', 'street', 'ln', 'lane', 'cir',
        'circle', 'trce', 'trace', 'trail', 'trl', 'way', 'bypass',
    ]
    if is_highway:
        return True
    return any(f' {kw}' in lower or lower.endswith(kw) for kw in road_keywords)


# TORTUOSITY_MIN (1.0) and TORTUOSITY_MAX (3.0) removed here —
# these were a duplicate, drifted set of the canonical Houston tortuosity
# constants now imported from arc_band.py above (values: 0.9 / 2.5).
# build_arc_donut() below uses the canonical TORT_MIN_HOUSTON / TORT_MAX_HOUSTON.


def _normalize_address(address: str) -> str:
    if not address:
        return ""
    cleaned = re.sub(r'[^\w\s]', ' ', address)
    return ' '.join(cleaned.strip().lower().split())

def _lookup_geocode_cache(address: str, cur) -> tuple | None:
    if not address:
        return None
    norm = _normalize_address(address)
    try:
        cur.execute(
            "SELECT lat, lng FROM app_private.geocode_cache WHERE address_text = %s",
            (norm,)
        )
        row = cur.fetchone()
        if row:
            logging.info(f"[Geocode] CACHE HIT: '{address}'")
            cur.execute(
                "UPDATE app_private.geocode_cache SET last_used_at = NOW(), hit_count = hit_count + 1 WHERE address_text = %s",
                (norm,)
            )
            return row['lat'], row['lng']
    except Exception as e:
        logging.warning(f"[Geocode] Cache lookup failed: {e}")
    return None

def _write_geocode_cache(address: str, lat: float, lng: float, cur):
    if not address or lat is None or lng is None:
        return
    norm = _normalize_address(address)
    try:
        cur.execute("""
            INSERT INTO app_private.geocode_cache (address_text, lat, lng)
            VALUES (%s, %s, %s)
            ON CONFLICT (address_text) DO UPDATE
            SET last_used_at = NOW(),
                hit_count = geocode_cache.hit_count + 1
        """, (norm, lat, lng))
    except Exception as e:
        logging.warning(f"[Geocode] Cache write failed for '{address}': {e}")

def build_arc_donut(yolo_miles, geocoded_miles, safety_buffer_miles=0.2):
    if yolo_miles <= 0:
        return 0.1, 1.0
    inner_yolo = yolo_miles / TORT_MAX_HOUSTON
    outer_yolo = yolo_miles / TORT_MIN_HOUSTON
    inner_geo = max(geocoded_miles - safety_buffer_miles, 0.1)
    outer_geo = geocoded_miles + safety_buffer_miles
    inner_radius = min(inner_yolo, inner_geo)
    outer_radius = max(outer_yolo, outer_geo)
    inner_radius = max(inner_radius, 0.1)
    outer_radius = min(outer_radius, inner_radius * 5.0)
    logging.info(
        f"Fused donut: YOLO={yolo_miles}mi geocode={geocoded_miles:.2f}mi "
        f"inner={inner_radius:.2f}mi outer={outer_radius:.2f}mi"
    )
    return inner_radius, outer_radius


def _haversine_miles(lat1, lng1, lat2, lng2):
    import math
    R = 3958.8
    lat1, lng1, lat2, lng2 = map(math.radians, [lat1, lng1, lat2, lng2])
    dlat, dlng = lat2 - lat1, lng2 - lng1
    a = math.sin(dlat/2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng/2)**2
    return R * 2 * math.asin(math.sqrt(a))


_TRIANGULATION_SQL = """
    WITH candidate_segments AS (
        SELECT
            s.tag_id,
            ST_ClosestPoint(s.the_geom, app_private.coords_to_point(%s, %s)) AS snap_point,
            ST_Distance(
                s.the_geom::geography,
                app_private.coords_to_point(%s, %s)::geography
            ) AS dist_to_geocode_m
        FROM routing.houston_ways s
        WHERE ST_DWithin(
            s.the_geom,
            app_private.coords_to_point(%s, %s),
            0.001
        )
          AND s.tag_id NOT IN (101, 102, 104, 105)
        ORDER BY dist_to_geocode_m
        LIMIT 20
    ),
    scored AS (
        SELECT
            snap_point,
            (0.45 + 0.35 * EXP(-dist_to_geocode_m / 100.0)) * (dist_to_geocode_m / 800.0) +
            0.20 * CASE WHEN tag_id IN (110, 109, 108, 111, 123)
                        THEN 0.0 ELSE 1.0 END AS score
        FROM candidate_segments
    )
    SELECT app_private.coords_to_h3(
        ST_Y(snap_point),
        ST_X(snap_point)
    ) AS h3
    FROM scored
    ORDER BY score ASC
    LIMIT 1
"""


def triangulate_pickup(
    current_lat, current_lng,
    p_lat, p_lng,
    pickup_miles,
    geocoded_miles,
    cur,
    street_name=None,
    gps_age_sec=None,
    pre_geocoded=False,
):
    """Thin wrapper — routes to refine_target_by_geometry(mode='pickup').

    Preserves the pre-00566a signature and h3-only return contract for
    existing callers in decisions/triangulation_enricher.py, decisions/router.py,
    and backtest scripts. New callers should call refine_target_by_geometry
    directly to access the full {h3, lat, lng, source, tier} result dict.
    """
    result = refine_target_by_geometry(
        anchor_lat=current_lat, anchor_lng=current_lng,
        initial_estimate_lat=p_lat, initial_estimate_lng=p_lng,
        intended_miles=pickup_miles,
        geocoded_miles=geocoded_miles,
        cur=cur,
        mode="pickup",
        street_name=street_name,
        gps_age_sec=gps_age_sec,
        pre_geocoded=pre_geocoded,
    )
    return result["h3"] if result else None


def triangulate_dropoff(
    pickup_lat, pickup_lng,
    dropoff_lat, dropoff_lng,
    trip_miles,
    cur,
    street_name=None,
    driver_lat=None,
    driver_lng=None,
):
    """Thin wrapper — routes to refine_target_by_geometry(mode='dropoff').

    Preserves the pre-00566a signature and h3-only return contract.
    Computes geocoded_miles internally via _compute_geocoded_miles() since
    existing callers don't pass it. New callers (decisions/nail_manager.py,
    Patch 00566a Step 9) should call refine_target_by_geometry directly.
    """
    geocoded_miles = _compute_geocoded_miles(
        pickup_lat, pickup_lng,
        dropoff_lat, dropoff_lng,
        cur,
    )
    result = refine_target_by_geometry(
        anchor_lat=pickup_lat, anchor_lng=pickup_lng,
        initial_estimate_lat=dropoff_lat, initial_estimate_lng=dropoff_lng,
        intended_miles=trip_miles,
        geocoded_miles=geocoded_miles,
        cur=cur,
        mode="dropoff",
        street_name=street_name,
        driver_lat=driver_lat,
        driver_lng=driver_lng,
    )
    return result["h3"] if result else None


def h3_to_coords(h3_hex, cur):
    if not h3_hex:
        return None
    try:
        cur.execute(
            "SELECT app_private.h3_to_lat(%s) AS lat, app_private.h3_to_lng(%s) AS lng",
            (h3_hex, h3_hex)
        )
        row = cur.fetchone()
        if row:
            return float(row['lat']), float(row['lng'])
    except Exception as e:
        logging.warning(f"H3 coordinate resolution failed for {h3_hex}: {e}")
    return None


# ════════════════════════════════════════════════════════════════════════════
# UNIFIED REFINEMENT ENGINE (Patch 00566a Step 2)
#
# Single source of truth for pickup + dropoff coordinate refinement.
# Walks a 5-tier cascade, returns best available refinement or None.
# triangulate_pickup() and triangulate_dropoff() will be rewritten as
# thin wrappers around this engine in Step 4.
# ════════════════════════════════════════════════════════════════════════════


def _compute_geocoded_miles(lat1, lng1, lat2, lng2, cur):
    """Straight-line distance in miles between two coords."""
    if not all([lat1, lng1, lat2, lng2]):
        return None
    try:
        cur.execute(
            "SELECT app_private.distance_miles(%s, %s, %s, %s) AS dist",
            (lat1, lng1, lat2, lng2)
        )
        row = cur.fetchone()
        return float(row["dist"]) if (row and row.get("dist") is not None) else None
    except Exception:
        return None


def refine_target_by_geometry(
    anchor_lat,
    anchor_lng,
    initial_estimate_lat,
    initial_estimate_lng,
    intended_miles,
    geocoded_miles,
    cur,
    *,
    mode,
    street_name=None,
    gps_age_sec=None,
    pre_geocoded=False,
    driver_lat=None,
    driver_lng=None,
):
    """
    Unified refinement engine for pickup and dropoff coordinate resolution.

    Walks a 5-tier cascade, returns first successful refinement.

    Tiers:
      0 - enhanced_arc_band: vague single-road addresses, ST_Buffer+houston_ways
      1 - google_cache/google_live: live Google geocode, cached for dedup
      2 - arc_band (donut): build_arc_donut() inner/outer bounds + houston_ways
      3 - scored_snap: _TRIANGULATION_SQL scored candidate ranker
      4 - estimate_snap: last-resort snap of initial estimate to H3

    Mode parameterization:
      - mode="pickup": anchor is driver GPS; honors gps_age_sec, pre_geocoded,
        center-switching in Tier 0, and Tier 2 YOLO-undercount retry.
      - mode="dropoff": anchor is nailed pickup; ignores GPS staleness
        (nailed pickup is not a live GPS source) and does not retry Tier 2.

    Returns: dict {h3, lat, lng, source, tier} or None.
      source: google_cache | google_live | enhanced_arc_band | arc_band
            | scored_snap | estimate_snap
      tier:   int 0..4 corresponding to the tier that fired
    """
    if mode not in ("pickup", "dropoff"):
        raise ValueError(f"mode must be 'pickup' or 'dropoff', got {mode!r}")
    if not all([anchor_lat, anchor_lng, initial_estimate_lat, initial_estimate_lng]):
        return None

    # Intended-miles fallback (pickup-mode): YOLO zeroed out but geocoded present
    if intended_miles is None or intended_miles <= 0:
        if mode == "pickup" and geocoded_miles is not None and geocoded_miles > 0:
            logging.info(
                f"⚠️ intended_miles=0 — falling back to geocoded "
                f"({geocoded_miles:.2f}mi) as arc radius"
            )
            intended_miles = geocoded_miles
        else:
            return None

    # Hallucination detection (mode-specific threshold)
    hallucination_factor = 2.0 if mode == "pickup" else 1.5
    arc_outer = intended_miles / TORT_MIN_HOUSTON
    is_hallucination = (
        geocoded_miles is not None
        and geocoded_miles > arc_outer * hallucination_factor
    )
    if is_hallucination:
        logging.warning(
            f"⚠️ [{mode}] Geocode mismatch ({geocoded_miles:.1f}mi vs "
            f"{intended_miles}mi intended, factor {hallucination_factor}x)"
        )

    # GPS staleness (pickup-mode only)
    gps_stale = (
        mode == "pickup"
        and gps_age_sec is not None
        and gps_age_sec > 30
    )
    if gps_stale:
        logging.info(f"⚠️ [{mode}] GPS stale ({gps_age_sec:.0f}s)")

    # ── TIER 0: Enhanced arc-banding for vague single-road ──────────────────
    if street_name and _is_vague_single_road(street_name) and not gps_stale:
        logging.info(f"🛣️ [{mode}] Tier 0 enhanced arc-band for '{street_name}'")
        result = _tier0_enhanced_arc_band(
            anchor_lat, anchor_lng,
            initial_estimate_lat, initial_estimate_lng,
            intended_miles, geocoded_miles,
            street_name, mode, cur,
        )
        if result:
            return result

    # ── TIER 1: Google geocode (cache → live) ───────────────────────────────
    if street_name and not pre_geocoded:
        result = _tier1_google_geocode(street_name, cur)
        if result:
            return result

    # ── TIER 2: Donut arc-banding ───────────────────────────────────────────
    if street_name and not gps_stale:
        result = _tier2_donut_arc_band(
            anchor_lat, anchor_lng,
            intended_miles, geocoded_miles,
            street_name,
            driver_lat, driver_lng,
            mode, cur,
        )
        if result:
            return result

    # ── TIER 3: Scored snap (with Option X safety gate) ─────────────────────
    result = _tier3_scored_snap(initial_estimate_lat, initial_estimate_lng, cur)
    if result:
        dist_m = _haversine_miles(
            initial_estimate_lat, initial_estimate_lng,
            result["lat"], result["lng"],
        ) * 1609.34
        if dist_m <= 500:
            return result
        logging.warning(
            f"⚠️ [{mode}] Tier 3 result {dist_m:.0f}m from initial_estimate — "
            f"rejected (Option X gate, falling through to Tier 4)"
        )

    # ── TIER 4: Last resort — initial-estimate snap ─────────────────────────
    return _tier4_estimate_snap(initial_estimate_lat, initial_estimate_lng, cur)


def _tier0_enhanced_arc_band(
    anchor_lat, anchor_lng,
    initial_estimate_lat, initial_estimate_lng,
    intended_miles, geocoded_miles,
    street_name, mode, cur,
):
    """Tier 0: vague single-road arc-banding. Pickup-mode may center-switch."""
    try:
        cleaned = clean_street_name(street_name)
        lower = cleaned.lower()

        # Center-switching (pickup-mode only per v0.3 §4.3):
        # When our prior geocode is strongly at odds with OCR pickup_miles (>3x
        # the expected arc), the driver GPS anchor may be in a totally different
        # part of the city than the pickup. Switch arc center from driver GPS
        # to the prior geocode — the assumption is that the geocode is closer
        # to reality than GPS-anchored arc-banding would yield.
        # Dropoff-mode never switches — nailed pickup is verified good.
        if (mode == "pickup"
                and geocoded_miles
                and geocoded_miles > intended_miles * 3.0):
            logging.info(
                f"   → Strong hallucination: center-switch to initial estimate "
                f"({initial_estimate_lat:.5f}, {initial_estimate_lng:.5f})"
            )
            arc_lat, arc_lng = initial_estimate_lat, initial_estimate_lng
        else:
            arc_lat, arc_lng = anchor_lat, anchor_lng

        # Variant expansion for freeway-class names
        variants = [cleaned]
        if any(x in lower for x in ['fwy', 'freeway', 'hwy', 'highway']):
            base = cleaned.replace('Fwy', 'Freeway').replace('Hwy', 'Highway')
            variants += [base, base + ' Frontage Road']
        variants = list(dict.fromkeys(variants))

        for variant in variants:
            cur.execute("""
                WITH arc AS (
                    SELECT ST_Buffer(
                        app_private.coords_to_point(%s, %s)::geography,
                        %s * 1609.34
                    )::geometry AS ring
                )
                SELECT
                    app_private.coords_to_h3(
                        ST_Y(ST_ClosestPoint(s.the_geom, a.ring)),
                        ST_X(ST_ClosestPoint(s.the_geom, a.ring))
                    ) AS h3,
                    ST_Y(ST_ClosestPoint(s.the_geom, a.ring)) AS lat,
                    ST_X(ST_ClosestPoint(s.the_geom, a.ring)) AS lng
                FROM routing.houston_ways s, arc a
                WHERE s.name ILIKE '%%' || %s || '%%'
                  AND ST_DWithin(
                        s.the_geom::geography,
                        app_private.coords_to_point(%s, %s)::geography,
                        (%s + 0.35) * 1609.34
                  )
                ORDER BY ST_Distance(s.the_geom, a.ring) ASC
                LIMIT 1
            """, (
                arc_lat, arc_lng, intended_miles,
                variant,
                arc_lat, arc_lng, intended_miles,
            ))
            row = cur.fetchone()
            if row and row['h3']:
                logging.info(
                    f"✅ Tier 0: {row['h3']} (variant '{variant}')"
                )
                return {
                    "h3": row['h3'],
                    "lat": float(row['lat']),
                    "lng": float(row['lng']),
                    "source": "enhanced_arc_band",
                    "tier": 0,
                }
        logging.info(f"⚠️ Tier 0: no match for any variant of '{street_name}'")
    except Exception as e:
        logging.warning(f"⚠️ Tier 0 failed: {e}")
    return None


def _tier1_google_geocode(street_name, cur):
    """Tier 1: cache lookup then live Google geocode. Returns coords → H3."""
    logging.info(f"📡 Tier 1 Google for '{street_name}'")
    google_coords = _lookup_geocode_cache(street_name, cur)
    source = "google_cache" if google_coords else None

    if google_coords is None:
        google_coords = _google_geocode(street_name)
        if google_coords:
            _write_geocode_cache(street_name, google_coords[0], google_coords[1], cur)
            try:
                cur.connection.commit()
            except Exception as commit_err:
                logging.warning(f"[CACHE] commit failed: {commit_err}")
            source = "google_live"

    if google_coords:
        g_lat, g_lng = google_coords
        try:
            cur.execute(
                "SELECT app_private.coords_to_h3(%s, %s)::text AS h3",
                (g_lat, g_lng)
            )
            row = cur.fetchone()
            if row and row['h3']:
                logging.info(f"🎯 Tier 1 ({source}): {row['h3']}")
                return {
                    "h3": row['h3'],
                    "lat": g_lat,
                    "lng": g_lng,
                    "source": source,
                    "tier": 1,
                }
        except Exception as e:
            logging.warning(f"⚠️ Tier 1 H3 conversion failed: {e}")
    else:
        logging.warning(f"⚠️ Tier 1: Google returned nothing for '{street_name}'")
    return None


def _tier2_donut_arc_band(
    anchor_lat, anchor_lng,
    intended_miles, geocoded_miles,
    street_name,
    driver_lat, driver_lng,
    mode, cur,
):
    """Tier 2: donut-bounded arc-band via build_arc_donut(). Pickup may retry."""
    logging.info(
        f"🔄 Tier 2 donut from ({anchor_lat:.4f},{anchor_lng:.4f}) "
        f"at ~{intended_miles}mi for '{street_name}'"
    )
    try:
        cleaned = clean_street_name(street_name)
        inner_miles, outer_miles = build_arc_donut(
            intended_miles, geocoded_miles or intended_miles
        )

        # Order-by bias: driver-aware ordering when driver coords provided
        # (offer-time enricher uses this for dropoff refinement).
        if driver_lat and driver_lng:
            order_sql = (
                "ST_Distance(s.the_geom::geography, "
                "app_private.coords_to_point(%s, %s)::geography) ASC"
            )
            order_params = (driver_lat, driver_lng)
        else:
            order_sql = "ST_Distance(s.the_geom, a.ring) ASC"
            order_params = ()

        result = _tier2_run_query(
            anchor_lat, anchor_lng,
            intended_miles, inner_miles, outer_miles,
            cleaned, order_sql, order_params, cur,
        )
        if result:
            logging.info(
                f"✅ Tier 2: {result['h3']} "
                f"(donut [{inner_miles:.2f}, {outer_miles:.2f}]mi)"
            )
            return result

        # Pickup-mode retry: YOLO significantly undercounts geocoded distance
        if (mode == "pickup"
                and geocoded_miles
                and abs(geocoded_miles - intended_miles) > 1.0):
            logging.info(
                f"🔄 Tier 2 retry with geocoded {geocoded_miles:.2f}mi "
                f"(vs intended {intended_miles}mi)"
            )
            inner_r, outer_r = build_arc_donut(geocoded_miles, geocoded_miles)
            result = _tier2_run_query(
                anchor_lat, anchor_lng,
                geocoded_miles, inner_r, outer_r,
                cleaned, order_sql, order_params, cur,
            )
            if result:
                logging.info(f"✅ Tier 2 retry: {result['h3']}")
                return result

        logging.info(f"⚠️ Tier 2: no match for '{street_name}'")
    except Exception as e:
        logging.warning(f"⚠️ Tier 2 failed: {e}")
    return None


def _tier2_run_query(
    anchor_lat, anchor_lng,
    ring_miles, inner_miles, outer_miles,
    cleaned_street, order_sql, order_params, cur,
):
    """Inner helper for Tier 2. Runs the donut+street query once."""
    cur.execute(f"""
        WITH arc AS (
            SELECT ST_Buffer(
                app_private.coords_to_point(%s, %s)::geography,
                %s * 1609.34
            )::geometry AS ring
        )
        SELECT
            app_private.coords_to_h3(
                ST_Y(ST_ClosestPoint(s.the_geom, a.ring)),
                ST_X(ST_ClosestPoint(s.the_geom, a.ring))
            ) AS h3,
            ST_Y(ST_ClosestPoint(s.the_geom, a.ring)) AS lat,
            ST_X(ST_ClosestPoint(s.the_geom, a.ring)) AS lng
        FROM routing.houston_ways s, arc a
        WHERE s.name ILIKE '%%' || %s || '%%'
          AND ST_DWithin(
                s.the_geom::geography,
                app_private.coords_to_point(%s, %s)::geography,
                %s * 1609.34
          )
          AND NOT ST_DWithin(
                s.the_geom::geography,
                app_private.coords_to_point(%s, %s)::geography,
                %s * 1609.34
          )
        ORDER BY {order_sql}
        LIMIT 1
    """, (
        anchor_lat, anchor_lng, ring_miles,
        cleaned_street,
        anchor_lat, anchor_lng, outer_miles,
        anchor_lat, anchor_lng, inner_miles,
    ) + order_params)
    row = cur.fetchone()
    if row and row['h3']:
        return {
            "h3": row['h3'],
            "lat": float(row['lat']),
            "lng": float(row['lng']),
            "source": "arc_band",
            "tier": 2,
        }
    return None


def _tier3_scored_snap(initial_estimate_lat, initial_estimate_lng, cur):
    """Tier 3: scored ranker over houston_ways candidates near initial estimate."""
    logging.info("🎯 Tier 3 scored snap")
    try:
        cur.execute("""
            WITH candidate_segments AS (
                SELECT
                    s.tag_id,
                    ST_ClosestPoint(s.the_geom,
                        app_private.coords_to_point(%s, %s)) AS snap_point,
                    ST_Distance(
                        s.the_geom::geography,
                        app_private.coords_to_point(%s, %s)::geography
                    ) AS dist_to_geocode_m
                FROM routing.houston_ways s
                WHERE ST_DWithin(
                    s.the_geom,
                    app_private.coords_to_point(%s, %s),
                    0.001
                )
                  AND s.tag_id NOT IN (101, 102, 104, 105)
                ORDER BY dist_to_geocode_m
                LIMIT 20
            ),
            scored AS (
                SELECT
                    snap_point,
                    (0.45 + 0.35 * EXP(-dist_to_geocode_m / 100.0))
                        * (dist_to_geocode_m / 800.0)
                    + 0.20 * CASE WHEN tag_id IN (110, 109, 108, 111, 123)
                                  THEN 0.0 ELSE 1.0 END AS score
                FROM candidate_segments
            )
            SELECT
                app_private.coords_to_h3(
                    ST_Y(snap_point),
                    ST_X(snap_point)
                ) AS h3,
                ST_Y(snap_point) AS lat,
                ST_X(snap_point) AS lng
            FROM scored
            ORDER BY score ASC
            LIMIT 1
        """, (
            initial_estimate_lat, initial_estimate_lng,
            initial_estimate_lat, initial_estimate_lng,
            initial_estimate_lat, initial_estimate_lng,
        ))
        row = cur.fetchone()
        if row and row['h3']:
            logging.info(f"✅ Tier 3 snap: {row['h3']}")
            return {
                "h3": row['h3'],
                "lat": float(row['lat']),
                "lng": float(row['lng']),
                "source": "scored_snap",
                "tier": 3,
            }
        logging.info("⚠️ Tier 3: no candidate")
    except Exception as e:
        logging.warning(f"⚠️ Tier 3 failed: {e}")
    return None


def _tier4_estimate_snap(initial_estimate_lat, initial_estimate_lng, cur):
    """Tier 4: last resort — snap initial estimate directly to H3."""
    logging.info("📍 Tier 4 estimate_snap (last resort)")
    try:
        cur.execute(
            "SELECT app_private.coords_to_h3(%s, %s)::text AS h3",
            (initial_estimate_lat, initial_estimate_lng)
        )
        row = cur.fetchone()
        if row and row['h3']:
            logging.info(f"Tier 4: {row['h3']}")
            return {
                "h3": row['h3'],
                "lat": initial_estimate_lat,
                "lng": initial_estimate_lng,
                "source": "estimate_snap",
                "tier": 4,
            }
        logging.warning("⚠️ Tier 4: no result for initial estimate")
    except Exception as e:
        logging.warning(f"⚠️ Tier 4 failed: {e}")
    return None
