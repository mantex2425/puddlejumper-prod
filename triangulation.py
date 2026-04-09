"""
triangulation.py - PuddleJumper Triangulation Engine

Owns all pickup and dropoff triangulation logic.
Extracted from decisions.py to keep concerns separated.

Canonical coordinate rule: ALL calls use (lat, lng) order.
NEVER call ST_MakePoint, h3_latlng_to_cell, h3_cell_to_latlng directly.
Use only app_private canonical functions.

v6 - April 2 2026
Tier 0: Enhanced arc-banding for vague single-road addresses.
         Uses Uber geocode as arc center when hallucination detected.
         Filled ST_Buffer (not ExteriorRing) — empirically better on
         fragmented Houston OSM data.
Tier 1: Google geocode → direct H3 (intersections, POIs)
Tier 2: Arc-banding fallback (GPS fresh)
Tier 3: Uber geocoded snap (last resort)

Confirmed working changes vs original v6:
- YOLO=0 fallback: uses geocoded_miles as arc radius
- Geocoded distance retry in Tier 2 when YOLO undercounts
- Stronger name variants (freeway frontage, suffix expansion)
- DWithin tolerance 0.4 → 0.5 in Tier 0
- arc_band.py uses routing.houston_ways for dropoff geometry
"""

import logging
import os
import requests

GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY")

TORT_MIN = 0.9
TORT_MAX = 2.5


def _google_geocode(address: str) -> tuple | None:
    if not GOOGLE_MAPS_API_KEY or not address:
        return None
    try:
        r = requests.get(
            "https://maps.googleapis.com/maps/api/geocode/json",
            params={"address": address, "key": GOOGLE_MAPS_API_KEY},
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
    if '&' in street_name:
        return False
    lower = street_name.lower()
    if any(x in lower for x in [' and ', ' at ', '/', 'near']):
        return False
    is_highway = bool(_HIGHWAY_RE.match(street_name.strip()))
    if not is_highway and (any(c.isdigit() for c in street_name[:10]) or '#' in lower):
        return False
    if len(street_name.split()) > 4:  # too long = likely intersection or POI
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


TORTUOSITY_MIN = 1.0
TORTUOSITY_MAX = 3.0


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
    inner_yolo = yolo_miles / TORTUOSITY_MAX
    outer_yolo = yolo_miles / TORTUOSITY_MIN
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
    if not all([current_lat, current_lng, p_lat, p_lng]):
        return None
    if geocoded_miles is None:
        return None
    if pickup_miles is None or pickup_miles <= 0:
        if geocoded_miles > 0:
            logging.info(
                f"⚠️ YOLO pickup_miles=0 — falling back to geocoded distance "
                f"({geocoded_miles:.2f}mi) as arc radius"
            )
            pickup_miles = geocoded_miles
        else:
            return None

    gps_stale = gps_age_sec is not None and gps_age_sec > 30
    if gps_stale:
        logging.info(f"⚠️ GPS stale ({gps_age_sec:.0f}s)")

    arc_outer = pickup_miles / TORT_MIN
    is_hallucination = geocoded_miles > arc_outer * 2.0
    if is_hallucination:
        logging.warning(
            f"⚠️ Geocode/GPS mismatch ({geocoded_miles:.1f}mi vs {pickup_miles}mi YOLO). "
            f"Proceeding with Google verification..."
        )

    # ── TIER 0: Enhanced arc-banding for vague single-road ───────────────────
    if street_name and _is_vague_single_road(street_name) and not gps_stale:
        logging.info(f"🛣️ Vague single-road detected → Enhanced arc-banding on '{street_name}'")
        try:
            cleaned = clean_street_name(street_name)
            lower = cleaned.lower()

            if geocoded_miles > pickup_miles * 3.0:
                logging.info(
                    f"   → Strong hallucination: using Uber geocode "
                    f"({p_lat:.5f}, {p_lng:.5f}) as arc center"
                )
                arc_lat, arc_lng = p_lat, p_lng
            else:
                arc_lat, arc_lng = current_lat, current_lng

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
                    SELECT app_private.coords_to_h3(
                        ST_Y(ST_ClosestPoint(s.the_geom, a.ring)),
                        ST_X(ST_ClosestPoint(s.the_geom, a.ring))
                    ) AS h3
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
                    arc_lat, arc_lng, pickup_miles,
                    variant,
                    arc_lat, arc_lng, pickup_miles
                ))
                row = cur.fetchone()
                if row and row['h3']:
                    logging.info(
                        f"✅ Enhanced arc-banding succeeded: {row['h3']} "
                        f"(variant: '{variant}')"
                    )
                    return row['h3']
            logging.warning(
                f"⚠️ Enhanced arc-banding: no match for any variant of '{street_name}'"
            )
        except Exception as e:
            logging.warning(f"⚠️ Enhanced arc-banding failed: {e}")

    # ── TIER 1: Google geocode → direct H3 ───────────────────────────────────
    # Skipped when pickup coords already geocoded upstream (saves API call)
    if street_name and not pre_geocoded:
        logging.info(f"📡 [ORACLE] Querying Google for '{street_name}'...")
        google_coords = _lookup_geocode_cache(street_name, cur)
        if google_coords is None:
            google_coords = _google_geocode(street_name)
            if google_coords:
                _write_geocode_cache(street_name, google_coords[0], google_coords[1], cur)
                try:
                    cur.connection.commit()
                except Exception as _commit_err:
                    logging.warning(f"[CACHE] Geocode cache commit failed: {_commit_err}")
        if google_coords:
            g_lat, g_lng = google_coords
            if True:  # Total Trust in Google Address
                try:
                    cur.execute(
                        "SELECT app_private.coords_to_h3(%s, %s)::text AS h3",
                        (g_lat, g_lng)
                    )
                    row = cur.fetchone()
                    if row and row['h3']:
                        logging.info(f"🎯 BULLSEYE: Using Google Coords Directly: {row['h3']}")
                        return row['h3']
                except Exception as e:
                    logging.warning(f"⚠️ Google direct H3 failed: {e}")
        else:
            logging.warning(f"⚠️ Google geocode returned nothing for '{street_name}'")

    # ── TIER 2: Arc-banding (GPS must be fresh) ───────────────────────────────
    if street_name and not gps_stale:
        logging.info(
            f"🔄 Arc-banding from ({current_lat:.4f},{current_lng:.4f}) "
            f"at {pickup_miles}mi for '{street_name}'"
        )
        try:
            cleaned = clean_street_name(street_name)
            cur.execute("""
                WITH arc AS (
                    SELECT ST_Buffer(
                        app_private.coords_to_point(%s, %s)::geography,
                        %s * 1609.34
                    )::geometry AS ring
                )
                SELECT app_private.coords_to_h3(
                    ST_Y(ST_ClosestPoint(s.the_geom, a.ring)),
                    ST_X(ST_ClosestPoint(s.the_geom, a.ring))
                ) AS h3
                FROM routing.houston_ways s, arc a
                WHERE s.name ILIKE '%%' || %s || '%%'
                  AND ST_DWithin(
                        s.the_geom::geography,
                        app_private.coords_to_point(%s, %s)::geography,
                        (%s + 0.2) * 1609.34
                  )
                ORDER BY ST_Distance(s.the_geom, a.ring) ASC
                LIMIT 1
            """, (
                current_lat, current_lng, pickup_miles,
                cleaned,
                current_lat, current_lng, pickup_miles
            ))
            row = cur.fetchone()
            if row and row['h3']:
                logging.info(f"✅ Arc-banding succeeded: {row['h3']}")
                return row['h3']
            else:
                logging.warning(
                    f"⚠️ Arc-banding: no match for '{street_name}' at {pickup_miles}mi"
                )
                # Geocoded distance retry when YOLO undercounts
                if geocoded_miles and abs(geocoded_miles - pickup_miles) > 1.0:
                    logging.info(
                        f"🔄 Retrying arc-banding with geocoded distance "
                        f"({geocoded_miles:.2f}mi vs YOLO {pickup_miles}mi)"
                    )
                    try:
                        cur.execute("""
                            WITH arc AS (
                                SELECT ST_Buffer(
                                    app_private.coords_to_point(%s, %s)::geography,
                                    %s * 1609.34
                                )::geometry AS ring
                            )
                            SELECT app_private.coords_to_h3(
                                ST_Y(ST_ClosestPoint(s.the_geom, a.ring)),
                                ST_X(ST_ClosestPoint(s.the_geom, a.ring))
                            ) AS h3
                            FROM routing.houston_ways s, arc a
                            WHERE s.name ILIKE '%%' || %s || '%%'
                              AND ST_DWithin(
                                    s.the_geom::geography,
                                    app_private.coords_to_point(%s, %s)::geography,
                                    (%s + 0.2) * 1609.34
                              )
                            ORDER BY ST_Distance(s.the_geom, a.ring) ASC
                            LIMIT 1
                        """, (
                            current_lat, current_lng, geocoded_miles,
                            cleaned,
                            current_lat, current_lng, geocoded_miles
                        ))
                        row = cur.fetchone()
                        if row and row['h3']:
                            logging.info(
                                f"✅ Arc-banding succeeded with geocoded distance: {row['h3']}"
                            )
                            return row['h3']
                        else:
                            logging.warning(
                                f"⚠️ Arc-banding: no match at geocoded {geocoded_miles:.2f}mi either"
                            )
                    except Exception as e:
                        logging.warning(f"⚠️ Arc-banding geocoded fallback failed: {e}")
        except Exception as e:
            logging.warning(f"⚠️ Arc-banding failed: {e}")

    # ── TIER 3: Uber geocoded snap ────────────────────────────────────────────
    logging.info("📍 Last resort: Uber geocoded coords")
    try:
        cur.execute(
            "SELECT app_private.coords_to_h3(%s, %s)::text AS h3",
            (p_lat, p_lng)
        )
        row = cur.fetchone()
        if row and row['h3']:
            logging.info(f"Pickup triangulation (Uber snap) succeeded: {row['h3']}")
            return row['h3']
        else:
            logging.warning("Pickup triangulation: no result for Uber geocoded point")
    except Exception as e:
        logging.warning(f"Pickup triangulation failed: {e}")
    return None


def triangulate_dropoff(
    pickup_lat, pickup_lng,
    dropoff_lat, dropoff_lng,
    trip_miles,
    cur,
    street_name=None,
    driver_lat=None,
    driver_lng=None
):
    if not all([pickup_lat, pickup_lng, dropoff_lat, dropoff_lng, trip_miles]):
        return None
    if trip_miles <= 0:
        return None

    geocoded_miles = None
    try:
        cur.execute(
            "SELECT app_private.distance_miles(%s, %s, %s, %s) AS dist",
            (pickup_lat, pickup_lng, dropoff_lat, dropoff_lng)
        )
        row = cur.fetchone()
        geocoded_miles = float(row["dist"]) if row else None
    except Exception:
        pass

    arc_outer = trip_miles / TORT_MIN
    is_hallucination = geocoded_miles is not None and geocoded_miles > arc_outer * 1.5

    if is_hallucination:
        logging.info(
            f"⚠️ Dropoff geocode hallucinated ({geocoded_miles:.1f}mi vs {trip_miles}mi). "
            f"Pivoting to arc-banding for '{street_name}'..."
        )
        if street_name:
            try:
                cleaned = clean_street_name(street_name)
                order_clause = (
                    "ST_Distance(s.the_geom::geography, "
                    "app_private.coords_to_point(%s, %s)::geography) ASC"
                    if driver_lat and driver_lng
                    else "ST_Distance(s.the_geom, a.ring) ASC"
                )
                params_base = (
                    pickup_lat, pickup_lng, trip_miles,
                    cleaned,
                    pickup_lat, pickup_lng, trip_miles
                )
                params_full = params_base + (
                    (driver_lat, driver_lng) if driver_lat and driver_lng else ()
                )
                cur.execute(f"""
                    WITH arc AS (
                        SELECT ST_Buffer(
                            app_private.coords_to_point(%s, %s)::geography,
                            %s * 1609.34
                        )::geometry AS ring
                    )
                    SELECT app_private.coords_to_h3(
                        ST_Y(ST_ClosestPoint(s.the_geom, a.ring)),
                        ST_X(ST_ClosestPoint(s.the_geom, a.ring))
                    ) AS h3
                    FROM routing.houston_ways s, arc a
                    WHERE s.name ILIKE '%%' || %s || '%%'
                      AND ST_DWithin(
                            s.the_geom::geography,
                            app_private.coords_to_point(%s, %s)::geography,
                            (%s / 0.9 + 0.5) * 1609.34
                      )
                    ORDER BY {order_clause}
                    LIMIT 1
                """, params_full)
                row = cur.fetchone()
                if row and row["h3"]:
                    logging.info(f"🎯 Dropoff arc-banding succeeded: {row['h3']}")
                    return row["h3"]
                else:
                    logging.warning(
                        f"⚠️ Dropoff arc-banding: no match for '{street_name}'"
                    )
            except Exception as e:
                logging.warning(f"⚠️ Dropoff arc-banding failed: {e}")
        return None

    try:
        cur.execute(_TRIANGULATION_SQL, (
            dropoff_lat, dropoff_lng,
            dropoff_lat, dropoff_lng,
            dropoff_lat, dropoff_lng,
        ))
        row = cur.fetchone()
        if row and row["h3"]:
            logging.info(f"Dropoff triangulation succeeded: {row['h3']}")
            return row["h3"]
        else:
            logging.warning("Dropoff triangulation: no street found near geocoded point")
    except Exception as e:
        logging.warning(f"Dropoff triangulation failed: {e}")
    return None


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
