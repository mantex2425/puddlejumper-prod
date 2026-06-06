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


# §5.5 lost-mode trigger (fix #2, 2026-06-06): on the idle receipt path
# (prev_offer None + odometer present) log_decision now issues two extra reads
# before the offer_history INSERT — compute_effective_last_move's driver_trip_state
# SELECT (fetchone) and _get_alive_unpicked_offer_ids' SELECT (fetchall). These
# mocks are taught those returns so the idle path runs cleanly to "not lost-mode →
# idle compute" (these tests verify param-binding logic, not the predicate boundary
# — the boundary is covered by the live-PG tests in test_lost_mode_deferral_trigger).
# A NULL-ish driver_trip_state row + empty alive-set = "idle, no peer".
_DTS_ROW = {"last_odometer_move_at": None, "prior_cum": None}


def _mock_cursor_returning(decision_log_id=42):
    cur = MagicMock()
    # fetchone order: decision_log id, prev_offer (None=idle), driver_trip_state row.
    cur.fetchone.side_effect = [{"id": decision_log_id}, None, _DTS_ROW]
    cur.fetchall.return_value = []   # no alive-unpicked peer → not lost-mode
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


# =============================================================================
# Phase 2c.2 Item 3b.W: expected_* anchor bindings
# =============================================================================

def _mock_cursor_with_prev(decision_log_id=42, prev_row=None):
    """Cursor mock that returns decision_log_id on first fetchone(),
    prev_row on second, and a NULL-ish driver_trip_state row on the third
    (the §5.5 idle-path read; unused on the stacked path where prev is non-None
    and the trigger is skipped).

    Use prev_row=None for idle case (no prior offer in DB).
    Use prev_row={...} for stacked case.
    """
    cur = MagicMock()
    cur.fetchone.side_effect = [{"id": decision_log_id}, prev_row, _DTS_ROW]
    cur.fetchall.return_value = []   # §5.5: no alive-unpicked peer → not lost-mode
    return cur


class TestExpectedAnchorBindings:
    """Verify decisions/logger.py log_decision() binds the 4 expected_* anchors.

    Item 3b.W (2026-05-08): chained from prior offer's dropoff anchors when in
    flight, idle (now-relative) when no prior. Bound NULL when cumulative_miles
    is None or compute_offer_expectations returns None.
    """

    def test_idle_case_binds_anchors_relative_to_now(self):
        """No prior offer -> expected_pickup_eta ~= now + pickup_min minutes."""
        import datetime as dt
        from decisions.logger import log_decision

        cur = _mock_cursor_with_prev(decision_log_id=42, prev_row=None)
        conn = MagicMock()
        ep = _ep_with_anchors(
            cumulative_miles=100.0,
            pickup_min=6,
            trip_min=15,
            pickup_miles=2.5,
            trip_miles=8.0,
        )

        before = dt.datetime.now(dt.timezone.utc)
        log_decision(cur, conn, "drv-1", {}, ep, _result_stub())

        sql, params = _find_offer_history_insert(cur)
        assert "expected_pickup_arrival_time" in sql
        assert "expected_pickup_distance" in sql
        assert "expected_dropoff_arrival_time" in sql
        assert "expected_dropoff_distance" in sql

        # The 4 anchors are the last 4 entries in the params tuple.
        expected_pickup_eta = params[-4]
        expected_pickup_dist = params[-3]
        expected_dropoff_eta = params[-2]
        expected_dropoff_dist = params[-1]

        assert isinstance(expected_pickup_eta, dt.datetime)
        assert expected_pickup_eta.tzinfo is not None

        # Idle case: pickup_eta ~= now + 6 minutes (within 5 sec tolerance).
        delta_pickup_s = (expected_pickup_eta - before).total_seconds()
        assert 360 - 5 <= delta_pickup_s <= 360 + 5

        # Distance: 100 + 2.5
        assert expected_pickup_dist == 102.5

        # Dropoff: pickup + 15 minutes; distance + 8.0.
        delta_dropoff_s = (expected_dropoff_eta - expected_pickup_eta).total_seconds()
        assert delta_dropoff_s == 900.0
        assert expected_dropoff_dist == 110.5

    def test_stacked_case_chains_from_prev_dropoff(self):
        """Prior dropoff_eta in future -> new pickup_eta = prev_dropoff_eta + pickup_min."""
        import datetime as dt
        from decisions.logger import log_decision

        prev_dropoff_eta = (
            dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=30)
        )
        prev_dropoff_dist = 50.0
        prev_row = {
            "oh_id": 99,
            "created_at": dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=20),
            "pickup_lat": 29.5, "pickup_lng": -95.5,
            "dropoff_lat": 29.6, "dropoff_lng": -95.6,
            "expected_dropoff_arrival_time": prev_dropoff_eta,
            "expected_dropoff_distance": prev_dropoff_dist,
        }

        cur = _mock_cursor_with_prev(decision_log_id=42, prev_row=prev_row)
        conn = MagicMock()
        ep = _ep_with_anchors(
            cumulative_miles=45.0,
            pickup_min=6,
            trip_min=15,
            pickup_miles=2.5,
            trip_miles=8.0,
        )

        log_decision(cur, conn, "drv-1", {}, ep, _result_stub())

        sql, params = _find_offer_history_insert(cur)
        expected_pickup_eta = params[-4]
        expected_pickup_dist = params[-3]
        expected_dropoff_eta = params[-2]
        expected_dropoff_dist = params[-1]

        # Stacked: pickup anchored on prev dropoff.
        assert expected_pickup_eta == prev_dropoff_eta + dt.timedelta(minutes=6)
        assert expected_pickup_dist == prev_dropoff_dist + 2.5  # 52.5

        # Dropoff: pickup + trip.
        assert expected_dropoff_eta == expected_pickup_eta + dt.timedelta(minutes=15)
        assert expected_dropoff_dist == expected_pickup_dist + 8.0  # 60.5

    def test_stacked_orphaned_falls_back_to_idle(self):
        """Prior dropoff_eta in past -> fall back to idle anchors (now-relative)."""
        import datetime as dt
        from decisions.logger import log_decision

        prev_dropoff_eta = (
            dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=30)
        )
        prev_row = {
            "oh_id": 99,
            "created_at": dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2),
            "pickup_lat": 29.5, "pickup_lng": -95.5,
            "dropoff_lat": 29.6, "dropoff_lng": -95.6,
            "expected_dropoff_arrival_time": prev_dropoff_eta,
            "expected_dropoff_distance": 50.0,
        }

        cur = _mock_cursor_with_prev(decision_log_id=42, prev_row=prev_row)
        conn = MagicMock()
        ep = _ep_with_anchors(
            cumulative_miles=100.0,
            pickup_min=6,
            trip_min=15,
            pickup_miles=2.5,
            trip_miles=8.0,
        )

        before = dt.datetime.now(dt.timezone.utc)
        log_decision(cur, conn, "drv-1", {}, ep, _result_stub())

        sql, params = _find_offer_history_insert(cur)
        expected_pickup_eta = params[-4]
        expected_pickup_dist = params[-3]

        # Should fall back to idle, NOT chain from orphaned prev (50.0 base).
        delta_pickup_s = (expected_pickup_eta - before).total_seconds()
        assert 0 < delta_pickup_s < 7 * 60
        assert expected_pickup_dist == 102.5  # 100 + 2.5, NOT 52.5

    def test_missing_pickup_min_binds_null(self):
        """ep['pickup_min']=None -> compute returns None -> all 4 anchors NULL."""
        from decisions.logger import log_decision

        cur = _mock_cursor_with_prev(decision_log_id=42, prev_row=None)
        conn = MagicMock()
        ep = _ep_with_anchors(
            cumulative_miles=100.0,
            pickup_min=None,
            trip_min=15,
            pickup_miles=2.5,
            trip_miles=8.0,
        )

        log_decision(cur, conn, "drv-1", {}, ep, _result_stub())

        sql, params = _find_offer_history_insert(cur)
        assert "expected_pickup_arrival_time" in sql
        assert params[-4] is None
        assert params[-3] is None
        assert params[-2] is None
        assert params[-1] is None

    def test_missing_pickup_miles_binds_null(self):
        """ep['pickup_miles']=None -> compute returns None -> all 4 NULL."""
        from decisions.logger import log_decision

        cur = _mock_cursor_with_prev(decision_log_id=42, prev_row=None)
        conn = MagicMock()
        ep = _ep_with_anchors(
            cumulative_miles=100.0,
            pickup_min=6,
            trip_min=15,
            pickup_miles=None,
            trip_miles=8.0,
        )

        log_decision(cur, conn, "drv-1", {}, ep, _result_stub())

        sql, params = _find_offer_history_insert(cur)
        assert params[-4] is None
        assert params[-3] is None
        assert params[-2] is None
        assert params[-1] is None

    def test_null_cumulative_miles_skips_compute(self):
        """ep['cumulative_miles']=None -> skip compute() entirely -> all 4 NULL.

        Distinct from missing_pickup_min: that path calls compute() which returns
        None. This path short-circuits via the `if cumulative_miles_value is not
        None` guard, so compute() is never called.
        """
        from decisions.logger import log_decision

        cur = _mock_cursor_with_prev(decision_log_id=42, prev_row=None)
        conn = MagicMock()
        ep = _ep_with_anchors(
            cumulative_miles=None,
            pickup_min=6,
            trip_min=15,
            pickup_miles=2.5,
            trip_miles=8.0,
        )

        log_decision(cur, conn, "drv-1", {}, ep, _result_stub())

        sql, params = _find_offer_history_insert(cur)
        assert params[-4] is None
        assert params[-3] is None
        assert params[-2] is None
        assert params[-1] is None

    def test_prev_offer_select_failure_falls_back_to_idle(self):
        """SELECT raises -> log warning, conn.rollback, idle anchors, INSERT proceeds."""
        import datetime as dt
        from decisions.logger import log_decision

        cur = MagicMock()
        select_called = [False]

        def _execute_side_effect(sql, *args, **kwargs):
            # Raise on the PREV_OFFER chaining query only (its distinctive clause),
            # NOT the §5.5 alive-unpicked query (which also reads offer_history oh
            # but lacks this clause) — so this test isolates prev-fetch failure.
            if "expected_dropoff_arrival_time IS NOT NULL" in sql:
                select_called[0] = True
                raise RuntimeError("simulated SELECT failure")
            return None

        cur.execute.side_effect = _execute_side_effect
        # fetchone order: decision_log id, then the §5.5 driver_trip_state read
        # (prev fetchone is never reached — its execute raised above).
        cur.fetchone.side_effect = [{"id": 42}, _DTS_ROW]
        cur.fetchall.return_value = []   # §5.5 alive-unpicked → empty → not lost-mode

        conn = MagicMock()
        ep = _ep_with_anchors(
            cumulative_miles=100.0,
            pickup_min=6,
            trip_min=15,
            pickup_miles=2.5,
            trip_miles=8.0,
        )

        before = dt.datetime.now(dt.timezone.utc)
        log_decision(cur, conn, "drv-1", {}, ep, _result_stub())

        assert select_called[0]
        conn.rollback.assert_called()

        sql, params = _find_offer_history_insert(cur)
        assert sql is not None  # INSERT executed despite SELECT failure
        # Idle anchors (no prev_offer because SELECT failed).
        expected_pickup_eta = params[-4]
        expected_pickup_dist = params[-3]
        delta_pickup_s = (expected_pickup_eta - before).total_seconds()
        assert 0 < delta_pickup_s < 7 * 60
        assert expected_pickup_dist == 102.5

    def test_compute_raises_binds_null(self):
        """compute_offer_expectations raises unexpectedly -> all 4 NULL, INSERT proceeds."""
        from unittest.mock import patch
        from decisions.logger import log_decision

        cur = _mock_cursor_with_prev(decision_log_id=42, prev_row=None)
        conn = MagicMock()
        ep = _ep_with_anchors(
            cumulative_miles=100.0,
            pickup_min=6,
            trip_min=15,
            pickup_miles=2.5,
            trip_miles=8.0,
        )

        with patch(
            "decisions.logger.compute_offer_expectations",
            side_effect=RuntimeError("simulated compute failure"),
        ):
            log_decision(cur, conn, "drv-1", {}, ep, _result_stub())

        sql, params = _find_offer_history_insert(cur)
        assert sql is not None
        assert params[-4] is None
        assert params[-3] is None
        assert params[-2] is None
        assert params[-1] is None
