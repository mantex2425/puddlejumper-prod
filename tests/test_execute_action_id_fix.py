"""Unit tests for the Alpha-Patch ID realignment in _execute_action.

The Alpha-Patch (2026-05-08) fixes a long-standing identity crisis where
_execute_action treated action.offer_id as decision_log.id when in fact
Offer.offer_id is offer_history.id (per driver_queue._project_offers).

Six SQL statements were realigned to translate at the schema boundary:

  FirePickup branch:
    1. UPDATE pickup_market_signals (subquery via offer_history.id)
    2. UPDATE offer_history          (WHERE id = ...)  + rowcount guard
    3. INSERT community_offers       (subquery via offer_history.id)

  FireDropoff branch:
    4. UPDATE pickup_market_signals (subquery via offer_history.id)
    5. UPDATE offer_history          (WHERE id = ...)  + rowcount guard
    6. INSERT community_offers       (subquery via offer_history.id)

These tests verify each SQL statement contains the expected realignment
markers and that the rowcount guard returns (False, fire_*_zero_rows)
when the offer_history UPDATE writes zero rows.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from driver_heartbeat import _execute_action
from dispatch import FirePickup, FireDropoff


# ============================================================================
# Helpers
# ============================================================================


class _FakeCluster:
    median_lat = 29.7604
    median_lng = -95.3698


def _setup_cur_and_queue():
    """Build a mock cursor and queue with sensible defaults for _execute_action."""
    cur = MagicMock()
    # Default: every UPDATE/INSERT writes 1 row (success path).
    cur.rowcount = 1
    conn = MagicMock()
    queue = MagicMock()
    return cur, conn, queue


def _executed_sql(cur):
    """Extract all SQL statements executed in order."""
    return [c.args[0] if c.args else "" for c in cur.execute.call_args_list]


# ============================================================================
# TestFirePickupSqlRealignment
# ============================================================================


class TestFirePickupSqlRealignment:
    """FirePickup branch: 3 SQL statements + 1 write_nailed_position call."""

    def test_pms_update_uses_subquery_on_offer_history_id(self):
        """pickup_market_signals UPDATE must lookup decision_log_id via oh.id."""
        cur, conn, queue = _setup_cur_and_queue()
        action = FirePickup(offer_id="7771")
        executed, err = _execute_action(
            action, cur, conn, "driver-x", queue,
            cluster=_FakeCluster(), cumulative_miles=145.5,
        )
        assert err is None
        assert executed is True
        sqls = _executed_sql(cur)
        # First UPDATE should target pms via subquery
        pms_sql = next(s for s in sqls if "pickup_market_signals" in s)
        assert "SELECT decision_log_id FROM app_private.offer_history" in pms_sql
        assert "WHERE id = %s::bigint" in pms_sql

    def test_offer_history_update_uses_id_not_decision_log_id(self):
        """offer_history UPDATE must target oh.id directly."""
        cur, conn, queue = _setup_cur_and_queue()
        action = FirePickup(offer_id="7771")
        _execute_action(action, cur, conn, "driver-x", queue,
                        cluster=_FakeCluster(), cumulative_miles=145.5)
        sqls = _executed_sql(cur)
        oh_sql = next(s for s in sqls
                      if "UPDATE app_private.offer_history" in s
                      and "actual_pickup_lat" in s)
        # Realigned: WHERE id = ..., not WHERE decision_log_id = ...
        assert "WHERE id = %s::bigint" in oh_sql
        # Negative assertion: the broken pattern must be gone.
        assert "WHERE decision_log_id = %s::integer" not in oh_sql

    def test_community_offers_insert_uses_subquery_on_offer_history_id(self):
        """community_offers INSERT must lookup decision_log_id via oh.id."""
        cur, conn, queue = _setup_cur_and_queue()
        action = FirePickup(offer_id="7771")
        _execute_action(action, cur, conn, "driver-x", queue,
                        cluster=_FakeCluster(), cumulative_miles=145.5)
        sqls = _executed_sql(cur)
        co_sql = next(s for s in sqls if "community_offers" in s)
        assert "SELECT decision_log_id FROM app_private.offer_history" in co_sql
        assert "WHERE id = %s::bigint" in co_sql

    def test_rowcount_zero_on_offer_history_returns_failure(self):
        """If offer_history UPDATE writes 0 rows, return failure tuple."""
        cur, conn, queue = _setup_cur_and_queue()

        # Make rowcount return 1 for everything EXCEPT the offer_history UPDATE.
        # We model this by tracking which call we're on and checking the SQL.
        call_count = {"n": 0}
        rowcount_values = []

        def execute_side_effect(sql, *_args):
            call_count["n"] += 1
            # offer_history UPDATE is the 2nd execute() call in FirePickup
            # (after pms UPDATE). Set rowcount=0 only for it.
            if "UPDATE app_private.offer_history" in sql and "actual_pickup_lat" in sql:
                cur.rowcount = 0
            else:
                cur.rowcount = 1
            rowcount_values.append(cur.rowcount)

        cur.execute.side_effect = execute_side_effect

        action = FirePickup(offer_id="9999999")  # nonexistent
        executed, err = _execute_action(
            action, cur, conn, "driver-x", queue,
            cluster=_FakeCluster(), cumulative_miles=145.5,
        )
        assert executed is False
        assert err == "fire_pickup_zero_rows"

    def test_rowcount_zero_on_pms_does_not_fail(self):
        """pms zero-row UPDATE is normal (sparse table); must NOT fail."""
        cur, conn, queue = _setup_cur_and_queue()

        def execute_side_effect(sql, *_args):
            if "pickup_market_signals" in sql:
                cur.rowcount = 0  # pms missing — normal
            else:
                cur.rowcount = 1
        cur.execute.side_effect = execute_side_effect

        action = FirePickup(offer_id="7771")
        executed, err = _execute_action(
            action, cur, conn, "driver-x", queue,
            cluster=_FakeCluster(), cumulative_miles=145.5,
        )
        # pms zero-row is NOT a failure condition.
        assert executed is True
        assert err is None


# ============================================================================
# TestFireDropoffSqlRealignment
# ============================================================================


class TestFireDropoffSqlRealignment:
    """FireDropoff branch: 3 SQL statements + 1 write_nailed_position call."""

    def test_pms_update_uses_subquery_on_offer_history_id(self):
        cur, conn, queue = _setup_cur_and_queue()
        action = FireDropoff(offer_id="7771", outcome=None)
        executed, err = _execute_action(
            action, cur, conn, "driver-x", queue,
            cluster=_FakeCluster(), cumulative_miles=148.0,
        )
        assert err is None
        assert executed is True
        sqls = _executed_sql(cur)
        pms_sql = next(s for s in sqls if "pickup_market_signals" in s)
        assert "SELECT decision_log_id FROM app_private.offer_history" in pms_sql
        assert "WHERE id = %s::bigint" in pms_sql

    def test_offer_history_update_uses_id_not_decision_log_id(self):
        cur, conn, queue = _setup_cur_and_queue()
        action = FireDropoff(offer_id="7771", outcome=None)
        _execute_action(action, cur, conn, "driver-x", queue,
                        cluster=_FakeCluster(), cumulative_miles=148.0)
        sqls = _executed_sql(cur)
        oh_sql = next(s for s in sqls
                      if "UPDATE app_private.offer_history" in s
                      and "actual_dropoff_lat" in s)
        assert "WHERE id = %s::bigint" in oh_sql
        assert "WHERE decision_log_id = %s::integer" not in oh_sql

    def test_community_offers_insert_uses_subquery_on_offer_history_id(self):
        cur, conn, queue = _setup_cur_and_queue()
        action = FireDropoff(offer_id="7771", outcome=None)
        _execute_action(action, cur, conn, "driver-x", queue,
                        cluster=_FakeCluster(), cumulative_miles=148.0)
        sqls = _executed_sql(cur)
        co_sql = next(s for s in sqls if "community_offers" in s)
        assert "SELECT decision_log_id FROM app_private.offer_history" in co_sql
        assert "WHERE id = %s::bigint" in co_sql

    def test_rowcount_zero_on_offer_history_returns_failure(self):
        cur, conn, queue = _setup_cur_and_queue()

        def execute_side_effect(sql, *_args):
            if "UPDATE app_private.offer_history" in sql and "actual_dropoff_lat" in sql:
                cur.rowcount = 0
            else:
                cur.rowcount = 1
        cur.execute.side_effect = execute_side_effect

        action = FireDropoff(offer_id="9999999", outcome=None)
        executed, err = _execute_action(
            action, cur, conn, "driver-x", queue,
            cluster=_FakeCluster(), cumulative_miles=148.0,
        )
        assert executed is False
        assert err == "fire_dropoff_zero_rows"


# ============================================================================
# TestSentinelMarkers — confirm the [α-fix] sentinel landed in source
# ============================================================================


class TestSentinelMarkers:
    """Sanity check that the alpha-patch markers exist in driver_heartbeat.py."""

    def test_alpha_fix_sentinel_present(self):
        with open("driver_heartbeat.py") as f:
            content = f.read()
        # At least one [α-fix] sentinel must be present in the SQL changes.
        assert "[α-fix]" in content, \
            "Alpha-Patch sentinel missing — patch may not have applied"
