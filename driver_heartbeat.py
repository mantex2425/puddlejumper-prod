# backend/driver_heartbeat.py
#
# Cut B3 — Simplified Architecture heartbeat handler.
#
# Pipeline:
#
#   LOAD       Read current_offer_id from driver_trip_state.
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
import json
import datetime
import logging

from flask import Blueprint, request, jsonify
from psycopg2.extras import RealDictCursor

from db import get_db
from utils import verify_and_get_user_id, require_firebase_auth
from nail_it_core import write_nailed_position
from where_am_i import WhereAmI, classify_commit_rule
from tad import OfferTadState
from driver_queue import LIVE_OFFER_PREDICATE_SQL, live_offer_predicate_params
from dispatch import (
    dispatch,
    FirePickup, FireDropoff,
    FirePickupObservation, FireDropoffObservation, ClearNarrative,
    LogNoMatch, LogPickupRematch, LogAmbiguousMatch,
)
from pudo_types import Offer, OfferMeta, TargetSpec, WAIMatch, WAI_CONFIDENCE_THRESHOLD
from motion_gate import (
    GateVerdict,
    evaluate_gates,
    filter_matches_by_gates,
)
from driver_queue import DriverQueue
from bead_on_wire import classify_address


# ── Rule XVI B-3 — Active Interrogation matcher constants ──
# Arrest threshold: contiguous zero-velocity seconds required
# to enter Phase 2b of the Forensic Ladder (§XVI.F).
# WAI floor is imported from pudo_types per §XIV.C.
ARREST_DURATION_THRESHOLD_S = 6.0

driver_heartbeat_bp = Blueprint('driver_heartbeat', __name__)

log = logging.getLogger(__name__)


# =============================================================================
# Helper: classify an address+coords into a TargetSpec
# =============================================================================

def _bucket_to_target_spec(address_text, lat, lng):
    """Classify an address string and convert to TargetSpec. None on garbage.

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

# GC constants moved to driver_queue.py in sub-commit 1c.1.1 and the
# canonical Houston Tax (1.25) lives there. _project_queue was deleted
# in sub-commit 1c.2 — its sole reader. See driver_queue.GC_BUFFER_MULT.


# _project_queue was deleted in sub-commit 1c.2.
# Replaced by driver_queue.DriverQueue.snapshot() in the orchestrator.


# =============================================================================
# EXECUTE: map a dispatch Action to its DB side effect
# =============================================================================

def _execute_action(action, cur, conn, driver_id, queue, cluster=None,
                    fallback_lat=None, fallback_lng=None,
                    cumulative_miles=None):
    """Map a dispatch Action to its DB side effect.

    Per dispatch.py contract:
      FirePickup             -> write_nailed_position(pickup) + UPDATE current_offer_id
      FireDropoff            -> write_nailed_position(dropoff) + UPDATE current_offer_id = NULL
      FirePickupObservation  -> cache writes only (pms + offer_history + community_offers);
                                no narrative state change. Rule XV / §XIV.I §5.3 pickup loser.
      FireDropoffObservation -> observation record only (offer_history + pms.offer_status);
                                no narrative state change. Rule XV / §XIV.I §5.3-mirror.
      ClearNarrative         -> queue.unbind only (UPDATE current_offer_id = NULL); no
                                cache writes. §XIV.I §5.3-mirror narrative clear.
      LogNoMatch             -> log INFO only
      LogPickupRematch       -> log DEBUG only
      LogAmbiguousMatch      -> log WARNING only

    cluster is diagnostics.cluster from WAI; centroid is the canonical
    PUDO position under the simplified architecture (no "corrected"
    coordinate step exists in the new flow).

    Returns: (executed: bool, error: Optional[str])
      executed is True when a state-changing action ran (FirePickup,
      FireDropoff). Used to populate pudo_decision_context.dispatch_executed.
      error is set if the action raised; the orchestrator halts the chain
      on first error so the LOG step still records what happened.
    """
    nail_lat = cluster.median_lat if cluster else fallback_lat
    nail_lng = cluster.median_lng if cluster else fallback_lng

    try:
        if isinstance(action, FirePickup):
            if nail_lat is None or nail_lng is None:
                # Defensive guard — heartbeat path supplies cluster, manual
                # path supplies fallback_lat/lng; one of them must resolve.
                return False, "fire_pickup_without_coords"
            write_nailed_position(cur, driver_id, 'pickup',
                                  nail_lat, nail_lng, 0)
            queue.bind(action.offer_id, cur)

            # [α-fix] pickup_market_signals: actual_pickup_* + nail_it elevation.
            # pms.offer_id REFERENCES decision_log(id), but action.offer_id is
            # offer_history.id; translate via subquery at schema boundary.
            cur.execute("""
                UPDATE app_private.pickup_market_signals
                SET actual_pickup_lat     = %s,
                    actual_pickup_lng     = %s,
                    actual_pickup_h3      = app_private.coords_to_h3(%s, %s)::text,
                    actual_pickup_at      = NOW(),
                    data_source           = 'nail_it',
                    offer_status          = 'completed'
                WHERE offer_id = (
                    SELECT decision_log_id FROM app_private.offer_history
                    WHERE id = %s::bigint
                )
            """, (nail_lat, nail_lng, nail_lat, nail_lng, action.offer_id))

            # [α-fix] offer_history: canonical PUDO record. action.offer_id
            # IS offer_history.id, so target it directly. Rowcount guard fires
            # on zero-row UPDATE — that means the offer doesn't exist (logged
            # WARNING + return failure).
            cur.execute("""
                UPDATE app_private.offer_history
                SET actual_pickup_lat                   = %s,
                    actual_pickup_lng                   = %s,
                    actual_pickup_h3                    = app_private.coords_to_h3(%s, %s)::text,
                    actual_pickup_at                    = NOW(),
                    pickup_classification               = 'auto',
                    pickup_data_source                  = 'nail_it',
                    leg_start_cumulative_miles_dropoff  = %s,
                    cumulative_miles_at_pickup_fire     = %s
                WHERE id = %s::bigint
            """, (
                nail_lat, nail_lng, nail_lat, nail_lng,
                cumulative_miles, cumulative_miles,
                action.offer_id,
            ))
            if cur.rowcount == 0:
                log.warning(
                    "[α-fix] FirePickup offer_history UPDATE wrote 0 rows "
                    "for offer_id=%s — offer not found in offer_history",
                    action.offer_id,
                )
                return False, "fire_pickup_zero_rows"


            # [α-fix] community_offers: radar feed. action.offer_id is
            # offer_history.id; pms.offer_id is decision_log.id; translate.
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
                    pms.actual_pickup_lat, pms.actual_pickup_lng, pms.actual_pickup_h3,
                    dl.fare, dl.trip_miles,
                    pms.dollars_per_mile, pms.hourly_rate_offered,
                    'nail_it',
                    app_private.coords_to_geography(pms.actual_pickup_lat, pms.actual_pickup_lng)
                FROM app_private.pickup_market_signals pms
                JOIN app_private.decision_log dl ON dl.id = pms.offer_id
                WHERE pms.offer_id = (
                    SELECT decision_log_id FROM app_private.offer_history
                    WHERE id = %s::bigint
                )
                  AND pms.actual_pickup_lat IS NOT NULL
                  AND pms.hourly_rate_offered BETWEEN 5 AND 150
                  AND NOT EXISTS (
                      SELECT 1 FROM public.community_offers co
                      WHERE co.actual_pickup_lat = pms.actual_pickup_lat
                        AND co.actual_pickup_lng = pms.actual_pickup_lng
                        AND co.data_source = 'nail_it'
                  )
            """, (action.offer_id,))

            log.info("[heartbeat] FirePickup offer=%s", action.offer_id)
            return True, None

        if isinstance(action, FireDropoff):
            if nail_lat is None or nail_lng is None:
                return False, "fire_dropoff_without_coords"
            write_nailed_position(cur, driver_id, 'dropoff',
                                  nail_lat, nail_lng, 0)
            queue.unbind(cur)

            # [α-fix] pms: data_source elevation. action.offer_id is
            # offer_history.id; translate via subquery.
            cur.execute("""
                UPDATE app_private.pickup_market_signals
                SET data_source  = 'nail_it',
                    offer_status = 'completed'
                WHERE offer_id = (
                    SELECT decision_log_id FROM app_private.offer_history
                    WHERE id = %s::bigint
                )
            """, (action.offer_id,))

            # [α-fix] offer_history: canonical PUDO record. action.offer_id
            # IS offer_history.id, target directly. Rowcount guard fires on
            # zero-row UPDATE.
            cur.execute("""
                UPDATE app_private.offer_history
                SET actual_dropoff_lat               = %s,
                    actual_dropoff_lng               = %s,
                    actual_dropoff_h3                = app_private.coords_to_h3(%s, %s)::text,
                    actual_dropoff_at                = NOW(),
                    dropoff_classification           = 'auto',
                    cumulative_miles_at_dropoff_fire = %s
                WHERE id = %s::bigint
            """, (
                nail_lat, nail_lng, nail_lat, nail_lng,
                cumulative_miles,
                action.offer_id,
            ))
            if cur.rowcount == 0:
                log.warning(
                    "[α-fix] FireDropoff offer_history UPDATE wrote 0 rows "
                    "for offer_id=%s — offer not found in offer_history",
                    action.offer_id,
                )
                return False, "fire_dropoff_zero_rows"


            # [α-fix] community_offers failsafe. action.offer_id is
            # offer_history.id; translate via subquery.
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
                    app_private.coords_to_h3(pms.actual_pickup_lat, pms.actual_pickup_lng)::text,
                    dl.fare, dl.trip_miles,
                    pms.dollars_per_mile, pms.hourly_rate_offered,
                    'nail_it',
                    app_private.coords_to_geography(pms.actual_pickup_lat, pms.actual_pickup_lng)
                FROM app_private.pickup_market_signals pms
                JOIN app_private.decision_log dl ON dl.id = pms.offer_id
                WHERE pms.offer_id = (
                    SELECT decision_log_id FROM app_private.offer_history
                    WHERE id = %s::bigint
                )
                  AND pms.data_source = 'nail_it'
                  AND pms.actual_pickup_lat IS NOT NULL
                  AND pms.hourly_rate_offered BETWEEN 5 AND 150
                  AND NOT EXISTS (
                      SELECT 1 FROM public.community_offers co
                      WHERE co.actual_pickup_lat = pms.actual_pickup_lat
                        AND co.actual_pickup_lng = pms.actual_pickup_lng
                        AND co.data_source = 'nail_it'
                  )
            """, (action.offer_id,))

            sev_map = {None: logging.INFO,
                       "canceled": logging.INFO,
                       "pickup_missed": logging.WARNING}
            log.log(sev_map.get(action.outcome, logging.INFO),
                    "[heartbeat] FireDropoff offer=%s outcome=%s",
                    action.offer_id, action.outcome)
            return True, None

        if isinstance(action, FirePickupObservation):
            # Rule XV / §XIV.I §5.3 pickup case: tied loser fires observation
            # only — cache writes mirror FirePickup's pms+offer_history+
            # community_offers triplet, but no write_nailed_position and no
            # queue.bind. The winner's FirePickup already set current_offer_id;
            # this captures the loser's location data for the caches without
            # claiming a narrative.
            if nail_lat is None or nail_lng is None:
                return False, "fire_pickup_observation_without_coords"

            # [α-fix mirror] pms: pricing cache write. Same SQL as FirePickup's
            # pms UPDATE — actual_pickup_* from cluster centroid, data_source
            # elevation, offer_status completed.
            cur.execute("""
                UPDATE app_private.pickup_market_signals
                SET actual_pickup_lat     = %s,
                    actual_pickup_lng     = %s,
                    actual_pickup_h3      = app_private.coords_to_h3(%s, %s)::text,
                    actual_pickup_at      = NOW(),
                    data_source           = 'nail_it',
                    offer_status          = 'completed'
                WHERE offer_id = (
                    SELECT decision_log_id FROM app_private.offer_history
                    WHERE id = %s::bigint
                )
            """, (nail_lat, nail_lng, nail_lat, nail_lng, action.offer_id))

            # [α-fix mirror] offer_history: per-offer observation record. The
            # WHERE actual_pickup_at IS NULL guard makes this idempotent — a
            # subsequent FirePickupObservation for the same offer is a no-op.
            cur.execute("""
                UPDATE app_private.offer_history
                SET actual_pickup_lat                   = %s,
                    actual_pickup_lng                   = %s,
                    actual_pickup_h3                    = app_private.coords_to_h3(%s, %s)::text,
                    actual_pickup_at                    = NOW(),
                    pickup_classification               = 'auto_observation',
                    pickup_data_source                  = 'nail_it',
                    leg_start_cumulative_miles_dropoff  = %s,
                    cumulative_miles_at_pickup_fire     = %s
                WHERE id = %s::bigint
                  AND actual_pickup_at IS NULL
            """, (
                nail_lat, nail_lng, nail_lat, nail_lng,
                cumulative_miles, cumulative_miles,
                action.offer_id,
            ))
            # No rowcount guard: idempotent no-op is acceptable for observation.

            # [α-fix mirror] community_offers: geographic cache insert. The
            # NOT EXISTS guard handles the §5.3 case naturally — when winner
            # and loser share cluster coords (which they do by definition),
            # winner's earlier INSERT succeeds and loser's no-ops on dedup.
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
                    pms.actual_pickup_lat, pms.actual_pickup_lng, pms.actual_pickup_h3,
                    dl.fare, dl.trip_miles,
                    pms.dollars_per_mile, pms.hourly_rate_offered,
                    'nail_it',
                    app_private.coords_to_geography(pms.actual_pickup_lat, pms.actual_pickup_lng)
                FROM app_private.pickup_market_signals pms
                JOIN app_private.decision_log dl ON dl.id = pms.offer_id
                WHERE pms.offer_id = (
                    SELECT decision_log_id FROM app_private.offer_history
                    WHERE id = %s::bigint
                )
                  AND pms.actual_pickup_lat IS NOT NULL
                  AND pms.hourly_rate_offered BETWEEN 5 AND 150
                  AND NOT EXISTS (
                      SELECT 1 FROM public.community_offers co
                      WHERE co.actual_pickup_lat = pms.actual_pickup_lat
                        AND co.actual_pickup_lng = pms.actual_pickup_lng
                        AND co.data_source = 'nail_it'
                  )
            """, (action.offer_id,))

            log.info("[heartbeat] FirePickupObservation offer=%s", action.offer_id)
            # executed=True so dispatch_executed reflects "we did real work";
            # the cache writes are the work. Forensic visibility for §XIV.I.
            return True, None

        if isinstance(action, FireDropoffObservation):
            # Rule XV / §XIV.I §5.3-mirror two-dropoff case: every tied dropoff
            # fires this. Updates offer_history.actual_dropoff_* (per-offer
            # observation record) and pms.offer_status='completed' (pricing
            # cache acknowledgment). No community_offers INSERT — that table
            # is a pickup-location cache; dropoff coords are already in
            # decision_log.dropoff_h3_index from offer-receipt geocoding.
            # No queue.unbind; ClearNarrative (emitted alongside in the same
            # action list) handles narrative state.
            if nail_lat is None or nail_lng is None:
                return False, "fire_dropoff_observation_without_coords"

            # pms: offer_status elevation. data_source NOT elevated to
            # 'nail_it' here because that signal is reserved for the pickup
            # path; dropoff observation doesn't establish data lineage the
            # same way (pickup_market_signals is fundamentally pickup-centric).
            cur.execute("""
                UPDATE app_private.pickup_market_signals
                SET offer_status = 'completed'
                WHERE offer_id = (
                    SELECT decision_log_id FROM app_private.offer_history
                    WHERE id = %s::bigint
                )
            """, (action.offer_id,))

            # offer_history: per-offer dropoff observation. Idempotent via
            # WHERE actual_dropoff_at IS NULL.
            cur.execute("""
                UPDATE app_private.offer_history
                SET actual_dropoff_lat               = %s,
                    actual_dropoff_lng               = %s,
                    actual_dropoff_h3                = app_private.coords_to_h3(%s, %s)::text,
                    actual_dropoff_at                = NOW(),
                    dropoff_classification           = 'auto_observation',
                    cumulative_miles_at_dropoff_fire = %s
                WHERE id = %s::bigint
                  AND actual_dropoff_at IS NULL
            """, (
                nail_lat, nail_lng, nail_lat, nail_lng,
                cumulative_miles,
                action.offer_id,
            ))

            log.info("[heartbeat] FireDropoffObservation offer=%s", action.offer_id)
            return True, None

        if isinstance(action, ClearNarrative):
            # §XIV.I §5.3-mirror narrative clear: set current_offer_id = NULL
            # without firing any dropoff. The system is now in observe-only
            # mode awaiting next anchoring event. No write_nailed_position
            # because we make no claim about which offer ended at what
            # coordinates — that's exactly the ambiguity ClearNarrative admits.
            queue.unbind(cur)
            log.info("[heartbeat] ClearNarrative (§XIV.I two-dropoff)")
            # executed=True: the narrative state mutation IS the side effect.
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

def _derive_post_offer_id(executed_actions, current_offer_id):
    """Derive post-dispatch current_offer_id from the executed action list.

    Phase 1B Deterministic Fold (Gemini-ratified Option B,
    PHASE_1B_PROPOSAL_v2.md §2). Mirrors dispatch.py module-docstring
    wiring rules:

        FirePickup(offer_id) -> current_offer_id = offer_id
        FireDropoff(...)     -> current_offer_id = None
        Log* actions         -> no state change

    Returns the offer_id string the driver is now bound to, or None if
    they were just dropped off (or were never bound and no FirePickup
    fired). When executed_actions is empty, returns the input
    current_offer_id unchanged.

    For the rare Case D scenario (§5.2: implicit cancel + new pickup in
    one heartbeat), dispatch.py emits FireDropoff first then FirePickup,
    so the fold yields the new offer_id -- the truthful post-state.
    """
    post = current_offer_id
    for action in executed_actions:
        if isinstance(action, FirePickup):
            post = action.offer_id
        elif isinstance(action, FireDropoff):
            post = None
    return post


# =============================================================================
# Phase 2c.2 Item 3b.R — caller-layer helpers for TAD context assembly
# =============================================================================

def _to_utc(dt):
    """Defensive tz-attach helper. [3b.R]

    Belt-and-suspenders against psycopg2 connection-config drift: when the
    DB connection's tz handling is misconfigured, timestamptz columns can
    return naive datetimes. tad.evaluate_tad_gate raises ValueError on
    naive input (Canonical Standards v2.1 Section III), so the caller
    coerces at the assembly boundary.

    Returns None for None input. Returns input normalized to UTC when
    aware. Attaches UTC when naive.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc)


def _assemble_per_offer_state(cur, driver_id, queue_offer_ids):
    """Fetch dict[offer_id, OfferTadState] for the queue. [3b.R]

    Joins offer_history through decision_log to filter by driver_id
    (offer_history has no driver_id column; identity lives on the FK
    parent decision_log). Excludes offers with NULL expected_* columns
    (legacy rows pre-dating the 3b.W writer) so tad.evaluate_tad_gate
    records a missing_state failed verdict for them rather than
    crashing on None math.

    queue_offer_ids: list[int] — offer_history.id values.
    Empty list returns empty dict without SQL call.

    All timestamptz fields pass through _to_utc for defensive tz-coercion
    (Q5 ratification, 2026-05-08).
    """
    if not queue_offer_ids:
        return {}
    cur.execute(
        """
        SELECT
            oh.id,
            oh.miles_at_offer_receipt,
            oh.created_at,
            oh.expected_pickup_arrival_time,
            oh.expected_pickup_distance,
            oh.actual_pickup_at,
            oh.cumulative_miles_at_pickup_fire,
            oh.pickup_exit_time,
            oh.exit_velocity_timeout
        FROM app_private.offer_history oh
        JOIN app_private.decision_log dl ON dl.id = oh.decision_log_id
        WHERE oh.id = ANY(%s::bigint[])
          AND dl.driver_id = %s
        """,
        (queue_offer_ids, driver_id),
    )
    out = {}
    for row in cur.fetchall():
        # Exclude legacy rows with NULL expected_* (pre-3b.W writer).
        if row["expected_pickup_arrival_time"] is None:
            continue
        if row["expected_pickup_distance"] is None:
            continue
        if row["miles_at_offer_receipt"] is None:
            continue
        out[str(row["id"])] = OfferTadState(
            offer_id=str(row["id"]),
            miles_at_offer_receipt=float(row["miles_at_offer_receipt"]),
            accepted_at=_to_utc(row["created_at"]),
            expected_pickup_arrival_time=_to_utc(row["expected_pickup_arrival_time"]),
            expected_pickup_distance=float(row["expected_pickup_distance"]),
            actual_pickup_at=_to_utc(row["actual_pickup_at"]),
            cumulative_miles_at_pickup_fire=(
                float(row["cumulative_miles_at_pickup_fire"])
                if row["cumulative_miles_at_pickup_fire"] is not None
                else None
            ),
            pickup_exit_time=_to_utc(row["pickup_exit_time"]),
            exit_velocity_timeout=bool(row["exit_velocity_timeout"]),
        )
    return out


def _get_last_known_anchor_id(cur, driver_id, current_cumulative_miles, reference_time):
    """Find most recent LIVE offer_id with confirmed PUDO. [3b.R, GC-aware]

    P0 fix 2026-05-10: applies LIVE_OFFER_PREDICATE_SQL. Stale offers
    (past wall-clock or distance horizon) are excluded — they would
    corrupt TAD distance computations by anchoring against ancient
    odometer values (root cause of the 2026-05-08 dispatch silence).

    Source-of-truth path: queries offer_history.actual_pickup_at /
    actual_dropoff_at directly, joining decision_log for driver
    identity. Same wall-clock + distance horizon as
    DriverQueue._project_offers per CANONICAL_RULES Section IV addendum.

    Args:
        cur: psycopg2 cursor.
        driver_id: Firebase UID.
        current_cumulative_miles: float or None. When None, distance
            axis degrades to time-only.

    Returns str(offer_id) or None when no live anchor exists.
    """
    cur.execute(
        f"""
        SELECT oh.id
        FROM app_private.offer_history oh
        JOIN app_private.decision_log dl ON dl.id = oh.decision_log_id
        WHERE dl.driver_id = %s
          AND (oh.actual_pickup_at IS NOT NULL OR oh.actual_dropoff_at IS NOT NULL)
          AND {LIVE_OFFER_PREDICATE_SQL}
        ORDER BY COALESCE(oh.actual_dropoff_at, oh.actual_pickup_at) DESC
        LIMIT 1
        """,
        (driver_id,) + live_offer_predicate_params(current_cumulative_miles, reference_time),
    )
    row = cur.fetchone()
    return str(row["id"]) if row else None


def _detect_lost_mode(cur, driver_id, queue_offer_ids, current_cumulative_miles, reference_time):
    """Detect narrative_blindness via HORIZON physics. [Rule VII, 2026-05-12]

    REWRITE (2026-05-12): the previous implementation used
    `actual_pickup_at IS NULL` as a proxy for "narrative broken" plus
    a crude `interval '2 hours'` wall-clock window. Both were wrong.

    Failure modes the old rule produced on the 2026-05-12 drive:

      Houston Miss (offers 7848, 7853): AAI missed the pickup
        observation, but dropoff fired cleanly. Old rule treated the
        offer as a permanent ghost for 2 hours after dropoff fired,
        poisoning every subsequent heartbeat with lost_mode=true and
        forcing the dispatcher into conservative commit mode. This
        cascaded — pickups missed because of lost_mode-conservative
        commit produced new ghosts, extending lost_mode further.

      Calhoun Zombie: pickup fires successfully but the dropoff
        address Uber gave doesn't exist where navigation took you.
        AAI never observes dropoff. Driver moves to next ride. Old
        rule's `actual_pickup_at IS NULL` clause excluded the Calhoun
        offer entirely (wrong direction — Calhoun is exactly the kind
        of ghost the rule was supposed to catch).

    The new rule uses LIVE_OFFER_PREDICATE_SQL — the same predicate
    DriverQueue uses to decide which offers are "alive" in the
    Diagnose-side queue. Physics, not fire state, decides:

        An offer triggers lost_mode iff
          accepted
          AND not currently in the active queue
          AND still inside its time/distance horizon
          (LIVE_OFFER_PREDICATE_SQL — see driver_queue.py)

    The predicate already contains `oh.actual_dropoff_at IS NULL` as
    its first clause, so dropoff-fired offers are excluded
    automatically (Houston Miss → not orphan, correct).

    Pickup-fired-no-dropoff offers stay live until the time OR
    distance horizon blows. Once physics terminates them, they stop
    poisoning lost_mode. The Calhoun Zombie self-resolves after the
    driver crosses the trip-miles horizon.

    Args:
        cur: psycopg2 cursor.
        driver_id: Firebase UID.
        queue_offer_ids: int list of offers currently in the active
            queue; these are excluded (already being evaluated).
        current_cumulative_miles: float or None. When None, the
            distance axis of the predicate short-circuits to TRUE
            (time-only fallback). Production heartbeat path always
            supplies a value.

    Returns True iff at least one live orphaned offer exists.
    """
    cur.execute(
        f"""
        SELECT oh.id
        FROM app_private.offer_history oh
        JOIN app_private.decision_log dl ON dl.id = oh.decision_log_id
        WHERE dl.driver_id = %s
          AND oh.id != ALL(%s::bigint[])
          AND oh.app_verdict = 'ACCEPT'
          AND {LIVE_OFFER_PREDICATE_SQL}
        ORDER BY oh.created_at DESC
        LIMIT 1
        """,
        (driver_id, queue_offer_ids or [0])
        + live_offer_predicate_params(current_cumulative_miles, reference_time),
    )
    return cur.fetchone() is not None


def _build_tad_decision_context(
    diagnostics, matches, lost_mode, last_known_anchor_id,
    executed_actions=None, queue_metadata=None,
):
    """Serialize tad_decision_context JSONB blob. [3b.R + Phase 4 narrative_tiebreaker]

    Returns str (json.dumps output) or None.
      None: bridge state — diagnostics.tad_verdicts is empty {} (caller
        invoked evaluate_with_diagnostics without per_offer_state).
        NULL JSONB in DB preserves regression compatibility with the
        pre-3b.R 521 test floor.
      str: TAD evaluation occurred — full forensic blob with verdicts,
        per-committed commit_rule classification, lost_mode state, the
        last_known_anchor_id forensic context, and (Phase 4)
        narrative_tiebreaker recording §5.3 dispatch decisions per
        CANONICAL_RULES.md §XIV.I.

    Per Bible Rule 5: classification labels live INSIDE this JSONB,
    never as flat columns on pudo_decision_context.

    Phase 4 (Rule XV / §XIV.I): the narrative_tiebreaker field records
    which §5.3 case fired in dispatch and the recency tiebreaker outcome.
    Detection uses executed_actions (truthful — records what was actually
    persisted) not matches (which would record dispatch's intent even if
    _execute_action raised partway through the action chain).

    executed_actions and queue_metadata default to None for backwards
    compatibility with callers that haven't been updated to Phase 4
    (notably tests/fixtures that synthesize _build_tad_decision_context
    inputs directly). When either is None, narrative_tiebreaker is
    omitted from the JSONB rather than emitted as null — preserves
    pre-Phase 4 blob shape exactly for those callers.
    """
    if not diagnostics.tad_verdicts:
        return None
    blob = {
        "verdicts": {
            oid: {
                "passed": v.passed,
                "time_boost": v.time_boost,
                "leg_evaluated": v.leg_evaluated,
                "lost_mode_reason": v.lost_mode_reason,
                "distance_gate": v.distance_gate,
                "time_signal": v.time_signal,
            }
            for oid, v in diagnostics.tad_verdicts.items()
        },
        "committed": [
            {
                "offer_id": m.offer_id,
                "commit_rule": classify_commit_rule(
                    diagnostics.outcome_for(m),
                    diagnostics.tad_verdicts.get(m.offer_id),
                ),
            }
            for m in matches
            if diagnostics.outcome_for(m) is not None
        ],
        "lost_mode": lost_mode,
        "last_known_anchor_id": last_known_anchor_id,
    }

    # Phase 4 narrative_tiebreaker: detect §5.3 cases from executed_actions.
    # Truthful forensic — what actually persisted, not dispatch intent.
    if executed_actions is not None and queue_metadata is not None:
        narrative_tiebreaker = _detect_narrative_tiebreaker(
            executed_actions, queue_metadata
        )
        if narrative_tiebreaker is not None:
            blob["narrative_tiebreaker"] = narrative_tiebreaker

    return json.dumps(blob)


def _detect_narrative_tiebreaker(executed_actions, queue_metadata):
    """Detect which §XIV.I §5.3 case fired in this heartbeat.

    Returns narrative_tiebreaker dict per §XIV.I "Forensic record":
      §5.3 pickup case:
        {winner, losers, signal: "created_at", winner_created_at: ISO-8601 UTC}
      §5.3-mirror dropoff case:
        {winner: null, losers, signal: "ambiguous_clear"}
      Neither fired: None (no field emitted)

    Detection by type-presence in executed_actions:
      - FirePickupObservation present -> §5.3 pickup case fired
      - ClearNarrative present -> §5.3-mirror dropoff case fired
      - Both shapes mutually exclusive: dispatch never emits both for one
        heartbeat (pickup case emits FirePickup+FPO; dropoff case emits
        FDO+CN; no overlap).

    Per Rule III: winner_created_at serialized via .isoformat() which on
    a UTC-aware datetime produces a '+00:00' suffix, preserving the
    timezone anchor in the forensic record.
    """
    fp_obs = [a for a in executed_actions if isinstance(a, FirePickupObservation)]
    fd_obs = [a for a in executed_actions if isinstance(a, FireDropoffObservation)]
    fire_pickups = [a for a in executed_actions if isinstance(a, FirePickup)]
    clear_narratives = [a for a in executed_actions if isinstance(a, ClearNarrative)]

    if fp_obs:
        # §5.3 pickup case: there's exactly one FirePickup (the winner)
        # and one or more FirePickupObservations (the losers).
        if not fire_pickups:
            # Defensive: FirePickupObservation without a FirePickup means
            # the winner's action raised and halted the loop. Record the
            # partial state honestly.
            return {
                "winner": None,
                "losers": [a.offer_id for a in fp_obs],
                "signal": "created_at",
                "winner_created_at": None,
                "note": "winner_action_failed_pre_persistence",
            }
        winner = fire_pickups[0]
        winner_meta = queue_metadata.get(winner.offer_id)
        return {
            "winner": winner.offer_id,
            "losers": [a.offer_id for a in fp_obs],
            "signal": "created_at",
            "winner_created_at": (
                winner_meta.created_at.isoformat() if winner_meta else None
            ),
        }

    if clear_narratives:
        # §5.3-mirror dropoff case: ClearNarrative emitted, with
        # FireDropoffObservation(s) for every tied dropoff.
        return {
            "winner": None,
            "losers": [a.offer_id for a in fd_obs],
            "signal": "ambiguous_clear",
        }

    return None


def _log_decision_context(
    cur, driver_id, body,
    current_lat, current_lng, speed_mph, gps_accuracy_m,
    current_offer_id,
    diagnostics, matches, executed_actions,
    dispatch_executed, dispatch_error_msg,
    gate_verdict=None,
    lost_mode=False,                    # [3b.R]
    last_known_anchor_id=None,          # [3b.R]
    queue_metadata=None,                # [Phase 4]
    arrest_started_at_post=None,        # [Rule XVI B-2]
    arrest_counter_s_post=None,         # [Rule XVI B-2]
    phase_reached=1,                    # [Rule XVI B-3]
    matched_offer_id=None,              # [Rule XVI B-3]
    match_signal=None,                  # [Rule XVI B-3]
    matcher_candidates=None,            # [Rule XVI B-3]
    unmatched_reason=None,              # [Rule XVI B-3]
):
    """Insert pudo_decision_context row from DiagnosticContext + dispatch result.

    §3 step 8 invariant: cluster_* columns are populated from
    diagnostics.cluster regardless of match outcome. Even when matches is
    empty, even when WAI returned no cluster, this INSERT runs and records
    what we saw.

    Phase 1B (2026-05-05): forensic restoration.
      - Restored 5 wai_* bindings dropped by Cut B3:
        target_address, reason, on_target_road, current_road,
        off_wire_duration_s. Sourced from MatchOutcome (looked up via
        DiagnosticContext.outcome_for) and RoadTopology.
      - Added wai_current_road_class binding (Phase 1A signal exposure).
      - Added 3 poi_* columns bound NULL until Phase 2 (Operation Strip
        Mall) populates them.
      - Fixed Cut B3 double-binding bug for current_offer_id: at_eval
        now binds the PRE-dispatch value (the parameter); current_offer_id
        binds the POST-dispatch value derived via deterministic fold over
        executed_actions (Gemini-ratified Option B).

    Tie-handling: when matches contains ties at max confidence (§3 Reduce
    step), top_match = matches[0] is stable / input-ordered (first-among-
    equals from queue order in the Map step). Per-target detail for all
    matches lives in diagnostics.per_target_outcomes; logging that
    fan-out is out of 1B scope.
    """
    cluster = diagnostics.cluster
    topo = diagnostics.topology

    # [3b.R] Build TAD forensic blob (Bible Rule 5: JSONB-resident, never flat)
    tad_decision_context = _build_tad_decision_context(
        diagnostics, matches, lost_mode, last_known_anchor_id,
        executed_actions=executed_actions,
        queue_metadata=queue_metadata,
    )

    # Project executed-action list into a forensic-readable string.
    # Multiple actions (Case D, §5.2) join with '+'. We project executed_
    # actions (not the dispatched intent) because forensic truthfulness
    # tracks what actually fired; failed dispatches surface via
    # dispatch_error.
    action_str = "+".join(type(a).__name__ for a in executed_actions) if executed_actions else None

    # Top match → outcome lookup for forensic fields (per WAIMatch's
    # encapsulation discipline: forensic fields live on MatchOutcome,
    # accessed via DiagnosticContext.outcome_for).
    top_match = matches[0] if matches else None
    top_outcome = diagnostics.outcome_for(top_match) if top_match else None

    # Deterministic fold (Phase 1B Option B): derive post-dispatch
    # current_offer_id. See _derive_post_offer_id docstring for rules.
    post_offer_id = _derive_post_offer_id(executed_actions, current_offer_id)

    cur.execute(
        """
        INSERT INTO app_private.pudo_decision_context (
            driver_id, current_offer_id_at_eval, current_offer_id,
            lat, lng, speed_mph, heading, gps_accuracy_m, gps_age_s,
            wai_pudo_type, wai_offer_id, wai_confidence,
            wai_target_address, wai_reason, wai_on_target_road,
            wai_current_road, wai_current_road_class, wai_off_wire_duration_s,
            wai_cluster_revisit,
            cluster_lat, cluster_lng, cluster_size, cluster_duration_s,
            poi_lookup_source, poi_match_score, poi_top_names,
            planner_action,
            dispatch_executed, dispatch_error,
            motion_gate_result, odometer_gate_result,
            gate_held_offer_ids, gate_held_legs,
            tad_decision_context,
            arrest_started_at, arrest_duration_s,
            phase_reached, matched_offer_id, match_signal,
            matcher_candidates, unmatched_reason
        ) VALUES (
            %s, %s, %s,
            %s, %s, %s, %s, %s, %s,
            %s, %s, %s,
            %s, %s, %s,
            %s, %s, %s,
            %s,
            %s, %s, %s, %s,
            %s, %s, %s,
            %s,
            %s, %s,
            %s, %s,
            %s, %s,
            %s,
            %s, %s,
            %s, %s, %s,
            %s, %s
        )
        """,
        (
            driver_id, current_offer_id, post_offer_id,
            current_lat, current_lng, speed_mph,
            body.get('heading'), gps_accuracy_m, body.get('gpsAgeSec'),
            top_match.location_type if top_match else None,
            top_match.offer_id if top_match else None,
            top_match.confidence if top_match else None,
            top_outcome.target_address if top_outcome else None,
            top_outcome.reason if top_outcome else None,
            (top_outcome.signals.get('on_target_road') if (top_outcome and top_outcome.signals) else None),
            topo.current_road if topo else None,
            topo.current_road_class if topo else None,
            topo.off_wire_duration_s if topo else None,
            diagnostics.cluster_revisit,
            cluster.median_lat if cluster else None,
            cluster.median_lng if cluster else None,
            cluster.n if cluster else None,
            cluster.duration_s if cluster else None,
            top_outcome.poi_witness if top_outcome else None,
            top_outcome.poi_match if top_outcome else None,
            diagnostics.cluster_poi_names if diagnostics.cluster_poi_names else None,
            action_str,
            dispatch_executed,
            dispatch_error_msg,
            # Sprint A gate-layer columns (Gemini round-2 ratified):
            (gate_verdict.motion_verdict if gate_verdict else None),
            (json.dumps(gate_verdict.jsonb_payload()) if gate_verdict else None),
            (gate_verdict.held_offer_ids_and_legs()[0] if gate_verdict else None),
            (gate_verdict.held_offer_ids_and_legs()[1] if gate_verdict else None),
            tad_decision_context,    # [3b.R]
            # Rule XVI B-2: forensic record of the arrest counter state.
            # Threaded as function params from post_heartbeat() where the
            # driver_trip_state UPDATE's RETURNING clause captured them.
            arrest_started_at_post,
            arrest_counter_s_post,
            # Rule XVI B-3: matcher forensic record.
            phase_reached,
            matched_offer_id,
            match_signal,
            matcher_candidates,
            unmatched_reason,
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
    # DriverQueue.snapshot() applies the L-19 invariant on read: if
    # bound_offer_id points outside the projected queue (e.g. seed_offer
    # priming wrote the pointer but no live offer_history row exists),
    # snap.bound_offer_id is None and a WARNING tagged INVARIANT_VIOLATION
    # is logged. Heartbeat continues with the self-healed value, and
    # FirePickup/FireDropoff naturally overwrite the DB pointer.
    queue = DriverQueue(driver_id, target_spec_builder=_bucket_to_target_spec)
    snap = queue.snapshot(cur)
    current_offer_id = snap.bound_offer_id
    queue_offer_ids = set(snap.offer_ids)
    # Phase 2 (§XIV.I): OfferMeta.created_at reads from offer.accepted_at
    # because the Offer dataclass field is misnamed — it actually carries
    # offer_history.created_at (no accepted_at column exists on the table;
    # the misnaming dates to commit 6e1d60f, sub-step 1b.1). Tracked in
    # docs/DEBT.md under "Phase 3: Lexical Alignment" for separate rename.
    queue_metadata = {
        str(offer.offer_id): OfferMeta(created_at=offer.accepted_at)
        for offer in snap.offers
    }

    # ── HEARTBEAT (preserve API contract for /driver/status) ─────────
    # Per Option H1 (ratified Sprint A): keep GPS/motion fields with real
    # data sources; drop state-machine-derived fields (armed, target_type,
    # dist_to_target_m, stopped_seconds, required_stopped_seconds). The
    # /driver/status endpoint uses null-safe .get() and surfaces missing
    # fields as null — honest representation of "this concept is gone."
    # Rule XVI B-2: arrest counter. SQL-side CASE atomically maintains
    # arrest_started_at and arrest_counter_s based on prior state +
    # current speed_mph. RETURNING pulls the post-update values back into
    # Python for the pudo_decision_context forensic write below.
    #
    # Bare NOW() per Rule III: column type is timestamptz, NOW() returns
    # timestamptz, no AT TIME ZONE conversion needed. PostgreSQL evaluates
    # all SET expressions against the pre-update row state, so
    # arrest_counter_s's CASE reads the OLD arrest_started_at, not the
    # one being assigned in the same SET clause.
    #
    # speed_mph IS NULL is treated as "moving" (counter resets) — safest
    # default for missing-data heartbeats.
    cur.execute("""
        UPDATE app_private.driver_trip_state
        SET heartbeat = %s::jsonb,
            heartbeat_at = NOW(),
            arrest_started_at = CASE
                WHEN %s::real = 0.0 AND arrest_started_at IS NULL
                    THEN NOW()
                WHEN %s::real = 0.0 AND arrest_started_at IS NOT NULL
                    THEN arrest_started_at
                ELSE NULL
            END,
            arrest_counter_s = CASE
                WHEN %s::real = 0.0 AND arrest_started_at IS NOT NULL
                    THEN EXTRACT(EPOCH FROM (NOW() - arrest_started_at))::real
                WHEN %s::real = 0.0 AND arrest_started_at IS NULL
                    THEN 0.0
                ELSE NULL
            END
        WHERE driver_id = %s
        RETURNING arrest_started_at, arrest_counter_s
    """, (json.dumps({
        "lat": current_lat,
        "lng": current_lng,
        "speed_mph": speed_mph,
        "gps_accuracy_m": gps_accuracy_m,
        "cumulative_miles": cumulative_miles,
        "received_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }), speed_mph, speed_mph, speed_mph, speed_mph, driver_id))

    # Capture the post-UPDATE counter state to thread into _log_decision_context.
    # Defensive: if the driver row doesn't exist (shouldn't happen post-LOAD
    # in the same transaction, but guard anyway), default to None/None.
    _arrest_row = cur.fetchone()
    if _arrest_row is not None:
        arrest_started_at_post, arrest_counter_s_post = _arrest_row
    else:
        arrest_started_at_post, arrest_counter_s_post = None, None

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
               current_offer_id)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (
            driver_id, current_lat, current_lng, speed_mph, gps_accuracy_m,
            current_offer_id,
        ))
    except Exception as e:
        log.warning("[heartbeat] heartbeat_log INSERT failed (non-fatal): %s", e)

    # ── DIAGNOSE ─────────────────────────────────────────────────────
    # [3b.R] Phase 2c.2 Item 3b.R: assemble TAD context from offer_history.
    # Activates the dual commit rule (Items 3a/3c/3e) by promoting the WAI
    # brain out of bridge state. When queue is empty, helpers return
    # {}/None/False and _evaluate falls through to legacy 0.40 floor —
    # bridge-state preservation per Item 3 contract.
    queue_ids_int = [int(oid) for oid in snap.offer_ids]
    per_offer_state = _assemble_per_offer_state(cur, driver_id, queue_ids_int)
    # Capture the reference clock ONCE for this heartbeat. All
    # predicate evaluations in this turn share the same "now",
    # making the dispatch decision deterministic and replayable.
    _heartbeat_now = datetime.datetime.now(datetime.timezone.utc)
    last_known_anchor_id = _get_last_known_anchor_id(cur, driver_id, cumulative_miles, _heartbeat_now)
    lost_mode = _detect_lost_mode(cur, driver_id, queue_ids_int, cumulative_miles, _heartbeat_now)

    wai = WhereAmI(cur)
    matches, diagnostics = wai.evaluate_with_diagnostics(
        driver_id, snap.offers,
        per_offer_state=per_offer_state,
        lost_mode=lost_mode,
        last_known_anchor_id=last_known_anchor_id,
        current_odometer=cumulative_miles,
    )

    # ── GATE ─────────────────────────────────────────────────────────
    # Sprint A §7 + Amendment 1: post-WAI filtering. Runs unconditionally
    # so motion_verdict and per-leg odometer math are logged for every
    # heartbeat regardless of whether dispatch fires (forensic visibility
    # for matcher tuning per §3 step 8).
    gate_verdict = evaluate_gates(
        cluster=diagnostics.cluster,
        cumulative_miles=cumulative_miles,
        queue_offers=snap.offers,
        speed_mph=speed_mph,
    )
    gated_matches = filter_matches_by_gates(matches, gate_verdict)

    # ── MATCH (Rule XVI B-3 Active Interrogation) ────────────────────
    # Forensic Ladder. matcher_actions is None when the matcher
    # abstains (arrest < threshold OR all candidates rejected) — in
    # those cases the lazy dispatch path retains agency below.
    phase_reached = 1
    matched_offer_id = None
    match_signal = None
    matcher_candidates = []
    unmatched_reason = None
    matcher_actions = None

    # Phase 1 → Phase 2 perimeter: is any offer in its destination zone?
    if any(v.distance_gate.get('passed') is True
           for v in diagnostics.tad_verdicts.values()):
        phase_reached = 2

    if (arrest_counter_s_post is not None
            and arrest_counter_s_post >= ARREST_DURATION_THRESHOLD_S):
        candidates = []  # list[(offer_id, leg, confidence)]
        tad_passed_any = False

        for offer_id, verdict in diagnostics.tad_verdicts.items():
            if verdict.distance_gate.get('passed') is not True:
                continue
            tad_passed_any = True
            leg = verdict.leg_evaluated
            if leg not in ('pickup', 'dropoff'):
                continue
            outcome = None
            for oid, ltype, oc in diagnostics.per_target_outcomes:
                if oid == offer_id and ltype == leg:
                    outcome = oc
                    break
            if outcome is None or outcome.confidence < WAI_CONFIDENCE_THRESHOLD:
                continue
            candidates.append((offer_id, leg, outcome.confidence))

        matcher_candidates = [c[0] for c in candidates]

        if len(candidates) == 1:
            # Single-match express lane (Option β).
            offer_id, leg, _conf = candidates[0]
            action_cls = FirePickup if leg == 'pickup' else FireDropoff
            matcher_actions = [action_cls(offer_id=offer_id)]
            matched_offer_id = offer_id
            match_signal = 'tad_and_wai'
            phase_reached = 5
        elif len(candidates) >= 2:
            # §5.3 ambiguity — hand to dispatch (Option α).
            synth = [WAIMatch(offer_id=oid, location_type=lg, confidence=cf)
                     for oid, lg, cf in candidates]
            matcher_actions = dispatch(synth, current_offer_id, queue_metadata)
            match_signal = 'dispatch_resolved'
            phase_reached = 5
            # Pull narrative winner from dispatch's action list (None for
            # §5.3-mirror ClearNarrative case where no fire occurs).
            for a in matcher_actions:
                if isinstance(a, (FirePickup, FireDropoff)):
                    matched_offer_id = a.offer_id
                    break
        else:
            # Arrest reached but no candidate passed both gates.
            match_signal = 'no_match'
            if not diagnostics.tad_verdicts:
                unmatched_reason = 'empty_queue'
            elif tad_passed_any:
                unmatched_reason = 'wai_below_floor'
                phase_reached = 3
            else:
                unmatched_reason = 'tad_failed'
                # phase_reached retains perimeter-scan value (1 or 2)

    # ── DECIDE ───────────────────────────────────────────────────────
    if matcher_actions is not None:
        # Matcher fired (single or §5.3). Lazy path skipped.
        actions = matcher_actions
    else:
        # Matcher abstained — lazy dispatch path retains agency
        # (handoff: 'let the lazy path fire' on matcher no-match).
        actions = dispatch(gated_matches, current_offer_id, queue_metadata)

    # ── EXECUTE ──────────────────────────────────────────────────────
    cluster = diagnostics.cluster
    executed_actions: list = []
    dispatch_error_msg = None
    for action in actions:
        executed, err = _execute_action(
            action, cur, conn, driver_id, queue, cluster,
            cumulative_miles=cumulative_miles,
            fallback_lat=current_lat, fallback_lng=current_lng,
        )
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
            current_offer_id,
            diagnostics, matches, executed_actions,
            dispatch_executed, dispatch_error_msg,
            gate_verdict=gate_verdict,
            lost_mode=lost_mode,                          # [3b.R]
            last_known_anchor_id=last_known_anchor_id,    # [3b.R]
            queue_metadata=queue_metadata,                # [Phase 4]
            arrest_started_at_post=arrest_started_at_post,    # [Rule XVI B-2]
            arrest_counter_s_post=arrest_counter_s_post,      # [Rule XVI B-2]
            phase_reached=phase_reached,                      # [Rule XVI B-3]
            matched_offer_id=matched_offer_id,                # [Rule XVI B-3]
            match_signal=match_signal,                        # [Rule XVI B-3]
            matcher_candidates=matcher_candidates,            # [Rule XVI B-3]
            unmatched_reason=unmatched_reason,                # [Rule XVI B-3]
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
