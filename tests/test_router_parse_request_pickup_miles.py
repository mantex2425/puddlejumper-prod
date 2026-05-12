"""Unit tests for parse_request's pickup_miles handling after the
2026-05-12 trust-input-distances patch.

Sprint: pickup_miles truth-flow
Date:   2026-05-12

What changed: parse_request previously substituted pickup_minutes * 0.33
whenever pickupMiles was absent from the /decide payload. That heuristic
fed app_private.decision_engine_v2 and produced approximately-correct
economic decisions while corrupting TAD's pickup-leg distance gate.

After the patch:
  - pickupMiles present and non-None -> float, passed verbatim to engine
  - pickupMiles absent -> None, WARN logged, engine handles via GPS fallback
  - pickupMiles null -> None, same as absent
  - pickupMiles == 0 -> 0.0 (real data, not absence; no WARN)

These tests are scoped to parse_request's pickup_miles field only.
Other fields are exercised by the existing test_decisions_router_offer_id
suite.
"""
import logging
import pytest

from decisions.router import parse_request


# --------------------------------------------------------------------------- #
# Baseline payload — minimum keys parse_request expects to not crash.
# All numeric fields are coerced via float() in the function body.
# --------------------------------------------------------------------------- #
def _base_payload(**overrides):
    """Build a minimal /decide payload. Override any field via kwargs."""
    payload = {
        "fare": 8.53,
        "tripMiles": 6.5,
        "tripMinutes": 20,
        "pickupMinutes": 11,
        "lat": 29.5283,
        "lng": -95.4486,
        "dropoffLat": 29.5631,
        "dropoffLng": -95.2861,
        "marketId": "6a35d28b-8e6c-4d60-94aa-2661e2650863",
    }
    payload.update(overrides)
    return payload


UID = "test_driver_uid"


# --------------------------------------------------------------------------- #
# Case 1: pickupMiles present and valid
# --------------------------------------------------------------------------- #
def test_pickup_miles_present_stored_as_float(caplog):
    """When Android sends pickupMiles, it lands in the params dict as a float."""
    payload = _base_payload(pickupMiles=5.4)
    with caplog.at_level(logging.WARNING):
        result = parse_request(payload, UID)

    assert result["pickup_miles"] == 5.4
    assert isinstance(result["pickup_miles"], float)
    # No WARN about missing pickupMiles
    assert not any(
        "missing pickupMiles" in record.message for record in caplog.records
    )


# --------------------------------------------------------------------------- #
# Case 2: pickupMiles absent (key not in dict)
# --------------------------------------------------------------------------- #
def test_pickup_miles_absent_stored_as_none(caplog):
    """When Android omits pickupMiles, params dict gets None and a WARN fires."""
    payload = _base_payload()
    # Explicitly ensure no pickupMiles key
    payload.pop("pickupMiles", None)

    with caplog.at_level(logging.WARNING):
        result = parse_request(payload, UID)

    assert result["pickup_miles"] is None
    # WARN must fire so we can track APK propagation
    assert any(
        "missing pickupMiles" in record.message for record in caplog.records
    ), "Expected WARN log when pickupMiles is absent"


# --------------------------------------------------------------------------- #
# Case 3: pickupMiles explicitly null (JSON null -> Python None)
# --------------------------------------------------------------------------- #
def test_pickup_miles_explicit_null_stored_as_none(caplog):
    """A null in JSON deserializes to None in Python; same path as absent."""
    payload = _base_payload(pickupMiles=None)

    with caplog.at_level(logging.WARNING):
        result = parse_request(payload, UID)

    assert result["pickup_miles"] is None
    assert any(
        "missing pickupMiles" in record.message for record in caplog.records
    )


# --------------------------------------------------------------------------- #
# Case 4: pickupMiles == 0 — real data, not absence
# --------------------------------------------------------------------------- #
def test_pickup_miles_zero_preserved_no_warn(caplog):
    """A literal 0 from Android is real OCR data (rare but legitimate);
    we preserve the 0.0 and do NOT log the absence WARN. The stored proc
    will then trigger its own GPS fallback via the < 0.1 gate, which is
    the correct downstream behavior for zero-distance pickups."""
    payload = _base_payload(pickupMiles=0)

    with caplog.at_level(logging.WARNING):
        result = parse_request(payload, UID)

    assert result["pickup_miles"] == 0.0
    assert isinstance(result["pickup_miles"], float)
    # Zero is real data, NOT a missing-field case
    assert not any(
        "missing pickupMiles" in record.message for record in caplog.records
    ), "WARN should not fire on legitimate zero"


# --------------------------------------------------------------------------- #
# Case 5: YOLO-swap guard handles None without crashing
# --------------------------------------------------------------------------- #
def test_pickup_miles_none_does_not_crash_yolo_guard():
    """The YOLO-swap suspicion guard at parse_request used to be
    `if pickup_min > 0 and pickup_miles > 0:`. In Python 3, `None > 0`
    raises TypeError. Patch must guard against None before comparison."""
    payload = _base_payload()
    payload.pop("pickupMiles", None)
    # If guard isn't fixed, this call raises TypeError
    result = parse_request(payload, UID)
    assert result["pickup_miles"] is None  # Reached without crashing
