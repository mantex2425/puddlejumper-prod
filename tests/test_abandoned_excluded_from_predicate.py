"""§XIV.J live-PG test for §9.9.2: the 'abandoned' status excludes an offer
from LIVE_OFFER_PREDICATE_SQL.

The keystone of the §9.9 abandoned-status design. Flipping a deferred offer to
'abandoned' at the dropoff handler (§9.9.3) accomplishes nothing UNLESS the live
predicate actually drops it — the predicate is the single liveness authority
(§9.9.6); the status writer feeds it.

The load-bearing assertion is INDEPENDENCE from the band. The odometer band is
NULL-permissive (ERRATUM §4): a NULL center / NULL leg-distance / NULL odometer
makes the band branch TRUE so the §5.5 deferred sentinel stays live. If the
abandoned exclusion only *appeared* to work because the band happened to reap the
row anyway, the clause would be dead weight. So both tests seed NULL band terms
(expected_pickup_distance NULL) — the band is PERMISSIVE — and prove the verdict
turns ENTIRELY on expected_odometer_status:

  - abandoned + NULL band -> DEAD  (the §9.9.2 clause, not the band, kills it)
  - deferred  + NULL band -> ALIVE (control: NULL-band is genuinely permissive;
                                    the §5.5 sentinel survives)

Real cursor (db_cur), SAVEPOINT-rolled-back per conftest.
"""
import datetime
import pytest

from driver_queue import LIVE_OFFER_PREDICATE_SQL, live_offer_predicate_params

UTC = datetime.timezone.utc


def _eval_predicate(cur, offer_id, current_cumulative_miles, reference_time,
                    last_odometer_move_at):
    """True iff LIVE_OFFER_PREDICATE_SQL holds for offer_id. Mirrors the
    canonical predicate-eval helper in test_predicate_staleness_and_reanchor."""
    cur.execute(
        f"""
        SELECT EXISTS (
            SELECT 1 FROM app_private.offer_history oh
            WHERE oh.id = %s
              AND {LIVE_OFFER_PREDICATE_SQL}
        ) AS alive
        """,
        (offer_id,) + live_offer_predicate_params(
            current_cumulative_miles, reference_time, last_odometer_move_at
        ),
    )
    return cur.fetchone()["alive"]


def test_abandoned_offer_excluded_from_live_predicate(
    db_cur, test_driver_id, seed_decision_log, seed_offer_history
):
    """An 'abandoned'-status offer fails the predicate EVEN with a permissive
    (NULL) band — proving §9.9.2's clause is load-bearing, not coincidental."""
    dlog = seed_decision_log(test_driver_id)
    now = datetime.datetime.now(UTC)

    # Pickup leg (actual_pickup_at NULL), expected_pickup_distance NULL ->
    # band branch is NULL-permissive (would keep the offer alive on its own).
    # Recent created_at + moving driver clear causality / ceiling / staleness.
    abandoned_id = seed_offer_history(
        dlog,
        created_at=now - datetime.timedelta(minutes=10),
        actual_pickup_at=None,
        actual_dropoff_at=None,
        expected_pickup_distance=None,   # NULL center -> band permissive
        expected_odometer_status="abandoned",
    )

    alive = _eval_predicate(
        db_cur, abandoned_id,
        current_cumulative_miles=100.0,
        reference_time=now,
        last_odometer_move_at=now,       # moving -> staleness permissive
    )
    assert alive is False, (
        "abandoned offer must be EXCLUDED from the live set even though every "
        "other gate (band/causality/ceiling/staleness) is permissive — the "
        "§9.9.2 `IS DISTINCT FROM 'abandoned'` clause is the only thing that "
        "can kill it here. If this passes-as-alive, the clause is missing or "
        "the band is masking the result."
    )


def test_deferred_offer_with_null_band_stays_alive_control(
    db_cur, test_driver_id, seed_decision_log, seed_offer_history
):
    """Control for the test above: the IDENTICAL row at status 'deferred' is
    ALIVE. Same NULL-band inputs, only the status differs — proves the band is
    genuinely permissive and isolates the kill to the abandoned clause."""
    dlog = seed_decision_log(test_driver_id)
    now = datetime.datetime.now(UTC)

    deferred_id = seed_offer_history(
        dlog,
        created_at=now - datetime.timedelta(minutes=10),
        actual_pickup_at=None,
        actual_dropoff_at=None,
        expected_pickup_distance=None,   # NULL center -> band permissive
        expected_odometer_status="deferred",
    )

    alive = _eval_predicate(
        db_cur, deferred_id,
        current_cumulative_miles=100.0,
        reference_time=now,
        last_odometer_move_at=now,
    )
    assert alive is True, (
        "deferred offer with a NULL (permissive) band must stay ALIVE — this is "
        "the §5.5 sentinel behavior, and it proves the NULL-band path is "
        "permissive, so the abandoned test's DEAD verdict is caused by the "
        "status, not by the band."
    )
