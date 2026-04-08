# backend/dropoff_confirm.py
# Nail It — Dropoff confirmation endpoint
# Handles: dropoff_h3 lookup, IN_TRIP→UNCOMMITTED transition, PMS elevation
# Shared targeting logic: nail_it_core.py

import logging
from flask import Blueprint, request, jsonify
from psycopg2.extras import RealDictCursor

from db import get_db
from utils import verify_and_get_user_id, require_firebase_auth
from state_machine import DriverStateMachine
from nail_it_core import compute_error, classify, should_refine, build_voice, get_accuracy_stats, write_nailed_position

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

        # ── Get active trip state ─────────────────────────────────────
        cur.execute("""
            SELECT dropoff_h3, dropoff_lat, dropoff_lng,
                   state, current_offer_id
            FROM app_private.driver_trip_state
            WHERE driver_id = %s
        """, (driver_id,))
        row = cur.fetchone()
        if not row:
            return jsonify({"status": "no_match", "message": "No active trip state"}), 404

        triangulated_h3 = row['dropoff_h3']
        offer_id        = row['current_offer_id']
        current_state   = row['state']

        if not triangulated_h3:
            logging.info("⚠️ No triangulated dropoff — accepting with actual GPS")

        # ── Check existing dropoff confirmation (incremental bullseye) ─
        existing_error = None
        already_nailed = False
        if offer_id:
            cur.execute("""
                SELECT dropoff_error_m, actual_dropoff_lat
                FROM app_private.offer_history
                WHERE decision_log_id = %s::integer
            """, (offer_id,))
            oh_row = cur.fetchone()
            if oh_row and oh_row['actual_dropoff_lat'] is not None:
                existing_error = float(oh_row['dropoff_error_m']) if oh_row['dropoff_error_m'] else None
                already_nailed = True

        # ── Compute error and check refinement ────────────────────────
        actual_h3, error_m = compute_error(cur, actual_lat, actual_lng, triangulated_h3)
        if not triangulated_h3:
            triangulated_h3 = actual_h3
        classification = classify(error_m)
        update_ok, improvement = should_refine(existing_error, error_m)

        if not update_ok:
            logging.info(
                f"📍 Dropoff refinement skipped — "
                f"improvement={improvement:.0f}m < 50m threshold"
            )
            conn.commit()
            return jsonify({
                "status":         "confirmed",
                "offerId":        offer_id,
                "errorMeters":    error_m,
                "classification": classification,
                "refined":        False,
                "driverState":    "UNCOMMITTED",
                "voice":          f"Already confirmed. {int(error_m)} meters." if error_m else "Dropoff confirmed."
            }), 200

        if already_nailed and improvement:
            logging.info(f"🎯 Dropoff REFINED: {existing_error:.0f}m → {error_m:.0f}m (+{improvement:.0f}m)")

        # ── State transition ──────────────────────────────────────────
        if current_state in ("IN_TRIP", "STACKED"):
            # ── Buffer swap logic (was confirm_dropoff_arrival in trip_state) ──
            _state_row    = DriverStateMachine.read(driver_id, cur)
            _current_state = _state_row["state"] if _state_row else "IN_TRIP"

            if _current_state == "STACKED":
                # Completing first ride — pivot to secondary dropoff
                _sec_dlat = _sec_dlng = _sec_dh3 = None
                if _state_row and _state_row.get("current_offer_id"):
                    cur.execute("""
                        SELECT dropoff_lat, dropoff_lng, dropoff_h3
                        FROM app_private.offer_history
                        WHERE decision_log_id = (
                            SELECT id FROM app_private.decision_log
                            WHERE driver_id = %s
                            ORDER BY created_at DESC LIMIT 1
                        ) LIMIT 1
                    """, (driver_id,))
                    _sec = cur.fetchone()
                    if _sec:
                        _sec_dlat = _sec["dropoff_lat"]
                        _sec_dlng = _sec["dropoff_lng"]
                        _sec_dh3  = _sec["dropoff_h3"]
                        logging.info(
                            f"🔄 Buffer swap: secondary dropoff "
                            f"({_sec_dlat:.4f},{_sec_dlng:.4f})"
                        )
                DriverStateMachine.transition(driver_id, 'dropoff_confirmed', cur, conn,
                    nailed_dropoff_lat=actual_lat,
                    nailed_dropoff_lng=actual_lng,
                    dropoff_lat=_sec_dlat,
                    dropoff_lng=_sec_dlng,
                    dropoff_h3=_sec_dh3,
                )
            else:
                # Solo ride completed — full reset
                DriverStateMachine.transition(driver_id, 'dropoff_confirmed', cur, conn,
                    nailed_dropoff_lat=actual_lat,
                    nailed_dropoff_lng=actual_lng,
                    clear_coords=True,
                )
        else:
            logging.info(f"📍 Dropoff Nail It: state={current_state} — coord refinement only")

        # ── Write nailed dropoff position ────────────────────────────
        try:
            write_nailed_position(cur, driver_id, "dropoff", actual_lat, actual_lng, error_m)
        except Exception as nail_e:
            logging.warning(f"⚠️ nailed_dropoff write failed: {nail_e}")

        # ── Elevate pickup_market_signals to nail_it ──────────────────
        try:
            if offer_id:
                cur.execute("""
                    UPDATE app_private.pickup_market_signals
                    SET data_source  = 'nail_it',
                        offer_status = 'completed'
                    WHERE offer_id = %s
                """, (offer_id,))
        except Exception as e:
            logging.warning(f"⚠️ PMS nail_it elevation failed: {e}")

        # ── Update offer_history ──────────────────────────────────────
        try:
            if offer_id:
                cur.execute("""
                    UPDATE app_private.offer_history
                    SET actual_dropoff_lat     = %s,
                        actual_dropoff_lng     = %s,
                        actual_dropoff_h3      = %s,
                        actual_dropoff_at      = NOW(),
                        dropoff_error_m        = %s,
                        dropoff_classification = %s
                    WHERE decision_log_id = %s::integer
                """, (actual_lat, actual_lng, actual_h3,
                      error_m, classification, offer_id))
        except Exception as oh_err:
            logging.warning(f"⚠️ Offer history dropoff update failed: {oh_err}")

        # ── Stats + response ──────────────────────────────────────────
        stats = get_accuracy_stats(cur, driver_id, "dropoff")
        conn.commit()

        voice = build_voice(stats['totalConfirmed'], error_m, "dropoff")
        logging.info(f"Dropoff confirmed: offer={offer_id} error={error_m}m class={classification}")

        return jsonify({
            "status":         "confirmed",
            "offerId":        offer_id,
            "errorMeters":    error_m,
            "classification": classification,
            "refined":        already_nailed,
            "triangulatedH3": triangulated_h3,
            "actualH3":       actual_h3,
            "driverState":    "UNCOMMITTED",
            "stats":          stats,
            "voice":          voice
        }), 200

    except Exception as e:
        logging.exception("dropoff confirm error")
        if 'conn' in locals(): conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        if 'conn' in locals(): conn.close()
