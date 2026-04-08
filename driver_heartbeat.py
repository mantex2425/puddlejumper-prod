# backend/driver_heartbeat.py
# Android AutoNailItManager pushes real-time metrics every 5 seconds
# POST /api/v1/driver/heartbeat
# Stored in driver_trip_state — surfaced via /api/v1/driver/status

import json
import logging
import datetime
import concurrent.futures

from flask import Blueprint, request, jsonify
from psycopg2.extras import RealDictCursor

from db import get_db
from utils import verify_and_get_user_id, require_firebase_auth
from nail_it_core import check_convergence, write_nailed_position
from decisions.triangulation_enricher import refine_dropoff_background
from state_machine import DriverStateMachine

driver_heartbeat_bp = Blueprint('driver_heartbeat', __name__)

# ── Module-level executor — one instance, never recreated per heartbeat ────────
_refine_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)


def _queue_refine(driver_id, plat, plng, dlat, dlng, trip_miles, label):
    """Submit a background dropoff refinement. Never raises."""
    try:
        _refine_executor.submit(
            refine_dropoff_background,
            driver_id, float(plat), float(plng),
            float(dlat), float(dlng), trip_miles, label
        )
        logging.info(f"[REFINE] {label} refinement queued")
    except Exception as e:
        logging.warning(f"[REFINE] Failed to queue {label}: {e}")


def _get_trip_miles(driver_id, cur):
    """Fetch trip_miles from most recent decision_log row."""
    try:
        cur.execute("""
            SELECT trip_miles FROM app_private.decision_log
            WHERE driver_id = %s
            ORDER BY created_at DESC LIMIT 1
        """, (driver_id,))
        r = cur.fetchone()
        return float(r['trip_miles']) if r and r['trip_miles'] else None
    except Exception as e:
        logging.warning(f"[HEARTBEAT] _get_trip_miles failed: {e}")
        return None


# _write_state_transition removed — use DriverStateMachine.transition()


@require_firebase_auth
@driver_heartbeat_bp.route("/driver/heartbeat", methods=["POST"])
def post_heartbeat():
    try:
        driver_id = verify_and_get_user_id(request)
        body = request.get_json(silent=True) or {}

        armed                    = body.get("armed")
        target_type              = body.get("target_type")
        dist_to_target_m         = body.get("dist_to_target_m")
        cumulative_miles         = body.get("cumulative_miles")
        stopped_seconds          = body.get("stopped_seconds")
        required_stopped_seconds = body.get("required_stopped_seconds")
        speed_mph                = body.get("speed_mph")
        gps_accuracy_m           = body.get("gps_accuracy_m")
        enroute_seconds          = body.get("enroute_seconds")
        current_lat              = body.get("lat")
        current_lng              = body.get("lng")

        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # ── Store heartbeat ────────────────────────────────────────────────────
        # Upsert — only update if row exists (never create phantom state rows)
        cur.execute("""
            UPDATE app_private.driver_trip_state
            SET heartbeat    = %s::jsonb,
                heartbeat_at = NOW()
            WHERE driver_id = %s
        """, (
            json.dumps({
                "armed":                    armed,
                "target_type":              target_type,
                "dist_to_target_m":         dist_to_target_m,
                "cumulative_miles":         cumulative_miles,
                "stopped_seconds":          stopped_seconds,
                "required_stopped_seconds": required_stopped_seconds,
                "speed_mph":                speed_mph,
                "gps_accuracy_m":           gps_accuracy_m,
                "enroute_seconds":          enroute_seconds,
                "received_at":              datetime.datetime.now().isoformat(),
            }),
            driver_id
        ))
        conn.commit()

        # ── Convergence engine ─────────────────────────────────────────────────
        # Default to real DB state — never lie to Android by defaulting UNCOMMITTED
        driverState = "UNCOMMITTED"

        if current_lat is None or current_lng is None or speed_mph is None:
            logging.info("[HEARTBEAT] Missing GPS or speed — skipping convergence")
            return jsonify({"status": "ok", "driverState": driverState}), 200

        cur.execute("""
            SELECT state, pickup_lat, pickup_lng, dropoff_lat, dropoff_lng,
                   nailed_pickup_lat, nailed_pickup_lng,
                   nailed_pickup_error_m, nailed_dropoff_error_m,
                   state_updated_at,
                   EXTRACT(EPOCH FROM (
                       NOW() - state_updated_at
                   ))::integer AS state_seconds
            FROM app_private.driver_trip_state
            WHERE driver_id = %s
        """, (driver_id,))
        state_row = cur.fetchone()

        if not state_row:
            logging.warning("[HEARTBEAT] No state row found for driver")
            return jsonify({"status": "ok", "driverState": driverState}), 200

        # Real state is the truth — override default immediately
        driverState = state_row['state']
        _s    = state_row['state']
        _dlat = state_row.get('dropoff_lat')
        _dlng = state_row.get('dropoff_lng')

        # ── Background dropoff refinement ──────────────────────────────────────
        # Queue only when refinement is needed and not already nailed
        if (_s == 'ENROUTE'
                and state_row.get('nailed_dropoff_lat') is None
                and state_row.get('pickup_lat') and state_row.get('pickup_lng')):
            tm = _get_trip_miles(driver_id, cur)
            if tm and _dlat and _dlng:
                _queue_refine(driver_id,
                              state_row['pickup_lat'], state_row['pickup_lng'],
                              _dlat, _dlng, tm, 'ENROUTE')

        elif (_s == 'IN_TRIP'
                and state_row.get('nailed_pickup_lat')
                and state_row.get('nailed_dropoff_error_m') is None):
            tm = _get_trip_miles(driver_id, cur)
            if tm and _dlat and _dlng:
                _queue_refine(driver_id,
                              state_row['nailed_pickup_lat'], state_row['nailed_pickup_lng'],
                              _dlat, _dlng, tm, 'IN_TRIP')

        # ── Convergence check ──────────────────────────────────────────────────
        verdict, new_state, new_error_m = check_convergence(
            driver_id, current_lat, current_lng,
            speed_mph, state_row, cur
        )
        logging.info(
            f"[HEARTBEAT] convergence verdict={verdict} "
            f"state={_s}->new={new_state} error={new_error_m}"
        )

        # ── Apply verdict — all transitions use _write_state_transition ────────
        _just_nailed_pickup = False  # set True when INITIAL_NAIL fires this heartbeat
        if verdict == 'INITIAL_NAIL':
            # ENROUTE → IN_TRIP: nail pickup, transition state
            write_nailed_position(cur, driver_id, 'pickup',
                                  current_lat, current_lng, new_error_m)
            DriverStateMachine.transition(driver_id, 'gps_convergence', cur, conn,
                nailed_pickup_lat=current_lat,
                nailed_pickup_lng=current_lng,
                nailed_pickup_error_m=new_error_m,
            )
            logging.warning(f"S29 INITIAL_NAIL: ENROUTE->IN_TRIP at {new_error_m or 0:.0f}m")
            driverState = "IN_TRIP"
            _just_nailed_pickup = True

        elif verdict == 'DROPOFF_NAIL':
            # IN_TRIP → UNCOMMITTED: nail dropoff, full coord reset
            write_nailed_position(cur, driver_id, 'dropoff',
                                  current_lat, current_lng, new_error_m)
            DriverStateMachine.transition(driver_id, 'dropoff_confirmed', cur, conn,
                clear_coords=True,
            )
            logging.warning(f"S31 DROPOFF_NAIL: IN_TRIP->UNCOMMITTED at {new_error_m or 0:.0f}m")
            driverState = "UNCOMMITTED"

        elif verdict == 'REFINE_PICKUP':
            write_nailed_position(cur, driver_id, 'pickup',
                                  current_lat, current_lng, new_error_m)
            logging.info(f"[HEARTBEAT] REFINE_PICKUP: improved to {new_error_m:.0f}m")
            # driverState already set to real state above

        elif verdict == 'REFINE_DROPOFF':
            write_nailed_position(cur, driver_id, 'dropoff',
                                  current_lat, current_lng, new_error_m)
            logging.info(f"[HEARTBEAT] REFINE_DROPOFF: improved to {new_error_m:.0f}m")
            # driverState already set to real state above

        elif verdict == 'REFINE':
            target = 'pickup' if _s == 'ENROUTE' else 'dropoff'
            write_nailed_position(cur, driver_id, target,
                                  current_lat, current_lng, new_error_m)
            logging.info(f"[HEARTBEAT] REFINE: {target} nail improved to {new_error_m:.0f}m")
            # driverState already set to real state above

        elif verdict == 'ABORT':
            # ENROUTE → UNCOMMITTED: driver diverged from pickup
            DriverStateMachine.transition(driver_id, 'gps_divergence', cur, conn,
                clear_coords=True,
            )
            logging.warning(f"S30 ABORT: ENROUTE->UNCOMMITTED gps_divergence")
            driverState = "UNCOMMITTED"

        else:
            # No transition — driver cruising normally
            # driverState already set to real state above
            logging.info(f"[HEARTBEAT] No transition — state stays {_s}")

        # ── Re-read current state after verdict (may have changed above) ──────
        _s = DriverStateMachine.read(driver_id, cur)["state"]

        # ── S11: UNCOMMITTED + GPS near recent offer pickup ───────────────────
        # Driver accepted an offer the system declined — promote to IN_TRIP
        if _s == "UNCOMMITTED":
            cur.execute(
                "SELECT * FROM app_private.sm_find_nearby_offer(%s, %s, %s)",
                (driver_id, current_lat, current_lng)
            )
            nearby = cur.fetchone()
            if nearby:
                logging.info(
                    f"[S11] GPS convergence at declined offer pickup "
                    f"(dist={nearby['dist_miles']:.2f}mi, offer={nearby['offer_id']}) → IN_TRIP"
                )
                DriverStateMachine.transition(driver_id, 'gps_convergence', cur, conn,
                    offer_id=str(nearby['offer_id']),
                    pickup_lat=nearby['pickup_lat'],
                    pickup_lng=nearby['pickup_lng'],
                    pickup_h3=nearby['pickup_h3'],
                    dropoff_lat=nearby['dropoff_lat'],
                    dropoff_lng=nearby['dropoff_lng'],
                    dropoff_h3=nearby['dropoff_h3'],
                )
                driverState = "IN_TRIP"
                _s = "IN_TRIP"

        # ── S17: STACKED + GPS near primary dropoff → ENROUTE (buffer swap) ───
        elif _s == "STACKED":
            _state_row = DriverStateMachine.read(driver_id, cur)
            _dlat = _state_row.get("dropoff_lat")
            _dlng = _state_row.get("dropoff_lng")
            if _dlat and _dlng:
                cur.execute(
                    "SELECT app_private.distance_miles(%s, %s, %s, %s) AS dist",
                    (current_lat, current_lng, _dlat, _dlng)
                )
                _dist = cur.fetchone()["dist"]
                if _dist <= 0.31:
                    # Buffer swap: fetch secondary ride dropoff
                    _sec_dlat = _sec_dlng = _sec_dh3 = None
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
                            f"[S17] Buffer swap: secondary dropoff "
                            f"({_sec_dlat:.4f},{_sec_dlng:.4f})"
                        )
                    DriverStateMachine.transition(driver_id, 'gps_convergence', cur, conn,
                        dropoff_lat=_sec_dlat,
                        dropoff_lng=_sec_dlng,
                        dropoff_h3=_sec_dh3,
                    )
                    driverState = "ENROUTE"
                    _s = "ENROUTE"

        # ── S12: IN_TRIP or STACKED + GPS near unexpected pickup ──────────────
        # Primary ride cancelled — promote secondary to primary
        # Guard 1: skip if we just nailed pickup this heartbeat (S12 cannot fire
        #          in the same heartbeat as INITIAL_NAIL — we ARE at our pickup)
        # Guard 2: skip if nearby offer IS the current offer
        if _s in ("IN_TRIP", "STACKED") and not _just_nailed_pickup:
            cur.execute(
                "SELECT * FROM app_private.sm_find_nearby_offer(%s, %s, %s)",
                (driver_id, current_lat, current_lng)
            )
            nearby = cur.fetchone()
            _current_offer = DriverStateMachine.read(driver_id, cur).get("current_offer_id")
            if nearby and str(nearby["offer_id"]) != str(_current_offer or ""):
                logging.info(
                    f"[S12] GPS at unexpected pickup while {_s} "
                    f"— primary cancelled, promoting secondary → IN_TRIP "
                    f"(dist={nearby['dist_miles']:.2f}mi, offer={nearby['offer_id']})"
                )
                DriverStateMachine.transition(driver_id, 'gps_convergence', cur, conn,
                    offer_id=str(nearby['offer_id']),
                    pickup_lat=nearby['pickup_lat'],
                    pickup_lng=nearby['pickup_lng'],
                    pickup_h3=nearby['pickup_h3'],
                    dropoff_lat=nearby['dropoff_lat'],
                    dropoff_lng=nearby['dropoff_lng'],
                    dropoff_h3=nearby['dropoff_h3'],
                )
                driverState = "IN_TRIP"

        conn.commit()
        return jsonify({"status": "ok", "driverState": driverState}), 200

    except Exception as e:
        logging.exception("[HEARTBEAT] Unhandled error")
        return jsonify({"error": str(e)}), 500
    finally:
        if 'conn' in locals():
            conn.close()
