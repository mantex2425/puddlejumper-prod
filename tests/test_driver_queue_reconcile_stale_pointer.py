"""Unit tests for DriverQueue.snapshot()'s active stale-pointer
reconciliation, 2026-05-31.

The pre-existing L-19 invariant (bound_offer_id outside live queue ->
return None for hint) was read-only: it warned and deferred DB
correction to "next bind/unbind." In lost-mode that bind/unbind never
arrives (no FirePickup ever fires once §XVIII demotes everything to
Observation), so the pointer dangles across shifts. See
docs/RECON_LOST_MODE_COLD_START_TRAP_2026-05-31.md and the
fix/stale-current-offer-id-reconciliation brief for the offer 8585 /
8657 forensic record.

The fix: when the invariant trips AND the bound offer is DEFINITIVELY
DEAD per _is_offer_definitively_dead, snapshot() issues an id-guarded
UPDATE to clear current_offer_id. "Definitively dead" requires
POSITIVE evidence from an existing offer_history row — either
actual_dropoff_at IS NOT NULL (terminated) or created_at older than
GC_ABANDONMENT_CEILING_HOURS (abandoned). Absence is NOT death — bind
can outrun offer_history INSERT (test_endpoints seed_offer race), so
a missing row stays transient.

The id-guarded UPDATE (WHERE driver_id = %s AND current_offer_id = %s)
makes the reconciliation safe under concurrent bind: if another
transaction wrote a fresh offer_id between our SELECT and our UPDATE,
the WHERE no-ops instead of clobbering.

Six tests per the brief's §5, as ratified:
  1. test_stale_pointer_terminated_offer_cleared
  2. test_stale_pointer_abandoned_offer_cleared
  3. test_transient_unprojected_live_offer_NOT_cleared (the critical
     race-closure proof)
  4. test_absent_from_offer_history_NOT_cleared (added per user
     clarification — orphan handling DROPPED in v1)
  5. test_8585_regression (pickup fired, dropoff NULL, prior-day —
     replays the originating bug shape)
  6. test_clear_update_is_id_guarded (concurrent fresh bind is
     preserved)
"""
from __future__ import annotations

import datetime
from unittest.mock import MagicMock

import pytest

from driver_queue import DriverQueue, GC_ABANDONMENT_CEILING_HOURS


_DRIVER_ID = "driver-x"
_STALE_OFFER_ID = "8585"
_FRESH_OFFER_ID = "8900"
_NOW = datetime.datetime(2026, 5, 31, 18, 0, 0, tzinfo=datetime.timezone.utc)


def _make_queue():
    """Construct a DriverQueue with a no-op target_spec_builder.

    The builder is required by the constructor but never invoked in
    these tests because we stub _project_offers to return a fixed list
    via cursor mocking (the projection path is exercised in
    test_driver_queue.py, not here).
    """
    return DriverQueue(_DRIVER_ID, target_spec_builder=lambda *a, **kw: None)


def _wire_cursor(cur, *, stale_pointer_row, live_offers, dead_check_row):
    """Configure cur.fetchone/fetchall to respond to the three queries
    snapshot() issues in order:

      1. _project_offers       -> SELECT...FROM offer_history (fetchall)
      2. _select_bound_offer_id -> SELECT current_offer_id (fetchone)
      3. _is_offer_definitively_dead -> SELECT actual_dropoff_at,
                                        created_at (fetchone)

    `stale_pointer_row` is what query 2 returns (dict with
    current_offer_id, or None for unbound driver).
    `live_offers` is what query 1 returns (list of dicts for
    fetchall — driving _project_offers).
    `dead_check_row` is what query 3 returns (dict with
    actual_dropoff_at + created_at, or None for absent).

    The order matters: snapshot() runs the queries in sequence, so
    fetchone/fetchall must return them in order.
    """
    fetchall_returns = [live_offers]
    fetchone_returns = [stale_pointer_row, dead_check_row]

    def fetchall_side():
        return fetchall_returns.pop(0) if fetchall_returns else []

    def fetchone_side():
        return fetchone_returns.pop(0) if fetchone_returns else None

    cur.fetchall.side_effect = fetchall_side
    cur.fetchone.side_effect = fetchone_side


def _captured_update_sql_calls(cur):
    """Return list of (sql, params) tuples for any UPDATE
    driver_trip_state executed against the cursor."""
    calls = []
    for c in cur.execute.call_args_list:
        sql = c.args[0] if c.args else ""
        if "UPDATE app_private.driver_trip_state" in sql:
            params = c.args[1] if len(c.args) > 1 else None
            calls.append((sql, params))
    return calls


# ============================================================================
# TestStalePointerReconciliation
# ============================================================================


class TestStalePointerReconciliation:

    def test_stale_pointer_terminated_offer_cleared(self):
        """Bound offer has actual_dropoff_at set + not in live queue ->
        snapshot reconciles via id-guarded UPDATE."""
        cur = MagicMock()
        _wire_cursor(
            cur,
            stale_pointer_row={"current_offer_id": _STALE_OFFER_ID},
            live_offers=[],  # _project_offers returns empty (stale not in queue)
            dead_check_row={
                "actual_dropoff_at": _NOW - datetime.timedelta(hours=2),
                "created_at": _NOW - datetime.timedelta(hours=3),
            },
        )

        snap = _make_queue().snapshot(cur)

        # In-memory hint self-heals.
        assert snap.bound_offer_id is None

        # DB UPDATE issued, id-guarded to the stale offer.
        updates = _captured_update_sql_calls(cur)
        assert len(updates) == 1, (
            f"expected exactly 1 reconciling UPDATE, got {len(updates)}"
        )
        sql, params = updates[0]
        assert "SET current_offer_id = NULL" in sql
        assert "AND current_offer_id = %s" in sql, (
            "id-guarded WHERE clause missing — concurrent fresh binds "
            "could be clobbered without it"
        )
        assert params == (_DRIVER_ID, _STALE_OFFER_ID)

    def test_stale_pointer_abandoned_offer_cleared(self):
        """Bound offer created >4h ago + not in live queue ->
        reconciles, reason=abandoned."""
        cur = MagicMock()
        # Created 5 hours ago, no dropoff. Past the 4h ceiling.
        old_ts = _NOW - datetime.timedelta(hours=GC_ABANDONMENT_CEILING_HOURS + 1)
        _wire_cursor(
            cur,
            stale_pointer_row={"current_offer_id": _STALE_OFFER_ID},
            live_offers=[],
            dead_check_row={
                "actual_dropoff_at": None,
                "created_at": old_ts,
            },
        )

        snap = _make_queue().snapshot(cur)

        assert snap.bound_offer_id is None
        updates = _captured_update_sql_calls(cur)
        assert len(updates) == 1
        sql, params = updates[0]
        assert "SET current_offer_id = NULL" in sql
        assert params == (_DRIVER_ID, _STALE_OFFER_ID)

    def test_transient_unprojected_live_offer_NOT_cleared(self):
        """The race-closure proof. A bound offer that's recent (within
        4h) AND has no dropoff AND exists in offer_history MUST NOT be
        cleared even when not in the current live-queue projection.

        Models the case the brief flagged as the only real risk:
        snapshot() runs against a transient projection gap (the offer
        IS alive, predicate would normally surface it, but THIS
        snapshot doesn't project it — mid-snapshot, mid-network-blip,
        or in a unit-test mock where _project_offers returns []).
        Without the dead-predicate gate, clearing here would race
        with the next FirePickup attempting to bind the same offer.

        Clock note: this test's `created_at` MUST be computed against
        the live wall-clock, not the module-level _NOW constant. The
        helper uses _now() internally (per ratified design — no
        reference_time threading), so a fixed _NOW would drift past
        the 4h ceiling whenever real UTC moves past _NOW + 4h,
        spuriously tripping the abandonment branch. The other tests
        in this file are robust to clock drift by accident (their
        fixtures are deliberately ≥4h old). This test's "recent"
        semantic requires live-clock anchoring.
        """
        cur = MagicMock()
        # Recent (well within 4h), no dropoff, row exists. Anchored to
        # live wall-clock for deterministic comparison against the
        # helper's internal _now() call.
        real_now = datetime.datetime.now(datetime.timezone.utc)
        _wire_cursor(
            cur,
            stale_pointer_row={"current_offer_id": _STALE_OFFER_ID},
            live_offers=[],
            dead_check_row={
                "actual_dropoff_at": None,
                "created_at": real_now - datetime.timedelta(minutes=10),
            },
        )

        snap = _make_queue().snapshot(cur)

        # In-memory hint still self-heals (existing L-19 behavior).
        assert snap.bound_offer_id is None

        # CRITICAL: NO UPDATE issued. Pointer preserved for the next
        # heartbeat to re-evaluate. This is the race closure.
        updates = _captured_update_sql_calls(cur)
        assert len(updates) == 0, (
            f"Transient unprojected offer was incorrectly cleared. "
            f"Got {len(updates)} UPDATE(s). The dead-predicate gate "
            f"failed and the race re-opened."
        )

    def test_absent_from_offer_history_NOT_cleared(self):
        """Per user clarification 2026-05-31: orphan/absent branch
        DROPPED in v1. Absence-as-death races mid-ingestion (bind()
        can run before offer_history INSERT commits — notably the
        test_endpoints seed_offer flow). Treat absent as transient,
        not as dead. The bound offer stays pointed at the (about-to-
        commit) row; the next snapshot after ingestion sees it
        normally."""
        cur = MagicMock()
        _wire_cursor(
            cur,
            stale_pointer_row={"current_offer_id": _STALE_OFFER_ID},
            live_offers=[],
            dead_check_row=None,  # offer_history has no row for this id
        )

        snap = _make_queue().snapshot(cur)

        # Hint self-heals (still treated as L-19 violation in-memory).
        assert snap.bound_offer_id is None

        # No reconciling UPDATE — absence is transient by design.
        updates = _captured_update_sql_calls(cur)
        assert len(updates) == 0, (
            f"Absent offer was incorrectly cleared. The dropped-"
            f"orphan-branch invariant was violated."
        )

    def test_8585_regression(self):
        """Replay the originating bug shape: offer 8585 had pickup
        fired (actual_pickup_at non-NULL) and dropoff NEVER fired
        (actual_dropoff_at NULL), created on the prior day (well past
        4h ceiling), and was NOT in today's live queue. The 5/31
        shift logged INVARIANT_VIOLATION every heartbeat and never
        cleared — until manually intervened. This test pins the
        fixture so a future regression would surface as a red test."""
        cur = MagicMock()
        # Created ~24h before the test's "now" — definitively past 4h.
        created_at = _NOW - datetime.timedelta(hours=24)
        _wire_cursor(
            cur,
            stale_pointer_row={"current_offer_id": "8585"},
            live_offers=[],
            dead_check_row={
                "actual_dropoff_at": None,  # dropoff NEVER fired (the bug)
                "created_at": created_at,
            },
        )

        snap = _make_queue().snapshot(cur)

        assert snap.bound_offer_id is None
        updates = _captured_update_sql_calls(cur)
        assert len(updates) == 1, (
            "8585's exact shape (pickup fired, dropoff NULL, prior-day) "
            "must reconcile automatically. Manual intervention should "
            "never be needed again."
        )
        sql, params = updates[0]
        assert "SET current_offer_id = NULL" in sql
        assert params == (_DRIVER_ID, "8585")

    def test_clear_update_is_id_guarded(self):
        """The reconciling UPDATE's WHERE must include both driver_id
        AND current_offer_id = %s. The id-guard is the only thing that
        prevents clobbering a concurrent fresh bind:

          - Tx A: snapshot reads current_offer_id = STALE (8585)
          - Tx B: bind() writes current_offer_id = FRESH (8900)
          - Tx A: reconciliation UPDATE fires; id-guard fails to
                  match (current_offer_id is now FRESH, not STALE) ->
                  UPDATE no-ops, preserving Tx B's bind

        Without the id-guard, the UPDATE would set current_offer_id
        = NULL regardless of what bind() wrote, silently undoing
        Tx B's narrative.
        """
        cur = MagicMock()
        _wire_cursor(
            cur,
            stale_pointer_row={"current_offer_id": _STALE_OFFER_ID},
            live_offers=[],
            dead_check_row={
                "actual_dropoff_at": _NOW - datetime.timedelta(hours=1),
                "created_at": _NOW - datetime.timedelta(hours=2),
            },
        )

        _make_queue().snapshot(cur)

        updates = _captured_update_sql_calls(cur)
        assert len(updates) == 1
        sql, params = updates[0]
        # The two-clause WHERE.
        assert "WHERE driver_id = %s" in sql
        assert "AND current_offer_id = %s" in sql
        # Params bind the specific stale id, not a wildcard.
        assert params == (_DRIVER_ID, _STALE_OFFER_ID)
