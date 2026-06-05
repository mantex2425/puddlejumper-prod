"""test_coerce_odometer.py

Pure-function unit test for `_coerce_odometer` (driver_heartbeat.py).

FINDING §5.7 / §7 Step 4 (2026-06-05). The load-bearing assertion is the
Gemini-ratified NULL-not-zero guard: a missing or non-numeric hardware
odometer payload MUST coerce to None, NEVER to 0 — because a fabricated 0
would produce a garbage expected_odometer in the band consumer (Step 6).

§XIV.J: this exercises NO cursor and touches NO database, so it is correctly
MagicMock-free / db_cur-free. Real-PG fixtures are for cursor-boundary code;
this is pure logic.

Provenance (L-9): inputs are the boundary classes the accessor must defend
against — None (missing payload), non-numeric strings/objects (malformed
payload), and genuine numeric readings including 0.0 and negatives (which
must pass through unchanged, since the accessor is a coercion point, not a
validity gate).
"""

import math

from driver_heartbeat import _coerce_odometer


# ── The load-bearing guard: NULL-not-zero ────────────────────────────────────

def test_none_coerces_to_none_not_zero():
    """A missing odometer payload MUST be None, never 0.

    This is THE assertion the whole accessor exists for. The negative
    assertion (`is not 0`, `!= 0`) makes the NULL-not-zero contract a
    permanent regression: any future change that fabricates a 0 fails here.
    """
    result = _coerce_odometer(None)
    assert result is None
    # Explicit NULL-not-zero: None must never have been coerced to 0/0.0.
    assert result is not 0       # noqa: F632  (identity check is intentional)
    assert result != 0
    assert result != 0.0


# ── Non-numeric payloads fail closed to None ─────────────────────────────────

def test_non_numeric_string_coerces_to_none():
    assert _coerce_odometer("abc") is None


def test_empty_string_coerces_to_none():
    assert _coerce_odometer("") is None


def test_dict_coerces_to_none():
    # A malformed payload that float() cannot consume (TypeError) → None.
    assert _coerce_odometer({}) is None


def test_list_coerces_to_none():
    assert _coerce_odometer([1, 2, 3]) is None


# ── Genuine numeric readings pass through unchanged ──────────────────────────

def test_positive_float_passes_through():
    assert _coerce_odometer(12.5) == 12.5


def test_zero_float_passes_through():
    """A genuine 0.0 reading is a valid odometer value and passes through.

    This is the deliberate counterpart to the NULL-not-zero guard: we never
    FABRICATE a 0, but a real 0.0 from the sensor is honest data and must be
    preserved. The accessor distinguishes 'missing' (None) from 'zero'
    (0.0); they are different facts.
    """
    result = _coerce_odometer(0.0)
    assert result == 0.0
    assert result is not None


def test_zero_int_passes_through():
    result = _coerce_odometer(0)
    assert result == 0.0
    assert result is not None
    assert isinstance(result, float)


def test_negative_float_passes_through():
    """Negative readings pass through (audit honesty). Validity is the
    band's call in Step 6, not the accessor's."""
    assert _coerce_odometer(-3.2) == -3.2


def test_numeric_string_coerces_to_float():
    assert _coerce_odometer("12.5") == 12.5


def test_integer_coerces_to_float():
    result = _coerce_odometer(42)
    assert result == 42.0
    assert isinstance(result, float)


# ── Type guarantee ───────────────────────────────────────────────────────────

def test_return_is_float_or_none():
    """Every return is either a float or None — never a str, int, or other."""
    for raw in (None, "abc", {}, [], 0, 0.0, -3.2, "12.5", 42, 99.9):
        result = _coerce_odometer(raw)
        assert result is None or isinstance(result, float)


def test_nan_string_behavior_documented():
    """float('nan') is technically producible from the string 'nan'.

    Documenting the boundary: 'nan' coerces to a float NaN (not None),
    because float('nan') does not raise. This is acceptable for Step 4 —
    the accessor's job is type coercion, not NaN-filtering. If NaN proves
    to be a real production hazard, the band (Step 6) is where it is
    rejected, consistent with 'accessor coerces, band validates'.
    """
    result = _coerce_odometer("nan")
    assert result is not None
    assert isinstance(result, float)
    assert math.isnan(result)
