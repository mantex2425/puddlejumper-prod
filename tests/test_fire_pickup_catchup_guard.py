"""Live-PG regression test for the Rule XV narrative-catch-up guard
in `_execute_action`'s FirePickup branch (driver_heartbeat.py around
L254-323).

The defect this guards against (proven in prod 2026-06-02 19:02:08-14
UTC, 5 occurrences ~1.5s apart): line 291 read
`existing_row[0]` against a RealDictCursor row, raising
`KeyError: 0` and crashing the FirePickup handler before the bind +
§XVI.G lock acquisition could run. Fix: `existing_row['actual_pickup_at']`.

Why mocks would not catch this: the bug only manifests when `cur` is a
RealDictCursor (positional indexing fails on a dict-row); a hand-mocked
tuple-row cursor passes on the buggy code. This test runs the SELECT
through a real Postgres RealDictCursor via the db_cur fixture
(conftest.py), so it would have caught the regression and will catch
any future re-introduction.

The test also pins the Rule XV invariant the catch-up branch exists to
enforce: when a FirePickup arrives for an offer whose pickup was
already fired by a prior FirePickupObservation, the
`offer_history.actual_pickup_*` cache (written by the Observation at
the rider's true pickup location) must NOT be overwritten with the
later, possibly miles-away FirePickup nail position. See
docs/RECON_IMPERIAL_VALLEY_PICKUP5_2026-05-30.md for the verbatim
1.8mi/10-min drift this guard prevents.
"""
from __future__ import annotations

import datetime

from dispatch import FirePickup
from driver_heartbeat import _execute_action
from driver_queue import DriverQueue


# Sentinel coordinates written by the prior Observation (the "true" location).
PRIOR_LAT = 29.7000
PRIOR_LNG = -95.3000

# Sentinel cluster centroid — the current FirePickup nail position.
# Deliberately different from PRIOR_* so cache-clobber would be visible
# if the catch-up branch failed to short-circuit before the UPDATE.
CLUSTER_LAT = 29.7604
CLUSTER_LNG = -95.3698


class _FakeCluster:
    median_lat = CLUSTER_LAT
    median_lng = CLUSTER_LNG


def _seed_driver_trip_state(db_cur, driver_id):
    db_cur.execute(
        """
        INSERT INTO app_private.driver_trip_state (driver_id, current_offer_id)
        VALUES (%s, NULL)
        """,
        (driver_id,),
    )


def _read_current_offer_id(db_cur, driver_id):
    db_cur.execute(
        """
        SELECT current_offer_id FROM app_private.driver_trip_state
        WHERE driver_id = %s
        """,
        (driver_id,),
    )
    row = db_cur.fetchone()
    return row["current_offer_id"] if row else None


def _read_actual_pickup(db_cur, oh_id):
    db_cur.execute(
        """
        SELECT actual_pickup_at, actual_pickup_lat, actual_pickup_lng
        FROM app_private.offer_history
        WHERE id = %s
        """,
        (oh_id,),
    )
    return db_cur.fetchone()


def test_catchup_preserves_cache_and_binds_narrative(
    db_cur, test_driver_id, seed_decision_log, seed_offer_history,
):
    """FirePickup catch-up against a row with non-NULL actual_pickup_at:

    1. Must NOT raise (regression for `KeyError: 0` on RealDictCursor at
       driver_heartbeat.py:291).
    2. Must take the catch-up branch — driver_trip_state.current_offer_id
       set via queue.bind.
    3. Rule XV invariant — offer_history.actual_pickup_at /
       actual_pickup_lat / actual_pickup_lng unchanged. The cluster
       centroid (CLUSTER_LAT/LNG) MUST NOT replace the prior
       Observation's true-location cache (PRIOR_LAT/LNG).
    """
    _seed_driver_trip_state(db_cur, test_driver_id)

    prior_pickup_at = datetime.datetime.now(
        datetime.timezone.utc
    ) - datetime.timedelta(minutes=10)

    dl_id = seed_decision_log(test_driver_id)
    oh_id = seed_offer_history(
        dl_id,
        actual_pickup_at=prior_pickup_at,
        actual_pickup_lat=PRIOR_LAT,
        actual_pickup_lng=PRIOR_LNG,
    )

    queue = DriverQueue(test_driver_id)
    action = FirePickup(offer_id=str(oh_id))

    executed, err = _execute_action(
        action, db_cur, db_cur.connection, test_driver_id, queue,
        cluster=_FakeCluster(),
        cumulative_miles=145.5,
    )

    # (1) No exception — the regression guard.
    assert executed is True, (
        f"catch-up branch did not execute (err={err!r}); pre-fix this "
        f"raised KeyError: 0 at driver_heartbeat.py:291 against a "
        f"RealDictCursor row."
    )
    assert err is None

    # (2) Narrative bound via queue.bind.
    bound = _read_current_offer_id(db_cur, test_driver_id)
    assert str(bound) == str(oh_id), (
        f"catch-up: expected driver_trip_state.current_offer_id={oh_id} "
        f"(queue.bind ran), got {bound}. Bind never happened — the "
        f"crash at L291 would have aborted before reaching queue.bind."
    )

    # (3) Rule XV invariant — cache untouched.
    after = _read_actual_pickup(db_cur, oh_id)
    assert after["actual_pickup_at"] == prior_pickup_at, (
        f"Rule XV violated: actual_pickup_at changed from "
        f"{prior_pickup_at} to {after['actual_pickup_at']} — the "
        f"catch-up branch must NOT overwrite the prior Observation's "
        f"cache."
    )
    assert float(after["actual_pickup_lat"]) == PRIOR_LAT, (
        f"Rule XV violated: actual_pickup_lat changed from {PRIOR_LAT} "
        f"to {after['actual_pickup_lat']} — the cluster centroid "
        f"(CLUSTER_LAT={CLUSTER_LAT}) clobbered the prior Observation's "
        f"true location."
    )
    assert float(after["actual_pickup_lng"]) == PRIOR_LNG, (
        f"Rule XV violated: actual_pickup_lng changed from {PRIOR_LNG} "
        f"to {after['actual_pickup_lng']} — the cluster centroid "
        f"(CLUSTER_LNG={CLUSTER_LNG}) clobbered the prior Observation's "
        f"true location."
    )
