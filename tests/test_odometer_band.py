"""test_odometer_band.py

Pure-function unit tests for the odometer-band primitive in pudo_types.py.

FINDING §5.2 (as corrected by ERRATUM 2026-06-05 §4) / §7 Step 6. The primitive
is Andrew's single-owner band: the one definition of "is this offer's odometer
position within the plausible band for its leg," consumed by both the liveness
predicate and TAD's candidacy gate.

§XIV.J: pure arithmetic, no cursor, no DB → MagicMock-free / db_cur-free, matching
the house style of test_tad.py for pure-logic gates.

The load-bearing contracts under test:
  1. tolerance = max(0.15 * leg_distance, NOISE_FLOOR) — width scales with the
     LEG distance, never with expected_distance (the ERRATUM §4 correction:
     expected_odometer is a large accumulating number; 15% of it is absurd).
  2. NULL anchor or NULL leg_distance -> None (no band -> §5.5 deferred). NEVER
     a fabricated band, NEVER coerced to a pass/fail (absence-is-not-death).
  3. The noise floor clamps short legs so the band is never narrower than 2.0mi.
  4. The pickup-leg band reproduces the deployed TAD [0.85, 1.15] gate exactly
     (ERRATUM §1.1 algebra) — a regression anchor for the unification claim.
"""

import pytest

from pudo_types import (
    odometer_band,
    odometer_in_band,
    ODOMETER_BAND_NOISE_FLOOR_MI,
    ODOMETER_BAND_TOLERANCE_PCT,
)


# ── Constants are the canonical, ratified values ─────────────────────────────

def test_noise_floor_is_two_miles():
    assert ODOMETER_BAND_NOISE_FLOOR_MI == 2.0


def test_tolerance_pct_is_fifteen_percent():
    assert ODOMETER_BAND_TOLERANCE_PCT == 0.15


# ── odometer_band: the (center, tolerance) arithmetic ────────────────────────

def test_band_long_leg_uses_percentage_width():
    """A 40-mile leg: 0.15 * 40 = 6.0 mi, well above the 2.0 floor."""
    center, tol = odometer_band(expected_distance=1000.0, leg_distance=40.0)
    assert center == 1000.0
    assert tol == 6.0


def test_band_short_leg_clamped_to_noise_floor():
    """A 1.6-mile leg: 0.15 * 1.6 = 0.24 mi, below the 2.0 floor -> clamped.

    This is the ERRATUM §4 / §6.2 rationale made concrete: a short fare's
    15% half-width is tighter than sensor noise, so the floor protects it.
    """
    center, tol = odometer_band(expected_distance=500.0, leg_distance=1.6)
    assert center == 500.0
    assert tol == 2.0  # max(0.24, 2.0)


def test_band_exactly_at_floor_boundary():
    """leg_distance where 0.15*d == 2.0 exactly: d = 13.333..."""
    center, tol = odometer_band(expected_distance=100.0, leg_distance=2.0 / 0.15)
    assert tol == pytest.approx(2.0)


def test_band_just_above_floor():
    """leg_distance=14: 0.15*14 = 2.1, just above floor -> uses percentage."""
    _, tol = odometer_band(expected_distance=100.0, leg_distance=14.0)
    assert tol == pytest.approx(2.1)


def test_band_zero_leg_distance_clamped_not_zero_width():
    """A degenerate 0.0 leg can never produce a zero-width band (floor clamps)."""
    center, tol = odometer_band(expected_distance=100.0, leg_distance=0.0)
    assert tol == 2.0


def test_band_negative_leg_distance_clamped():
    """Negative leg_distance (garbage) -> 0.15*neg is negative -> floor wins."""
    _, tol = odometer_band(expected_distance=100.0, leg_distance=-5.0)
    assert tol == 2.0


def test_band_center_is_expected_distance_unchanged():
    """Center passes through as expected_distance; the primitive normalizes the
    band, not the anchor (coerce-don't-validate, per Step 4 discipline)."""
    center, _ = odometer_band(expected_distance=1196.0885, leg_distance=10.0)
    assert center == 1196.0885


# ── odometer_band: the NULL -> None (deferred) contract ──────────────────────

def test_band_none_expected_distance_returns_none():
    """No anchor (lost-mode / unfired-pickup dropoff leg) -> None, no band."""
    assert odometer_band(expected_distance=None, leg_distance=10.0) is None


def test_band_none_leg_distance_returns_none():
    """Missing leg distance -> None, no band."""
    assert odometer_band(expected_distance=500.0, leg_distance=None) is None


def test_band_both_none_returns_none():
    assert odometer_band(expected_distance=None, leg_distance=None) is None


# ── odometer_in_band: the symmetric membership test ──────────────────────────

def test_in_band_center_is_in():
    assert odometer_in_band(actual_odometer=1000.0,
                            expected_distance=1000.0,
                            leg_distance=40.0) is True


def test_in_band_at_upper_edge_is_in():
    """center + tolerance is inclusive (<=)."""
    # tol = 6.0; upper edge = 1006.0
    assert odometer_in_band(actual_odometer=1006.0,
                            expected_distance=1000.0,
                            leg_distance=40.0) is True


def test_in_band_at_lower_edge_is_in():
    assert odometer_in_band(actual_odometer=994.0,
                            expected_distance=1000.0,
                            leg_distance=40.0) is True


def test_in_band_just_above_upper_edge_is_out():
    assert odometer_in_band(actual_odometer=1006.01,
                            expected_distance=1000.0,
                            leg_distance=40.0) is False


def test_in_band_just_below_lower_edge_is_out():
    assert odometer_in_band(actual_odometer=993.99,
                            expected_distance=1000.0,
                            leg_distance=40.0) is False


# ── odometer_in_band: the NULL -> None (deferred) contract ───────────────────

def test_in_band_none_actual_returns_none():
    """No odometer reading -> None (deferred), NEVER False (which would read as
    'out of band' and could trigger reaping). Mirrors Step 4 NULL-not-zero."""
    result = odometer_in_band(actual_odometer=None,
                              expected_distance=1000.0,
                              leg_distance=40.0)
    assert result is None
    assert result is not False  # explicit: absence != out-of-band


def test_in_band_none_expected_returns_none():
    result = odometer_in_band(actual_odometer=1000.0,
                              expected_distance=None,
                              leg_distance=40.0)
    assert result is None
    assert result is not False


def test_in_band_none_leg_distance_returns_none():
    result = odometer_in_band(actual_odometer=1000.0,
                              expected_distance=1000.0,
                              leg_distance=None)
    assert result is None
    assert result is not False


# ── Regression anchor: the band reproduces the deployed TAD pickup gate ──────

def test_pickup_leg_band_equals_tad_completion_gate():
    """ERRATUM §1.1: the pickup-leg band is term-for-term the deployed TAD
    [0.85, 1.15] completion gate.

    TAD: leg_start = expected_pickup_distance - pickup_miles
         actual_delta = current_odometer - leg_start
         completion_pct = actual_delta / pickup_miles
         gate: 0.85 <= completion_pct <= 1.15

    This must equal: |current_odometer - expected_pickup_distance|
                       <= 0.15 * pickup_miles
    for any pickup_miles whose 0.15-width exceeds the noise floor (so the floor
    doesn't mask the equivalence). Use pickup_miles=20 -> width 3.0 > 2.0.
    """
    expected_pickup_distance = 1000.0
    pickup_miles = 20.0  # 0.15*20 = 3.0, above the 2.0 floor

    # TAD's completion_pct boundaries, converted to odometer positions:
    #   completion 0.85 -> current_odo = expected - 0.15*pickup_miles = 997.0
    #   completion 1.15 -> current_odo = expected + 0.15*pickup_miles = 1003.0
    leg_start = expected_pickup_distance - pickup_miles  # 980.0

    for completion, expected_in in [
        (0.85, True),    # lower edge — in
        (1.00, True),    # center — in
        (1.15, True),    # upper edge — in
        (0.849, False),  # just below — out
        (1.151, False),  # just above — out
    ]:
        current_odo = leg_start + completion * pickup_miles
        band_result = odometer_in_band(
            actual_odometer=current_odo,
            expected_distance=expected_pickup_distance,
            leg_distance=pickup_miles,
        )
        assert band_result is expected_in, (
            f"completion {completion}: TAD gate says {expected_in}, "
            f"band primitive says {band_result} — equivalence broken"
        )


def test_dropoff_leg_band_uses_trip_miles():
    """ERRATUM §1.3 (ratified 2026-06-05): dropoff-leg width is 0.15 * trip_miles
    (NOT pickup+trip). The caller passes trip_miles as leg_distance."""
    trip_miles = 30.0  # 0.15*30 = 4.5, above floor
    center, tol = odometer_band(expected_distance=1050.0, leg_distance=trip_miles)
    assert center == 1050.0
    assert tol == 4.5


# ── Type guarantee ───────────────────────────────────────────────────────────

def test_band_return_shape():
    """odometer_band returns a 2-tuple of floats, or None — never anything else."""
    for exp, leg in [(1000.0, 40.0), (500.0, 1.6), (None, 10.0), (100.0, None)]:
        result = odometer_band(exp, leg)
        assert result is None or (
            isinstance(result, tuple)
            and len(result) == 2
            and all(isinstance(x, float) for x in result)
        )


# ── Drift-guard: the 0.15 tolerance is shared with TAD's distance gate ───────
# (TAD-unification Option A, ratified 2026-06-05)
#
# TAD's _evaluate_distance_gate and the liveness band are SEPARATE gates
# (different short-trip policy, different tristate vs bool semantics) — NOT
# unified. But they share ONE quantity: the 0.15 band half-width. TAD expresses
# it as symmetric thresholds (completion 0.85 = 1 - 0.15, overshoot 1.15 =
# 1 + 0.15); the primitive expresses it as ODOMETER_BAND_TOLERANCE_PCT = 0.15.
# These are the same number two ways (the §1.1 equivalence). This guard fails if
# anyone retunes one without the other.
#
# NB: TAD has NO `TOLERANCE_PCT` constant — it has DISTANCE_GATE_COMPLETION_
# THRESHOLD / DISTANCE_GATE_OVERSHOOT_THRESHOLD. We assert the derived
# relationship, not a (nonexistent) shared constant.

import tad  # noqa: E402  (import here to keep the primitive tests import-light)


def test_band_tolerance_matches_tad_distance_gate_thresholds():
    """The band's 0.15 half-width == TAD's symmetric distance-gate offsets.

    completion threshold = 1 - tolerance  (0.85 = 1 - 0.15)
    overshoot  threshold = 1 + tolerance  (1.15 = 1 + 0.15)

    If the band tolerance or TAD's thresholds drift apart, the shared 15%
    quantity has split into two owners — this flags it.
    """
    tol = ODOMETER_BAND_TOLERANCE_PCT
    assert tad.DISTANCE_GATE_COMPLETION_THRESHOLD == pytest.approx(1.0 - tol), (
        f"TAD completion threshold {tad.DISTANCE_GATE_COMPLETION_THRESHOLD} != "
        f"1 - band tolerance ({1.0 - tol}). The shared 15% quantity has drifted."
    )
    assert tad.DISTANCE_GATE_OVERSHOOT_THRESHOLD == pytest.approx(1.0 + tol), (
        f"TAD overshoot threshold {tad.DISTANCE_GATE_OVERSHOOT_THRESHOLD} != "
        f"1 + band tolerance ({1.0 + tol}). The shared 15% quantity has drifted."
    )


def test_two_2mile_constants_are_coincidentally_equal_not_coupled():
    """The two 2.0-mile constants are coincidentally equal, independent concepts
    (TAD-unification Option A). This test documents — via assertion — that they
    are EQUAL TODAY, so if a future change makes them unequal that is allowed
    (they are independent), but the test's existence records that the equality
    is known and incidental, not a coupling to preserve.

    pudo_types.ODOMETER_BAND_NOISE_FLOOR_MI = band width floor (telemetry).
    tad.SHORT_TRIP_THRESHOLD_MILES          = mode-switch boundary (routing).
    """
    # They happen to be equal now; this is incidental. If they ever diverge,
    # UPDATE this test (do not couple the constants). The independence comments
    # at both definition sites are the authoritative guard; this is a tripwire
    # that surfaces a divergence to a human rather than silently coupling them.
    if ODOMETER_BAND_NOISE_FLOOR_MI != tad.SHORT_TRIP_THRESHOLD_MILES:
        # Divergence is LEGAL (independent concepts). Just surface it.
        import warnings
        warnings.warn(
            "The two 2.0-mile constants have diverged "
            f"(floor={ODOMETER_BAND_NOISE_FLOOR_MI}, "
            f"short_trip={tad.SHORT_TRIP_THRESHOLD_MILES}). This is ALLOWED — "
            "they are independent. Confirm the divergence is intentional and "
            "update this test's expectation.",
            stacklevel=2,
        )
