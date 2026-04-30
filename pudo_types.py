"""
pudo_types.py — shared dataclass contracts for the PUDO awareness/planning subsystem.

Top-level peer of cluster_detection.py and where_am_i.py. Both DIAGNOSE
primitives (WAI) and PLAN consumers (pudo_planner.py, Phase E) import from
here. Putting these inside either box would create a one-way import that
becomes circular as soon as PLAN feeds signal back into DIAGNOSE.

NB: deliberately NOT named `types.py` — that shadows Python's stdlib `types`
module and would silently break anything in the codebase doing `import types`.

Box discipline (per canonical rules):
  Only DIAGNOSE and PLAN see these. EXECUTE writes via sm_transition, which
  has its own argument set; it does not import from here.

Phase D scope (2026-04):
  - WhereAmIResult is consumed by Phase D shadow-mode logging only.
  - TargetSpec, Offer are constructed by the caller (driver_heartbeat.py
    integration in Phase F) from offer_history rows.
  - No suspected_pudos writes happen via these types in Phase D; per Q12
    consensus the ghost-cache write moves to PLAN/EXECUTE in Phase E.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Optional

from cluster_detection import Cluster


# ============================================================================
# States — canonical Python constants for driver-state names
# ============================================================================

class States:
    """Canonical Python constants for driver-state names.

    PG remains the source of truth (valid_state_transitions table).
    These constants exist so Python-side comparisons don't typo-drift
    out of sync with PG. Adding consumers progressively across the
    codebase is v2.2 backlog item B-5.

    REFINE_DROPOFF: legacy armed-state from the pre-WAI two-zone linger
    loop. Still alive in production (April refactor preserved it). WAI
    treats REFINE_DROPOFF as a synonym for IN_TRIP per Step 4 lock —
    both mean "trip in progress, dropoff target." Eventual deprecation
    is v2.2 backlog item B-8 once shadow-mode data shows whether the
    armed-state binary adds value beyond WAI's continuous confidence.

    REFINE_PICKUP is intentionally absent — per fact-check 2026-04-26
    it appears only as a verdict label returned by check_convergence(),
    never as a sustained driver state.
    """
    UNCOMMITTED      = "UNCOMMITTED"
    ENROUTE          = "ENROUTE"
    IN_TRIP          = "IN_TRIP"
    REFINE_DROPOFF   = "REFINE_DROPOFF"
    STACKED          = "STACKED"


# ============================================================================
# TargetSpec — a single PUDO target (pickup or dropoff)
# ============================================================================

@dataclass(frozen=True)
class TargetSpec:
    """A single PUDO target with the metadata WAI needs for class-aware matching.

    Constructed at offer-ingest time. WAI consumes; nothing in WAI mutates.

    Field semantics:
      lat, lng        -- geocoded target point (Uber-claimed, Google-resolved).
                         Per canonical rule "Distance over Geocode," this is a
                         starting suggestion; the cluster median can override.
      address_class   -- which RFC §5.1 matching rule applies.
      named_roads     -- street names parsed from the address. Length 1 for
                         single_road / number_on_street / apartment_complex,
                         length 2 for intersection, length 0 for poi.
                         Comparison against current_road is case-insensitive
                         exact match (normalization already happened upstream
                         in arc_band.py street name normalization).
    """
    lat: float
    lng: float
    address_class: Literal[
        "single_road",
        "intersection",
        "number_on_street",
        "poi",
        "apartment_complex",
    ]
    named_roads: tuple[str, ...]


# ============================================================================
# Offer — the bundle of TargetSpecs WAI evaluates against per heartbeat
# ============================================================================

@dataclass(frozen=True)
class Offer:
    """The current ride context handed to WhereAmI.evaluate().

    secondary_dropoff is non-None only when the driver has STACKED a second
    ride. Per canonical rule, current_offer_id is a live pointer to the
    currently active offer; in STACKED state current_offer_id is the secondary
    and the primary dropoff (the one being completed) is in `dropoff` here.
    Caller (driver_heartbeat.py / decisions.router) is responsible for
    assembling this struct correctly per state — WAI does not query
    offer_history.

    accepted_at (v2.6 amendment, sub-step 1b.1): the offer-acceptance
    timestamp from app_private.offer_history.accepted_at. Anchors the
    cluster-history lookback window in WAI's cluster_revisit topology
    check per Step 6 Amendment 1. UTC, timezone-aware.
    """
    offer_id: str
    accepted_at: datetime
    pickup: TargetSpec
    dropoff: TargetSpec
    secondary_dropoff: Optional[TargetSpec] = None


# ============================================================================
# WAIMatch — the naked-list contract for the queue-aware evaluate()
# ============================================================================
#
# Per SIMPLIFIED_ARCHITECTURE.md §3 and the "Pure Sensor" encapsulation
# discipline ratified 2026-04-30: WAI returns only what targets matched the
# cluster. The downstream heartbeat handler interprets the list against
# current_offer_id per §4 Cases A-E and §5 disambiguation rules.
#
# Three fields. No more. No status labels (those are handler-derived). No
# coordinates (those live on the cluster). No topology (internal to the
# matcher's confidence computation, not exposed). Per §10 A8, this is the
# sole legitimate output shape of the matcher pipeline.

WAI_CONFIDENCE_THRESHOLD: float = 0.40
"""Canonical matcher threshold per SIMPLIFIED_ARCHITECTURE.md §3 step 3d.

WAI.evaluate() returns only matches whose confidence clears this floor.
Below threshold, the match doesn't count. Tuning this value is matcher-
calibration scope (per §11 and §10 A1's evening empirical state — current
real-world matches sit at 0.404-0.409, threshold-edge sensitive).
"""


@dataclass(frozen=True)
class WAIMatch:
    """A single match returned by WhereAmI.evaluate().

    Per the naked-list contract (SIMPLIFIED_ARCHITECTURE.md §3): WAI is a
    Pure Sensor that reports what targets matched the cluster. The downstream
    interpreter (heartbeat handler) reads coordinates from the cluster object
    and offer queue directly, and applies §4 case-resolution rules to decide
    which actions to fire.

    Encapsulation discipline: this dataclass is intentionally minimal. No
    coordinates (handler reads from cluster). No topology (internal to
    confidence computation). No status labels (handler derives from match
    list + current_offer_id 1-bit memory). Per §10 A8, code that bypasses
    WAI to infer PUDOs from raw cluster proximity violates the architecture.
    """
    offer_id: str
    location_type: Literal["pickup", "dropoff"]
    confidence: float


# ============================================================================
# WhereAmIResult — the diagnostic output of WhereAmI.evaluate()
# ============================================================================

@dataclass(frozen=True)
class WhereAmIResult:
    """Pure DIAGNOSE output: what does the system believe is happening NOW?

    No write side effects implied by any field. PLAN (Phase E) is responsible
    for deciding what to do with this — fire, hold, persist a ghost, etc.
    Per canonical 4-box discipline, anything that would call sm_transition or
    write to a state-bearing table belongs in PLAN/EXECUTE, not in the
    construction of this object.
    """

    # --- Primary status -----------------------------------------------------
    status: Literal[
        "at_current_pudo",   # Cluster matches an expected PUDO of current ride
        "at_previous_pudo",  # Cluster matches a cached suspected_pudo (ghost)
        "at_unknown_pudo",   # Cluster exists, no offer/ghost explains it
        "not_at_pudo",       # No cluster, or below MIN_REPORT_THRESHOLD
    ]
    pudo_type: Optional[Literal["pickup", "dropoff"]]
    offer_id: Optional[str]   # Which offer this PUDO belongs to (None if unknown)

    # --- Corrected coordinates ----------------------------------------------
    # Cluster median if stopped, else None. Per "Distance over Geocode,"
    # these override the offer's geocoded coords for any downstream write
    # PLAN may eventually decide to perform.
    corrected_lat: Optional[float]
    corrected_lng: Optional[float]

    # --- Road topology context ----------------------------------------------
    on_wire: bool
    current_road: Optional[str]
    on_target_road: bool
    off_wire_duration_s: int

    # --- Stop context (v1.0: always "unknown_stop" when stopped) -----------
    # Stop Atlas wiring is deferred to v1.1; until then _stop_context()
    # returns "not_stopped" or "unknown_stop" exclusively. The full literal
    # set is declared here so v1.1 can land without a contract change.
    stop_context: Optional[Literal[
        "at_traffic_signal",
        "at_rr_crossing",
        "at_stop_sign",
        "in_parking_lot",
        "at_curb",
        "in_traffic",
        "unknown_stop",
        "not_stopped",
    ]]

    # --- Confidence ---------------------------------------------------------
    confidence: float   # 0.0 to 1.0
    reason: str         # Human-readable; includes breakdown components inline

    # --- Target attribution -------------------------------------------------
    # The address string of the matched target's TargetSpec, primarily useful
    # for STACKED disambiguation: when both primary and secondary dropoffs
    # match, target_address tells PLAN which one won the higher-confidence
    # tie-break (Q3 ruling). Non-None only when status == "at_current_pudo".
    # Declared with no default to match the style of every other Optional[]
    # field on this dataclass — explicit None at construction is required.
    target_address: Optional[str]

    # --- Ghost recovery -----------------------------------------------------
    # ghost_id is non-None only when status == "at_previous_pudo" AND the
    # cached suspected_pudo row's id is known. Phase D shadow mode does NOT
    # write to suspected_pudos (per Q12: writes belong to PLAN/EXECUTE), so
    # ghost_id is always None throughout Phase D — the field exists for
    # forward compatibility with Phase E which will populate it.
    ghost_id: Optional[int]

    # --- Underlying cluster snapshot (Q1) -----------------------------------
    # PLAN uses this directly for is_stable() across heartbeats rather than
    # re-running detect_cluster(). Keeping cluster math single-sourced is
    # the architectural reason Phase C extracted detect_cluster() into a
    # shared primitive in the first place.
    cluster: Optional[Cluster]

    # --- Cluster history topology (v2.6 amendment, sub-step 1b) -------------
    # True when WAI detects topological evidence of a round-trip (the
    # "Houston Loop"): a non-current cluster within CLUSTER_REVISIT_MIN_GAP_M
    # of the active cluster's centroid AND >=1 intermediate cluster
    # >= MIN_GAP_M from both. Sub-step 1b.1 ships the field as always-False
    # (placeholder); sub-step 1b.2 wires the real computation in evaluate();
    # sub-step 1c (B-26) is the same-address PLAN-side latch consumer.
    cluster_revisit: bool


# ============================================================================
# DriverStateSnapshot — Input to PudoPlanner.consume()
# ============================================================================

@dataclass(frozen=True)
class DriverStateSnapshot:
    """A point-in-time view of driver state and active targets.

    Assembled by the heartbeat loop (Phase F) and handed to
    PudoPlanner.consume(). Contains all the context PLAN needs for
    high-level planning decisions without further DB reads — keeping
    pudo_planner.py "database-blind" per Gemini ruling on Phase E
    architecture.

    STACKED-state semantics:
      - current_offer_id is the SECONDARY (live pointer per canonical rule)
      - primary_offer_id is the offer whose dropoff is being completed
      - dropoff_lat/lng = primary's dropoff (the one actively being driven to)
      - secondary_pickup_lat/lng = the SECONDARY ride's pickup location
      - secondary_dropoff_lat/lng = the SECONDARY ride's eventual dropoff

    Non-STACKED states populate primary_offer_id == current_offer_id and
    leave the secondary_* coordinate pairs as None.

    The secondary_pickup_lat/lng pair was added per R3 ruling for B-11
    (implicit STACKED cancel) coordinate-only proximity check, since
    TargetSpec.address is not yet populated (Phase F TODO).
    """
    state: str
    current_offer_id: Optional[str]
    primary_offer_id: Optional[str]

    # Active offer's coordinates (the one PLAN is currently reasoning about)
    pickup_lat: Optional[float]
    pickup_lng: Optional[float]
    dropoff_lat: Optional[float]
    dropoff_lng: Optional[float]

    # Secondary offer's coordinates (STACKED only)
    secondary_pickup_lat: Optional[float]
    secondary_pickup_lng: Optional[float]
    secondary_dropoff_lat: Optional[float]
    secondary_dropoff_lng: Optional[float]


# ============================================================================
# PlannerDecision — Output from PudoPlanner.consume()
# ============================================================================

@dataclass(frozen=True)
class PlannerDecision:
    """Strategic intent produced by the PLAN layer.

    EXECUTE (state_machine.py / driver_heartbeat.py write paths) reads this
    and performs the required writes. PLAN never writes to the DB —
    "database-blind" per Phase E architecture.

    Action semantics:
      noop                      -- nothing to do this heartbeat
      arm_candidate             -- WAI saw at_current_pudo; start/increment
                                   the N-heartbeat stable-match counter
      cancel_candidate          -- brief at_current_pudo -> not_at_pudo
                                   transition (traffic light); abandon
                                   the armed candidate
      fire_pickup               -- stable match; transition to PICKUP_CONFIRMED
      fire_dropoff              -- stable match; transition to DROPOFF_CONFIRMED
      fire_retroactive          -- ghost match; fire a previously-missed PUDO
      fire_stacked_swap         -- B-11 implicit STACKED cancel: close primary,
                                   promote secondary
      cache_ghost               -- at_unknown_pudo; EXECUTE inserts a
                                   suspected_pudos row using ghost_insert_payload
      reconcile_missed_pickup   -- "Calhoun" case (R1/R2): dropoff just fired
                                   successfully, but the pickup was never
                                   confirmed. EXECUTE corrects offer_history
                                   honestly via reconciliation_payload.
                                   Coordinates are NOT synthesized.
      reconcile_missed_dropoff  -- symmetric to above for the missed-dropoff
                                   case (next-ride pickup fires on a ride
                                   whose dropoff was never confirmed)

    Payload semantics:
      ghost_insert_payload      -- non-None only when action == 'cache_ghost'.
                                   Carries the row data for the
                                   suspected_pudos INSERT.
      reconciliation_payload    -- non-None only when action in
                                   {'reconcile_missed_pickup',
                                    'reconcile_missed_dropoff'}. Carries the
                                   offer_id, the missed-PUDO type, the
                                   suspected_pudos row id (if any) to mark
                                   resolved, and the offer_history column
                                   updates (e.g. pickup_missed=true).
    """
    action: Literal[
        "noop",
        "noop_same_pudo_no_revisit",
        "arm_candidate",
        "cancel_candidate",
        "fire_pickup",
        "fire_dropoff",
        "fire_retroactive",
        "fire_stacked_swap",
        "fire_stacked_revert",
        "cache_ghost",
        "reconcile_missed_pickup",
        "reconcile_missed_dropoff",
    ]
    offer_id: Optional[str]
    target_state: Optional[str]
    corrected_lat: Optional[float]
    corrected_lng: Optional[float]
    ghost_insert_payload: Optional[dict]
    reconciliation_payload: Optional[dict]
    reason: str
