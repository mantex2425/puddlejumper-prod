# backend/driver_heartbeat.py
#
# Cut B3 — Simplified Architecture heartbeat handler.
#
# Pipeline:
#
#   LOAD       Read current_offer_id + state from driver_trip_state.
#              Project current_offer_id into a synthetic 1-offer queue
#              (Option α; per CANONICAL_RULES Section VI's "1-bit memory").
#
#   HEARTBEAT  UPDATE driver_trip_state.heartbeat (JSONB) + heartbeat_at.
#              Preserves the API contract for /api/v1/driver/status.
#
#   DIAGNOSE   wai.evaluate_with_diagnostics(driver_id, queue)
#                -> (matches: list[WAIMatch], diagnostics: DiagnosticContext)
#              Per §3 step 8, diagnostics.cluster is populated even when no
#              match is produced — this is the data-loss-bug fix.
#
#   DECIDE     dispatch(matches, current_offer_id, queue_offer_ids)
#                -> list[Action]
#              Pure function; implements §4 Cases A–G and §5 disambiguation.
#
#   EXECUTE    For each Action, _execute_action() applies the side effect:
#                FirePickup        -> write_nailed_position + UPDATE current_offer_id
#                FireDropoff       -> write_nailed_position + UPDATE current_offer_id = NULL
#                Log* actions      -> log only, no state change
#
#   LOG        INSERT pudo_decision_context (§3 step 8 invariant: cluster
#              data logged from DiagnosticContext.cluster regardless of
#              match outcome).
#
# State column policy (Option IV, ratified Sprint A): the new handler
# never writes driver_trip_state.state. Per enforce_state_transition()
# branch 2, UPDATEs that don't change state pass without GUC requirement.
# The state column becomes a stale forensic surface read by the
# pudo_decision_context.state_at_eval INSERT and the /driver/status
# endpoint. Schema demolition deferred to a future cut.

import json
import datetime
import logging

from flask import Blueprint, request, jsonify
from psycopg2.extras import RealDictCursor

from db import get_db
from utils import verify_and_get_user_id, require_firebase_auth
from nail_it_core import write_nailed_position
from where_am_i import WhereAmI
from dispatch import (
    dispatch,
    FirePickup, FireDropoff,
    LogNoMatch, LogPickupRematch, LogAmbiguousMatch,
)
from pudo_types import Offer
from bead_on_wire import classify_address

driver_heartbeat_bp = Blueprint('driver_heartbeat', __name__)

log = logging.getLogger(__name__)


# =============================================================================
# Helper: classify an address+coords into a TargetSpec
# =============================================================================

def _bucket_to_target_spec(address_text, lat, lng):
    """Classify an address string and convert to TargetSpec. None on garbage.

    classify_address (from bead_on_wire) is the canonical address parser;
    it returns 5 buckets: intersection, street_number, single_road, poi,
    garbage. Garbage classification means we cannot build a TargetSpec, so
    the offer is unevaluatable and the queue is empty for this heartbeat.
    """
    if not address_text or lat is None or lng is None:
        return None
    try:
        spec = classify_address(address_text, float(lat), float(lng))
    except Exception as e:
        log.warning("[heartbeat] classify_address failed for %r: %s",
                    address_text, e)
        return None
    if spec is None or getattr(spec, 'address_class', None) == 'garbage':
        return None
    return spec


# =============================================================================
# LOAD: project driver_trip_state into the §3 Map-Reduce queue contract
# =============================================================================

def _project_queue(driver_id, cur):
    """Project driver_trip_state into a synthetic 1-offer queue.

    Per CANONICAL_RULES Section VI, current_offer_id is the system's only
    memory of active work. The §3 queue is the dispatcher-side projection
    of that 1-bit memory: empty list when no active ride, one-element
    list when a ride is active.

    Returns: (queue: list[Offer],
              current_offer_id: Optional[str],
              state_at_eval: str)

    state_at_eval is read here once (forensic only) and threaded to
    _log_decision_context. The new handler never writes state.

    The dispatcher's §5.2 hot-swap branch is dormant under this projection
    (it requires len(queue) == 2). It remains tested and ready; activation
    awaits multi-offer infrastructure (separate ratification cycle).
    """
    cur.execute("""
        SELECT current_offer_id, state
        FROM app_private.driver_trip_state
        WHERE driver_id = %s
    """, (driver_id,))
    row = cur.fetchone()
    if not row:
        # Fresh driver, no row yet — no active offer, default state vocabulary
        # for the forensic INSERT.
        return [], None, 'UNCOMMITTED'

    state_at_eval = row['state']
    current_offer_id_val = row['current_offer_id']
    if not current_offer_id_val:
        return [], None, state_at_eval

    current_offer_id = str(current_offer_id_val)

    # Hydrate the one active offer from offer_history.
    cur.execute("""
        SELECT id, pickup_address, dropoff_address,
               pickup_lat, pickup_lng,
               dropoff_lat, dropoff_lng,
               accepted_at
        FROM app_private.offer_history
        WHERE id = %s
    """, (int(current_offer_id),))
    o = cur.fetchone()
    if not o:
        # Memory points at a phantom offer — log as data corruption,
        # treat as empty queue (dispatch will Case A on next eval).
        log.error("[heartbeat] current_offer_id=%s not in offer_history",
                  current_offer_id)
        return [], current_offer_id, state_at_eval

    pickup_spec = _bucket_to_target_spec(
        o['pickup_address'], o['pickup_lat'], o['pickup_lng'])
    dropoff_spec = _bucket_to_target_spec(
        o['dropoff_address'], o['dropoff_lat'], o['dropoff_lng'])
    if pickup_spec is None or dropoff_spec is None:
        log.warning(
            "[heartbeat] offer %s has unbuildable geocode "
            "(pickup_ok=%s dropoff_ok=%s) — queue empty for this heartbeat",
            current_offer_id, pickup_spec is not None, dropoff_spec is not None,
        )
        return [], current_offer_id, state_at_eval

    accepted_at = o['accepted_at']
    if accepted_at is None:
        # offer_history has no accepted_at (rare; usually NULL only for
        # very old rows). WAI's Memory Eye uses min(accepted_at) as anchor;
        # missing anchor disables cluster_revisit but doesn't break eval.
        log.warning("[heartbeat] offer %s has NULL accepted_at",
                    current_offer_id)

    offer = Offer(
        offer_id=str(o['id']),
        accepted_at=accepted_at,
        pickup=pickup_spec,
        dropoff=dropoff_spec,
    )
    return [offer], current_offer_id, state_at_eval


# =============================================================================
# EXECUTE: map a dispatch Action to its DB side effect
# =============================================================================

def _execute_action(action, cur, conn, driver_id, cluster):
    """Map a dispatch Action to its DB side effect.

    Per dispatch.py contract:
      FirePickup        -> write_nailed_position(pickup) + UPDATE current_offer_id
      FireDropoff       -> write_nailed_position(dropoff) + UPDATE current_offer_id = NULL
      LogNoMatch        -> log INFO only
      LogPickupRematch  -> log DEBUG only
      LogAmbiguousMatch -> log WARNING only

    cluster is diagnostics.cluster from WAI; centroid is the canonical
    PUDO position under the simplified architecture (no "corrected"
    coordinate step exists in the new flow).

    Returns: (executed: bool, error: Optional[str])
      executed is True when a state-changing action ran (FirePickup,
      FireDropoff). Used to populate pudo_decision_context.dispatch_executed.
      error is set if the action raised; the orchestrator halts the chain
      on first error so the LOG step still records what happened.
    """
    cluster_lat = cluster.median_lat if cluster else None
    cluster_lng = cluster.median_lng if cluster else None

    try:
        if isinstance(action, FirePickup):
            if cluster_lat is None or cluster_lng is None:
                # Should not happen — dispatch only emits FirePickup when
                # WAI matched, which requires a cluster. Defensive guard.
                return False, "fire_pickup_without_cluster"
            write_nailed_position(cur, driver_id, 'pickup',
                                  cluster_lat, cluster_lng, 0)
            cur.execute("""
                UPDATE app_private.driver_trip_state
                SET current_offer_id = %s
                WHERE driver_id = %s
            """, (action.offer_id, driver_id))
            log.info("[heartbeat] FirePickup offer=%s", action.offer_id)
            return True, None

        if isinstance(action, FireDropoff):
            if cluster_lat is None or cluster_lng is None:
                return False, "fire_dropoff_without_cluster"
            write_nailed_position(cur, driver_id, 'dropoff',
                                  cluster_lat, cluster_lng, 0)
            cur.execute("""
                UPDATE app_private.driver_trip_state
                SET current_offer_id = NULL
                WHERE driver_id = %s
            """, (driver_id,))
            sev_map = {None: logging.INFO,
                       "canceled": logging.INFO,
                       "pickup_missed": logging.WARNING}
            log.log(sev_map.get(action.outcome, logging.INFO),
                    "[heartbeat] FireDropoff offer=%s outcome=%s",
                    action.offer_id, action.outcome)
            return True, None

        if isinstance(action, LogNoMatch):
            log.info("[heartbeat] no_match")
            return False, None

        if isinstance(action, LogPickupRematch):
            log.debug("[heartbeat] pickup_rematch offer=%s", action.offer_id)
            return False, None

        if isinstance(action, LogAmbiguousMatch):
            log.warning(
                "[heartbeat] ambiguous_match reason=%s candidates=%s",
                action.reason,
                [(c.offer_id, c.location_type, c.confidence)
                 for c in action.candidates],
            )
            return False, None

        # Total over the Action union; defensive fallthrough.
        return False, "unknown_action_type:" + type(action).__name__

    except Exception as e:
        log.exception("[heartbeat] action execution failed: %r", action)
        return False, "{}:{}".format(type(e).__name__, e)


# =============================================================================
# LOG: pudo_decision_context INSERT (§3 step 8 — always fires)
# =============================================================================

def _log_decision_context(
    cur, driver_id, body,
    current_lat, current_lng, speed_mph, gps_accuracy_m,
    current_offer_id, state_at_eval,
    diagnostics, matches, actions,
    dispatch_executed, dispatch_error_msg,
):
    """Insert pudo_decision_context row from DiagnosticContext + dispatch result.

    §3 step 8 invariant: cluster_* columns are populated from
    diagnostics.cluster regardless of match outcome. Even when matches is
    empty, even when WAI returned no cluster, this INSERT runs and records
    what we saw.

    Column policy (Cut B3): kept columns are listed below; dropped columns
    (primary_offer_id, wai_status, wai_target_address, wai_reason,
    wai_on_target_road, wai_current_road, wai_off_wire_duration_s,
    planner_target_state, planner_corrected_lat, planner_corrected_lng,
    planner_reason) become vestigial NULL in the table — eligible for
    DROP COLUMN in a future schema-cleanup cut.
    """
    cluster = diagnostics.cluster

    # Project Action list into a forensic-readable string. Multiple actions
    # (Case D, §5.2) join with '+'.
    action_str = "+".join(type(a).__name__ for a in actions) if actions else None

    # Match-side fields: take the highest-confidence match for the legacy
    # single-match columns. Per-target detail is in
    # diagnostics.per_target_outcomes — projection into a separate table or
    # JSONB column is a future cut.
    top_match = matches[0] if matches else None

    cur.execute(
        """
        INSERT INTO app_private.pudo_decision_context (
            driver_id, state_at_eval, current_offer_id,
            lat, lng, speed_mph, heading, gps_accuracy_m, gps_age_s,
            wai_pudo_type, wai_offer_id, wai_confidence,
            wai_cluster_revisit,
            cluster_lat, cluster_lng, cluster_size, cluster_duration_s,
            planner_action,
            dispatch_executed, dispatch_error
        ) VALUES (
            %s, %s, %s,
            %s, %s, %s, %s, %s, %s,
            %s, %s, %s,
            %s,
            %s, %s, %s, %s,
            %s,
            %s, %s
        )
        """,
        (
            driver_id, state_at_eval, current_offer_id,
            current_lat, current_lng, speed_mph,
            body.get('heading'), gps_accuracy_m, body.get('gpsAgeSec'),
            top_match.location_type if top_match else None,
            top_match.offer_id if top_match else None,
            top_match.confidence if top_match else None,
            diagnostics.cluster_revisit,
            cluster.median_lat if cluster else None,
            cluster.median_lng if cluster else None,
            cluster.n if cluster else None,
            cluster.duration_s if cluster else None,
            action_str,
            dispatch_executed,
            dispatch_error_msg,
        ),
    )


# =============================================================================
# Orchestrator: the Quarterback
# =============================================================================

@driver_heartbeat_bp.route('/api/v1/driver/heartbeat', methods=['POST'])
@require_firebase_auth
def post_heartbeat():
    driver_id = verify_and_get_user_id(request)
    body = request.get_json() or {}

    current_lat = body.get('lat')
    current_lng = body.get('lng')
    speed_mph = body.get('speedMph')
    gps_accuracy_m = body.get('gpsAccuracyM')
    cumulative_miles = body.get('cumulativeMiles')

    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    # ── LOAD ─────────────────────────────────────────────────────────
    queue, current_offer_id, state_at_eval = _project_queue(driver_id, cur)
    queue_offer_ids = {o.offer_id for o in queue}

    # ── HEARTBEAT (preserve API contract for /driver/status) ─────────
    # Per Option H1 (ratified Sprint A): keep GPS/motion fields with real
    # data sources; drop state-machine-derived fields (armed, target_type,
    # dist_to_target_m, stopped_seconds, required_stopped_seconds). The
    # /driver/status endpoint uses null-safe .get() and surfaces missing
    # fields as null — honest representation of "this concept is gone."
    cur.execute("""
        UPDATE app_private.driver_trip_state
        SET heartbeat = %s::jsonb,
            heartbeat_at = NOW()
        WHERE driver_id = %s
    """, (json.dumps({
        "lat": current_lat,
        "lng": current_lng,
        "speed_mph": speed_mph,
        "gps_accuracy_m": gps_accuracy_m,
        "cumulative_miles": cumulative_miles,
        "received_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }), driver_id))

    # ── DIAGNOSE ─────────────────────────────────────────────────────
    wai = WhereAmI(cur)
    matches, diagnostics = wai.evaluate_with_diagnostics(driver_id, queue)

    # ── DECIDE ───────────────────────────────────────────────────────
    actions = dispatch(matches, current_offer_id, queue_offer_ids)

    # ── EXECUTE ──────────────────────────────────────────────────────
    cluster = diagnostics.cluster
    dispatch_executed = False
    dispatch_error_msg = None
    for action in actions:
        executed, err = _execute_action(action, cur, conn, driver_id, cluster)
        dispatch_executed = dispatch_executed or executed
        if err:
            dispatch_error_msg = err
            break  # halt on first error; LOG still records the attempt

    # ── LOG ──────────────────────────────────────────────────────────
    try:
        _log_decision_context(
            cur, driver_id, body,
            current_lat, current_lng, speed_mph, gps_accuracy_m,
            current_offer_id, state_at_eval,
            diagnostics, matches, actions,
            dispatch_executed, dispatch_error_msg,
        )
    except Exception as e:
        # LOG failure must not break the heartbeat — the API contract is
        # liveness, not forensic completeness. Surface to logs.
        log.exception("[heartbeat] pudo_decision_context INSERT failed: %s", e)

    conn.commit()
    return jsonify({"ok": True}), 200