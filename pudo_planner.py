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
