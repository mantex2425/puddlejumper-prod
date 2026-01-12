# backend/routes/decisions.py
# VERSION: 5.8 - Added Decision History Retrieval & Fixed Schema Persistence
import os
import random
import json
import traceback
from dotenv import load_dotenv
from flask import Blueprint, request, jsonify
from psycopg2.extras import RealDictCursor
from db import get_db
from utils import verify_and_get_user_id, require_firebase_auth

load_dotenv()
decisions_bp = Blueprint('decisions', __name__)

# ======================================================================
# HELPER: Get Coordinates from Hex via SQL
# ======================================================================
def get_coords_from_hex(cur, hex_code):
    """
    Verified fix based on SQL Ground Truth.
    pt[0] = Longitude, pt[1] = Latitude.
    """
    cur.execute("""
        SELECT 
            (h3_cell_to_latlng(%s::h3index))[0] as lng, 
            (h3_cell_to_latlng(%s::h3index))[1] as lat
    """, (hex_code, hex_code))
    row = cur.fetchone()
    if not row:
        return None
    return {"lat": float(row['lat']), "lng": float(row['lng'])}

# ======================================================================
# GET: /api/v1/decisions/history
# ======================================================================
@require_firebase_auth
@decisions_bp.route("/history", methods=["GET"])
def get_decision_history():
    """
    Retrieves the last N decisions for the authenticated driver.
    Matches the schema verified in the app_private.decision_log table.
    """
    try:
        uid = verify_and_get_user_id(request)
        limit = request.args.get('limit', 10, type=int)

        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # Query matches verified table columns
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
        
        # Ensure decisionResult is parsed if stored as a string
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


# ======================================================================
# POST: /api/v1/decisions/simulate-suite
# ======================================================================
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

@require_firebase_auth
@decisions_bp.route("/", methods=["POST"], strict_slashes=False)
def make_decision():
    try:
        uid = verify_and_get_user_id(request)
    except:
        return jsonify({"error": "Auth failed"}), 403

    p = request.get_json(silent=True) or {}

    fare = float(p.get("fare") or 0)
    trip_miles = float(p.get("tripMiles") or 0)
    trip_min = float(p.get("tripMinutes") or 0)
    pickup_min = float(p.get("pickupMinutes") or 0)

    d_lat = float(p.get("dropoffLat") or 0)
    d_lng = float(p.get("dropoffLng") or 0)
    p_lat = float(p.get("lat") or 0)
    p_lng = float(p.get("lng") or 0)

    # Use the validated Houston Market UUID if none provided
    market_id = p.get("marketId") or "6a35d28b-8e6c-4d60-94aa-2661e2650863"

    pickup_miles = float(p.get("pickupMiles") or (pickup_min * 0.33))

    # Towards Mode Parameters (from Android voice command)
    towards_active = bool(p.get("towardsActive", False))
    towards_target_lat = p.get("towardsTargetLat")
    towards_target_lng = p.get("towardsTargetLng")
    towards_market_id = p.get("towardsMarketId")

    # Convert to float or None for SQL
    if towards_target_lat is not None:
        towards_target_lat = float(towards_target_lat)
    if towards_target_lng is not None:
        towards_target_lng = float(towards_target_lng)

    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    try:
        # 1. Run the Decision Engine SQL function (now with Towards Mode)
        cur.execute("""
            SELECT * FROM app_private.decision_engine_v2(
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s
            )
        """, (
            uid, p_lat, p_lng, d_lat, d_lng, fare, trip_miles, trip_min,
            pickup_min, pickup_miles, market_id,
            towards_active, towards_target_lat, towards_target_lng, towards_market_id
        ))

        row = cur.fetchone()
        if not row: 
            return jsonify({"error": "Engine returned no result"}), 500

        # Output format remains camelCase for the Android DTOs
        result = {
            "verdict": row['verdict'],
            "reason": row['reason'],
            "netPay": float(row['net_pay']),
            "hourlyRate": float(row['hourly_rate']),
            "dollarsPerMile": float(row['dollars_per_mile']),
            "deadheadMiles": float(row['deadhead_miles']),
            "deadheadCost": float(row['deadhead_cost']),
            "auditSource": "SQL_V2"
        }

        # 2. Persist the record to the decision_log table
        try:
            cur.execute("""
                INSERT INTO app_private.decision_log (
                    driver_id, market_id, fare, pickup_minutes, trip_minutes,
                    pickup_lat, pickup_lng, dropoff_lat, dropoff_lng,
                    decision_result, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
            """, (
                uid, market_id, fare, pickup_min, trip_min, 
                p_lat, p_lng, d_lat, d_lng, json.dumps(result)
            ))
            conn.commit()
        except Exception as db_e:
            print(f"Logging failed: {db_e}")
            conn.rollback()

        return jsonify(result), 200

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()
        conn.close()