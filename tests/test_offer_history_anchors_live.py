"""
Live-DB tests for offer_history anchor logic.

These tests exercise REAL SQL against a live Postgres connection,
validating semantics that mock-based tests cannot reach (e.g., that
LIVE_OFFER_PREDICATE_SQL actually filters stale rows when the database
runs the query — not just that the bind tuple has the right shape).

Tech debt: tests run against the production database; see
tests/conftest.py module docstring for the post-launch P1 to migrate
to a dedicated test environment.
"""

from __future__ import annotations

import datetime

from driver_heartbeat import _get_last_known_anchor_id


def test_get_last_known_anchor_id_returns_none_when_only_stale_anchor_exists(
    db_cur,
    test_driver_id,
    seed_decision_log,
    seed_offer_history,
):
    """Bug-3 regression scenario, live-DB.

    Insert a confirmed-pickup offer 14 days old. _get_last_known_anchor_id
    must return None — the GC predicate's time axis excludes anchors
    older than the wall-clock horizon.

    Pre-2026-05-10 (before LIVE_OFFER_PREDICATE_SQL): this query had
    no time predicate. It would return the stale anchor's offer_id,
    poisoning every TAD distance computation downstream. That was the
    dispatch-silence root cause diagnosed from
    pudo_decision_context.tad_decision_context.

    This test would have FAILED on the pre-fix code (returning the
    stale id), proving the regression-guard is real.

    Predicate math, given default seed_offer_history values
    (pickup_minutes=8, trip_minutes=15):
        raw_min     = 8 + 15 = 23
        window_min  = LEAST(GREATEST(23 * 1.25, 15), 240) = 28.75 min
    A 14-day-old created_at + 28.75 min is still 14 days in the past;
    the time axis filters it. Distance axis short-circuits to TRUE
    (current_cumulative_miles is None by default), so only the time
    axis decides.
    """
    fourteen_days_ago = (
        datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(days=14)
    )
    decision_log_id = seed_decision_log(test_driver_id)
    seed_offer_history(
        decision_log_id,
        created_at=fourteen_days_ago,
        actual_pickup_at=fourteen_days_ago + datetime.timedelta(minutes=10),
        miles_at_offer_receipt=100.0,
    )

    result = _get_last_known_anchor_id(db_cur, test_driver_id)

    assert result is None, (
        f"Expected None (stale anchor excluded by GC predicate), "
        f"got {result!r}. This indicates LIVE_OFFER_PREDICATE_SQL's "
        f"time axis is not filtering the 14-day-old anchor — a "
        f"regression of the bug diagnosed 2026-05-10."
    )
