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
from nail_it_core import (check_convergence, write_nailed_position,
                          classify, process_heartbeat, clear_buffer,
                          get_next_stacked_offer)
from decisions.triangulation_enricher import refine_dropoff_background
from state_machine import DriverStateMachine
from decisions.triangulation_enricher import _cache_stacked_polyline_async
from nail_it_core import write_nail_contest_event

driver_heartbeat_bp = Blueprint('driver_heartbeat', __name__)

# ── Module-level executor — one instance, never recreated per heartbeat ────────
_refine_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)

# ── Dual-Watchdog state tracking (module-level, reset on restart) ─────────────
_stopped_since   = None   # datetime when speed dropped below threshold
_candidate_lat   = None   # most recent micro-stop lat
_candidate_lng   = None   # most recent micro-stop lng
_candidate_at    = None   # timestamp of micro-stop

def _reset_watchdog_state(cur=None, conn=None, driver_id=None):
    """Clear candidate and stopped timer. Call on transition to UNCOMMITTED."""
    global _stopped_since, _candidate_lat, _candidate_lng, _candidate_at
    _stopped_since = None
    _candidate_lat = None
    _candidate_lng = None
    _candidate_at  = None
    if cur and conn and driver_id:
        try:
            cur.execute(
                """
                UPDATE app_private.driver_trip_state
                SET candidate_lat = NULL, candidate_lng = NULL, candidate_at = NULL
                WHERE driver_id = %s
                """,
                (driver_id,)
            )
            conn.commit()
        except Exception as _e:
            logging.warning(f"[WATCHDOG] Failed to clear candidate in DB: {_e}")


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
    global _candidate_lat, _candidate_lng, _candidate_at, _stopped_since
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

        heading = body.get("heading")  # degrees 0-360, nullable
        # MONITOR → DIAGNOSE: forward raw heartbeat to Phase 0 shadow buffer
        if current_lat is not None and current_lng is not None and speed_mph is not None:
            process_heartbeat(driver_id, current_lat, current_lng, speed_mph, heading=heading)

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
                "lat":                      current_lat,
                "lng":                      current_lng,
                "received_at":              datetime.datetime.now().isoformat(),
            }),
            driver_id
        ))
        conn.commit()

        # ── Patch 00567: heartbeat_log flight recorder ──────────────────────
        # No explicit commit — absorbed into downstream commit(s) in this handler.
        # On failure: log + rollback so heartbeat state is still clean.
        try:
            cur.execute("""
                SELECT state, current_offer_id
                FROM app_private.driver_trip_state
                WHERE driver_id = %s
            """, (driver_id,))
            _log_state_row = cur.fetchone() or {}
            cur.execute("""
                INSERT INTO app_private.heartbeat_log
                  (driver_id, lat, lng, speed_mph, gps_accuracy_m,
                   stopped_seconds, armed, target_type, dist_to_target_m,
                   state, current_offer_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                driver_id, current_lat, current_lng, speed_mph, gps_accuracy_m,
                stopped_seconds, armed, target_type, dist_to_target_m,
                _log_state_row.get('state'),
                _log_state_row.get('current_offer_id'),
            ))
            conn.commit()  # explicit commit — flight recorder must persist
        except Exception as _hb_log_err:
            logging.warning(f"[HEARTBEAT_LOG] insert failed (non-fatal): {_hb_log_err}")
            try:
                conn.rollback()
            except Exception:
                pass
        # ── End Patch 00567 ─────────────────────────────────────────────────

        # ── Convergence engine ─────────────────────────────────────────────────
        # Default to real DB state — never lie to Android by defaulting UNCOMMITTED
        driverState = "UNCOMMITTED"

        if current_lat is None or current_lng is None or speed_mph is None:
            # Read real state from DB before returning — don't lie to Android
            _state_check = DriverStateMachine.read(driver_id, cur)
            if _state_check:
                driverState = _state_check["state"]
            logging.info(f"[HEARTBEAT] Missing GPS or speed — skipping convergence (state={driverState})")
            return jsonify({"status": "ok", "driverState": driverState}), 200

        cur.execute("""
            SELECT dts.state, dts.pickup_lat, dts.pickup_lng, dts.dropoff_lat, dts.dropoff_lng,
                   dts.nailed_pickup_lat, dts.nailed_pickup_lng,
                   dts.nailed_pickup_error_m, dts.nailed_dropoff_error_m,
                   dts.current_offer_id,
                   dts.candidate_lat, dts.candidate_lng, dts.candidate_at,
                   dts.potential_cancellation,
                   dts.state_updated_at,
                   EXTRACT(EPOCH FROM (
                       NOW() - dts.state_updated_at
                   ))::integer AS state_seconds,
                   oh.dropoff_address,
                   oh.pickup_address
            FROM app_private.driver_trip_state dts
            LEFT JOIN app_private.decision_log dl
                   ON dl.id = dts.current_offer_id::integer
            LEFT JOIN app_private.offer_history oh
                   ON oh.decision_log_id = dl.id
            WHERE dts.driver_id = %s
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
        _raw = check_convergence(
            driver_id, current_lat, current_lng,
            speed_mph, state_row, cur,
            stopped_seconds=stopped_seconds or 0,
            candidate_lat=state_row.get("candidate_lat"),
            candidate_lng=state_row.get("candidate_lng"),
            cumulative_miles=cumulative_miles,
        )
        if len(_raw) == 4:
            verdict, new_state, new_error_m, _extra = _raw
        else:
            verdict, new_state, new_error_m = _raw
            _extra = None
        logging.info(
            f"[HEARTBEAT] convergence verdict={verdict} "
            f"state={_s}->new={new_state} error={new_error_m}"
        )

        # ── Apply verdict — all transitions use _write_state_transition ────────
        _just_nailed_pickup = False  # set True when INITIAL_NAIL fires this heartbeat
        if new_state == 'UNCOMMITTED':
            _reset_watchdog_state(cur=cur, conn=conn, driver_id=driver_id)
            clear_buffer(driver_id)  # DIAGNOSE: clear stop buffer on new trip cycle
            logging.info("[WATCHDOG] State reset to UNCOMMITTED — cleared candidate stack")
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

            # Patch 00562: contest event for gps_convergence pickup (Scorer B = pickup_not_scorable)
            try:
                write_nail_contest_event(
                    cur, conn,
                    driver_id=driver_id,
                    state_row=state_row,
                    nail_path="gps_convergence_pickup",
                    current_lat=current_lat,
                    current_lng=current_lng,
                    current_speed_mph=speed_mph or 0.0,
                    current_heading=0.0,
                    stopped_seconds=float(stopped_seconds or 0),
                    dist_to_target_m=float(new_error_m or 0),
                    target_lat=state_row.get('pickup_lat'),
                    target_lng=state_row.get('pickup_lng'),
                    pickup_lat=state_row.get('pickup_lat'),
                    pickup_lng=state_row.get('pickup_lng'),
                    dropoff_lat=state_row.get('dropoff_lat'),
                    dropoff_lng=state_row.get('dropoff_lng'),
                )
            except Exception as _ce:
                logging.warning(f"[CONTEST] hook1 non-fatal: {_ce}")

            # SB3 post-nail refinement removed with arc_band.py / nail_manager.py.
            # Bead-on-wire does target refinement at heartbeat time via
            # check_convergence — nothing left to refine after the nail fires.
            #
            # FIXME [4-box violation, tracked for post-bead refactor]:
            # Sibling UPDATEs on pickup_market_signals + community_offers
            # below this block are pre-existing direct writes from PLAN
            # layer that should flow through an EXECUTE primitive. Defer
            # until bead-on-wire is validated in production.

        if _just_nailed_pickup:
            # ── Feed community radar from Auto Nail It (fire-and-forget) ──
            try:
                cur.execute("""
                    UPDATE app_private.pickup_market_signals
                    SET actual_pickup_lat     = %s,
                        actual_pickup_lng     = %s,
                        actual_pickup_h3      = app_private.coords_to_h3(%s, %s),
                        actual_pickup_at      = NOW() AT TIME ZONE 'America/Chicago',
                        triangulation_error_m = %s,
                        data_source           = 'nail_it',
                        offer_status          = 'completed'
                    WHERE offer_id = (
                        SELECT current_offer_id::integer
                        FROM app_private.driver_trip_state
                        WHERE driver_id = %s
                    )
                    AND data_source != 'nail_it'
                """, (current_lat, current_lng,
                      current_lat, current_lng,
                      new_error_m, driver_id))

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
                        %s, %s, app_private.coords_to_h3(%s, %s),
                        dl.fare, dl.trip_miles,
                        pms.dollars_per_mile, pms.hourly_rate_offered,
                        'nail_it',
                        app_private.coords_to_geography(%s, %s)
                    FROM app_private.pickup_market_signals pms
                    JOIN app_private.decision_log dl ON dl.id = pms.offer_id
                    JOIN app_private.driver_trip_state dts ON dts.driver_id = %s
                    WHERE pms.offer_id = dts.current_offer_id::integer
                      AND pms.hourly_rate_offered BETWEEN 5 AND 150
                      AND NOT EXISTS (
                          SELECT 1 FROM public.community_offers co
                          WHERE co.actual_pickup_lat = %s
                            AND co.actual_pickup_lng = %s
                            AND co.data_source = 'nail_it'
                      )
                """, (
                    current_lat, current_lng,
                    current_lat, current_lng,
                    current_lat, current_lng,
                    driver_id,
                    current_lat, current_lng,
                ))
                # Update personal offer_history with GPS accuracy
                cur.execute("""
                    UPDATE app_private.offer_history
                    SET actual_pickup_lat     = %s,
                        actual_pickup_lng     = %s,
                        actual_pickup_h3      = app_private.coords_to_h3(%s, %s),
                        actual_pickup_at      = NOW() AT TIME ZONE 'America/Chicago',
                        pickup_error_m        = %s,
                        pickup_classification = %s,
                        pickup_data_source    = 'nail_it'
                    WHERE decision_log_id = (
                        SELECT current_offer_id::integer
                        FROM app_private.driver_trip_state
                        WHERE driver_id = %s
                    )
                """, (current_lat, current_lng,
                      current_lat, current_lng,
                      new_error_m,
                      classify(new_error_m),
                      driver_id))
                conn.commit()
                logging.info(
                    f"🌐 Auto Nail It → community radar: "
                    f"({current_lat:.5f},{current_lng:.5f})"
                )
            except Exception as radar_err:
                logging.warning(
                    f"⚠️ Auto Nail It radar feed failed (non-fatal): {radar_err}"
                )
                try:
                    conn.rollback()
                except Exception:
                    pass

        elif verdict == 'DROPOFF_NAIL':
            # IN_TRIP → REFINE_DROPOFF → UNCOMMITTED: enforce linear path
            write_nailed_position(cur, driver_id, 'dropoff',
                                  current_lat, current_lng, new_error_m)
            # ── Box 4: Close the audit loop ───────────────────────────
            _offer_id = state_row.get('current_offer_id')
            # For STACKED rides, current_offer_id points to the secondary offer.
            # Find the primary offer (pickup confirmed, dropoff not yet confirmed).
            _audit_id = _offer_id
            if _s == 'STACKED':
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
                        _audit_id = _primary['decision_log_id']
                        logging.info(f"[S17] Audit lock: primary offer={_audit_id} (state had {_offer_id})")
                except Exception as _al_err:
                    logging.warning(f"[S17] Audit lock failed, falling back to {_offer_id}: {_al_err}")
            if _audit_id:
                try:
                    cur.execute("SELECT app_private.safe_h3(%s,%s)::text AS h3",
                                (current_lat, current_lng))
                    _h3r = cur.fetchone()
                    _actual_h3 = _h3r['h3'] if _h3r else None

                    # SB3 trip-close hook (Patch 00566a Step 12, watchdog_a path):
                    # Propagate refined_dropoff_* and refinement_source from
                    # driver_trip_state into this trip's offer_history row.
                    cur.execute("""
                        SELECT refined_dropoff_lat, refined_dropoff_lng, refinement_source
                        FROM app_private.driver_trip_state
                        WHERE driver_id = %s
                    """, (driver_id,))
                    _dts_row = cur.fetchone() or {}
                    _refined_lat = _dts_row.get("refined_dropoff_lat")
                    _refined_lng = _dts_row.get("refined_dropoff_lng")
                    _refinement_source = _dts_row.get("refinement_source")

                    cur.execute("""
                        UPDATE app_private.offer_history
                        SET actual_dropoff_lat     = %s,
                            actual_dropoff_lng     = %s,
                            actual_dropoff_h3      = %s,
                            actual_dropoff_at      = NOW(),
                            dropoff_error_m        = %s,
                            dropoff_classification = %s,
                            refined_dropoff_lat    = %s,
                            refined_dropoff_lng    = %s,
                            refinement_source      = %s
                        WHERE decision_log_id = %s::integer
                    """, (current_lat, current_lng, _actual_h3,
                          new_error_m, 'watchdog_a',
                          _refined_lat, _refined_lng, _refinement_source,
                          _audit_id))
                    logging.info(
                        f"[HEARTBEAT] offer_history dropoff updated (watchdog_a) offer={_audit_id} "
                        f"refined={'yes' if _refined_lat is not None else 'no'} "
                        f"source={_refinement_source}"
                    )
                except Exception as _oh_err:
                    logging.warning(f"[HEARTBEAT] offer_history dropoff update failed: {_oh_err}")
            if _s == 'STACKED':
                # Atomic buffer swap — fetch secondary ride coords + offer ID
                _sec_dlat = _sec_dlng = _sec_dh3 = _sec_offer_id = None
                _sec = get_next_stacked_offer(cur, driver_id)
                if _sec:
                    _sec_offer_id = str(_sec["decision_log_id"])
                    _sec_dlat = _sec["dropoff_lat"]
                    _sec_dlng = _sec["dropoff_lng"]
                    _sec_dh3  = _sec["dropoff_h3"]
                    logging.info(f"[S17] Atomic buffer swap: secondary offer={_sec_offer_id} dropoff ({_sec_dlat},{_sec_dlng})")
                else:
                    logging.warning(f"[S17] Atomic buffer swap: no secondary offer found")
                DriverStateMachine.transition(driver_id, 'dropoff_confirmed', cur, conn,
                    offer_id=_sec_offer_id,
                    dropoff_lat=_sec_dlat,
                    dropoff_lng=_sec_dlng,
                    dropoff_h3=_sec_dh3,
                )
                logging.warning(f"[S17] STACKED→ENROUTE atomic swap complete — new offer={_sec_offer_id}")
                driverState = "ENROUTE"
                _s = "ENROUTE"
                # Contest Mode (Rev 00558): async polyline fetch for newly-promoted offer.
                # Skip if coords missing or potential_cancellation flagged pre-swap.
                if (_sec_offer_id and _sec_dlat and _sec_dlng
                        and not state_row.get('potential_cancellation')):
                    _refine_executor.submit(
                        _cache_stacked_polyline_async,
                        driver_id, _sec_offer_id, _sec_dlat, _sec_dlng
                    )
                    logging.info(f"[ROUTES_CACHE/STACK] submitted async fetch for offer={_sec_offer_id}")

                # Patch 00562: contest event for watchdog_a STACKED dropoff (primary ride end)
                try:
                    write_nail_contest_event(
                        cur, conn,
                        driver_id=driver_id,
                        state_row=state_row,
                        nail_path="watchdog_a_dropoff_stacked",
                        current_lat=current_lat,
                        current_lng=current_lng,
                        current_speed_mph=speed_mph or 0.0,
                        current_heading=0.0,
                        stopped_seconds=float(stopped_seconds or 0),
                        dist_to_target_m=float(new_error_m or 0),
                        target_lat=state_row.get('dropoff_lat'),
                        target_lng=state_row.get('dropoff_lng'),
                        pickup_lat=state_row.get('pickup_lat'),
                        pickup_lng=state_row.get('pickup_lng'),
                        dropoff_lat=state_row.get('dropoff_lat'),
                        dropoff_lng=state_row.get('dropoff_lng'),
                    )
                except Exception as _ce:
                    logging.warning(f"[CONTEST] hook3 non-fatal: {_ce}")
            else:
                if _s == 'IN_TRIP':
                    DriverStateMachine.transition(driver_id, 'approaching_dropoff', cur, conn)
                    _s = 'REFINE_DROPOFF'
                    logging.warning(f"S31 DROPOFF_NAIL: IN_TRIP→REFINE_DROPOFF (arm+nail same heartbeat)")
                DriverStateMachine.transition(driver_id, 'dropoff_confirmed', cur, conn,
                    clear_coords=True,
                )
                logging.warning(f"S31 DROPOFF_NAIL: REFINE_DROPOFF→UNCOMMITTED at {new_error_m or 0:.0f}m")
                driverState = "UNCOMMITTED"

                # Patch 00562: contest event for watchdog_a non-STACKED dropoff nail
                try:
                    write_nail_contest_event(
                        cur, conn,
                        driver_id=driver_id,
                        state_row=state_row,
                        nail_path="watchdog_a_dropoff",
                        current_lat=current_lat,
                        current_lng=current_lng,
                        current_speed_mph=speed_mph or 0.0,
                        current_heading=0.0,
                        stopped_seconds=float(stopped_seconds or 0),
                        dist_to_target_m=float(new_error_m or 0),
                        target_lat=state_row.get('dropoff_lat'),
                        target_lng=state_row.get('dropoff_lng'),
                        pickup_lat=state_row.get('pickup_lat'),
                        pickup_lng=state_row.get('pickup_lng'),
                        dropoff_lat=state_row.get('dropoff_lat'),
                        dropoff_lng=state_row.get('dropoff_lng'),
                    )
                except Exception as _ce:
                    logging.warning(f"[CONTEST] hook2 non-fatal: {_ce}")

        elif verdict == 'SET_CANDIDATE':
            # Watchdog B: record micro-stop position
            _cand_lat, _cand_lng = _extra
            cur.execute(
                """
                UPDATE app_private.driver_trip_state
                SET candidate_lat = %s, candidate_lng = %s, candidate_at = NOW()
                WHERE driver_id = %s
                """,
                (_cand_lat, _cand_lng, driver_id)
            )
            conn.commit()
            logging.info(f"[WATCHDOG_B] Candidate persisted to DB at {_cand_lat:.5f},{_cand_lng:.5f}")

        elif verdict == 'DROPOFF_NAIL_B':
            # Watchdog B retroactive: nail at candidate coords, enforce linear path
            nail_lat, nail_lng = _extra
            write_nailed_position(cur, driver_id, 'dropoff', nail_lat, nail_lng, new_error_m)
            # ── Box 4: Close the audit loop ───────────────────────────
            _offer_id = state_row.get('current_offer_id')
            # For STACKED rides, current_offer_id points to the secondary offer.
            # Find the primary offer (pickup confirmed, dropoff not yet confirmed).
            _audit_id = _offer_id
            if _s == 'STACKED':
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
                        _audit_id = _primary['decision_log_id']
                        logging.info(f"[S17] Audit lock (B): primary offer={_audit_id} (state had {_offer_id})")
                except Exception as _al_err:
                    logging.warning(f"[S17] Audit lock (B) failed, falling back to {_offer_id}: {_al_err}")
            if _audit_id:
                try:
                    cur.execute("SELECT app_private.safe_h3(%s,%s)::text AS h3",
                                (nail_lat, nail_lng))
                    _h3r = cur.fetchone()
                    _actual_h3 = _h3r['h3'] if _h3r else None

                    # SB3 trip-close hook (Patch 00566a Step 12, watchdog_b path):
                    # Propagate refined_dropoff_* and refinement_source from
                    # driver_trip_state into this trip's offer_history row.
                    cur.execute("""
                        SELECT refined_dropoff_lat, refined_dropoff_lng, refinement_source
                        FROM app_private.driver_trip_state
                        WHERE driver_id = %s
                    """, (driver_id,))
                    _dts_row = cur.fetchone() or {}
                    _refined_lat = _dts_row.get("refined_dropoff_lat")
                    _refined_lng = _dts_row.get("refined_dropoff_lng")
                    _refinement_source = _dts_row.get("refinement_source")

                    cur.execute("""
                        UPDATE app_private.offer_history
                        SET actual_dropoff_lat     = %s,
                            actual_dropoff_lng     = %s,
                            actual_dropoff_h3      = %s,
                            actual_dropoff_at      = NOW(),
                            dropoff_error_m        = %s,
                            dropoff_classification = %s,
                            refined_dropoff_lat    = %s,
                            refined_dropoff_lng    = %s,
                            refinement_source      = %s
                        WHERE decision_log_id = %s::integer
                    """, (nail_lat, nail_lng, _actual_h3,
                          new_error_m, 'watchdog_b',
                          _refined_lat, _refined_lng, _refinement_source,
                          _audit_id))
                    logging.info(
                        f"[HEARTBEAT] offer_history dropoff updated (watchdog_b) offer={_audit_id} "
                        f"refined={'yes' if _refined_lat is not None else 'no'} "
                        f"source={_refinement_source}"
                    )
                except Exception as _oh_err:
                    logging.warning(f"[HEARTBEAT] offer_history dropoff update failed: {_oh_err}")
            if _s == 'STACKED':
                # Atomic buffer swap — current_offer_id IS already the secondary offer
                _sec_dlat = _sec_dlng = _sec_dh3 = None
                cur.execute("""
                    SELECT oh.dropoff_lat, oh.dropoff_lng, oh.dropoff_h3
                    FROM app_private.offer_history oh
                    WHERE oh.decision_log_id = %s::integer
                """, (_offer_id,))
                _sec = cur.fetchone()
                if _sec:
                    _sec_dlat = _sec["dropoff_lat"]
                    _sec_dlng = _sec["dropoff_lng"]
                    _sec_dh3  = _sec["dropoff_h3"]
                    logging.info(f"[S17] Atomic buffer swap (B): offer={_offer_id} dropoff ({_sec_dlat},{_sec_dlng})")
                else:
                    logging.warning(f"[S17] Atomic buffer swap (B): no coords for offer={_offer_id}")
                DriverStateMachine.transition(driver_id, 'dropoff_confirmed', cur, conn,
                    dropoff_lat=_sec_dlat,
                    dropoff_lng=_sec_dlng,
                    dropoff_h3=_sec_dh3,
                )
                logging.warning(f"[S17] STACKED→ENROUTE atomic swap complete (watchdog_b) — offer={_offer_id}")
                driverState = "ENROUTE"
                _s = "ENROUTE"
                # Contest Mode (Rev 00558): async polyline fetch for newly-promoted offer.
                # Skip if coords missing or potential_cancellation flagged pre-swap.
                if (_offer_id and _sec_dlat and _sec_dlng
                        and not state_row.get('potential_cancellation')):
                    _refine_executor.submit(
                        _cache_stacked_polyline_async,
                        driver_id, _offer_id, _sec_dlat, _sec_dlng
                    )
                    logging.info(f"[ROUTES_CACHE/STACK] submitted async fetch (B) for offer={_offer_id}")

                # Patch 00562: contest event for watchdog_b STACKED dropoff (primary ride end)
                try:
                    write_nail_contest_event(
                        cur, conn,
                        driver_id=driver_id,
                        state_row=state_row,
                        nail_path="watchdog_b_dropoff_stacked",
                        current_lat=current_lat,
                        current_lng=current_lng,
                        current_speed_mph=speed_mph or 0.0,
                        current_heading=0.0,
                        stopped_seconds=float(stopped_seconds or 0),
                        dist_to_target_m=float(new_error_m or 0),
                        target_lat=state_row.get('dropoff_lat'),
                        target_lng=state_row.get('dropoff_lng'),
                        pickup_lat=state_row.get('pickup_lat'),
                        pickup_lng=state_row.get('pickup_lng'),
                        dropoff_lat=state_row.get('dropoff_lat'),
                        dropoff_lng=state_row.get('dropoff_lng'),
                    )
                except Exception as _ce:
                    logging.warning(f"[CONTEST] hook5 non-fatal: {_ce}")
            else:
                if _s == 'IN_TRIP':
                    DriverStateMachine.transition(driver_id, 'approaching_dropoff', cur, conn)
                    _s = 'REFINE_DROPOFF'
                    logging.warning(f"S31 DROPOFF_NAIL_B: IN_TRIP→REFINE_DROPOFF (arm+nail same heartbeat)")
                DriverStateMachine.transition(driver_id, 'dropoff_confirmed', cur, conn,
                    clear_coords=True,
                )
                logging.warning(f"S31 DROPOFF_NAIL_B (retroactive): REFINE_DROPOFF→UNCOMMITTED at {new_error_m or 0:.0f}m")
                driverState = "UNCOMMITTED"

                # Patch 00562: contest event for watchdog_b non-STACKED dropoff nail
                try:
                    write_nail_contest_event(
                        cur, conn,
                        driver_id=driver_id,
                        state_row=state_row,
                        nail_path="watchdog_b_dropoff",
                        current_lat=current_lat,
                        current_lng=current_lng,
                        current_speed_mph=speed_mph or 0.0,
                        current_heading=0.0,
                        stopped_seconds=float(stopped_seconds or 0),
                        dist_to_target_m=float(new_error_m or 0),
                        target_lat=state_row.get('dropoff_lat'),
                        target_lng=state_row.get('dropoff_lng'),
                        pickup_lat=state_row.get('pickup_lat'),
                        pickup_lng=state_row.get('pickup_lng'),
                        dropoff_lat=state_row.get('dropoff_lat'),
                        dropoff_lng=state_row.get('dropoff_lng'),
                    )
                except Exception as _ce:
                    logging.warning(f"[CONTEST] hook4 non-fatal: {_ce}")

        elif verdict == 'REFINE_PICKUP':
            write_nailed_position(cur, driver_id, 'pickup',
                                  current_lat, current_lng, new_error_m)
            logging.info(f"[HEARTBEAT] REFINE_PICKUP: improved to {new_error_m:.0f}m")
            # driverState already set to real state above

        elif verdict == 'REFINE_DROPOFF':
            write_nailed_position(cur, driver_id, 'dropoff',
                                  current_lat, current_lng, new_error_m)
            if _s == 'IN_TRIP':
                DriverStateMachine.transition(driver_id, 'approaching_dropoff', cur, conn)
                driverState = 'REFINE_DROPOFF'
                logging.warning(
                    f"[REFINE_DROPOFF ARMED] "
                    f"dropoff_lat={state_row.get('dropoff_lat')} "
                    f"dropoff_lng={state_row.get('dropoff_lng')} "
                    f"dropoff_h3={state_row.get('dropoff_h3')} "
                    f"current_offer_id={state_row.get('current_offer_id')} "
                    f"driver_lat={current_lat} driver_lng={current_lng} "
                    f"dist_m={new_error_m:.0f}"
                )
            else:
                logging.info(f"[HEARTBEAT] REFINE_DROPOFF: improved to {new_error_m:.0f}m")

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
                    f"[S11] Near declined offer pickup — arming ENROUTE "
                    f"(dist={nearby['dist_miles']:.2f}mi, offer={nearby['offer_id']})"
                )
                DriverStateMachine.transition(driver_id, 'offer_accepted', cur, conn,
                    offer_id=str(nearby['offer_id']),
                    pickup_lat=nearby['pickup_lat'],
                    pickup_lng=nearby['pickup_lng'],
                    pickup_h3=nearby['pickup_h3'],
                    dropoff_lat=nearby['dropoff_lat'],
                    dropoff_lng=nearby['dropoff_lng'],
                    dropoff_h3=nearby['dropoff_h3'],
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

                # Patch 00562: contest event for S12 gps_convergence override
                try:
                    write_nail_contest_event(
                        cur, conn,
                        driver_id=driver_id,
                        state_row=state_row,
                        nail_path="gps_convergence_pickup_s12",
                        current_lat=current_lat,
                        current_lng=current_lng,
                        current_speed_mph=speed_mph or 0.0,
                        current_heading=0.0,
                        stopped_seconds=float(stopped_seconds or 0),
                        dist_to_target_m=float(nearby.get('dist_miles', 0) * 1609.34),
                        target_lat=nearby['pickup_lat'],
                        target_lng=nearby['pickup_lng'],
                        pickup_lat=nearby['pickup_lat'],
                        pickup_lng=nearby['pickup_lng'],
                        dropoff_lat=nearby['dropoff_lat'],
                        dropoff_lng=nearby['dropoff_lng'],
                    )
                except Exception as _ce:
                    logging.warning(f"[CONTEST] hook6 non-fatal: {_ce}")

        conn.commit()
        return jsonify({"status": "ok", "driverState": driverState}), 200

    except Exception as e:
        logging.exception("[HEARTBEAT] Unhandled error")
        return jsonify({"error": str(e)}), 500
    finally:
        if 'conn' in locals():
            conn.close()
