# backend/driver_heartbeat.py
# Android AutoNailItManager pushes real-time metrics every 5 seconds
# POST /api/v1/driver/heartbeat
# Stored in driver_trip_state — surfaced via /api/v1/driver/status

import json
import logging
import datetime

from flask import Blueprint, request, jsonify
from psycopg2.extras import RealDictCursor

from db import get_db
from utils import verify_and_get_user_id, require_firebase_auth
from nail_it_core import (check_convergence, write_nailed_position,
                          classify, process_heartbeat, clear_buffer,
                          get_next_stacked_offer)
from state_machine import DriverStateMachine
from nail_it_core import write_nail_contest_event
from where_am_i import WhereAmI
from pudo_planner import PudoPlanner
from pudo_types import DriverStateSnapshot, Offer, TargetSpec
from bead_on_wire import classify_address

driver_heartbeat_bp = Blueprint('driver_heartbeat', __name__)

# ─── Sprint 1 Trilogy bootstrap (PUDO-FIRST DIRECTIVE 2026-04-29) ─────────────
#
# PudoPlanner holds per-driver temporal state in self._state_store and that
# state must persist across heartbeats for _is_stable_match to accumulate
# N_HEARTBEATS_TO_FIRE counts. Therefore: module-level singleton.
#
# WhereAmI takes a Flask request-scoped psycopg cursor in __init__, so it
# is constructed PER-REQUEST inside post_heartbeat(), not here.
#
# classify_address (from bead_on_wire) is the canonical address parser. It
# returns 5 buckets: intersection, street_number, single_road, poi, garbage.
# garbage → no Offer constructed → WAI runs in offer-less mode (ghost cache
# and unknown-cluster paths only, per where_am_i.py:903 evaluate()).

_pudo_planner_singleton = PudoPlanner()


def _bucket_to_target_spec(address_text, lat, lng):
    """Classify an address string and convert to TargetSpec. None on garbage.

    Mapping (classify_address bucket → TargetSpec.address_class):
      intersection   → intersection      (named_roads = (road_a, road_b))
      street_number  → number_on_street  (named_roads = (road,))
      single_road    → single_road       (named_roads = (road,))
      poi            → poi               (named_roads = ())
      garbage        → None              (caller must pass current_offer=None)

    apartment_complex is a documented TargetSpec.address_class but
    classify_address never produces it — Sprint 2 backlog item B-NEW-7.

    Returns None if address_text is empty, lat/lng are None, or bucket is
    'garbage'. Coords are required because TargetSpec.lat/lng are not Optional.
    """
    if not address_text or lat is None or lng is None:
        return None
    classification = classify_address(address_text)
    bucket = classification.get("bucket")
    parts = classification.get("parts", {})

    if bucket == "intersection":
        ra = parts.get("road_a", "")
        rb = parts.get("road_b", "")
        return TargetSpec(
            lat=float(lat), lng=float(lng),
            address_class="intersection",
            named_roads=(ra, rb),
        )
    if bucket == "street_number":
        road = parts.get("road", "")
        return TargetSpec(
            lat=float(lat), lng=float(lng),
            address_class="number_on_street",
            named_roads=(road,),
        )
    if bucket == "single_road":
        road = parts.get("road", "")
        return TargetSpec(
            lat=float(lat), lng=float(lng),
            address_class="single_road",
            named_roads=(road,),
        )
    if bucket == "poi":
        return TargetSpec(
            lat=float(lat), lng=float(lng),
            address_class="poi",
            named_roads=(),
        )
    # bucket == "garbage" or unknown
    return None


def _assemble_offer(state_row):
    """Build an Offer from the heartbeat handler's state_row dict, or None.

    Returns None when:
      - current_offer_id is missing (UNCOMMITTED state)
      - either pickup or dropoff TargetSpec is None (garbage classification
        on either address — WAI runs in offer-less mode)
      - state_updated_at (the accepted_at proxy) is None

    state_updated_at is used as Offer.accepted_at. Per Phase F TODO in
    pudo_types.py:90, the canonical source is offer_history.accepted_at;
    state_updated_at is the closest available proxy in driver_trip_state.
    """
    offer_id = state_row.get("current_offer_id")
    if not offer_id:
        return None

    accepted_at = state_row.get("state_updated_at")
    if accepted_at is None:
        return None

    pickup_spec = _bucket_to_target_spec(
        state_row.get("pickup_address"),
        state_row.get("pickup_lat"),
        state_row.get("pickup_lng"),
    )
    dropoff_spec = _bucket_to_target_spec(
        state_row.get("dropoff_address"),
        state_row.get("dropoff_lat"),
        state_row.get("dropoff_lng"),
    )
    if pickup_spec is None or dropoff_spec is None:
        return None

    return Offer(
        offer_id=str(offer_id),
        accepted_at=accepted_at,
        pickup=pickup_spec,
        dropoff=dropoff_spec,
        secondary_dropoff=None,  # B-strict-pragmatic: stacks not assembled in 6c
    )


def _build_snapshot(state_row):
    """Build a DriverStateSnapshot from the heartbeat handler's state_row.

    Sprint 1 scope: primary_offer_id == current_offer_id. STACKED disambiguation
    (where they differ) is a known gap per pudo_planner.consume() docstring;
    Sprint 2 closes it via Phase E Step 4 Section B.
    """
    offer_id = state_row.get("current_offer_id")
    offer_id_str = str(offer_id) if offer_id else None
    return DriverStateSnapshot(
        state=state_row.get("state"),
        current_offer_id=offer_id_str,
        primary_offer_id=offer_id_str,
        pickup_lat=state_row.get("pickup_lat"),
        pickup_lng=state_row.get("pickup_lng"),
        dropoff_lat=state_row.get("dropoff_lat"),
        dropoff_lng=state_row.get("dropoff_lng"),
        secondary_pickup_lat=None,
        secondary_pickup_lng=None,
        secondary_dropoff_lat=None,
        secondary_dropoff_lng=None,
    )


# ─── End Sprint 1 Trilogy bootstrap ───────────────────────────────────────────


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

        # ── SPRINT 1 TRILOGY PIPE (B-strict-pragmatic per PUDO-FIRST 2026-04-29) ──
        # Replaces the legacy check_convergence verdict ladder. WAI evaluates,
        # PudoPlanner decides, dispatch executes per PlannerDecision.action.
        # Truth-table INSERT happens unconditionally (success or exception) so
        # the truth table never has gaps for diagnosed heartbeats.
        #
        # S11 and S12 handlers below remain — they're the implicit-cancel
        # recovery layer the Trilogy explicitly does not yet handle (per
        # pudo_planner.consume() docstring KNOWN GAPS).

        wai_result = None
        decision = None
        dispatch_executed = False
        dispatch_error_msg = None
        new_state = state_row.get('state')   # for downstream S11/S12 path

        try:
            # ── DIAGNOSE ──────────────────────────────────────────────────
            current_offer = _assemble_offer(state_row)
            wai = WhereAmI(cur)
            wai_result = wai.evaluate(driver_id, current_offer, state_row.get('state'))

            # ── PLAN ──────────────────────────────────────────────────────
            snapshot = _build_snapshot(state_row)
            decision = _pudo_planner_singleton.consume(
                driver_id, wai_result,
                driver_state_snapshot=snapshot,
            )

            # ── EXECUTE — dispatch all 12 PlannerDecision.action literals ──
            action = decision.action
            if action in ('noop', 'noop_same_pudo_no_revisit', 'arm_candidate', 'cancel_candidate'):
                # No state writes, no nailed-position writes — pure observation
                pass

            elif action == 'fire_pickup':
                write_nailed_position(cur, driver_id, 'pickup',
                                      decision.corrected_lat, decision.corrected_lng, 0)
                DriverStateMachine.transition(
                    driver_id, 'gps_convergence', cur, conn,
                    lat=current_lat, lng=current_lng,
                    cumulative_miles=cumulative_miles,
                )
                dispatch_executed = True

            elif action == 'fire_dropoff':
                write_nailed_position(cur, driver_id, 'dropoff',
                                      decision.corrected_lat, decision.corrected_lng, 0)
                DriverStateMachine.transition(
                    driver_id, 'dropoff_confirmed', cur, conn,
                    lat=current_lat, lng=current_lng,
                    cumulative_miles=cumulative_miles,
                )
                dispatch_executed = True

            elif action == 'fire_retroactive':
                # Retroactive fire — Planner already determined the missed PUDO
                # via _is_long_stop_then_departure. Use corrected coords.
                pudo_type = (wai_result.pudo_type if wai_result else None) or 'pickup'
                trigger = 'gps_convergence' if pudo_type == 'pickup' else 'dropoff_confirmed'
                write_nailed_position(cur, driver_id, pudo_type,
                                      decision.corrected_lat, decision.corrected_lng, 0)
                DriverStateMachine.transition(
                    driver_id, trigger, cur, conn,
                    lat=current_lat, lng=current_lng,
                    cumulative_miles=cumulative_miles,
                )
                dispatch_executed = True

            elif action == 'fire_stacked_swap':
                # Primary closes, secondary promotes to active. Per CANONICAL
                # § VI: current_offer_id advances; transition trigger is
                # gps_convergence (matches existing S12 semantics).
                DriverStateMachine.transition(
                    driver_id, 'gps_convergence', cur, conn,
                    lat=current_lat, lng=current_lng,
                    cumulative_miles=cumulative_miles,
                )
                dispatch_executed = True

            elif action == 'fire_stacked_revert':
                # Symmetric to swap — Uber re-awarded the original primary.
                # Same trigger; state machine handles the live-pointer math.
                DriverStateMachine.transition(
                    driver_id, 'gps_convergence', cur, conn,
                    lat=current_lat, lng=current_lng,
                    cumulative_miles=cumulative_miles,
                )
                dispatch_executed = True

            elif action == 'cache_ghost':
                # at_unknown_pudo with insert payload — write a suspected_pudos
                # row. Sprint 1: log only, do not write. Phase E Q12 ratification
                # locked ghost INSERTs to PLAN/EXECUTE; we have a payload but
                # the EXECUTE pathway for it isn't built yet. Truth table
                # captures the intent; Sprint 2 wires the actual INSERT.
                pass

            elif action in ('reconcile_missed_pickup', 'reconcile_missed_dropoff'):
                # Honest reconciliation of offer_history. Sprint 1: log only;
                # actual UPDATE on offer_history is Sprint 2 work. Coordinates
                # are NOT synthesized per pudo_types.py:285 contract.
                pass

            else:
                logging.warning(f"[TRILOGY] unrecognized PlannerDecision.action: {action!r}")

            logging.info(
                f"[TRILOGY] state={state_row.get('state')} "
                f"wai={wai_result.status if wai_result else 'n/a'} "
                f"action={action} executed={dispatch_executed}"
            )

        except Exception as e:
            dispatch_error_msg = str(e)
            logging.exception(f"[TRILOGY] perception/plan failure: {e}")

        # ── LOG: truth-table INSERT (always, success or exception) ────────
        try:
            cluster = wai_result.cluster if (wai_result and wai_result.cluster) else None
            cur.execute(
                """
                INSERT INTO app_private.pudo_decision_context (
                    driver_id, state_at_eval, current_offer_id, primary_offer_id,
                    lat, lng, speed_mph, heading, gps_accuracy_m, gps_age_s,
                    wai_status, wai_pudo_type, wai_offer_id, wai_target_address,
                    wai_confidence, wai_reason,
                    wai_on_target_road, wai_current_road,
                    wai_off_wire_duration_s, wai_cluster_revisit,
                    cluster_lat, cluster_lng, cluster_size, cluster_duration_s,
                    planner_action, planner_target_state,
                    planner_corrected_lat, planner_corrected_lng, planner_reason,
                    dispatch_executed, dispatch_error
                ) VALUES (
                    %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s,
                    %s, %s,
                    %s, %s,
                    %s, %s, %s, %s,
                    %s, %s,
                    %s, %s, %s,
                    %s, %s
                )
                """,
                (
                    driver_id,
                    state_row.get('state'),
                    str(state_row.get('current_offer_id')) if state_row.get('current_offer_id') else None,
                    str(state_row.get('current_offer_id')) if state_row.get('current_offer_id') else None,
                    current_lat, current_lng, speed_mph, heading,
                    gps_accuracy_m, body.get('gpsAgeSec'),
                    wai_result.status if wai_result else None,
                    wai_result.pudo_type if wai_result else None,
                    wai_result.offer_id if wai_result else None,
                    wai_result.target_address if wai_result else None,
                    wai_result.confidence if wai_result else None,
                    wai_result.reason if wai_result else None,
                    wai_result.on_target_road if wai_result else None,
                    wai_result.current_road if wai_result else None,
                    wai_result.off_wire_duration_s if wai_result else None,
                    wai_result.cluster_revisit if wai_result else None,
                    cluster.median_lat if cluster else None,
                    cluster.median_lng if cluster else None,
                    cluster.n if cluster else None,
                    cluster.duration_s if cluster else None,
                    decision.action if decision else None,
                    decision.target_state if decision else None,
                    decision.corrected_lat if decision else None,
                    decision.corrected_lng if decision else None,
                    decision.reason if decision else None,
                    dispatch_executed,
                    dispatch_error_msg,
                ),
            )
            conn.commit()
        except Exception as log_err:
            logging.exception(f"[TRILOGY] truth-table INSERT failed: {log_err}")
            try:
                conn.rollback()
            except Exception:
                pass

        # ── End Trilogy pipe ──────────────────────────────────────────────

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
                    cumulative_miles=cumulative_miles,
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
                    cumulative_miles=cumulative_miles,
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
