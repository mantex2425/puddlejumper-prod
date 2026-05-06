"""Motion Gate + Dual-Leg Odometer Gate (Sprint A §7 + Amendment 1).

Pure-function gate layer that filters WAI matches before dispatch. Never
mutates DB, never reads beyond its arguments. The matcher (where_am_i.py)
is intentionally untouched — gate sits POST-WAI per §3 step 4 invariant
("WAI evaluation may run on every heartbeat. Firing transitions require
the cluster to be Closed").

Two protections, two failure modes:

  Motion Gate (per §7, driver-level):
    - cluster.max_recent_speed_mph < 2.0
    - cluster.duration_s            >= 20.0
    Holds drive-by false positives (passing strip mall at 30mph) and
    brief-tap false positives (5s stops at lights).

  Dual-Leg Odometer Gate (per memo 1 ratification, per-offer per-leg):
    pickup leg : (cm - leg_start_cumulative_miles_pickup)  / pickup_miles >= 0.9
    dropoff leg: (cm - leg_start_cumulative_miles_dropoff) / trip_miles   >= 0.9
    Holds mid-route long-stop false positives (2-min light at intersection
    with commercial POI 150m away — the Drive 1 Stop 1A failure mode).

The gate filters AFTER WAI returns matches. Held matches never reach
dispatch; their forensic record is preserved in pudo_decision_context for
matcher-tuning visibility.

Threshold provenance (per L-10 categorization):
  motion_max_speed_mph   = 2.0   cat-1 (per §7, Product Law)
  motion_min_duration_s  = 20.0  cat-1 (per §7 default, TBD per first-shift validation)
  odometer_floor_fraction= 0.9   cat-1 (port from BEAD's _BLIND_MAN_TORT_MIN, ratified)

NULL handling:
  cumulative_miles is None -> ALL legs HOLD with reason 'missing_cumulative_miles'
  target distance is None  -> that leg HOLDS with reason 'missing_target_distance'
  anchor is None           -> that leg PASSES with reason 'pre_gate_offer'
                              (forward-only enforcement; in-flight offers grandfathered)
  target == 0              -> that leg PASSES with reason 'zero_target'
                              (Errands scenario per §5.1, or driver standing at
                              pickup at acceptance time)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

# =============================================================================
# Constants (module-level for test introspection + override-by-import)
# =============================================================================

MOTION_MAX_SPEED_MPH = 2.0
MOTION_MIN_DURATION_S = 20.0
ODOMETER_FLOOR_FRACTION = 0.9


# =============================================================================
# Verdict types
# =============================================================================

# motion_verdict ∈ {"closed", "moving", "transient", "no_cluster"}
#   closed     -> cluster exists, speed < threshold, duration >= threshold
#   moving     -> cluster exists, speed >= threshold (regardless of duration)
#   transient  -> cluster exists, speed < threshold, duration < threshold
#   no_cluster -> cluster is None
MotionVerdict = str


@dataclass(frozen=True)
class LegResult:
    """Per-leg odometer verdict with raw-math forensic detail.

    Serialized into the pudo_decision_context.odometer_gate_result jsonb
    column for "audit-from-couch" forensic queries (per Gemini round-2
    ratification, "raw math included" parenthetical).
    """
    eligible: bool
    progress: Optional[float]   # None when target is 0/None or cm is None
    odo: Optional[float]        # cumulative_miles (echoed for audit)
    anchor: Optional[float]     # leg_start_cumulative_miles_* (echoed for audit)
    target: Optional[float]     # pickup_miles or trip_miles (echoed for audit)
    reason: str                 # 'passed' | 'below_floor' | 'zero_target' |
                                # 'pre_gate_offer' | 'missing_target_distance' |
                                # 'missing_cumulative_miles'

    def to_jsonable(self) -> dict[str, Any]:
        """Render for jsonb storage. None values preserved (jsonb null)."""
        return {
            "eligible": self.eligible,
            "progress": self.progress,
            "odo": self.odo,
            "anchor": self.anchor,
            "target": self.target,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class OfferLegEligibility:
    """Both legs' verdicts for a single offer."""
    pickup: LegResult
    dropoff: LegResult

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "pickup": self.pickup.to_jsonable(),
            "dropoff": self.dropoff.to_jsonable(),
        }


@dataclass(frozen=True)
class GateVerdict:
    """Aggregate gate verdict, returned by evaluate_gates().

    Carries everything the LOG step needs to populate the four gate columns:
      pudo_decision_context.motion_gate_result      <- motion_verdict
      pudo_decision_context.odometer_gate_result    <- jsonb of leg_eligibility
      pudo_decision_context.gate_held_offer_ids     <- derived from leg_eligibility
      pudo_decision_context.gate_held_legs          <- derived from leg_eligibility

    Designed as a sibling to DiagnosticContext rather than mutating it
    (DiagnosticContext is frozen by CANONICAL_RULES XIV.A "naked-list
    contract for business logic").
    """
    motion_verdict: MotionVerdict
    leg_eligibility: dict[str, OfferLegEligibility]  # offer_id (str) -> verdicts

    def jsonb_payload(self) -> dict[str, Any]:
        """Render the leg_eligibility map for jsonb storage."""
        return {oid: ole.to_jsonable() for oid, ole in self.leg_eligibility.items()}

    def held_offer_ids_and_legs(self) -> tuple[list[str], list[str]]:
        """Sparse arrays for indexable forensic queries.

        Returns:
          (held_offer_ids, held_legs)
          held_offer_ids is the list of offer_ids that had at least one leg
          held by the odometer gate.
          held_legs is a deduplicated list of leg names that were held
          across all offers (subset of {'pickup', 'dropoff'}).

        Motion gate holds are NOT reflected here — motion is driver-level
        and surfaced via motion_verdict directly.
        """
        held_offers: list[str] = []
        held_legs_set: set[str] = set()
        for oid, ole in self.leg_eligibility.items():
            held_any = False
            if not ole.pickup.eligible and ole.pickup.reason not in ("pre_gate_offer", "zero_target"):
                held_any = True
                held_legs_set.add("pickup")
            if not ole.dropoff.eligible and ole.dropoff.reason not in ("pre_gate_offer", "zero_target"):
                held_any = True
                held_legs_set.add("dropoff")
            if held_any:
                held_offers.append(oid)
        return held_offers, sorted(held_legs_set)


# =============================================================================
# Motion gate (driver-level)
# =============================================================================

def evaluate_motion_gate(
    cluster: Any,  # Optional[Cluster]; duck-typed to avoid import cycle
    *,
    max_speed_mph: float = MOTION_MAX_SPEED_MPH,
    min_duration_s: float = MOTION_MIN_DURATION_S,
) -> MotionVerdict:
    """Per §7: cluster is "Closed" when speed < 2mph AND duration >= 20s.

    cluster is duck-typed: must expose .duration_s and either
    .max_recent_speed_mph or .speed_mph (Cluster dataclass field name
    varies by sprint; canonical is .max_recent_speed_mph per §7 wording
    "max recent heartbeat speed"). We try max_recent_speed_mph first,
    fall back to speed_mph for compatibility.

    Returns "no_cluster" when cluster is None (never reached by dispatch
    in practice; explicit verdict for forensic logging completeness).
    """
    if cluster is None:
        return "no_cluster"

    speed = getattr(cluster, "max_recent_speed_mph", None)
    if speed is None:
        speed = getattr(cluster, "speed_mph", None)
    duration = getattr(cluster, "duration_s", None)

    if speed is None or duration is None:
        # Defensive: missing fields treated as moving (gate fails closed).
        return "moving"

    if speed >= max_speed_mph:
        return "moving"
    if duration < min_duration_s:
        return "transient"
    return "closed"


# =============================================================================
# Odometer gate (per offer, per leg)
# =============================================================================

def _evaluate_leg(
    cumulative_miles: Optional[float],
    anchor: Optional[float],
    target: Optional[float],
    floor_fraction: float,
) -> LegResult:
    """Single-leg odometer evaluation.

    Order of NULL checks matters:
      1. cumulative_miles None -> hold (we can't compute progress)
      2. target None           -> hold (data integrity issue)
      3. target == 0           -> pass (Errands / standing-at-pickup)
      4. anchor None           -> pass (pre-gate offer, grandfathered)
      5. compute progress, compare to floor_fraction
    """
    if cumulative_miles is None:
        return LegResult(
            eligible=False, progress=None, odo=None,
            anchor=anchor, target=target,
            reason="missing_cumulative_miles",
        )
    if target is None:
        return LegResult(
            eligible=False, progress=None, odo=cumulative_miles,
            anchor=anchor, target=None,
            reason="missing_target_distance",
        )
    if target == 0:
        return LegResult(
            eligible=True, progress=None, odo=cumulative_miles,
            anchor=anchor, target=0.0,
            reason="zero_target",
        )
    if anchor is None:
        return LegResult(
            eligible=True, progress=None, odo=cumulative_miles,
            anchor=None, target=target,
            reason="pre_gate_offer",
        )

    delta = float(cumulative_miles) - float(anchor)
    progress = delta / float(target)
    eligible = progress >= floor_fraction
    return LegResult(
        eligible=eligible,
        progress=progress,
        odo=float(cumulative_miles),
        anchor=float(anchor),
        target=float(target),
        reason="passed" if eligible else "below_floor",
    )


def evaluate_odometer_gate(
    cumulative_miles: Optional[float],
    queue_offers: list[Any],
    *,
    floor_fraction: float = ODOMETER_FLOOR_FRACTION,
) -> dict[str, OfferLegEligibility]:
    """Run per-leg odometer evaluation for every offer in the queue.

    queue_offers is duck-typed; each element must expose:
      .id (or .offer_id) -> int or str
      .pickup_miles                          -> Optional[float]
      .trip_miles                            -> Optional[float]
      .leg_start_cumulative_miles_pickup     -> Optional[float]
      .leg_start_cumulative_miles_dropoff    -> Optional[float]

    Field-name flex: pickup_miles fallback to .pickupMiles, trip_miles to
    .tripMiles, anchors to camelCase variants. This is paranoia for the
    OfferRow dataclass shape stability across the demolition refactor.

    Returns a dict keyed by stringified offer_id.
    """
    result: dict[str, OfferLegEligibility] = {}
    for offer in queue_offers:
        oid = getattr(offer, "id", None)
        if oid is None:
            oid = getattr(offer, "offer_id", None)
        if oid is None:
            # Skip malformed offers rather than raise; gate is forensic-
            # safety, must not crash the heartbeat.
            continue
        oid_str = str(oid)

        pickup_miles = _get_first(offer, "pickup_miles", "pickupMiles")
        trip_miles = _get_first(offer, "trip_miles", "tripMiles")
        anchor_pickup = _get_first(
            offer,
            "leg_start_cumulative_miles_pickup",
            "legStartCumulativeMilesPickup",
        )
        anchor_dropoff = _get_first(
            offer,
            "leg_start_cumulative_miles_dropoff",
            "legStartCumulativeMilesDropoff",
        )

        result[oid_str] = OfferLegEligibility(
            pickup=_evaluate_leg(cumulative_miles, anchor_pickup, pickup_miles, floor_fraction),
            dropoff=_evaluate_leg(cumulative_miles, anchor_dropoff, trip_miles, floor_fraction),
        )
    return result


def _get_first(obj: Any, *names: str) -> Any:
    """Return the first attribute that exists and is not None."""
    for name in names:
        v = getattr(obj, name, None)
        if v is not None:
            return v
    return None


# =============================================================================
# Combined gate evaluation
# =============================================================================

def evaluate_gates(
    cluster: Any,  # Optional[Cluster]
    cumulative_miles: Optional[float],
    queue_offers: list[Any],
    *,
    motion_max_speed_mph: float = MOTION_MAX_SPEED_MPH,
    motion_min_duration_s: float = MOTION_MIN_DURATION_S,
    odometer_floor_fraction: float = ODOMETER_FLOOR_FRACTION,
) -> GateVerdict:
    """Top-level gate evaluator. Called by driver_heartbeat.py post-WAI.

    No DB access. No external service calls. Pure function of inputs.
    """
    motion_verdict = evaluate_motion_gate(
        cluster,
        max_speed_mph=motion_max_speed_mph,
        min_duration_s=motion_min_duration_s,
    )
    leg_eligibility = evaluate_odometer_gate(
        cumulative_miles,
        queue_offers,
        floor_fraction=odometer_floor_fraction,
    )
    return GateVerdict(
        motion_verdict=motion_verdict,
        leg_eligibility=leg_eligibility,
    )


# =============================================================================
# Match filtering (the actual "gate" surface that touches dispatch)
# =============================================================================

def filter_matches_by_gates(
    matches: list[Any],     # list[WAIMatch]; duck-typed
    gate_verdict: GateVerdict,
) -> list[Any]:
    """Drop matches that fail the gate layer.

    A match is dropped when:
      1. motion_verdict != "closed", OR
      2. The (offer_id, location_type) leg is ineligible per
         gate_verdict.leg_eligibility.

    Returns a new list; input is not mutated.

    Match shape: each WAIMatch must expose .offer_id and .location_type
    (∈ {'pickup', 'dropoff'}). Location_type values outside this set
    pass through (defensive: if the matcher ever adds a third type we
    don't accidentally hold it).
    """
    if gate_verdict.motion_verdict != "closed":
        return []

    survivors: list[Any] = []
    for match in matches:
        oid_raw = getattr(match, "offer_id", None)
        ltype = getattr(match, "location_type", None)
        if oid_raw is None or ltype is None:
            # Malformed match; pass through — not the gate's job to
            # validate match shape.
            survivors.append(match)
            continue

        oid_str = str(oid_raw)
        ole = gate_verdict.leg_eligibility.get(oid_str)
        if ole is None:
            # Match for an offer not in the queue snapshot. Defensive
            # pass-through; this should never happen in production but
            # we don't drop matches we can't reason about.
            survivors.append(match)
            continue

        if ltype == "pickup" and not ole.pickup.eligible:
            continue
        if ltype == "dropoff" and not ole.dropoff.eligible:
            continue
        survivors.append(match)
    return survivors
