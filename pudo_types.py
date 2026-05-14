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


