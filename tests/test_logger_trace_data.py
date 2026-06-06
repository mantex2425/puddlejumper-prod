"""
Smoke tests for decisions/logger.py trace_data persistence.

Asserts that cumulative_miles and gps_age_sec aliases reach the persisted
trace_data JSONB blob. Regression coverage for the Item 8 allowlist fix
(2026-05-09) and the prior gps_age_sec wiring.

Pattern: mock cursor, capture INSERT params, parse trace_data JSON, assert
keys present with expected values. We test the function, not the SQL.
"""
import json
from unittest.mock import MagicMock

import pytest

from decisions.logger import log_decision


def _build_minimal_ep(cumulative_miles=42.5, gps_age_sec=1.2):
    """Minimal ep dict satisfying log_decision's reads. Only fields actually
    read by log_decision need to be populated; others can be None."""
    return {
        "_raw_trace": {"cumulativeMiles": cumulative_miles, "gpsAgeSec": gps_age_sec},
        "_arc_band_trace": None,
        "gps_age_sec": gps_age_sec,
        "cumulative_miles": cumulative_miles,
        "market_id": "TEST_MARKET",
        "fare": 25.0,
        "pickup_min": 5,
        "trip_min": 20,
        "p_lat": 29.7099,
        "p_lng": -95.5455,
        "d_lat": 29.6284,
        "d_lng": -95.6509,
        "mode_name": "FREESTYLE",
        "market_name": None,
        "ocr_confidence": None,
        "current_lat": 29.7100,
        "current_lng": -95.5400,
        "trip_miles": 8.6,
        "pickup_miles": 0.5,
        "towards_market_id": None,
        "towards_target_lat": None,
        "towards_target_lng": None,
    }


def _capture_trace_data(cur_mock):
    """Pull the trace_data JSON string out of any captured execute call.

    log_decision makes multiple cur.execute calls (decision_log INSERT,
    prev_offer SELECT, offer_history INSERT). We don't care which call
    contains trace_data -- just that one of them does, with the expected
    keys. The offer_history INSERT may fail in the mock (non-blocking
    by design); that's acceptable -- trace_data lands in decision_log
    INSERT first.
    """
    assert cur_mock.execute.called, "log_decision did not call cur.execute"
    for call in cur_mock.execute.call_args_list:
        args, _ = call
        if len(args) < 2:
            continue
        params = args[1]
        if not isinstance(params, (tuple, list)):
            continue
        for param in params:
            if isinstance(param, str) and param.startswith("{") and "gps_age_sec" in param:
                return json.loads(param)
    raise AssertionError(
        f"trace_data JSON not found in any execute call. "
        f"Calls made: {len(cur_mock.execute.call_args_list)}"
    )


def test_trace_payload_includes_cumulative_miles():
    """Item 8 (2026-05-09): cumulative_miles must round-trip into trace_data."""
    cur = MagicMock()
    # fetchone order: decision_log id, prev_offer (None=idle), driver_trip_state
    # row (§5.5 fix #2 idle-path read); fetchall=[] → no alive-unpicked peer.
    cur.fetchone.side_effect = [
        {"id": 12345}, None, {"last_odometer_move_at": None, "prior_cum": None}]
    cur.fetchall.return_value = []
    conn = MagicMock()

    ep = _build_minimal_ep(cumulative_miles=42.5)
    log_decision(cur, conn, "test_driver", {}, ep, {"verdict": "ACCEPT"})

    trace = _capture_trace_data(cur)
    assert "cumulative_miles" in trace, (
        f"cumulative_miles missing from trace_data; keys present: {list(trace.keys())}"
    )
    assert trace["cumulative_miles"] == 42.5


def test_trace_payload_includes_gps_age_sec_regression():
    """Regression coverage for the pre-existing gps_age_sec wiring.

    This test exists so that any future refactor of trace_payload assembly
    cannot silently break the gps_age_sec alias the way cumulative_miles
    was missed pre-Item-8.
    """
    cur = MagicMock()
    cur.fetchone.side_effect = [
        {"id": 12345}, None, {"last_odometer_move_at": None, "prior_cum": None}]
    cur.fetchall.return_value = []
    conn = MagicMock()

    ep = _build_minimal_ep(gps_age_sec=1.2)
    log_decision(cur, conn, "test_driver", {}, ep, {"verdict": "ACCEPT"})

    trace = _capture_trace_data(cur)
    assert "gps_age_sec" in trace
    assert trace["gps_age_sec"] == 1.2


def test_trace_payload_handles_none_values():
    """Defensive: None should round-trip cleanly (JSON null) for both aliases."""
    cur = MagicMock()
    cur.fetchone.side_effect = [
        {"id": 12345}, None, {"last_odometer_move_at": None, "prior_cum": None}]
    cur.fetchall.return_value = []
    conn = MagicMock()

    ep = _build_minimal_ep(cumulative_miles=None, gps_age_sec=None)
    log_decision(cur, conn, "test_driver", {}, ep, {"verdict": "ACCEPT"})

    trace = _capture_trace_data(cur)
    assert trace["cumulative_miles"] is None
    assert trace["gps_age_sec"] is None
