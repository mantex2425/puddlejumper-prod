"""
where_am_i.py — Continuous location awareness primitive.

Pure DIAGNOSE layer per the 4-Box Controller. Reads only.
No sm_transition calls, no suspected_pudos INSERTs, no Discord pings,
no offer_history mutations. PLAN consumer (pudo_planner.py, Phase E)
owns all writes derived from this module's output.

Public surface (Phase D Step 5.5+):
    WhereAmI(cur)             - instantiate with a psycopg cursor
    WhereAmI.evaluate(...)    - returns WhereAmIResult on every heartbeat

Algorithm: see WHERE_AM_I_PROPOSAL_v2.md sections 4 and 5.

Module structure:
    - Constants and confidence weights (locked in Steps 1-3)
    - Section A: signal library (this file as of Step 5.2)
    - Section B: plumbing - _MatchOutcome, _weighted_confidence,
      _render_reason, _validate_target (Step 5.3)
    - Section C: per-class matchers and _CLASS_DISPATCH (Step 5.4)
    - Section D: WhereAmI class with evaluate() and helpers (Step 5.5)
"""
from __future__ import annotations

import logging
import math
from typing import Optional

from cluster_detection import Cluster
from pivot_context import _road_names_match

log = logging.getLogger(__name__)


# =============================================================================
# Module-level constants (locked in Steps 1-3 of Phase D)
# =============================================================================

# Confidence thresholds (Step 1 Q4, RFC section 5.2)
MIN_REPORT_THRESHOLD = 0.4
STRONG_MATCH_CONFIDENCE = 0.7

# Per-class proximity radii (Steps 1 Q5, 3 sections C.2/C.3/C.4)
INTERSECTION_RADIUS_M = 250.0
SINGLE_ROAD_RADIUS_M = 500.0
NUMBER_ON_STREET_RADIUS_M = 50.0
APARTMENT_RADIUS_M = 300.0

# Ghost-cache match radius (Step 1 Q8)
GHOST_MATCH_RADIUS_M = 50.0

# Cluster history topology — Houston Loop revisit gate (v2.6 amendment, sub-step 1b.2)
# L-10 category 1 provenance: production-data-grounded structural noise floor.
# Houston GPS multipath wobble in the rideshare heartbeat stream produces
# centroid drift at a single address; 200m forces evidence of a genuinely
# different spatial context (driver actually departed and returned) before
# the round-trip latch (B-26) can fire. Not a policy threshold — adjusting
# requires physics-of-the-environment justification, not behavioral preference.
CLUSTER_REVISIT_MIN_GAP_M = 200.0

# Cluster tightness thresholds (Step 3 section A.3)
# - <= 15m spread:  tight stop, parked at curb
# - >= 50m spread:  loose, GPS noise or maneuvering
_CLUSTER_TIGHT_M = 15.0
_CLUSTER_LOOSE_M = 50.0

# Cluster duration thresholds (Step 3 section A.4)
# Triangle profile: ramp 0->0.5 (0-15s), ramp 0.5->1.0 (15-30s),
# plateau 1.0 (30-180s), decay 1.0->0.5 (180-360s), floor 0.5 (360s+).
_DURATION_MIN_S = 15
_DURATION_FULL_S = 30
_DURATION_DECAY_S = 180

# Apartment-pivot thresholds (Step 3 section A.6)
# off_wire_duration_s < 10:  no signal
# 10s <= off_wire_duration_s < 30s:  linear ramp 0.0 -> 1.0
# off_wire_duration_s >= 30s:  flat 1.0
_PIVOT_MIN_OFF_WIRE_S = 10
_PIVOT_FULL_OFF_WIRE_S = 30


# =============================================================================
# Confidence weights table (Step 1 lock; ratified by Gemini Step 5.2 entry)
# =============================================================================
#
# TUNABLE: revisit after shadow-mode data accumulates. Each row sums to 1.0
# (Gemini math-checked Step 5.2 entry). Per-class intuition:
#   - intersection:      breadcrumb-heavy with strong on_target_road weight
#   - single_road:       breadcrumb dominates
#   - number_on_street:  proximity dominates (Google geocode is precise)
#   - apartment_complex: pivot dominates (off-wire arrival is the signal)

_CONFIDENCE_WEIGHTS = {
    "intersection": {
        "proximity":         0.20,
        "breadcrumb_match":  0.30,
        "cluster_tightness": 0.15,
        "cluster_duration":  0.10,
        "on_target_road":    0.20,
        "off_wire_pivot":    0.05,
    },
    "single_road": {
        "proximity":         0.10,
        "breadcrumb_match":  0.40,
        "cluster_tightness": 0.15,
        "cluster_duration":  0.15,
        "on_target_road":    0.15,
        "off_wire_pivot":    0.05,
    },
    "number_on_street": {
        "proximity":         0.40,
        "breadcrumb_match":  0.20,
        "cluster_tightness": 0.15,
        "cluster_duration":  0.10,
        "on_target_road":    0.15,
        "off_wire_pivot":    0.00,
    },
    "apartment_complex": {
        "proximity":         0.10,
        "breadcrumb_match":  0.10,
        "cluster_tightness": 0.20,
        "cluster_duration":  0.15,
        "on_target_road":    0.05,
        "off_wire_pivot":    0.40,
    },
}


# =============================================================================
# Section A - The signal library
# =============================================================================
#
# Six pure functions plus the Haversine helper. Each signal returns a value in
# [0.0, 1.0]. Each is independently unit-testable. Each has zero dependence on
# the others.
#
# Per Step 3 section A.7 lock: the proximity signal computes distance with a
# Python Haversine helper rather than calling the canonical app_private SQL
# functions. The canonical rule's spirit ("never write ST_MakePoint") is about
# the WRITE side of coordinates; distance computation for diagnostic scoring
# is a read-only Python concern. Keeps signals pure-function and testable
# without a PostGIS connection.


def haversine_meters(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in meters between two lat/lng points.

    Earth radius constant: R = 6_371_000.0 meters. This value is
    load-bearing for fixture-math parity (per L-9 corollary, sub-step 1b.3
    boundary precision lesson). Test fixtures that probe gate-constant
    boundaries (e.g., 30m colocation threshold, 200m cluster-revisit gap)
    MUST use this exact function — not desk approximations like
    "111,320 meters per degree latitude" — to compute boundary coordinates.
    Use a REPL probe of this helper to derive fixture lat/lng offsets at
    fixture-authoring time.

    Used by DIAGNOSE signal scoring (where_am_i.py) and PLAN spatial
    primitives (pudo_planner._is_same_pudo_colocation, sub-step 1c).
    All write-path coordinates and all geometric operations that go to PG
    continue to use the app_private.* canonical functions per the
    canonical coordinate rules.

    Returns 0.0 for identical points; the formula handles antipodes
    correctly via atan2 / asin clamping.
    """
    R = 6_371_000.0  # earth radius, meters
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    # Clamp a into [0, 1] to defend against floating-point overshoot near antipodes
    a = max(0.0, min(1.0, a))
    return 2 * R * math.asin(math.sqrt(a))


def _signal_proximity(
    cluster: Cluster,
    target_lat: float,
    target_lng: float,
    threshold_m: float,
) -> float:
    """Distance from cluster median to target, normalized by threshold.

    Decay model (Gemini Step 3 ruling: linear is the v1 default, exponential
    deferred to v1.1 if shadow data demands sharper short-distance discrimination):
      - At target (0m):       1.0
      - At threshold:         0.0
      - Beyond threshold:     0.0 (does not go negative)
      - Inside threshold:     1.0 - (distance / threshold)

    Defensive: threshold_m <= 0 returns 0.0 rather than ZeroDivisionError.
    """
    if threshold_m <= 0:
        return 0.0

    distance_m = haversine_meters(
        cluster.median_lat, cluster.median_lng,
        target_lat, target_lng,
    )

    if distance_m >= threshold_m:
        return 0.0
    return 1.0 - (distance_m / threshold_m)


def _signal_breadcrumb_match(
    breadcrumb: tuple[str, ...],
    target_road_names: tuple[str, ...],
) -> float:
    """Did the driver actually traverse a target road?

    Step function (Gemini Step 3 Q4 ruling: boolean is correct, no partial
    credit for parallel streets):
      - Any target road appears in breadcrumb:  1.0
      - No target road in breadcrumb:           0.0
      - Empty breadcrumb (sparse trail):        0.0 (fail-closed)
      - Empty target_road_names:                0.0 (fail-closed)

    Comparison uses pivot_context._road_names_match, which canonically
    handles abbreviations ("Settemont Rd" matches "Settemont Road").
    """
    if not breadcrumb or not target_road_names:
        return 0.0

    for crumb in breadcrumb:
        for target_road in target_road_names:
            if _road_names_match(crumb, target_road):
                return 1.0
    return 0.0


def _signal_cluster_tightness(cluster: Cluster) -> float:
    """Tighter clusters indicate a real stop; looser ones suggest GPS noise,
    light-cycle creep, or active maneuvering.

    Linear ramp (Step 3 section A.3):
      - spread <= 15m:      1.0
      - spread >= 50m:      0.0
      - in between:         linearly interpolated between the two
    """
    if cluster.spread_m <= _CLUSTER_TIGHT_M:
        return 1.0
    if cluster.spread_m >= _CLUSTER_LOOSE_M:
        return 0.0
    return 1.0 - (cluster.spread_m - _CLUSTER_TIGHT_M) / (
        _CLUSTER_LOOSE_M - _CLUSTER_TIGHT_M
    )


def _signal_cluster_duration(cluster: Cluster) -> float:
    """Longer stops are more likely real PUDOs - up to a point.

    Triangle profile (Step 3 section A.4, Gemini-ratified Step 5.2 entry):
      - duration <  15s:           linear ramp 0.0 -> 0.5
      - 15s <= duration <= 30s:    linear ramp 0.5 -> 1.0
      - 30s <= duration <= 180s:   flat 1.0
      - 180s < duration <= 360s:   linear decay 1.0 -> 0.5
      - duration > 360s:           flat 0.5 (still significant, but suspect)

    Reasoning: a 5-minute stop is probably real but might be a meal break.
    Don't drop below 0.5 - long stops are still strong signals, just
    ambiguous about whether they're THIS PUDO.
    """
    d = cluster.duration_s
    if d < _DURATION_MIN_S:
        return 0.5 * (d / _DURATION_MIN_S)
    if d < _DURATION_FULL_S:
        return 0.5 + 0.5 * ((d - _DURATION_MIN_S) / (_DURATION_FULL_S - _DURATION_MIN_S))
    if d <= _DURATION_DECAY_S:
        return 1.0
    if d <= 2 * _DURATION_DECAY_S:
        return 1.0 - 0.5 * ((d - _DURATION_DECAY_S) / _DURATION_DECAY_S)
    return 0.5


def _signal_on_target_road(
    current_road: Optional[str],
    target_road_names: tuple[str, ...],
) -> float:
    """Is the driver currently snapped to a road that appears in the
    target's address?

    Step function (boolean by nature):
      - current_road matches a target road:  1.0
      - current_road is None (off-wire):     0.0
      - current_road doesn't match:          0.0
      - target_road_names is empty:          0.0
    """
    if current_road is None or not target_road_names:
        return 0.0
    for target_road in target_road_names:
        if _road_names_match(current_road, target_road):
            return 1.0
    return 0.0


def _signal_off_wire_pivot(
    on_wire: bool,
    last_named_road: Optional[str],
    off_wire_duration_s: int,
    target_road_names: tuple[str, ...],
) -> float:
    """Did the driver recently leave a target road and stay off-wire?
    Classic apartment-complex / POI parking-lot arrival pattern.

    Returns:
      - 0.0 if currently on-wire (no pivot happened)
      - 0.0 if last_named_road is None
      - 0.0 if last_named_road doesn't match any target road
      - 0.0 if off_wire_duration_s < 10
      - Linear ramp 0.0 -> 1.0 as off_wire_duration_s grows from 10s to 30s
      - 1.0 from 30s onward (within recency window enforced by pivot_context's
        own backward-scan window, ~300s)

    Signature note: takes the four fields directly rather than a _RoadTopology
    object. _RoadTopology is defined in Section D (Step 5.5); Section A
    deliberately has no dependency on it so signal tests don't need the
    topology-construction machinery. Section C matchers will assemble the
    arguments from _RoadTopology.
    """
    if on_wire:
        return 0.0
    if last_named_road is None or not target_road_names:
        return 0.0
    if off_wire_duration_s < _PIVOT_MIN_OFF_WIRE_S:
        return 0.0

    pivoted_off_target = any(
        _road_names_match(last_named_road, tr)
        for tr in target_road_names
    )
    if not pivoted_off_target:
        return 0.0

    if off_wire_duration_s >= _PIVOT_FULL_OFF_WIRE_S:
        return 1.0

    return (off_wire_duration_s - _PIVOT_MIN_OFF_WIRE_S) / (
        _PIVOT_FULL_OFF_WIRE_S - _PIVOT_MIN_OFF_WIRE_S
    )


# =============================================================================
# Section B - Plumbing
# =============================================================================
#
# Internal types and pure helpers used by the per-class matchers (Section C)
# and the WhereAmI orchestrator (Section D). All private to this module —
# tests import them as "special friends" per Gemini Q6.
#
# _MatchOutcome is the contract between matchers and evaluate(): every
# matcher returns one. evaluate() reads the fields directly to assemble
# the final WhereAmIResult.
#
# Field ordering follows Gemini Step 5.3 Q1 ruling: verdict first
# (matched, confidence), then geography (corrected coords), then
# context (reason, attribution), then metadata (signals).


from dataclasses import dataclass


@dataclass(frozen=True)
class _MatchOutcome:
    """Internal carrier between per-class matchers and evaluate().

    Every _match_<class> function returns one of these. evaluate()
    consumes the fields directly when assembling the final
    WhereAmIResult. Frozen because the structure is immutable post-
    construction; mutability would invite subtle bugs in the
    STACKED tie-break path (Q3) where multiple outcomes are
    compared by confidence.

    All fields can be None except matched / confidence / reason —
    these are always populated, even by fail-closed validators.
    """
    # --- Verdict (always populated) ---------------------------------------
    matched: bool
    confidence: float

    # --- Geography (None if no match) -------------------------------------
    corrected_lat: Optional[float]
    corrected_lng: Optional[float]

    # --- Context (always populated) ---------------------------------------
    reason: str

    # --- Attribution (None when not at_current_pudo) ----------------------
    pudo_type: Optional[str]
    target_address: Optional[str]

    # --- Metadata (None for fail-closed outcomes) -------------------------
    # Per Gemini Q5: stashing the per-signal score breakdown on the
    # outcome lets evaluate() preserve the dominant-signal forensic trail
    # all the way through to the WhereAmIResult.
    signals: Optional[dict[str, float]]


def _weighted_confidence(
    signals: dict[str, float],
    weights: dict[str, float],
) -> float:
    """Sum of (signal * weight) across the six confidence components.

    Per-class weights live in _CONFIDENCE_WEIGHTS. Each row sums to 1.0.

    Defensive: if `signals` is missing a key that `weights` requires,
    raises KeyError loudly rather than silently substituting 0.0 — a
    matcher that forgets a signal is a bug, not a tunable parameter.
    """
    return sum(signals[k] * w for k, w in weights.items())


def _render_reason(
    class_name: str,
    signals: dict[str, float],
    confidence: float,
) -> str:
    """Human-readable summary used in WhereAmIResult.reason and
    [WAI matcher=...] debug log lines.

    Format (Gemini Q2 lock — .2f precision):
        "{class_name} conf={conf:.2f} [signal=score, signal=score, ...]"

    Signals are sorted by score descending so the dominant signal
    appears first — when a fire is forensically wrong, the log
    immediately reveals which signal carried the decision.
    """
    sorted_signals = sorted(signals.items(), key=lambda kv: -kv[1])
    parts = ", ".join(f"{k}={v:.2f}" for k, v in sorted_signals)
    return f"{class_name} conf={confidence:.2f} [{parts}]"


def _validate_target(target, class_name: str) -> Optional[_MatchOutcome]:
    """Fail-closed guard against unusable targets.

    Returns None if `target` is usable (matcher should proceed to
    compute signals). Returns a fail-closed _MatchOutcome if the
    target has NULL coords — happens in production when triangulation
    failed (S27 from the canonical scenarios doc).

    Per the canonical contract (Step 5 inspection), TargetSpec.lat
    and TargetSpec.lng can legitimately be None when triangulation
    failed for an offer. WAI must never crash on this; it must
    produce a fail-closed outcome that PLAN can reason about.

    Empty target.named_roads is NOT a validation failure — that's
    valid for poi-class targets. Class-specific signal computation
    handles missing road names gracefully (returns 0.0 for breadcrumb
    and on_target_road signals).
    """
    if target.lat is None or target.lng is None:
        return _MatchOutcome(
            matched=False,
            confidence=0.0,
            corrected_lat=None,
            corrected_lng=None,
            reason=f"{class_name} skipped: NULL target coords",
            pudo_type=None,
            target_address=getattr(target, "address", None),
            signals=None,
        )
    return None


# =============================================================================
# Section C - Per-class matchers and dispatch
# =============================================================================
#
# Five orchestrator functions, one per address class, plus the _CLASS_DISPATCH
# table that evaluate() (Section D) uses to route a target to its matcher.
#
# Each matcher follows the same shape:
#   1. Validate the target (fail-closed if NULL coords)
#   2. Compute the 6 signals via _compute_signals helper
#   3. Sum signals * class-specific weights via _weighted_confidence
#   4. Render reason string via _render_reason
#   5. Build _MatchOutcome
#
# Class-specific behavior is entirely in (a) which proximity threshold gets
# passed to _compute_signals and (b) which _CONFIDENCE_WEIGHTS row applies.
# Per Gemini Step 3 ratification: the weights table is the only "knob"; all
# matchers share the same orchestration shape.
#
# _RoadTopology is brought forward from Section D (Gemini Step 5.4 Q1
# ruling). It's a frozen dataclass with the topology fields the matchers
# need. Section D's _compute_road_topology adapter constructs instances
# from get_pivot_context() output; tests construct them directly.


@dataclass(frozen=True)
class _RoadTopology:
    """Snapshot of the driver's road-network context at a single heartbeat.

    Constructed in production by _compute_road_topology (Section D) from
    pivot_context.get_pivot_context() output. Constructed in tests
    directly. Frozen because the snapshot is taken at a moment in time
    and shouldn't mutate as the matchers reason about it.

    on_target_road is NOT a field here (Q1 ruling, Step 2). It's
    relationship between topology and a specific TargetSpec, computed
    inside each matcher via _signal_on_target_road.
    """
    on_wire: bool                       # currently snapped to a named road?
    current_road: Optional[str]         # name of the snapped road, else None
    last_named_road: Optional[str]      # most recent named road touched
    off_wire_duration_s: int            # 0 when on_wire; else seconds since pivot
    breadcrumb: tuple[str, ...]         # raw road names, recent -> older


def _compute_signals(
    cluster: Cluster,
    topo: _RoadTopology,
    target,
    threshold_m: float,
) -> dict[str, float]:
    """Build the 6-signal dict for a matcher.

    Eliminates duplication across the 5 matchers (otherwise each would
    have the same 6-line construction block). The class-specific knobs
    are the proximity threshold (passed in) and the weights row applied
    downstream — never the signals themselves.

    target must have lat, lng, named_roads attributes (TargetSpec).
    Validation that lat/lng are non-None happens BEFORE this is called,
    in _validate_target.
    """
    return {
        "proximity": _signal_proximity(
            cluster, target.lat, target.lng, threshold_m,
        ),
        "breadcrumb_match": _signal_breadcrumb_match(
            topo.breadcrumb, target.named_roads,
        ),
        "cluster_tightness": _signal_cluster_tightness(cluster),
        "cluster_duration": _signal_cluster_duration(cluster),
        "on_target_road": _signal_on_target_road(
            topo.current_road, target.named_roads,
        ),
        "off_wire_pivot": _signal_off_wire_pivot(
            on_wire=topo.on_wire,
            last_named_road=topo.last_named_road,
            off_wire_duration_s=topo.off_wire_duration_s,
            target_road_names=target.named_roads,
        ),
    }


def _build_outcome(
    cluster: Cluster,
    target,
    class_name: str,
    signals: dict[str, float],
    confidence: float,
) -> _MatchOutcome:
    """Assemble a _MatchOutcome from computed signals + confidence.

    Common tail of every matcher. The matched flag is True iff confidence
    clears MIN_REPORT_THRESHOLD (Step 1 Q4 lock); below that, the matcher
    reports "tried but didn't match" so evaluate() can fall through to
    ghost-match or at_unknown_pudo.
    """
    reason = _render_reason(class_name, signals, confidence)
    return _MatchOutcome(
        matched=confidence >= MIN_REPORT_THRESHOLD,
        confidence=confidence,
        corrected_lat=cluster.median_lat,
        corrected_lng=cluster.median_lng,
        reason=reason,
        pudo_type=None,           # Set by _match_current_pudo orchestrator (Step 5.5)
        target_address=getattr(target, "address", None),
        signals=signals,
    )


def _match_intersection(
    cluster: Cluster,
    topo: _RoadTopology,
    target,
) -> _MatchOutcome:
    """Match an intersection-class target ("Joan St & Settemont Rd")."""
    if (skip := _validate_target(target, "intersection")) is not None:
        return skip

    signals = _compute_signals(cluster, topo, target, INTERSECTION_RADIUS_M)
    confidence = _weighted_confidence(signals, _CONFIDENCE_WEIGHTS["intersection"])

    if log.isEnabledFor(logging.DEBUG):
        log.debug(
            "[WAI matcher=intersection] %s",
            _render_reason("intersection", signals, confidence),
        )

    return _build_outcome(cluster, target, "intersection", signals, confidence)


def _match_single_road(
    cluster: Cluster,
    topo: _RoadTopology,
    target,
) -> _MatchOutcome:
    """Match a single_road target ("fondren rd")."""
    if (skip := _validate_target(target, "single_road")) is not None:
        return skip

    signals = _compute_signals(cluster, topo, target, SINGLE_ROAD_RADIUS_M)
    confidence = _weighted_confidence(signals, _CONFIDENCE_WEIGHTS["single_road"])

    if log.isEnabledFor(logging.DEBUG):
        log.debug(
            "[WAI matcher=single_road] %s",
            _render_reason("single_road", signals, confidence),
        )

    return _build_outcome(cluster, target, "single_road", signals, confidence)


def _match_number_on_street(
    cluster: Cluster,
    topo: _RoadTopology,
    target,
) -> _MatchOutcome:
    """Match a number_on_street target ("1234 Main St").

    Tightest proximity threshold of any class (50m) because Google's
    house-number geocode is precise to within a few meters typically.
    """
    if (skip := _validate_target(target, "number_on_street")) is not None:
        return skip

    signals = _compute_signals(cluster, topo, target, NUMBER_ON_STREET_RADIUS_M)
    confidence = _weighted_confidence(signals, _CONFIDENCE_WEIGHTS["number_on_street"])

    if log.isEnabledFor(logging.DEBUG):
        log.debug(
            "[WAI matcher=number_on_street] %s",
            _render_reason("number_on_street", signals, confidence),
        )

    return _build_outcome(cluster, target, "number_on_street", signals, confidence)


def _match_apartment_complex(
    cluster: Cluster,
    topo: _RoadTopology,
    target,
) -> _MatchOutcome:
    """Match an apartment_complex target.

    Generous proximity threshold (300m) because Google often pins the
    leasing office or a generic centroid rather than the actual unit.
    The off_wire_pivot signal carries 40% of the weight here — the
    classic "drove off the target road into a parking lot" pattern.
    """
    if (skip := _validate_target(target, "apartment_complex")) is not None:
        return skip

    signals = _compute_signals(cluster, topo, target, APARTMENT_RADIUS_M)
    confidence = _weighted_confidence(signals, _CONFIDENCE_WEIGHTS["apartment_complex"])

    if log.isEnabledFor(logging.DEBUG):
        log.debug(
            "[WAI matcher=apartment_complex] %s",
            _render_reason("apartment_complex", signals, confidence),
        )

    return _build_outcome(cluster, target, "apartment_complex", signals, confidence)


def _match_poi_stub(
    cluster: Cluster,
    topo: _RoadTopology,
    target,
) -> _MatchOutcome:
    """Stub for poi-class targets (airports, named businesses).

    Per Step 1 Q4 lock and Gemini Step 5.4 Q3 ratification: returns
    not_at_pudo semantics with WARN log. The cluster falls through to
    ghost match -> at_unknown_pudo in evaluate(), and the WARN log
    surfaces the POI miss rate in shadow-mode aggregates.

    POI matching deferred to v1.1 per RFC v2.4.7. Polygon-based
    matching (airport curbs, business footprints) is a separate
    architectural conversation from point-proximity matching.
    """
    log.warning(
        "[WAI matcher=poi_stub] target=%r class=poi - match deferred to v1.1, "
        "falling through to ghost / at_unknown_pudo",
        getattr(target, "address", None),
    )
    return _MatchOutcome(
        matched=False,
        confidence=0.0,
        corrected_lat=None,
        corrected_lng=None,
        reason="poi_stub",
        pudo_type=None,
        target_address=getattr(target, "address", None),
        signals=None,
    )


# Dispatch table — Section D's _match_current_pudo uses this to route a
# TargetSpec to its class-appropriate matcher. .get() returns None for
# unknown classes; the orchestrator logs and skips in that case.
_CLASS_DISPATCH = {
    "intersection":      _match_intersection,
    "single_road":       _match_single_road,
    "number_on_street":  _match_number_on_street,
    "apartment_complex": _match_apartment_complex,
    "poi":               _match_poi_stub,
}


# =============================================================================
# Section D - The orchestrator
# =============================================================================
#
# Public API: WhereAmI.evaluate(driver_id, current_offer, state).
#
# Implements RFC §4 hierarchical matching:
#   1. Cluster check       → no cluster → not_at_pudo
#   2. Topology            → compute road context (single pivot_context call)
#   3. Stop context        → "unknown_stop" (Stop Atlas v1.1)
#   4. Current PUDO match  → dispatch by state and address_class
#   5. Ghost cache READ    → unresolved suspect within 50m
#   6. Unknown PUDO        → cluster exists, no offer/ghost explains it
#
# Per Q12 lock: WAI is pure DIAGNOSE. No suspected_pudos INSERTs, no
# sm_transition calls. PLAN consumer (Phase E) owns all writes.
#
# Per Q6 + Step 2 extension: dependencies (cluster detection, pivot context)
# are injected via __init__. Production code uses real implementations;
# tests pass fakes for hermetic, DB-free unit tests.


from datetime import datetime, timezone

from cluster_detection import detect_cluster, get_recent_clusters
from pivot_context import get_pivot_context
from pudo_types import WhereAmIResult, States


# Ghost cache SELECT — read-only per Q12. Phase A schema:
# app_private.suspected_pudos is hash-partitioned by driver_id, with a
# partial index on (driver_id, match_expires_at) WHERE resolved_at IS NULL.
# Query is microsecond-fast at scale.
#
# Canonical-rule compliance:
#   - app_private.coords_to_geography(lat, lng): sanctioned, (lat, lng) order
#   - bare NOW() against timestamptz match_expires_at column
#   - ST_DWithin uses geography type (GIST index)
_GHOST_CACHE_SQL = """
SELECT
    id,
    lat,
    lng,
    offer_id_at_time,
    confidence,
    detected_at
FROM app_private.suspected_pudos
WHERE driver_id = %s
  AND resolved_at IS NULL
  AND match_expires_at > NOW()
  AND ST_DWithin(
        geog,
        app_private.coords_to_geography(%s, %s),
        %s
      )
ORDER BY detected_at DESC
LIMIT 1;
"""


def _compute_cluster_revisit(
    active_cluster: "Cluster",
    recent_clusters: list,
) -> bool:
    """Houston Loop topology check (v2.6 amendment, sub-step 1b.2).

    Returns True iff `recent_clusters` contains topological evidence that
    the driver has departed and returned to the active cluster's location
    within the offer-anchored lookback window. The signal is structural,
    not temporal — the intermediate cluster's existence IS the proof of
    departure-and-return.

    Args:
      active_cluster: the cluster currently under evaluation. Always the
        last element of recent_clusters when called from evaluate(), but
        passed separately for clarity and to keep this helper testable
        without ordering assumptions on recent_clusters.
      recent_clusters: oldest-first list of all clusters in the offer-
        anchored lookback window, as returned by get_recent_clusters().

    Algorithm:
      1. History = recent_clusters minus active_cluster (matched by identity
         on (median_lat, median_lng, latest) — the natural unique key).
      2. If |history| < 2: return False (no room for prior_pudo + intermediate).
      3. For each candidate prior_pudo in history (chronological order):
         If prior_pudo within MIN_GAP_M of active centroid:
           For each later cluster mid in history (after prior_pudo):
             If mid >= MIN_GAP_M from BOTH active AND prior_pudo:
               Return True.
      4. Return False.

    The "from both" framing (Gemini Q3 ratification) protects against GPS
    multipath drift at a single address being mistaken for a true revisit:
    if the driver simply shuffled around the pickup, the intermediate would
    be near the prior_pudo (and thus also near active, since prior_pudo is
    near active). Requiring distance from both proves a genuinely different
    spatial context.

    Performance: O(n^2) in cluster count. n is bounded by the offer-
    anchored window (typically <30 clusters even for long fares); the
    nested loop terminates on first valid pair, so worst case is rare.
    """
    # Identify the active cluster within recent_clusters by its tuple key.
    # Equality-by-content is safer than `is` since callers may construct
    # the active_cluster freshly.
    active_key = (active_cluster.median_lat, active_cluster.median_lng,
                  active_cluster.latest)
    history = [
        c for c in recent_clusters
        if (c.median_lat, c.median_lng, c.latest) != active_key
    ]

    if len(history) < 2:
        return False

    # history is oldest-first (per get_recent_clusters' ORDER BY latest ASC).
    # Iterate prior_pudo candidates in chronological order.
    for i, prior_pudo in enumerate(history):
        d_prior_to_active = haversine_meters(
            prior_pudo.median_lat, prior_pudo.median_lng,
            active_cluster.median_lat, active_cluster.median_lng,
        )
        if d_prior_to_active >= CLUSTER_REVISIT_MIN_GAP_M:
            continue  # not a prior_pudo candidate

        # Look for an intermediate AFTER prior_pudo (chronologically).
        for mid in history[i + 1:]:
            d_mid_to_active = haversine_meters(
                mid.median_lat, mid.median_lng,
                active_cluster.median_lat, active_cluster.median_lng,
            )
            d_mid_to_prior = haversine_meters(
                mid.median_lat, mid.median_lng,
                prior_pudo.median_lat, prior_pudo.median_lng,
            )
            if (d_mid_to_active >= CLUSTER_REVISIT_MIN_GAP_M and
                    d_mid_to_prior >= CLUSTER_REVISIT_MIN_GAP_M):
                return True

    return False


class WhereAmI:
    """Continuous location awareness primitive.

    Pure DIAGNOSE per the 4-Box Controller. Reads only. evaluate() is
    safe to call on every heartbeat without side effects.

    Construction:
      WhereAmI(cur)                              # production
      WhereAmI(cur, _cluster_fn=fake, ...)       # tests with injected deps

    Public surface:
      evaluate(driver_id, current_offer, state) -> WhereAmIResult
    """

    def __init__(
        self,
        cur,
        *,
        _cluster_fn=detect_cluster,
        _pivot_fn=get_pivot_context,
        _recent_clusters_fn=get_recent_clusters,
    ):
        """
        cur: psycopg cursor for ghost-cache SELECT.
        _cluster_fn: callable(driver_id, cur) -> Optional[Cluster].
            Default: cluster_detection.detect_cluster. Tests inject fakes.
        _pivot_fn: callable(driver_id, cur, anchor_time=None) -> dict.
            Default: pivot_context.get_pivot_context. Tests inject fakes.
        _recent_clusters_fn: callable(driver_id, cur, accepted_at_anchor, ...)
            -> list[Cluster]. Default: cluster_detection.get_recent_clusters.
            Used by evaluate()'s cluster_revisit topology check (v2.6
            amendment, sub-step 1b.2). Tests inject fakes.

        Keyword-only args via `*` so production callers never accidentally
        pass test doubles positionally.
        """
        self.cur = cur
        self._cluster_fn = _cluster_fn
        self._pivot_fn = _pivot_fn
        self._recent_clusters_fn = _recent_clusters_fn

    # =========================================================================
    # Public entry point
    # =========================================================================

    def evaluate(
        self,
        driver_id: str,
        current_offer,
        state: str,
    ) -> WhereAmIResult:
        """Compute current location awareness. Pure read.

        Returns exactly one WhereAmIResult per call. Never raises for
        normal flow; only DB connection errors propagate (Q6 lock:
        bubble errors, don't catch — silent degradation is worse than
        a loud failure in commercial code).

        Algorithm: RFC §4 hierarchical matching. See module docstring.
        """
        # --- Step 1: Cluster check -----------------------------------------
        cluster = self._cluster_fn(driver_id, self.cur)
        if cluster is None:
            return self._not_at_pudo(reason="no cluster detected")

        # --- Step 2: Topology (single pivot_context call, reused below) ----
        topo = self._compute_road_topology(driver_id)

        # --- Step 3: Stop context — STUB for v1.0 (Stop Atlas v1.1) -------
        stop_context = "unknown_stop"

        # --- Step 3.5: Cluster history topology (v2.6 amendment) ----------
        # Compute cluster_revisit only when an active offer anchors the
        # lookback window (Q2 ratification). Without an offer there's no
        # "current ride" for round-trip semantics; cluster_revisit is
        # ride-scoped, not driver-scoped.
        if current_offer is not None:
            recent_clusters = self._recent_clusters_fn(
                driver_id, self.cur,
                accepted_at_anchor=current_offer.accepted_at,
            )
            cluster_revisit = _compute_cluster_revisit(cluster, recent_clusters)
        else:
            cluster_revisit = False

        # --- Step 4: Current-ride PUDO matching ---------------------------
        if current_offer is not None:
            current_outcome = self._match_current_pudo(
                cluster, topo, current_offer, state,
            )
            if current_outcome is not None and current_outcome.matched:
                return self._build_current_result(
                    outcome=current_outcome,
                    topo=topo,
                    stop_context=stop_context,
                    cluster=cluster,
                    offer=current_offer,
                    state=state,
                    cluster_revisit=cluster_revisit,
                )

        # --- Step 5: Ghost cache READ (Q12 lock: read-only) ---------------
        ghost_result = self._match_ghost_cache(
            driver_id, cluster, topo, stop_context, cluster_revisit,
        )
        if ghost_result is not None:
            return ghost_result

        # --- Step 6: Cluster exists, no offer or ghost explains it --------
        # Per Q12: WAI does NOT INSERT here. PLAN consumer (Phase E)
        # decides whether to persist a suspect into suspected_pudos.
        return self._at_unknown_pudo(cluster, topo, stop_context, cluster_revisit)

    # =========================================================================
    # Topology adapter
    # =========================================================================

    def _compute_road_topology(self, driver_id: str) -> _RoadTopology:
        """Map pivot_context.get_pivot_context() output onto _RoadTopology.

        Per Q7: all topology comes from a single backward heartbeat_log
        scan inside pivot_context — no additional queries here. The
        adapter only reshapes and computes off_wire_duration_s in Python.

        Per Q14: time arithmetic uses datetime.now(timezone.utc) against
        the timestamptz returned by pivot_context. Defensive max(0, ...)
        guards against clock skew (negative durations would break
        downstream comparisons).
        """
        # Real signature is (driver_id, cur, anchor_time=None) per fact-check
        ctx = self._pivot_fn(driver_id, self.cur)

        on_wire = ctx["on_wire"]
        pivot_time = ctx.get("pivot_time")

        if on_wire:
            off_wire_duration_s = 0
        elif pivot_time is None:
            # Off-wire but no pivot recorded. Sparse trail / new session /
            # never been on a named road. Fail-closed: 0 ensures apartment
            # matchers (which require off_wire_duration_s > 10) don't fire
            # on unknowable trails.
            off_wire_duration_s = 0
        else:
            delta = datetime.now(timezone.utc) - pivot_time
            off_wire_duration_s = max(0, int(delta.total_seconds()))

        breadcrumb_raw = ctx.get("breadcrumb") or ()
        # pivot_context returns a list; convert to tuple for frozen dataclass
        breadcrumb = tuple(breadcrumb_raw)

        return _RoadTopology(
            on_wire=on_wire,
            current_road=ctx.get("current_road"),
            last_named_road=ctx.get("last_named_road"),
            off_wire_duration_s=off_wire_duration_s,
            breadcrumb=breadcrumb,
        )

    # =========================================================================
    # Current-ride PUDO matching (state-aware)
    # =========================================================================

    def _match_current_pudo(
        self,
        cluster: Cluster,
        topo: _RoadTopology,
        offer,
        state: str,
    ) -> Optional[_MatchOutcome]:
        """Select target(s) by state, dispatch to class matcher, return best.

        Q3 STACKED tie-break: test both primary and secondary, return
        whichever has higher confidence; primary wins on tie because
        targets list is in priority order and max() is stable.

        Returns None if no targets apply to this state (e.g., UNCOMMITTED).
        """
        targets = self._targets_for_state(offer, state)
        if not targets:
            return None

        outcomes = []
        for target, pudo_type in targets:
            matcher = _CLASS_DISPATCH.get(target.address_class)
            if matcher is None:
                log.warning(
                    "[WAI] unknown address_class=%r for target — skipping",
                    target.address_class,
                )
                continue
            outcome = matcher(cluster, topo, target)
            outcomes.append((outcome, target, pudo_type))

        if not outcomes:
            return None

        # Q3: best confidence wins; primary breaks ties via stable max().
        # outcomes is in priority order: primary first, secondary second.
        best_triple = max(outcomes, key=lambda triple: triple[0].confidence)
        best_outcome, best_target, best_pudo_type = best_triple

        # Re-emit the outcome with pudo_type and target_address populated.
        # The matcher itself can't know pudo_type; that's our job here.
        return _MatchOutcome(
            matched=best_outcome.matched,
            confidence=best_outcome.confidence,
            corrected_lat=best_outcome.corrected_lat,
            corrected_lng=best_outcome.corrected_lng,
            reason=best_outcome.reason,
            pudo_type=best_pudo_type,
            target_address=getattr(best_target, "address", None),
            signals=best_outcome.signals,
        )

    def _targets_for_state(
        self,
        offer,
        state: str,
    ) -> list:
        """Return [(target, pudo_type), ...] in priority order.

        State -> target mapping (Step 4 lock + Step 5 inspection):
          UNCOMMITTED:    []  (no current offer; S11 promotion is EXECUTE's job)
          ENROUTE:        [(pickup, "pickup")]
          IN_TRIP:        [(dropoff, "dropoff")]
          REFINE_DROPOFF: [(dropoff, "dropoff")]  (synonym of IN_TRIP)
          STACKED:        [(primary_dropoff, "dropoff"),
                           (secondary_dropoff, "dropoff")]

        Per Step 4 fact-check: in STACKED state, offer.dropoff IS the primary
        (the one currently being completed) and offer.secondary_dropoff is
        the queued one. Both are tested per Q3 ruling.

        REFINE_PICKUP intentionally absent — verdict label, not a state.
        """
        if state == States.UNCOMMITTED:
            # WAI cannot match against current PUDOs (no active offer in scope).
            # S11 (declined-offer scan) is owned by EXECUTE layer.
            return []
        if state == States.ENROUTE:
            return [(offer.pickup, "pickup")]
        if state in (States.IN_TRIP, States.REFINE_DROPOFF):
            return [(offer.dropoff, "dropoff")]
        if state == States.STACKED:
            targets = [(offer.dropoff, "dropoff")]
            if offer.secondary_dropoff is not None:
                targets.append((offer.secondary_dropoff, "dropoff"))
            return targets
        return []

    # =========================================================================
    # Ghost cache READ (the only SQL in WAI)
    # =========================================================================

    def _match_ghost_cache(
        self,
        driver_id: str,
        cluster: Cluster,
        topo: _RoadTopology,
        stop_context: str,
        cluster_revisit: bool,
    ) -> Optional[WhereAmIResult]:
        """READ-ONLY ghost-cache lookup (Q12).

        Returns at_previous_pudo if an unresolved suspect within 50m of the
        cluster median exists. Returns None if no ghost.

        Topology is threaded through (Q2) so the result describes the CURRENT
        moment alongside the historical evidence — useful for forensics.
        """
        self.cur.execute(_GHOST_CACHE_SQL, (
            driver_id,
            cluster.median_lat,
            cluster.median_lng,
            GHOST_MATCH_RADIUS_M,
        ))
        row = self.cur.fetchone()
        if row is None:
            return None

        # Dict access per production audit (Step 5.7.1): the original Q1
        # ruling (tuple unpacking) was based on a faulty assumption that
        # production used default tuple cursors. The Step 5.7 live-PG smoke
        # audit revealed that EVERY production caller of WAI's dependencies
        # (pickup_confirm.py, geo.py, etc.) uses psycopg2.extras.RealDictCursor.
        # detect_cluster already requires this — see cluster_detection.py:154
        # `row["n"]`. WAI must follow the same convention or it crashes on
        # the first real ghost match. The original Q1 reasoning (perf via
        # tuple unpacking) is moot when the cursor type isn't tuple anyway.
        ghost_id = row["id"]
        lat = row["lat"]
        lng = row["lng"]
        offer_id_at_time = row["offer_id_at_time"]
        confidence = row["confidence"]
        detected_at = row["detected_at"]

        if log.isEnabledFor(logging.DEBUG):
            log.debug(
                "[WAI ghost_match] ghost_id=%s offer=%s detected=%s "
                "ghost_coords=(%.6f, %.6f) cluster=(%.6f, %.6f) conf=%.2f",
                ghost_id, offer_id_at_time, detected_at,
                lat, lng, cluster.median_lat, cluster.median_lng, float(confidence),
            )

        return WhereAmIResult(
            status="at_previous_pudo",
            pudo_type=None,
            offer_id=offer_id_at_time,
            corrected_lat=cluster.median_lat,
            corrected_lng=cluster.median_lng,
            on_wire=topo.on_wire,
            current_road=topo.current_road,
            on_target_road=False,
            off_wire_duration_s=topo.off_wire_duration_s,
            stop_context=stop_context,
            confidence=float(confidence),
            reason=f"ghost_match id={ghost_id} offer={offer_id_at_time}",
            target_address=None,
            ghost_id=int(ghost_id),
            cluster=cluster,
            cluster_revisit=cluster_revisit,
        )

    # =========================================================================
    # Result builders
    # =========================================================================

    def _not_at_pudo(self, reason: str) -> WhereAmIResult:
        """Construct a not_at_pudo result with neutral defaults."""
        return WhereAmIResult(
            status="not_at_pudo",
            pudo_type=None,
            offer_id=None,
            corrected_lat=None,
            corrected_lng=None,
            on_wire=False,
            current_road=None,
            on_target_road=False,
            off_wire_duration_s=0,
            stop_context="not_stopped",
            confidence=0.0,
            reason=reason,
            target_address=None,
            ghost_id=None,
            cluster=None,
            cluster_revisit=False,
        )

    def _at_unknown_pudo(
        self,
        cluster: Cluster,
        topo: _RoadTopology,
        stop_context: str,
        cluster_revisit: bool,
    ) -> WhereAmIResult:
        """Cluster exists, no offer matches, no ghost matches."""
        return WhereAmIResult(
            status="at_unknown_pudo",
            pudo_type=None,
            offer_id=None,
            corrected_lat=cluster.median_lat,
            corrected_lng=cluster.median_lng,
            on_wire=topo.on_wire,
            current_road=topo.current_road,
            on_target_road=False,
            off_wire_duration_s=topo.off_wire_duration_s,
            stop_context=stop_context,
            confidence=0.0,
            reason="cluster detected but no offer or ghost match",
            target_address=None,
            ghost_id=None,
            cluster=cluster,
            cluster_revisit=cluster_revisit,
        )

    def _build_current_result(
        self,
        outcome: _MatchOutcome,
        topo: _RoadTopology,
        stop_context: str,
        cluster: Cluster,
        offer,
        state: str,
        cluster_revisit: bool,
    ) -> WhereAmIResult:
        """Assemble at_current_pudo result from a winning matcher outcome.

        offer_id resolution: for non-STACKED states, offer.offer_id is
        unambiguous. For STACKED, offer.offer_id is the secondary's ID
        (per canonical pointer rule); when the primary wins the match,
        the offer_id reported here is still the secondary's. This is a
        known limitation: TargetSpec doesn't carry the primary offer_id,
        and per the Offer docstring, WAI does not query offer_history.
        Phase F can address this if downstream consumers need primary
        attribution; for now, target_address (which IS captured) is the
        unambiguous identifier.

        on_target_road in the result reflects the matched outcome's
        signal value: 1.0 if the matcher saw on_target_road=1.0 in its
        signal computation, else False. Without the matcher exposing
        signals dict (already present per Step 4 Q5), we'd have to
        recompute; with it, we can read.
        """
        # Pull on_target_road from the outcome's signals dict if present
        on_target_road_signal = (
            outcome.signals.get("on_target_road", 0.0)
            if outcome.signals is not None
            else 0.0
        )

        return WhereAmIResult(
            status="at_current_pudo",
            pudo_type=outcome.pudo_type,
            offer_id=getattr(offer, "offer_id", None),
            corrected_lat=outcome.corrected_lat,
            corrected_lng=outcome.corrected_lng,
            on_wire=topo.on_wire,
            current_road=topo.current_road,
            on_target_road=on_target_road_signal >= 1.0,
            off_wire_duration_s=topo.off_wire_duration_s,
            stop_context=stop_context,
            confidence=outcome.confidence,
            reason=outcome.reason,
            target_address=outcome.target_address,
            ghost_id=None,
            cluster=cluster,
            cluster_revisit=cluster_revisit,
        )
