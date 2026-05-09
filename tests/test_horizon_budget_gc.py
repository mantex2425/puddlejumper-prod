"""Tests for the Horizon Budget GC gate in decisions/logger.py.

Phase 2c.2 follow-up sprint — establishes pytest coverage for the
TAD anchor snowball killer. The gate runs in Python after the
prev_row SELECT in log_decision; if either elapsed time or elapsed
odometer exceeds the projected horizon (remaining anchor T&D + new
pickup T&D, * 1.25), prev_row is set to None and the new offer falls
through to idle anchoring.

Test pattern: mock cursor with fetchone returning the prev_row dict
(or the decision_log_id INSERT result, depending on call order).
We don't test the SQL — that's integration's job. We test the
Python-layer gate predicate by capturing the offer_history INSERT
params tuple and asserting on the expected_pickup_distance binding.

  - When the gate KEEPs the anchor, expected_pickup_distance reflects
    a STACKED computation off the prev anchor's expected_dropoff_distance.
  - When the gate GCs the anchor, expected_pickup_distance reflects
    an IDLE computation off current_odometer.

The cleanest discriminator: in IDLE mode, expected_pickup_distance ==
current_odometer + new_offer.pickup_miles. In STACKED mode, it equals
prev.expected_dropoff_distance + new_offer.pickup_miles. Different
prev anchor distances + carefully chosen current_odometer make these
two outcomes numerically distinguishable.

Test count: 5
  - test_fresh_en_route_within_horizon_keeps_anchor
  - test_stacked_mid_trip_within_horizon_keeps_anchor
  - test_ghosted_pickup_exceeds_time_horizon_gcs_anchor
  - test_declined_queue_old_exceeds_horizon_gcs_anchor
  - test_slow_and_stuck_time_blown_distance_ok_gcs_anchor

Pytest floor: 564 -> 569.
"""
from __future__ import annotations

import datetime
from unittest.mock import MagicMock

import pytest

from decisions.logger import log_decision


UTC = datetime.timezone.utc
# L-9 provenance: production code in decisions/logger.py uses
# datetime.datetime.now(datetime.timezone.utc) as now_utc, computed at
# call time. Fixtures must anchor to real wall-clock now and offset via
# timedelta — a hardcoded literal goes stale relative to production's
# clock and trips tad.compute_offer_expectations' orphan-check.
# Convention matches tests/test_offer_history_anchors.py.
NOW = datetime.datetime.now(UTC)


def _ep(**overrides):
    """Minimal ep dict for a new offer arriving at NOW.

    cumulative_miles=1000.0 is the driver's current odometer. The new
    offer is a 5mi / 15min pickup + 10mi / 20min trip — typical Houston.
    """
    base = {
        "fare": 12.0,
        "trip_miles": 10.0, "trip_min": 20.0,
        "pickup_min": 15.0, "pickup_miles": 5.0,
        "yolo_swap_suspected": False,
        "d_lat": 29.51, "d_lng": -95.52,
        "p_lat": 29.50, "p_lng": -95.51,
        "current_lat": 29.49, "current_lng": -95.50,
        "gps_age_sec": 1.5,
        "market_id": "M1", "market_name": "Houston",
        "towards_active": False,
        "towards_target_lat": None, "towards_target_lng": None,
        "towards_market_id": None,
        "is_puddle_jump": True,
        "towards_backtrack_tolerance": 3.0,
        "ocr_confidence": None,
        "pickup_address": "123 Main St",
        "dropoff_address": "456 Oak Ave",
        "ride_type": "uberx",
        "is_surge": False, "is_priority": False, "is_reserve": False,
        "mode_name": "PUDDLE_JUMP",
        "_raw_trace": {},
        "_arc_band_trace": None,
        "cumulative_miles": 1000.0,
    }
    base.update(overrides)
    return base


def _result_stub():
    return {
        "verdict": "ACCEPT",
        "reason": "good rate",
        "hourlyRate": 25.0,
        "dollarsPerMile": 1.50,
    }


def _prev_row(
    *,
    oh_id=99,
    created_at_offset_min=0,
    pickup_minutes=10,
    pickup_miles=3.0,
    trip_minutes=15,
    trip_miles=8.0,
    miles_at_offer_receipt=950.0,
    actual_pickup_at=None,
    expected_dropoff_distance=961.0,
):
    """Build a prev_row dict matching the SELECT projection.

    created_at_offset_min: minutes BEFORE NOW (positive = older).
    """
    created_at = NOW - datetime.timedelta(minutes=created_at_offset_min)
    return {
        "oh_id": oh_id,
        "created_at": created_at,
        "pickup_lat": 29.40, "pickup_lng": -95.40,
        "dropoff_lat": 29.45, "dropoff_lng": -95.45,
        "expected_dropoff_arrival_time": NOW + datetime.timedelta(minutes=10),
        "expected_dropoff_distance": expected_dropoff_distance,
        "pickup_minutes": pickup_minutes,
        "pickup_miles": pickup_miles,
        "trip_minutes": trip_minutes,
        "trip_miles": trip_miles,
        "miles_at_offer_receipt": miles_at_offer_receipt,
        "actual_pickup_at": actual_pickup_at,
    }


def _make_cursor(prev_row_dict, decision_log_id=42):
    """Mock cursor that returns decision_log_id first, then prev_row.

    Call order in log_decision:
      1. INSERT INTO decision_log RETURNING id   -> {"id": decision_log_id}
      2. SELECT FROM offer_history (prev_row)    -> prev_row_dict or None
      3. INSERT INTO offer_history (no fetch)
    """
    cur = MagicMock()
    cur.fetchone.side_effect = [{"id": decision_log_id}, prev_row_dict]
    return cur


def _capture_oh_insert_params(cur):
    """Find the offer_history INSERT call and return its params tuple."""
    for call in cur.execute.call_args_list:
        args = call.args
        if len(args) >= 2 and "INSERT INTO app_private.offer_history" in args[0]:
            return args[1]
    raise AssertionError("offer_history INSERT not found in cursor calls")


# Column positions in the offer_history INSERT params (from logger.py:218+).
# Counted from the column list in the function. The one we care about:
#   expected_pickup_distance — index for STACKED vs IDLE discrimination.
# If pytest fails on these assertions with values that "look almost right
# but in the wrong place," update this index.
EXPECTED_PICKUP_DIST_IDX = 35


def _idle_expected_pickup_distance(ep):
    """In idle mode: expected_pickup_distance = current_odometer + new_pickup_miles."""
    return float(ep["cumulative_miles"]) + float(ep["pickup_miles"])


def _stacked_expected_pickup_distance(prev_row, ep):
    """In stacked mode: expected_pickup_distance = prev.expected_dropoff_distance + new_pickup_miles."""
    return float(prev_row["expected_dropoff_distance"]) + float(ep["pickup_miles"])


# ============================================================================
# Tests
# ============================================================================


def test_fresh_en_route_within_horizon_keeps_anchor():
    """Prev offer 5 min old, never picked up. New offer arrives. Within horizon, KEEP."""
    prev = _prev_row(
        created_at_offset_min=5,
        pickup_minutes=10, pickup_miles=3.0,
        trip_minutes=15, trip_miles=8.0,
        miles_at_offer_receipt=998.0,
        actual_pickup_at=None,
    )
    # elapsed: 5 min, 2 mi
    # rem (not picked up): (10+15)=25 min, (3+8)=11 mi
    # horizon: (25+15)*1.25 = 50 min, (11+5)*1.25 = 20 mi
    # 5 < 50 AND 2 < 20: KEEP

    cur = _make_cursor(prev)
    conn = MagicMock()
    ep = _ep()

    log_decision(cur, conn, "driver_X", {}, ep, _result_stub())

    params = _capture_oh_insert_params(cur)
    expected_stacked = _stacked_expected_pickup_distance(prev, ep)
    assert abs(params[EXPECTED_PICKUP_DIST_IDX] - expected_stacked) < 0.01, (
        f"Expected STACKED ({expected_stacked}), got {params[EXPECTED_PICKUP_DIST_IDX]}"
    )


def test_stacked_mid_trip_within_horizon_keeps_anchor():
    """Prev offer picked up 10 min ago, dropoff hasn't fired. New offer arrives. KEEP."""
    prev = _prev_row(
        created_at_offset_min=15,
        pickup_minutes=10, pickup_miles=3.0,
        trip_minutes=20, trip_miles=10.0,
        miles_at_offer_receipt=985.0,
        actual_pickup_at=NOW - datetime.timedelta(minutes=10),
    )
    # elapsed: 15 min, 15 mi
    # rem (picked up, dropoff leg only): 20 min, 10 mi
    # horizon: (20+15)*1.25 = 43.75 min, (10+5)*1.25 = 18.75 mi
    # 15 < 43.75 AND 15 < 18.75: KEEP

    cur = _make_cursor(prev)
    conn = MagicMock()
    ep = _ep()

    log_decision(cur, conn, "driver_X", {}, ep, _result_stub())

    params = _capture_oh_insert_params(cur)
    expected_stacked = _stacked_expected_pickup_distance(prev, ep)
    assert abs(params[EXPECTED_PICKUP_DIST_IDX] - expected_stacked) < 0.01, (
        f"Expected STACKED ({expected_stacked}), got {params[EXPECTED_PICKUP_DIST_IDX]}"
    )


def test_ghosted_pickup_exceeds_time_horizon_gcs_anchor():
    """Prev offer picked up 90 min ago, dropoff never fired. Ghosted. GC."""
    prev = _prev_row(
        created_at_offset_min=120,
        pickup_minutes=10, pickup_miles=3.0,
        trip_minutes=15, trip_miles=8.0,
        miles_at_offer_receipt=950.0,
        actual_pickup_at=NOW - datetime.timedelta(minutes=90),
    )
    # elapsed: 120 min, 50 mi
    # rem (picked up, dropoff only): 15 min, 8 mi
    # horizon: (15+15)*1.25 = 37.5 min, (8+5)*1.25 = 16.25 mi
    # 120 > 37.5 OR 50 > 16.25: GC (both axes blown)

    cur = _make_cursor(prev)
    conn = MagicMock()
    ep = _ep()

    log_decision(cur, conn, "driver_X", {}, ep, _result_stub())

    params = _capture_oh_insert_params(cur)
    expected_idle = _idle_expected_pickup_distance(ep)
    assert abs(params[EXPECTED_PICKUP_DIST_IDX] - expected_idle) < 0.01, (
        f"Expected IDLE ({expected_idle}), got {params[EXPECTED_PICKUP_DIST_IDX]}"
    )


def test_declined_queue_old_exceeds_horizon_gcs_anchor():
    """Prev offer received 60 min ago, never picked up (declined or matched-not-awarded). GC."""
    prev = _prev_row(
        created_at_offset_min=60,
        pickup_minutes=10, pickup_miles=3.0,
        trip_minutes=15, trip_miles=8.0,
        miles_at_offer_receipt=970.0,
        actual_pickup_at=None,
    )
    # elapsed: 60 min, 30 mi
    # rem (not picked up, full T&D): (10+15)=25 min, (3+8)=11 mi
    # horizon: (25+15)*1.25 = 50 min, (11+5)*1.25 = 20 mi
    # 60 > 50 OR 30 > 20: GC

    cur = _make_cursor(prev)
    conn = MagicMock()
    ep = _ep()

    log_decision(cur, conn, "driver_X", {}, ep, _result_stub())

    params = _capture_oh_insert_params(cur)
    expected_idle = _idle_expected_pickup_distance(ep)
    assert abs(params[EXPECTED_PICKUP_DIST_IDX] - expected_idle) < 0.01, (
        f"Expected IDLE ({expected_idle}), got {params[EXPECTED_PICKUP_DIST_IDX]}"
    )


def test_slow_and_stuck_time_blown_distance_ok_gcs_anchor():
    """Prev offer parked 90 min ago, low odometer delta. Time blown, distance fine. OR semantics: GC."""
    prev = _prev_row(
        created_at_offset_min=90,
        pickup_minutes=10, pickup_miles=3.0,
        trip_minutes=15, trip_miles=8.0,
        miles_at_offer_receipt=999.0,
        actual_pickup_at=None,
    )
    # elapsed: 90 min, 1 mi
    # rem: 25 min, 11 mi
    # horizon: 50 min, 20 mi
    # 90 > 50 (TIME blown) OR 1 > 20 (false): GC by time-only.
    # This proves OR semantics — AND would have KEPT this case.

    cur = _make_cursor(prev)
    conn = MagicMock()
    ep = _ep()

    log_decision(cur, conn, "driver_X", {}, ep, _result_stub())

    params = _capture_oh_insert_params(cur)
    expected_idle = _idle_expected_pickup_distance(ep)
    assert abs(params[EXPECTED_PICKUP_DIST_IDX] - expected_idle) < 0.01, (
        f"Expected IDLE (slow-and-stuck OR-semantics proof), got {params[EXPECTED_PICKUP_DIST_IDX]}"
    )
