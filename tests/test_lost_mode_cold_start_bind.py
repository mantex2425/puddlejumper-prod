"""Unit tests for the §XVIII spatial-local bind on FirePickupObservation.

Migrated 2026-06-01 from the original global-singleton gate (2026-05-31
cold-start fix, ratified in docs/RECON_LOST_MODE_COLD_START_TRAP_2026-05-31.md)
to the spatial-local competing-set gate (2026-06-01, docs/FIX_PROPOSAL_
BIND_SPATIAL_LOCAL_2026-06-01.md, Gemini-ratified).

The original gate evaluated:
    if alive_unpicked_offer_ids == frozenset({action.offer_id}):
        bind
which measured AMBIGUITY GLOBALLY ("any other offer on the clipboard"),
withholding the bind whenever earlier-but-cross-town offers existed even
though they scored well below the WAI pickup floor. 2026-06-01 AM drive
recon (RECON_PICKUP_NOBIND_2026-06-01.md, H-A) showed only 1 of 7 pickup
fires bound under the old gate; the other six fired observation-only
because the global gate counted offers that were never spatial candidates.

The new gate evaluates:
    local_competing = pickup_floor_clearers & alive_unpicked_offer_ids
    if local_competing == frozenset({action.offer_id}):
        bind
which measures LOCAL AMBIGUITY (offers actually competing for *this*
piece of asphalt — those WAI scored on the pickup leg and that cleared
the canonical _commits floor). Equality (not membership) preserves the
§XIV.I §5.3 shared-curb fail-closed protection; intersection with
alive_unpicked retires already-picked offers so a WAI-rescored retired
leg can't block a fresh bind (proposal §1.3).

Test surface continues to use a MagicMock cursor + mock queue + fake
cluster for fast unit-level pinning of the gate's algebra. The
real-PG, end-to-end validation lives in tests/test_spatial_local_bind.py
(db_cur fixture, mandated by §XIV.J).

Coverage:
  - test_singleton_floor_clearer_binds                 — the H-A morning case
  - test_shared_curb_two_floor_clearers_no_bind        — §XIV.I §5.3 preserved
  - test_morning_case_solo_clearer_with_stranded_offers_binds — the H-A regression net
  - test_action_in_alive_but_not_in_floor_no_bind      — fail-closed when floor empty for action
  - test_action_in_floor_but_already_picked_no_bind    — §1.3 retirement via intersect
  - test_empty_floor_clearers_no_bind                  — fail-closed when floor empty
  - test_zero_alive_unpicked_no_crash                  — edge: empty alive set
  - test_cache_writes_undisturbed_by_bind_outcome      — Rule XV invariant preserved
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


def _exec_fpo(cur, conn, queue, action, *,
              alive_unpicked_offer_ids,
              pickup_floor_clearers):
    """Invoke the FPO handler with acquire_lock patched out.

    Both gate-axis kwargs are required for clarity: the new gate is a
    function of (pickup_floor_clearers, alive_unpicked_offer_ids) and
    every test should make both explicit.
    """
    with patch("driver_heartbeat.acquire_lock") as _lock:
        return _execute_action(
            action, cur, conn, "driver-x", queue,
            cluster=_FakeCluster(),
            cumulative_miles=145.5,
            alive_unpicked_offer_ids=alive_unpicked_offer_ids,
            pickup_floor_clearers=pickup_floor_clearers,
        )


# ============================================================================
# TestSpatialLocalBind — §XVIII spatial-local narrative bind on FPO
# ============================================================================


class TestSpatialLocalBind:

    def test_singleton_floor_clearer_binds(self):
        """The simplest binding case: one offer cleared the pickup floor,
        same offer is alive-unpicked, FPO fires on that offer.
        local_competing = {X} & {X} = {X} == {X} → bind.
        """
        cur, conn, queue = _setup_cur_and_queue()
        action = FirePickupObservation(offer_id="8653")

        executed, err = _exec_fpo(
            cur, conn, queue, action,
            alive_unpicked_offer_ids=frozenset({"8653"}),
            pickup_floor_clearers=frozenset({"8653"}),
        )

        assert executed is True
        assert err is None
        queue.bind.assert_called_once_with("8653", cur)

    def test_shared_curb_two_floor_clearers_no_bind(self):
        """§XIV.I §5.3 preserved: two offers both clearing pickup floor at
        one cluster → genuine shared-curb ambiguity → no bind.
        local_competing = {X,Y} & {X,Y} = {X,Y} != {X} → withhold.
        """
        cur, conn, queue = _setup_cur_and_queue()
        action = FirePickupObservation(offer_id="8653")

        executed, err = _exec_fpo(
            cur, conn, queue, action,
            alive_unpicked_offer_ids=frozenset({"8653", "8700"}),
            pickup_floor_clearers=frozenset({"8653", "8700"}),
        )

        assert executed is True
        assert err is None
        queue.bind.assert_not_called()

    def test_morning_case_solo_clearer_with_stranded_offers_binds(self):
        """The 2026-06-01 morning regression net (H-A):

        The driver has three offers alive-unpicked (8736, 8737, 8739)
        but is geographically at 8739's pickup zone. WAI scores 8739's
        pickup leg at 0.628 (clears 0.55 lost floor); 8736 and 8737 are
        miles away and score 0.186 each (well below floor).

        OLD global-singleton gate: alive_unpicked={8736,8737,8739} !=
        {8739} → withhold. This was the bug — the bind was withheld
        despite 8739 being the sole spatial candidate.

        NEW spatial-local gate: pickup_floor_clearers={8739};
        local_competing = {8739} & {8736,8737,8739} = {8739} == {8739}
        → bind. The stranded earlier offers don't block the bind
        because they were never competing for *this* piece of asphalt.
        """
        cur, conn, queue = _setup_cur_and_queue()
        action = FirePickupObservation(offer_id="8739")

        executed, err = _exec_fpo(
            cur, conn, queue, action,
            alive_unpicked_offer_ids=frozenset({"8736", "8737", "8739"}),
            pickup_floor_clearers=frozenset({"8739"}),
        )

        assert executed is True
        assert err is None
        # The whole point of the fix: this case now binds.
        queue.bind.assert_called_once_with("8739", cur)

    def test_action_in_alive_but_not_in_floor_no_bind(self):
        """Fail-closed: action.offer_id is alive-unpicked but did not
        clear the pickup floor at this heartbeat (e.g. WAI is firing
        an Observation under §XIV.I §5.3 loser pathway where this
        offer's WAI score was below floor). No bind.
        """
        cur, conn, queue = _setup_cur_and_queue()
        action = FirePickupObservation(offer_id="8653")

        executed, err = _exec_fpo(
            cur, conn, queue, action,
            alive_unpicked_offer_ids=frozenset({"8653"}),
            pickup_floor_clearers=frozenset({"8700"}),  # different offer
        )

        assert executed is True
        assert err is None
        queue.bind.assert_not_called()

    def test_action_in_floor_but_already_picked_no_bind(self):
        """§1.3 retirement: even when WAI keeps scoring a retired pickup
        leg for an already-picked offer (the 2026-06-01 §4 anomaly), the
        intersection with alive_unpicked_offer_ids excludes that offer.

        Scenario: WAI re-scored 8739's pickup leg above floor at a later
        heartbeat after 8739's actual_pickup_at was already written. A
        DIFFERENT offer (8740) fires FPO. The retired-but-floor-clearing
        8739 must NOT bind, and must NOT inflate local_competing in a
        way that blocks 8740's bind if 8740 happens to be the only
        alive-unpicked floor clearer.

        Here we test the simpler form: action.offer_id IS the retired
        one, no bind because it's not alive-unpicked.
        """
        cur, conn, queue = _setup_cur_and_queue()
        action = FirePickupObservation(offer_id="8739")

        executed, err = _exec_fpo(
            cur, conn, queue, action,
            # 8739 NOT in alive (already picked)
            alive_unpicked_offer_ids=frozenset({"8740"}),
            # WAI still re-scores 8739's pickup leg above floor
            pickup_floor_clearers=frozenset({"8739"}),
        )

        assert executed is True
        assert err is None
        queue.bind.assert_not_called()

    def test_empty_floor_clearers_no_bind(self):
        """Edge: no offer cleared the pickup floor at this heartbeat (e.g.
        WAI fired FPO under a code path that does not pre-validate floor
        clearance, or a future caller forgot to compute the set). The
        gate must fail-closed.

        Default-frozenset() callers (manual confirm endpoints, legacy
        tests) ride this path implicitly.
        """
        cur, conn, queue = _setup_cur_and_queue()
        action = FirePickupObservation(offer_id="8653")

        executed, err = _exec_fpo(
            cur, conn, queue, action,
            alive_unpicked_offer_ids=frozenset({"8653"}),
            pickup_floor_clearers=frozenset(),
        )

        assert executed is True
        assert err is None
        queue.bind.assert_not_called()

    def test_zero_alive_unpicked_no_crash(self):
        """Edge: idempotent redo or race produces an empty alive-unpicked
        set at the bind point. local_competing degenerates to empty;
        equality test fails cleanly; no bind; no exception.
        """
        cur, conn, queue = _setup_cur_and_queue()
        action = FirePickupObservation(offer_id="8653")

        executed, err = _exec_fpo(
            cur, conn, queue, action,
            alive_unpicked_offer_ids=frozenset(),
            pickup_floor_clearers=frozenset({"8653"}),
        )

        assert executed is True
        assert err is None
        queue.bind.assert_not_called()

    def test_cache_writes_undisturbed_by_bind_outcome(self):
        """Rule XV invariant preserved through the migration: cache
        writes (pms + offer_history + community_offers) happen identically
        whether or not the spatial-local bind fires. The bind is additive.

        Verified by running the FPO twice with identical inputs EXCEPT the
        local-competing axis, then comparing the executed SQL sequences.
        Cache write set must appear in BOTH runs; only queue.bind differs.
        """
        action = FirePickupObservation(offer_id="8653")

        # Run A: bind fires (singleton local competing set)
        cur_a, conn_a, queue_a = _setup_cur_and_queue()
        _exec_fpo(
            cur_a, conn_a, queue_a, action,
            alive_unpicked_offer_ids=frozenset({"8653"}),
            pickup_floor_clearers=frozenset({"8653"}),
        )

        # Run B: bind does NOT fire (shared-curb ambiguity)
        cur_b, conn_b, queue_b = _setup_cur_and_queue()
        _exec_fpo(
            cur_b, conn_b, queue_b, action,
            alive_unpicked_offer_ids=frozenset({"8653", "8700"}),
            pickup_floor_clearers=frozenset({"8653", "8700"}),
        )

        sqls_a = [c.args[0] for c in cur_a.execute.call_args_list if c.args]
        sqls_b = [c.args[0] for c in cur_b.execute.call_args_list if c.args]

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
