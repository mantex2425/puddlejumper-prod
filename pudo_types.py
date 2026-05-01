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


