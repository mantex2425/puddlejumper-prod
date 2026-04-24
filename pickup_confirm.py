# backend/pickup_confirm.py
# Nail It — Pickup confirmation endpoint
# Handles: offer lookup, ENROUTE→IN_TRIP transition, dropoff recalibration
# Shared targeting logic: nail_it_core.py

import logging
from flask import Blueprint, request, jsonify
from psycopg2.extras import RealDictCursor

from db import get_db
from utils import verify_and_get_user_id, require_firebase_auth
from state_machine import DriverStateMachine
from nail_it_core import compute_error, classify, should_refine, build_voice, get_accuracy_stats, write_nailed_position, write_nail_contest_event

pickup_confirm_bp = Blueprint('pickup_confirm', __name__)


@require_firebase_auth
@pickup_confirm_bp.route("/pickup/confirm", methods=["POST"])
def confirm_pickup():
    try:
        driver_id = verify_and_get_user_id(request)
        body = request.get_json()
        actual_lat = body.get("lat")
        actual_lng = body.get("lng")
        cumulative_miles = body.get("cumulative_miles")  # AAR odometer anchor; may be None
        if actual_lat is None or actual_lng is None:
            return jsonify({"error": "lat and lng are required"}), 400

        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # ── GPS-First Offer Resolution ────────────────────────────────
        # Step 1: Try current_offer_id (happy path)
        cur.execute("""
            SELECT
                pms.id AS pms_id,
                pms.offer_id,
                pms.pickup_h3,
                pms.triangulation_error_m AS existing_error_m,
                pms.actual_pickup_lat,
                dl.dropoff_lat,
                dl.dropoff_lng,
                dl.trip_miles,
                oh.pickup_address
            FROM app_private.driver_trip_state dts
            JOIN app_private.decision_log dl ON dl.id = dts.current_offer_id::integer
            JOIN app_private.pickup_market_signals pms ON pms.offer_id = dl.id
            LEFT JOIN app_private.offer_history oh ON oh.decision_log_id = dl.id
            WHERE dts.driver_id = %s
              AND dts.state IN ('ENROUTE', 'IN_TRIP', 'STACKED')
              AND dts.current_offer_id IS NOT NULL
        """, (driver_id,))
        row = cur.fetchone()

        # Step 2: GPS-first resolution — find offer by proximity to actual nail position
        # Handles: match offer race condition, S11 override (declined offer driven anyway)
        # Does NOT filter by app_verdict — DECLINE overrides must be found too
        cur.execute("""
            SELECT
                pms.id AS pms_id,
                pms.offer_id,
                pms.pickup_h3,
                pms.triangulation_error_m AS existing_error_m,
                pms.actual_pickup_lat,
                dl.dropoff_lat,
                dl.dropoff_lng,
                dl.trip_miles,
                oh.pickup_address,
                app_private.distance_miles(%s, %s, oh.pickup_lat, oh.pickup_lng)
                    * 1609.34 AS dist_m
            FROM app_private.offer_history oh
            JOIN app_private.decision_log dl ON dl.id = oh.decision_log_id
            JOIN app_private.pickup_market_signals pms ON pms.offer_id = dl.id
            WHERE dl.driver_id = %s
              AND oh.created_at > NOW() - INTERVAL '10 minutes'
              AND oh.pickup_lat IS NOT NULL
              AND oh.pickup_lng IS NOT NULL
            ORDER BY dist_m ASC, oh.created_at DESC
            LIMIT 1
        """, (actual_lat, actual_lng, driver_id,))
        gps_row = cur.fetchone()

        if gps_row and float(gps_row['dist_m']) < 500:
            if not row:
                logging.warning(
                    f"[OFFER RESOLUTION] current_offer_id NULL — "
                    f"resolved to offer={gps_row['offer_id']} "
                    f"by GPS proximity ({gps_row['dist_m']:.0f}m)"
                )
                row = gps_row
            elif str(gps_row['offer_id']) != str(row['offer_id']):
                logging.warning(
                    f"[OFFER RESOLUTION] current_offer_id={row['offer_id']} "
                    f"overridden by GPS proximity → offer={gps_row['offer_id']} "
                    f"({gps_row['dist_m']:.0f}m from nail position)"
                )
                row = gps_row
            else:
                logging.info(
                    f"[OFFER RESOLUTION] GPS confirms current_offer_id={row['offer_id']} "
                    f"({gps_row['dist_m']:.0f}m) ✅"
                )

        if not row:
            return jsonify({"status": "no_match", "message": "No active offer found"}), 404

        pms_id         = row['pms_id']
        offer_id       = row['offer_id']
        triangulated_h3 = row['pickup_h3']
        existing_error = float(row['existing_error_m']) if row['existing_error_m'] else None
        already_nailed = row['actual_pickup_lat'] is not None

        # ── Compute error and check refinement ────────────────────────
        actual_h3, error_m = compute_error(cur, actual_lat, actual_lng, triangulated_h3)
        classification = classify(error_m)
        update_ok, improvement = should_refine(existing_error, error_m)

        if not update_ok:
            logging.info(
                f"📍 Pickup refinement skipped — "
                f"improvement={improvement:.0f}m < 50m threshold"
            )
            conn.commit()
            return jsonify({
                "status":         "confirmed",
                "offerId":        offer_id,
                "errorMeters":    error_m,
                "classification": classification,
                "refined":        False,
                "driverState":    "IN_TRIP",
                "voice":          f"Already confirmed. {int(error_m)} meters."
            }), 200

        if already_nailed and improvement:
            logging.info(f"🎯 Pickup REFINED: {existing_error:.0f}m → {error_m:.0f}m (+{improvement:.0f}m)")

        # ── Update pickup_market_signals ──────────────────────────────
        cur.execute("""
            UPDATE app_private.pickup_market_signals
            SET actual_pickup_lat     = %s,
                actual_pickup_lng     = %s,
                actual_pickup_h3      = %s,
                actual_pickup_at      = NOW(),
                triangulation_error_m = %s,
                offer_status          = 'completed'
            WHERE id = %s
        """, (actual_lat, actual_lng, actual_h3, error_m, pms_id))

        # ── State transition ──────────────────────────────────────────
        current_state_row = DriverStateMachine.read(driver_id, cur)
        current_state = current_state_row["state"] if current_state_row else "ENROUTE"
        if current_state == "ENROUTE":
            DriverStateMachine.transition(driver_id, 'pickup_confirmed', cur, conn,
                offer_id=str(offer_id),
                nailed_pickup_lat=actual_lat,
                nailed_pickup_lng=actual_lng,
                cumulative_miles=cumulative_miles,
            )
        else:
            logging.info(f"📍 Pickup Nail It: state={current_state} — coord refinement only")

        # ── Write nailed pickup position ─────────────────────────────
        try:
            write_nailed_position(cur, driver_id, "pickup", actual_lat, actual_lng, error_m)
        except Exception as nail_e:
            logging.warning(f"⚠️ nailed_pickup write failed: {nail_e}")

        # SB3 post-nail refinement removed with arc_band.py / nail_manager.py.
        # Bead-on-wire does target refinement at heartbeat time via
        # check_convergence — nothing left to refine after the nail fires.
        #
        # FIXME [4-box violation, tracked for post-bead refactor]:
        # This file (PLAN layer) runs UPDATE statements directly against
        # app_private.offer_history and app_private.driver_trip_state below.
        # The 4-box rule says only EXECUTE (sm_transition) writes. These
        # pre-existing writes don't flow through sm_transition because they
        # mutate coordinate columns on the existing state row rather than
        # triggering a state transition. Fix requires either extending
        # sm_transition() to accept coord-only updates or adding a new
        # EXECUTE primitive for nailed-coord writes. Defer until bead-on-wire
        # is validated in production.

        # ── Update offer_history ──────────────────────────────────────
        try:
            cur.execute("""
                UPDATE app_private.offer_history
                SET actual_pickup_lat     = %s,
                    actual_pickup_lng     = %s,
                    actual_pickup_h3      = %s,
                    actual_pickup_at      = NOW(),
                    pickup_error_m        = %s,
                    pickup_classification = %s,
                    pickup_data_source    = 'nail_it'
                WHERE decision_log_id = %s
            """, (actual_lat, actual_lng, actual_h3, error_m, classification, offer_id))
        except Exception as oh_err:
            logging.warning(f"⚠️ Offer history pickup update failed: {oh_err}")

        # ── Stats + response ──────────────────────────────────────────
        stats = get_accuracy_stats(cur, driver_id, "pickup")
        conn.commit()

        # ── Community radar feed (fire-and-forget) ────────────────────
        try:
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
                    %s, %s, %s,
                    dl.fare, dl.trip_miles,
                    pms.dollars_per_mile, pms.hourly_rate_offered,
                    'nail_it',
                    app_private.coords_to_geography(%s, %s)
                FROM app_private.pickup_market_signals pms
                JOIN app_private.decision_log dl ON dl.id = pms.offer_id
                WHERE pms.offer_id = %s
                  AND pms.hourly_rate_offered BETWEEN 5 AND 150
                  AND NOT EXISTS (
                      SELECT 1 FROM public.community_offers co
                      WHERE co.actual_pickup_lat = %s
                        AND co.actual_pickup_lng = %s
                        AND co.data_source = 'nail_it'
                  )
            """, (
                actual_lat, actual_lng, actual_h3,
                actual_lat, actual_lng,
                offer_id,
                actual_lat, actual_lng,
            ))
            conn.commit()
            logging.info(f"🌐 Community radar feed written: offer={offer_id} ({actual_lat:.5f}, {actual_lng:.5f})")
        except Exception as community_err:
            logging.warning(f"⚠️ Community radar INSERT failed (non-fatal): {community_err}")
            try:
                conn.rollback()
            except Exception:
                pass

        # Arc-band street-geometry pre-warm removed with arc_band.py.
        # Bead-on-wire uses Google geocode cache, which warms naturally
        # on first lookup.

        voice = build_voice(stats['totalConfirmed'], error_m, "pickup")
        logging.info(f"Pickup confirmed: offer={offer_id} error={error_m}m class={classification}")

        return jsonify({
            "status":         "confirmed",
            "offerId":        offer_id,
            "errorMeters":    error_m,
            "classification": classification,
            "refined":        already_nailed,
            "triangulatedH3": triangulated_h3,
            "actualH3":       actual_h3,
            "driverState":    "IN_TRIP",
            "stats":          stats,
            "voice":          voice
        }), 200

    except Exception as e:
        logging.exception("pickup confirm error")
        if 'conn' in locals(): conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        if 'conn' in locals(): conn.close()
