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


def _setup_cur_and_queue(existing_actual_pickup_at=None, offer_exists=True):
    """Build a mock cursor and queue with sensible defaults for _execute_action.

    The FirePickup handler does a SELECT for the offer's current
    actual_pickup_at BEFORE any cache writes (Rule XV idempotency guard,
    2026-05-30). This helper mocks cur.fetchone() accordingly:

      offer_exists=True, existing_actual_pickup_at=None  (default)
        → SELECT returns (None,) → "offer exists, not yet fired" → normal path
      offer_exists=True, existing_actual_pickup_at=<ts>
        → SELECT returns (ts,) → "Rule XV catch-up" → skip cache UPDATEs
      offer_exists=False
        → SELECT returns None → "offer not in offer_history" → fire_pickup_zero_rows
    """
    cur = MagicMock()
    # Default: every UPDATE/INSERT writes 1 row (success path).
    cur.rowcount = 1
    if offer_exists:
        cur.fetchone.return_value = {
            'actual_pickup_at': existing_actual_pickup_at,
            'pickup_lat': None,
            'pickup_lng': None,
            'dropoff_lat': None,
            'dropoff_lng': None,
        }
    else:
        cur.fetchone.return_value = None
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

    def test_offer_not_found_via_select_returns_failure(self):
        """Offer absent from offer_history → SELECT returns None → failure.

        Renamed from test_rowcount_zero_on_offer_history_returns_failure
        (2026-05-30): the Rule XV idempotency guard moved the
        offer-existence check from the offer_history UPDATE's rowcount
        to a leading SELECT. Semantics preserved (nonexistent offer →
        (False, "fire_pickup_zero_rows")); detection mechanism changed.
        """
        cur, conn, queue = _setup_cur_and_queue(offer_exists=False)

        action = FirePickup(offer_id="9999999")  # nonexistent
        executed, err = _execute_action(
            action, cur, conn, "driver-x", queue,
            cluster=_FakeCluster(), cumulative_miles=145.5,
        )
        assert executed is False
        assert err == "fire_pickup_zero_rows"

        # No cache UPDATEs should have run — we exited at the leading SELECT.
        sqls = _executed_sql(cur)
        assert not any("UPDATE app_private.offer_history" in s for s in sqls), (
            "FirePickup must not attempt offer_history UPDATE when SELECT "
            "proved the offer doesn't exist"
        )
        assert not any("UPDATE app_private.pickup_market_signals" in s
                       for s in sqls), (
            "FirePickup must not attempt pms UPDATE when SELECT proved the "
            "offer doesn't exist"
        )

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
# TestFirePickupRuleXvIdempotency — Rule XV: Observation Over Narrative
# ============================================================================
#
# When a FirePickupObservation already wrote actual_pickup_at at the true
# pickup location (e.g. while §XVIII lost-mode was active), a subsequent
# FirePickup arriving after lost-mode lifts MUST NOT overwrite that cache
# write with the FirePickup's (potentially wrong-location) coordinates.
# Rule XV: "We would rather have a perfectly populated map and a 'Lost'
# narrative than a 'Found' narrative and a blank map."
#
# These tests pin the behavior introduced 2026-05-30 in response to
# pickup 5 of that drive (offer 8585: Observation fired correctly at the
# true pickup (30.0278, -95.4203) at 13:59:31; FirePickup fired 10min
# later at the wrong location (30.0221, -95.3900) and overwrote
# offer_history.actual_pickup_at with the wrong timestamp and lat/lng).
# See docs/RECON_IMPERIAL_VALLEY_PICKUP5_2026-05-30.md VERDICT section.


class TestFirePickupRuleXvIdempotency:
    """Rule XV: FirePickup must preserve a prior Observation's cache write."""

    def test_catchup_skips_cache_updates(self):
        """FirePickup after Observation skips both pms and offer_history UPDATEs.

        Cache is the Observation's correct write; narrative still binds.
        """
        import datetime
        prior_fire_time = datetime.datetime(
            2026, 5, 30, 13, 59, 31, 615027,
            tzinfo=datetime.timezone.utc,
        )
        cur, conn, queue = _setup_cur_and_queue(
            existing_actual_pickup_at=prior_fire_time,
        )

        action = FirePickup(offer_id="8585")
        executed, err = _execute_action(
            action, cur, conn, "driver-x", queue,
            cluster=_FakeCluster(), cumulative_miles=67.46,
        )

        assert executed is True
        assert err is None

        sqls = _executed_sql(cur)
        # NO cache UPDATEs should have run.
        assert not any("UPDATE app_private.offer_history" in s for s in sqls), (
            "Rule XV violated: FirePickup ran offer_history UPDATE despite "
            "actual_pickup_at being already populated by prior Observation"
        )
        assert not any("UPDATE app_private.pickup_market_signals" in s
                       for s in sqls), (
            "Rule XV violated: FirePickup ran pms UPDATE despite "
            "actual_pickup_at being already populated by prior Observation"
        )
        # No community_offers INSERT either — it derives from pms which
        # we are NOT touching in the catch-up case.
        assert not any("INSERT INTO public.community_offers" in s
                       for s in sqls), (
            "Rule XV violated: FirePickup ran community_offers INSERT in "
            "the Observation-catch-up case"
        )

    def test_catchup_still_binds_narrative(self):
        """FirePickup catch-up MUST bind current_offer_id (queue.bind)."""
        import datetime
        prior_fire_time = datetime.datetime(
            2026, 5, 30, 13, 59, 31,
            tzinfo=datetime.timezone.utc,
        )
        cur, conn, queue = _setup_cur_and_queue(
            existing_actual_pickup_at=prior_fire_time,
        )

        action = FirePickup(offer_id="8585")
        _execute_action(
            action, cur, conn, "driver-x", queue,
            cluster=_FakeCluster(), cumulative_miles=67.46,
        )

        # queue.bind is the narrative bind; catch-up MUST still do it.
        queue.bind.assert_called_once_with("8585", cur)

    def test_normal_path_offer_history_update_has_idempotency_guard(self):
        """Normal-path offer_history UPDATE must include `actual_pickup_at IS NULL`.

        Defense-in-depth: even when the leading SELECT proved
        actual_pickup_at IS NULL, the UPDATE's WHERE keeps the guard so
        a race between SELECT and UPDATE can't corrupt the cache.
        """
        cur, conn, queue = _setup_cur_and_queue()  # default: not yet fired
        action = FirePickup(offer_id="7771")
        _execute_action(
            action, cur, conn, "driver-x", queue,
            cluster=_FakeCluster(), cumulative_miles=145.5,
        )

        sqls = _executed_sql(cur)
        oh_sql = next(s for s in sqls
                      if "UPDATE app_private.offer_history" in s
                      and "actual_pickup_lat" in s)
        # The guard must appear in the WHERE clause.
        assert "actual_pickup_at IS NULL" in oh_sql, (
            "Rule XV idempotency guard missing on offer_history UPDATE"
        )

    def test_normal_path_pms_update_has_idempotency_guard(self):
        """Normal-path pms UPDATE must include `actual_pickup_at IS NULL`."""
        cur, conn, queue = _setup_cur_and_queue()  # default: not yet fired
        action = FirePickup(offer_id="7771")
        _execute_action(
            action, cur, conn, "driver-x", queue,
            cluster=_FakeCluster(), cumulative_miles=145.5,
        )

        sqls = _executed_sql(cur)
        pms_sql = next(s for s in sqls if "pickup_market_signals" in s)
        assert "actual_pickup_at IS NULL" in pms_sql, (
            "Rule XV idempotency guard missing on pms UPDATE"
        )

    def test_normal_path_first_fire_still_runs_all_updates(self):
        """Sanity: normal first-fire path must still run pms + oh + co."""
        cur, conn, queue = _setup_cur_and_queue()  # default: not yet fired
        action = FirePickup(offer_id="7771")
        executed, err = _execute_action(
            action, cur, conn, "driver-x", queue,
            cluster=_FakeCluster(), cumulative_miles=145.5,
        )

        assert executed is True
        assert err is None

        sqls = _executed_sql(cur)
        assert any("UPDATE app_private.pickup_market_signals" in s
                   for s in sqls), "Normal path must run pms UPDATE"
        assert any("UPDATE app_private.offer_history" in s
                   and "actual_pickup_lat" in s for s in sqls), (
            "Normal path must run offer_history UPDATE"
        )
        assert any("INSERT INTO public.community_offers" in s
                   for s in sqls), "Normal path must run community_offers INSERT"


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
