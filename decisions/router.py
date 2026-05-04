from concurrent.futures import ThreadPoolExecutor
# backend/decisions.py
# VERSION: 8.0 - Added ocr_confidence storage
import os
import random
import json
import traceback
import logging

from dotenv import load_dotenv
from flask import Blueprint, request, jsonify
from psycopg2.extras import RealDictCursor

# 🟢 RESTORING ORIGINAL WORKING IMPORTS
from db import get_db
from utils import verify_and_get_user_id, require_firebase_auth

load_dotenv()
decisions_bp = Blueprint('decisions', __name__)
executor = ThreadPoolExecutor(max_workers=4)

# ======================================================================
# HELPER: Get Coordinates from Hex via SQL
# ======================================================================
def get_coords_from_hex(cur, hex_code):
    cur.execute("""
        SELECT 
            app_private.h3_to_lat(%s) AS lat,
            app_private.h3_to_lng(%s) AS lng
    """, (hex_code, hex_code))
    row = cur.fetchone()
    if not row: return None
    return {"lat": float(row['lat']), "lng": float(row['lng'])}

# ======================================================================
# ROUTES
# ======================================================================

@require_firebase_auth
@decisions_bp.route("/history", methods=["GET"])
def get_decision_history():
    try:
        uid = verify_and_get_user_id(request)
        limit = request.args.get('limit', 10, type=int)

        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        cur.execute("""
            SELECT 
                id, driver_id as "driverId", created_at as "createdAt", 
                fare, pickup_minutes as "pickupMinutes", trip_minutes as "tripMinutes", 
                pickup_lat as "pickupLat", pickup_lng as "pickupLng", 
                dropoff_lat as "dropoffLat", dropoff_lng as "dropoffLng", 
                ping_h3_index as "pingH3Index", market_id as "marketId",
                decision_result as "decisionResult"
            FROM app_private.decision_log
            WHERE driver_id = %s
            ORDER BY created_at DESC
            LIMIT %s
        """, (uid, limit))

        rows = cur.fetchall()
        history = []
        for row in rows:
            if isinstance(row.get('decisionResult'), str):
                row['decisionResult'] = json.loads(row['decisionResult'])
            history.append(row)

        return jsonify({"status": "success", "history": history}), 200

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
    finally:
        if 'conn' in locals(): conn.close()


@require_firebase_auth
@decisions_bp.route("/simulate-suite", methods=["POST"])
def simulate_test_suite():
    try:
        uid = verify_and_get_user_id(request)
        p = request.get_json() or {}
        market_id = p.get("marketId")

        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        cur.execute("""
            WITH driver_data AS (SELECT settings FROM app_private.driver_settings_new WHERE driver_id = %s),
            target_market AS (SELECT elem FROM driver_data, jsonb_array_elements(settings->'markets') elem WHERE elem->>'id' = %s),
            green_hexes AS (SELECT jsonb_array_elements_text(elem->'greenZones') as hex FROM target_market),
            red_hexes AS (SELECT jsonb_array_elements_text(settings->'redZones') as hex FROM driver_data)
            SELECT (SELECT array_agg(hex) FROM green_hexes) as green_list,
                   (SELECT array_agg(hex) FROM red_hexes) as red_list
        """, (uid, market_id))

        data = cur.fetchone()
        green_list = data.get('green_list') or []
        red_list = data.get('red_list') or []
        
        if not green_list:
            return jsonify({"error": "No Green Zones found."}), 400

        g1_coords = get_coords_from_hex(cur, random.choice(green_list))
        g2_coords = get_coords_from_hex(cur, random.choice(green_list))

        archetypes = [
            ("The Perfect Ride", "Green Zone, high pay.", 15.0, 5.0, 12.0, g2_coords),
            ("The Lowball", "Green Zone, low pay.", 4.50, 5.0, 12.0, g2_coords),
            ("The Deadhead Trap", "Drops you 25 miles away.", 22.0, 25.0, 35.0, None)
        ]

        if red_list:
            r_coords = get_coords_from_hex(cur, random.choice(red_list))
            archetypes.append(("Safety Hazard", "Ends in your Red Zone.", 25.0, 6.0, 15.0, r_coords))

        suite_results = []
        for title, desc, fare, miles, minutes, target in archetypes:
            d_lat = target['lat'] if target else (g1_coords['lat'] + 0.35)
            d_lng = target['lng'] if target else g1_coords['lng']
            
            cur.execute("""
                SELECT * FROM app_private.decision_engine_v2(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (uid, g1_coords['lat'], g1_coords['lng'], d_lat, d_lng, fare, miles, minutes, 3.0, 1.0, market_id))
            row = cur.fetchone()
            
            suite_results.append({
                "title": title,
                "description": desc,
                "inputValues": {
                    "marketId": market_id, "lat": g1_coords['lat'], "lng": g1_coords['lng'],
                    "dropoffLat": d_lat, "dropoffLng": d_lng, "fare": fare,
                    "tripMiles": miles, "tripMinutes": minutes, "pickupMinutes": 3.0
                },
                "result": {
                    "verdict": row['verdict'],
                    "reason": row['reason'],
                    "netPay": float(row['net_pay']),
                    "hourlyRate": float(row['hourly_rate']),
                    "deadheadCost": float(row['deadhead_cost'])
                }
            })

        return jsonify({"suite": suite_results}), 200

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
    finally:
        if 'conn' in locals(): conn.close()

# decisions_pipeline.py
# Pipeline stage functions for decisions.py refactor.
# Inserted before make_decision() by patch_decisions.py.
# VERSION: 9.0 - Pipeline architecture

# ── Pipeline sub-modules ──────────────────────────────────────────────
from .engine                 import run_decision_engine
from .logger                 import log_decision, patch_decision_log
from .triangulation_enricher import enrich_with_triangulation


# ======================================================================
# PIPELINE STAGE 1 — parse_request
# ======================================================================
def parse_request(p, uid):
    """Extract and type all request parameters. Never fails. Returns params dict."""
    fare            = float(p.get("fare") or 0)
    trip_miles      = float(p.get("tripMiles") or 0)
    trip_min        = float(p.get("tripMinutes") or 0)
    pickup_min      = float(p.get("pickupMinutes") or 0)
    pickup_miles    = float(p.get("pickupMiles") or (pickup_min * 0.33))

    yolo_swap_suspected = False
    if pickup_min > 0 and pickup_miles > 0:
        max_realistic_miles = pickup_min * 0.75
        if pickup_miles > max_realistic_miles:
            yolo_swap_suspected = True
            logging.warning(
                f"[WARN] YOLO swap suspected: {pickup_miles}mi in {pickup_min}min "
                f"({round(pickup_miles / pickup_min * 60, 1)}mph). Flagging only."
            )

    d_lat = float(p.get("dropoffLat") or 0)
    d_lng = float(p.get("dropoffLng") or 0)
    p_lat = float(p.get("lat") or 0)
    p_lng = float(p.get("lng") or 0)

    current_lat = p.get("currentLat")
    current_lng = p.get("currentLng")
    if current_lat is not None: current_lat = float(current_lat)
    if current_lng is not None: current_lng = float(current_lng)

    gps_age_sec = p.get("gpsAgeSec")
    if gps_age_sec is not None: gps_age_sec = float(gps_age_sec)

    market_id                   = p.get("marketId")
    towards_active              = bool(p.get("towardsActive", False))
    towards_target_lat          = p.get("towardsTargetLat")
    towards_target_lng          = p.get("towardsTargetLng")
    towards_market_id           = p.get("towardsMarketId")
    is_puddle_jump              = bool(p.get("isPuddleJumpMode", True))
    towards_backtrack_tolerance = float(p.get("towardsBacktrackTolerance", 3.0))

    if towards_target_lat is not None: towards_target_lat = float(towards_target_lat)
    if towards_target_lng is not None: towards_target_lng = float(towards_target_lng)

    ocr_confidence  = p.get("ocrConfidence")
    pickup_address  = p.get("pickupAddress")
    dropoff_address = p.get("dropoffAddress")
    ride_type       = p.get("rideType") or p.get("vehicleType")
    is_surge        = bool(p.get("isSurge", False))
    is_priority     = bool(p.get("isPriority", False))
    is_reserve      = bool(p.get("isReserve", False))

    if towards_active:
        mode_name   = "TOWARDS"
        market_name = p.get("marketName")
    elif is_puddle_jump:
        mode_name   = "PUDDLE_JUMP"
        market_name = p.get("marketName")
    else:
        mode_name   = "FREESTYLE"
        market_name = None

    return {
        "fare": fare, "trip_miles": trip_miles, "trip_min": trip_min,
        "pickup_min": pickup_min, "pickup_miles": pickup_miles,
        "yolo_swap_suspected": yolo_swap_suspected,
        "d_lat": d_lat, "d_lng": d_lng,
        "p_lat": p_lat, "p_lng": p_lng,
        "current_lat": current_lat, "current_lng": current_lng,
        "gps_age_sec": gps_age_sec, "market_id": market_id,
        "towards_active": towards_active,
        "towards_target_lat": towards_target_lat,
        "towards_target_lng": towards_target_lng,
        "towards_market_id": towards_market_id,
        "is_puddle_jump": is_puddle_jump,
        "towards_backtrack_tolerance": towards_backtrack_tolerance,
        "ocr_confidence": ocr_confidence,
        "pickup_address": pickup_address, "dropoff_address": dropoff_address,
        "ride_type": ride_type, "is_surge": is_surge,
        "is_priority": is_priority, "is_reserve": is_reserve,
        "mode_name": mode_name, "market_name": market_name,
    }


# ======================================================================
# ROUTE -- /decisions/  (orchestrator only, no business logic)
# ======================================================================
@require_firebase_auth
@decisions_bp.route("/", methods=["POST"], strict_slashes=False)
def make_decision():
    import time
    _t0 = time.time()

    # Auth
    try:
        uid = verify_and_get_user_id(request)
    except Exception:
        return jsonify({"error": "Auth failed"}), 403

    p = request.get_json(silent=True) or {}
    logging.info(
        f"[DECISION] Request -- "
        f"currentLat: {p.get('currentLat')}, "
        f"currentLng: {p.get('currentLng')}, "
        f"dropoff: {p.get('dropoffAddress')}, "
        f"gpsAgeSec: {p.get('gpsAgeSec')}"
    )

    # ── Stage 1: Parse request (never fails) ──────────────────────────
    params = parse_request(p, uid)

    conn = get_db()
    cur  = conn.cursor(cursor_factory=RealDictCursor)

    try:
        # ── Stage 2: Decision engine (raises on SQL failure -> 500) ────
        result, arc_band_trace, ep = run_decision_engine(cur, conn, uid, params)

        # ── Stage 3: Log decision (failure is non-fatal) ───────────────
        decision_log_id = None
        try:
            decision_log_id = log_decision(cur, conn, uid, params, ep, result)
        except Exception as log_err:
            logging.error(
                f"[ERROR] log_decision failed (continuing): {log_err}", exc_info=True
            )
            try:    conn.rollback()
            except: pass

        # ── Stage 4: 1-bit memory snapshot for API shape ──────────────
        # Replaces legacy state-machine read + S04 dispatch (demolition
        # 2026-05-04). The new architecture has only NULL or set; we
        # synthesize the legacy enum values Android still reads.
        # See docs/RIDE_LIFECYCLE.md §1.
        try:
            cur.execute(
                "SELECT current_offer_id "
                "FROM app_private.driver_trip_state "
                "WHERE driver_id = %s",
                (uid,)
            )
            _row = cur.fetchone()
            result["driverState"] = (
                "IN_TRIP"
                if (_row and _row.get("current_offer_id"))
                else "UNCOMMITTED"
            )
        except Exception as _state_err:
            logging.warning(f"[STATE] driverState lookup failed: {_state_err}")
            result["driverState"] = "UNCOMMITTED"
            try:    conn.rollback()
            except: pass

        # ── Stage 5: Triangulation + shadow (never fails) ─────────────
        result = enrich_with_triangulation(
            cur, conn, uid, ep, result, decision_log_id
        )

        # ── Stage 5b: Price Radar shadow (never fails) ─────────────────
        try:
            import time as _time
            _t_radar = _time.time()
            # Use best available pickup location — triangulated > raw payload > driver GPS
            p_lat = result.get("triangulatedPickupLat") or ep.get("pickupLat") or ep.get("p_lat")
            p_lng = result.get("triangulatedPickupLng") or ep.get("pickupLng") or ep.get("p_lng")
            if result.get("triangulatedPickupLat"):
                logging.info(f"[RADAR] using triangulated pickup: {p_lat:.5f},{p_lng:.5f}")
            elif ep.get("pickupLat"):
                logging.info(f"[RADAR] using raw payload pickup")
            else:
                logging.info(f"[RADAR] falling back to driver GPS")
            if p_lat and p_lng and abs(p_lat) > 1 and abs(p_lng) > 1:
                cur.execute("""
                    SELECT idw_hourly, idw_mileage, point_count,
                           avg_distance_m, confidence_tier
                    FROM app_private.get_price_radar(%s, %s)
                """, (p_lat, p_lng))
                radar_row = cur.fetchone()
                radar_ms = (_time.time() - _t_radar) * 1000
                if radar_ms > 100:
                    logging.warning(f"[RADAR] ⚠️ Slow query: {radar_ms:.0f}ms")
                if radar_row and radar_row['point_count'] > 0:
                    result["radarHourly"]     = float(radar_row['idw_hourly'] or 0)
                    result["radarMileage"]    = float(radar_row['idw_mileage'] or 0)
                    result["radarPointCount"] = radar_row['point_count']
                    result["radarAvgDistM"]   = float(radar_row['avg_distance_m'] or 0)
                    result["radarConfidence"] = radar_row['confidence_tier']
                    result["radarLatencyMs"]  = round(radar_ms, 1)
                    hex_hourly = result.get("hourlyRate")
                    if hex_hourly and radar_row['idw_hourly']:
                        result["radarVsHexDelta"] = round(
                            float(radar_row['idw_hourly']) - float(hex_hourly), 2
                        )
                    logging.info(
                        f"[RADAR] 📡 {radar_row['confidence_tier'].upper()} "
                        f"n={radar_row['point_count']} "
                        f"idw=${float(radar_row['idw_hourly']):.2f}/hr "
                        f"hex=${result.get('hourlyRate', 0):.2f}/hr "
                        f"delta={result.get('radarVsHexDelta', 'n/a')} "
                        f"{radar_ms:.0f}ms"
                    )
                else:
                    result["radarConfidence"] = "none"
                    result["radarPointCount"] = 0
                    result["radarLatencyMs"]  = round(radar_ms, 1)
                    logging.info(
                        f"[RADAR] 📡 no signal at ({p_lat:.4f},{p_lng:.4f}) "
                        f"{radar_ms:.0f}ms"
                    )
        except Exception as radar_err:
            logging.warning(f"[RADAR] ⚠️ Shadow query failed (non-fatal): {radar_err}")

        # ── Stage 6: Patch decision_log (never fails) ──────────────────
        patch_decision_log(cur, conn, decision_log_id, result)

        # Arc-band street-geometry pre-warm removed with arc_band.py.

        _total_ms = (time.time() - _t0) * 1000
        if _total_ms > 2000:
            logging.warning(
                f"[TIMER] ⚠️ SLOW decision {_total_ms:.0f}ms "
                f"dropoff={ep.get('dropoff_address')!r} "
                f"pickup_miles={ep.get('pickup_miles')} "
                f"trip_miles={ep.get('trip_miles')} "
                f"verdict={result.get('verdict')}"
            )
        else:
            logging.info(f"[TIMER] TOTAL decision time: {_total_ms:.0f}ms")

        # ── Stage 7: Seed geocode cache with Android coords (free, non-blocking) ──
        try:
            pickup_address  = ep.get("pickup_address")
            dropoff_address = ep.get("dropoff_address")
            p_lat = ep.get("p_lat")
            p_lng = ep.get("p_lng")
            d_lat = ep.get("d_lat")
            d_lng = ep.get("d_lng")
            conn.commit()
        except Exception as _ce:
            logging.warning(f"[CACHE] Geocode seed failed (non-fatal): {_ce}")

        # ── Stage 8: Always return ─────────────────────────────────────
        return jsonify(result), 200

    except Exception as e:
        logging.error(f"[ERROR] Decision engine error: {e}")
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()
        conn.close()
        


# ======================================================================
# HARVEST: Async offer data collection (fire-and-forget)
# ======================================================================

def validate_offer(fare, trip_miles, trip_minutes, pickup_miles, pickup_minutes, hourly_rate):
    """Sanity Gatekeeper - flag suspicious OCR data"""
    flags = []
    is_valid = True
    
    # Check 1: Impossible speed (>110 mph)
    if trip_miles and trip_minutes and trip_minutes > 0:
        mph = trip_miles / (trip_minutes / 60)
        if mph > 110:
            flags.append('IMPOSSIBLE_SPEED')
            is_valid = False
    
    # Check 2: Insane hourly rate (>$150/hr)
    if hourly_rate and hourly_rate > 150:
        flags.append('HIGH_HOURLY')
        is_valid = False
    
    # Check 3: Suspiciously cheap (<$3 fare)
    if fare and fare < 3.00:
        flags.append('LOW_FARE')
        is_valid = False
    
    # Check 4: Zero distance with high fare
    if trip_miles and fare and trip_miles < 0.5 and fare > 10:
        flags.append('SUSPICIOUS_DISTANCE')
        is_valid = False
    
    # Check 5: Missing critical data
    if not trip_minutes or trip_minutes == 0:
        flags.append('MISSING_TIME')
        is_valid = False
    
    if not trip_miles or trip_miles == 0:
        flags.append('MISSING_DISTANCE')
        is_valid = False
    
    return is_valid, flags

@require_firebase_auth
@decisions_bp.route("/harvest", methods=["POST"])
def harvest_offer():
    """
    Fire-and-forget endpoint to store anonymous offer data for crowdsourced pricing database.
    Called by Android AFTER decision is made and announced.

    Privacy: NO driver_id, NO lat/lng coordinates, NO addresses stored.
    Only anonymized market signals: H3 hexes, pricing, timing, platform.
    """
    try:
        uid = verify_and_get_user_id(request)
    except:
        return jsonify({"status": "auth_failed"}), 403

    p = request.get_json(silent=True) or {}

    # Extract offer data
    fare = p.get("fare")
    trip_miles = p.get("tripMiles")
    trip_minutes = p.get("tripMinutes")
    pickup_miles = p.get("pickupMiles")
    pickup_minutes = p.get("pickupMinutes")

    # Locations — H3 only, no lat/lng stored in community table
    pickup_lat = p.get("pickupLat") or p.get("lat")
    pickup_lng = p.get("pickupLng") or p.get("lng")
    dropoff_lat = p.get("dropoffLat")
    dropoff_lng = p.get("dropoffLng")
    driver_lat = p.get("driverLat") or p.get("currentLat")
    driver_lng = p.get("driverLng") or p.get("currentLng")

    # Offer details
    ride_type = p.get("rideType") or p.get("vehicleType")
    is_surge = bool(p.get("isSurge", False))
    platform = p.get("platform") or "uber"
    market_id = p.get("marketId")

    # Calculated metrics
    effective_hourly_rate = p.get("hourlyRate")
    dollars_per_mile = p.get("dollarsPerMile")

    # Convert to proper types
    if fare is not None: fare = float(fare)
    if trip_miles is not None: trip_miles = float(trip_miles)
    if trip_minutes is not None: trip_minutes = int(float(trip_minutes))
    if pickup_miles is not None: pickup_miles = float(pickup_miles)
    if pickup_minutes is not None: pickup_minutes = int(float(pickup_minutes))
    if pickup_lat is not None: pickup_lat = float(pickup_lat)
    if pickup_lng is not None: pickup_lng = float(pickup_lng)
    if dropoff_lat is not None: dropoff_lat = float(dropoff_lat)
    if dropoff_lng is not None: dropoff_lng = float(dropoff_lng)
    if driver_lat is not None: driver_lat = float(driver_lat)
    if driver_lng is not None: driver_lng = float(driver_lng)
    if effective_hourly_rate is not None: effective_hourly_rate = float(effective_hourly_rate)
    if dollars_per_mile is not None: dollars_per_mile = float(dollars_per_mile)

    # GEOCODE HALLUCINATION GUARD
    if driver_lat and driver_lng and pickup_lat and pickup_lng:
        from math import radians, cos, sin, asin, sqrt
        def _hav(lat1, lng1, lat2, lng2):
            lat1, lng1, lat2, lng2 = map(radians, [lat1, lng1, lat2, lng2])
            dlat = lat2 - lat1
            dlng = lng2 - lng1
            a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlng/2)**2
            return 3956 * 2 * asin(sqrt(a))
        geo_dist = _hav(driver_lat, driver_lng, pickup_lat, pickup_lng)
        if geo_dist > 30:
            logging.warning(f"HARVEST GEOCODE HALLUCINATION: {geo_dist:.0f}mi. Nulling pickup coords.")
            pickup_lat = None
            pickup_lng = None

    # Compute time breakdowns in driver's local timezone
    from datetime import datetime
    from zoneinfo import ZoneInfo
    conn_tz = get_db()
    cur_tz = conn_tz.cursor()
    cur_tz.execute(
        "SELECT settings->>'timezone' AS timezone FROM app_private.driver_settings_new WHERE driver_id = %s",
        (uid,)
    )
    tz_row = cur_tz.fetchone()
    driver_tz_str = tz_row.get('timezone') if tz_row and tz_row.get('timezone') else 'America/Chicago'
    driver_tz = ZoneInfo(driver_tz_str)
    cur_tz.close()
    conn_tz.close()
    now = datetime.now(driver_tz)
    day_of_year = now.timetuple().tm_yday

    conn = get_db()
    cur = conn.cursor()

    try:
        cur.execute("SET LOCAL app.driver_tz = %s", (driver_tz_str,))

        # Write anonymous market signal to public community table
        # NO driver_id, NO lat/lng, NO addresses -- H3 hexes only
        cur.execute("""
            INSERT INTO public.community_offers (
                created_at, day_of_year, day_of_week, hour_of_day,
                market_id, platform,
                pickup_h3, dropoff_h3,
                fare, trip_miles, trip_minutes,
                pickup_miles, pickup_minutes,
                dollars_per_mile, effective_hourly_rate,
                is_surge, ride_type
            ) VALUES (
                NOW(),
                %s,
                EXTRACT(DOW FROM NOW() AT TIME ZONE 'America/Chicago')::smallint,
                EXTRACT(HOUR FROM NOW() AT TIME ZONE 'America/Chicago')::smallint,
                %s, %s,
                app_private.safe_h3(%s, %s),
                app_private.safe_h3(%s, %s),
                %s, %s, %s,
                %s, %s,
                %s, %s,
                %s, %s
            )
        """, (
            day_of_year,
            market_id, platform,
            pickup_lat, pickup_lng,
            dropoff_lat, dropoff_lng,
            fare, trip_miles, trip_minutes,
            pickup_miles, pickup_minutes,
            dollars_per_mile, effective_hourly_rate,
            is_surge, ride_type
        ))

        # [PHASE 1] Android coord cache writes removed — server geocode is sole cache writer

        conn.commit()

        # Referral offer count increment (per-driver, separate from community data)
        try:
            cur2 = conn.cursor()
            cur2.execute("""
                UPDATE referrals
                SET offer_count = offer_count + 1,
                    updated_at = NOW(),
                    status = CASE
                        WHEN offer_count + 1 >= 100 THEN 'PENDING_PAYMENT'::referral_status
                        ELSE status
                    END
                WHERE referee_id = %s AND status = 'PENDING_WORK'
            """, (uid,))
            conn.commit()
            cur2.close()
        except Exception as ref_err:
            logging.error(f"Referral count update failed (non-blocking): {ref_err}")

        logging.info(f"Community offer harvested — platform: {platform} market: {market_id}")

        return jsonify({"status": "harvested"}), 200

    except Exception as e:
        logging.error(f"Harvest failed: {e}")
        traceback.print_exc()
        conn.rollback()
        return jsonify({"status": "failed", "error": str(e)}), 200
    finally:
        cur.close()
        conn.close()
@require_firebase_auth
@decisions_bp.route("/optimize", methods=["GET"])
def get_optimization_recommendations():
    """
    Returns threshold optimization recommendations based on historical decision data.
    
    Query params:
        market_id (optional): UUID of specific market to analyze
        days (optional): Number of days to analyze (default: 7)
        mode (optional): PUDDLE_JUMP, TOWARDS, or FREESTYLE (default: all)
        time_period (optional): morning_commute, midday, evening_commute, evening, night_shift, weekend_party, sunday
    
    Returns JSON with optimization matrix showing acceptance rates and revenue
    at different threshold combinations.
    """
    try:
        uid = verify_and_get_user_id(request)
    except:
        return jsonify({"status": "auth_failed"}), 403
    
    try:
        market_id = request.args.get('market_id', None)
        if not market_id:
            return jsonify({"error": "market_id is required. Pass your market UUID as ?market_id=..."}), 400
        days_back = request.args.get('days', 7, type=int)
        mode_filter = request.args.get('mode', None)
        time_period = request.args.get('time_period', None)
        
        # Validate mode if provided
        if mode_filter and mode_filter not in ('PUDDLE_JUMP', 'TOWARDS', 'FREESTYLE'):
            return jsonify({"error": "Invalid mode. Must be PUDDLE_JUMP, TOWARDS, or FREESTYLE"}), 400
        
        # Validate time_period if provided
        valid_periods = ('morning_commute', 'midday', 'evening_commute', 'evening', 'night_shift', 'weekend_party', 'sunday')
        if time_period and time_period not in valid_periods:
            return jsonify({"error": f"Invalid time_period. Must be one of: {', '.join(valid_periods)}"}), 400
        
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        cur.execute("""
            SELECT 
                test_hourly as "testHourly",
                test_mileage as "testMileage",
                test_efficiency as "testEfficiency",
                would_accept as "wouldAccept",
                total_offers as "totalOffers",
                accept_pct as "acceptPct",
                total_revenue as "totalRevenue",
                avg_hourly as "avgHourly",
                avg_dpm as "avgDpm",
                total_progress as "totalProgress",
                avg_progress_per_ride as "avgProgressPerRide",
                mode_analyzed as "modeAnalyzed",
                market_analyzed as "marketAnalyzed",
                time_period_analyzed as "timePeriodAnalyzed"
            FROM app_private.optimize_market_settings(%s, %s, %s, %s, %s)
        """, (uid, market_id, days_back, mode_filter, time_period))
        
        rows = cur.fetchall()
        
        # Convert Decimal types to float for JSON serialization
        results = []
        for row in rows:
            clean_row = {}
            for key, value in row.items():
                if value is None:
                    clean_row[key] = None
                elif hasattr(value, 'is_finite'):  # Decimal type
                    clean_row[key] = float(value)
                else:
                    clean_row[key] = value
            results.append(clean_row)
        
        return jsonify({
            "status": "success",
            "driverId": uid,
            "marketId": market_id,
            "daysAnalyzed": days_back,
            "modeFilter": mode_filter,
            "timePeriod": time_period,
            "recommendations": results
        }), 200

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
    finally:
        if 'conn' in locals(): conn.close()

@require_firebase_auth
@decisions_bp.route("/optimize/pareto", methods=["GET"])
def get_pareto_frontier():
    """
    Returns Pareto-optimal threshold combinations - the meaningful tradeoffs
    between revenue and hourly rate.
    
    Query params:
        market_id (required): UUID of market to analyze
        days (optional): Number of days to analyze (default: 28)
        mode (optional): PUDDLE_JUMP, TOWARDS, or FREESTYLE (default: all)
        time_period (optional): morning_commute, midday, evening_commute, etc.
        min_accept_pct (optional): Minimum acceptance % to consider (default: 20)
        min_hourly_jump (optional): Minimum $/hr improvement between options (default: 1.0)
        target_accept_pct (optional): User's target acceptance % for highlighting
    
    Returns JSON with Pareto frontier options and recommendations.
    """
    try:
        uid = verify_and_get_user_id(request)
    except:
        return jsonify({"status": "auth_failed"}), 403
    
    try:
        market_id = request.args.get('market_id', None)
        if not market_id:
            return jsonify({"error": "market_id is required"}), 400
            
        days_back = request.args.get('days', 28, type=int)
        mode_filter = request.args.get('mode', None)
        time_period = request.args.get('time_period', None)
        min_accept_pct = request.args.get('min_accept_pct', 20.0, type=float)
        min_hourly_jump = request.args.get('min_hourly_jump', 1.0, type=float)
        target_accept_pct = request.args.get('target_accept_pct', None, type=float)
        
        # Validate mode if provided
        if mode_filter and mode_filter not in ('PUDDLE_JUMP', 'TOWARDS', 'FREESTYLE'):
            return jsonify({"error": "Invalid mode. Must be PUDDLE_JUMP, TOWARDS, or FREESTYLE"}), 400
        
        # Validate time_period if provided
        valid_periods = ('morning_commute', 'midday', 'evening_commute', 'evening', 'night_shift', 'weekend_party', 'sunday')
        if time_period and time_period not in valid_periods:
            return jsonify({"error": f"Invalid time_period. Must be one of: {', '.join(valid_periods)}"}), 400
        
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        cur.execute("""
            SELECT 
                test_hourly,
                test_mileage,
                would_accept,
                total_offers,
                accept_pct,
                avg_hourly,
                avg_dpm,
                total_revenue,
                is_max_revenue,
                is_max_hourly,
                is_ai_pick
            FROM app_private.get_pareto_frontier(%s, %s, %s, %s, %s, %s, %s)
        """, (uid, market_id, days_back, mode_filter, time_period, min_accept_pct, min_hourly_jump))
        
        rows = cur.fetchall()
        
        # Convert to clean JSON and find key options
        options = []
        max_revenue_option = None
        max_hourly_option = None
        ai_pick_option = None
        closest_to_target = None
        closest_distance = float('inf')
        
        for row in rows:
            option = {
                "testHourly": float(row['test_hourly']) if row['test_hourly'] else None,
                "testMileage": float(row['test_mileage']) if row['test_mileage'] else None,
                "wouldAccept": int(row['would_accept']) if row['would_accept'] else 0,
                "totalOffers": int(row['total_offers']) if row['total_offers'] else 0,
                "acceptPct": float(row['accept_pct']) if row['accept_pct'] else 0,
                "avgHourly": float(row['avg_hourly']) if row['avg_hourly'] else 0,
                "avgDpm": float(row['avg_dpm']) if row['avg_dpm'] else 0,
                "totalRevenue": float(row['total_revenue']) if row['total_revenue'] else 0,
                "isMaxRevenue": row['is_max_revenue'],
                "isMaxHourly": row['is_max_hourly'],
                "isAiPick": row.get('is_ai_pick', False)
            }
            options.append(option)
            
            # Track key options
            if row['is_max_revenue']:
                max_revenue_option = option
            if row['is_max_hourly']:
                max_hourly_option = option
            if row.get('is_ai_pick'):
                ai_pick_option = option
            
            # Find closest to user's target acceptance %
            if target_accept_pct and row['accept_pct']:
                distance = abs(float(row['accept_pct']) - target_accept_pct)
                if distance < closest_distance:
                    closest_distance = distance
                    closest_to_target = option
        
        # Build recommendation summary
        recommendation = None
        if max_revenue_option and closest_to_target and max_revenue_option != closest_to_target:
            revenue_diff = max_revenue_option['totalRevenue'] - closest_to_target['totalRevenue']
            hourly_diff = closest_to_target['avgHourly'] - max_revenue_option['avgHourly']
            
            if revenue_diff > 0:
                recommendation = {
                    "message": f"Over the last {days_back} days, you'd have earned ~${revenue_diff:.0f} more total by relaxing to {max_revenue_option['acceptPct']:.0f}% acceptance",
                    "revenueDiff": round(revenue_diff, 2),
                    "hourlyDiff": round(hourly_diff, 2),
                    "suggestedOption": max_revenue_option
                }
        
        return jsonify({
            "status": "success",
            "driverId": uid,
            "marketId": market_id,
            "daysAnalyzed": days_back,
            "modeFilter": mode_filter,
            "timePeriod": time_period,
            "targetAcceptPct": target_accept_pct,
            "options": options,
            "maxRevenueOption": max_revenue_option,
            "maxHourlyOption": max_hourly_option,
            "aiPickOption": ai_pick_option,
            "closestToTarget": closest_to_target,
            "recommendation": recommendation
        }), 200

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
    finally:
        if 'conn' in locals(): conn.close()
# ======================================================================
# GET /api/v1/decisions/market-rate
# Returns current market rate for a given lat/lng (and optional dow/hour)
# ======================================================================
@require_firebase_auth
@decisions_bp.route("/market-rate", methods=["GET"])
def get_market_rate():
    try:
        driver_id = verify_and_get_user_id(request)
        lat  = request.args.get("lat",  type=float)
        lng  = request.args.get("lng",  type=float)
        dow  = request.args.get("dow",  type=int)   # 0=Sun..6=Sat, optional
        hour = request.args.get("hour", type=int)   # 0-23, optional

        if lat is None or lng is None:
            return jsonify({"error": "lat and lng are required"}), 400

        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # If dow/hour provided, override the driver's current local time
        if dow is not None and hour is not None:
            cur.execute("""
                SELECT market_hourly, market_mileage, sample_count, data_source
                FROM app_private.get_market_rate(%s, %s, %s,
                    (date_trunc('week', NOW() AT TIME ZONE 'America/Chicago')
                     + (%s || ' days')::interval
                     + (%s || ' hours')::interval) AT TIME ZONE 'America/Chicago')
            """, (lat, lng, driver_id, dow, hour))
        else:
            cur.execute("""
                SELECT market_hourly, market_mileage, sample_count, data_source
                FROM app_private.get_market_rate(%s, %s, %s)
            """, (lat, lng, driver_id))

        r = cur.fetchone()

        if not r or r['data_source'] == 'market:no_data':
            return jsonify({
                "market_hourly":  None,
                "market_mileage": None,
                "sample_count":   0,
                "data_source":    "no_data",
                "message":        "No market data available for this location/time"
            }), 200

        return jsonify({
            "market_hourly":  float(r['market_hourly'])  if r['market_hourly']  else None,
            "market_mileage": float(r['market_mileage']) if r['market_mileage'] else None,
            "sample_count":   r['sample_count'],
            "data_source":    r['data_source'],
        }), 200

    except Exception as e:
        logging.exception("market-rate endpoint error")
        return jsonify({"error": str(e)}), 500
    finally:
        if 'conn' in locals(): conn.close()
