"""§XIV.J live-PG test for §5.5 Class B (FINDING §9.2 + §9.9.3): window-scoped
deferred recompute and out-of-window abandonment.

Proves _resolve_deferred_at_dropoff does what §9.2/§9.9.3 specify:
  - in-window deferred offer -> recomputed to 'active' with the CHAINED anchor
    (expected_odometer == X.expected_dropoff_distance + D.pickup_miles), proving
    §9.1 "the chaining IS the bridge" — the test that would have caught a
    9132-class confident-wrong-formula bug.
  - out-of-window deferred offer -> flipped to 'abandoned' (§9.9.3): proven part
    of no ride, so it drops from the live set immediately via §9.9.2 rather than
    lingering 'deferred' for hours (the §9.8 disposition this corrects).
  - X with no prev anchor -> the in-window recompute no-ops (cannot supply a
    bridge it does not have), but the out-of-window abandonment STILL runs
    (§9.9.6: no 4h linger regardless of X's anchor).

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


def test_out_of_window_deferred_is_abandoned(
    db_cur, test_driver_id, seed_decision_log, seed_offer_history
):
    """Out-of-window deferred offer is flipped to 'abandoned' (§9.9.3).

    The dropoff of X proves the ride window; a deferred offer received BEFORE
    that window began belongs to no resolved ride. Per §9.9.3 it is NOT
    recomputed (no bridge) and NOT left lingering deferred (§9.8's wrong
    disposition) — it is marked 'abandoned' so §9.9.2 drops it from the live
    set immediately. The anchor stays NULL (never fabricated)."""
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
    # It belongs to no resolved ride -> abandoned.
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
    assert row["expected_odometer_status"] == "abandoned", (
        "out-of-window deferred offer must be flipped to 'abandoned' (§9.9.3) so "
        "the §9.9.2 predicate clause drops it immediately — not left lingering"
    )
    assert row["expected_odometer"] is None, (
        "abandoned anchor must remain NULL (the bridge was never fabricated)"
    )


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
        "no prev anchor -> IN-window deferred offer left deferred (cannot "
        "fabricate the bridge; in-window recompute no-ops)"
    )


def test_out_of_window_abandoned_even_when_x_has_no_anchor(
    db_cur, test_driver_id, seed_decision_log, seed_offer_history
):
    """§9.9.6 guarantee: out-of-window abandonment runs even when X carries NO
    chaining anchor. The abandonment keys only on window membership (window_lo =
    COALESCE(X.actual_pickup_at, X.created_at)), so it MUST run before — and
    independently of — the chaining-anchor early-return that gates the in-window
    recompute. Otherwise an X with no dropoff anchor would leave out-of-window
    deferred offers lingering for the 4h ceiling, defeating §9.9.6."""
    dlog = seed_decision_log(test_driver_id)

    # X fires dropoff but has NO chaining anchor (expected_dropoff_distance NULL)
    # -> the in-window recompute path early-returns. window_lo = pickup_at (-30).
    x_id = seed_offer_history(
        dlog,
        created_at=_dt(-40),
        actual_pickup_at=_dt(-30),
        expected_dropoff_distance=None,
        expected_dropoff_arrival_time=None,
        expected_odometer_status="active",
    )

    # D: deferred, received BEFORE X's window (-90 < window_lo -30) -> out of
    # window -> must be abandoned despite X having no anchor.
    d_id = seed_offer_history(
        dlog,
        created_at=_dt(-90),
        pickup_miles=3.0,
        expected_odometer=None,
        expected_odometer_status="deferred",
    )

    _resolve_deferred_at_dropoff(db_cur, test_driver_id, x_id, dropoff_fire_odometer=125.0)

    db_cur.execute(
        "SELECT expected_odometer_status FROM app_private.offer_history WHERE id = %s",
        (d_id,),
    )
    assert db_cur.fetchone()["expected_odometer_status"] == "abandoned", (
        "out-of-window deferred offer must be abandoned even when X has no "
        "chaining anchor (§9.9.6: abandonment is independent of the recompute "
        "bridge; it must run before the chaining-anchor early-return)"
    )
