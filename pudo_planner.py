"""
pudo_planner.py — PLAN consumer of WhereAmI diagnostic output.

Top-level peer of where_am_i.py and cluster_detection.py. Phase F's
heartbeat loop assembles a DriverStateSnapshot, calls
WhereAmI.evaluate() to get a WhereAmIResult, then calls
PudoPlanner.consume() with both. PLAN returns a PlannerDecision;
EXECUTE acts on it.

Box discipline (per canonical rules):
  PLAN. Pure logic. Database-blind. No DB reads, no DB writes.
  All context required for decisions arrives in DriverStateSnapshot.
  All writes (state transitions, suspected_pudos INSERTs, offer_history
  UPDATEs) belong to EXECUTE.

Architectural rulings locked during Phase E Step 2 (Gemini paired-
programming consensus 2026-04-26):

  R1 (state-correction over coordinate-synthesis): when a dropoff fires
      successfully but the pickup was never confirmed (S33 "Calhoun"),
      PLAN emits reconcile_missed_pickup with a reconciliation_payload.
      EXECUTE updates offer_history truthfully (pickup_missed=true,
      leave actual_pickup_lat/lng NULL). PLAN does NOT synthesize a
      pickup at the cluster median.

  R2 (database-blind PLAN): no DB writes anywhere in this module.
      Even ghost-cache INSERTs go through the cache_ghost action with
      a ghost_insert_payload that EXECUTE acts on.

  R3 (coordinate-only proximity for STACKED): both fire_stacked_swap
      (S32) and fire_stacked_revert (S35) detect via comparing WAI's
      corrected_lat/lng against snapshot's pickup/dropoff coords. No
      address-string matching (TargetSpec.address is None until Phase F).

  R4 (TOML <-> Python verdict mapping): TOML uppercase verdicts map to
      Python lowercase actions. Established in S33: RECONCILE_MISSED_PICKUP
      <-> reconcile_missed_pickup.

Phase D scope (this is Phase E):
  - Phase D's WAI is the DIAGNOSE primitive. WAI is pure-read.
  - Phase E's PudoPlanner consumes WAI output. Pure logic.
  - Phase F (driver_heartbeat.py) wires WAI->Planner->EXECUTE per heartbeat.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from pudo_types import (
    DriverStateSnapshot,
    PlannerDecision,
    WhereAmIResult,
)


# ============================================================================
# Module constants
# ============================================================================

# Number of consecutive heartbeats WAI must report at_current_pudo with
# matching offer_id before PLAN fires. Three heartbeats at ~5s/heartbeat
# is ~15s of stable presence — enough to filter traffic-light pauses, brief
# enough to avoid the driver pulling away before fire.
N_HEARTBEATS_TO_FIRE: int = 3

# How long an armed candidate can sit without WAI reaffirming it before
# PLAN drops it. Heartbeat cadence drift, brief signal loss, etc.
CANDIDATE_STALE_SECONDS: int = 15

# Ghost-match radius. Mirrors WAI's GHOST_MATCH_RADIUS_M (Phase D Q8).
# When PLAN looks for a previously-cached suspected_pudo near WAI's
# corrected coords, this is the proximity threshold.
GHOST_MATCH_RADIUS_M: float = 50.0


# ============================================================================
# _DriverTemporalState — private per-driver memory
# ============================================================================

@dataclass
class _DriverTemporalState:
    """PLAN's memory of an armed candidate across heartbeats.

    Private to this module. Stored in PudoPlanner._state_store keyed by
    driver_id. Per Gemini ruling on Phase E architecture, in-memory dict
    is sufficient for v1.0 — pod restart loses ~15s of arming state, the
    driver re-arms on the next stable cluster.

    A driver with no armed candidate has no entry in _state_store.
    Cleared via PudoPlanner._clear_temporal_state(driver_id).
    """
    armed_at: datetime
    armed_offer_id: str
    armed_pudo_type: str         # "pickup" or "dropoff"
    heartbeat_count: int
    last_seen_status: str        # last WAI status that touched this candidate
    last_seen_at: datetime

    # Forensic trajectory: the last N WAI observations that touched this
    # candidate, oldest-first. Capped at OBSERVATION_WINDOW (=3, matching
    # N_HEARTBEATS_TO_FIRE so a fire decision logs its full triggering
    # sequence). Default empty tuple means "no observations yet" — used
    # at construction time before _advance_armed_state populates it.
    recent_observations: tuple = ()


# ============================================================================
# _LastWAIObservation — forensic snapshot of one heartbeat's WAI verdict
# ============================================================================

@dataclass(frozen=True)
class _LastWAIObservation:
    """A lightweight snapshot of one WAI observation, stored on
    _DriverTemporalState.recent_observations for forensic trajectories.

    Frozen so we can safely store these in tuples without aliasing
    surprises. The full WhereAmIResult is intentionally NOT stored —
    it's a 14-field dataclass containing Cluster, signals, etc.
    Storing a tuple of those per-driver costs kilobytes; this snapshot
    is ~40 bytes, captures what forensics actually need: the verdict,
    the confidence, the offer matched (if any), and where the driver
    was at the time.

    Per canonical rule "Distance over Geocode", corrected_lat/lng are
    the ground-truth physical location at observation time and are the
    most diagnostically valuable fields for shadow-mode replay.
    """
    status: str                       # WAI status: at_current_pudo, etc.
    confidence: float                 # WAI confidence score
    offer_id: Optional[str]           # Which offer matched (None if not)
    corrected_lat: Optional[float]    # Cluster median lat at observation
    corrected_lng: Optional[float]    # Cluster median lng at observation
    observed_at: datetime             # When this heartbeat happened


# Window size for _DriverTemporalState.recent_observations. Chosen to
# match N_HEARTBEATS_TO_FIRE so a fire decision's log includes the
# complete triggering sequence.
OBSERVATION_WINDOW: int = 3


# ============================================================================
# Section A — temporal pattern detection (pure functions)
# ============================================================================

def _make_observation(
    wai_result: WhereAmIResult,
    *,
    now: datetime,
) -> _LastWAIObservation:
    """Project a WhereAmIResult into a forensic snapshot.

    Pure function. No side effects. Used by _advance_armed_state to
    record the trajectory.
    """
    return _LastWAIObservation(
        status=wai_result.status,
        confidence=wai_result.confidence,
        offer_id=wai_result.offer_id,
        corrected_lat=wai_result.corrected_lat,
        corrected_lng=wai_result.corrected_lng,
        observed_at=now,
    )


def _is_stable_match(
    state: _DriverTemporalState,
    current_wai: WhereAmIResult,
    *,
    n_required: int,
) -> bool:
    """True when the armed candidate has been reaffirmed enough heartbeats.

    "Reaffirmed" means: WAI's current status is at_current_pudo with the
    same offer_id and the same pudo_type as what's armed, AND the
    heartbeat count has reached n_required.

    The actual count is updated by _advance_armed_state (which calls this
    helper after incrementing). _is_stable_match itself only inspects
    state.heartbeat_count — it does NOT mutate.
    """
    if current_wai.status != "at_current_pudo":
        return False
    if current_wai.offer_id != state.armed_offer_id:
        return False
    if current_wai.pudo_type != state.armed_pudo_type:
        return False
    return state.heartbeat_count >= n_required


def _is_brief_disappearance(
    state: _DriverTemporalState,
    current_wai: WhereAmIResult,
    *,
    now: datetime,
) -> bool:
    """True when WAI flipped to not_at_pudo but the gap is short.

    The "traffic light" pattern: driver was at_current_pudo, the cluster
    momentarily dispersed (rolled forward at a stoplight, brief sensor
    drift), but the disappearance is shorter than CANDIDATE_STALE_SECONDS.
    PLAN keeps the candidate armed across the gap rather than treating
    it as a cancellation.
    """
    if current_wai.status == "at_current_pudo":
        return False
    gap_seconds = (now - state.last_seen_at).total_seconds()
    return gap_seconds < CANDIDATE_STALE_SECONDS


def _is_long_stop_then_departure(
    state: _DriverTemporalState,
    current_wai: WhereAmIResult,
    *,
    now: datetime,
) -> bool:
    """True when a long-stopped armed candidate has now departed.

    The "implicit fire" pattern: driver stopped at a PUDO long enough to
    plausibly complete the action, but PLAN didn't fire (perhaps because
    confidence stayed below STRONG_MATCH_CONFIDENCE the whole time, or
    because we're below n_required). Now WAI reports the driver moving
    AND the gap from last_seen_at is >= CANDIDATE_STALE_SECONDS. The
    window for a real-time fire has closed; PLAN may still want to
    reconcile retroactively (e.g., emit fire_retroactive).
    """
    if current_wai.status == "at_current_pudo":
        return False
    if state.heartbeat_count < 1:
        return False
    gap_seconds = (now - state.last_seen_at).total_seconds()
    return gap_seconds >= CANDIDATE_STALE_SECONDS


def _advance_armed_state(
    state: Optional[_DriverTemporalState],
    current_wai: WhereAmIResult,
    *,
    now: datetime,
) -> Optional[_DriverTemporalState]:
    """Compute the next _DriverTemporalState given the current observation.

    The state machine for the armed candidate, expressed as a pure
    function:
      - If current WAI is at_current_pudo and matches armed offer/type:
        increment heartbeat_count, append observation, advance last_seen_at.
      - If current WAI is at_current_pudo but for a DIFFERENT offer/type:
        re-arm fresh on the new candidate (clear count, start observation
        trajectory anew).
      - If current WAI is at_current_pudo and state was None: arm fresh.
      - If current WAI is not at_current_pudo and the gap is brief: keep
        the armed state but append the dissenting observation (so forensics
        can see the dip).
      - If current WAI is not at_current_pudo and the gap is long: clear
        (return None). The candidate is dead.

    No side effects. Caller is responsible for calling
    self._set_temporal_state(driver_id, returned_state) (or
    _clear_temporal_state if None is returned).
    """
    obs = _make_observation(current_wai, now=now)

    # Case 1: state was None, current WAI is at_current_pudo -> arm fresh.
    if state is None:
        if current_wai.status != "at_current_pudo":
            return None
        if current_wai.offer_id is None or current_wai.pudo_type is None:
            return None
        return _DriverTemporalState(
            armed_at=now,
            armed_offer_id=current_wai.offer_id,
            armed_pudo_type=current_wai.pudo_type,
            heartbeat_count=1,
            last_seen_status=current_wai.status,
            last_seen_at=now,
            recent_observations=(obs,),
        )

    # Case 2: state existed, but current WAI matches a DIFFERENT offer/type.
    # Re-arm fresh on the new candidate.
    if (
        current_wai.status == "at_current_pudo"
        and current_wai.offer_id is not None
        and current_wai.pudo_type is not None
        and (
            current_wai.offer_id != state.armed_offer_id
            or current_wai.pudo_type != state.armed_pudo_type
        )
    ):
        return _DriverTemporalState(
            armed_at=now,
            armed_offer_id=current_wai.offer_id,
            armed_pudo_type=current_wai.pudo_type,
            heartbeat_count=1,
            last_seen_status=current_wai.status,
            last_seen_at=now,
            recent_observations=(obs,),
        )

    # Case 3: state existed, current WAI matches armed candidate -> reaffirm.
    if (
        current_wai.status == "at_current_pudo"
        and current_wai.offer_id == state.armed_offer_id
        and current_wai.pudo_type == state.armed_pudo_type
    ):
        new_obs = (state.recent_observations + (obs,))[-OBSERVATION_WINDOW:]
        return _DriverTemporalState(
            armed_at=state.armed_at,
            armed_offer_id=state.armed_offer_id,
            armed_pudo_type=state.armed_pudo_type,
            heartbeat_count=state.heartbeat_count + 1,
            last_seen_status=current_wai.status,
            last_seen_at=now,
            recent_observations=new_obs,
        )

    # Case 4: state existed, WAI now reports something else (not_at_pudo,
    # at_unknown_pudo, etc.). Decide based on gap.
    if _is_brief_disappearance(state, current_wai, now=now):
        # Keep the armed state but record the dissent.
        new_obs = (state.recent_observations + (obs,))[-OBSERVATION_WINDOW:]
        return _DriverTemporalState(
            armed_at=state.armed_at,
            armed_offer_id=state.armed_offer_id,
            armed_pudo_type=state.armed_pudo_type,
            heartbeat_count=state.heartbeat_count,
            last_seen_status=current_wai.status,
            last_seen_at=state.last_seen_at,  # don't advance — gap is the gap
            recent_observations=new_obs,
        )

    # Case 5: long gap or otherwise clearly dead -> clear.
    return None


# ============================================================================
# Section C — decision builders (one factory per action)
# ============================================================================
#
# Each builder is pure: takes its required inputs and returns a frozen
# PlannerDecision. Builders do NOT decide whether to fire — that's
# Section D's job (consume() dispatch in Step 3.4). They just package
# decisions with the right fields populated for EXECUTE to act on.
#
# All builders return the same shape: a PlannerDecision with all 9
# fields set explicitly (no defaults), even if a field is None for
# this action. This keeps the contract honest — readers can see at a
# glance what each action carries.


def _build_noop(reason: str) -> PlannerDecision:
    """Nothing to do this heartbeat."""
    return PlannerDecision(
        action="noop",
        offer_id=None,
        target_state=None,
        corrected_lat=None,
        corrected_lng=None,
        ghost_insert_payload=None,
        reconciliation_payload=None,
        reason=reason,
    )


def _build_arm_candidate(
    *,
    offer_id: str,
    pudo_type: str,
    heartbeat_count: int,
    reason: str,
) -> PlannerDecision:
    """Section A returned a fresh or incrementing armed state.

    Phase F may use this to update a UI indicator ("we think we're at the
    pickup, gathering confidence"). EXECUTE does not write the DB on this
    action — there is no state transition yet.
    """
    return PlannerDecision(
        action="arm_candidate",
        offer_id=offer_id,
        target_state=None,
        corrected_lat=None,
        corrected_lng=None,
        ghost_insert_payload=None,
        reconciliation_payload=None,
        reason=f"{reason} (heartbeat {heartbeat_count}, type={pudo_type})",
    )


def _build_cancel_candidate(reason: str) -> PlannerDecision:
    """An armed candidate is being abandoned (long miss or scope change).

    Phase F may use this to clear any "candidate detected" UI. EXECUTE
    does not write the DB on this action.
    """
    return PlannerDecision(
        action="cancel_candidate",
        offer_id=None,
        target_state=None,
        corrected_lat=None,
        corrected_lng=None,
        ghost_insert_payload=None,
        reconciliation_payload=None,
        reason=reason,
    )


def _build_fire_pickup(
    *,
    offer_id: str,
    corrected_lat: Optional[float],
    corrected_lng: Optional[float],
    target_state: str,
    reason: str,
) -> PlannerDecision:
    """Stable match on a pickup target. EXECUTE will call sm_transition
    to the supplied target_state and record the pickup using
    corrected_lat/lng.
    """
    return PlannerDecision(
        action="fire_pickup",
        offer_id=offer_id,
        target_state=target_state,
        corrected_lat=corrected_lat,
        corrected_lng=corrected_lng,
        ghost_insert_payload=None,
        reconciliation_payload=None,
        reason=reason,
    )


def _build_fire_dropoff(
    *,
    offer_id: str,
    corrected_lat: Optional[float],
    corrected_lng: Optional[float],
    target_state: str,
    reason: str,
) -> PlannerDecision:
    """Stable match on a dropoff target. Mirrors _build_fire_pickup."""
    return PlannerDecision(
        action="fire_dropoff",
        offer_id=offer_id,
        target_state=target_state,
        corrected_lat=corrected_lat,
        corrected_lng=corrected_lng,
        ghost_insert_payload=None,
        reconciliation_payload=None,
        reason=reason,
    )


def _build_fire_retroactive(
    *,
    offer_id: str,
    pudo_type: str,
    corrected_lat: Optional[float],
    corrected_lng: Optional[float],
    target_state: str,
    reason: str,
) -> PlannerDecision:
    """Driver stopped long enough to plausibly complete a PUDO but PLAN
    didn't fire in real time, and the driver has now departed.
    EXECUTE fires the missed PUDO retroactively using corrected_lat/lng
    from the trajectory.
    """
    return PlannerDecision(
        action="fire_retroactive",
        offer_id=offer_id,
        target_state=target_state,
        corrected_lat=corrected_lat,
        corrected_lng=corrected_lng,
        ghost_insert_payload=None,
        reconciliation_payload=None,
        reason=f"{reason} (retroactive {pudo_type})",
    )


def _build_fire_stacked_swap(
    *,
    primary_offer_id: str,
    secondary_offer_id: str,
    corrected_lat: Optional[float],
    corrected_lng: Optional[float],
    reason: str,
) -> PlannerDecision:
    """B-11 / S32: WAI matched the secondary's pickup while state machine
    thinks primary is alive. Close primary (mark missed dropoff or
    appropriate audit), promote secondary as current_offer_id, transition
    to the secondary's pickup state.

    Step 4 implements the detection logic that calls this builder.
    EXECUTE handles the atomic swap via sm_transition.
    """
    return PlannerDecision(
        action="fire_stacked_swap",
        offer_id=secondary_offer_id,
        target_state="IN_TRIP",  # post-swap, secondary's pickup just landed
        corrected_lat=corrected_lat,
        corrected_lng=corrected_lng,
        ghost_insert_payload=None,
        reconciliation_payload={
            "swap_kind": "primary_to_secondary",
            "primary_offer_id": primary_offer_id,
            "secondary_offer_id": secondary_offer_id,
        },
        reason=reason,
    )


def _build_fire_stacked_revert(
    *,
    primary_offer_id: str,
    secondary_offer_id: str,
    corrected_lat: Optional[float],
    corrected_lng: Optional[float],
    reason: str,
) -> PlannerDecision:
    """S35: Uber re-awarded primary while state machine thinks secondary
    is alive. Close secondary (it was never actually awarded), restore
    primary as current_offer_id, revert state to ENROUTE on primary's
    pickup.

    Symmetric inverse of _build_fire_stacked_swap.
    """
    return PlannerDecision(
        action="fire_stacked_revert",
        offer_id=primary_offer_id,
        target_state="ENROUTE",  # post-revert, driving to primary's pickup
        corrected_lat=corrected_lat,
        corrected_lng=corrected_lng,
        ghost_insert_payload=None,
        reconciliation_payload={
            "swap_kind": "secondary_to_primary",
            "primary_offer_id": primary_offer_id,
            "secondary_offer_id": secondary_offer_id,
        },
        reason=reason,
    )


def _build_cache_ghost(
    *,
    cluster_lat: float,
    cluster_lng: float,
    cluster,
    state_at_time: str,
    confidence: float,
    reason: str,
) -> PlannerDecision:
    """WAI returned at_unknown_pudo: a real cluster but no offer explains
    it. EXECUTE inserts a row in app_private.suspected_pudos using
    ghost_insert_payload — PLAN does NOT touch the DB (per R2 ruling).

    cluster is the underlying Cluster object from WAI. Phase F passes
    this through; EXECUTE projects it into the row's geometry / spread /
    duration columns.
    """
    return PlannerDecision(
        action="cache_ghost",
        offer_id=None,
        target_state=None,
        corrected_lat=cluster_lat,
        corrected_lng=cluster_lng,
        ghost_insert_payload={
            "lat": cluster_lat,
            "lng": cluster_lng,
            "cluster_spread_m": cluster.spread_m if cluster is not None else None,
            "cluster_duration_s": cluster.duration_s if cluster is not None else None,
            "state_at_time": state_at_time,
            "confidence": confidence,
        },
        reconciliation_payload=None,
        reason=reason,
    )


def _build_reconcile_missed_pickup(
    *,
    offer_id: str,
    target_state: str,
    suspected_pudo_id: Optional[int],
    reason: str,
) -> PlannerDecision:
    """S33 (Calhoun): dropoff fired successfully but actual_pickup_at is
    NULL and an unresolved suspected_pudo from earlier in this offer's
    lifecycle exists. EXECUTE corrects offer_history honestly per R1
    (state-correction over coordinate-synthesis):
      - actual_pickup_lat/lng remain NULL
      - pickup_missed = true
      - suspected_pudo row resolved as 'inferred_pickup_no_retroactive_nail'

    NO coordinates are passed to EXECUTE. This is intentional — R1
    forbids synthesizing a pickup point from inference. The audit trail
    stays honest: 'ride completed, pickup unobserved'.
    """
    return PlannerDecision(
        action="reconcile_missed_pickup",
        offer_id=offer_id,
        target_state=target_state,
        corrected_lat=None,
        corrected_lng=None,
        ghost_insert_payload=None,
        reconciliation_payload={
            "missed_pudo_type": "pickup",
            "offer_id": offer_id,
            "suspected_pudo_id": suspected_pudo_id,
            "offer_history_updates": {
                "pickup_missed": True,
                "pickup_inference_source": "dropoff_completed",
            },
        },
        reason=reason,
    )


def _build_reconcile_missed_dropoff(
    *,
    offer_id: str,
    target_state: str,
    suspected_pudo_id: Optional[int],
    reason: str,
) -> PlannerDecision:
    """Symmetric to _build_reconcile_missed_pickup for the missed-dropoff
    case (next-ride pickup fires successfully on a ride whose dropoff
    was never observed). Same R1 policy — no coordinate synthesis.
    """
    return PlannerDecision(
        action="reconcile_missed_dropoff",
        offer_id=offer_id,
        target_state=target_state,
        corrected_lat=None,
        corrected_lng=None,
        ghost_insert_payload=None,
        reconciliation_payload={
            "missed_pudo_type": "dropoff",
            "offer_id": offer_id,
            "suspected_pudo_id": suspected_pudo_id,
            "offer_history_updates": {
                "dropoff_missed": True,
                "dropoff_inference_source": "next_pickup_completed",
            },
        },
        reason=reason,
    )


# ============================================================================
# PudoPlanner — the orchestrator
# ============================================================================

class PudoPlanner:
    """The PLAN-layer orchestrator. Pure logic, database-blind.

    Phase F's heartbeat loop:
      1. Builds a DriverStateSnapshot from the driver's current state row
         and active Offer.
      2. Calls WhereAmI.evaluate(...) to get a WhereAmIResult.
      3. Calls planner.consume(driver_id, wai_result, driver_state_snapshot)
         to get a PlannerDecision.
      4. Hands the PlannerDecision to EXECUTE, which performs any DB
         writes (sm_transition, suspected_pudos INSERTs, offer_history
         UPDATEs).

    Steps 3.2-3.4 add the temporal helpers, decision builders, and
    dispatch logic. This skeleton ships consume() returning 'noop' for
    every input, providing the signature contract Phase F can integrate
    against immediately.
    """

    def __init__(
        self,
        cur=None,
        *,
        _now_fn=None,
        _state_store: Optional[dict] = None,
    ) -> None:
        """Initialize the planner.

        Args:
          cur: DB cursor. Currently unused — PLAN is database-blind. Kept
               on the signature for forward compatibility with Phase F if
               any read-only context fetches eventually move here. Today
               it should always be None or a dummy.
          _now_fn: injection seam for time. Default is datetime.now(UTC).
                   Tests inject deterministic clocks.
          _state_store: injection seam for per-driver temporal state.
                        Default is a fresh empty dict. Tests inject a
                        pre-populated dict to set up scenarios. v1.1 may
                        swap to a Postgres-backed store via this seam.
        """
        self._cur = cur
        self._now_fn = _now_fn or (lambda: datetime.now(timezone.utc))
        self._state_store: dict = _state_store if _state_store is not None else {}

    def consume(
        self,
        driver_id: str,
        wai_result: WhereAmIResult,
        *,
        driver_state_snapshot: DriverStateSnapshot,
    ) -> PlannerDecision:
        """Per-heartbeat decision call. THE PUBLIC ENTRY POINT.

        Six-step dispatch (Gemini-ratified ordering, 2026-04-26):

          1. Always: advance temporal state via _advance_armed_state and
             persist to _state_store. This step happens before any
             decision is made — the trajectory must be recorded even on
             noop heartbeats so forensic logs show the sequence.

          2. If wai_result.status == 'at_unknown_pudo': short-circuit
             to _build_cache_ghost. A cluster with no offer explanation
             is its own track and never interacts with armed candidates.

          3. STACKED disambiguation (Step 4 / Section B placeholder):
             _attempt_stacked_disambiguation returns None in Step 3.4.
             When Step 4 lands, it implements the coordinate-only proximity
             check that distinguishes S32 (fire_stacked_swap) from S35
             (fire_stacked_revert) and returns the appropriate
             PlannerDecision. Until then this is a no-op.

          4. Long-stop-then-departure: if next_state was just cleared
             (the candidate "died" because of a long miss) AND the
             previous state existed AND _is_long_stop_then_departure
             returns True, build fire_retroactive using prev_state's
             trajectory. CRITICAL: Step 4 (this step in the dispatch,
             not Phase E Step 4) MUST come before Step 5 because
             prev_state is needed; if we evaluated stable-match first,
             the fall-through in Step 6 would emit cancel_candidate and
             we'd lose the retroactive-fire opportunity.

          5. Stable match: if next_state is non-None and _is_stable_match
             returns True at N_HEARTBEATS_TO_FIRE, dispatch to
             _build_fire_pickup or _build_fire_dropoff based on
             armed_pudo_type. target_state is determined by the type:
             pickup -> 'IN_TRIP', dropoff -> 'UNCOMMITTED'.

          6. Fall-through:
             - next_state cleared but prev_state existed -> cancel_candidate
             - next_state armed/incrementing but not stable -> arm_candidate
             - both None -> noop

        KNOWN GAPS (Step 4 Section B closes these):

          - S32 implicit STACKED cancel: when WAI matches the secondary's
            pickup while state machine claims primary is alive, this
            dispatch will fire a normal fire_pickup for the secondary.
            That's wrong for S32 — the primary should be closed first
            via fire_stacked_swap. Step 4 inserts the contradiction
            detection into Step 3 above.

          - S35 Uber stacked re-award: the symmetric inverse of S32.
            Same problem: this dispatch fires a normal fire_pickup
            instead of fire_stacked_revert.

          - S34 hot swap: this dispatch handles it correctly within a
            single heartbeat *if* Phase F's heartbeat loop calls WAI a
            second time after a state transition (post-fire re-evaluation).
            Without that double-tap, the secondary pickup is missed.
            Phase F backlog item B-13.

        LOGGING: this method does NOT emit logs. The 'reason' field on
        the returned PlannerDecision is populated meaningfully on every
        non-noop decision; Phase F's heartbeat loop wraps consume() in
        its existing structured-logging pipeline. Keeping consume() pure
        (no I/O) is per Gemini's R2 ratification.

        Args:
          driver_id: identifies the driver whose temporal state to read/write.
          wai_result: WhereAmI's diagnosis for this heartbeat.
          driver_state_snapshot: state machine snapshot + active offer coords.

        Returns:
          A PlannerDecision the heartbeat loop hands to EXECUTE.
        """
        now = self._now_fn()
        prev_state = self._get_temporal_state(driver_id)

        # Step 1: advance temporal state (always)
        next_state = _advance_armed_state(prev_state, wai_result, now=now)
        if next_state is None:
            self._clear_temporal_state(driver_id)
        else:
            self._set_temporal_state(driver_id, next_state)

        # Hand off to pure decision logic. _decide takes everything as
        # arguments — no state-store reads inside. Makes _decide
        # independently unit-testable.
        return self._decide(
            wai_result=wai_result,
            snapshot=driver_state_snapshot,
            prev_state=prev_state,
            next_state=next_state,
            now=now,
        )

    def _decide(
        self,
        *,
        wai_result: WhereAmIResult,
        snapshot: DriverStateSnapshot,
        prev_state: Optional[_DriverTemporalState],
        next_state: Optional[_DriverTemporalState],
        now: datetime,
    ) -> PlannerDecision:
        """Pure dispatch — no state-store side effects.

        Implements steps 2-6 of the consume() dispatch. Caller (consume)
        is responsible for steps 1 (advancing temporal state) and for
        persisting next_state.
        """
        # Step 2: at_unknown_pudo -> cache_ghost (short-circuit)
        if wai_result.status == "at_unknown_pudo":
            if (
                wai_result.corrected_lat is not None
                and wai_result.corrected_lng is not None
            ):
                return _build_cache_ghost(
                    cluster_lat=wai_result.corrected_lat,
                    cluster_lng=wai_result.corrected_lng,
                    cluster=wai_result.cluster,
                    state_at_time=snapshot.state,
                    confidence=wai_result.confidence,
                    reason=f"at_unknown_pudo conf={wai_result.confidence:.2f}",
                )
            # at_unknown_pudo without coords is malformed; fall through to noop.
            return _build_noop(
                "at_unknown_pudo without corrected coords (malformed WAI result)"
            )

        # Step 3: STACKED disambiguation (Section B / Step 4 placeholder)
        stacked_decision = self._attempt_stacked_disambiguation(
            wai_result=wai_result,
            snapshot=snapshot,
        )
        if stacked_decision is not None:
            return stacked_decision

        # Step 4: long-stop-then-departure -> fire_retroactive
        # This MUST come before Step 5 because prev_state is needed and
        # next_state is None by definition in this case.
        if (
            next_state is None
            and prev_state is not None
            and _is_long_stop_then_departure(prev_state, wai_result, now=now)
        ):
            # Use the most recent observation in prev_state's trajectory
            # for corrected coords. Per R1, retroactive fires DO use
            # observed coords (this is not coordinate synthesis — these
            # are coordinates WAI actually reported during the stop).
            obs = (
                prev_state.recent_observations[-1]
                if prev_state.recent_observations
                else None
            )
            target_state = (
                "IN_TRIP" if prev_state.armed_pudo_type == "pickup"
                else "UNCOMMITTED"
            )
            return _build_fire_retroactive(
                offer_id=prev_state.armed_offer_id,
                pudo_type=prev_state.armed_pudo_type,
                corrected_lat=obs.corrected_lat if obs else None,
                corrected_lng=obs.corrected_lng if obs else None,
                target_state=target_state,
                reason=(
                    f"long stop then departure: armed for "
                    f"{prev_state.heartbeat_count}hb, gap exceeded "
                    f"{CANDIDATE_STALE_SECONDS}s"
                ),
            )

        # Step 5: stable match -> fire_pickup / fire_dropoff
        if (
            next_state is not None
            and _is_stable_match(
                next_state, wai_result, n_required=N_HEARTBEATS_TO_FIRE
            )
        ):
            reason = (
                f"stable match {N_HEARTBEATS_TO_FIRE}hb conf="
                f"{wai_result.confidence:.2f}"
            )
            if next_state.armed_pudo_type == "pickup":
                return _build_fire_pickup(
                    offer_id=next_state.armed_offer_id,
                    corrected_lat=wai_result.corrected_lat,
                    corrected_lng=wai_result.corrected_lng,
                    target_state="IN_TRIP",
                    reason=reason,
                )
            else:
                return _build_fire_dropoff(
                    offer_id=next_state.armed_offer_id,
                    corrected_lat=wai_result.corrected_lat,
                    corrected_lng=wai_result.corrected_lng,
                    target_state="UNCOMMITTED",
                    reason=reason,
                )

        # Step 6: fall-through
        if next_state is None and prev_state is not None:
            return _build_cancel_candidate(
                f"candidate cleared (was armed for "
                f"{prev_state.heartbeat_count}hb on "
                f"{prev_state.armed_offer_id}/{prev_state.armed_pudo_type})"
            )

        if next_state is not None:
            return _build_arm_candidate(
                offer_id=next_state.armed_offer_id,
                pudo_type=next_state.armed_pudo_type,
                heartbeat_count=next_state.heartbeat_count,
                reason=f"armed conf={wai_result.confidence:.2f}",
            )

        return _build_noop(f"no candidate, status={wai_result.status}")

    def _attempt_stacked_disambiguation(
        self,
        *,
        wai_result: WhereAmIResult,
        snapshot: DriverStateSnapshot,
    ) -> Optional[PlannerDecision]:
        """Section B: STACKED contradiction detection (S32 / S35).

        Identity-based detection (R1 ratified, revised from earlier
        coord-proximity ruling). WAI in STACKED state returns ONE
        at_current_pudo with the winning offer_id (primary or secondary,
        pickup or dropoff). PLAN interprets the identity:

          primary's dropoff   -> normal progression, return None
          secondary's pickup  -> S32: driver bypassed primary dropoff.
                                  fire_stacked_swap (close primary,
                                  promote secondary).
          primary's pickup    -> S35: Uber re-awarded primary. driver
                                  went BACK to primary's pickup.
                                  fire_stacked_revert (close secondary,
                                  restore primary).
          secondary's dropoff -> sequence violation (secondary's pickup
                                  hasn't fired). Return None per
                                  Gemini R2 fail-closed ruling. Backlog
                                  B-14: Sequence Violation Detector for
                                  forensic alert.

        Returns:
          PlannerDecision if S32 or S35 detected, otherwise None
          (caller's _decide falls through to normal dispatch).
        """
        # Guard 1: only applies in STACKED state.
        if snapshot.state != "STACKED":
            return None

        # Guard 2: only applies when WAI matched a target.
        if wai_result.status != "at_current_pudo":
            return None

        # Guard 3: malformed WAI (no offer_id) -> bail.
        if wai_result.offer_id is None or wai_result.pudo_type is None:
            return None

        # Guard 4: snapshot must carry both offer ids for STACKED logic.
        # Phase F is responsible for assembly; if either is missing,
        # something's wrong upstream and we fail closed.
        if (
            snapshot.primary_offer_id is None
            or snapshot.current_offer_id is None
        ):
            return None

        is_primary = (wai_result.offer_id == snapshot.primary_offer_id)
        is_secondary = (wai_result.offer_id == snapshot.current_offer_id)
        is_pickup = (wai_result.pudo_type == "pickup")
        is_dropoff = (wai_result.pudo_type == "dropoff")

        # Case A: primary's dropoff -> normal progression. Not a contradiction.
        # Falls through to _decide()'s stable-match path.
        if is_primary and is_dropoff:
            return None

        # Case B (S32): secondary's pickup. Driver bypassed primary's dropoff
        # which never fired — the implicit-cancel pattern.
        if is_secondary and is_pickup:
            return _build_fire_stacked_swap(
                primary_offer_id=snapshot.primary_offer_id,
                secondary_offer_id=snapshot.current_offer_id,
                corrected_lat=wai_result.corrected_lat,
                corrected_lng=wai_result.corrected_lng,
                reason=(
                    f"S32 implicit STACKED cancel: WAI at secondary pickup "
                    f"({snapshot.current_offer_id}) without primary dropoff "
                    f"({snapshot.primary_offer_id}) firing first"
                ),
            )

        # Case C (S35): primary's pickup. Uber re-awarded primary; driver
        # went back to primary's pickup. Secondary was never actually
        # awarded.
        if is_primary and is_pickup:
            return _build_fire_stacked_revert(
                primary_offer_id=snapshot.primary_offer_id,
                secondary_offer_id=snapshot.current_offer_id,
                corrected_lat=wai_result.corrected_lat,
                corrected_lng=wai_result.corrected_lng,
                reason=(
                    f"S35 Uber re-award: WAI at primary pickup "
                    f"({snapshot.primary_offer_id}) while state expected "
                    f"secondary ({snapshot.current_offer_id}) — secondary "
                    f"unawarded by Uber"
                ),
            )

        # Case D: secondary's dropoff. Sequence violation — secondary's
        # pickup hasn't fired. Per Gemini R2: fail closed (return None).
        # Backlog B-14: Sequence Violation Detector for forensic alert.
        if is_secondary and is_dropoff:
            return None

        # Anything else: WAI matched a target with offer_id we don't
        # recognize relative to the snapshot. Could be a stale offer_id
        # or a third stacked offer in some edge case. Fall through.
        return None

    # -- private helpers ---------------------------------------------------

    def _get_temporal_state(
        self, driver_id: str
    ) -> Optional[_DriverTemporalState]:
        """Read this driver's armed-candidate state, or None if not armed."""
        return self._state_store.get(driver_id)

    def _set_temporal_state(
        self, driver_id: str, state: _DriverTemporalState
    ) -> None:
        """Set this driver's armed-candidate state."""
        self._state_store[driver_id] = state

    def _clear_temporal_state(self, driver_id: str) -> None:
        """Remove this driver's armed-candidate state. Idempotent."""
        self._state_store.pop(driver_id, None)
