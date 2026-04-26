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
        """Per-heartbeat decision call.

        Step 3.1 stub: returns 'noop' for every input. Steps 3.2-3.4
        replace this with the real dispatch logic.

        Args:
          driver_id: identifies the driver whose temporal state to read/write.
          wai_result: WhereAmI's diagnosis for this heartbeat.
          driver_state_snapshot: state machine snapshot + active offer coords.

        Returns:
          A PlannerDecision the heartbeat loop hands to EXECUTE.
        """
        return PlannerDecision(
            action="noop",
            offer_id=None,
            target_state=None,
            corrected_lat=None,
            corrected_lng=None,
            ghost_insert_payload=None,
            reconciliation_payload=None,
            reason="step_3_1_skeleton_stub",
        )

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
