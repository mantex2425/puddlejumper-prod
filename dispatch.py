"""Pure-function dispatch from WAI matches to side-effecting Actions.

Implements SIMPLIFIED_ARCHITECTURE.md §4 Cases A-G and §5 disambiguation
rules. This module is a Pure Pipe: input -> 7-case map -> list[Action].
No DB reads, no DB writes, no GPS access, no file I/O, no logging side
effects. The heartbeat handler (post Cut B3) executes the action list.

Per SIMPLIFIED_ARCHITECTURE.md §10 A8 and CANONICAL_RULES Section XIV.B,
this is the only legitimate path from WAI matches to PUDO actions. No
fast-path heuristics outside this dispatcher.

The doc and this code stay synchronized: every Case in §4 maps to a
branch below; every disambiguation rule in §5 maps to a sub-branch in
the multi-match handler. Doc/code amendments land in lockstep.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional, Union

from pudo_types import WAIMatch


# ============================================================================
# Action types -- the output vocabulary of dispatch()
# ============================================================================
#
# Each Action carries side-effect intent for the wiring layer (Cut B3).
# State updates are implicit by Action type -- no separate SetCurrentOfferId
# action is emitted. The wiring layer maps:
#
#   FirePickup(offer_id)         -> fire_pickup; current_offer_id = offer_id
#   FireDropoff(offer_id, ...)   -> fire_dropoff; current_offer_id = None
#                                   outcome=None            -> INFO    (Case C)
#                                   outcome="canceled"      -> INFO    (Case D)
#                                   outcome="pickup_missed" -> WARNING (Case F)
#   LogNoMatch                   -> INFO log; no state change          (Case A)
#   LogPickupRematch             -> DEBUG log; no state change         (Case G)
#   LogAmbiguousMatch            -> WARNING log; no state change       (§5.3 + default)


@dataclass(frozen=True)
class FirePickup:
    """Pickup observation. Wiring: log INFO; current_offer_id = offer_id."""
    offer_id: str


@dataclass(frozen=True)
class FireDropoff:
    """Dropoff observation. Wiring: log + clear current_offer_id.

    outcome=None              -> normal completion (Case C). Severity INFO.
    outcome="canceled"        -> implicit cancel half (Case D). Severity INFO.
    outcome="pickup_missed"   -> S33 analog (Case F).         Severity WARNING.
    """
    offer_id: str
    outcome: Optional[Literal["canceled", "pickup_missed"]] = None


@dataclass(frozen=True)
class LogNoMatch:
    """No match this heartbeat. §4 Case A. Severity INFO. No state change."""


@dataclass(frozen=True)
class LogPickupRematch:
    """Pickup re-matched while ride active. §4 Case G. Severity DEBUG.

    Idempotency safety net: fire_pickup must not fire twice for the same
    offer. Driver returned to pickup geocode mid-trip (circled the block,
    geocode overlaps dropoff path, etc.). No state change; log payload
    minimal per §4 Case G (offer_id + cluster forensics at the wiring layer).
    """
    offer_id: str


@dataclass(frozen=True)
class LogAmbiguousMatch:
    """Multi-match WAI couldn't disambiguate. §5.3 + fail-closed default.

    Severity WARNING. No state change. Surface for manual review (production)
    or manual nail (dev builds only). The architecture fails safe by not
    firing when WAI's intelligence (Memory + Signal + Latch + Topology)
    cannot distinguish candidates.
    """
    candidates: tuple[WAIMatch, ...]
    reason: str


Action = Union[
    FirePickup,
    FireDropoff,
    LogNoMatch,
    LogPickupRematch,
    LogAmbiguousMatch,
]


# ============================================================================
# dispatch() -- the §4 / §5 case-resolution pure function
# ============================================================================


def dispatch(
    matches: list[WAIMatch],
    current_offer_id: Optional[str],
    queue_offer_ids: set[str],
) -> list[Action]:
    """Map WAI matches against current_offer_id memory to side-effect Actions.

    Pure function. No I/O. Implements SIMPLIFIED_ARCHITECTURE.md §4 Cases
    A-G and §5 disambiguation rules.

    Args:
        matches: list[WAIMatch] from WAI.evaluate(). May be empty.
        current_offer_id: 1-bit memory of active ride (None or offer_id).
        queue_offer_ids: offer_ids currently in the driver's queue. Used as
            a defensive border filter -- matches for offer_ids not in the
            queue are silently dropped before case resolution. Per §10 A8
            WAI evaluates against the queue and should not return stale
            matches; this filter is belt-and-suspenders against a WAI bug.
            Silent drop (no Action emitted) preserves downstream case logic.

    Returns:
        list[Action] for the wiring layer to execute. Ordering matters for
        Case D and §5.2 (dropoff before pickup).
    """
    # Border filter: silently drop any match whose offer_id is not in the
    # active queue. Per §10 A8, WAI shouldn't produce these; this guard
    # keeps a WAI bug from cascading into a stale-offer fire.
    matches = [m for m in matches if m.offer_id in queue_offer_ids]

    # Case A: empty match list ------------------------------------------
    if not matches:
        return [LogNoMatch()]

    # Single match: Cases B, C, D, F, G ---------------------------------
    if len(matches) == 1:
        return _dispatch_single(matches[0], current_offer_id)

    # Two matches: §5.1 errands, §5.2 hot-swap, §5.3 ambiguous ----------
    if len(matches) == 2:
        return _dispatch_pair(matches, current_offer_id)

    # Three+ matches: unenumerated, fail closed -------------------------
    return [LogAmbiguousMatch(
        candidates=tuple(matches),
        reason="unenumerated_multi_match",
    )]


def _dispatch_single(
    m: WAIMatch,
    current_offer_id: Optional[str],
) -> list[Action]:
    """Single-match resolution. §4 Cases B, C, D, F, G."""
    if m.location_type == "pickup":
        if current_offer_id is None:
            # Case B: pickup, no active ride
            return [FirePickup(m.offer_id)]
        if current_offer_id == m.offer_id:
            # Case G: pickup re-match while active (idempotency safety net)
            return [LogPickupRematch(m.offer_id)]
        # Case D: pickup of different offer while active -> implicit cancel.
        # Order matters: fire_dropoff(canceled) MUST precede fire_pickup so
        # the wiring layer clears current_offer_id before setting it anew.
        return [
            FireDropoff(current_offer_id, outcome="canceled"),
            FirePickup(m.offer_id),
        ]

    # m.location_type == "dropoff"
    if current_offer_id == m.offer_id:
        # Case C: dropoff of active ride -> normal completion
        return [FireDropoff(m.offer_id)]
    # Case F: dropoff of an offer that is not current_offer_id
    # (current_offer_id may be None or a different offer; both route here
    # per the §4 amendment ratified 2026-04-30 -- the S33 missed-pickup
    # analog surfaced synchronously at the dropoff heartbeat).
    return [FireDropoff(m.offer_id, outcome="pickup_missed")]


def _dispatch_pair(
    matches: list[WAIMatch],
    current_offer_id: Optional[str],
) -> list[Action]:
    """Two-match resolution. §5.1 errands, §5.2 hot-swap, §5.3 ambiguous."""
    m1, m2 = matches[0], matches[1]
    types = {m1.location_type, m2.location_type}

    # §5.1 errands: same offer, pickup AND dropoff ----------------------
    if m1.offer_id == m2.offer_id and types == {"pickup", "dropoff"}:
        offer_id = m1.offer_id
        if current_offer_id is None:
            # Errands shape, ride not started -> fire pickup
            return [FirePickup(offer_id)]
        if current_offer_id == offer_id:
            # Errands shape, ride active -> fire dropoff
            return [FireDropoff(offer_id)]
        # Errands shape but a DIFFERENT ride is active. Unenumerated by §5.1
        # (which assumes current_offer_id in {None, offer_id}). Fail closed.
        return [LogAmbiguousMatch(
            candidates=tuple(matches),
            reason="errands_with_unrelated_active",
        )]

    # §5.2 hot-swap: dropoff of current + pickup of different -----------
    if types == {"pickup", "dropoff"} and m1.offer_id != m2.offer_id:
        dropoff = m1 if m1.location_type == "dropoff" else m2
        pickup = m1 if m1.location_type == "pickup" else m2
        if current_offer_id == dropoff.offer_id:
            # Clean hot-swap: complete current ride, start the next.
            # Order matters per §5.2: dropoff first clears current_offer_id,
            # pickup second sets it to the new offer.
            return [
                FireDropoff(dropoff.offer_id),
                FirePickup(pickup.offer_id),
            ]
        # Different-offer pickup+dropoff but current_offer_id doesn't match
        # the dropoff side. §5.2 assumes current_offer_id == dropoff.offer_id;
        # this shape is unenumerated. Fail closed.
        return [LogAmbiguousMatch(
            candidates=tuple(matches),
            reason="hot_swap_without_matching_active",
        )]

    # §5.3 + fail closed: two pickups, two dropoffs, or other ----------
    if types == {"pickup"}:
        # §5.3 explicitly: two pickups -> ambiguous
        return [LogAmbiguousMatch(
            candidates=tuple(matches),
            reason="two_pickups",
        )]
    if types == {"dropoff"}:
        # Mirror of §5.3 for dropoffs. Unenumerated by §5 but symmetric.
        return [LogAmbiguousMatch(
            candidates=tuple(matches),
            reason="two_dropoffs",
        )]

    # Defensive: should not reach here given the type-set possibilities
    # ({"pickup"}, {"dropoff"}, {"pickup", "dropoff"}). Keeps the function
    # total under any future Literal expansion.
    return [LogAmbiguousMatch(
        candidates=tuple(matches),
        reason="unenumerated_pair",
    )]
