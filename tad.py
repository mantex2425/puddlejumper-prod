"""
tad.py — Time And Distance primitive for PUDO candidate filtering.

Pure DIAGNOSE module. Reads only. No DB calls, no state mutation. Consumed by
where_am_i.evaluate() as the candidate-filter "bouncer" between the Map step
(candidate generation from queue) and the Dispatch step (per-class matchers).

Architectural role (Phase 2c.2):
  TAD answers two questions for each (cluster, offer-leg) pair:
    1. Has the driver traveled a plausible distance toward this PUDO? (HARD)
    2. Has the driver arrived at a plausible time? (SOFT, asymmetric)

  Distance gate is the bouncer — fail it and the candidate never reaches
  spatial scoring. Time signal is a confidence boost — never a penalty.

  See docs/PHASE_2C_2_SPRINT_BIBLE.md for the architectural reconciliation
  (TAD as candidate filter, NOT as a sibling spatial signal).

Canonical compliance (PuddleJumper Standards v2.1):
  - Section III (Temporal): all datetimes are tz-aware UTC. naive datetimes
    cause ValueError at compute_offer_expectations() entry. No silent
    coercion.
  - Section IV (Logic): TAD is the mandatory hard-gate filter. Distance is
    the primary signal; time is asymmetric (boost only, never penalty).
  - Section V (Forensic): per-evaluation telemetry persists to
    pudo_decision_context.tad_decision_context jsonb (Part 2 module).

Module structure:
  Part 1 (this commit):
    - Constants
    - OfferExpectations dataclass
    - compute_offer_expectations() — runs at offer receipt in router.py

  Part 2 (next commit):
    - TadVerdict dataclass
    - evaluate_tad_gate() — runs in where_am_i.evaluate() per cluster

Public surface:
    tad.compute_offer_expectations(new_offer, prev_offer, current_odometer, now)
        -> OfferExpectations
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from pudo_types import Offer

log = logging.getLogger(__name__)


# =============================================================================
# Constants — empirically grounded in 2026-05-07 shift data
# =============================================================================
#
# These thresholds are CATEGORY 1 per L-10 (production-data-grounded). They
# may be tuned in Phase 2g after shadow-mode data accumulates; tuning
# requires explicit re-ratification, not an inline edit.

# Distance gate (hard). For trips >= SHORT_TRIP_THRESHOLD_MILES, the cluster's
# odometer delta must equal at least DISTANCE_GATE_COMPLETION_THRESHOLD of the
# expected delta. Empirical: 7/8 of 2026-05-07 shift rides reached >= 85%
# completion at actual pickup; the 8th was a train-delay outlier (Ride #4,
# +483% time error) that the time signal correctly down-weighted to zero.
DISTANCE_GATE_COMPLETION_THRESHOLD = 0.85

# Below this trip length, percentage math is too noisy. Switch to absolute
# tolerance instead. 2.0mi is the empirical noise floor — at 1.5mi a 0.2mi
# navigation deviation is +13% / -13% but matters operationally.
SHORT_TRIP_THRESHOLD_MILES = 2.0

# Absolute tolerance for short trips. Catches Ride #2 case (0.8mi pickup,
# +0.2mi navigation deviation puts cluster at 1.0mi cumulative delta).
SHORT_TRIP_ABSOLUTE_TOLERANCE_MILES = 0.5

# Time signal (soft, asymmetric — never a penalty for being late).
# Boost values per the elevator architecture: time signal flows into
# combined confidence as a SEPARATE concern from distance, never as a gate.
TIME_SIGNAL_TIGHT_PCT = 0.15      # |error| <= 15% of expected duration
TIME_SIGNAL_TIGHT_BOOST = 0.15
TIME_SIGNAL_LOOSE_PCT = 0.50      # |error| <= 50% of expected duration
TIME_SIGNAL_LOOSE_BOOST = 0.05
# Outside +-50% the boost is 0 (no confidence loss for being late).


# =============================================================================
# OfferExpectations — the snapshot computed at offer receipt
# =============================================================================

@dataclass(frozen=True)
class OfferExpectations:
    """The four expected-* anchors persisted to offer_history at receipt.

    Frozen because the values are a snapshot of "what we predicted at the
    moment the offer arrived." Mutating them after the fact would corrupt
    the forensic trail (offer_history rows are append-only).

    Field semantics:
      expected_pickup_arrival_time:
          Wall-clock UTC datetime when driver is expected to reach pickup.
          Used as the time anchor for pickup-side TAD evaluation.

      expected_pickup_distance:
          Cumulative odometer reading (miles) at which driver is expected
          to reach pickup. NOT a delta — a target absolute reading. Used
          as the distance anchor for pickup-side TAD evaluation.

      expected_dropoff_arrival_time:
          UTC datetime, same shape as pickup but for dropoff.

      expected_dropoff_distance:
          Cumulative odometer (miles), same shape as pickup but for dropoff.

    Idle case (prev_offer is None or orphaned):
        expected_pickup_arrival_time = now + pickup_minutes * 60
        expected_pickup_distance     = current_odometer + pickup_miles

    Stacked case (prev_offer's expected dropoff is still in the future):
        expected_pickup_arrival_time = prev.expected_dropoff_arrival_time
                                       + pickup_minutes * 60
        expected_pickup_distance     = prev.expected_dropoff_distance
                                       + pickup_miles

    Dropoff anchors (always derived from pickup anchors + trip):
        expected_dropoff_arrival_time = expected_pickup_arrival_time
                                        + trip_minutes * 60
        expected_dropoff_distance     = expected_pickup_distance + trip_miles
    """
    expected_pickup_arrival_time: datetime
    expected_pickup_distance: float
    expected_dropoff_arrival_time: datetime
    expected_dropoff_distance: float


# =============================================================================
# compute_offer_expectations — runs at offer receipt in router.py
# =============================================================================

def compute_offer_expectations(
    new_offer: Offer,
    prev_offer: Optional[Offer],
    current_odometer: float,
    now: datetime,
    prev_expected_dropoff_arrival_time: Optional[datetime] = None,
    prev_expected_dropoff_distance: Optional[float] = None,
) -> Optional[OfferExpectations]:
    """Compute the four expected_* anchors for new_offer at receipt time.

    Pure arithmetic. No DB access. Idempotent — same inputs always produce
    same outputs.

    Args:
        new_offer:
            The offer just received. Must have pickup_minutes, pickup_miles,
            trip_minutes, trip_miles populated. Returns None if any are
            missing (defensive — TAD evaluation will skip this offer).

        prev_offer:
            The most recent offer in the driver's queue (chronologically
            previous to new_offer), or None if the driver was idle. Caller
            (router.py) is responsible for fetching this from offer_history.

        current_odometer:
            Driver's cumulative odometer reading (miles) at the moment of
            new_offer receipt. Comes from heartbeat-log or the receipt
            request payload. Must be >= 0.

        now:
            UTC datetime of new_offer receipt. Caller should pass the
            offer's accepted_at field for consistency with other Phase 2c.2
            time anchors.

        prev_expected_dropoff_arrival_time:
            From offer_history: prev_offer.expected_dropoff_arrival_time.
            Required when prev_offer is not None (caller fetches alongside
            prev_offer). None means "no prior anchor" — treat as idle.

        prev_expected_dropoff_distance:
            From offer_history: prev_offer.expected_dropoff_distance.
            Same handling as the time anchor.

    Returns:
        OfferExpectations populated with the four anchor values, or None if
        new_offer is missing required time/distance fields. Caller (router.py)
        should treat None as "TAD skipped for this offer" and persist NULL
        in the four expected_* columns.

    Cancellation detection (forensic side-effect):
        If prev_expected_dropoff_arrival_time is in the past relative to
        `now`, the previous trip's expected dropoff has elapsed without
        completion — likely a cancellation or a missed dropoff confirmation.
        We log a warning (offer_id, time delta) and treat new_offer as if
        the driver were idle. No DB write — forensic trail lives in
        structured logs, grep-able by offer_id.

    Edge cases:
        - "Already halfway there" pattern (driver accepts a 0.8mi pickup
          while already 0.4mi into the approach): handled implicitly.
          current_odometer captures actual cumulative state at receipt;
          expected_pickup_distance = current_odometer + pickup_miles is
          a forward-looking target, never an "impossible" backward number.

        - new_offer arrives WITHOUT prev_offer present but driver is mid-
          trip (e.g., heartbeat capture lag, queue refresh race): prev_offer
          is None, expectations computed as idle. TAD distance gate will
          still work because cluster odometer is monotonically increasing;
          the time gate may be slightly optimistic but degrades gracefully.

        - Negative current_odometer: caller responsibility; this function
          does not validate. (The DB column is numeric, no constraint.)
    """
    # =========================================================================
    # v2.1 Section III: UTC-mandatory enforcement
    # =========================================================================
    # Hard fail-fast on naive datetimes. Silent tz coercion is exactly the
    # pattern that lets timezone bugs hide for months in production. Callers
    # MUST pass tz-aware UTC datetimes (psycopg returns these natively from
    # timestamptz columns; new datetimes should use datetime.now(timezone.utc)).
    if now.tzinfo is None:
        raise ValueError(
            "tad.compute_offer_expectations: 'now' must be tz-aware UTC, "
            "got naive datetime. (See PuddleJumper Canonical Standards v2.1 "
            "Section III.)"
        )
    if (prev_expected_dropoff_arrival_time is not None
            and prev_expected_dropoff_arrival_time.tzinfo is None):
        raise ValueError(
            "tad.compute_offer_expectations: 'prev_expected_dropoff_arrival_time' "
            "must be tz-aware UTC, got naive datetime."
        )

    # Required offer fields. Defensive: missing minutes or miles means we
    # can't compute. Caller persists NULL and skips TAD for this offer.
    if (new_offer.pickup_minutes is None
            or new_offer.pickup_miles is None
            or new_offer.trip_minutes is None
            or new_offer.trip_miles is None):
        log.warning(
            "[tad] compute_offer_expectations: offer %s missing required "
            "fields (pickup_minutes=%r, pickup_miles=%r, trip_minutes=%r, "
            "trip_miles=%r) — skipping TAD",
            new_offer.offer_id,
            new_offer.pickup_minutes, new_offer.pickup_miles,
            new_offer.trip_minutes, new_offer.trip_miles,
        )
        return None

    # Determine pickup anchors from prev_offer's dropoff anchors if available
    # AND still in the future. Else fall back to idle.
    use_idle_anchors = True
    pickup_time_anchor = now
    pickup_distance_anchor = current_odometer

    if prev_offer is not None:
        # We expect both prev anchors to be present together; if either is
        # None, treat as if prev_offer didn't exist (no anchor available).
        if (prev_expected_dropoff_arrival_time is not None
                and prev_expected_dropoff_distance is not None):
            # Cancellation detection: prev's expected dropoff already past?
            if prev_expected_dropoff_arrival_time < now:
                delta_s = (now - prev_expected_dropoff_arrival_time).total_seconds()
                log.warning(
                    "[tad] offer %s: prev offer %s expected_dropoff_arrival_time "
                    "is %.1fs in the past — treating prev as orphaned, computing "
                    "expectations as idle case",
                    new_offer.offer_id, prev_offer.offer_id, delta_s,
                )
                # Fall through to idle anchors (already initialized).
            else:
                # Stacked case: chain off prev's anchors.
                use_idle_anchors = False
                pickup_time_anchor = prev_expected_dropoff_arrival_time
                pickup_distance_anchor = prev_expected_dropoff_distance

    # Compute pickup anchors.
    expected_pickup_arrival_time = (
        pickup_time_anchor + timedelta(seconds=new_offer.pickup_minutes * 60)
    )
    expected_pickup_distance = (
        float(pickup_distance_anchor) + float(new_offer.pickup_miles)
    )

    # Compute dropoff anchors (always derived from pickup + trip).
    expected_dropoff_arrival_time = (
        expected_pickup_arrival_time
        + timedelta(seconds=new_offer.trip_minutes * 60)
    )
    expected_dropoff_distance = (
        expected_pickup_distance + float(new_offer.trip_miles)
    )

    if log.isEnabledFor(logging.DEBUG):
        log.debug(
            "[tad] offer %s: anchors=%s, pickup_eta=%s, pickup_dist=%.3f, "
            "dropoff_eta=%s, dropoff_dist=%.3f",
            new_offer.offer_id,
            "idle" if use_idle_anchors else "stacked",
            expected_pickup_arrival_time.isoformat(),
            expected_pickup_distance,
            expected_dropoff_arrival_time.isoformat(),
            expected_dropoff_distance,
        )

    return OfferExpectations(
        expected_pickup_arrival_time=expected_pickup_arrival_time,
        expected_pickup_distance=expected_pickup_distance,
        expected_dropoff_arrival_time=expected_dropoff_arrival_time,
        expected_dropoff_distance=expected_dropoff_distance,
    )