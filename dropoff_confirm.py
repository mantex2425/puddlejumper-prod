# backend/dropoff_confirm.py
# Manual dropoff nail endpoint (dev-build only). Routes through the
# unified _execute_action in driver_heartbeat — same code path as
# auto-detection, just with cluster=None and explicit fallback coords.
#
# 2026-05-04 Commit 3b: rewritten from scratch. Old version performed
# direct DB writes plus state-machine transitions and contest hooks;
# that surface area is now owned entirely by _execute_action.
# outcome=None: manual dropoffs are normal completions (Case C),
# never implicit-cancels (Case D) and never pickup_missed (Case F).

import logging
from flask import Blueprint, request, jsonify
from psycopg2.extras import RealDictCursor

from db import get_db
from utils import verify_and_get_user_id, require_firebase_auth
from nail_it_core import build_voice, get_accuracy_stats

dropoff_confirm_bp = Blueprint('dropoff_confirm', __name__)


@require_firebase_auth
@dropoff_confirm_bp.route("/dropoff/confirm", methods=["POST"])
def confirm_dropoff():
    try:
        driver_id = verify_and_get_user_id(request)
        body = request.get_json()
        actual_lat = body.get("lat")
        actual_lng = body.get("lng")
        if actual_lat is None or actual_lng is None:
            return jsonify({"error": "lat and lng are required"}), 400

        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        cur.execute("""
            SELECT current_offer_id
            FROM app_private.driver_trip_state
            WHERE driver_id = %s
        """, (driver_id,))
        row = cur.fetchone()
        if not row or not row.get("current_offer_id"):
            return jsonify({
                "status": "no_active_ride",
                "message": "Manual dropoff nail requires an active offer",
            }), 404

        offer_id = row["current_offer_id"]

        from driver_heartbeat import _execute_action
        from dispatch import FireDropoff
        action = FireDropoff(offer_id=str(offer_id), outcome=None)
        executed, err = _execute_action(
            action, cur, conn, driver_id,
            cluster=None,
            fallback_lat=actual_lat, fallback_lng=actual_lng,
        )
        conn.commit()

        if not executed:
            return jsonify({"status": "error", "message": err or "execute failed"}), 500

        stats = get_accuracy_stats(cur, driver_id, "dropoff")
        voice = build_voice(stats["totalConfirmed"], 0, "dropoff")

        return jsonify({
            "status": "confirmed",
            "offerId": offer_id,
            "errorMeters": 0,
            "classification": "manual",
            "driverState": "UNCOMMITTED",
            "stats": stats,
            "voice": voice,
        }), 200

    except Exception as e:
        logging.exception("dropoff confirm error")
        if "conn" in locals():
            conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        if "conn" in locals():
            conn.close()