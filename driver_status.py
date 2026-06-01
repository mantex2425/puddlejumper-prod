# backend/driver_status.py
# Real-time driver state endpoint for web monitor at app.puddlejumper.io/monitor
# GET /api/v1/driver/status — polls every 3 seconds from iPhone

import logging
import datetime
from flask import Blueprint, request, jsonify
from psycopg2.extras import RealDictCursor

from db import get_db
from driver_queue import DriverQueue
from utils import verify_and_get_user_id, require_firebase_auth

driver_status_bp = Blueprint('driver_status', __name__)


@require_firebase_auth
@driver_status_bp.route("/driver/status", methods=["GET"])
def get_driver_status():
    try:
        driver_id = verify_and_get_user_id(request)

        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # ── Current state (HINT post-Sub-commit 1c: bound_offer_id) ────
        # See driver_queue.py / L-19. This composite SELECT pulls the
        # bound pointer alongside coords + heartbeat for the UI status
        # payload; treat current_offer_id as advisory. For authoritative
        # queue membership, route through DriverQueue.snapshot(). UI
        # rendering can tolerate brief inconsistency during an L-19-class
        # self-heal — next status poll converges.
        cur.execute("""
            SELECT
                current_offer_id,
                pickup_lat, pickup_lng,
                dropoff_lat, dropoff_lng,
                potential_cancellation,
                heartbeat,
                heartbeat_at,
                last_odometer_move_at
            FROM app_private.driver_trip_state
            WHERE driver_id = %s
        """, (driver_id,))
        state_row = cur.fetchone()
        hb = state_row["heartbeat"] if state_row and state_row["heartbeat"] else {}

        # ── Last known GPS + triangulation context ────────────────────
        cur.execute("""
            SELECT
                current_lat, current_lng,
                trace_data->>'gps_age_sec'                 AS gps_age_sec,
                decision_result->>'confidenceTier'         AS confidence_tier,
                decision_result->>'confidenceRadius'       AS confidence_radius,
                decision_result->>'odometerFloor'          AS odometer_floor,
                decision_result->>'odometerCeiling'        AS odometer_ceiling,
                decision_result->>'triangulatedPickupLat'  AS tri_pickup_lat,
                decision_result->>'triangulatedPickupLng'  AS tri_pickup_lng,
                decision_result->>'triangulatedDropoffLat' AS tri_dropoff_lat,
                decision_result->>'triangulatedDropoffLng' AS tri_dropoff_lng,
                decision_result->>'verdict'                AS last_verdict,
                decision_result->>'reason'                 AS last_reason,
                fare                                       AS last_fare,
                pickup_miles                               AS last_pickup_miles,
                trip_miles                                 AS last_trip_miles,
                created_at AS last_decision_at
            FROM app_private.decision_log
            WHERE driver_id = %s
            ORDER BY created_at DESC
            LIMIT 1
        """, (driver_id,))
        ld = cur.fetchone()

        # ── Recent offers ─────────────────────────────────────────────
        cur.execute("""
            SELECT
                dl.id,
                dl.fare,
                dl.trip_miles,
                dl.pickup_miles,
                oh.pickup_address,
                oh.dropoff_address,
                oh.app_verdict,
                oh.app_reason,
                pms.offer_status,
                pms.data_source,
                pms.triangulation_error_m,
                EXTRACT(EPOCH FROM (
                    NOW() -
                    dl.created_at
                ))::integer AS seconds_ago
            FROM app_private.decision_log dl
            LEFT JOIN app_private.offer_history oh ON oh.decision_log_id = dl.id
            LEFT JOIN app_private.pickup_market_signals pms ON pms.offer_id = dl.id
            WHERE dl.driver_id = %s
            ORDER BY dl.created_at DESC
            LIMIT 4
        """, (driver_id,))
        recent_offers = cur.fetchall()

        # ── Last 3 planner dispatch actions (Cut B3 observability) ──
        cur.execute("""
            SELECT
                planner_action,
                primary_offer_id,
                cluster_lat,
                cluster_lng,
                cluster_size,
                dispatch_executed,
                dispatch_error,
                current_offer_id_at_eval,
                wai_pudo_type,
                wai_target_address,
                wai_reason,
                wai_confidence,
                planner_reason,
                EXTRACT(EPOCH FROM (
                    NOW() - created_at
                ))::integer AS seconds_ago
            FROM app_private.pudo_decision_context
            WHERE driver_id = %s
            ORDER BY created_at DESC
            LIMIT 3
        """, (driver_id,))
        last_3_dispatch_actions = cur.fetchall()

        def f(val):
            try: return float(val) if val is not None else None
            except: return None

        return jsonify({
            # 1-bit memory (offer_id is the post-demolition state surface)
            "offer_id":                      state_row["current_offer_id"] if state_row else None,
            "potential_cancellation":        state_row["potential_cancellation"] if state_row else False,

            # Auto Nail It — from Android heartbeat
            "armed":                    hb.get("armed"),
            "target_type":              hb.get("target_type", "pickup" if not state_row or not state_row["current_offer_id"] else "dropoff"),
            "dist_to_target_m":         hb.get("dist_to_target_m"),
            "confidence_radius_m":      f(ld["confidence_radius"]) if ld else None,
            "cumulative_miles":         hb.get("cumulative_miles"),
            "expected_trip_miles":      f(ld["last_trip_miles"]) if ld and state_row and state_row["current_offer_id"] else None,
            "odometer_floor":           f(ld["odometer_floor"]) if ld else None,
            "stopped_seconds":          hb.get("stopped_seconds"),
            "required_stopped_seconds": hb.get("required_stopped_seconds"),
            "heartbeat_age_sec":        round((datetime.datetime.now(datetime.timezone.utc) - state_row["heartbeat_at"]).total_seconds()) if state_row and state_row.get("heartbeat_at") else None,

            # GPS — heartbeat first, fallback to last decision
            "gps_lat":        hb.get("lat") or (f(ld["current_lat"]) if ld else None),
            "gps_lng":        hb.get("lng") or (f(ld["current_lng"]) if ld else None),
            "gps_age_sec":    f(ld["gps_age_sec"]) if ld else None,
            "speed_mph":      hb.get("speed_mph"),
            "gps_accuracy_m": hb.get("gps_accuracy_m"),

            # Last decision
            "last_fare":          f(ld["last_fare"]) if ld else None,
            "last_pickup_miles":  f(ld["last_pickup_miles"]) if ld else None,
            "last_trip_miles":    f(ld["last_trip_miles"]) if ld else None,
            "last_verdict":       ld["last_verdict"] if ld else None,
            "last_reason":        ld["last_reason"] if ld else None,
            "confidence_tier":    ld["confidence_tier"] if ld else None,
            "triangulated_pickup_lat":  f(ld["tri_pickup_lat"]) if ld else None,
            "triangulated_pickup_lng":  f(ld["tri_pickup_lng"]) if ld else None,
            "triangulated_dropoff_lat": f(ld["tri_dropoff_lat"]) if ld else None,
            "triangulated_dropoff_lng": f(ld["tri_dropoff_lng"]) if ld else None,

            # Recent offers
            "recent_offers": [{
                "id":                    o["id"],
                "fare":                  f(o["fare"]),
                "trip_miles":            f(o["trip_miles"]),
                "pickup_miles":          f(o["pickup_miles"]),
                "pickup_address":        o["pickup_address"],
                "dropoff_address":       o["dropoff_address"],
                "app_verdict":           o["app_verdict"],
                "app_reason":            o["app_reason"],
                "offer_status":          o["offer_status"],
                "data_source":           o["data_source"],
                "triangulation_error_m": f(o["triangulation_error_m"]),
                "seconds_ago":           o["seconds_ago"],
            } for o in recent_offers],

            # All live offers in the GC-survivor queue, ordered by
            # created_at DESC (newest first). The bound offer (the one
            # whose pickup has fired) is also surfaced as top-level
            # `offer_id`; the monitor floats that one to the top.
            # Lightweight projection — no TargetSpec building needed.
            #
            # Per §XIV.H: thread the heartbeat's per-tick odometer state
            # so the predicate's distance and staleness gates evaluate
            # against the same signals the heartbeat handler sees. Prior
            # to 2026-06-01 this call passed nothing; both gates degraded
            # to "always pass" and the monitor surfaced offers the
            # heartbeat handler had correctly reaped. See
            # docs/RECON_QUEUE_NO_REAP_2026-06-01.md.
            "planner_queue": list(
                DriverQueue(driver_id).offer_ids_only(
                    cur,
                    current_cumulative_miles=hb.get("cumulative_miles"),
                    last_odometer_move_at=(
                        state_row["last_odometer_move_at"] if state_row else None
                    ),
                )
            ),

            # Latest WAI evaluation (lifted from newest pudo_decision_context row)
            "last_wai_evaluation": {
                "wai_pudo_type":      last_3_dispatch_actions[0]["wai_pudo_type"]      if last_3_dispatch_actions else None,
                "wai_target_address": last_3_dispatch_actions[0]["wai_target_address"] if last_3_dispatch_actions else None,
                "wai_reason":         last_3_dispatch_actions[0]["wai_reason"]         if last_3_dispatch_actions else None,
                "wai_confidence":     f(last_3_dispatch_actions[0]["wai_confidence"])  if last_3_dispatch_actions else None,
                "seconds_ago":        last_3_dispatch_actions[0]["seconds_ago"]        if last_3_dispatch_actions else None,
            },

            # Last 3 planner actions (forensic visibility into Auto Nail It)
            "last_3_dispatch_actions": [{
                "planner_action":     a["planner_action"],
                "primary_offer_id":   a["primary_offer_id"],
                "cluster_lat":        f(a["cluster_lat"]),
                "cluster_lng":        f(a["cluster_lng"]),
                "cluster_size":       a["cluster_size"],
                "dispatch_executed":  a["dispatch_executed"],
                "dispatch_error":     a["dispatch_error"],
                "current_offer_id_at_eval": a["current_offer_id_at_eval"],
                "wai_pudo_type":      a["wai_pudo_type"],
                "wai_target_address": a["wai_target_address"],
                "wai_reason":         a["wai_reason"],
                "wai_confidence":     f(a["wai_confidence"]),
                "planner_reason":     a["planner_reason"],
                "seconds_ago":        a["seconds_ago"],
            } for a in last_3_dispatch_actions],

            "server_time": datetime.datetime.now().isoformat(),
        }), 200

    except Exception as e:
        logging.exception("driver status error")
        return jsonify({"error": str(e)}), 500
    finally:
        if 'conn' in locals():
            conn.close()
