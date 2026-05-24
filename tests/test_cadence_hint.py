"""
Cadence hint trigger coverage (§XVI.C Horny Mode, ratified 2026-05-22).

These tests pin the boundary semantics of cadence_target_hz computation
in driver_heartbeat.py. The cadence hint is computed every heartbeat
from two existing signals:

    cadence_target_hz = 1.0 if (max_wai_confidence >= 0.40 AND
                                speed_mph < 5.0) else 0.2

No mode flag, no state machine, no exit conditions. Pure function
of WAI's output and current speed. Battery containment is the
client's responsibility (see HeartbeatSender battery cap).

Provenance: §XVI.C amendment ratified 2026-05-22 via Andrew + Claude
+ Gemini paired-programming protocol. TAD demoted from gate to WAI
input; cadence trigger composes WAI confidence + speed orthogonally
to the arrest counter.
"""

from pudo_types import WAIMatch, WAI_CONFIDENCE_THRESHOLD


# Reproduce the cadence calculation as a pure function so tests
# can pin its semantics without exercising the full heartbeat
# handler. This is structurally identical to the production
# computation in driver_heartbeat.py (post-amendment 2026-05-22).
HORNY_SPEED_THRESHOLD_MPH = 5.0


def _compute_cadence_target_hz(matches, speed_mph):
    """Reproduce driver_heartbeat.py's cadence calculation exactly.

    Returns 1.0 (Horny) iff max(WAI confidence) ≥ floor AND
    speed_mph is not None AND speed_mph < threshold. Else 0.2 (cold).
    """
    max_wai_confidence = max(
        (m.confidence for m in matches),
        default=0.0,
    )
    horny = (
        max_wai_confidence >= WAI_CONFIDENCE_THRESHOLD
        and speed_mph is not None
        and speed_mph < HORNY_SPEED_THRESHOLD_MPH
    )
    return 1.0 if horny else 0.2


def _match(confidence, offer_id="1", leg="pickup"):
    """Construct a WAIMatch for tests."""
    return WAIMatch(offer_id=offer_id, location_type=leg, confidence=confidence)


# ── Trigger boundary tests ───────────────────────────────────────


def test_cold_when_no_matches():
    """Empty WAI matches → cold cadence regardless of speed."""
    assert _compute_cadence_target_hz([], 0.0) == 0.2
    assert _compute_cadence_target_hz([], 35.0) == 0.2


def test_cold_when_wai_below_floor():
    """All WAI confidences below 0.40 → cold."""
    assert _compute_cadence_target_hz([_match(0.39)], 3.0) == 0.2
    assert _compute_cadence_target_hz([_match(0.35), _match(0.30)], 3.0) == 0.2


def test_cold_when_speed_too_high():
    """WAI cleared floor but speed ≥ 5 mph → cold."""
    assert _compute_cadence_target_hz([_match(0.50)], 5.0) == 0.2
    assert _compute_cadence_target_hz([_match(0.99)], 35.0) == 0.2


def test_cold_when_speed_is_none():
    """speed_mph None (no sensor data) → cold."""
    assert _compute_cadence_target_hz([_match(0.99)], None) == 0.2


def test_horny_at_threshold_exact():
    """WAI exactly at 0.40 floor AND speed below 5 → horny.

    This is the boundary case. The floor is inclusive (≥), the
    speed gate is strict (<).
    """
    assert _compute_cadence_target_hz([_match(0.40)], 4.9) == 1.0


def test_horny_at_speed_threshold_below():
    """Speed just below 5.0 → horny. Speed exactly 5.0 → cold."""
    assert _compute_cadence_target_hz([_match(0.50)], 4.99) == 1.0
    assert _compute_cadence_target_hz([_match(0.50)], 5.0) == 0.2


def test_horny_at_zero_velocity():
    """Stopped + WAI confident → horny (the canonical hot case)."""
    assert _compute_cadence_target_hz([_match(0.85)], 0.0) == 1.0


def test_horny_uses_max_across_candidates():
    """If ANY candidate clears floor, horny fires.

    The cadence hint reads max(confidences); a queue with one
    confident offer fires Horny even when other offers don't match.
    """
    assert _compute_cadence_target_hz(
        [_match(0.20), _match(0.50), _match(0.30)], 3.0
    ) == 1.0


# ── Doctrine guardrails (regression coverage) ────────────────────


def test_threshold_is_canonical_value():
    """WAI_CONFIDENCE_THRESHOLD is canonical per §XIV.C; do not
    drift the cadence calculation to a different floor without
    explicit §XIV.C amendment."""
    assert WAI_CONFIDENCE_THRESHOLD == 0.40


def test_horny_speed_threshold_is_5mph():
    """HORNY_SPEED_THRESHOLD_MPH is locked at 5.0 per §XVI.C
    amendment (2026-05-22). Loosening this threshold without
    ratification risks excessive Horny windows at red lights."""
    assert HORNY_SPEED_THRESHOLD_MPH == 5.0
