# backend/pickup_confirm.py
# Ground Truth Validation — "Nail It" button endpoint
# Records actual pickup location against triangulated estimate

import logging
from flask import Blueprint, request, jsonify
from psycopg2.extras import RealDictCursor

from db import get_db
from utils import verify_and_get_user_id, require_firebase_auth

pickup_confirm_bp = Blueprint('pickup_confirm', __name__)

# ======================================================================
# POST /api/v1/pickup/confirm
# Called when driver taps "Nail It" at actual pickup location.
# Finds most recent accepted offer for this driver, records actual
# pickup coordinates, calculates triangulation error in meters.
# ======================================================================
@require_firebase_auth
@pickup_confirm_bp.route("/pickup/confirm", methods=["POST"])
def confirm_pickup():
    try:
        driver_id = verify_and_get_user_id(request)
        body = request.get_json()

        actual_lat = body.get("lat")
        actual_lng = body.get("lng")

        if actual_lat is None or actual_lng is None:
            return jsonify({"error": "lat and lng are required"}), 400

        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # Find the most recent accepted offer in pickup_market_signals
        # for this driver that hasn't been confirmed yet
        cur.execute("""
            SELECT 
                pms.id AS pms_id,
                pms.offer_id,
                pms.pickup_h3,
                dl.created_at
            FROM app_private.pickup_market_signals pms
            JOIN app_private.decision_log dl ON dl.id = pms.offer_id
            WHERE dl.driver_id = %s
              AND pms.is_accepted = true
              AND pms.actual_pickup_at IS NULL
            ORDER BY dl.created_at DESC
            LIMIT 1
        """, (driver_id,))

        row = cur.fetchone()
        if not row:
            return jsonify({
                "status": "no_match",
                "message": "No unconfirmed accepted offer found"
            }), 404

        pms_id = row['pms_id']
        triangulated_h3 = row['pickup_h3']

        # Compute actual H3 and error distance in one query
        cur.execute("""
            WITH actual AS (
                SELECT 
                    h3_latlng_to_cell(point(%s, %s), 8) AS actual_h3,
                    ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography AS actual_point
            ),
            triangulated AS (
                SELECT 
                    ST_SetSRID(
                        ST_MakePoint(
                            ST_X(h3_cell_to_latlng(%s::h3index)::geometry),
                            ST_Y(h3_cell_to_latlng(%s::h3index)::geometry)
                        ), 4326
                    )::geography AS tri_point
            )
            SELECT 
                actual.actual_h3::text,
                round(ST_Distance(actual.actual_point, triangulated.tri_point)::numeric, 1) AS error_m
            FROM actual, triangulated
        """, (actual_lat, actual_lng, actual_lng, actual_lat,
              triangulated_h3, triangulated_h3))

        geo_row = cur.fetchone()
        actual_h3 = geo_row['actual_h3']
        error_m = float(geo_row['error_m'])

        # Classify result
        if error_m <= 400:
            classification = "bullseye"
        elif error_m <= 800:
            classification = "on_target"
        else:
            classification = "miss"

        # Update the shadow table row
        cur.execute("""
            UPDATE app_private.pickup_market_signals
            SET 
                actual_pickup_lat     = %s,
                actual_pickup_lng     = %s,
                actual_pickup_h3      = %s,
                actual_pickup_at      = NOW(),
                triangulation_error_m = %s
            WHERE id = %s
        """, (actual_lat, actual_lng, actual_h3, error_m, pms_id))

        # Calculate running accuracy stats for this driver
        cur.execute("""
            SELECT
                COUNT(*) AS total_confirmed,
                round(100.0 * COUNT(*) FILTER (WHERE triangulation_error_m <= 800)
                    / NULLIF(COUNT(*), 0), 1) AS pct_on_target,
                round(AVG(triangulation_error_m)::numeric, 0) AS avg_error_m
            FROM app_private.pickup_market_signals pms
            JOIN app_private.decision_log dl ON dl.id = pms.offer_id
            WHERE dl.driver_id = %s
              AND pms.actual_pickup_at IS NOT NULL
        """, (driver_id,))

        stats = cur.fetchone()
        conn.commit()

        total = int(stats['total_confirmed'])
        pct = float(stats['pct_on_target']) if stats['pct_on_target'] else 0.0
        avg_err = float(stats['avg_error_m']) if stats['avg_error_m'] else 0.0

        # Voice string for Android TTS
        if total == 1:
            voice = f"First confirmation. {int(error_m)} meters."
        else:
            voice = f"{total} confirmed. {pct:.0f}% on target. Average {int(avg_err)} meters."

        logging.info(f"Pickup confirmed: offer={row['offer_id']} "
                     f"error={error_m}m class={classification} "
                     f"total={total} pct={pct}%")

        return jsonify({
            "status": "confirmed",
            "offerId": row['offer_id'],
            "errorMeters": error_m,
            "classification": classification,
            "triangulatedH3": triangulated_h3,
            "actualH3": actual_h3,
            "stats": {
                "totalConfirmed": total,
                "pctOnTarget": pct,
                "avgErrorMeters": avg_err
            },
            "voice": voice
        }), 200

    except Exception as e:
        logging.exception("pickup confirm error")
        if 'conn' in locals():
            conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        if 'conn' in locals():
            conn.close()