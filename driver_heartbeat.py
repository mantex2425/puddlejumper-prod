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
from where_am_i import WhereAmI, _commits, classify_commit_rule, haversine_meters
from tad import OfferTadState
from driver_queue import LIVE_OFFER_PREDICATE_SQL, live_offer_predicate_params
from dispatch import (
    dispatch,
    FirePickup, FireDropoff,
    FirePickupObservation, FireDropoffObservation, ClearNarrative,
    LogNoMatch, LogPickupRematch, LogAmbiguousMatch,
)
from decisions.transaction_lock import (
    acquire_lock, is_locked, release_lock, LockContext,
)
from pudo_types import Offer, OfferMeta, TargetSpec, WAIMatch, WAI_CONFIDENCE_THRESHOLD
from driver_queue import DriverQueue
from bead_on_wire import classify_address
from cluster_detection import ARREST_DURATION_THRESHOLD_S


# ── §XVI.C TAD-as-Input matcher constants (ratified 2026-05-22) ──
# Arrest threshold: contiguous zero-velocity seconds required for
# PUDO commit. At 5s cold cadence: 1 confirming sample. At 1Hz Horny
# cadence: 5 confirming samples. Per §XVI.C amended doctrine, this
# composes with WAI ≥ WAI_CONFIDENCE_THRESHOLD as the canonical
# two-key commit gate. TAD is no longer a separate gate.
# NOTE: the value itself is defined ONCE in cluster_detection.py
# (the stdlib-only leaf) and imported above — single source of truth
# shared with detect_cluster's min_duration_s default. Do not redefine here.

# Horny mode speed threshold: cadence target jumps to 1Hz when WAI
# confidence ≥ floor AND speed drops below this value. Two existing
# signals composed into a per-heartbeat cadence hint. No mode flag,
# no state machine — cadence is recomputed every heartbeat from
# current signals.
HORNY_SPEED_THRESHOLD_MPH = 5.0

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
      "garbage"        -> "garbage"          named_roads=() — §PR-A:
                                              admitted with null coords,
                                              routed to _match_poi_class
                                              via _CLASS_DISPATCH so geofence
                                              and semantic anchor can rescue.
      unrecognized     -> None (defensive; classify_address should never emit)
    """
    if not address_text:
        return None
    # §PR-A: null coords admitted. TargetSpec contract permits per
    # pudo_types.py:54-55 (Optional[float]). Downstream heads degrade
    # gracefully: coord-dependent heads contribute 0, coord-independent
    # heads (Head 5 §XVII, Head 6 P18 geofence) run normally.
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
            lat=float(lat) if lat is not None else None,
            lng=float(lng) if lng is not None else None,
            address_class="intersection",
            named_roads=(parts.get("road_a", ""), parts.get("road_b", "")),
            address=address_text,
        )
    if bucket == "street_number":
        return TargetSpec(
            lat=float(lat) if lat is not None else None,
            lng=float(lng) if lng is not None else None,
            address_class="number_on_street",
            named_roads=(parts.get("road", ""),),
            address=address_text,
        )
    if bucket == "single_road":
        return TargetSpec(
            lat=float(lat) if lat is not None else None,
            lng=float(lng) if lng is not None else None,
            address_class="single_road",
            named_roads=(parts.get("road", ""),),
            address=address_text,
        )
    if bucket == "poi":
        return TargetSpec(
            lat=float(lat) if lat is not None else None,
            lng=float(lng) if lng is not None else None,
            address_class="poi",
            named_roads=(),
            address=address_text,
        )
    # §PR-A: "garbage" bucket admitted with explicit address_class.
    # Routes via _CLASS_DISPATCH["garbage"] to _match_poi_class so
    # geofence (Head 6) and semantic anchor (Head 5) can rescue offers
    # whose text was OCR-shredded past classify_address keyword regexes.
    if bucket == "garbage":
        return TargetSpec(
            lat=float(lat) if lat is not None else None,
            lng=float(lng) if lng is not None else None,
            address_class="garbage",
            named_roads=(),
            address=address_text,
        )
    # Defensive: classify_address contract guarantees one of the five
    # buckets above plus "garbage". Unknown bucket means upstream broke.
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
                    cumulative_miles=None,
                    alive_unpicked_offer_ids=frozenset(),
                    pickup_floor_clearers=frozenset()):
    """Map a dispatch Action to its DB side effect.

    Per dispatch.py contract:
      FirePickup             -> write_nailed_position(pickup) + UPDATE current_offer_id
      FireDropoff            -> write_nailed_position(dropoff) + UPDATE current_offer_id = NULL
      FirePickupObservation  -> cache writes only (pms + offer_history + community_offers);
                                MAY bind current_offer_id when the queue contains exactly
                                ONE alive-unpicked offer == this action's offer_id (the
                                §XVIII cold-start exit, 2026-05-31). Rule XV / §XIV.I §5.3
                                pickup loser otherwise.
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

    alive_unpicked_offer_ids is the §XVIII bit-2 set as computed by
    _get_alive_unpicked_offer_ids at the heartbeat's top. Pass-through
    enables the FirePickupObservation cold-start bind to read the
    same canonical set _detect_lost_mode evaluated against, avoiding
    a parallel query. Default frozenset() preserves callers that pre-
    date the cold-start fix (and tests that don't exercise the bind).

    pickup_floor_clearers is the spatial-local competing set for the
    §XVIII bind gate (2026-06-01 sharpening, FIX_PROPOSAL_BIND_SPATIAL_
    LOCAL). It is the set of offer_ids that (a) WAI evaluated on the
    pickup leg at this heartbeat AND (b) cleared the canonical _commits
    floor for their TAD verdict. Computed once in the heartbeat body
    and threaded as a finished frozenset (Gemini §4.1) to avoid
    coupling the FPO handler to the raw per_target_outcomes /
    tad_verdicts structures. Default frozenset() makes the bind gate
    fail-closed for callers that don't supply it — manual confirm
    endpoints, tests that don't exercise the bind, etc.

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

            # [Rule XV Observation Over Narrative — 2026-05-30] Before any
            # cache writes, detect whether this offer was already fired by
            # a prior FirePickupObservation. If so, this FirePickup is a
            # "narrative catch-up": lost-mode demoted the original fire to
            # Observation (which correctly wrote the cache at the true
            # pickup location and moment), then lost-mode lifted and the
            # matcher re-emitted FirePickup — potentially minutes later
            # and miles away from where the rider actually got in.
            #
            # Mandate per Rule XV: preserve the Observation's cache write
            # (offer_history.actual_pickup_*, pms.actual_pickup_*, and
            # community_offers — all populated at the true location). DO
            # still bind the narrative (queue.bind → current_offer_id)
            # and write the current nail position (driver_trip_state's
            # nailed_pickup_* fields are a per-driver state snapshot, not
            # a per-offer record, so they reflect the current fire).
            #
            # See docs/RECON_IMPERIAL_VALLEY_PICKUP5_2026-05-30.md VERDICT
            # for the verbatim 1.8mi / 10-min geographic drift this guard
            # prevents. Pickup 5 of the 2026-05-30 drive corrupted
            # offer_history.actual_pickup_at + .actual_pickup_lat/lng
            # because this guard was absent.
            # [error-metric 2026-06-02] Widened from SELECT actual_pickup_at
            # to also fetch intended pickup_lat/lng — the normal-fire path
            # below computes pickup_error_m from these. The catch-up branch
            # (already_fired_at is not None) must NOT write pickup_error_m
            # (Rule XV: preserve the prior Observation's error_m intact).
            cur.execute("""
                SELECT actual_pickup_at, pickup_lat, pickup_lng
                FROM app_private.offer_history
                WHERE id = %s::bigint
            """, (action.offer_id,))
            existing_row = cur.fetchone()
            if existing_row is None:
                # Offer not in offer_history — matcher must never emit
                # FirePickup for an unknown id. Preserves the original
                # "fire_pickup_zero_rows" failure mode semantically.
                log.warning(
                    "[α-fix] FirePickup offer not found in offer_history "
                    "for offer_id=%s",
                    action.offer_id,
                )
                return False, "fire_pickup_zero_rows"
            already_fired_at = existing_row['actual_pickup_at']
            if already_fired_at is not None:
                # [Rule XV catch-up] Cache write was already captured by a
                # prior fire (typically a FirePickupObservation while
                # lost-mode was active). Bind narrative, snapshot driver
                # nail-state, acquire the §XVI.G lock — but DO NOT
                # overwrite the cache (Rule XV invariant: preserve the
                # observation's location data; narrative is secondary).
                write_nailed_position(cur, driver_id, 'pickup',
                                      nail_lat, nail_lng, 0)
                queue.bind(action.offer_id, cur)
                acquire_lock(
                    cur=cur,
                    driver_id=driver_id,
                    offer_id=action.offer_id,
                    pudo_type='pickup',
                    fired_at=datetime.datetime.now(datetime.timezone.utc),
                    fired_lat=nail_lat,
                    fired_lng=nail_lng,
                    fired_cumulative_miles=cumulative_miles,
                )
                log.info(
                    "[Rule XV catch-up] FirePickup offer=%s: cache "
                    "preserved (prior fire at %s), narrative bound",
                    action.offer_id, already_fired_at,
                )
                return True, None

            # Normal path: first fire for this offer. Cache writes +
            # narrative bind together.
            write_nailed_position(cur, driver_id, 'pickup',
                                  nail_lat, nail_lng, 0)
            queue.bind(action.offer_id, cur)

            # [α-fix] pickup_market_signals: actual_pickup_* + nail_it elevation.
            # pms.offer_id REFERENCES decision_log(id), but action.offer_id is
            # offer_history.id; translate via subquery at schema boundary.
            #
            # [Rule XV 2026-05-30] AND actual_pickup_at IS NULL — defense-
            # in-depth. The SELECT above proved offer_history's actual_pickup_at
            # is NULL in this transaction, but the guard also makes the UPDATE
            # idempotent under race conditions and makes the symmetry with
            # FirePickupObservation's idempotency contract (driver_heartbeat.py
            # FPO handler) explicit at the call site.
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
                  AND actual_pickup_at IS NULL
            """, (nail_lat, nail_lng, nail_lat, nail_lng, action.offer_id))

            # [α-fix] offer_history: canonical PUDO record. action.offer_id
            # IS offer_history.id, so target it directly.
            #
            # [Rule XV 2026-05-30] AND actual_pickup_at IS NULL — same
            # idempotency contract FirePickupObservation has advertised since
            # the α-fix (see FPO handler comment "subsequent ... for the same
            # offer is a no-op"). Without this guard, a FirePickup arriving
            # AFTER a FirePickupObservation overwrites the Observation's
            # correct cache write with whatever location the current
            # heartbeat is at, violating Rule XV.
            #
            # [error-metric 2026-06-02] pickup_error_m from intended pickup
            # coords (fetched by the catch-up SELECT above, widened to
            # include pickup_lat/lng) vs the fired nail point. None-guarded:
            # missing intended coords → NULL written, no raise.
            intended_lat = existing_row['pickup_lat']
            intended_lng = existing_row['pickup_lng']
            if intended_lat is not None and intended_lng is not None:
                error_m = haversine_meters(intended_lat, intended_lng,
                                           nail_lat, nail_lng)
            else:
                error_m = None
            cur.execute("""
                UPDATE app_private.offer_history
                SET actual_pickup_lat                   = %s,
                    actual_pickup_lng                   = %s,
                    actual_pickup_h3                    = app_private.coords_to_h3(%s, %s)::text,
                    actual_pickup_at                    = NOW(),
                    pickup_classification               = 'auto',
                    pickup_data_source                  = 'nail_it',
                    leg_start_cumulative_miles_dropoff  = %s,
                    cumulative_miles_at_pickup_fire     = %s,
                    expected_dropoff_distance           = %s + COALESCE(trip_miles, 0),
                    expected_dropoff_arrival_time       = NOW() + (COALESCE(trip_minutes, 0)::text || ' minutes')::interval,
                    pickup_error_m                      = %s
                WHERE id = %s::bigint
                  AND actual_pickup_at IS NULL
            """, (
                nail_lat, nail_lng, nail_lat, nail_lng,
                cumulative_miles, cumulative_miles, cumulative_miles,
                error_m,
                action.offer_id,
            ))
            if cur.rowcount == 0:
                # Should not happen: the SELECT above proved this offer
                # exists AND actual_pickup_at IS NULL in the same
                # transaction. Zero rows here means a race or transaction
                # anomaly — log loudly and fail.
                log.warning(
                    "[α-fix] FirePickup offer_history UPDATE wrote 0 rows "
                    "for offer_id=%s after SELECT confirmed actual_pickup_at "
                    "IS NULL — race or transactional anomaly",
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

            # §XVI.G: latch the transaction lock on this fire so the
            # matcher won't re-emit FirePickup for the same offer until
            # the driver moves 500ft OR sustains >5mph for 10s.
            acquire_lock(
                cur=cur,
                driver_id=driver_id,
                offer_id=action.offer_id,
                pudo_type='pickup',
                fired_at=datetime.datetime.now(datetime.timezone.utc),
                fired_lat=nail_lat,
                fired_lng=nail_lng,
                fired_cumulative_miles=cumulative_miles,
            )
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
            #
            # [error-metric 2026-06-02] Compute dropoff_error_m from the
            # intended (offer-time) dropoff coords vs the fired nail point.
            # None-guarded: missing intended coords → NULL written, no raise.
            cur.execute("""
                SELECT dropoff_lat, dropoff_lng FROM app_private.offer_history
                WHERE id = %s::bigint
            """, (action.offer_id,))
            row = cur.fetchone()
            intended_lat = row['dropoff_lat'] if row else None
            intended_lng = row['dropoff_lng'] if row else None
            if intended_lat is not None and intended_lng is not None:
                error_m = haversine_meters(intended_lat, intended_lng,
                                           nail_lat, nail_lng)
            else:
                error_m = None
            cur.execute("""
                UPDATE app_private.offer_history
                SET actual_dropoff_lat               = %s,
                    actual_dropoff_lng               = %s,
                    actual_dropoff_h3                = app_private.coords_to_h3(%s, %s)::text,
                    actual_dropoff_at                = NOW(),
                    dropoff_classification           = 'auto',
                    cumulative_miles_at_dropoff_fire = %s,
                    dropoff_error_m                  = %s
                WHERE id = %s::bigint
            """, (
                nail_lat, nail_lng, nail_lat, nail_lng,
                cumulative_miles,
                error_m,
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

            # §XVI.G: latch dropoff lock and release any orphan locks
            # for this offer. Once an offer's dropoff fires, it leaves
            # the live queue per §XIV.H, so any pickup-leg lock that
            # didn't release spatially is cleaned up here.
            acquire_lock(
                cur=cur,
                driver_id=driver_id,
                offer_id=action.offer_id,
                pudo_type='dropoff',
                fired_at=datetime.datetime.now(datetime.timezone.utc),
                fired_lat=nail_lat,
                fired_lng=nail_lng,
                fired_cumulative_miles=cumulative_miles,
            )
            release_lock(cur, driver_id, action.offer_id, 'pickup')
            release_lock(cur, driver_id, action.offer_id, 'dropoff')
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

            # [§XVIII cold-start bind — 2026-05-31] When the heartbeat's
            # alive-unpicked set contains EXACTLY this fire's offer and
            # nothing else, the queue's narrative is unambiguous — there
            # is one and only one offer waiting on a pickup observation,
            # and that observation is happening right now. Bind
            # current_offer_id so the next heartbeat's lost-mode check
            # flips False (bit 2 clears once this offer_history UPDATE
            # writes actual_pickup_at), allowing normal narrative
            # routing to resume.
            #
            # Sampling BEFORE the offer_history UPDATE below is the
            # whole point: at this moment the firing offer is still
            # in the alive-unpicked set, so the singleton-match gate
            # reads as the brief's literal phrasing ("exactly ONE
            # alive unpicked offer"). After the UPDATE, action.offer_id
            # would be excluded and the gate would invert; that was
            # the alternative phrasing in recon §Q3's sub-finding,
            # rejected in favor of this one for reader clarity.
            #
            # The bind is ADDITIVE. Cache writes (pms above,
            # offer_history below, community_offers further down) and
            # the §XVI.G lock all run regardless. Rule XV preserved:
            # the Observation's role as cache populator is unchanged;
            # this only adds the narrative bind in the unambiguous case.
            #
            # The gate uses frozenset equality (not membership) so it
            # falsifies cleanly whenever any other offer is also
            # competing for *this* piece of asphalt — count>=2 stays
            # ambiguous, count==0 means this fire is a no-op redo
            # (idempotency case) and must not bind.
            #
            # 2026-06-01 spatial-local sharpening (FIX_PROPOSAL_BIND_
            # SPATIAL_LOCAL): the singleton test now runs against the
            # *local competing set* — offers that both (a) cleared the
            # WAI pickup-leg floor at this heartbeat via _commits AND
            # (b) are alive-unpicked. Pre-fix the gate measured global
            # ambiguity ("any other alive-unpicked offer anywhere on
            # the clipboard"), withholding the bind whenever earlier-
            # but-cross-town offers existed even though they scored
            # well below floor (2026-06-01 morning recon, H-A: 1 of 7
            # fires bound under the global gate). Local ambiguity is
            # the right unit — measured here as "more than one offer
            # cleared the pickup floor at this heartbeat and is still
            # waiting on a pickup observation."
            #
            # The intersection with alive_unpicked_offer_ids retires
            # already-picked offers: even if WAI keeps re-scoring a
            # retired pickup leg (the 2026-06-01 §4 anomaly), an
            # already-picked offer is excluded from local_competing
            # and cannot block a fresh bind on a different offer.
            #
            # No geometric / radius / H3 test runs here — locality
            # emerges from the WAI score, which already fuses spatial
            # signals correctly. Introducing a distance gate would
            # violate §XVI.D (distance-to-geocode is never a gate).
            #
            # See docs/RECON_LOST_MODE_COLD_START_TRAP_2026-05-31.md
            # for the original cold-start diagnosis, docs/RECON_PICKUP_
            # NOBIND_2026-06-01.md for the H-A ratification, and
            # docs/FIX_PROPOSAL_BIND_SPATIAL_LOCAL_2026-06-01.md for
            # the Gemini-ratified design (§1.1, §1.3, §4.1).
            local_competing = pickup_floor_clearers & alive_unpicked_offer_ids
            if local_competing == frozenset({str(action.offer_id)}):
                queue.bind(action.offer_id, cur)
                log.info(
                    "[§XVIII spatial-local bind] FirePickupObservation "
                    "offer=%s narrative bound (sole pickup-floor-clearer "
                    "among alive-unpicked: %s)",
                    action.offer_id, sorted(local_competing),
                )

            # [α-fix mirror] offer_history: per-offer observation record. The
            # WHERE actual_pickup_at IS NULL guard makes this idempotent — a
            # subsequent FirePickupObservation for the same offer is a no-op.
            #
            # [error-metric 2026-06-02] Compute pickup_error_m from the
            # intended (offer-time) pickup coords vs the fired nail point.
            # None-guarded: missing intended coords → NULL written, no raise.
            cur.execute("""
                SELECT pickup_lat, pickup_lng FROM app_private.offer_history
                WHERE id = %s::bigint
            """, (action.offer_id,))
            row = cur.fetchone()
            intended_lat = row['pickup_lat'] if row else None
            intended_lng = row['pickup_lng'] if row else None
            if intended_lat is not None and intended_lng is not None:
                error_m = haversine_meters(intended_lat, intended_lng,
                                           nail_lat, nail_lng)
            else:
                error_m = None
            cur.execute("""
                UPDATE app_private.offer_history
                SET actual_pickup_lat                   = %s,
                    actual_pickup_lng                   = %s,
                    actual_pickup_h3                    = app_private.coords_to_h3(%s, %s)::text,
                    actual_pickup_at                    = NOW(),
                    pickup_classification               = 'auto_observation',
                    pickup_data_source                  = 'nail_it',
                    leg_start_cumulative_miles_dropoff  = %s,
                    cumulative_miles_at_pickup_fire     = %s,
                    expected_dropoff_distance           = %s + COALESCE(trip_miles, 0),
                    expected_dropoff_arrival_time       = NOW() + (COALESCE(trip_minutes, 0)::text || ' minutes')::interval,
                    pickup_error_m                      = %s
                WHERE id = %s::bigint
                  AND actual_pickup_at IS NULL
            """, (
                nail_lat, nail_lng, nail_lat, nail_lng,
                cumulative_miles, cumulative_miles, cumulative_miles,
                error_m,
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

            # §XVI.G: latch the transaction lock on this observation
            # fire. Observation-class fires use the same lock as
            # narrative-class fires — the matcher should not re-emit
            # observations for the same (offer, leg) until release.
            acquire_lock(
                cur=cur,
                driver_id=driver_id,
                offer_id=action.offer_id,
                pudo_type='pickup',
                fired_at=datetime.datetime.now(datetime.timezone.utc),
                fired_lat=nail_lat,
                fired_lng=nail_lng,
                fired_cumulative_miles=cumulative_miles,
            )
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
            #
            # [error-metric 2026-06-02] Compute dropoff_error_m from the
            # intended (offer-time) dropoff coords vs the fired nail point.
            # None-guarded: missing intended coords → NULL written, no raise.
            cur.execute("""
                SELECT dropoff_lat, dropoff_lng FROM app_private.offer_history
                WHERE id = %s::bigint
            """, (action.offer_id,))
            row = cur.fetchone()
            intended_lat = row['dropoff_lat'] if row else None
            intended_lng = row['dropoff_lng'] if row else None
            if intended_lat is not None and intended_lng is not None:
                error_m = haversine_meters(intended_lat, intended_lng,
                                           nail_lat, nail_lng)
            else:
                error_m = None
            cur.execute("""
                UPDATE app_private.offer_history
                SET actual_dropoff_lat               = %s,
                    actual_dropoff_lng               = %s,
                    actual_dropoff_h3                = app_private.coords_to_h3(%s, %s)::text,
                    actual_dropoff_at                = NOW(),
                    dropoff_classification           = 'auto_observation',
                    cumulative_miles_at_dropoff_fire = %s,
                    dropoff_error_m                  = %s
                WHERE id = %s::bigint
                  AND actual_dropoff_at IS NULL
            """, (
                nail_lat, nail_lng, nail_lat, nail_lng,
                cumulative_miles,
                error_m,
                action.offer_id,
            ))

            # §XVI.G: latch dropoff observation lock and release any
            # orphan locks for this offer. Same cleanup semantics as
            # FireDropoff — once dropoff observation fires, the offer
            # leaves the live queue.
            acquire_lock(
                cur=cur,
                driver_id=driver_id,
                offer_id=action.offer_id,
                pudo_type='dropoff',
                fired_at=datetime.datetime.now(datetime.timezone.utc),
                fired_lat=nail_lat,
                fired_lng=nail_lng,
                fired_cumulative_miles=cumulative_miles,
            )
            release_lock(cur, driver_id, action.offer_id, 'pickup')
            release_lock(cur, driver_id, action.offer_id, 'dropoff')
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


def _get_last_known_anchor_id(cur, driver_id, current_cumulative_miles, reference_time, last_odometer_move_at=None):
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
        (driver_id,) + live_offer_predicate_params(current_cumulative_miles, reference_time, last_odometer_move_at),
    )
    row = cur.fetchone()
    return str(row["id"]) if row else None


def _get_alive_unpicked_offer_ids(cur, driver_id, current_cumulative_miles,
                                  reference_time, last_odometer_move_at=None):
    """Return the set of offer_history.id values that are predicate-alive
    AND have no pickup observation recorded yet.

    Single canonical definition of the §XVIII bit-2 predicate, factored
    out 2026-05-31 so the cold-start bind in the FirePickupObservation
    handler can read the same set that _detect_lost_mode evaluates
    against (recon: RECON_LOST_MODE_COLD_START_TRAP_2026-05-31.md §2.1
    no-hand-rolled-predicate constraint). _detect_lost_mode delegates
    to this helper, so the predicate has exactly one body.

    The set composition mirrors _detect_lost_mode's prior query:
    LIVE_OFFER_PREDICATE_SQL (the canonical horizon predicate from
    driver_queue.py) + `AND oh.actual_pickup_at IS NULL`. No new
    filters, no divergent definition of "alive."

    Args:
        cur: psycopg2 cursor.
        driver_id: Firebase UID.
        current_cumulative_miles: float or None. When None, the distance
            axis of LIVE_OFFER_PREDICATE_SQL short-circuits to TRUE
            (time-only fallback). Production heartbeat path always supplies.
        reference_time: UTC datetime; the heartbeat's reference point for
            horizon evaluation.
        last_odometer_move_at: UTC datetime or None; the staleness-gate
            anchor. Permissive on NULL.

    Returns frozenset[str] of offer_history.id values (string-typed
    to match action.offer_id at call sites that compare them).
    """
    cur.execute(
        f"""
        SELECT oh.id
        FROM app_private.offer_history oh
        JOIN app_private.decision_log dl ON dl.id = oh.decision_log_id
        WHERE dl.driver_id = %s
          AND oh.actual_pickup_at IS NULL
          AND {LIVE_OFFER_PREDICATE_SQL}
        """,
        (driver_id,)
        + live_offer_predicate_params(current_cumulative_miles, reference_time, last_odometer_move_at),
    )
    return frozenset(str(row["id"]) for row in cur.fetchall())


def _detect_lost_mode(cur, driver_id, queue_offer_ids, current_cumulative_miles, reference_time, last_odometer_move_at=None):
    """Detect driver-state lost-mode per §XVIII.

    Driver-state lost-mode is the natural operating state of a PUDO
    system whose narrative is broken. Per §XVIII.A, the trigger is two
    bits derived (not stored):

        bit 1: current_offer_id IS NULL    (checked by caller)
        bit 2: queue contains an offer with actual_pickup_at IS NULL
               AND predicate-alive (this function returns this bit)

    The PUDO infrastructure is advice-blind per §0.B and §XV. The car's
    physical position is the sole sensor of driver intent — accept/decline
    advice from the decision engine is not consulted here.

    Args:
        cur: psycopg2 cursor.
        driver_id: Firebase UID.
        queue_offer_ids: legacy parameter, unused under §XVIII. Retained
            for signature compatibility; removed in a future cleanup.
        current_cumulative_miles: float or None. When None, the distance
            axis of LIVE_OFFER_PREDICATE_SQL short-circuits to TRUE
            (time-only fallback). Production heartbeat path always supplies.
        reference_time: UTC datetime; the heartbeat's reference point for
            horizon evaluation.

    Returns True iff bit 2 holds — at least one queued offer is
    predicate-alive AND has no pickup observation recorded.

    Implementation note (2026-05-31): the predicate body was extracted
    to _get_alive_unpicked_offer_ids so the FirePickupObservation
    cold-start bind can read the same set this function evaluates. This
    function delegates rather than duplicating the query — single source
    of truth, no parallel-function drift surface.
    """
    _ = queue_offer_ids  # legacy parameter; unused under §XVIII (see docstring)
    return len(_get_alive_unpicked_offer_ids(
        cur, driver_id, current_cumulative_miles, reference_time, last_odometer_move_at,
    )) > 0


def _build_tad_decision_context(
    diagnostics, matches, lost_mode, last_known_anchor_id,
    executed_actions=None, queue_metadata=None,
    suppressed_contexts=None,  # §XVI.G recovery (2026-05-27)
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

    # §XVI.G recovery (2026-05-27): inject lock_suppressions on the dict
    # before serialization. Previously this lived in _log_decision_context
    # AFTER json.dumps had returned a string, producing TypeError on every
    # heartbeat that carried suppressed contexts. Ownership lives here now;
    # the builder owns the blob shape end-to-end.
    if suppressed_contexts:
        blob["lock_suppressions"] = [
            {
                "locked_offer_id": ctx.locked_offer_id,
                "locked_pudo_type": ctx.locked_pudo_type,
                "lock_age_s": ctx.lock_age_s,
                "release_metrics": {
                    "current_speed_streak_s": ctx.current_speed_streak_s,
                    "target_speed_streak_s": ctx.target_speed_streak_s,
                    "current_distance_delta_m": ctx.current_distance_delta_m,
                    "target_distance_delta_m": ctx.target_distance_delta_m,
                },
            }
            for ctx in suppressed_contexts
        ]

    return json.dumps(blob)


def _build_wai_per_offer_scores(diagnostics):
    """Serialize WAI's per_target_outcomes for forensic capture in
    pudo_decision_context.wai_per_offer_scores JSONB.

    Returns str (json.dumps output) or None.
      None: per_target_outcomes is empty (no offers in queue, WAI
        evaluation short-circuited, or cluster unavailable).
      str: one entry per (offer_id, leg) WAI scored, recording
        confidence, per-signal breakdown, target address, and reason.

    Closes the forensic gap surfaced by the 2026-05-30 starved-pickup
    recon (RECON_ROAD_NAME_CANONICALIZATION_GAP_2026-05-30.md §4.2):
    the existing wai_* flat columns only populate for the WINNING
    outcome of a heartbeat. Offers that scored below
    WAI_CONFIDENCE_THRESHOLD (or below the winner) left no trace,
    making wai_below_floor and lost_mode_no_candidate failure modes
    undiagnosable from PDC alone. This blob captures all losers.

    Per-signal breakdown shape mirrors _CONFIDENCE_WEIGHTS keys
    (proximity, breadcrumb_match, on_target_road, etc.). Reading the
    signals dict in a failed heartbeat shows immediately which signal
    zeroed out — distinguishing canonicalization gaps (road signals
    zero) from geocode-distance misses (proximity zero) from cluster
    weakness (cluster_tightness / cluster_duration low).
    """
    if not diagnostics.per_target_outcomes:
        return None
    return json.dumps([
        {
            "offer_id": offer_id,
            "leg": leg,
            "matched": outcome.matched,
            "confidence": outcome.confidence,
            "signals": outcome.signals,
            "target_address": outcome.target_address,
            "reason": outcome.reason,
        }
        for offer_id, leg, outcome in diagnostics.per_target_outcomes
    ])


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


def _coerce_odometer(raw):
    """Single coercion point for the hardware odometer payload.

    FINDING §5.7 / Step 4 (2026-06-05). Returns a float, or None. NEVER
    returns 0 for a missing/invalid reading — a fabricated 0 would produce a
    garbage expected_odometer in any band consumer (Gemini NULL-not-zero
    guard, ratified 2026-06-05). Genuine 0.0 and negative real readings pass
    through unchanged: this is a coercion point, not a validity gate. The
    band (Step 6) judges validity; the accessor only normalizes type.
    """
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _log_decision_context(
    cur, driver_id, body,
    current_lat, current_lng, speed_mph, gps_accuracy_m,
    current_offer_id,
    diagnostics, matches, executed_actions,
    dispatch_executed, dispatch_error_msg,
    lost_mode=False,                    # [3b.R]
    last_known_anchor_id=None,          # [3b.R]
    queue_metadata=None,                # [Phase 4]
    arrest_started_at_post=None,        # [Rule XVI B-2]
    arrest_counter_s_post=None,         # [Rule XVI B-2]
    phase_reached=None,                 # write-frozen per §XVI.C amendment
    matched_offer_id=None,              # [Rule XVI B-3]
    match_signal=None,                  # [Rule XVI B-3]
    matcher_candidates=None,            # [Rule XVI B-3]
    unmatched_reason=None,              # [Rule XVI B-3]
    cadence_target_hz=None,             # §XVI.C cadence hint (2026-05-24)
    suppressed_contexts=None,           # §XVI.G lock_suppressed forensic payload
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

    # [3b.R] Build TAD forensic blob (Bible Rule 5: JSONB-resident, never flat).
    # §XVI.G recovery (2026-05-27): suppressed_contexts now threaded into
    # the builder so lock_suppressions lands on the dict before json.dumps.
    tad_decision_context = _build_tad_decision_context(
        diagnostics, matches, lost_mode, last_known_anchor_id,
        executed_actions=executed_actions,
        queue_metadata=queue_metadata,
        suppressed_contexts=suppressed_contexts,
    )

    # Step 4 (FINDING §5.7, 2026-06-05): persist the point-in-time odometer
    # the GC gate saw this heartbeat. `actual_odometer` is the genuinely-
    # missing, genuinely-point-in-time scalar — no other copy exists anywhere
    # (offer_history `expected_*` anchors are MUTATED at pickup-fire, so they
    # cannot reconstruct the at-heartbeat odometer in general). Per-offer
    # `expected_odometer` logging is DEFERRED to Step 6, shaped by the band
    # consumer. schema_version=1 lets Step 6 extend this blob (per-offer
    # expected + the §5.5 deferred/active taxonomy) without ambiguity about
    # which rows predate the band. NULL-not-zero via _coerce_odometer.
    _actual_odo = _coerce_odometer(body.get('cumulative_miles'))
    _odometer_gate_result = json.dumps({
        "schema_version": 1,
        "actual_odometer": _actual_odo,
        "odometer_status": "present" if _actual_odo is not None else "missing",
    })

    # WAI per-offer signal-score forensic blob (2026-05-30): captures the
    # signals breakdown for every offer WAI scored, not just the winner.
    # Closes the diagnostic gap behind wai_below_floor /
    # lost_mode_no_candidate failure modes — see
    # docs/RECON_ROAD_NAME_CANONICALIZATION_GAP_2026-05-30.md §4.2.
    wai_per_offer_scores = _build_wai_per_offer_scores(diagnostics)

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

    # §XVII Patch 4 (2026-05-14): PDC forensic-column resolution + drift fix.
    #
    # Pre-Patch-4 binding (bug): poi_lookup_source <- top_outcome.poi_witness
    # poi_witness is a Head-1 witness string (e.g. "fuzzy:Pappadeaux"), NOT a
    # lookup-source string. The column was 100% NULL in production for 7 days
    # because Head 1 was dormant. Patch 3 brought Head 5 online; this block
    # routes Head 5's source string to the correct column at the same moment
    # forensic data starts arriving.
    #
    # Tie-break rule: Head 5 wins when its score >= Head 1's score (None as 0).
    # When Head 5 wins, all three PDC columns reflect Head 5's data. When
    # Head 1 wins (or no head fired), Head 1's score lands in poi_match_score
    # and cluster_poi_names lands in poi_top_names — same as before.
    #
    # TODO (post-Patch-4 cleanup): Head 1's witness (top_outcome.poi_witness)
    # is now unbound from any PDC column. A follow-up "PDC witness cleanup"
    # patch will add a dedicated poi_witness column or repurpose match_signal.
    # Production volume of Head 1 witnesses: ~65 rows in 7 days, previously
    # misfiled into poi_lookup_source.
    sem_score = top_outcome.semantic_anchor_score if top_outcome else None
    sem_witness = top_outcome.semantic_anchor_witness if top_outcome else None
    sem_source = top_outcome.semantic_lookup_source if top_outcome else None
    head1_score = top_outcome.poi_match if top_outcome else None

    head5_wins = (
        sem_score is not None
        and sem_score >= (head1_score or 0.0)
    )

    if head5_wins:
        pdc_poi_lookup_source = sem_source
        pdc_poi_match_score = sem_score
        if sem_witness:
            pdc_poi_top_names = [sem_witness] + list(diagnostics.cluster_poi_names or [])
        else:
            pdc_poi_top_names = diagnostics.cluster_poi_names or None
    else:
        pdc_poi_lookup_source = None
        pdc_poi_match_score = head1_score
        pdc_poi_top_names = diagnostics.cluster_poi_names or None

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
            cluster_started_at,
            poi_lookup_source, poi_match_score, poi_top_names,
            planner_action,
            dispatch_executed, dispatch_error,
            motion_gate_result, odometer_gate_result,
            gate_held_offer_ids, gate_held_legs,
            tad_decision_context,
            arrest_started_at, arrest_duration_s,
            phase_reached, matched_offer_id, match_signal,
            matcher_candidates, unmatched_reason,
            cadence_target_hz,
            wai_per_offer_scores
        ) VALUES (
            %s, %s, %s,
            %s, %s, %s, %s, %s, %s,
            %s, %s, %s,
            %s, %s, %s,
            %s, %s, %s,
            %s,
            %s, %s, %s, %s,
            %s,
            %s, %s, %s,
            %s,
            %s, %s,
            %s, %s,
            %s, %s,
            %s,
            %s, %s,
            %s, %s, %s,
            %s, %s,
            %s,
            %s
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
            cluster.started_at if cluster else None,
            pdc_poi_lookup_source,
            pdc_poi_match_score,
            pdc_poi_top_names,
            action_str,
            dispatch_executed,
            dispatch_error_msg,
            # Sprint A gate-layer columns (DEPRECATED 2026-05-17 — motion_gate
            # purged per §XIV.A Naked-List Contract. Three columns retained
            # in the schema for historical rows; new rows write NULL.
            # Schema-drop follow-up queued post-vocabulary-sweep.):
            # odometer_gate_result (slot 2) now carries the Step 4 forensic
            # blob (FINDING §5.7, 2026-06-05): point-in-time actual_odometer
            # + odometer_status. The other three slots remain NULL pending
            # schema drop. Positional order matches the column list:
            #   motion_gate_result, odometer_gate_result, gate_held_offer_ids, gate_held_legs
            None,
            _odometer_gate_result,
            None,
            None,
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
            cadence_target_hz,
            wai_per_offer_scores,
        ),
    )


# =============================================================================
# Orchestrator: the Quarterback
# =============================================================================

def _voice_for_actions(
    executed_actions,
    last_voiced_offer_id=None,
    last_voiced_action_type=None,
):
    """Map an executed-action list to a single voice utterance.

    Voice is suppressed unless a STATE-CHANGING or OBSERVATION action
    ran. State-changing actions (FirePickup / FireDropoff) are the
    narrative path; Observation actions (FirePickupObservation /
    FireDropoffObservation) are the §XVIII lost-mode path. Detection-only
    actions (LogAmbiguousMatch, LogNoMatch, LogPickupRematch) never voice.

    §XVIII LOST-MODE EXTENSION (2026-05-27): per Andrew + Claude Code's
    diagnosis, the production system is permanently in lost-mode due to
    a 272-offer unfired-pickup backlog. §XVIII demotes every PUDO fire
    to its Observation variant. Without matching Observation variants
    here, voice goes silent entirely.

    Wording (per Andrew, 2026-05-27): identical voice for narrative and
    observation variants. The driver should hear "Pickup confirmed" or
    "Dropoff confirmed" without needing to distinguish architectural state.

    Priority handles the implicit-cancel pair: when both
    FireDropoff(outcome="canceled") and FirePickup execute on the same
    heartbeat, the cancel string wins and suppresses the redundant
    "Pickup confirmed".

    DEDUP GATE (2026-05-27): Observation variants fire multiple times
    per (offer, leg) within a short window. Without dedup the driver
    would hear the same utterance 2-3+ times per real PUDO event. The
    last_voiced_offer_id / last_voiced_action_type kwargs hold the
    prior voiced tuple (read from driver_trip_state by the caller).
    When the candidate (offer_id, action_type) equals the prior, voice
    is suppressed.

    Returns:
      tuple[Optional[str], Optional[str], Optional[str]]:
        (voice_string, voiced_offer_id, voiced_action_type)
      All three are None when no voice should be emitted (either no
      voice-worthy action present, or dedup gate suppressed). When
      voice fires, the caller persists the two non-None ID strings to
      driver_trip_state for the next heartbeat's dedup check.
    """
    candidate = None  # tuple of (voice_base, offer_id, action_type)

    # 1. Implicit cancel — paired FireDropoff(canceled) + FirePickup
    for action in executed_actions:
        if isinstance(action, FireDropoff) and action.outcome == "canceled":
            candidate = ("Implicit cancel, new ride starting",
                         action.offer_id, "cancel")
            break

    # 2. Pickup missed (Case F)
    if candidate is None:
        for action in executed_actions:
            if isinstance(action, FireDropoff) and action.outcome == "pickup_missed":
                candidate = ("Dropoff confirmed, pickup was missed",
                             action.offer_id, "dropoff_missed_pickup")
                break

    # 3. Normal dropoff (narrative)
    if candidate is None:
        for action in executed_actions:
            if isinstance(action, FireDropoff) and action.outcome is None:
                candidate = ("Dropoff confirmed", action.offer_id, "dropoff")
                break

    # 4. Dropoff observation (§XVIII lost-mode demotion path)
    if candidate is None:
        for action in executed_actions:
            if isinstance(action, FireDropoffObservation):
                candidate = ("Dropoff confirmed", action.offer_id, "dropoff")
                break

    # 5. Pickup (narrative)
    if candidate is None:
        for action in executed_actions:
            if isinstance(action, FirePickup):
                candidate = ("Pickup confirmed", action.offer_id, "pickup")
                break

    # 6. Pickup observation (§XVIII lost-mode demotion path)
    if candidate is None:
        for action in executed_actions:
            if isinstance(action, FirePickupObservation):
                candidate = ("Pickup confirmed", action.offer_id, "pickup")
                break

    if candidate is None:
        return (None, None, None)

    voice_base, offer_id, action_type = candidate

    # Dedup gate: same (offer_id, action_type) as last voiced → suppress.
    # §IV-v2 (2026-05-29): emit forensic log line so Cloud Run captures
    # every suppression event for post-drive audit.
    if (offer_id == last_voiced_offer_id
            and action_type == last_voiced_action_type):
        log.info(
            "VOICE_DEDUP_SUPPRESSED candidate_offer=%s candidate_action=%s "
            "prior_offer=%s prior_action=%s",
            offer_id, action_type,
            last_voiced_offer_id, last_voiced_action_type,
        )
        return (None, None, None)

    # §IV-v2 (2026-05-29): append offer_id to every voice utterance so
    # the driver can disambiguate which offer the system is announcing.
    # Format: "<base>, <offer_id>" (Option 2 — natural English with
    # comma pause). Forensic log line records exact emitted voice.
    voice_str = f"{voice_base}, {offer_id}"
    log.info(
        "VOICE_EMITTED voice=%r offer=%s action=%s",
        voice_str, offer_id, action_type,
    )
    return (voice_str, offer_id, action_type)


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

    # §XVI.G recovery (2026-05-27): function-scope binding ensures the
    # _log_decision_context call site (which runs on every heartbeat,
    # including the many that bypass the matcher block) always sees a
    # bound `suppressed_contexts`. The matcher block overwrites this
    # with actual LockContext entries when arrest-driven candidate
    # evaluation runs.
    suppressed_contexts = []

    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    # ── LOAD ─────────────────────────────────────────────────────────
    # DriverQueue.snapshot() applies the L-19 invariant on read AND
    # actively reconciles the DB pointer when the stale bound offer is
    # definitively dead (2026-05-31). Two outcomes when bound_offer_id
    # points outside the projected queue:
    #
    #   - Dead (terminated dropoff OR >4h abandoned): snapshot() issues
    #     an id-guarded UPDATE clearing current_offer_id, logs INFO
    #     [reconciled stale current_offer_id]. Fixes the offer 8585 /
    #     8657 class — pickup fired, dropoff never fired (lost-mode,
    #     GC-reaped dropoff leg), pointer dangled across shifts
    #     because "next bind/unbind" never arrived in lost-mode.
    #
    #   - Transient (offer present + recent, OR row absent mid-
    #     ingestion): snapshot() preserves the DB pointer, logs
    #     WARNING. Closes the race condition that motivated keeping
    #     the original L-19 read-only design.
    #
    # Either way, snap.bound_offer_id is None for downstream consumers
    # (the in-memory hint always self-heals). FirePickup/FireDropoff
    # naturally overwrite the DB pointer when they fire in the future.
    queue = DriverQueue(driver_id, target_spec_builder=_bucket_to_target_spec)
    # §XIV.H Odometer-Staleness Gate (2026-05-19): pre-fetch the prior
    # cumulative_miles and last_odometer_move_at from the row that's about
    # to be UPDATEd. Computes effective_last_move for THIS tick — if the
    # odometer just moved, treat the offer-liveness staleness as alive
    # now (not the prior stale timestamp), preventing the "killed on the
    # revive tick" race. The SQL UPDATE at line 1192 commits the same
    # logic atomically via CASE; the two are equivalent by construction.
    _heartbeat_now = datetime.datetime.now(datetime.timezone.utc)
    cur.execute("""
        SELECT last_odometer_move_at,
               (heartbeat->>'cumulative_miles')::numeric AS prior_cum
        FROM app_private.driver_trip_state
        WHERE driver_id = %s
    """, (driver_id,))
    _pre = cur.fetchone()
    if _pre is not None:
        _prior_cum = float(_pre['prior_cum']) if _pre['prior_cum'] is not None else None
        _prior_last_move = _pre['last_odometer_move_at']
    else:
        _prior_cum = None
        _prior_last_move = None

    if (cumulative_miles is not None and _prior_cum is not None
            and cumulative_miles != _prior_cum):
        effective_last_move = _heartbeat_now
    else:
        effective_last_move = _prior_last_move

    snap = queue.snapshot(cur, current_cumulative_miles=cumulative_miles, last_odometer_move_at=effective_last_move)
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
            -- §XIV.H Odometer-Staleness Gate atomic maintenance.
            -- IS DISTINCT FROM handles NULL transitions, resets (cumulative
            -- miles drops to 0 on Android offer_accepted), and numeric
            -- changes. Bumps timestamp on any movement, preserves on freeze.
            last_odometer_move_at = CASE
                WHEN heartbeat IS NULL OR (heartbeat->>'cumulative_miles') IS NULL THEN NOW()
                WHEN %s::numeric IS DISTINCT FROM (heartbeat->>'cumulative_miles')::numeric THEN NOW()
                ELSE COALESCE(last_odometer_move_at, NOW())
            END,
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
            END,
            -- §XVI.G Transaction Lock temporal release substrate.
            -- Parallel structure to arrest_counter_s but inverted:
            -- tracks contiguous seconds above 5.0 mph. NULL/0.0 means
            -- "not currently above threshold" (mirrors arrest's
            -- semantics for the opposite condition). speed_mph IS NULL
            -- is treated as "below threshold" (NULL > 5.0 is false) so
            -- missing-data heartbeats reset, consistent with arrest.
            speed_streak_started_at = CASE
                WHEN %s::real > 5.0 AND speed_streak_started_at IS NULL
                    THEN NOW()
                WHEN %s::real > 5.0 AND speed_streak_started_at IS NOT NULL
                    THEN speed_streak_started_at
                ELSE NULL
            END,
            current_speed_streak_s = CASE
                WHEN %s::real > 5.0 AND speed_streak_started_at IS NOT NULL
                    THEN EXTRACT(EPOCH FROM (NOW() - speed_streak_started_at))::real
                WHEN %s::real > 5.0 AND speed_streak_started_at IS NULL
                    THEN 0.0
                ELSE 0.0
            END
        WHERE driver_id = %s
        RETURNING arrest_started_at, arrest_counter_s, last_odometer_move_at,
                  current_speed_streak_s
    """, (json.dumps({
        "lat": current_lat,
        "lng": current_lng,
        "speed_mph": speed_mph,
        "gps_accuracy_m": gps_accuracy_m,
        "cumulative_miles": cumulative_miles,
        "received_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }), cumulative_miles,
        speed_mph, speed_mph, speed_mph, speed_mph,           # arrest CASE binds
        speed_mph, speed_mph, speed_mph, speed_mph,           # §XVI.G speed-streak CASE binds
        driver_id))

    # Capture the post-UPDATE counter state to thread into _log_decision_context.
    # Defensive: if the driver row doesn't exist (shouldn't happen post-LOAD
    # in the same transaction, but guard anyway), default to None/None.
    _arrest_row = cur.fetchone()
    if _arrest_row is not None:
        # RealDictCursor: iteration yields KEYS not values; use key access.
        arrest_started_at_post = _arrest_row['arrest_started_at']
        arrest_counter_s_post = _arrest_row['arrest_counter_s']
        # §XVI.G: speed-streak counter for the temporal release condition.
        current_speed_streak_s = _arrest_row['current_speed_streak_s'] or 0.0
    else:
        arrest_started_at_post, arrest_counter_s_post = None, None
        current_speed_streak_s = 0.0

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
               current_offer_id, cumulative_miles)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (
            driver_id, current_lat, current_lng, speed_mph, gps_accuracy_m,
            current_offer_id, cumulative_miles,
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
    last_known_anchor_id = _get_last_known_anchor_id(cur, driver_id, cumulative_miles, _heartbeat_now, effective_last_move)
    # §XVIII bit-2 evaluation. Compute the alive-unpicked offer set
    # ONCE and derive lost_mode from it; pass the set through to
    # _execute_action so the FirePickupObservation cold-start bind
    # (driver_heartbeat.py FPO handler, 2026-05-31) reads the same
    # source-of-truth — no parallel query, no possibility of drift.
    alive_unpicked_offer_ids = _get_alive_unpicked_offer_ids(
        cur, driver_id, cumulative_miles, _heartbeat_now, effective_last_move,
    )
    lost_mode = len(alive_unpicked_offer_ids) > 0

    wai = WhereAmI(cur)
    matches, diagnostics = wai.evaluate_with_diagnostics(
        driver_id, snap.offers,
        per_offer_state=per_offer_state,
        lost_mode=lost_mode,
        last_known_anchor_id=last_known_anchor_id,
        current_odometer=cumulative_miles,
    )

    # §XVIII spatial-local bind precompute (2026-06-01, FIX_PROPOSAL_
    # BIND_SPATIAL_LOCAL §1.1 + §4.1). Compute the set of offers that
    # cleared the WAI pickup-leg floor at THIS heartbeat — the
    # candidate "local competing set" for the §XVIII bind gate inside
    # the FirePickupObservation handler. Delegates to _commits (the
    # canonical floor predicate, where_am_i.py:1790-1837) per §1.4 so
    # any future floor or POI-lift change flows automatically.
    #
    # Computed ONCE here, in the heartbeat body where diagnostics +
    # tad_verdicts both live (Gemini §4.1: compute-once-and-thread,
    # mirroring alive_unpicked_offer_ids). The finished frozenset is
    # passed into _execute_action as a kwarg; the handler does not
    # see the raw per_target_outcomes / tad_verdicts structures.
    #
    # The intersection with alive_unpicked_offer_ids happens at the
    # gate site (line ~620) so that an already-picked offer whose
    # pickup leg WAI re-scores (the 2026-06-01 §4 anomaly) cannot
    # inflate local_competing and block a fresh bind on a different
    # offer. See proposal §1.3.
    pickup_floor_clearers = frozenset(
        str(offer_id)
        for offer_id, leg, outcome in diagnostics.per_target_outcomes
        if leg == 'pickup'
        and _commits(outcome, diagnostics.tad_verdicts.get(offer_id))
    )

    # ── MATCH (Rule XVI B-3 Active Interrogation) ────────────────────
    # Forensic Ladder. matcher_actions is None when the matcher
    # abstains (arrest < threshold OR all candidates rejected) — in
    # those cases the lazy dispatch path retains agency below.
    phase_reached = None  # write-frozen per §XVI.C amendment (2026-05-22)
    matched_offer_id = None
    match_signal = None
    matcher_candidates = []
    unmatched_reason = None
    matcher_actions = None

    # Phase 1 → Phase 2 perimeter: is any offer in its destination zone?
    # §XVIII.C.1: lost-mode bypasses TAD pre-filter (verdicts will all have
    # passed=null in lost-mode, which is correct abstention, not failure).
    if (lost_mode
            or any(v.distance_gate.get('passed') is True
                   for v in diagnostics.tad_verdicts.values())):
        phase_reached = None  # write-frozen per §XVI.C amendment (Phase economy collapsed)

    if (arrest_counter_s_post is not None
            and arrest_counter_s_post >= ARREST_DURATION_THRESHOLD_S):
        candidates = []  # list[(offer_id, leg, confidence)]

        # P15-final consolidated candidate loop (2026-05-19): the matcher's
        # source of truth is diagnostics.per_target_outcomes (WAI's spatial-
        # scoring results — populated for EVERY offer in the queue, both
        # legs per offer). TAD is CONSULTED, not iterated.
        #
        # Three invariants land here together:
        #
        #   (1) Iteration source = per_target_outcomes. Fixes the pre-P15
        #       matcher-blindness bug where empty tad_verdicts produced
        #       empty matcher_candidates despite live offers in the queue.
        #
        #   (2) §XVI.C (amended 2026-05-22): TAD verdict no longer
        #       gates candidates. The candidate loop adds any offer
        #       whose WAI confidence clears the floor; TAD's verdict
        #       is preserved only in tad_decision_context for
        #       forensic analysis.
        #
        for offer_id, leg, outcome in diagnostics.per_target_outcomes:
            if leg not in ('pickup', 'dropoff'):
                continue

            # WAI confidence floor: the canonical match signal per
            # §XVI.C (amended 2026-05-22). TAD verdict is consulted
            # only for forensic recording in tad_decision_context;
            # it does not gate candidates here.
            if outcome is None or outcome.confidence < WAI_CONFIDENCE_THRESHOLD:
                continue

            # §XVI.C (amended 2026-05-22): TAD is input to WAI's
            # confidence calculation, not a separate gate. An offer
            # with WAI ≥ WAI_CONFIDENCE_THRESHOLD is a candidate
            # regardless of TAD verdict. The §XVIII lost-mode bypass
            # framing is subsumed — there is no gate to bypass.
            #
            # The TAD verdict remains forensically valuable; it is
            # preserved in tad_decision_context JSONB per §V Flight
            # Recorder. Future analysis of "WAI fired but TAD said
            # no" cases informs WAI's calibration of TAD as a signal.

            candidates.append((offer_id, leg, outcome.confidence))

        # §XVI.G Transaction Lock: filter candidates that are currently
        # locked from a recent fire on the same (offer, leg). Locked
        # candidates produce a lock_suppressed PDC row instead of
        # executing redundant actions. Per Sprint A composite-key
        # design, locks are per-(offer, leg) — stacked offers maintain
        # independent lock state.
        suppressed_contexts = []  # list[LockContext] for forensic payload
        _unlocked_candidates = []
        _now_utc = datetime.datetime.now(datetime.timezone.utc)
        for offer_id, leg, conf in candidates:
            lock_ctx = is_locked(
                cur=cur,
                driver_id=driver_id,
                offer_id=offer_id,
                pudo_type=leg,
                current_lat=current_lat,
                current_lng=current_lng,
                current_cumulative_miles=cumulative_miles,
                current_speed_streak_s=current_speed_streak_s,
                now=_now_utc,
            )
            if lock_ctx is not None:
                suppressed_contexts.append(lock_ctx)
            else:
                _unlocked_candidates.append((offer_id, leg, conf))
        candidates = _unlocked_candidates

        matcher_candidates = [c[0] for c in candidates]

        if len(candidates) == 1:
            # Single-match express lane (Option β).
            offer_id, leg, _conf = candidates[0]
            # §XVIII.C.4: in lost-mode, demote narrative fires to
            # observation fires. The §5.3 path achieves this via
            # dispatch(lost_mode=True); the express lane bypasses
            # dispatch, so the demotion is applied here directly.
            # Mapping mirrors dispatch._demote_to_observation per
            # §XVIII.C.4 canonical text.
            if leg == 'pickup':
                action_cls = FirePickupObservation if lost_mode else FirePickup
            else:
                action_cls = FireDropoffObservation if lost_mode else FireDropoff
            matcher_actions = [action_cls(offer_id=offer_id)]
            matched_offer_id = offer_id
            # §XVI.C / §XVIII.D.1: lost-mode fires get canonical
            # lost_mode_observation label; non-lost fires use
            # wai_above_floor (post-2026-05-22 taxonomy; 'tad_and_wai'
            # deprecated when TAD became input, not gate).
            match_signal = 'lost_mode_observation' if lost_mode else 'wai_above_floor'
            # phase_reached: write-frozen per Decision 1 of §XVI.C
            # amendment (2026-05-22). Field deprecated; derive
            # equivalents from matched_offer_id, unmatched_reason,
            # cluster_lat populations.
            phase_reached = None
        elif len(candidates) >= 2:
            # §5.3 ambiguity — hand to dispatch (Option α).
            synth = [WAIMatch(offer_id=oid, location_type=lg, confidence=cf)
                     for oid, lg, cf in candidates]
            matcher_actions = dispatch(synth, current_offer_id, queue_metadata, lost_mode=lost_mode)
            # §XVIII.D.1: lost-mode ambiguous fires get canonical
            # lost_mode_ambiguous_observation; cold-mode retains dispatch_resolved.
            match_signal = 'lost_mode_ambiguous_observation' if lost_mode else 'dispatch_resolved'
            phase_reached = None  # write-frozen per §XVI.C amendment (success path)
            # Pull narrative winner from dispatch's action list (None for
            # §5.3-mirror ClearNarrative case where no fire occurs).
            for a in matcher_actions:
                if isinstance(a, (FirePickup, FireDropoff)):
                    matched_offer_id = a.offer_id
                    break
        else:
            # Arrest reached but no candidate passed both gates.
            match_signal = 'no_match'
            if suppressed_contexts:
                # §XVI.G: candidates cleared the WAI floor but were
                # caught by the transaction lock. Forensic payload
                # threaded into tad_decision_context.lock_suppressions
                # via _log_decision_context's suppressed_contexts kwarg.
                unmatched_reason = 'lock_suppressed'
            elif not diagnostics.tad_verdicts:
                # Bug B' 2026-05-18: the historical 'empty_queue' label
                # conflated four distinct precondition failures. Split
                # them so the next miss self-classifies. The WAI guard
                # at where_am_i.py:_evaluate requires
                #   `queue and per_offer_state is not None
                #    and current_odometer is not None`.
                # plus cluster non-None (Step 1 in _evaluate). Failure
                # of any of these produces tad_verdicts == {}.
                if not snap.offers:
                    unmatched_reason = 'queue_actually_empty'
                elif diagnostics.cluster is None:
                    unmatched_reason = 'cluster_unavailable'
                elif cumulative_miles is None:
                    unmatched_reason = 'odometer_unavailable'
                else:
                    # Bug B'-2: queue non-empty, cluster present,
                    # odometer present — TAD still didn't run. We could
                    # not localize this from code reading on 2026-05-18.
                    # Emit a WARNING so Cloud Run surfaces recurrences,
                    # and tag the row so we can find it forensically.
                    unmatched_reason = 'tad_skipped_unknown'
                    log.warning(
                        "[Bug B'-2] tad_skipped_unknown fired | "
                        "driver_id=%s | "
                        "snap.offers=%d offer_ids=%s | "
                        "per_offer_state_keys=%s | "
                        "cluster lat,lng=(%s, %s) size=%s duration_s=%s | "
                        "cumulative_miles=%s | lost_mode=%s | "
                        "please investigate this pudo_decision_context row.",
                        driver_id,
                        len(snap.offers), [o.offer_id for o in snap.offers],
                        sorted(per_offer_state.keys()) if per_offer_state else [],
                        diagnostics.cluster.median_lat if diagnostics.cluster else None,
                        diagnostics.cluster.median_lng if diagnostics.cluster else None,
                        diagnostics.cluster.n if diagnostics.cluster else None,
                        diagnostics.cluster.duration_s if diagnostics.cluster else None,
                        cumulative_miles, lost_mode,
                    )
            elif lost_mode:
                # §XVIII.D.2: lost-mode misses get canonical
                # lost_mode_no_candidate label.
                unmatched_reason = 'lost_mode_no_candidate'
            else:
                # §XVI.C (amended 2026-05-22): with TAD as input not
                # gate, the only non-lost miss reason at this point is
                # that no offer's WAI confidence cleared the floor.
                # 'tad_failed' is deprecated (collapsed into
                # 'wai_below_floor').
                unmatched_reason = 'wai_below_floor'
            # phase_reached write-frozen per §XVI.C amendment (2026-05-22)
            phase_reached = None

    # ── DECIDE ───────────────────────────────────────────────────────
    # Phase 2b (§XVI Forensic Ladder + §XVIII Driver-State Lost Mode) is
    # the canonical and only matcher. If it abstains, no action fires.
    # Per §XVIII.D.2 this is the correct lost_mode_no_candidate behavior;
    # the legacy lazy fallback that previously occupied this branch was
    # a Sprint A residual that fired phantom observations off a stale
    # gated_matches list (2026-05-17 offer 7938 incident — see
    # apply_purge_motion_gate_2026-05-17.py docstring).
    actions = matcher_actions if matcher_actions is not None else []

    # ── EXECUTE ──────────────────────────────────────────────────────
    cluster = diagnostics.cluster
    executed_actions: list = []
    dispatch_error_msg = None
    for action in actions:
        executed, err = _execute_action(
            action, cur, conn, driver_id, queue, cluster,
            cumulative_miles=cumulative_miles,
            fallback_lat=current_lat, fallback_lng=current_lng,
            alive_unpicked_offer_ids=alive_unpicked_offer_ids,
            pickup_floor_clearers=pickup_floor_clearers,
        )
        if executed:
            executed_actions.append(action)
        if err:
            dispatch_error_msg = err
            break  # halt on first error; LOG still records the attempt
    dispatch_executed = bool(executed_actions)

    # ── LOG ──────────────────────────────────────────────────────────
    try:
        # Compute cadence_target_hz BEFORE logging so the forensic row
        # captures the hint that was actually emitted to the client. §XVI.C
        # amendment (2026-05-22); persistence added 2026-05-24.
        #
        # Horny trigger: WAI ≥ 0.40 AND speed_mph < 5.0. Stateless per
        # heartbeat — no mode flag, no state machine, no exit conditions.
        _max_wai_confidence = max(
            (m.confidence for m in matches),
            default=0.0,
        )
        _horny = (
            _max_wai_confidence >= WAI_CONFIDENCE_THRESHOLD
            and speed_mph is not None
            and speed_mph < HORNY_SPEED_THRESHOLD_MPH
        )
        cadence_target_hz = 1.0 if _horny else 0.2

        _log_decision_context(
            cur, driver_id, body,
            current_lat, current_lng, speed_mph, gps_accuracy_m,
            current_offer_id,
            diagnostics, matches, executed_actions,
            dispatch_executed, dispatch_error_msg,
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
            cadence_target_hz=cadence_target_hz,              # §XVI.C forensic (2026-05-24)
            suppressed_contexts=suppressed_contexts,          # §XVI.G forensic (2026-05-26)
        )
    except Exception as e:
        # LOG failure must not break the heartbeat — the API contract is
        # liveness, not forensic completeness. Surface to logs.
        log.exception("[heartbeat] pudo_decision_context INSERT failed: %s", e)

    conn.commit()

    # ── RESPONSE ─────────────────────────────────────────────────────
    # cadence_target_hz was computed earlier (before _log_decision_context)
    # for forensic persistence per §XVI.C cadence column (2026-05-24).
    # The variable is still in scope here.
    response = {"ok": True, "cadence_target_hz": cadence_target_hz}
    # §IV recovery (2026-05-27): voice dedup state read from driver_trip_state
    # before _voice_for_actions, written back when voice fires. State persists
    # across container restarts per Andrew's directive (do the right thing).
    cur.execute(
        '''SELECT last_voiced_offer_id, last_voiced_action_type
           FROM app_private.driver_trip_state
           WHERE driver_id = %s''',
        (driver_id,),
    )
    _voice_row = cur.fetchone()
    _prior_voiced_offer_id = (
        _voice_row['last_voiced_offer_id'] if _voice_row else None
    )
    _prior_voiced_action_type = (
        _voice_row['last_voiced_action_type'] if _voice_row else None
    )

    voice, _voiced_offer_id, _voiced_action_type = _voice_for_actions(
        executed_actions,
        last_voiced_offer_id=_prior_voiced_offer_id,
        last_voiced_action_type=_prior_voiced_action_type,
    )

    if voice is not None:
        cur.execute(
            '''UPDATE app_private.driver_trip_state
               SET last_voiced_offer_id = %s,
                   last_voiced_action_type = %s
               WHERE driver_id = %s''',
            (_voiced_offer_id, _voiced_action_type, driver_id),
        )
    if voice is not None:
        response["voice"] = voice
    return jsonify(response), 200# rebuild 1777679926
