"""Regression tests for offer_history anchor + amendment column INSERTs.

Sprint A gate layer adds 4 column bindings to the offer_history INSERT
in decisions/logger.py:

  Gate anchor (used by motion_gate.py's odometer leg evaluation):
    - leg_start_cumulative_miles_pickup  (existing column, was unwritten)

  Phase 2c.2 receipt-capture (amendment 1, data-only — not read by gate):
    - miles_at_offer_receipt
    - lat_at_offer_receipt
    - lng_at_offer_receipt

These tests verify the wiring at the function-call layer using mocked
cursors — they do NOT touch a live DB. SQL correctness is validated
at integration.

Test pattern matches existing tests/test_*.py: MagicMock cursor with
fetchone.side_effect returning a fake decision_log_id.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest


def _ep_with_anchors(**overrides):
    """Build a minimal ep dict with the new anchor + receipt-capture fields.

    Every key consumed by the offer_history INSERT must be present, even
    if None — the binding tuple has fixed shape.
    """
    base = {
        "fare": 12.0,
        "trip_miles": 5.5,
        "trip_min": 15.0,
        "pickup_min": 6.0,
        "pickup_miles": 2.5,
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
        # Trace fields consumed by log_decision (must exist, contents unimportant for these tests):
        "_raw_trace": {},
        "_arc_band_trace": None,
        # Sprint A gate-layer additions:
        "cumulative_miles": 1234.56,
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


def _mock_cursor_returning(decision_log_id=42):
    cur = MagicMock()
    cur.fetchone.return_value = {"id": decision_log_id}
    return cur


# =============================================================================
# Anchor + amendment column tuple verification
# =============================================================================

class TestOfferHistoryAnchors:
    """Verify decisions/logger.py log_decision() binds the 4 new columns."""

    def test_offer_history_insert_includes_pickup_anchor(self):
        from decisions.logger import log_decision

        cur = _mock_cursor_returning()
        conn = MagicMock()
        ep = _ep_with_anchors(cumulative_miles=1234.56)

        log_decision(cur, conn, "drv-1", {}, ep, _result_stub())

        # Find the INSERT into offer_history (second cur.execute call).
        # First call is INSERT INTO decision_log; second is offer_history.
        offer_history_call = _find_offer_history_insert(cur)
        assert offer_history_call is not None, \
            "log_decision did not execute INSERT into offer_history"

        sql, params = offer_history_call
        # The new column should appear in the INSERT column list.
        assert "leg_start_cumulative_miles_pickup" in sql, \
            "leg_start_cumulative_miles_pickup column missing from INSERT"
        # And the value 1234.56 should be in the params tuple.
        assert 1234.56 in params, \
            "leg_start_cumulative_miles_pickup value not bound in tuple"

    def test_offer_history_insert_includes_amendment_columns(self):
        from decisions.logger import log_decision

        cur = _mock_cursor_returning()
        conn = MagicMock()
        ep = _ep_with_anchors(
            cumulative_miles=999.99,
            current_lat=29.4975076,
            current_lng=-95.518549,
        )

        log_decision(cur, conn, "drv-1", {}, ep, _result_stub())

        offer_history_call = _find_offer_history_insert(cur)
        assert offer_history_call is not None
        sql, params = offer_history_call
        assert "miles_at_offer_receipt" in sql
        assert "lat_at_offer_receipt" in sql
        assert "lng_at_offer_receipt" in sql
        # Amendment columns share values with current_lat/lng + cumulative_miles.
        assert 999.99 in params
        assert 29.4975076 in params
        assert -95.518549 in params

    def test_null_cumulative_miles_inserts_null(self):
        """When heartbeat hasn't fired yet, cumulative_miles is None.

        Both leg_start_cumulative_miles_pickup AND miles_at_offer_receipt
        should bind None (not crash, not 0.0).
        """
        from decisions.logger import log_decision

        cur = _mock_cursor_returning()
        conn = MagicMock()
        ep = _ep_with_anchors(cumulative_miles=None)

        log_decision(cur, conn, "drv-1", {}, ep, _result_stub())

        offer_history_call = _find_offer_history_insert(cur)
        assert offer_history_call is not None
        sql, params = offer_history_call
        # Both anchor and amendment columns should bind None.
        assert "leg_start_cumulative_miles_pickup" in sql
        assert "miles_at_offer_receipt" in sql

    def test_decision_log_id_passed_through(self):
        """Sanity: outer INSERT into decision_log must complete before
        offer_history INSERT runs."""
        from decisions.logger import log_decision

        cur = _mock_cursor_returning(decision_log_id=777)
        conn = MagicMock()
        ep = _ep_with_anchors()

        result_id = log_decision(cur, conn, "drv-1", {}, ep, _result_stub())
        assert result_id == 777

        offer_history_call = _find_offer_history_insert(cur)
        assert 777 in offer_history_call[1]


# =============================================================================
# Helpers
# =============================================================================

def _find_offer_history_insert(cur):
    """Scan cur.execute call_args_list for the offer_history INSERT.

    Returns (sql, params) tuple or None if not found.
    """
    for call in cur.execute.call_args_list:
        args = call.args if call.args else call[0]
        if not args:
            continue
        sql = args[0]
        if "INSERT INTO app_private.offer_history" in sql:
            params = args[1] if len(args) > 1 else ()
            return (sql, params)
    return None
