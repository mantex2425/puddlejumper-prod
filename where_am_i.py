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


def _haversine_meters(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in meters between two lat/lng points.

    Used only inside DIAGNOSE signal scoring. All write-path coordinates
    and all geometric operations that go to PG continue to use the
    app_private.* canonical functions.

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

    distance_m = _haversine_meters(
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
