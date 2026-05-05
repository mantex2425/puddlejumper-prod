# backend/pickup_confirm.py
# Manual nail endpoint (dev-build only). Routes through the unified
# _execute_action in driver_heartbeat — same code path as auto-detection,
# just with cluster=None and explicit fallback coords.
#
# 2026-05-04 Commit 3b: rewritten from scratch. Old version performed
# direct DB writes to driver_trip_state, pickup_market_signals,
# offer_history, and community_offers; that surface area is now owned
# entirely by _execute_action so heartbeat-driven and manual paths
# share the same code path.

import logging
from flask import Blueprint, request, jsonify
from psycopg2.extras import RealDictCursor

from db import get_db
from utils import verify_and_get_user_id, require_firebase_auth
from nail_it_core import build_voice, get_accuracy_stats

pickup_confirm_bp = Blueprint('pickup_confirm', __name__)


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

        # Resolve bound_offer_id — manual nail requires an active ride.
        #
        # bound_offer_id is a HINT post-Sub-commit 1c (see driver_queue.py).
        # This site trusts the hint: if the pointer is stale (L-19 class),
        # the downstream FirePickup execution will fail loudly when it
        # tries to load the offer's coords from offer_history. That's the
        # desired behavior for manual nails — better to fail loudly here
        # than to fire a pickup against an aged-out offer.
        from driver_queue import DriverQueue
        queue = DriverQueue(driver_id)
        offer_id = queue.bound_offer_id(cur)
        if not offer_id:
            return jsonify({
                "status": "no_active_ride",
                "message": "Manual pickup nail requires an active offer",
            }), 404

        # Build action and route through unified executor
        from driver_heartbeat import _execute_action
        from dispatch import FirePickup
        action = FirePickup(offer_id=str(offer_id))
        executed, err = _execute_action(
            action, cur, conn, driver_id, queue,
            cluster=None,
            fallback_lat=actual_lat, fallback_lng=actual_lng,
        )
        conn.commit()

        if not executed:
            return jsonify({"status": "error", "message": err or "execute failed"}), 500

        stats = get_accuracy_stats(cur, driver_id, "pickup")
        voice = build_voice(stats["totalConfirmed"], 0, "pickup")

        return jsonify({
            "status": "confirmed",
            "offerId": offer_id,
            "errorMeters": 0,
            "classification": "manual",
            "driverState": "IN_TRIP",
            "stats": stats,
            "voice": voice,
        }), 200

    except Exception as e:
        logging.exception("pickup confirm error")
        if "conn" in locals():
            conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        if "conn" in locals():
            conn.close()