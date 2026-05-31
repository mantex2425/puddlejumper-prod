"""Unit tests for the §XVIII cold-start bind on FirePickupObservation.

Pins the behavior introduced 2026-05-31 in
docs/RECON_LOST_MODE_COLD_START_TRAP_2026-05-31.md (verdict §11)
and the implementation brief CC BRIEF — fix/lost-mode-cold-start (§5).

The defect: lost-mode latches on any unfired live offer (recon §7),
demoting every FirePickup to FirePickupObservation, which never binds
current_offer_id. Circular. The fix: when an FPO fires AND the
alive-unpicked offer set is exactly {action.offer_id}, the FPO also
binds current_offer_id. Cache writes happen unconditionally either way
(Rule XV: Observation is the primary output).

Test surface mirrors tests/test_execute_action_id_fix.py — mock cursor
+ mock queue + a fake cluster. acquire_lock is patched to a no-op so
the test doesn't touch the transaction-lock module.

Five tests per the brief's §5:
  1. test_coldstart_single_alive_offer_binds — the core happy path
  2. test_coldstart_two_alive_offers_no_bind  — ambiguous, stays Observation-only
  3. test_coldstart_zero_alive_offers_no_crash — edge: empty set, no bind, no crash
  4. test_coldstart_does_not_disturb_cache_writes — caches written regardless
  5. test_coldstart_replay_5_31_density — regression mirroring the 2026-05-31
     density vector (alive_count=1 binds; alive_count=2 doesn't)
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from driver_heartbeat import _execute_action
from dispatch import FirePickupObservation


# ============================================================================
# Helpers
# ============================================================================


class _FakeCluster:
    """Stand-in for diagnostics.cluster — only the centroid fields matter."""
    median_lat = 29.7604
    median_lng = -95.3698


def _setup_cur_and_queue():
    """Build mocks suitable for an FPO call.

    cur: MagicMock. FPO doesn't read fetchone() (the leading SELECT lives
    in the FirePickup handler, not FPO), so no special return wiring.
    Every UPDATE/INSERT writes 1 row.

    queue: MagicMock. queue.bind is a MagicMock method we can assert on.
    queue.driver_id is set so queue.bind's internal cur.execute formatting
    works if anyone unpacks it (they don't here, since queue is mocked).
    """
    cur = MagicMock()
    cur.rowcount = 1
    conn = MagicMock()
    queue = MagicMock()
    queue.driver_id = "driver-x"
    return cur, conn, queue


def _exec_fpo(cur, conn, queue, action, *, alive_unpicked_offer_ids):
    """Invoke the FPO handler with acquire_lock patched out."""
    with patch("driver_heartbeat.acquire_lock") as _lock:
        return _execute_action(
            action, cur, conn, "driver-x", queue,
            cluster=_FakeCluster(),
            cumulative_miles=145.5,
            alive_unpicked_offer_ids=alive_unpicked_offer_ids,
        )


# ============================================================================
# TestColdStartBind — §XVIII cold-start narrative bind on FirePickupObservation
# ============================================================================


class TestColdStartBind:

    def test_coldstart_single_alive_offer_binds(self):
        """FPO fires, alive-unpicked set is exactly {action.offer_id} → bind."""
        cur, conn, queue = _setup_cur_and_queue()
        action = FirePickupObservation(offer_id="8653")

        executed, err = _exec_fpo(
            cur, conn, queue, action,
            alive_unpicked_offer_ids=frozenset({"8653"}),
        )

        assert executed is True
        assert err is None
        # The bind is what breaks the lost-mode trap.
        queue.bind.assert_called_once_with("8653", cur)

    def test_coldstart_two_alive_offers_no_bind(self):
        """Two alive-unpicked offers → ambiguous, no bind."""
        cur, conn, queue = _setup_cur_and_queue()
        action = FirePickupObservation(offer_id="8653")

        executed, err = _exec_fpo(
            cur, conn, queue, action,
            alive_unpicked_offer_ids=frozenset({"8653", "8700"}),
        )

        assert executed is True
        assert err is None
        # Ambiguous queue → narrative MUST stay unbound. This is the
        # 14:22+ vector from the 5/31 replay.
        queue.bind.assert_not_called()

    def test_coldstart_zero_alive_offers_no_crash(self):
        """Empty alive-unpicked set (idempotent redo / race) → no bind, no crash.

        Theoretically shouldn't happen in production: the firing offer
        must be in the alive-unpicked set at the moment of FPO emission.
        But under §XVI.G lock expiration + FPO re-fire after that offer's
        actual_pickup_at already wrote, the set could be empty at the
        bind point. The gate falsifies cleanly; no bind; no exception.
        """
        cur, conn, queue = _setup_cur_and_queue()
        action = FirePickupObservation(offer_id="8653")

        executed, err = _exec_fpo(
            cur, conn, queue, action,
            alive_unpicked_offer_ids=frozenset(),
        )

        assert executed is True
        assert err is None
        queue.bind.assert_not_called()

    def test_coldstart_does_not_disturb_cache_writes(self):
        """Cache writes (pms + offer_history + community_offers) happen
        identically whether or not the cold-start bind fires.

        Rule XV invariant: Observation is the primary cache writer. The
        bind is additive — it must never gate, skip, or alter the cache
        path. We verify by running the FPO twice with identical inputs
        EXCEPT the alive-unpicked set, then comparing the executed SQL
        sequences. They must match modulo ordering of the bind insert.
        """
        action = FirePickupObservation(offer_id="8653")

        # Run A: bind fires (alive == singleton match)
        cur_a, conn_a, queue_a = _setup_cur_and_queue()
        _exec_fpo(
            cur_a, conn_a, queue_a, action,
            alive_unpicked_offer_ids=frozenset({"8653"}),
        )

        # Run B: bind does NOT fire (ambiguous)
        cur_b, conn_b, queue_b = _setup_cur_and_queue()
        _exec_fpo(
            cur_b, conn_b, queue_b, action,
            alive_unpicked_offer_ids=frozenset({"8653", "8700"}),
        )

        # Extract SQL statements executed against cur (queue.bind goes
        # through the queue mock, not cur — separate channel).
        sqls_a = [c.args[0] for c in cur_a.execute.call_args_list if c.args]
        sqls_b = [c.args[0] for c in cur_b.execute.call_args_list if c.args]

        # Cache write set must be present in BOTH runs.
        for label, sqls in (("bind-firing", sqls_a), ("no-bind", sqls_b)):
            assert any("UPDATE app_private.pickup_market_signals" in s
                       for s in sqls), (
                f"{label} run missing pms UPDATE — cache write disturbed"
            )
            assert any("UPDATE app_private.offer_history" in s
                       and "actual_pickup_lat" in s for s in sqls), (
                f"{label} run missing offer_history UPDATE — cache write disturbed"
            )
            assert any("INSERT INTO public.community_offers" in s
                       for s in sqls), (
                f"{label} run missing community_offers INSERT — cache write disturbed"
            )

        # Sanity: bind fired in A, not in B.
        queue_a.bind.assert_called_once_with("8653", cur_a)
        queue_b.bind.assert_not_called()

    def test_coldstart_replay_5_31_density(self):
        """Replay regression: the 2026-05-31 density vector.

        Pickup at 13:14–14:10 CT: alive_unpicked = {8653} → first FPO
        binds 8653; subsequent fires in that window would also bind
        their respective single-alive offers. The trap breaks on the
        first bind.

        Pickup at 14:22+ CT: alive_unpicked = {8653, 8700, ...} → bind
        gate falsifies; FPOs stay Observation-only (correct ambiguous
        behavior under §XVIII).

        This test pins the regression: if either branch flips, the
        5/31 drive would not have been fixed (binds-when-it-shouldn't
        or doesn't-bind-when-it-should), and a future drive would
        repeat the trap.
        """
        # Window 1 (13:14–14:10): single alive offer, FPO binds.
        cur1, conn1, queue1 = _setup_cur_and_queue()
        action1 = FirePickupObservation(offer_id="8653")
        _exec_fpo(
            cur1, conn1, queue1, action1,
            alive_unpicked_offer_ids=frozenset({"8653"}),
        )
        queue1.bind.assert_called_once_with("8653", cur1)

        # Window 2 (14:22+): multiple alive offers, FPO does NOT bind.
        cur2, conn2, queue2 = _setup_cur_and_queue()
        action2 = FirePickupObservation(offer_id="8700")
        _exec_fpo(
            cur2, conn2, queue2, action2,
            alive_unpicked_offer_ids=frozenset({"8700", "8712"}),
        )
        queue2.bind.assert_not_called()
