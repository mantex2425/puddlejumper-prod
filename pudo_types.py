"""
pudo_types.py — shared dataclass contracts for the PUDO awareness/planning subsystem.

Top-level peer of cluster_detection.py and where_am_i.py. Both DIAGNOSE
primitives (WAI) and PLAN consumers (pudo_planner.py, Phase E) import from
here. Putting these inside either box would create a one-way import that
becomes circular as soon as PLAN feeds signal back into DIAGNOSE.

NB: deliberately NOT named `types.py` — that shadows Python's stdlib `types`
module and would silently break anything in the codebase doing `import types`.

Box discipline (per canonical rules):
  Only DIAGNOSE and PLAN see these. EXECUTE writes via _execute_action
  in driver_heartbeat (post-demolition), which has its own argument set
  and does not import from here.

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
    lat: Optional[float]
    lng: Optional[float]
    address_class: Literal[
        "single_road",
        "intersection",
        "number_on_street",
        "poi",
        "apartment_complex",
        "garbage",
    ]
    named_roads: tuple[str, ...]
    # §XVII Patch 3: offer's raw address text, populated from
    # offer_history.{pickup_address,dropoff_address}. Used by Head 5
    # (_signal_semantic_anchor) as the query for Google Places
    # searchText resolution. Optional/default-None preserves backward
    # compat with the 13+ existing TargetSpec construction sites.
    # Production code at 8 sites uses getattr(target, 'address', None)
    # defensively, anticipating this exact field.
    address: Optional[str] = None


# ============================================================================
# Offer — the bundle of TargetSpecs WAI evaluates against per heartbeat
# ============================================================================

@dataclass(frozen=True)
class Offer:
    """The current ride context handed to WhereAmI.evaluate().

    Per the queue-aware Cut B2 contract (Sprint A, 2026-04-30): an Offer
    represents a single ride leg with a pickup and a dropoff. The "stacked
    rides" concept is deprecated; multi-ride scenarios are represented as
    a list[Offer] passed to WhereAmI.evaluate(), with one Offer per leg.
    Caller (driver_heartbeat.py / decisions.router) assembles the queue;
    WAI does not query offer_history.

    accepted_at (v2.6 amendment, sub-step 1b.1): the offer-acceptance
    timestamp from app_private.offer_history.accepted_at. Anchors the
    cluster-history lookback window in WAI's cluster_revisit topology
    check per Step 6 Amendment 1 (Cut B2 extension: anchor =
    min(accepted_at) across the queue). UTC, timezone-aware.
    """
    offer_id: str
    accepted_at: datetime
    pickup: TargetSpec
    dropoff: TargetSpec
    # Sprint A gate-layer additions (2026-05-06):
    # Used by motion_gate.evaluate_odometer_gate per-leg progress check.
    # All Optional — default None preserves existing fixture constructors.
    pickup_miles: Optional[float] = None
    trip_miles: Optional[float] = None
    leg_start_cumulative_miles_pickup: Optional[float] = None
    leg_start_cumulative_miles_dropoff: Optional[float] = None
    # Phase 2c.2 additions (2026-05-07):
    # Used by tad.compute_offer_expectations() to compute expected pickup
    # and dropoff arrival times. Optional/default-None preserves existing
    # fixture constructors (8 production callers, 1 in tests/test_where_am_i.py).
    # Source: app_private.offer_history.pickup_minutes / trip_minutes (smallint).
    pickup_minutes: Optional[int] = None
    trip_minutes: Optional[int] = None


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


# ============================================================================
# Odometer Band — the single-owner distance band (Step 6, ERRATUM §4)
# ============================================================================
#
# The ONE definition of "is this offer's odometer position within the
# plausible band for its leg." Both the liveness predicate (driver_queue
# LIVE_OFFER_PREDICATE_SQL) and TAD's candidacy gate (tad._evaluate_distance_gate)
# are intended to read from here, eliminating the multi-finger split that lost
# offer 9132 (ERRATUM §2.3: the liveness predicate consumed the wrong column).
#
# The band is per-leg, centered on the absolute cumulative anchor, with width
# scaled to that leg's journey distance (NOT to expected_odometer, which is a
# large accumulating number — ERRATUM §4, correcting FINDING §5.2):
#
#   center    = expected_distance        (tad.py's expected_pickup_distance for
#                                          the pickup leg, expected_dropoff_distance
#                                          for the dropoff leg — consumed as-is,
#                                          NO bridge term per ERRATUM §2)
#   tolerance = max( 0.15 * leg_distance,  ODOMETER_BAND_NOISE_FLOOR_MI )
#
#   where leg_distance is the CALLER's choice:
#     pickup  leg -> pickup_miles          (ERRATUM §1.1, proven == TAD gate)
#     dropoff leg -> trip_miles            (ERRATUM §1.3, ratified 2026-06-05)
#
# Edge semantics are the CALLER's, not the primitive's:
#   liveness  (driver_queue) enforces the UPPER edge only — an offer the driver
#             has overshot is reaped; a not-yet-reached offer stays live.
#   candidacy (TAD/WAI) enforces the LOWER edge — a not-yet-reached offer is
#             withheld from spend/scoring but not reaped.
#   The primitive returns the symmetric (center, tolerance); each caller applies
#   the edge it owns. This keeps the band arithmetic single-source while letting
#   the two axes treat the edges per their distinct responsibilities.
#
# NULL handling (ERRATUM §4, §5.5 deferred sentinel): when expected_distance or
# leg_distance is None (e.g. lost-mode receipt with no anchor, or a dropoff leg
# whose pickup never fired so cumulative_miles_at_pickup_fire is NULL), there is
# NO band. The primitive returns None. Callers MUST route None to the §5.5
# deferred path (offer stays alive, resolved at dropoff-disambiguation or the
# 4-hour abandonment ceiling) — NEVER fabricate a band, NEVER reap on absence.

# SYSTEM INVARIANT (TAD-unification Option A, ratified 2026-06-05): this 2.0 is
# a WIDTH FLOOR — the minimum band tolerance, insulating against telemetry/GPS
# jitter so a short leg's 15% half-width is never tighter than sensor noise. It
# is COINCIDENTALLY EQUAL to tad.SHORT_TRIP_THRESHOLD_MILES (2.0), which is a
# different concept — a mode-switch boundary, not a width. Do NOT couple or
# unify these two 2.0 constants; they protect independent domains (telemetry
# insulation here vs routing mode-switch there) and would drift in different
# directions if retuned.
ODOMETER_BAND_NOISE_FLOOR_MI: float = 2.0
"""Canonical minimum band half-width, in miles (FINDING §6.2, ratified 2026-06-05).

The band is never narrower than this regardless of leg distance. A short trip
(e.g. a 1.6-mile fare) has a 15%% half-width of ~0.24 mi, tighter than GPS/
odometer sensor noise; the floor insulates against false reaping/exclusion on
such trips. Re-calibrate against real jitter data post-launch if needed.

SINGLE OWNER. Supersedes driver_queue.GC_MIN_DIST_MI (also 2.0) — the Step-6
predicate work retires that constant IN FAVOR of this one rather than keeping a
second copy (the "two fingers on one quantity" anti-pattern this whole effort
exists to kill). Distinct from refinement_gates.NOISE_FLOOR_MILES (0.3), which
is a minimum-trip-length noise gate — a different quantity entirely.
"""

ODOMETER_BAND_TOLERANCE_PCT: float = 0.15
"""Band half-width as a fraction of the leg's journey distance (ERRATUM §4).

15%% of leg_distance. Proven term-for-term identical to the deployed TAD
[0.85, 1.15] completion gate on the pickup leg (ERRATUM §1.1): TAD's
completion_pct in [0.85, 1.15] rearranges exactly to
|actual_odometer - expected_pickup_distance| <= 0.15 * pickup_miles.
"""


def odometer_band(
    expected_distance: Optional[float],
    leg_distance: Optional[float],
) -> "Optional[tuple[float, float]]":
    """Return the (center, tolerance) odometer band for one leg, or None.

    The single-owner band primitive (ERRATUM §4). Pure arithmetic; no I/O,
    no DB, no side effects. Idempotent.

    Args:
        expected_distance:
            The leg's absolute cumulative odometer target — tad.py's
            expected_pickup_distance (pickup leg) or expected_dropoff_distance
            (dropoff leg), consumed as-is. NO bridge term (ERRATUM §2).
            None means "no anchor" (lost-mode / unfired-pickup dropoff leg).
        leg_distance:
            The leg's journey distance — pickup_miles (pickup leg) or
            trip_miles (dropoff leg). The CALLER selects which. None means
            "leg distance unknown" (offer missing required fields).

    Returns:
        (center, tolerance) where:
            center    = expected_distance
            tolerance = max(ODOMETER_BAND_TOLERANCE_PCT * leg_distance,
                            ODOMETER_BAND_NOISE_FLOOR_MI)
        OR None when expected_distance or leg_distance is None — signaling
        "no band; route to the §5.5 deferred sentinel" (NEVER fabricate a
        band, NEVER reap on absence; ERRATUM §4 / §0.D.4 absence-is-not-death).

    Note on negative/zero inputs: a genuine 0.0 or negative leg_distance is
    coerced through the noise floor (max clamps it to the floor), so a
    degenerate leg can never produce a zero-width band. expected_distance is
    passed through as center unchanged (the accessor normalizes the band, not
    the anchor's validity — consistent with _coerce_odometer's "coerce, don't
    validate" discipline from Step 4).
    """
    if expected_distance is None or leg_distance is None:
        return None
    tolerance = max(
        ODOMETER_BAND_TOLERANCE_PCT * float(leg_distance),
        ODOMETER_BAND_NOISE_FLOOR_MI,
    )
    return (float(expected_distance), tolerance)


def odometer_in_band(
    actual_odometer: Optional[float],
    expected_distance: Optional[float],
    leg_distance: Optional[float],
) -> "Optional[bool]":
    """Return whether actual_odometer is within the leg's symmetric band.

    The shared in-band test both the liveness predicate and TAD's gate are
    intended to consult, so the band membership question has exactly one
    implementation (ERRATUM §4 / Andrew's "one owner").

    Returns:
        True  — |actual_odometer - center| <= tolerance (in band).
        False — outside the band (caller decides upper-vs-lower edge meaning).
        None  — no band (expected_distance, leg_distance, or actual_odometer is
                None) → route to §5.5 deferred. NEVER coerced to True/False;
                absence forces the deferred branch and fails loud, mirroring the
                NULL-not-zero discipline of Step 4's _coerce_odometer.

    This is the SYMMETRIC test. Callers needing edge-specific behavior
    (liveness = reap only on upper-edge breach; candidacy = withhold only on
    lower-edge) compute the signed comparison themselves from odometer_band()'s
    (center, tolerance); odometer_in_band is the convenience wrapper for the
    symmetric question.
    """
    if actual_odometer is None:
        return None
    band = odometer_band(expected_distance, leg_distance)
    if band is None:
        return None
    center, tolerance = band
    return abs(float(actual_odometer) - center) <= tolerance


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
# OfferMeta — per-offer metadata threaded to dispatch for tiebreaker decisions
# ============================================================================

@dataclass(frozen=True)
class OfferMeta:
    """Per-offer metadata threaded to dispatch for tiebreaker decisions.

    Per CANONICAL_RULES.md §XIV.I: strict scope. Carries only the timestamp
    dispatch needs for the recency tiebreaker (§5.3 two-pickups case).
    Adding fields requires a separate canonical amendment.

    Per CANONICAL_RULES.md §III (UTC-Mandatory): created_at must be
    timezone-aware UTC. The __post_init__ guard rejects naive datetimes at
    construction, preventing silent ordering bugs downstream from tzinfo
    stripping anywhere in the snapshot path.

    Failure mode the guard prevents: if snap.offers[i].created_at gets
    stripped of tzinfo somewhere upstream (via .replace(tzinfo=None) or
    .astimezone(None) or unaware fromtimestamp), two naive datetimes from
    different timezones would compare as if same-zone — the recency
    tiebreaker silently picks the wrong winner. Constructor guard makes
    this fail at the source, not at the comparison.
    """
    created_at: datetime  # UTC-aware; sourced from offer_history.created_at

    def __post_init__(self):
        if self.created_at.tzinfo is None:
            raise ValueError(
                f"OfferMeta.created_at must be timezone-aware (Rule III). "
                f"Got naive datetime: {self.created_at!r}"
            )


