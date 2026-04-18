import logging
import json
import traceback
import math
import os
from triangulation import triangulate_pickup, triangulate_dropoff, h3_to_coords
import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(__file__)))
from state_machine import DriverStateMachine

def _cache_trip_polyline_on_accept(driver_id, offer_id, pickup_lat, pickup_lng,
                                    dropoff_lat, dropoff_lng, cur, conn):
    """Sync Routes API fetch + cache. Non-blocking semantics — all failures
    logged and swallowed. Called immediately after successful offer_accepted
    state machine transition. Populates app_private.trip_route_cache so
    Scorer B can read polyline during cluster scoring."""
    import logging
    if not (pickup_lat and pickup_lng and dropoff_lat and dropoff_lng):
        logging.info(f"[ROUTES_CACHE] skipped — missing coords offer={offer_id}")
        return
    try:
        result = _fetch_trip_polyline(float(pickup_lat), float(pickup_lng),
                                      float(dropoff_lat), float(dropoff_lng))
        status = result.get('status', 'unknown')
        latency = result.get('latency_ms')
        if status == 'ok':
            cur.execute("""
                INSERT INTO app_private.trip_route_cache (
                    driver_id, offer_id, encoded_polyline,
                    pickup_lat, pickup_lng, dropoff_lat, dropoff_lng,
                    api_latency_ms, api_status
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (driver_id, offer_id) DO UPDATE SET
                    encoded_polyline = EXCLUDED.encoded_polyline,
                    api_latency_ms   = EXCLUDED.api_latency_ms,
                    api_status       = EXCLUDED.api_status,
                    fetched_at       = NOW()
            """, (driver_id, offer_id, result['encoded_polyline'],
                  pickup_lat, pickup_lng, dropoff_lat, dropoff_lng,
                  latency, status))
            conn.commit()
            logging.info(f"[ROUTES_CACHE] ✅ cached offer={offer_id} "
                         f"dist={result.get('distance_m')}m {latency}ms")
        else:
            # Log the failure but still record the attempt for post-drive analysis
            cur.execute("""
                INSERT INTO app_private.trip_route_cache (
                    driver_id, offer_id, encoded_polyline,
                    pickup_lat, pickup_lng, dropoff_lat, dropoff_lng,
                    api_latency_ms, api_status
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (driver_id, offer_id) DO NOTHING
            """, (driver_id, offer_id, '',
                  pickup_lat, pickup_lng, dropoff_lat, dropoff_lng,
                  latency, status))
            conn.commit()
            logging.warning(f"[ROUTES_CACHE] ⚠️  status={status} offer={offer_id} {latency}ms")
    except Exception as e:
        logging.exception(f"[ROUTES_CACHE] exception (non-fatal): {e}")
        try:
            conn.rollback()
        except Exception:
            pass


def _cache_stacked_polyline_async(driver_id: str, offer_id: str,
                                    dropoff_lat: float, dropoff_lng: float) -> None:
    """Async executor target: fetch polyline for a stacked offer at promotion time.

    Called from _refine_executor in driver_heartbeat.py immediately after a
    STACKED→ENROUTE atomic swap. Unlike _cache_trip_polyline_on_accept, this
    opens its own DB connection (caller's cursor is heartbeat-scoped and will be
    released before this completes).

    Pickup coords are fetched from offer_history by decision_log_id. This is
    safe because the secondary offer_history row was written at offer_accepted
    time (when the stack was accepted), minutes before the swap fires.

    All failures are logged and swallowed — never raises.
    """
    import logging
    from db import get_db
    conn = None
    cur = None
    try:
        conn = get_db()
        cur = conn.cursor()
        # Fetch secondary's pickup coords from offer_history
        cur.execute("""
            SELECT pickup_lat, pickup_lng
            FROM app_private.offer_history
            WHERE decision_log_id = %s::integer
        """, (offer_id,))
        row = cur.fetchone()
        if not row or row[0] is None or row[1] is None:
            logging.warning(f"[ROUTES_CACHE/STACK] no pickup coords for offer={offer_id} — skipping")
            return
        pickup_lat, pickup_lng = float(row[0]), float(row[1])

        result = _fetch_trip_polyline(pickup_lat, pickup_lng,
                                       float(dropoff_lat), float(dropoff_lng))
        status = result.get('status', 'unknown')
        latency = result.get('latency_ms')
        if status == 'ok':
            cur.execute("""
                INSERT INTO app_private.trip_route_cache (
                    driver_id, offer_id, encoded_polyline,
                    pickup_lat, pickup_lng, dropoff_lat, dropoff_lng,
                    api_latency_ms, api_status
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (driver_id, offer_id) DO UPDATE SET
                    encoded_polyline = EXCLUDED.encoded_polyline,
                    api_latency_ms   = EXCLUDED.api_latency_ms,
                    api_status       = EXCLUDED.api_status,
                    fetched_at       = NOW()
            """, (driver_id, offer_id, result['encoded_polyline'],
                  pickup_lat, pickup_lng, dropoff_lat, dropoff_lng,
                  latency, status))
            conn.commit()
            logging.info(f"[ROUTES_CACHE/STACK] ✅ cached offer={offer_id} "
                         f"dist={result.get('distance_m')}m {latency}ms")
        else:
            cur.execute("""
                INSERT INTO app_private.trip_route_cache (
                    driver_id, offer_id, encoded_polyline,
                    pickup_lat, pickup_lng, dropoff_lat, dropoff_lng,
                    api_latency_ms, api_status
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (driver_id, offer_id) DO NOTHING
            """, (driver_id, offer_id, '',
                  pickup_lat, pickup_lng, dropoff_lat, dropoff_lng,
                  latency, status))
            conn.commit()
            logging.warning(f"[ROUTES_CACHE/STACK] ⚠️  status={status} offer={offer_id} {latency}ms")
    except Exception as e:
        logging.exception(f"[ROUTES_CACHE/STACK] exception (non-fatal): {e}")
        try:
            if conn:
                conn.rollback()
        except Exception:
            pass
    finally:
        try:
            if cur:
                cur.close()
        except Exception:
            pass
        try:
            if conn:
                conn.close()
        except Exception:
            pass

from routes_api import fetch_trip_polyline as _fetch_trip_polyline




# ======================================================================
# MAPBOX DROPOFF REFINEMENT
# ======================================================================
def _bearing(lat1, lng1, lat2, lng2):
    lat1, lat2 = math.radians(lat1), math.radians(lat2)
    dlng = math.radians(lng2 - lng1)
    x = math.sin(dlng) * math.cos(lat2)
    y = math.cos(lat1)*math.sin(lat2) - math.sin(lat1)*math.cos(lat2)*math.cos(dlng)
    return (math.degrees(math.atan2(x, y)) + 360) % 360

def _project_point(lat, lng, bearing_deg, distance_miles=20.0):
    R = 3958.8
    b = math.radians(bearing_deg)
    lat1 = math.radians(lat); lng1 = math.radians(lng)
    lat2 = math.asin(math.sin(lat1)*math.cos(distance_miles/R) +
                     math.cos(lat1)*math.sin(distance_miles/R)*math.cos(b))
    lng2 = lng1 + math.atan2(math.sin(b)*math.sin(distance_miles/R)*math.cos(lat1),
                              math.cos(distance_miles/R)-math.sin(lat1)*math.sin(lat2))
    return math.degrees(lat2), math.degrees(lng2)

def _hav_miles(lon1, lat1, lon2, lat2):
    lon1,lat1,lon2,lat2 = map(math.radians,[lon1,lat1,lon2,lat2])
    dlon=lon2-lon1; dlat=lat2-lat1
    a = math.sin(dlat/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin(dlon/2)**2
    return 3956 * 2 * math.asin(math.sqrt(a))

def refine_dropoff_background(driver_id, pickup_lat, pickup_lng,
                               vague_dropoff_lat, vague_dropoff_lng,
                               trip_miles, label=''):
    import psycopg2, requests as _req
    conn = cur = None
    try:
        MAPBOX_KEY = os.environ.get('MAPBOX_API_KEY') or os.environ.get('MAPBOX_TOKEN')
        if not MAPBOX_KEY:
            logging.warning('[REFINE] No MAPBOX_API_KEY'); return

        # Step 1: route pickup -> vague_dropoff to get road distance to geocode
        url = (f'https://api.mapbox.com/directions/v5/mapbox/driving/'
               f'{pickup_lng},{pickup_lat};{vague_dropoff_lng},{vague_dropoff_lat}'
               f'?geometries=geojson&overview=full&access_token={MAPBOX_KEY}')
        data = _req.get(url, timeout=5.0).json()

        if not data.get('routes'):
            logging.warning('[REFINE] No routes'); return

        coords = data['routes'][0]['geometry']['coordinates']
        road_dist = data['routes'][0]['distance'] / 1609.34

        # Gate: trust geocode if trip too short, delta_pct<25% OR delta_abs<1.0mi
        delta_abs = abs(road_dist - trip_miles)
        delta_pct = delta_abs / trip_miles if trip_miles else 1.0
        if trip_miles < 2.0:
            logging.info(f'[REFINE{label}] Short trip ({trip_miles}mi) — skipping refinement')
            return
        if delta_pct < 0.25 or delta_abs < 1.0:
            logging.info(f'[REFINE{label}] Geocode trusted — delta {delta_abs:.2f}mi ({delta_pct*100:.0f}%)')
            return

        logging.info(f'[REFINE{label}] Refining — road_dist={road_dist:.2f}mi trip_miles={trip_miles}mi delta={delta_abs:.2f}mi')

        # Step 2: if geocode is short of trip_miles, extend past it
        if road_dist < trip_miles:
            bear = _bearing(pickup_lat, pickup_lng, vague_dropoff_lat, vague_dropoff_lng)
            ext_lat, ext_lng = _project_point(vague_dropoff_lat, vague_dropoff_lng, bear, 10.0)
            ext_url = (f'https://api.mapbox.com/directions/v5/mapbox/driving/'
                       f'{pickup_lng},{pickup_lat};{vague_dropoff_lng},{vague_dropoff_lat};{ext_lng},{ext_lat}'
                       f'?geometries=geojson&overview=full&access_token={MAPBOX_KEY}')
            ext_data = _req.get(ext_url, timeout=5.0).json()
            if not ext_data.get('routes'):
                logging.warning('[REFINE] Extension route failed'); return
            coords = ext_data['routes'][0]['geometry']['coordinates']

        total_miles = sum(_hav_miles(coords[i-1][0],coords[i-1][1],coords[i][0],coords[i][1])
                         for i in range(1,len(coords)))
        if total_miles < trip_miles:
            logging.warning(f'[REFINE] Route {total_miles:.2f}mi < {trip_miles}mi'); return

        cumulative = 0.0; refined_lat = refined_lng = None
        for i in range(1, len(coords)):
            cumulative += _hav_miles(coords[i-1][0],coords[i-1][1],coords[i][0],coords[i][1])
            if cumulative >= trip_miles:
                refined_lng = coords[i][0]; refined_lat = coords[i][1]; break

        if not refined_lat:
            logging.warning('[REFINE] Could not find trip_miles mark'); return

        delta_m = round(abs(cumulative - trip_miles) * 1609.34)
        logging.info(f'[REFINE{label}] ({vague_dropoff_lat:.5f},{vague_dropoff_lng:.5f})'
                     f' -> ({refined_lat:.5f},{refined_lng:.5f}) +-{delta_m}m')

        conn = psycopg2.connect(host=os.environ.get('DB_HOST','10.128.0.2'),
                                user=os.environ.get('DB_USER','postgres'),
                                dbname=os.environ.get('DB_NAME','puddlejumper'))
        cur = conn.cursor()
        cur.execute("""UPDATE app_private.driver_trip_state
                       SET nailed_dropoff_lat=%s, nailed_dropoff_lng=%s, nailed_dropoff_error_m=%s
                       WHERE driver_id=%s AND state IN ('ENROUTE','IN_TRIP')""",
                    (refined_lat, refined_lng, delta_m, driver_id))
        conn.commit()
        logging.info(f'[REFINE{label}] nailed_dropoff updated')

    except Exception as e:
        logging.error(f'[REFINE{label}] {type(e).__name__}: {e}')
    finally:
        if cur: cur.close()
        if conn: conn.close()


# ======================================================================
# PIPELINE STAGE 5 — enrich_with_triangulation
# ======================================================================
def enrich_with_triangulation(cur, conn, uid, ep, result, driver_state, decision_log_id):
    """
    Triangulate pickup/dropoff, log shadow signal, update driver state machine.
    Never raises. Enriches result in-place. Returns result.

    CRITICAL FIX: all triangulated_* variables are declared at the top of this
    function and assigned before any DB write or patch. This eliminates the
    NameError bug that caused universal truncation of decision_result.
    """
    try:
        if not (ep["current_lat"] and ep["current_lng"]):
            return result

        reported                = ep["pickup_miles"] or 0
        geocoded_miles          = None
        delta_pct               = 1.0
        is_validated            = False
        pickup_h3               = None
        triangulated_h3         = None
        triangulated_pickup_lat  = None   # declared here -- before any branch
        triangulated_pickup_lng  = None
        triangulated_dropoff_h3  = None
        triangulated_dropoff_lat = None
        triangulated_dropoff_lng = None

        if ep["p_lat"] and ep["p_lng"]:
            # Geocoded distance
            cur.execute(
                "SELECT app_private.distance_miles(%s, %s, %s, %s) AS dist",
                (ep["current_lat"], ep["current_lng"], ep["p_lat"], ep["p_lng"])
            )
            geo_row = cur.fetchone()
            geocoded_miles = float(geo_row["dist"]) if (geo_row and geo_row.get("dist") is not None) else None

            if geocoded_miles is not None and reported > 0:
                raw_delta    = abs(geocoded_miles - reported) / reported
                is_validated = (
                    abs(geocoded_miles - reported) < 1.5
                    if reported < 3
                    else raw_delta < 0.60
                )
                delta_pct = raw_delta
            else:
                delta_pct    = 1.0
                is_validated = False

            cur.execute(
                "SELECT app_private.safe_h3(%s, %s)::text AS h3",
                (ep["p_lat"], ep["p_lng"])
            )
            r = cur.fetchone()
            pickup_h3 = r["h3"] if (r and r.get("h3")) else None

            # Triangulate pickup
            triangulated_h3 = triangulate_pickup(
                driver_state["arc_center_lat"], driver_state["arc_center_lng"],
                ep["p_lat"], ep["p_lng"],
                reported, geocoded_miles, cur,
                street_name=ep["pickup_address"],
                gps_age_sec=ep["gps_age_sec"],
                pre_geocoded=False,  # Uber never sends pickup coords; driver GPS is not a pre-geocode
            )

            # Resolve pickup lat/lng immediately -- before any DB write or patch
            if triangulated_h3:
                pickup_coords = h3_to_coords(triangulated_h3, cur)
                if pickup_coords:
                    triangulated_pickup_lat = pickup_coords[0]
                    triangulated_pickup_lng = pickup_coords[1]

            # Same-address guard: circular trip / errand detection
            p_addr = (ep.get("pickup_address") or "").strip().lower()
            d_addr = (ep.get("dropoff_address") or "").strip().lower()
            _same_address = bool(p_addr and d_addr and p_addr == d_addr)
            if _same_address:
                logging.info(f"[TRI] Same-address errand detected ({p_addr}) — mirroring pickup coords")
                triangulated_dropoff_lat = triangulated_pickup_lat or ep.get("p_lat")
                triangulated_dropoff_lng = triangulated_pickup_lng or ep.get("p_lng")
                triangulated_dropoff_h3  = triangulated_h3

            # Triangulate dropoff (uses resolved pickup coords, not raw h3)
            if (not _same_address and triangulated_h3 and triangulated_pickup_lat and triangulated_pickup_lng
                    and ep["d_lat"] and ep["d_lng"] and ep["trip_miles"]):
                triangulated_dropoff_h3 = triangulate_dropoff(
                    triangulated_pickup_lat, triangulated_pickup_lng,
                    ep["d_lat"], ep["d_lng"],
                    ep["trip_miles"], cur,
                    street_name=ep["dropoff_address"],
                    driver_lat=ep["current_lat"],
                    driver_lng=ep["current_lng"],
                )
                if triangulated_dropoff_h3:
                    dropoff_coords = h3_to_coords(triangulated_dropoff_h3, cur)
                    if dropoff_coords:
                        triangulated_dropoff_lat = dropoff_coords[0]
                        triangulated_dropoff_lng = dropoff_coords[1]
                        logging.info(
                            f"[TRI] Dropoff triangulated: {triangulated_dropoff_h3} "
                            f"({triangulated_dropoff_lat:.4f}, "
                            f"{triangulated_dropoff_lng:.4f})"
                        )

        # ── Confidence tier ───────────────────────────────────────────
        if triangulated_h3 and is_validated:
            confidence_tier   = "high"
            confidence_radius = 400
            odometer_floor    = 0.92
        elif is_validated:
            confidence_tier   = "medium"
            confidence_radius = 600
            odometer_floor    = 0.88
        else:
            confidence_tier   = "low"
            confidence_radius = 1000
            odometer_floor    = 0.75
        odometer_ceiling = 1.40

        # ── Enrich result (ALL fields set before any DB write) ─────────
        result["confidenceTier"]         = confidence_tier
        result["confidenceRadius"]       = confidence_radius
        result["odometerFloor"]          = odometer_floor
        result["odometerCeiling"]        = odometer_ceiling
        result["triangulatedPickupLat"]  = triangulated_pickup_lat
        result["triangulatedPickupLng"]  = triangulated_pickup_lng
        result["triangulatedDropoffLat"] = triangulated_dropoff_lat
        result["triangulatedDropoffLng"] = triangulated_dropoff_lng

        # ── Shadow signal ─────────────────────────────────────────────
        cur.execute(
            "SELECT app_private.safe_h3(%s, %s)::text AS h3",
            (ep["current_lat"], ep["current_lng"])
        )
        r = cur.fetchone()
        driver_h3 = r.get("h3") if r else None

        if driver_h3:
            final_pickup_h3 = pickup_h3 or triangulated_h3
            if pickup_h3:         data_source = "geocode"
            elif triangulated_h3: data_source = "triangulated"
            else:                 data_source = "unresolved"

            cur.execute(
                "INSERT INTO app_private.pickup_market_signals "
                "(offer_id, driver_h3, pickup_h3, reported_miles, geocoded_miles, "
                "distance_delta_pct, is_validated, hourly_rate_offered, "
                "dollars_per_mile, day_of_week, hour_of_day, is_accepted, "
                "data_source, offer_status) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, "
                "EXTRACT(DOW  FROM NOW() AT TIME ZONE 'America/Chicago')::integer, "
                "EXTRACT(HOUR FROM NOW() AT TIME ZONE 'America/Chicago')::integer, "
                "%s, %s, %s)",
                (
                    decision_log_id, driver_h3, final_pickup_h3,
                    reported, geocoded_miles, delta_pct, is_validated,
                    result.get("hourlyRate"), result.get("dollarsPerMile"),
                    result["verdict"] == "ACCEPT",
                    data_source,
                    "pending" if result["verdict"] == "ACCEPT" else "declined",
                )
            )
            conn.commit()
            logging.info(
                f"[SHADOW] Signal logged -- validated: {is_validated}, "
                f"accepted: {result['verdict'] == 'ACCEPT'}"
            )

            # ── Driver trip state machine ──────────────────────────────
            if pickup_h3 and ep.get("p_lat") and ep.get("p_lng"):
                final_pickup_h3  = pickup_h3
                final_pickup_lat = ep["p_lat"]
                final_pickup_lng = ep["p_lng"]
            elif triangulated_h3:
                final_pickup_h3  = triangulated_h3
                final_pickup_lat = triangulated_pickup_lat
                final_pickup_lng = triangulated_pickup_lng
            else:
                final_pickup_h3  = None
                final_pickup_lat = ep["p_lat"] if ep.get("p_lat") else None
                final_pickup_lng = ep["p_lng"] if ep.get("p_lng") else None
                logging.info(
                    f"[WARN] Triangulation failed -- falling back to raw Android coords "
                    f"({final_pickup_lat}, {final_pickup_lng})"
                )

            # ── State machine transition (replaces update_driver_state) ──
            _verdict       = result["verdict"]
            _current_state = driver_state["state"]
            _dropoff_lat   = triangulated_dropoff_lat or (ep["d_lat"] if is_validated else None)
            _dropoff_lng   = triangulated_dropoff_lng or (ep["d_lng"] if is_validated else None)

            if _verdict == "ACCEPT" and _current_state in ("UNCOMMITTED", "ENROUTE"):
                _sm_result = DriverStateMachine.transition(
                    uid, "offer_accepted", cur, conn,
                    offer_id=str(decision_log_id) if decision_log_id else None,
                    pickup_lat=final_pickup_lat, pickup_lng=final_pickup_lng,
                    pickup_h3=final_pickup_h3,
                    dropoff_lat=_dropoff_lat, dropoff_lng=_dropoff_lng,
                    dropoff_h3=triangulated_dropoff_h3,
                )
                # Contest Mode (Rev 00557/00558): sync Routes API cache on accept.
                # This is the decision path, not heartbeat hot path — sync is acceptable.
                if (_sm_result and _sm_result.get("success")
                        and final_pickup_lat and final_pickup_lng
                        and _dropoff_lat and _dropoff_lng):
                    _cache_trip_polyline_on_accept(
                        uid,
                        str(decision_log_id) if decision_log_id else None,
                        final_pickup_lat, final_pickup_lng,
                        _dropoff_lat, _dropoff_lng,
                        cur, conn,
                    )
            elif _verdict == "ACCEPT" and _current_state in ("IN_TRIP", "REFINE_DROPOFF"):
                DriverStateMachine.transition(
                    uid, "offer_accepted", cur, conn,
                    offer_id=str(decision_log_id) if decision_log_id else None,
                    pickup_lat=final_pickup_lat, pickup_lng=final_pickup_lng,
                    pickup_h3=final_pickup_h3,
                    # STACKED: preserve existing dropoff — only pass new pickup
                )
            elif _verdict != "ACCEPT" and _current_state == "ENROUTE":
                # S04 already handled implicit cancel in state_enricher;
                # this fires if it somehow slipped through
                DriverStateMachine.transition(
                    uid, "offer_cancelled_implicit", cur, conn,
                    clear_coords=True,
                )
            elif _verdict != "ACCEPT" and _current_state == "STACKED":
                # New offer while STACKED = secondary cancelled (Uber wouldn't offer
                # a new ride if the existing secondary was still valid).
                # Assume Scenario A: secondary cancelled, primary still active.
                # Fire offer_declined → IN_TRIP, restore primary dropoff coords.
                # Scenario B (primary completed, nail missed) handled by self-healing route.
                try:
                    cur.execute("""
                        SELECT oh.decision_log_id,
                               oh.dropoff_lat, oh.dropoff_lng, oh.dropoff_h3
                        FROM app_private.offer_history oh
                        JOIN app_private.decision_log dl ON dl.id = oh.decision_log_id
                        WHERE dl.driver_id = %s
                          AND oh.actual_pickup_at IS NOT NULL
                          AND oh.actual_dropoff_at IS NULL
                        ORDER BY oh.actual_pickup_at DESC
                        LIMIT 1
                    """, (uid,))
                    _primary = cur.fetchone()
                    if _primary:
                        DriverStateMachine.transition(
                            uid, "offer_declined", cur, conn,
                            offer_id=str(_primary["decision_log_id"]),
                            dropoff_lat=_primary["dropoff_lat"],
                            dropoff_lng=_primary["dropoff_lng"],
                            dropoff_h3=_primary["dropoff_h3"],
                        )
                        logging.warning(
                            f"[S04-STACKED] Secondary cancelled — demoted to IN_TRIP "
                            f"on primary offer={_primary['decision_log_id']} "
                            f"dropoff=({_primary['dropoff_lat']},{_primary['dropoff_lng']})"
                        )
                    else:
                        # No active primary found — full reset
                        DriverStateMachine.transition(
                            uid, "manual_reset", cur, conn,
                            clear_coords=True,
                        )
                        logging.warning("[S04-STACKED] Secondary cancelled, no active primary — full reset to UNCOMMITTED")
                except Exception as _sc_err:
                    logging.warning(f"[S04-STACKED] Secondary cancel handling failed: {_sc_err}")
                    try: conn.rollback()
                    except: pass
            else:
                # DECLINE + UNCOMMITTED or IN_TRIP: no state change, no commit needed
                logging.info(
                    f"[SM] No transition fired: verdict={_verdict} state={_current_state}"
                )

    except Exception as tri_err:
        logging.error(
            f"[ERROR] enrich_with_triangulation failed: "
            f"{type(tri_err).__name__}: {tri_err}\n"
            f"{traceback.format_exc()}"
        )
        try:    conn.rollback()
        except: pass

    return result

