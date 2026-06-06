"""§XIV.J live-PG test for §5.5 Class B (FINDING §9.2): window-scoped deferred recompute.

Proves _resolve_deferred_at_dropoff does what §9.2 specifies:
  - in-window deferred offer -> recomputed to 'active' with the CHAINED anchor
    (expected_odometer == X.expected_dropoff_distance + D.pickup_miles), proving
    §9.1 "the chaining IS the bridge" — the test that would have caught a
    9132-class confident-wrong-formula bug.
  - out-of-window deferred offer -> LEFT 'deferred' (the load-bearing window
    guardrail; recomputing against the wrong trip is the failure the sentinel
    exists to prevent).
  - X with no prev anchor -> no-op (cannot supply a bridge it does not have).

Real cursor (db_cur), SAVEPOINT-rolled-back per conftest. Seeds offer_history +
decision_log atomically via the conftest fixtures (L-19).
"""
import datetime
import pytest

from driver_heartbeat import _resolve_deferred_at_dropoff


UTC = datetime.timezone.utc


def _dt(offset_min):
    """A tz-aware UTC datetime offset_min minutes from now."""
    return datetime.datetime.now(UTC) + datetime.timedelta(minutes=offset_min)


def test_in_window_deferred_recomputed_to_active(
    db_cur, test_driver_id, seed_decision_log, seed_offer_history
):
    """In-window deferred offer flips to active with the chained anchor."""
    dlog = seed_decision_log(test_driver_id)

    # X: the resolving dropoff. Picked up 30 min ago, dropoff fires now.
    # Its dropoff anchor (the chaining prev) is a known cumulative odometer.
    x_dropoff_dist = 120.0
    x_id = seed_offer_history(
        dlog,
        created_at=_dt(-40),
        actual_pickup_at=_dt(-30),
        actual_dropoff_at=None,  # not yet stamped; helper runs at the moment of fire
        expected_dropoff_distance=x_dropoff_dist,
        expected_dropoff_arrival_time=_dt(-5),
        expected_odometer=x_dropoff_dist,
        expected_odometer_status="active",
    )

    # D: deferred offer received DURING X's trip (created 15 min ago, between
    # X.actual_pickup_at (-30) and now). pickup_miles drives the chained anchor.
    d_pickup_miles = 3.0
    d_id = seed_offer_history(
        dlog,
        created_at=_dt(-15),
        pickup_miles=d_pickup_miles,
        trip_miles=7.0,
        pickup_minutes=10,
        trip_minutes=20,
        expected_odometer=None,
        expected_odometer_status="deferred",
        expected_pickup_distance=None,
        expected_dropoff_distance=None,
    )

    # Fire the recompute as the dropoff handler would, with X's dropoff odometer.
    _resolve_deferred_at_dropoff(db_cur, test_driver_id, x_id, dropoff_fire_odometer=125.0)

    db_cur.execute(
        """SELECT expected_odometer, expected_odometer_status,
                  expected_pickup_distance, expected_dropoff_distance
           FROM app_private.offer_history WHERE id = %s""",
        (d_id,),
    )
    row = db_cur.fetchone()

    # §9.1: the chaining IS the bridge. expected_pickup_distance (== expected_odometer)
    # must be X's dropoff anchor + D's pickup_miles — produced by the real
    # compute_offer_expectations stacked branch, NOT a hardcoded number.
    expected_anchor = x_dropoff_dist + d_pickup_miles  # 120.0 + 3.0 = 123.0
    assert row["expected_odometer_status"] == "active", "deferred -> active flip"
    assert row["expected_odometer"] is not None, "anchor must be filled, not NULL"
    assert abs(row["expected_odometer"] - expected_anchor) < 0.01, (
        f"chained anchor: expected {expected_anchor} "
        f"(X.dropoff_dist {x_dropoff_dist} + D.pickup_miles {d_pickup_miles}), "
        f"got {row['expected_odometer']}"
    )
    # Full backfill: the four expected_* anchors are no longer NULL.
    # NB: expected_pickup_distance / expected_dropoff_distance are numeric(Decimal);
    # expected_odometer is double(float). Coerce to float before arithmetic.
    assert row["expected_pickup_distance"] is not None, "full backfill: pickup_distance"
    assert abs(float(row["expected_pickup_distance"]) - expected_anchor) < 0.01
    assert row["expected_dropoff_distance"] is not None, "full backfill: dropoff_distance"


def test_out_of_window_deferred_stays_deferred(
    db_cur, test_driver_id, seed_decision_log, seed_offer_history
):
    """Out-of-window deferred offer is LEFT deferred (the §9.2 window guardrail)."""
    dlog = seed_decision_log(test_driver_id)

    x_id = seed_offer_history(
        dlog,
        created_at=_dt(-40),
        actual_pickup_at=_dt(-30),
        expected_dropoff_distance=120.0,
        expected_dropoff_arrival_time=_dt(-5),
        expected_odometer=120.0,
        expected_odometer_status="active",
    )

    # D2: deferred, but received BEFORE X's window lower bound (-30). Created -90.
    # It belongs to an earlier, unresolved segment — must NOT be recomputed.
    d2_id = seed_offer_history(
        dlog,
        created_at=_dt(-90),
        pickup_miles=3.0,
        expected_odometer=None,
        expected_odometer_status="deferred",
    )

    _resolve_deferred_at_dropoff(db_cur, test_driver_id, x_id, dropoff_fire_odometer=125.0)

    db_cur.execute(
        "SELECT expected_odometer, expected_odometer_status FROM app_private.offer_history WHERE id = %s",
        (d2_id,),
    )
    row = db_cur.fetchone()
    assert row["expected_odometer_status"] == "deferred", (
        "out-of-window offer must stay deferred (window guardrail; the GC reaps it)"
    )
    assert row["expected_odometer"] is None, "out-of-window anchor must remain NULL"


def test_x_without_prev_anchor_is_noop(
    db_cur, test_driver_id, seed_decision_log, seed_offer_history
):
    """If X has no dropoff anchor, the helper cannot supply a bridge -> no-op."""
    dlog = seed_decision_log(test_driver_id)

    # X with NULL expected_dropoff_distance — no chaining anchor available.
    x_id = seed_offer_history(
        dlog,
        created_at=_dt(-40),
        actual_pickup_at=_dt(-30),
        expected_dropoff_distance=None,
        expected_dropoff_arrival_time=None,
        expected_odometer_status="active",
    )
    d_id = seed_offer_history(
        dlog,
        created_at=_dt(-15),
        pickup_miles=3.0,
        expected_odometer=None,
        expected_odometer_status="deferred",
    )

    _resolve_deferred_at_dropoff(db_cur, test_driver_id, x_id, dropoff_fire_odometer=125.0)

    db_cur.execute(
        "SELECT expected_odometer_status FROM app_private.offer_history WHERE id = %s",
        (d_id,),
    )
    assert db_cur.fetchone()["expected_odometer_status"] == "deferred", (
        "no prev anchor -> deferred offer left untouched (cannot fabricate the bridge)"
    )
