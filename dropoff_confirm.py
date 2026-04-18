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
from nail_it_core import compute_error, classify, should_refine, build_voice, get_accuracy_stats, write_nailed_position, write_nail_contest_event, get_next_stacked_offer

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

        # ── Audit lock: for STACKED rides, find the primary offer ─────
        # current_offer_id points to the secondary (stacked) offer.
        # The primary is the one with a confirmed pickup but no dropoff yet.
        _audit_offer_id = offer_id
        if current_state == 'STACKED':
            try:
                cur.execute("""
                    SELECT oh.decision_log_id
                    FROM app_private.offer_history oh
                    JOIN app_private.decision_log dl ON dl.id = oh.decision_log_id
                    WHERE dl.driver_id = %s
                      AND oh.actual_pickup_at IS NOT NULL
                      AND oh.actual_dropoff_at IS NULL
                    ORDER BY oh.actual_pickup_at DESC
                    LIMIT 1
                """, (driver_id,))
                _primary = cur.fetchone()
                if _primary:
                    _audit_offer_id = _primary['decision_log_id']
                    logging.info(f"[DROPOFF_CONFIRM] Audit lock: primary offer={_audit_offer_id} (state had {offer_id})")
            except Exception as _al_err:
                logging.warning(f"[DROPOFF_CONFIRM] Audit lock failed, falling back to {offer_id}: {_al_err}")

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
        # For STACKED rides, use the primary offer's dropoff_h3 for error calculation
        # driver_trip_state.dropoff_h3 has already been swapped to secondary coords
        _triangulated_h3 = triangulated_h3
        if current_state == 'STACKED' and _audit_offer_id:
            try:
                cur.execute(
                    "SELECT dropoff_h3 FROM app_private.offer_history "
                    "WHERE decision_log_id = %s::integer",
                    (_audit_offer_id,))
                _oh_pin = cur.fetchone()
                if _oh_pin and _oh_pin['dropoff_h3']:
                    _triangulated_h3 = _oh_pin['dropoff_h3']
                    logging.info(f"[DROPOFF_CONFIRM] Using primary pin h3 for error calc: offer={_audit_offer_id}")
            except Exception as _ph_err:
                logging.warning(f"[DROPOFF_CONFIRM] Primary pin fetch failed: {_ph_err}")
        actual_h3, error_m = compute_error(cur, actual_lat, actual_lng, _triangulated_h3)
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
        if current_state in ("IN_TRIP", "STACKED", "REFINE_DROPOFF"):
            # ── Buffer swap logic (was confirm_dropoff_arrival in trip_state) ──
            _state_row    = DriverStateMachine.read(driver_id, cur)
            _current_state = _state_row["state"] if _state_row else "IN_TRIP"

            if _current_state == "STACKED":
                # Completing first ride — pivot to secondary dropoff
                _sec_dlat = _sec_dlng = _sec_dh3 = None
                _sec = get_next_stacked_offer(cur, driver_id)
                if _sec:
                    _sec_offer_id = str(_sec["decision_log_id"])
                    _sec_dlat = _sec["dropoff_lat"]
                    _sec_dlng = _sec["dropoff_lng"]
                    _sec_dh3  = _sec["dropoff_h3"]
                    logging.info(f"🔄 Buffer swap: secondary offer={_sec_offer_id} dropoff ({_sec_dlat:.4f},{_sec_dlng:.4f})")
                else:
                    _sec_offer_id = None
                    logging.warning("🔄 Buffer swap: no secondary offer found")
                DriverStateMachine.transition(driver_id, 'dropoff_confirmed', cur, conn,
                    offer_id=_sec_offer_id,
                    nailed_dropoff_lat=actual_lat,
                    nailed_dropoff_lng=actual_lng,
                    dropoff_lat=_sec_dlat,
                    dropoff_lng=_sec_dlng,
                    dropoff_h3=_sec_dh3,
                )
                # Patch 00562: contest event for manual STACKED dropoff (primary ride end)
                try:
                    write_nail_contest_event(
                        cur, conn,
                        driver_id=driver_id,
                        state_row=_state_row or {},
                        nail_path="manual_dropoff_stacked",
                        current_lat=actual_lat,
                        current_lng=actual_lng,
                        current_speed_mph=0.0,
                        current_heading=0.0,
                        stopped_seconds=0.0,
                        dist_to_target_m=float(error_m or 0),
                        target_lat=(_state_row or {}).get('dropoff_lat'),
                        target_lng=(_state_row or {}).get('dropoff_lng'),
                        pickup_lat=(_state_row or {}).get('pickup_lat'),
                        pickup_lng=(_state_row or {}).get('pickup_lng'),
                        dropoff_lat=(_state_row or {}).get('dropoff_lat'),
                        dropoff_lng=(_state_row or {}).get('dropoff_lng'),
                    )
                except Exception as _ce:
                    logging.warning(f"[CONTEST] manual_dropoff_stacked hook non-fatal: {_ce}")
            else:
                # Solo ride completed — enforce linear path via REFINE_DROPOFF
                if _current_state == 'IN_TRIP':
                    DriverStateMachine.transition(driver_id, 'approaching_dropoff', cur, conn)
                    logging.warning(f"[DROPOFF_CONFIRM] IN_TRIP→REFINE_DROPOFF (arm+nail same call)")
                DriverStateMachine.transition(driver_id, 'dropoff_confirmed', cur, conn,
                    nailed_dropoff_lat=actual_lat,
                    nailed_dropoff_lng=actual_lng,
                    clear_coords=True,
                )
                # Patch 00562: contest event for manual solo dropoff
                try:
                    write_nail_contest_event(
                        cur, conn,
                        driver_id=driver_id,
                        state_row=_state_row or {},
                        nail_path="manual_dropoff",
                        current_lat=actual_lat,
                        current_lng=actual_lng,
                        current_speed_mph=0.0,
                        current_heading=0.0,
                        stopped_seconds=0.0,
                        dist_to_target_m=float(error_m or 0),
                        target_lat=(_state_row or {}).get('dropoff_lat'),
                        target_lng=(_state_row or {}).get('dropoff_lng'),
                        pickup_lat=(_state_row or {}).get('pickup_lat'),
                        pickup_lng=(_state_row or {}).get('pickup_lng'),
                        dropoff_lat=(_state_row or {}).get('dropoff_lat'),
                        dropoff_lng=(_state_row or {}).get('dropoff_lng'),
                    )
                except Exception as _ce:
                    logging.warning(f"[CONTEST] manual_dropoff hook non-fatal: {_ce}")
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

        # ── Community radar feed from PMS elevation (fire-and-forget) ──
        try:
            if offer_id:
                cur.execute("""
                    INSERT INTO public.community_offers (
                        created_at, day_of_year, day_of_week, hour_of_day,
                        platform, metroplex_id,
                        pickup_h3, dropoff_h3,
                        actual_pickup_lat, actual_pickup_lng, actual_pickup_h3,
                        fare, trip_miles,
                        dollars_per_mile, effective_hourly_rate,
                        data_source, geog
                    )
                    SELECT
                        NOW(),
                        EXTRACT(DOY  FROM NOW() AT TIME ZONE 'America/Chicago')::smallint,
                        EXTRACT(DOW  FROM NOW() AT TIME ZONE 'America/Chicago')::smallint,
                        EXTRACT(HOUR FROM NOW() AT TIME ZONE 'America/Chicago')::smallint,
                        'uber', 1,
                        pms.pickup_h3, dl.dropoff_h3_index,
                        pms.actual_pickup_lat, pms.actual_pickup_lng,
                        app_private.coords_to_h3(pms.actual_pickup_lat, pms.actual_pickup_lng),
                        dl.fare, dl.trip_miles,
                        pms.dollars_per_mile, pms.hourly_rate_offered,
                        'nail_it',
                        app_private.coords_to_geography(pms.actual_pickup_lat, pms.actual_pickup_lng)
                    FROM app_private.pickup_market_signals pms
                    JOIN app_private.decision_log dl ON dl.id = pms.offer_id
                    WHERE pms.offer_id = %s
                      AND pms.data_source = 'nail_it'
                      AND pms.actual_pickup_lat IS NOT NULL
                      AND pms.hourly_rate_offered BETWEEN 5 AND 150
                      AND NOT EXISTS (
                          SELECT 1 FROM public.community_offers co
                          WHERE co.actual_pickup_lat = pms.actual_pickup_lat
                            AND co.actual_pickup_lng = pms.actual_pickup_lng
                            AND co.data_source = 'nail_it'
                      )
                """, (offer_id,))
                conn.commit()
                logging.info(f"🌐 Community radar feed written via dropoff elevation: offer={offer_id}")
        except Exception as community_err:
            logging.warning(f"⚠️ Community radar INSERT failed (non-fatal): {community_err}")
            try:
                conn.rollback()
            except Exception:
                pass

        # ── Update offer_history ──────────────────────────────────────
        try:
            if _audit_offer_id:
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
                      error_m, classification, _audit_offer_id))
                logging.info(f"✅ Offer history dropoff updated: offer={_audit_offer_id}")
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
