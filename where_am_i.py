"""
where_am_i.py — Continuous location awareness primitive.

Pure DIAGNOSE layer per the 4-Box Controller. Reads only.
No sm_transition calls, no suspected_pudos INSERTs, no Discord pings,
no offer_history mutations. PLAN consumer (pudo_planner.py, Phase E)
owns all writes derived from this module's output.

Public surface (Cut B2, Sprint A):
    WhereAmI(cur)                          - instantiate with a psycopg cursor
    WhereAmI.evaluate(driver_id, queue)    - returns list[WAIMatch]
    WhereAmI.evaluate_with_diagnostics(...) - returns (list[WAIMatch], DiagnosticContext)

Algorithm: see WHERE_AM_I_PROPOSAL_v2.md sections 4 and 5.

Module structure:
    - Constants and confidence weights (locked in Steps 1-3)
    - Section A: signal library (this file as of Step 5.2)
    - Section B: plumbing - MatchOutcome, _weighted_confidence,
      _render_reason, _validate_target (Step 5.3)
    - Section C: per-class matchers and _CLASS_DISPATCH (Step 5.4)
    - Section D: WhereAmI class with evaluate() and helpers (Step 5.5)
"""
from __future__ import annotations

import logging
import math
import re
from typing import Optional

from rapidfuzz import fuzz

from bead_on_wire import _AIRLINE_AIRPORT_TOKENS, detect_branded_token
from cluster_detection import Cluster
from pivot_context import _road_names_match
from poi_service import POI

log = logging.getLogger(__name__)

# Resurrection alert: per-process flag flipped to True on the first POI
# lookup that returns a non-empty witness list. Logged once per Cloud Run
# container start. Removed once production data confirms the resurrection
# is stable — see Phase 2c.2 sprint completion criteria.
_first_poi_success_logged: bool = False


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

# Patch 2a (Phase 2c.2, 2026-05-05): caller-internal radius for
# _signal_poi_match. Independent of poi_service.DEFAULT_RADIUS_M
# (which governs cache-lookup radius) -- gives the matcher its own
# threshold to filter POIs before computing co-reference scores.
POI_RADIUS_M = 100.0

# Item 2 (Phase 2c.2, 2026-05-08): CLASS_TO_TYPE_MAP for Head 4
# (_signal_poi_type_match). Maps Uber's address_class -> the set of
# Google Places types that qualify as a valid type-match witness.
#
# Houston insight (validated against 6+ audit cases): three of the five
# address classes are coordinate-naming conventions, not destination-type
# filters. Numbered addresses (Pappasito's at 10005 FM 1960), road-only
# addresses (Sienna Parkway, US-90), AND intersections (21st & Palmsprings,
# Curtis & Wafer, tire-shop-in-residential) all routinely resolve at any
# kind of commercial destination -- strip-mall storefronts, corner stores,
# tire shops between houses. The 7 weighted signals discriminate WHERE
# the cluster is; Head 4 is the FOURTH tool that confirms WHAT is there.
#
# Witness signal architecture (Bible Rule 2): NOT in _CONFIDENCE_WEIGHTS.
# Populates MatchOutcome.poi_type_match (boolean) and
# MatchOutcome.poi_type_witness (string). Consumed by Item 3's evaluate()
# integration via Rule 3a (Normal Mode) and Rule 3b (Lost Mode).
_STREET_ADDRESS_DESTINATIONS = frozenset({
    # Food & beverage
    "restaurant", "bar", "cafe", "bakery", "meal_takeaway", "meal_delivery",
    # Retail
    "store", "supermarket", "shopping_mall", "convenience_store",
    "clothing_store", "department_store", "electronics_store",
    "furniture_store", "home_goods_store", "hardware_store",
    "pet_store", "shoe_store", "book_store", "jewelry_store",
    "liquor_store", "florist",
    # Auto
    "gas_station", "car_repair", "car_wash", "car_dealer",
    # Health & wellness
    "doctor", "dentist", "hospital", "pharmacy", "veterinary_care",
    "physiotherapist",
    # Financial & professional services
    "bank", "atm", "post_office", "insurance_agency", "accounting",
    "lawyer", "real_estate_agency",
    # Personal services
    "gym", "spa", "beauty_salon", "hair_care",
    # Hospitality
    "lodging",
    # Civic / educational (commonly numbered or intersection-located in Houston)
    "school", "university", "library", "museum",
    "church", "place_of_worship",
})

CLASS_TO_TYPE_MAP: dict[str, frozenset] = {
    # number_on_street, single_road, intersection: coordinate-naming
    # conventions for the same destination universe in Houston.
    "number_on_street": _STREET_ADDRESS_DESTINATIONS,
    "single_road":      _STREET_ADDRESS_DESTINATIONS,
    "intersection":     _STREET_ADDRESS_DESTINATIONS,
    # apartment_complex: residential anchor itself, not commercial neighbors.
    # Conservative -- protects against no-zoning false-positives where a
    # commercial POI sits across the street from a residential cluster.
    "apartment_complex": frozenset({
        "lodging",
        "real_estate_agency",
        "premise",
    }),
    # poi: address itself names a destination (airport, university, stadium).
    "poi": frozenset({
        "airport", "train_station", "subway_station", "transit_station",
        "bus_station",
        "stadium", "tourist_attraction", "amusement_park", "zoo", "museum",
        "shopping_mall", "university", "school", "library",
        "hospital", "church", "place_of_worship", "park",
    }),
}

# Patch 2b (Phase 2c.2, 2026-05-05): _signal_poi_match Option B
# noise-gate KEY. Matches leading street number (e.g. "7623 Forum
# Park Dr" but not "Forum Park Dr"). Per the 2026-05-05 audit (2101
# offers), street-number presence is the disambiguator that actually
# correlates with real-address-vs-road-name distinction. R4's old
# global 0.2 multiplier was demolished because 96.5% of branded
# matches lacked street number; Option B uses presence as a gate KEY
# only, not a continuous score component.
_STREET_NUMBER_RE = re.compile(r"^\s*\d+\b")

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
        "proximity":            0.10,
        "breadcrumb_match":     0.30,
        "cluster_tightness":    0.15,
        "cluster_duration":     0.10,
        "on_target_road":       0.20,
        "off_wire_pivot":       0.05,
        "adjacent_road_match":  0.10,
    },
    "single_road": {
        "proximity":            0.05,
        "breadcrumb_match":     0.35,
        "cluster_tightness":    0.15,
        "cluster_duration":     0.15,
        "on_target_road":       0.15,
        "off_wire_pivot":       0.05,
        "adjacent_road_match":  0.10,
    },
    "number_on_street": {
        "proximity":            0.30,
        "breadcrumb_match":     0.20,
        "cluster_tightness":    0.15,
        "cluster_duration":     0.10,
        "on_target_road":       0.15,
        "off_wire_pivot":       0.00,
        "adjacent_road_match":  0.10,
    },
    "apartment_complex": {
        "proximity":            0.10,
        "breadcrumb_match":     0.10,
        "cluster_tightness":    0.20,
        "cluster_duration":     0.15,
        "on_target_road":       0.05,
        "off_wire_pivot":       0.40,
        "adjacent_road_match":  0.00,
    },
}


# §XVII Patch 2: per-anchor-type horizons for Head 5 semantic-anchor scoring.
# Keys are Google Places v1 types-array values (case-sensitive; lowercase).
# Lookup is first-match-wins against each anchor's types list — order
# inside the anchor matters but Google sorts by relevance, so first match
# usually IS the most specific applicable type.
#
# Horizons reflect real-world venue geometry per canonical §XVII §C:
#   - Airports/stadiums: huge polygons, passenger drop happens anywhere
#     in the complex (IAH terminals span ~1.5km end-to-end).
#   - Universities/malls: large but more compact; 500m covers most.
#   - Hospitals/lodging: tight footprints, driver must reach specific
#     building; 150m prevents wrong-hospital false matches in dense
#     medical center districts.
#   - Default 500m: anything Google returns without one of the above
#     types gets a moderate horizon. Empirically calibrated by the
#     Tier A backtest (2026-05-14).
#
# Maintenance discipline (canonical §XVII §K): this map keys off Google's
# own taxonomy. Adding a new market doesn't require extending it. Adding
# new entries here requires Gemini ratification — it's the only category
# step in §XVII and bypassing review reintroduces the lexicon-maintenance
# trap §XVII was built to eliminate.
_SEMANTIC_TYPE_HORIZON_MAP: dict[str, float] = {
    "airport": 1000.0,
    "international_airport": 1000.0,
    "stadium": 600.0,
    "tourist_attraction": 600.0,
    "amusement_park": 600.0,
    "zoo": 600.0,
    "university": 500.0,
    "shopping_mall": 500.0,
    "hospital": 150.0,
    "medical_clinic": 150.0,
    "doctor": 150.0,
    "lodging": 150.0,
}

# Default horizon for anchors whose types don't match any key above.
_SEMANTIC_DEFAULT_HORIZON_M: float = 500.0


# §XVII Patch 3: fixed market-center bias for Head 5 searchText calls.
# Per Andrew + Gemini ratification 2026-05-14, the locationBias center
# is FIXED to Houston downtown (not cluster centroid). Eliminates cache
# fragmentation — IAH's 7+ sub-clusters now share one cache row per
# unique offer text. Trade-off: 1% slight relevance reduction (Google
# may slightly reorder anchors when biased far from cluster) vs.
# ~7x cache hit rate increase for high-volume venues.
#
# When PuddleJumper expands beyond Houston, this becomes a per-market
# lookup keyed on driver location. For now, single-market is correct.
_HOUSTON_BIAS_LAT: float = 29.7604
_HOUSTON_BIAS_LNG: float = -95.3698


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


def _signal_adjacent_road_match(
    adjacent_roads: tuple[str, ...],
    target_road_names: tuple[str, ...],
    current_road_class: Optional[str] = None,
) -> float:
    """Is any of the target's named roads in the cluster's adjacent-roads
    whitelist?

    Sprint 2 Step E (B.1-B.6 ratification). Complements _signal_on_target_road
    for the off-wire case: when the GPS-snapped current_road is a private
    driveway, internal lot road, or null, but the cluster IS physically
    adjacent (within ADJACENCY_BUFFER_M = 150m) to a road named in the
    target's address.

    Solves the Planet Fitness / strip-mall back-entrance / hospital
    parking-lot cases where on_target_road is 0 because the snap missed.

    TRANSIT GATE (Phase 1A, 2026-05-04 — Operation Strip Mall):
    adjacency rescue is suppressed when current_road_class is 'transit'
    (motorway / motorway_link / trunk / trunk_link / primary /
    primary_link / secondary / tertiary — OSM tag_id 101-109).

    Rationale: a driver on a tertiary road is in transit-mode, not
    "near a destination, off-wire". The McKeever-Sienna case
    (offer 8336, ride 1, 2026-05-04) produced a false-positive dropoff
    cluster at a McKeever Rd traffic light because Sienna Pkwy ran 32m
    away. Adjacency was designed for off-wire cases (parking lots,
    residential side streets); applying it to transit roads
    misclassifies "stuck in traffic near the dropoff" as "arrived at
    the dropoff."

    Step function (boolean by nature):
      - current_road_class == 'transit':     0.0  (gated, regardless of overlap)
      - any target_road in adjacent_roads:  1.0
      - adjacent_roads is empty:            0.0
      - target_road_names is empty:         0.0
      - no overlap:                         0.0

    Reuses pivot_context._road_names_match for canonical normalization.
    """
    # Transit gate: driver is on a primary/secondary/tertiary road,
    # adjacency rescue does not apply.
    if current_road_class == 'transit':
        return 0.0

    if not adjacent_roads or not target_road_names:
        return 0.0
    for adjacent_road in adjacent_roads:
        for target_road in target_road_names:
            if _road_names_match(adjacent_road, target_road):
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

    Signature note: takes the four fields directly rather than a RoadTopology
    object. RoadTopology is defined in Section D (Step 5.5); Section A
    deliberately has no dependency on it so signal tests don't need the
    topology-construction machinery. Section C matchers will assemble the
    arguments from RoadTopology.
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
# MatchOutcome is the contract between matchers and evaluate(): every
# matcher returns one. evaluate() reads the fields directly to assemble
# the final MatchOutcome.
#
# Field ordering follows Gemini Step 5.3 Q1 ruling: verdict first
# (matched, confidence), then geography (corrected coords), then
# context (reason, attribution), then metadata (signals).


def _signal_poi_match(
    pois: list,
    target_address: str,
) -> tuple[float, Optional[str]]:
    """3-headed POI co-reference signal with Option B noise-gate.

    Heads:
      1. Fuzzy: rapidfuzz.fuzz.partial_ratio between each POI name and
         the target address. Continuous [0, 1.0]. Witness:
         "fuzzy:{poi_name}" of the highest-scoring POI.
      2. Branded co-reference: detect_branded_token applied to both
         target and each POI name; both must produce the same token
         to fire. Binary 1.0. Witness: "branded:{token}".
      3. Airport-type co-reference: target address contains an
         airline/airport token AND any near-POI has substring
         "airport" in its types list. Binary 0.9 (capped below
         Head 2 because the POI-side anchor is type-based not
         name-based). Witness: "airport_type:{token}".

    Final score = max(head1, head2, head3). Witness from winning head.

    Option B noise-gate (Gemini ratification 2026-05-05):
      - Head 1 winner: bypass (no token concept; rapidfuzz score is
        already partial-ratio-grounded).
      - Head 3 winner: bypass (all tokens in _AIRLINE_AIRPORT_TOKENS
        verified CLEAN in HIGH_NOISE per 2c.1 audit).
      - Head 2 winner with HIGH_NOISE token AND no street number in
        target: cap final score at 0.5.
      - Otherwise: return signal score unchanged.

    Provenance: production audit 2026-05-05 (2101 offers, 4202
    address-instances) demolished R4's global 0.2 multiplier
    (96.5% of real branded matches lacked street number; multiplier
    locked out R7 floor relaxation). Option B uses street_number as
    gate KEY only, where data shows it actually disambiguates road-
    name from address.

    Args:
        pois: list of POI records (typically from POILookupResult.pois).
            Distance-prefiltered internally to POI_RADIUS_M (100m).
        target_address: the offer's address string (pickup or dropoff).

    Returns:
        (score, witness) tuple. score in [0.0, 1.0]; witness is
        provenance string or None when score is 0.0.
    """
    if not pois or not target_address:
        return 0.0, None

    # Distance pre-filter -- caller-internal radius independent of cache
    near_pois = [p for p in pois if p.dist_m <= POI_RADIUS_M]
    if not near_pois:
        return 0.0, None

    # Head 1: fuzzy partial-ratio between each near-POI name and target
    head1_score = 0.0
    head1_witness: Optional[str] = None
    for p in near_pois:
        s = fuzz.partial_ratio(p.name, target_address) / 100.0
        if s > head1_score:
            head1_score = s
            head1_witness = f"fuzzy:{p.name}"

    # Head 2: branded co-reference (token equality on target side and POI side)
    target_branded = detect_branded_token(target_address)
    head2_score = 0.0
    head2_witness: Optional[str] = None
    head2_token_high_noise = False
    if target_branded is not None:
        target_token, target_high_noise = target_branded
        for p in near_pois:
            poi_branded = detect_branded_token(p.name)
            if poi_branded is not None and poi_branded[0] == target_token:
                head2_score = 1.0
                head2_witness = f"branded:{target_token}"
                head2_token_high_noise = target_high_noise
                break

    # Head 3: airport-type co-reference (target token in airline/airport
    # set AND any near-POI has 'airport' substring in types)
    head3_score = 0.0
    head3_witness: Optional[str] = None
    if target_branded is not None:
        target_token, _ = target_branded
        if target_token in _AIRLINE_AIRPORT_TOKENS:
            if any("airport" in t for p in near_pois for t in p.types):
                head3_score = 0.9
                head3_witness = f"airport_type:{target_token}"

    # Pick winning head by score
    candidates = [
        (head1_score, head1_witness, "head1"),
        (head2_score, head2_witness, "head2"),
        (head3_score, head3_witness, "head3"),
    ]
    winning_score, winning_witness, winning_head = max(
        candidates, key=lambda c: c[0]
    )

    if winning_score == 0.0:
        return 0.0, None

    # Option B noise-gate: only fires when Head 2 wins with HIGH_NOISE
    # token AND target lacks a leading street number.
    if winning_head == "head2" and head2_token_high_noise:
        if _STREET_NUMBER_RE.match(target_address) is None:
            return min(winning_score, 0.5), winning_witness

    return winning_score, winning_witness


def _signal_poi_type_match(
    pois: list,
    address_class: str,
) -> tuple[bool, Optional[str]]:
    """Head 4 POI type-match witness signal (Phase 2c.2 Item 2, 2026-05-08).

    Sibling to _signal_poi_match (which matches POI NAME against address);
    this matches POI TYPE against address_class. Both are witness signals
    outside _CONFIDENCE_WEIGHTS per Bible Rule 2 -- they corroborate the
    7 weighted signals' spatial answer, never replace it.

    Algorithm:
      1. Filter pois to those within POI_RADIUS_M (100m) of cluster.
      2. Look up the accepted-types set for address_class in CLASS_TO_TYPE_MAP.
      3. First near-POI with any type in the accepted set -> True + witness.
      4. No accepted-type match -> False + None.

    First-match-wins semantics: the witness signal is binary, so there's
    no "best" -- the first qualifying type/POI pair is sufficient
    corroboration.

    Args:
        pois: list of POI records (typically from POILookupResult.pois).
            Distance-prefiltered internally to POI_RADIUS_M (100m).
        address_class: Uber's address class for the target -- one of
            "intersection", "single_road", "number_on_street",
            "apartment_complex", "poi". Unknown classes return False/None.

    Returns:
        (matched, witness) tuple. matched is bool; witness is
        f"poi_type:{matched_type}/{poi_name}" when matched=True,
        else None. Witness format mirrors _signal_poi_match's
        "fuzzy:" / "branded:" / "airport_type:" patterns.
    """
    if not pois or not address_class:
        return False, None
    accepted_types = CLASS_TO_TYPE_MAP.get(address_class)
    if accepted_types is None:
        return False, None

    near_pois = [p for p in pois if p.dist_m <= POI_RADIUS_M]
    if not near_pois:
        return False, None

    for p in near_pois:
        for t in p.types:
            if t in accepted_types:
                return True, f"poi_type:{t}/{p.name}"

    return False, None


from dataclasses import dataclass, field


def _signal_semantic_anchor(
    anchors: list,
    horizon_map: dict[str, float] = None,
    default_horizon: float = None,
) -> tuple[float, Optional[str]]:
    """Head 5 of §XVII: linear-decay confidence over semantic anchors.

    Pure-Python signal function. Mirrors the established matcher-head
    contract: takes precomputed POI list (Option B / Gemini-ratified
    2026-05-14), returns (score, witness). No SQL, no cursor.

    The caller (Patch 3 in evaluate()) is responsible for:
      (a) Fetching anchors via poi_service.get_anchors_for_text using
          the offer's address text as the query.
      (b) Recomputing each anchor's dist_m relative to cluster centroid
          (overwriting the dist_m=0.0 that get_anchors_for_text returns).
          Use app_private.distance_miles per canonical §II.
      (c) Passing the list with cluster-relative dist_m to this function.

    Scoring per canonical §XVII §D:
        score_a = max(0, 1.0 - dist(cluster, a) / h_a)
    where h_a is the horizon for anchor a's type (from horizon_map, with
    default_horizon fallback). Final score = max(score_a over all anchors).

    Witness format per canonical §XVII §F:
        semantic_anchor:{name}/{primary_type} ({dist_m}m)

    Returns:
        (score, witness) tuple. score in [0.0, 1.0]; witness is None
        when no anchor produces a positive score (empty list, all outside
        their horizons, or all anchor records malformed).
    """
    # Default args resolved here, not in signature, so future overrides
    # of the module constants automatically apply without touching callers.
    if horizon_map is None:
        horizon_map = _SEMANTIC_TYPE_HORIZON_MAP
    if default_horizon is None:
        default_horizon = _SEMANTIC_DEFAULT_HORIZON_M

    if not anchors:
        return 0.0, None

    best_score = 0.0
    best_witness: Optional[str] = None

    for a in anchors:
        # Horizon resolution: first match in anchor's types array wins.
        # Google sorts types by relevance, so first-match is typically
        # the most specific applicable type (§XVII §C).
        h_a = default_horizon
        for t in a.types:
            if t in horizon_map:
                h_a = horizon_map[t]
                break

        # Linear-decay confidence weighting (§XVII §D).
        score = max(0.0, 1.0 - (a.dist_m / h_a))

        if score > best_score:
            best_score = score
            # Defensive: empty types array shouldn't happen (poi_service
            # filters incomplete records) but witness must still produce
            # a forensic-legible string if it does.
            primary_type = a.types[0] if a.types else "unknown"
            best_witness = (
                f"semantic_anchor:{a.name}/{primary_type} ({a.dist_m:.0f}m)"
            )

    return best_score, best_witness


@dataclass(frozen=True)
class MatchOutcome:
    """Internal carrier between per-class matchers and evaluate().

    Every _match_<class> function returns one of these. evaluate()
    consumes the fields directly when assembling the final
    MatchOutcome. Frozen because the structure is immutable post-
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
    # all the way through to the MatchOutcome.
    signals: Optional[dict[str, float]]

    # --- POI co-reference (Patch 2a infra shell, wired by Item 2) -------
    # Both pairs default None so existing tests + production constructions
    # are unaffected. Item 2 lands _signal_poi_match wiring (Patch 2c)
    # and _signal_poi_type_match (Head 4) via _build_outcome extension.
    # Both signals are witnesses outside _CONFIDENCE_WEIGHTS per Bible Rule 2.
    poi_match: Optional[float] = None
    poi_witness: Optional[str] = None
    poi_type_match: Optional[bool] = None
    poi_type_witness: Optional[str] = None
    semantic_anchor_score: Optional[float] = None
    semantic_anchor_witness: Optional[str] = None
    # §XVII Patch 4 (2026-05-14): plumbs POILookupResult.source through
    # the matcher pipeline. Values: 'semantic_cache_hit', 'semantic_api_call',
    # 'semantic_api_error', or None (Head 5 not consulted for this target).
    semantic_lookup_source: Optional[str] = None


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
    """Human-readable summary used in MatchOutcome.reason and
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


def _validate_target(target, class_name: str) -> Optional[MatchOutcome]:
    """Fail-closed guard against unusable targets.

    Returns None if `target` is usable (matcher should proceed to
    compute signals). Returns a fail-closed MatchOutcome if the
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
        return MatchOutcome(
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
#   5. Build MatchOutcome
#
# Class-specific behavior is entirely in (a) which proximity threshold gets
# passed to _compute_signals and (b) which _CONFIDENCE_WEIGHTS row applies.
# Per Gemini Step 3 ratification: the weights table is the only "knob"; all
# matchers share the same orchestration shape.
#
# RoadTopology is brought forward from Section D (Gemini Step 5.4 Q1
# ruling). It's a frozen dataclass with the topology fields the matchers
# need. Section D's _compute_road_topology adapter constructs instances
# from get_pivot_context() output; tests construct them directly.


@dataclass(frozen=True)
class RoadTopology:
    """Snapshot of the driver's road-network context at a single heartbeat.

    Constructed in production by _compute_road_topology (Section D) from
    pivot_context.get_pivot_context() output. Constructed in tests
    directly. Frozen because the snapshot is taken at a moment in time
    and shouldn't mutate as the matchers reason about it.

    on_target_road is NOT a field here (Q1 ruling, Step 2). It's
    relationship between topology and a specific TargetSpec, computed
    inside each matcher via _signal_on_target_road.

    adjacent_roads (Sprint 2 Step D, B.1-B.6 ratification):
    Tuple of named roads within ADJACENCY_BUFFER_M (150m) of the
    cluster centroid. Populated by adjacency.get_adjacent_roads_for_cluster
    inside _compute_road_topology. Empty tuple when cluster is None.
    Consumed by _signal_adjacent_road_match (Step E) for off-wire
    matching: when current_road is a private driveway or null but
    the cluster is physically adjacent to the offer's named road.
    """
    on_wire: bool                       # currently snapped to a named road?
    current_road: Optional[str]         # name of the snapped road, else None
    last_named_road: Optional[str]      # most recent named road touched
    off_wire_duration_s: int            # 0 when on_wire; else seconds since pivot
    breadcrumb: tuple[str, ...]         # raw road names, recent -> older
    adjacent_roads: tuple[str, ...] = ()  # named roads within ADJACENCY_BUFFER_M of cluster centroid
    current_road_class: Optional[str] = None  # OSM-derived class: 'transit'|'residential'|'off_wire'|'unknown'|None


def _compute_signals(
    cluster: Cluster,
    topo: RoadTopology,
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
        "adjacent_road_match": _signal_adjacent_road_match(
            topo.adjacent_roads, target.named_roads,
            current_road_class=topo.current_road_class,
        ),
    }


def _build_outcome(
    cluster: Cluster,
    target,
    class_name: str,
    signals: dict[str, float],
    confidence: float,
    poi_match: Optional[float] = None,
    poi_witness: Optional[str] = None,
    poi_type_match: Optional[bool] = None,
    poi_type_witness: Optional[str] = None,
    semantic_anchor_score: Optional[float] = None,
    semantic_anchor_witness: Optional[str] = None,
    semantic_lookup_source: Optional[str] = None,
) -> MatchOutcome:
    """Assemble a MatchOutcome from computed signals + confidence.

    Common tail of every matcher. The matched flag is True iff confidence
    clears MIN_REPORT_THRESHOLD (Step 1 Q4 lock); below that, the matcher
    reports "tried but didn't match" so evaluate() can fall through to
    ghost-match or at_unknown_pudo.

    Item 2 (Phase 2c.2, 2026-05-08): added 4 optional witness kwargs.
    All default None for backward compatibility with callers that don't
    yet pass them (e.g. _match_poi_stub which constructs MatchOutcome
    directly). The 4 real per-class matchers populate these from the
    two witness signal calls (_signal_poi_match + _signal_poi_type_match).
    """
    reason = _render_reason(class_name, signals, confidence)
    return MatchOutcome(
        matched=confidence >= MIN_REPORT_THRESHOLD,
        confidence=confidence,
        corrected_lat=cluster.median_lat,
        corrected_lng=cluster.median_lng,
        reason=reason,
        pudo_type=None,           # Set by _match_current_pudo orchestrator (Step 5.5)
        target_address=getattr(target, "address", None),
        signals=signals,
        poi_match=poi_match,
        poi_witness=poi_witness,
        poi_type_match=poi_type_match,
        poi_type_witness=poi_type_witness,
        semantic_anchor_score=semantic_anchor_score,
        semantic_anchor_witness=semantic_anchor_witness,
        semantic_lookup_source=semantic_lookup_source,
    )


def _match_intersection(
    cluster: Cluster,
    topo: RoadTopology,
    target,
    pois: Optional[list] = None,
    anchors: Optional[list] = None,
    semantic_lookup_source: Optional[str] = None,
) -> MatchOutcome:
    """Match an intersection-class target ("Joan St & Settemont Rd")."""
    if (skip := _validate_target(target, "intersection")) is not None:
        return skip

    signals = _compute_signals(cluster, topo, target, INTERSECTION_RADIUS_M)
    confidence = _weighted_confidence(signals, _CONFIDENCE_WEIGHTS["intersection"])

    # Item 2 witness signals: outside _CONFIDENCE_WEIGHTS per Bible Rule 2.
    poi_match_score, poi_witness_str = _signal_poi_match(
        pois or [], getattr(target, "address", "") or "",
    )
    poi_type_match_bool, poi_type_witness_str = _signal_poi_type_match(
        pois or [], target.address_class,
    )

    if log.isEnabledFor(logging.DEBUG):
        log.debug(
            "[WAI matcher=intersection] %s",
            _render_reason("intersection", signals, confidence),
        )

    # §XVII Patch 3: Head 5 semantic anchor signal.
    sem_score, sem_witness = _signal_semantic_anchor(anchors or [])

    return _build_outcome(
        cluster, target, "intersection", signals, confidence,
        poi_match=poi_match_score,
        poi_witness=poi_witness_str,
        poi_type_match=poi_type_match_bool,
        poi_type_witness=poi_type_witness_str,
        semantic_anchor_score=sem_score,
        semantic_anchor_witness=sem_witness,
        semantic_lookup_source=semantic_lookup_source,
    )


def _match_single_road(
    cluster: Cluster,
    topo: RoadTopology,
    target,
    pois: Optional[list] = None,
    anchors: Optional[list] = None,
    semantic_lookup_source: Optional[str] = None,
) -> MatchOutcome:
    """Match a single_road target ("fondren rd")."""
    if (skip := _validate_target(target, "single_road")) is not None:
        return skip

    signals = _compute_signals(cluster, topo, target, SINGLE_ROAD_RADIUS_M)
    confidence = _weighted_confidence(signals, _CONFIDENCE_WEIGHTS["single_road"])

    # Item 2 witness signals: outside _CONFIDENCE_WEIGHTS per Bible Rule 2.
    poi_match_score, poi_witness_str = _signal_poi_match(
        pois or [], getattr(target, "address", "") or "",
    )
    poi_type_match_bool, poi_type_witness_str = _signal_poi_type_match(
        pois or [], target.address_class,
    )

    if log.isEnabledFor(logging.DEBUG):
        log.debug(
            "[WAI matcher=single_road] %s",
            _render_reason("single_road", signals, confidence),
        )

    # §XVII Patch 3: Head 5 semantic anchor signal.
    sem_score, sem_witness = _signal_semantic_anchor(anchors or [])

    return _build_outcome(
        cluster, target, "single_road", signals, confidence,
        poi_match=poi_match_score,
        poi_witness=poi_witness_str,
        poi_type_match=poi_type_match_bool,
        poi_type_witness=poi_type_witness_str,
        semantic_anchor_score=sem_score,
        semantic_anchor_witness=sem_witness,
        semantic_lookup_source=semantic_lookup_source,
    )


def _match_number_on_street(
    cluster: Cluster,
    topo: RoadTopology,
    target,
    pois: Optional[list] = None,
    anchors: Optional[list] = None,
    semantic_lookup_source: Optional[str] = None,
) -> MatchOutcome:
    """Match a number_on_street target ("1234 Main St").

    Tightest proximity threshold of any class (50m) because Google's
    house-number geocode is precise to within a few meters typically.
    """
    if (skip := _validate_target(target, "number_on_street")) is not None:
        return skip

    signals = _compute_signals(cluster, topo, target, NUMBER_ON_STREET_RADIUS_M)
    confidence = _weighted_confidence(signals, _CONFIDENCE_WEIGHTS["number_on_street"])

    # Item 2 witness signals: outside _CONFIDENCE_WEIGHTS per Bible Rule 2.
    poi_match_score, poi_witness_str = _signal_poi_match(
        pois or [], getattr(target, "address", "") or "",
    )
    poi_type_match_bool, poi_type_witness_str = _signal_poi_type_match(
        pois or [], target.address_class,
    )

    if log.isEnabledFor(logging.DEBUG):
        log.debug(
            "[WAI matcher=number_on_street] %s",
            _render_reason("number_on_street", signals, confidence),
        )

    # §XVII Patch 3: Head 5 semantic anchor signal.
    sem_score, sem_witness = _signal_semantic_anchor(anchors or [])

    return _build_outcome(
        cluster, target, "number_on_street", signals, confidence,
        poi_match=poi_match_score,
        poi_witness=poi_witness_str,
        poi_type_match=poi_type_match_bool,
        poi_type_witness=poi_type_witness_str,
        semantic_anchor_score=sem_score,
        semantic_anchor_witness=sem_witness,
        semantic_lookup_source=semantic_lookup_source,
    )


def _match_apartment_complex(
    cluster: Cluster,
    topo: RoadTopology,
    target,
    pois: Optional[list] = None,
    anchors: Optional[list] = None,
    semantic_lookup_source: Optional[str] = None,
) -> MatchOutcome:
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

    # Item 2 witness signals: outside _CONFIDENCE_WEIGHTS per Bible Rule 2.
    poi_match_score, poi_witness_str = _signal_poi_match(
        pois or [], getattr(target, "address", "") or "",
    )
    poi_type_match_bool, poi_type_witness_str = _signal_poi_type_match(
        pois or [], target.address_class,
    )

    if log.isEnabledFor(logging.DEBUG):
        log.debug(
            "[WAI matcher=apartment_complex] %s",
            _render_reason("apartment_complex", signals, confidence),
        )

    # §XVII Patch 3: Head 5 semantic anchor signal.
    sem_score, sem_witness = _signal_semantic_anchor(anchors or [])

    return _build_outcome(
        cluster, target, "apartment_complex", signals, confidence,
        poi_match=poi_match_score,
        poi_witness=poi_witness_str,
        poi_type_match=poi_type_match_bool,
        poi_type_witness=poi_type_witness_str,
        semantic_anchor_score=sem_score,
        semantic_anchor_witness=sem_witness,
        semantic_lookup_source=semantic_lookup_source,
    )


def _match_poi_class(
    cluster: Cluster,
    topo: RoadTopology,
    target,
    pois: Optional[list] = None,
    anchors: Optional[list] = None,
    semantic_lookup_source: Optional[str] = None,
) -> MatchOutcome:
    """Matcher for address_class='poi' (airports, named venues, named
    businesses where the offer text IS the destination identity).

    §XVII Patch 3 (2026-05-14): replaces _match_poi_stub. Per
    Andrew + Gemini ratification, the poi class is no longer
    deferred to v1.1 — Head 5 (semantic anchor) gives us the
    identity, Heads 1 and 4 provide defense in depth.

    Composition: max(Head 5, Head 4, Head 1) per Gemini ratification.

    Head 5 (_signal_semantic_anchor) is the high-precision primary.
    Queries Google Places searchText with the offer text via
    poi_service.get_anchors_for_text; returns linear-decay score.
    At airports the horizon is 1000m (Gemini ratification 2026-05-14)
    so even the centroid-only fallback (no sub-anchors returned) at
    Terminal-E-equivalent distance ~500m yields confidence 0.50,
    clearing the 0.40 WAI floor.

    Head 4 (_signal_poi_type_match) is the structural fallback.
    Fires when a nearby cluster POI has the right type for the offer
    class (e.g. cluster includes a 'doctor' POI for a dentist offer).
    Useful when Google's text search returns nothing useful (rare
    but happens for generic offer text like "Dentist, Sugar Land").

    Head 1 (_signal_poi_match) is the legacy safety net. Fuzzy /
    branded / airport-type co-reference between offer address and
    cluster POIs. Catches the cases where Heads 4 and 5 miss but
    name correlation is high.

    The winning head's witness propagates to MatchOutcome.
    semantic_anchor_witness / poi_type_witness / poi_witness reflect
    which signal source identified the match, queryable from the
    pudo_decision_context forensic record (Patch 4).
    """
    if (skip := _validate_target(target, "poi")) is not None:
        return skip

    # Head 5: semantic anchor (primary signal for poi class)
    sem_score, sem_witness = _signal_semantic_anchor(anchors or [])

    # Head 4: poi_type_match (structural fallback)
    poi_type_match_bool, poi_type_witness_str = _signal_poi_type_match(
        pois or [], target.address_class,
    )
    # Head 4 returns bool not float — coerce for max() comparison.
    h4_score = 1.0 if poi_type_match_bool else 0.0

    # Head 1: fuzzy/branded/airport-type (legacy safety net)
    poi_match_score, poi_witness_str = _signal_poi_match(
        pois or [], getattr(target, "address", "") or "",
    )

    # Composition: max() per Gemini ratification (defense in depth).
    # Ties broken by source priority: Head 5 > Head 4 > Head 1.
    # Pure max-on-tuple would prefer Head 5 at ties due to argument
    # order in Python's stable max — explicit ordering documented here.
    candidates = [
        (sem_score, sem_witness, "head5_semantic"),
        (h4_score, poi_type_witness_str, "head4_type"),
        (poi_match_score, poi_witness_str, "head1_fuzzy"),
    ]
    confidence, winning_witness, winning_head = max(
        candidates, key=lambda c: c[0]
    )

    # Forensic INFO log per Andrew + Gemini ratification 2026-05-14.
    # Replaces the previous WARN log which implied something broken.
    # A Semantic Anchor match at IAH isn't broken — it's the system
    # working as intended. INFO surfaces the match for shadow-mode
    # aggregates and post-drive audit.
    if confidence >= WAI_CONFIDENCE_THRESHOLD:
        log.info(
            "[WAI matcher=poi_class] target=%r winning_head=%s confidence=%.3f witness=%r",
            getattr(target, "address", None),
            winning_head,
            confidence,
            winning_witness,
        )

    # Signals dict for reason rendering. poi class doesn't compute the
    # standard 6 road-based signals (no road topology relevance for an
    # airport drop); we build a minimal dict.
    signals = {"semantic_anchor": sem_score}

    return _build_outcome(
        cluster, target, "poi", signals, confidence,
        poi_match=poi_match_score,
        poi_witness=poi_witness_str,
        poi_type_match=poi_type_match_bool,
        poi_type_witness=poi_type_witness_str,
        semantic_anchor_score=sem_score,
        semantic_anchor_witness=sem_witness,
        semantic_lookup_source=semantic_lookup_source,
    )


# Dispatch table — Section D's _match_current_pudo uses this to route a
# TargetSpec to its class-appropriate matcher. .get() returns None for
# unknown classes; the orchestrator logs and skips in that case.
_CLASS_DISPATCH = {
    "intersection":      _match_intersection,
    "single_road":       _match_single_road,
    "number_on_street":  _match_number_on_street,
    "apartment_complex": _match_apartment_complex,
    "poi":               _match_poi_class,
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
from adjacency import get_adjacent_roads_for_cluster
from pivot_context import get_pivot_context
from pudo_types import WAIMatch, WAI_CONFIDENCE_THRESHOLD
from tad import evaluate_tad_gate, OfferTadState, TadVerdict


# =============================================================================
# Phase 2c.2 Item 3 — commit rule constants (Bible Rules 3a + 3b)
# =============================================================================
# Three thresholds used by Step 6's dual commit rule. WAI_CONFIDENCE_THRESHOLD
# (0.40 in pudo_types) remains the legacy/bridge-state floor used when no TAD
# context is supplied by the caller (graceful degradation during Item 3b
# rollout). Once Item 3b lands, the dual rule below subsumes the floor.
#
# Tuning: these are Phase 2g concerns. Constants are module-local because the
# commit policy is internal to this module (per Gemini ratification 2026-05-08).
# Phase 2c.2 Item 3c — dual commit rule thresholds (Fix B, 2026-05-11):
# POI reframed as a LIFTER, not a gate. TAD verdict's three-signal corroboration
# (proximity + time + odometer) is sufficient for commit at the floor; POI, when
# available, lifts borderline matches over the floor. Restores dispatch under
# real-world Houston confidence ceiling (~0.41 per §10 A1 empirical) while
# preserving forward compatibility with Operation Strip Mall (poi_type_match
# will start returning True once the matcher ships; lift kicks in automatically).
COMMIT_NORMAL_FLOOR = 0.40      # Normal Mode (verdict.passed=True): TAD is sufficient
COMMIT_LOST_FLOOR = 0.55        # Lost Mode (verdict.passed=None): stricter floor
POI_ELEVATOR_LIFT = 0.10        # POI corroboration lifts effective confidence


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


@dataclass(frozen=True)
class DiagnosticContext:
    """Forensic context returned by evaluate_with_diagnostics().

    Carries everything the wiring layer needs for pudo_decision_context
    inserts and forensic replay tests (e.g. _replay_S31), while keeping
    the naked-list contract pure for business logic per CANONICAL_RULES
    Section XIV.A.

    Fields:
        cluster              The cluster that triggered evaluation, or
                             None if no cluster was detected. Per §3 step 8,
                             populated independently of match outcome so
                             forensic logging never silently drops rows.
        topology             Road topology snapshot (on_wire, current_road,
                             breadcrumb, etc.) or None if no cluster.
        cluster_revisit      True if the cluster centroid is at a location
                             revisited within the queue's accepted_at-
                             anchored lookback window (Memory Eye / Houston
                             Loop signal).
        stop_context         Stop Atlas classification ("unknown_stop" stub
                             until v1.1).
        per_target_outcomes  Every (offer_id, location_type, MatchOutcome)
                             evaluated this heartbeat. The wiring layer
                             projects these into pudo_decision_context's
                             per-target rows.

    No timestamp field: per CANONICAL_RULES Section II (Postgres owns the
    clock), the evaluated_at column is populated at INSERT time via NOW().
    Python clock is never the canonical timestamp.
    """
    cluster: "Optional[Cluster]"
    topology: "Optional[RoadTopology]"
    cluster_revisit: bool
    stop_context: str
    per_target_outcomes: list  # list[tuple[str, str, MatchOutcome]]
    # Phase 2c.2 Item 3e: TAD verdicts per offer for forensic JSONB
    # serialization. Defaulted to empty dict so legacy constructors
    # (4 test fixtures + bridge-state production calls) keep working
    # without modification. Caller layer (driver_heartbeat.py, Item 3b)
    # serializes this into pudo_decision_context.tad_decision_context.
    tad_verdicts: dict = field(default_factory=dict)

    # Phase 2c.2 forensic wiring (2026-05-09): cluster-scoped POI
    # names for pudo_decision_context.poi_top_names. Top 3 by
    # distance, formatted as "Name (Xm)". Defaulted to empty list
    # so legacy constructors keep working without modification
    # (same discipline as tad_verdicts above).
    cluster_poi_names: list = field(default_factory=list)

    def outcome_for(self, match) -> "Optional[MatchOutcome]":
        """Return the MatchOutcome for a WAIMatch, by (offer_id, location_type).

        Bridges the WAIMatch <-> MatchOutcome encapsulation gap (per
        SIMPLIFIED_ARCHITECTURE.md s10 A8). WAIMatch is intentionally
        minimal (offer_id, location_type, confidence) per its frozen
        public contract; the forensic fields (reason, target_address,
        signals) live on the internal MatchOutcome carrier.

        per_target_outcomes is populated by _evaluate for every candidate
        considered, regardless of whether it cleared WAI_CONFIDENCE_THRESHOLD.
        Lookup is by (offer_id, location_type) key.

        Returns None if no outcome matches the (offer_id, location_type)
        of the given match. Defensive: every WAIMatch returned by
        evaluate() is projected from a MatchOutcome at the bottom of
        _evaluate, so the lookup should always succeed in production
        paths. Test code that constructs WAIMatch manually may
        legitimately hit None.

        match is intentionally untyped to avoid a circular import with
        pudo_types. Implementation is duck-typed on .offer_id and
        .location_type.

        Phase 1B (2026-05-05): added for forensic restoration.
        Gemini-ratified per PHASE_1B_PROPOSAL_v2.md Section 1, Decision A1.
        """
        for offer_id, location_type, outcome in self.per_target_outcomes:
            if offer_id == match.offer_id and location_type == match.location_type:
                return outcome
        return None


def _commits(outcome, verdict) -> bool:
    """Phase 2c.2 Item 3c — dual commit rule (Fix B, 2026-05-11).

    Pure policy function. Returns True if `outcome` should commit given the
    TAD `verdict` for the same offer. No side effects.

    Fix B reframing: POI is a LIFTER (additive bonus), not a GATE (precondition).
    TAD verdict's three-signal corroboration (proximity + time + odometer) is
    sufficient for commit at the floor. POI, when available, adds POI_ELEVATOR_LIFT
    to the effective confidence, helping borderline matches clear the floor.

    Three branches (in priority order):
      1. verdict is None (bridge state, caller hasn't wired Item 3b):
            legacy WAI_CONFIDENCE_THRESHOLD floor. Preserved for graceful
            degradation and test fixtures.
      2. verdict.passed is True (Normal Mode):
            Commit if (confidence + lift) >= COMMIT_NORMAL_FLOOR (0.40).
            POI_ELEVATOR_LIFT (0.10) is added to confidence when
            outcome.poi_type_match is True; otherwise no lift.
      3. verdict.passed is None (Lost Mode):
            Commit if (confidence + lift) >= COMMIT_LOST_FLOOR (0.55).
            POI lift applies in Lost Mode as well — Lost Mode's higher floor
            already compensates for broken narrative; POI further corroborates
            when available.

    verdict.passed is False is unreachable here — Step 5 skips dispatch for
    those offers, so per_target_outcomes never contains them. Defensive
    fall-through returns False.
    """
    if not outcome.matched:
        return False
    conf = outcome.confidence
    # POI lift: when POI type matches the address class, add the elevator bonus.
    # Operation Strip Mall ships -> poi_type_match starts returning True ->
    # borderline matches automatically clear the floor. No code change needed.
    if outcome.poi_type_match is True:
        conf += POI_ELEVATOR_LIFT
    if verdict is None:
        # Bridge state: legacy floor.
        return conf >= WAI_CONFIDENCE_THRESHOLD
    if verdict.passed is True:
        # Normal Mode: TAD's three-signal corroboration is sufficient.
        return conf >= COMMIT_NORMAL_FLOOR
    if verdict.passed is None:
        # Lost Mode: stricter floor compensates for broken narrative.
        return conf >= COMMIT_LOST_FLOOR
    # passed=False: defensive (Step 5 already skipped this offer)
    return False


def classify_commit_rule(outcome, verdict) -> str:
    """Phase 2c.2 Item 3e — forensic classifier for the JSONB blob.

    Pure reporter. Names which clause of the dual commit rule fired for an
    outcome that _commits() returned True for. Caller (heartbeat handler's
    pudo_decision_context serializer) invokes this only on committed outcomes.

    Returns one of:
      "legacy_floor"              — verdict is None (bridge state), conf >= 0.40
      "normal_floor"              — verdict.passed=True, conf >= 0.40, no POI lift
      "normal_floor_with_poi_lift"— verdict.passed=True, POI lifted match over floor
      "lost_floor"                — verdict.passed=None, conf >= 0.55, no POI lift
      "lost_floor_with_poi_lift"  — verdict.passed=None, POI lifted match over floor
      "unknown"                   — defensive (verdict.passed=False; unreachable)

    NOT a decision function — _commits() owns the decision. This labels the
    decision after the fact for forensic queries like "how often does POI lift
    save the day?" (Phase 2g tuning input).
    """
    conf = outcome.confidence
    poi_lifted = outcome.poi_type_match is True
    if verdict is None:
        return "legacy_floor"
    if verdict.passed is True:
        return "normal_floor_with_poi_lift" if poi_lifted else "normal_floor"
    if verdict.passed is None:
        return "lost_floor_with_poi_lift" if poi_lifted else "lost_floor"
    return "unknown"


class WhereAmI:
    """Continuous location awareness primitive.

    Pure DIAGNOSE per the 4-Box Controller. Reads only. evaluate() is
    safe to call on every heartbeat without side effects.

    Construction:
      WhereAmI(cur)                              # production
      WhereAmI(cur, _cluster_fn=fake, ...)       # tests with injected deps

    Public surface:
      evaluate(driver_id, queue) -> list[WAIMatch]
      evaluate_with_diagnostics(driver_id, queue) -> (list[WAIMatch], DiagnosticContext)
    """

    def __init__(
        self,
        cur,
        *,
        _cluster_fn=detect_cluster,
        _pivot_fn=get_pivot_context,
        _recent_clusters_fn=get_recent_clusters,
        _adjacency_fn=None,
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
        self._adjacency_fn = _adjacency_fn or get_adjacent_roads_for_cluster

    # =========================================================================
    # Public entry point
    # =========================================================================

    def evaluate(
        self,
        driver_id: str,
        queue: list,
        per_offer_state: "Optional[dict[str, OfferTadState]]" = None,
        lost_mode: bool = False,
        last_known_anchor_id: "Optional[str]" = None,
        current_odometer: "Optional[float]" = None,
    ) -> list[WAIMatch]:
        """Pure Sensor: returns naked match list per the §3 Map-Reduce contract.

        Thin wrapper around _evaluate() that discards the DiagnosticContext.
        Use evaluate_with_diagnostics() for forensic logging and replay tests.

        Per CANONICAL_RULES Section XIV.A and SIMPLIFIED_ARCHITECTURE.md §10
        A8: this is the only legitimate path from cluster to PUDO match.

        Phase 2c.2 Item 3 TAD parameters (all optional; bridge state preserved
        when caller does not supply them):
          per_offer_state: dict[offer_id, OfferTadState]. Caller (driver_
              heartbeat.py) assembles from offer_history rows alongside queue.
          lost_mode: True signals narrative_blindness (queue empty, prev GC'd).
          last_known_anchor_id: most recent confirmed-anchor offer_id (forensic).
          current_odometer: cumulative miles at heartbeat time.
        """
        matches, _ = self._evaluate(
            driver_id, queue,
            per_offer_state=per_offer_state,
            lost_mode=lost_mode,
            last_known_anchor_id=last_known_anchor_id,
            current_odometer=current_odometer,
        )
        return matches

    def evaluate_with_diagnostics(
        self,
        driver_id: str,
        queue: list,
        per_offer_state: "Optional[dict[str, OfferTadState]]" = None,
        lost_mode: bool = False,
        last_known_anchor_id: "Optional[str]" = None,
        current_odometer: "Optional[float]" = None,
    ) -> tuple[list[WAIMatch], "DiagnosticContext"]:
        """Forensic counterpart to evaluate().

        Returns (matches, diagnostics) for the heartbeat handler's
        pudo_decision_context insert and for forensic replay tests
        (e.g. _replay_S31). The DiagnosticContext carries the cluster,
        topology, cluster_revisit flag, stop context, per-target
        outcomes, AND tad_verdicts (Phase 2c.2 Item 3e) — everything
        pudo_decision_context's logging surface needs to record what
        WAI saw on this heartbeat.

        Per SIMPLIFIED_ARCHITECTURE.md §3 step 8: cluster data MUST be
        logged independently of WAI outcome. DiagnosticContext.cluster is
        populated even when no targets match, so forensic logging never
        silently drops cluster rows.

        Phase 2c.2 Item 3 TAD parameters: see evaluate() docstring.
        """
        return self._evaluate(
            driver_id, queue,
            per_offer_state=per_offer_state,
            lost_mode=lost_mode,
            last_known_anchor_id=last_known_anchor_id,
            current_odometer=current_odometer,
        )

    def _evaluate(
        self,
        driver_id: str,
        queue: list,
        per_offer_state: "Optional[dict[str, OfferTadState]]" = None,
        lost_mode: bool = False,
        last_known_anchor_id: "Optional[str]" = None,
        current_odometer: "Optional[float]" = None,
    ) -> tuple[list[WAIMatch], "DiagnosticContext"]:
        """Internal: §3 Map-Reduce algorithm. Single source of truth.

        Map     for each offer in queue, generate (pickup, "pickup") and
                (dropoff, "dropoff") target candidates. Two TargetSpecs
                per offer. Empty queue -> empty Map.

        Filter  per-class matchers apply their own proximity radii
                (INTERSECTION_RADIUS_M etc.) per §3 step 3b.

        Evaluate run Signal Economics on each surviving candidate via
                _CLASS_DISPATCH. Per §3 step 3c.

        Reduce  return all candidates that clear the dual commit rule
                (Bible Rules 3a/3b, Phase 2c.2 Item 3c) and tie at the
                maximum confidence. Ties at exact float equality are
                returned together for §5 disambiguation downstream.

        Memory Eye: cluster-history lookback anchors on the oldest
        accepted_at across the queue (Cut B2 extension of Step 6
        Amendment 1's Offer-Anchor Lookback). Empty queue -> no lookback
        fetch, cluster_revisit=False.

        Phase 2c.2 Item 3 — Step 4.5 TAD bouncer:
            Between Map (Step 4) and Dispatch (Step 5), a parallel pass
            evaluates each offer through tad.evaluate_tad_gate(). Three
            verdicts:
              passed=True  -> Normal Mode, apply Rule 3a elevator at Step 6
              passed=False -> skip dispatch entirely (no Google API spend)
              passed=None  -> Lost Mode, apply Rule 3b strict floor at Step 6

        Bridge state: when per_offer_state or current_odometer is None
        (caller hasn't yet wired Item 3b), TAD evaluation is bypassed and
        Step 6 falls back to legacy WAI_CONFIDENCE_THRESHOLD floor. This
        preserves backward compatibility during Item 3b rollout.
        """
        # Step 1: Cluster check
        cluster = self._cluster_fn(driver_id, self.cur)
        if cluster is None:
            return [], DiagnosticContext(
                cluster=None,
                topology=None,
                cluster_revisit=False,
                stop_context="unknown_stop",
                per_target_outcomes=[],
                tad_verdicts={},
                cluster_poi_names=[],
            )

        # Step 2: Topology (single pivot_context call, reused in Evaluate)
        topo = self._compute_road_topology(driver_id, cluster)

        # Step 3: Stop context (Stop Atlas v1.1 stub)
        stop_context = "unknown_stop"

        # Step 3.5: Memory Eye — cluster history lookback. Anchor on
        # min(accepted_at) across the queue. Empty queue -> skip.
        if queue:
            accepted_at_anchor = min(offer.accepted_at for offer in queue)
            recent_clusters = self._recent_clusters_fn(
                driver_id, self.cur,
                accepted_at_anchor=accepted_at_anchor,
            )
            cluster_revisit = _compute_cluster_revisit(cluster, recent_clusters)
        else:
            cluster_revisit = False

        # Step 3.6: POI lookup -- cluster-scoped, fed to per-class matchers
        # for witness-signal corroboration (Patch 2c name-match + Head 4
        # type-match). Mantra-aligned error handling: empty list on any
        # failure so witnesses just don't testify. Per Bible Rule 2,
        # both witness signals stay outside _CONFIDENCE_WEIGHTS.
        try:
            from poi_service import get_pois_near_cluster
            pois_result = get_pois_near_cluster(cluster, self.cur)
            cluster_pois = list(pois_result.pois) if pois_result else []
            # Phase 2c.2 forensic wiring: project top-3 POI names by
            # distance for cluster-scoped logging. Format "Name (Xm)"
            # matches Gemini's psql-scannability ratification — at-a-
            # glance disambiguation between nearby establishments in
            # dense commercial clusters.
            cluster_poi_names = [
                f"{p.name} ({p.dist_m:.0f}m)"
                for p in sorted(cluster_pois, key=lambda x: x.dist_m)[:3]
            ]
            # Resurrection alert: log the first non-empty POI return per
            # process. High-signal forensic marker that the call-contract
            # bug fix has taken effect on this Cloud Run container.
            global _first_poi_success_logged
            if cluster_pois and not _first_poi_success_logged:
                log.info(
                    "[RESURRECTION] First successful POI lookup at call site: "
                    "found %d witnesses at cluster=(%s, %s)",
                    len(cluster_pois), cluster.median_lat, cluster.median_lng,
                )
                _first_poi_success_logged = True
        except Exception:
            log.warning(
                "[WAI] POI fetch failed for cluster=(%s, %s) -- proceeding without witnesses",
                cluster.median_lat, cluster.median_lng,
                exc_info=True,
            )
            cluster_pois = []
            cluster_poi_names = []

        # §XVII Patch 3: cluster-anchors fetch helper. Used per-candidate
        # inside the dispatch loop below. Returns list of POI with dist_m
        # recomputed relative to cluster centroid via canonical
        # app_private.distance_miles (Path 2: per-anchor SQL, mirrors
        # _read_cache pattern). Returns empty list on no-text, no-anchors,
        # or any error — Head 5 falls through; other heads still fire.
        def _fetch_cluster_anchors(target_addr):
            """Returns (cluster_anchors, semantic_lookup_source).

            §XVII Patch 4 (2026-05-14): surfaces POILookupResult.source so the
            matcher can record which endpoint sourced the anchor data
            ('semantic_cache_hit', 'semantic_api_call', 'semantic_api_error').
            Both elements of the tuple are None/empty on no-target or
            exception paths.
            """
            if not target_addr:
                return [], None
            try:
                from poi_service import get_anchors_for_text
                anchor_result = get_anchors_for_text(
                    target_addr, self.cur,
                    bias_lat=_HOUSTON_BIAS_LAT,
                    bias_lng=_HOUSTON_BIAS_LNG,
                )
                raw_source = anchor_result.source if anchor_result else None
                raw_anchors = list(anchor_result.pois) if anchor_result else []
                # Recompute dist_m for each anchor relative to cluster centroid
                # (Patch 1 returns anchors with dist_m=0.0 by Gemini directive 1).
                cluster_anchors = []
                from poi_service import POI as _POI
                for a in raw_anchors:
                    self.cur.execute(
                        "SELECT app_private.distance_miles(%s, %s, %s, %s) * 1609.344 AS dist_m",
                        (cluster.median_lat, cluster.median_lng, a.lat, a.lng),
                    )
                    row = self.cur.fetchone()
                    dist_m = float(row["dist_m"]) if row and row["dist_m"] is not None else float("inf")
                    cluster_anchors.append(_POI(
                        place_id=a.place_id, name=a.name, types=a.types,
                        lat=a.lat, lng=a.lng, dist_m=dist_m,
                    ))
                return cluster_anchors, raw_source
            except Exception:
                log.warning(
                    "[WAI] semantic anchor fetch failed for target_addr=%r — proceeding without Head 5",
                    target_addr, exc_info=True,
                )
                return [], None

        # Step 4 (Map): generate (target, location_type, offer_id) candidates.
        candidates = []
        for offer in queue:
            candidates.append((offer.pickup, "pickup", offer.offer_id))
            candidates.append((offer.dropoff, "dropoff", offer.offer_id))

        # Step 4.5 (TAD bouncer, Phase 2c.2 Item 3a): parallel pass over the
        # queue producing per-offer verdicts. Bypassed in bridge state when
        # caller hasn't supplied TAD context — empty dict means "no filtering"
        # at Step 5 and "legacy floor" at Step 6.
        tad_verdicts: dict = {}
        if queue and per_offer_state is not None and current_odometer is not None:
            tad_verdicts = evaluate_tad_gate(
                cluster=cluster,
                queue_offers=tuple(queue),
                current_odometer=current_odometer,
                per_offer_state=per_offer_state,
                lost_mode=lost_mode,
                last_known_anchor_id=last_known_anchor_id,
            )

        # Step 5 (Evaluate): run per-class matcher against each candidate.
        # TAD-failed offers (verdict.passed is False) are skipped here — no
        # spatial scoring, no Google API spend (Bible Rule 1, the wallet gate).
        # Defensive logging on unknown address_class — alerts to upstream
        # geocoding drift without polluting the match logic.
        per_target_outcomes = []
        for target, location_type, offer_id in candidates:
            # TAD bouncer skip: when verdicts dict is populated and verdict
            # for this offer says passed=False, skip entirely. passed=True
            # and passed=None both proceed to spatial scoring (Lost Mode
            # still scores; Step 6 applies stricter rule).
            if tad_verdicts:
                verdict = tad_verdicts.get(offer_id)
                if verdict is not None and verdict.passed is False:
                    continue
            matcher = _CLASS_DISPATCH.get(target.address_class)
            if matcher is None:
                log.warning(
                    "[WAI] unknown address_class=%r for offer_id=%r — skipping",
                    target.address_class, offer_id,
                )
                continue
            cluster_anchors, semantic_source = _fetch_cluster_anchors(
                getattr(target, "address", None)
            )
            outcome = matcher(
                cluster, topo, target,
                pois=cluster_pois, anchors=cluster_anchors,
                semantic_lookup_source=semantic_source,
            )
            per_target_outcomes.append((offer_id, location_type, outcome))

        # Step 6 (Reduce, Phase 2c.2 Item 3c): apply dual commit rule per
        # Bible Rules 3a + 3b, then tie at max across surviving candidates.
        above_threshold = [
            (offer_id, location_type, outcome)
            for offer_id, location_type, outcome in per_target_outcomes
            if _commits(outcome, tad_verdicts.get(offer_id))
        ]

        matches: list[WAIMatch] = []
        if above_threshold:
            max_confidence = max(o.confidence for _, _, o in above_threshold)
            for offer_id, location_type, outcome in above_threshold:
                if outcome.confidence == max_confidence:
                    matches.append(WAIMatch(
                        offer_id=offer_id,
                        location_type=location_type,
                        confidence=outcome.confidence,
                    ))

        diagnostics = DiagnosticContext(
            cluster=cluster,
            topology=topo,
            cluster_revisit=cluster_revisit,
            stop_context=stop_context,
            per_target_outcomes=per_target_outcomes,
            tad_verdicts=tad_verdicts,
            cluster_poi_names=cluster_poi_names,
        )
        return matches, diagnostics

    # =========================================================================
    # Topology adapter
    # =========================================================================

    def _compute_road_topology(self, driver_id: str, cluster) -> RoadTopology:
        """Map pivot_context.get_pivot_context() output onto RoadTopology.

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
        # pivot_context returns a list of segment dicts ({road_name, entered_at,
        # exited_at}); RoadTopology.breadcrumb is typed tuple[str, ...] and all
        # downstream signal consumers assume road-name strings. Project the
        # road_name field here, filter out any segments missing it.
        breadcrumb = tuple(
            seg["road_name"] for seg in breadcrumb_raw if seg.get("road_name")
        )

        # Adjacency lookup (Sprint 2 Step D): named roads within 150m of
        # cluster centroid, for off-wire matching of parking-lot / strip-mall
        # / apartment-complex cases. Empty tuple when cluster is None per
        # adjacency.get_adjacent_roads_for_cluster contract.
        adjacent_roads = self._adjacency_fn(self.cur, cluster)

        return RoadTopology(
            on_wire=on_wire,
            current_road=ctx.get("current_road"),
            last_named_road=ctx.get("last_named_road"),
            off_wire_duration_s=off_wire_duration_s,
            breadcrumb=breadcrumb,
            adjacent_roads=adjacent_roads,
            current_road_class=ctx.get("current_road_class"),
        )

    # =========================================================================
    # Current-ride PUDO matching (state-aware)
    # =========================================================================
