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
from pudo_types import Offer, TargetSpec
from bead_on_wire import classify_address

driver_heartbeat_bp = Blueprint('driver_heartbeat', __name__)

log = logging.getLogger(__name__)


# =============================================================================
# Helper: classify an address+coords into a TargetSpec
# =============================================================================

def _bucket_to_target_spec(address_text, lat, lng):
    """Classify an address string and convert to TargetSpec. None on garbage.

    Mirrors the working reference in smoke_test_wai_24h.py:_build_target_spec.

    Pipeline:
      1. classify_address(text) -> {"bucket": str, "parts": dict}
         (single positional arg; coords are NOT passed to the parser)
      2. Switch on bucket; construct TargetSpec with proper address_class
         and named_roads per the §5.1 matching rule.
      3. Return None for "garbage" bucket or any unrecognized result.

    bucket -> address_class mapping (per pudo_types.TargetSpec.address_class
    Literal):
      "intersection"   -> "intersection"     named_roads=(road_a, road_b)
      "street_number"  -> "number_on_street" named_roads=(road,)
      "single_road"    -> "single_road"      named_roads=(road,)
      "poi"            -> "poi"              named_roads=()
      "garbage" or _   -> None
    """
    if not address_text or lat is None or lng is None:
        return None
    try:
        classification = classify_address(address_text)
    except Exception as e:
        log.warning("[heartbeat] classify_address failed for %r: %s",
                    address_text, e)
        return None
    if not classification:
        return None

    bucket = classification.get("bucket")
    parts = classification.get("parts", {})

    if bucket == "intersection":
        return TargetSpec(
            lat=float(lat), lng=float(lng),
            address_class="intersection",
            named_roads=(parts.get("road_a", ""), parts.get("road_b", "")),
        )
    if bucket == "street_number":
        return TargetSpec(
            lat=float(lat), lng=float(lng),
            address_class="number_on_street",
            named_roads=(parts.get("road", ""),),
        )
    if bucket == "single_road":
        return TargetSpec(
            lat=float(lat), lng=float(lng),
            address_class="single_road",
            named_roads=(parts.get("road", ""),),
        )
    if bucket == "poi":
        return TargetSpec(
            lat=float(lat), lng=float(lng),
            address_class="poi",
            named_roads=(),
        )
    # "garbage" or any unrecognized bucket -> unevaluatable
    return None


# =============================================================================
# LOAD: project driver_trip_state into the §3 Map-Reduce queue contract
# =============================================================================

# ─── GC window constants (Andrew's "Houston Tax") ─────────────────────────────
# Window per offer = (pickup_minutes + trip_minutes) * BUFFER, clamped.
# Rationale: real-world rides drift past Uber's estimates due to traffic,
# missed turns, and pickup delays. The buffer keeps WAI's "Peripheral Vision"
# alive for offers the driver might still be working. Clamps protect against
# (a) zero-duration outliers collapsing the window, (b) airport-class outliers
# making old offers immortal.
GC_BUFFER_MULT = 1.5
GC_MIN_MINUTES = 15
GC_MAX_MINUTES = 240
# Fallback when offer_history has NULL minutes (legacy rows or missing data).
# Conservative defaults: 15min pickup + 30min trip = 45min raw, * 1.5 = 67.5min,
# clamped to 67min — long enough to cover most rides without going stale.
GC_NULL_PICKUP_MIN = 15
GC_NULL_TRIP_MIN = 30


def _project_queue(driver_id, cur):
    """Project the driver's Workload Queue per SIMPLIFIED_ARCHITECTURE.md §3 + §6.

    Returns ALL offers the driver has SEEN (any verdict — ACCEPT, DECLINE, etc.)
    that have not been completed (`actual_dropoff_at IS NULL`) and have not been
    garbage-collected (within the per-offer "Houston Tax" window: minutes since
    `created_at` < (pickup_minutes + trip_minutes) * GC_BUFFER_MULT, clamped).

    Why all-seen vs. ACCEPT-only:
      A driver routinely arrives at the pickup of a declined offer (already
      heading that way for a different ride; previous ride canceled and they
      were already mid-stream). WAI must be able to discover that PUDO via
      Case B (FirePickup against a queue containing the seen offer).

    Why per-offer GC windows:
      A 5-minute errand and a 90-minute airport run have very different
      "is this offer still relevant?" timescales. A global cutoff would
      either drop the airport run too early or keep the errand alive
      far past its useful life.

    Returns:
        queue: list[Offer]                  — all live, in-window offers
        current_offer_id: Optional[str]     — `driver_trip_state.current_offer_id`
                                              (still the 1-bit memory of which
                                              offer FirePickup most recently
                                              fired against; threaded to
                                              dispatch for §4 case resolution)
        state_at_eval: str                  — forensic only; threaded to
                                              _log_decision_context
    """
    cur.execute("""
        SELECT current_offer_id, state
        FROM app_private.driver_trip_state
        WHERE driver_id = %s
    """, (driver_id,))
    row = cur.fetchone()
    if row:
        state_at_eval = row['state']
        current_offer_id = (
            str(row['current_offer_id']) if row['current_offer_id'] else None
        )
    else:
        # Fresh driver, no row yet.
        state_at_eval = 'UNCOMMITTED'
        current_offer_id = None

    # Workload Queue: all seen, not-completed, in-window offers.
    #
    # Per-offer GC window (in minutes):
    #   raw_min   = COALESCE(pickup_minutes, GC_NULL_PICKUP_MIN)
    #             + COALESCE(trip_minutes,   GC_NULL_TRIP_MIN)
    #   window_min = LEAST(GREATEST(raw_min * GC_BUFFER_MULT,
    #                               GC_MIN_MINUTES),
    #                      GC_MAX_MINUTES)
    #   live      = (NOW() - created_at) < window_min minutes
    cur.execute("""
        SELECT
            id, pickup_address, dropoff_address,
            pickup_lat, pickup_lng,
            dropoff_lat, dropoff_lng,
            created_at,
            COALESCE(pickup_minutes, %s) + COALESCE(trip_minutes, %s) AS raw_min
        FROM app_private.offer_history
        WHERE decision_log_id IN (
            SELECT id FROM app_private.decision_log WHERE driver_id = %s
        )
          AND actual_dropoff_at IS NULL
          AND created_at + (
                LEAST(
                    GREATEST(
                        (COALESCE(pickup_minutes, %s) + COALESCE(trip_minutes, %s)) * %s,
                        %s
                    ),
                    %s
                ) * INTERVAL '1 minute'
              ) > NOW()
        ORDER BY created_at DESC
    """, (
        GC_NULL_PICKUP_MIN, GC_NULL_TRIP_MIN,    # SELECT raw_min COALESCEs
        driver_id,                                # FK lookup
        GC_NULL_PICKUP_MIN, GC_NULL_TRIP_MIN,    # WHERE coalesces (must duplicate;
                                                  #   PG can't reuse SELECT alias here)
        GC_BUFFER_MULT,
        GC_MIN_MINUTES,
        GC_MAX_MINUTES,
    ))

    queue = []
    for o in cur.fetchall():
        pickup_spec = _bucket_to_target_spec(
            o['pickup_address'], o['pickup_lat'], o['pickup_lng'])
        dropoff_spec = _bucket_to_target_spec(
            o['dropoff_address'], o['dropoff_lat'], o['dropoff_lng'])
        if pickup_spec is None or dropoff_spec is None:
            log.warning(
                "[heartbeat] offer %s has unbuildable geocode "
                "(pickup_ok=%s dropoff_ok=%s) — excluded from queue",
                o['id'], pickup_spec is not None, dropoff_spec is not None,
            )
            continue
        # Offer.accepted_at semantic mapping: offer_history.created_at is the
        # row-creation moment (also the offer-seen moment for declined/other
        # verdicts). For ACCEPT verdicts it's effectively the accepted-at
        # timestamp because the row is inserted at decision time. Single
        # column does double duty for the Memory Eye anchor in
        # where_am_i.py:1080 (min(accepted_at) across queue).
        queue.append(Offer(
            offer_id=str(o['id']),
            accepted_at=o['created_at'],
            pickup=pickup_spec,
            dropoff=dropoff_spec,
        ))

    return queue, current_offer_id, state_at_eval


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

def _voice_for_actions(executed_actions):
    """Map an executed-action list to a single voice utterance, or None.

    Voice is suppressed unless a STATE-CHANGING action ran. This is the
    natural rate limiter: FirePickup / FireDropoff fire once per state
    transition, never per heartbeat. Detection-only actions
    (LogAmbiguousMatch, LogNoMatch, LogPickupRematch) never voice — they
    surface via /driver/status.last_3_dispatch_actions for forensic review.

    Priority handles the implicit-cancel pair: when both
    FireDropoff(outcome="canceled") and FirePickup execute on the same
    heartbeat, the cancel string wins and suppresses the redundant
    "Pickup confirmed".

    Returns None if no executed action warrants a voice utterance.
    """
    # 1. Implicit cancel — paired FireDropoff(canceled) + FirePickup
    for action in executed_actions:
        if isinstance(action, FireDropoff) and action.outcome == "canceled":
            return "Implicit cancel, new ride starting"
    # 2. Pickup missed (Case F)
    for action in executed_actions:
        if isinstance(action, FireDropoff) and action.outcome == "pickup_missed":
            return "Dropoff confirmed, pickup was missed"
    # 3. Normal dropoff
    for action in executed_actions:
        if isinstance(action, FireDropoff) and action.outcome is None:
            return "Dropoff confirmed"
    # 4. Standalone pickup
    for action in executed_actions:
        if isinstance(action, FirePickup):
            return "Pickup confirmed"
    return None


@driver_heartbeat_bp.route('/driver/heartbeat', methods=['POST'])
@require_firebase_auth
def post_heartbeat():
    driver_id = verify_and_get_user_id(request)
    body = request.get_json() or {}

    current_lat = body.get('lat')
    current_lng = body.get('lng')
    speed_mph = body.get('speed_mph')
    gps_accuracy_m = body.get('gps_accuracy_m')
    cumulative_miles = body.get('cumulative_miles')

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

    # ── HEARTBEAT_LOG (flight recorder for cluster detection) ───────
    # cluster_detection.detect_cluster + pivot_context + bead_on_wire
    # all read FROM app_private.heartbeat_log. The Sprint A rewrite
    # dropped Patch 00567's INSERT along with the deprecated state-
    # machine fields (armed, target_type, dist_to_target_m,
    # stopped_seconds) but kept the table's consumers — cluster
    # detection went blind and WAI short-circuited on cluster=None.
    #
    # Sprint A's Option H1 doesn't compute the dropped fields; they
    # stay NULL (schema permits). Same transaction as the UPDATE
    # above — atomic frame. Defensive try/except matches the
    # _log_decision_context pattern: heartbeat_log is forensic, its
    # failure must not break the live heartbeat.
    try:
        cur.execute("""
            INSERT INTO app_private.heartbeat_log
              (driver_id, lat, lng, speed_mph, gps_accuracy_m,
               state, current_offer_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (
            driver_id, current_lat, current_lng, speed_mph, gps_accuracy_m,
            state_at_eval, current_offer_id,
        ))
    except Exception as e:
        log.warning("[heartbeat] heartbeat_log INSERT failed (non-fatal): %s", e)

    # ── DIAGNOSE ─────────────────────────────────────────────────────
    wai = WhereAmI(cur)
    matches, diagnostics = wai.evaluate_with_diagnostics(driver_id, queue)

    # ── DECIDE ───────────────────────────────────────────────────────
    actions = dispatch(matches, current_offer_id, queue_offer_ids)

    # ── EXECUTE ──────────────────────────────────────────────────────
    cluster = diagnostics.cluster
    executed_actions: list = []
    dispatch_error_msg = None
    for action in actions:
        executed, err = _execute_action(action, cur, conn, driver_id, cluster)
        if executed:
            executed_actions.append(action)
        if err:
            dispatch_error_msg = err
            break  # halt on first error; LOG still records the attempt
    dispatch_executed = bool(executed_actions)

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

    response = {"ok": True}
    voice = _voice_for_actions(executed_actions)
    if voice is not None:
        response["voice"] = voice
    return jsonify(response), 200# rebuild 1777679926
