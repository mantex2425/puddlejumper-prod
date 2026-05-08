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

Three TAD verdict states (tristate `passed: Optional[bool]`):
  - True:  Normal Mode pass. Distance in [85%, 115%] window. Caller applies
           elevator rule: weighted >= 0.90 OR (weighted >= 0.80 AND
           poi_type_match).
  - False: Distance < 85%. Driver hasn't arrived yet. Caller skips this
           candidate entirely (no spatial scoring, no API spend).
  - None:  Lost Mode. Either narrative_blindness (no anchor available) or
           narrative_violation (distance > 115%, narrative dead). Caller
           applies stricter Lost Mode rule: weighted >= 0.85 AND
           poi_type_match TRUE (mandatory Head 4 corroboration).

Lost Mode rationale:
  Houston driving has high-variance "narrative breaks" — dead zones where
  geocode fails (Calhoun Street / UH campus), long-way-round detours that
  blow distance budget by >15%, and orphaned offers that age out of queue.
  Without Lost Mode, a single missed PUDO would silence the system for the
  rest of the shift. Lost Mode "fails open" to spatial-only with stricter
  scrutiny — slight inaccuracies are acceptable; developer paralysis and
  No-Fires are not. (v2.1 Section VI mandate.)

Canonical compliance (PuddleJumper Standards v2.1):
  - Section III (Temporal): all datetimes are tz-aware UTC. naive datetimes
    cause ValueError at compute_offer_expectations() / evaluate_tad_gate()
    entry. No silent coercion.
  - Section IV (Logic): TAD is the mandatory hard-gate filter. Distance is
    the primary signal; time is asymmetric (boost only, never penalty).
    Time alone does NOT trigger Lost Mode (volatile signal — traffic,
    festivals, train delays).
  - Section V (Forensic): per-evaluation telemetry persists to
    pudo_decision_context.tad_decision_context jsonb (caller's writeback
    in where_am_i.evaluate()).

Module structure:
  Section A — Constants
  Section B — OfferExpectations dataclass + compute_offer_expectations()
  Section C — OfferTadState dataclass (per-offer DB-fetched state)
  Section D — TadVerdict dataclass (per-offer per-evaluation result)
  Section E — evaluate_tad_gate() public surface
  Section F — internal helpers (per-leg evaluators, distance gate, time signal)

Public surface:
    tad.compute_offer_expectations(...) -> Optional[OfferExpectations]
    tad.evaluate_tad_gate(cluster, queue_offers, current_odometer,
                          per_offer_state, lost_mode=False,
                          last_known_anchor_id=None) -> dict[str, TadVerdict]
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from cluster_detection import Cluster
from pudo_types import Offer

log = logging.getLogger(__name__)


# =============================================================================
# Section A — Constants (CATEGORY 1 per L-10: production-data-grounded)
# =============================================================================

# Distance gate window. Real PUDOs cluster between 85% and 115% of expected
# travel. Below 85%: driver hasn't arrived. Above 115%: narrative is dead
# (driver overshot, possibly missed a previous PUDO or detoured).
# Empirical: 7/8 of 2026-05-07 shift rides reached >= 85% completion at
# actual pickup; the 8th was a train-delay outlier the time signal correctly
# down-weighted.
DISTANCE_GATE_COMPLETION_THRESHOLD = 0.85
DISTANCE_GATE_OVERSHOOT_THRESHOLD = 1.15

# Below this trip length, percentage math is too noisy. Switch to absolute
# tolerance instead. 2.0mi is the empirical noise floor — at 1.5mi a 0.2mi
# navigation deviation is +13% / -13% but matters operationally.
SHORT_TRIP_THRESHOLD_MILES = 2.0

# Absolute tolerance for short trips. Catches 0.8mi pickup with +0.2mi
# navigation deviation (cluster forms at 1.0mi cumulative delta).
SHORT_TRIP_ABSOLUTE_TOLERANCE_MILES = 0.5

# Time signal (soft, asymmetric — never a penalty for being late).
TIME_SIGNAL_TIGHT_PCT = 0.15      # |error| <= 15% of expected duration
TIME_SIGNAL_TIGHT_BOOST = 0.15
TIME_SIGNAL_LOOSE_PCT = 0.50      # |error| <= 50% of expected duration
TIME_SIGNAL_LOOSE_BOOST = 0.05
# Outside +-50% the boost is 0 (no confidence loss for being late).


# =============================================================================
# Section B — OfferExpectations + compute_offer_expectations()
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
            UTC datetime of new_offer receipt. MUST BE TZ-AWARE UTC.

        prev_expected_dropoff_arrival_time:
            From offer_history: prev_offer.expected_dropoff_arrival_time.
            Required when prev_offer is not None. None means "no prior
            anchor" — treat as idle.

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

        - Negative current_odometer: caller responsibility; this function
          does not validate. (The DB column is numeric, no constraint.)
    """
    # v2.1 Section III: UTC-mandatory enforcement
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
        if (prev_expected_dropoff_arrival_time is not None
                and prev_expected_dropoff_distance is not None):
            if prev_expected_dropoff_arrival_time < now:
                delta_s = (now - prev_expected_dropoff_arrival_time).total_seconds()
                log.warning(
                    "[tad] offer %s: prev offer %s expected_dropoff_arrival_time "
                    "is %.1fs in the past — treating prev as orphaned, computing "
                    "expectations as idle case",
                    new_offer.offer_id, prev_offer.offer_id, delta_s,
                )
            else:
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


# =============================================================================
# Section C — OfferTadState
# =============================================================================

@dataclass(frozen=True)
class OfferTadState:
    """Per-offer state needed for TAD gate evaluation.

    Caller (where_am_i.evaluate()) assembles one of these per offer in the
    queue from offer_history rows fetched alongside DriverQueue.offers().
    Frozen because TAD treats it as a snapshot of the offer's persisted
    state at evaluation time.

    All fields source from app_private.offer_history columns of the same
    name except where noted.

      offer_id: matches Offer.offer_id.
      miles_at_offer_receipt: pickup leg distance anchor (cumulative miles).
      accepted_at: pickup leg time anchor (== offer_history.created_at).
      expected_pickup_arrival_time: pickup leg time target. tz-aware UTC.
      expected_pickup_distance: pickup leg distance target (cumulative miles).
      actual_pickup_at: NULL until pickup confirms. XOR discriminator —
        NULL=evaluate pickup leg, non-NULL=evaluate dropoff leg.
      cumulative_miles_at_pickup_fire: dropoff leg distance anchor.
        Required when actual_pickup_at is non-NULL.
      pickup_exit_time: dropoff leg time anchor. NULL during post-pickup-
        pre-exit window OR when exit_velocity_timeout=TRUE.
      exit_velocity_timeout: TRUE if 30 min elapsed post-pickup without
        exit velocity firing. Forces dropoff time signal to applied=False.
    """
    offer_id: str
    miles_at_offer_receipt: float
    accepted_at: datetime
    expected_pickup_arrival_time: datetime
    expected_pickup_distance: float
    actual_pickup_at: Optional[datetime]
    cumulative_miles_at_pickup_fire: Optional[float]
    pickup_exit_time: Optional[datetime]
    exit_velocity_timeout: bool


# =============================================================================
# Section D — TadVerdict
# =============================================================================

@dataclass(frozen=True)
class TadVerdict:
    """Per-offer verdict from evaluate_tad_gate.

    Tristate `passed`:
      True:  Normal Mode TAD passed (distance in [85%, 115%] window).
             Caller applies elevator rule.
      False: Distance < 85%. Caller skips this candidate entirely.
      None:  Lost Mode (narrative_blindness OR narrative_violation).
             Caller applies stricter Lost Mode rule.

    Field semantics:
      passed: tristate as above.
      time_boost: 0.0, 0.05, or 0.15. Always 0.0 in Lost Mode and when time
        signal can't be applied (timeout, missing anchor).
      leg_evaluated: 'pickup' or 'dropoff' or 'unknown'.
      lost_mode_reason: None when passed in (True, False); 'narrative_
        blindness' or 'narrative_violation' when passed=None.
      distance_gate: JSONB-shaped dict. Always populated.
      time_signal: JSONB-shaped dict OR None.
    """
    passed: Optional[bool]
    time_boost: float
    leg_evaluated: str
    lost_mode_reason: Optional[str]
    distance_gate: dict
    time_signal: Optional[dict]


# =============================================================================
# Section E — evaluate_tad_gate (public surface)
# =============================================================================

def evaluate_tad_gate(
    cluster: Cluster,
    queue_offers: tuple[Offer, ...],
    current_odometer: float,
    per_offer_state: dict[str, OfferTadState],
    lost_mode: bool = False,
    last_known_anchor_id: Optional[str] = None,
) -> dict[str, TadVerdict]:
    """For each offer in queue_offers, evaluate the appropriate leg's TAD gate.

    Pure function. No DB calls. No state mutation.

    Per-offer evaluation per the XOR-by-pickup-confirmation model:
      - state.actual_pickup_at IS NULL  -> evaluate pickup leg
      - state.actual_pickup_at IS NOT NULL -> evaluate dropoff leg

    Lost Mode:
      Caller signals Lost Mode when the narrative chain is broken (prev
      dropoff unconfirmed, queue empty after GC, etc.). When lost_mode=True,
      ALL verdicts return passed=None with lost_mode_reason='narrative_
      blindness'. Distance gate may still be evaluated for forensic capture
      but does not affect the verdict.

      Lost Mode can ALSO trigger per-offer when distance overshoots 115% of
      expected — the narrative is dead even if the overall context wasn't
      flagged. Returns passed=None with lost_mode_reason='narrative_
      violation'.

    No promotion logic, no cancellation inference. The Reduce step in
    where_am_i.evaluate() picks winners across all offers' legs by combined
    confidence, applying Normal Mode rule when verdict.passed=True or Lost
    Mode rule when verdict.passed=None.

    Args:
        cluster: stationary cluster currently being evaluated. cluster.latest
            is the time anchor for time signal scoring. tz-aware UTC required.
        queue_offers: tuple[Offer, ...] from DriverQueue.offers(cur).
        current_odometer: cumulative miles at evaluation time.
        per_offer_state: dict keyed by offer_id. Caller assembles from
            offer_history. Offers without an entry record TAD-failed verdicts
            with logged warning.
        lost_mode: when True, all verdicts return passed=None with
            narrative_blindness reason. Caller decides when to enter
            Lost Mode based on its own state knowledge.
        last_known_anchor_id: optional offer_id of the most recent confirmed
            anchor (for forensic blob capture during Lost Mode).

    Returns:
        dict[offer_id, TadVerdict]. One entry per offer in queue_offers.
    """
    if cluster.latest.tzinfo is None:
        raise ValueError(
            "evaluate_tad_gate: cluster.latest must be tz-aware UTC, got "
            "naive datetime. (See PuddleJumper Canonical Standards v2.1 "
            "Section III.)"
        )

    verdicts: dict[str, TadVerdict] = {}

    for offer in queue_offers:
        state = per_offer_state.get(offer.offer_id)
        if state is None:
            log.warning(
                "[tad] evaluate_tad_gate: offer %s in queue but missing "
                "from per_offer_state — recording TAD-failed verdict",
                offer.offer_id,
            )
            verdicts[offer.offer_id] = TadVerdict(
                passed=False,
                time_boost=0.0,
                leg_evaluated="unknown",
                lost_mode_reason=None,
                distance_gate={
                    "mode": "missing_state",
                    "passed": False,
                    "fail_reason": "no per_offer_state entry",
                },
                time_signal=None,
            )
            continue

        # If caller signaled global Lost Mode (narrative_blindness),
        # short-circuit per-offer evaluation.
        if lost_mode:
            verdicts[offer.offer_id] = _build_lost_mode_verdict(
                cluster=cluster,
                offer=offer,
                state=state,
                current_odometer=current_odometer,
                reason="narrative_blindness",
                last_known_anchor_id=last_known_anchor_id,
            )
            continue

        # XOR discriminator
        if state.actual_pickup_at is None:
            verdicts[offer.offer_id] = _evaluate_pickup_leg(
                cluster, offer, state, current_odometer,
            )
        else:
            verdicts[offer.offer_id] = _evaluate_dropoff_leg(
                cluster, offer, state, current_odometer,
            )

    return verdicts


# =============================================================================
# Section F — Internal helpers
# =============================================================================

def _evaluate_pickup_leg(
    cluster: Cluster,
    offer: Offer,
    state: OfferTadState,
    current_odometer: float,
) -> TadVerdict:
    """Distance + time evaluation for an offer whose pickup hasn't fired yet."""
    if offer.pickup_miles is None or offer.pickup_minutes is None:
        log.warning(
            "[tad] _evaluate_pickup_leg: offer %s missing pickup_miles or "
            "pickup_minutes — recording permissive (passed=True, no boost)",
            offer.offer_id,
        )
        return TadVerdict(
            passed=True,
            time_boost=0.0,
            leg_evaluated="pickup",
            lost_mode_reason=None,
            distance_gate={
                "mode": "missing_offer_fields",
                "passed": True,
                "fail_reason": None,
            },
            time_signal=None,
        )

    expected_distance = state.expected_pickup_distance
    leg_start_odometer = expected_distance - float(offer.pickup_miles)
    actual_delta = current_odometer - leg_start_odometer
    expected_delta = float(offer.pickup_miles)

    distance_gate = _evaluate_distance_gate(
        actual_delta_miles=actual_delta,
        expected_delta_miles=expected_delta,
    )

    # Tristate: True (in window), False (under), None (overshoot/lost)
    if distance_gate["passed"] is None:
        # Lost Mode: narrative_violation (overshoot)
        return TadVerdict(
            passed=None,
            time_boost=0.0,
            leg_evaluated="pickup",
            lost_mode_reason="narrative_violation",
            distance_gate=distance_gate,
            time_signal=None,
        )

    if not distance_gate["passed"]:
        return TadVerdict(
            passed=False,
            time_boost=0.0,
            leg_evaluated="pickup",
            lost_mode_reason=None,
            distance_gate=distance_gate,
            time_signal=None,
        )

    # Distance gate passed in [85%, 115%] window — compute time signal
    time_signal = _evaluate_time_signal(
        cluster_time=cluster.latest,
        expected_arrival_time=state.expected_pickup_arrival_time,
        leg_duration_minutes=int(offer.pickup_minutes),
    )

    return TadVerdict(
        passed=True,
        time_boost=time_signal["boost"] if time_signal else 0.0,
        leg_evaluated="pickup",
        lost_mode_reason=None,
        distance_gate=distance_gate,
        time_signal=time_signal,
    )


def _evaluate_dropoff_leg(
    cluster: Cluster,
    offer: Offer,
    state: OfferTadState,
    current_odometer: float,
) -> TadVerdict:
    """Distance + time evaluation for an offer whose pickup has fired.

    Distance anchor: cumulative_miles_at_pickup_fire (cluster centroid).
    Time anchor: pickup_exit_time (when driver crossed exit velocity).
    """
    if offer.trip_miles is None or offer.trip_minutes is None:
        log.warning(
            "[tad] _evaluate_dropoff_leg: offer %s missing trip_miles or "
            "trip_minutes — recording permissive (passed=True, no boost)",
            offer.offer_id,
        )
        return TadVerdict(
            passed=True,
            time_boost=0.0,
            leg_evaluated="dropoff",
            lost_mode_reason=None,
            distance_gate={
                "mode": "missing_offer_fields",
                "passed": True,
                "fail_reason": None,
            },
            time_signal=None,
        )

    if state.cumulative_miles_at_pickup_fire is None:
        log.warning(
            "[tad] _evaluate_dropoff_leg: offer %s has actual_pickup_at=%s "
            "but cumulative_miles_at_pickup_fire IS NULL — invariant violation, "
            "recording permissive verdict",
            offer.offer_id, state.actual_pickup_at,
        )
        return TadVerdict(
            passed=True,
            time_boost=0.0,
            leg_evaluated="dropoff",
            lost_mode_reason=None,
            distance_gate={
                "mode": "invariant_violation",
                "passed": True,
                "fail_reason": "cumulative_miles_at_pickup_fire is NULL",
            },
            time_signal=None,
        )

    leg_start_odometer = float(state.cumulative_miles_at_pickup_fire)
    actual_delta = current_odometer - leg_start_odometer
    expected_delta = float(offer.trip_miles)

    distance_gate = _evaluate_distance_gate(
        actual_delta_miles=actual_delta,
        expected_delta_miles=expected_delta,
    )

    if distance_gate["passed"] is None:
        # Lost Mode: narrative_violation (long-way-round detour or missed dropoff)
        return TadVerdict(
            passed=None,
            time_boost=0.0,
            leg_evaluated="dropoff",
            lost_mode_reason="narrative_violation",
            distance_gate=distance_gate,
            time_signal=None,
        )

    if not distance_gate["passed"]:
        return TadVerdict(
            passed=False,
            time_boost=0.0,
            leg_evaluated="dropoff",
            lost_mode_reason=None,
            distance_gate=distance_gate,
            time_signal=None,
        )

    # Distance gate passed in [85%, 115%] window — determine time anchor.
    if state.pickup_exit_time is not None:
        expected_dropoff_arrival = (
            state.pickup_exit_time
            + timedelta(seconds=int(offer.trip_minutes) * 60)
        )
        time_signal = _evaluate_time_signal(
            cluster_time=cluster.latest,
            expected_arrival_time=expected_dropoff_arrival,
            leg_duration_minutes=int(offer.trip_minutes),
        )
    elif state.exit_velocity_timeout:
        time_signal = {
            "expected_arrival_time": None,
            "cluster_time": cluster.latest.isoformat(),
            "error_pct": None,
            "boost": 0.0,
            "applied": False,
            "reason": "exit_velocity_timeout",
        }
    else:
        log.warning(
            "[tad] _evaluate_dropoff_leg: offer %s has actual_pickup_at=%s "
            "but pickup_exit_time IS NULL AND exit_velocity_timeout=FALSE — "
            "treating as timeout (no time signal applied)",
            offer.offer_id, state.actual_pickup_at,
        )
        time_signal = {
            "expected_arrival_time": None,
            "cluster_time": cluster.latest.isoformat(),
            "error_pct": None,
            "boost": 0.0,
            "applied": False,
            "reason": "invariant_violation_no_anchor",
        }

    return TadVerdict(
        passed=True,
        time_boost=time_signal["boost"] if time_signal else 0.0,
        leg_evaluated="dropoff",
        lost_mode_reason=None,
        distance_gate=distance_gate,
        time_signal=time_signal,
    )


def _build_lost_mode_verdict(
    cluster: Cluster,
    offer: Offer,
    state: OfferTadState,
    current_odometer: float,
    reason: str,
    last_known_anchor_id: Optional[str],
) -> TadVerdict:
    """Build a Lost Mode verdict (passed=None) for a single offer.

    Used when caller signals global Lost Mode (narrative_blindness). The
    distance gate is still evaluated for forensic capture so Phase 2g can
    see "what would TAD have said if it weren't lost?" — but it doesn't
    affect the verdict.

    The narrative_violation case (overshoot detected per-offer) does NOT
    flow through this helper — see _evaluate_*_leg functions.
    """
    leg_evaluated = "pickup" if state.actual_pickup_at is None else "dropoff"

    distance_gate = {
        "mode": "lost_mode",
        "lost_mode_reason": reason,
        "last_known_anchor_id": last_known_anchor_id,
        "passed": None,
        "fail_reason": None,
    }

    return TadVerdict(
        passed=None,
        time_boost=0.0,
        leg_evaluated=leg_evaluated,
        lost_mode_reason=reason,
        distance_gate=distance_gate,
        time_signal=None,
    )


def _evaluate_distance_gate(
    actual_delta_miles: float,
    expected_delta_miles: float,
) -> dict:
    """Evaluate the distance gate. Tristate result.

    Modes:
      'percentage' (trips >= SHORT_TRIP_THRESHOLD_MILES = 2.0):
        - actual < 0.85 * expected     -> passed=False (skip, not arrived)
        - 0.85 <= actual <= 1.15       -> passed=True (in window)
        - actual > 1.15 * expected     -> passed=None (Lost Mode, overshot)

      'absolute_short_trip' (trips < 2.0):
        - |actual - expected| <= 0.5 mi -> passed=True
        - actual < expected - 0.5 mi    -> passed=False (not arrived)
        - actual > expected + 0.5 mi    -> passed=None (Lost Mode, overshot)

    Returns JSONB-shaped dict matching the schema-documented structure.
    """
    if expected_delta_miles >= SHORT_TRIP_THRESHOLD_MILES:
        completion_pct = (
            actual_delta_miles / expected_delta_miles
            if expected_delta_miles > 0 else 0.0
        )

        if completion_pct >= DISTANCE_GATE_OVERSHOOT_THRESHOLD:
            # Lost Mode: narrative dead, driver overshot
            return {
                "mode": "percentage",
                "expected_distance_miles": expected_delta_miles,
                "actual_delta_miles": actual_delta_miles,
                "completion_pct": completion_pct,
                "passed": None,
                "fail_reason": (
                    f"completion {completion_pct:.3f} > "
                    f"{DISTANCE_GATE_OVERSHOOT_THRESHOLD} (lost mode, overshot)"
                ),
            }

        passed = completion_pct >= DISTANCE_GATE_COMPLETION_THRESHOLD
        return {
            "mode": "percentage",
            "expected_distance_miles": expected_delta_miles,
            "actual_delta_miles": actual_delta_miles,
            "completion_pct": completion_pct,
            "passed": passed,
            "fail_reason": (
                None if passed
                else f"completion {completion_pct:.3f} < "
                     f"{DISTANCE_GATE_COMPLETION_THRESHOLD}"
            ),
        }

    # Short trip: absolute tolerance
    delta_from_expected = actual_delta_miles - expected_delta_miles

    # Overshoot: actual > expected + tolerance → Lost Mode
    if delta_from_expected > SHORT_TRIP_ABSOLUTE_TOLERANCE_MILES:
        return {
            "mode": "absolute_short_trip",
            "expected_distance_miles": expected_delta_miles,
            "actual_delta_miles": actual_delta_miles,
            "tolerance_miles": SHORT_TRIP_ABSOLUTE_TOLERANCE_MILES,
            "passed": None,
            "fail_reason": (
                f"delta_from_expected {delta_from_expected:+.3f}mi > "
                f"+{SHORT_TRIP_ABSOLUTE_TOLERANCE_MILES}mi (lost mode, overshot)"
            ),
        }

    passed = abs(delta_from_expected) <= SHORT_TRIP_ABSOLUTE_TOLERANCE_MILES
    return {
        "mode": "absolute_short_trip",
        "expected_distance_miles": expected_delta_miles,
        "actual_delta_miles": actual_delta_miles,
        "tolerance_miles": SHORT_TRIP_ABSOLUTE_TOLERANCE_MILES,
        "passed": passed,
        "fail_reason": (
            None if passed
            else f"delta_from_expected {delta_from_expected:+.3f}mi outside "
                 f"+-{SHORT_TRIP_ABSOLUTE_TOLERANCE_MILES}mi"
        ),
    }


def _evaluate_time_signal(
    cluster_time: datetime,
    expected_arrival_time: datetime,
    leg_duration_minutes: int,
) -> dict:
    """Per the schema-documented JSONB structure for time_signal.

    Boost rules (asymmetric — never penalty):
      |error_pct| <= 0.15  -> boost = 0.15
      |error_pct| <= 0.50  -> boost = 0.05
      otherwise            -> boost = 0.0

    error_pct = |cluster_time - expected_arrival_time| / leg_duration

    Defensive: if leg_duration_minutes is 0 or negative, returns boost=0
    with reason=invalid_duration.
    """
    if leg_duration_minutes <= 0:
        return {
            "expected_arrival_time": expected_arrival_time.isoformat(),
            "cluster_time": cluster_time.isoformat(),
            "error_pct": None,
            "boost": 0.0,
            "applied": False,
            "reason": "invalid_duration",
        }

    error_seconds = abs((cluster_time - expected_arrival_time).total_seconds())
    duration_seconds = leg_duration_minutes * 60
    error_pct = error_seconds / duration_seconds

    if error_pct <= TIME_SIGNAL_TIGHT_PCT:
        boost = TIME_SIGNAL_TIGHT_BOOST
    elif error_pct <= TIME_SIGNAL_LOOSE_PCT:
        boost = TIME_SIGNAL_LOOSE_BOOST
    else:
        boost = 0.0

    return {
        "expected_arrival_time": expected_arrival_time.isoformat(),
        "cluster_time": cluster_time.isoformat(),
        "error_pct": error_pct,
        "boost": boost,
        "applied": True,
    }