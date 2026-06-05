"""test_band_clause_matches_primitive.py

THE EQUIVALENCE GATE (Step 6 piece i, ERRATUM 2026-06-05 §4 point 5).

The liveness predicate's distance band is hand-written SQL. The single-owner
band is pudo_types.odometer_band / odometer_in_band (Python). "Hand-written to
match" drifts silently unless a test pins them together — this is that test.

It seeds offer_history rows at known band boundaries, runs the REAL
LIVE_OFFER_PREDICATE_SQL via live_offer_predicate_params (the same composition
production uses), and asserts the SQL's liveness verdict matches the primitive's
UPPER-EDGE verdict for the same inputs. If the SQL and the primitive ever
diverge (e.g. someone retunes ODOMETER_BAND_NOISE_FLOOR_MI in Python but the SQL
keeps a stale literal — the option-(b) failure we rejected), this test goes red.

Live-PG test (§XIV: db_cur SAVEPOINT/ROLLBACK isolation). Matches the house
pattern of test_lost_mode_houston_playback_live.py / test_offer_history_anchors_live.py.

IMPORTANT — UPPER EDGE ONLY: the liveness predicate reaps an offer that has
OVERSHOT (actual > center + tolerance). It does NOT reap a not-yet-reached offer
(actual < center - tolerance) — that offer stays live (driver en route; lower
edge is the candidacy layer's concern). So the SQL's "live" verdict is:
    actual_odometer <= center + tolerance        (upper edge)
which is TRUE for both in-band AND below-band positions, FALSE only above.
The primitive's odometer_in_band is SYMMETRIC; for the liveness comparison we
therefore compare against the primitive's UPPER-edge half, computed here from
odometer_band()'s (center, tolerance).
"""

import datetime

import pytest

from driver_queue import LIVE_OFFER_PREDICATE_SQL, live_offer_predicate_params
from pudo_types import odometer_band, ODOMETER_BAND_NOISE_FLOOR_MI


def _sql_says_live(db_cur, driver_id, offer_id, current_odometer, reference_time):
    """Run the REAL liveness predicate for one offer; return True iff it's live.

    Uses the exact production composition: the canonical predicate SQL plus the
    canonical params helper. last_odometer_move_at=None makes the staleness gate
    permissive so we isolate the DISTANCE axis (the band) as the only thing that
    can reap in this test.
    """
    db_cur.execute(
        f"""
        SELECT oh.id
        FROM app_private.offer_history oh
        JOIN app_private.decision_log dl ON dl.id = oh.decision_log_id
        WHERE dl.driver_id = %s
          AND oh.id = %s
          AND {LIVE_OFFER_PREDICATE_SQL}
        """,
        (driver_id, offer_id)
        + live_offer_predicate_params(current_odometer, reference_time, None),
    )
    return db_cur.fetchone() is not None


def _primitive_says_live(current_odometer, expected_distance, leg_distance):
    """The primitive's UPPER-EDGE verdict (the liveness question).

    live iff actual <= center + tolerance, OR no band (None -> permissive).
    """
    band = odometer_band(expected_distance, leg_distance)
    if band is None:
        return True  # NULL center/leg-distance -> permissive (§5.5 deferred)
    center, tolerance = band
    return current_odometer <= center + tolerance


# Boundary scenarios. center=1000, pickup_miles=20 -> tolerance=max(3.0,2.0)=3.0,
# so upper edge = 1003.0. We probe around it. Also a short leg (floored) and the
# NULL-permissive cases. trip leg mirrors with trip_miles.
PICKUP_SCENARIOS = [
    # (label, current_odo, expected_pickup_distance, pickup_miles, expect_live)
    ("pickup_well_below_center",      950.0, 1000.0, 20.0, True),   # en route, live
    ("pickup_at_center",             1000.0, 1000.0, 20.0, True),
    ("pickup_at_upper_edge",         1003.0, 1000.0, 20.0, True),   # <= edge, live
    ("pickup_just_over_upper_edge",  1003.01, 1000.0, 20.0, False), # overshot, reap
    ("pickup_far_over",              1100.0, 1000.0, 20.0, False),
    # short leg: pickup_miles=4 -> 0.15*4=0.6 -> floored to 2.0; edge=1002.0
    ("pickup_short_leg_floored_in",  1002.0, 1000.0, 4.0,  True),
    ("pickup_short_leg_floored_out", 1002.01, 1000.0, 4.0, False),
    # NULL-permissive: NULL center, NULL leg-distance
    ("pickup_null_center",           9999.0, None,   20.0, True),
    ("pickup_null_leg_distance",     9999.0, 1000.0, None, True),
]

DROPOFF_SCENARIOS = [
    # (label, current_odo, expected_dropoff_distance, trip_miles, expect_live)
    ("dropoff_below_center",         1020.0, 1050.0, 30.0, True),   # 0.15*30=4.5; edge 1054.5
    ("dropoff_at_upper_edge",        1054.5, 1050.0, 30.0, True),
    ("dropoff_just_over",            1054.51, 1050.0, 30.0, False),
    ("dropoff_far_over",             1200.0, 1050.0, 30.0, False),
    ("dropoff_null_center",          9999.0, None,   30.0, True),
    ("dropoff_null_trip_miles",      9999.0, 1050.0, None, True),
]


@pytest.mark.parametrize(
    "label,odo,expected_pickup_distance,pickup_miles,expect_live",
    PICKUP_SCENARIOS,
    ids=[s[0] for s in PICKUP_SCENARIOS],
)
def test_pickup_leg_sql_matches_primitive(
    db_cur, seed_decision_log, seed_offer_history, test_driver_id,
    label, odo, expected_pickup_distance, pickup_miles, expect_live,
):
    ref = datetime.datetime.now(datetime.timezone.utc)
    dl_id = seed_decision_log(test_driver_id)
    offer_id = seed_offer_history(
        dl_id,
        actual_pickup_at=None,                      # pickup leg
        expected_pickup_distance=expected_pickup_distance,
        pickup_miles=pickup_miles,
        # created_at recent so causality/abandonment clauses stay permissive
        created_at=ref,
    )
    sql_live = _sql_says_live(db_cur, test_driver_id, offer_id, odo, ref)
    prim_live = _primitive_says_live(odo, expected_pickup_distance, pickup_miles)

    assert sql_live == prim_live, (
        f"[{label}] SQL says live={sql_live}, primitive says live={prim_live} "
        f"— hand-written SQL has DRIFTED from pudo_types.odometer_band"
    )
    assert sql_live == expect_live, (
        f"[{label}] expected live={expect_live}, SQL says {sql_live}"
    )


@pytest.mark.parametrize(
    "label,odo,expected_dropoff_distance,trip_miles,expect_live",
    DROPOFF_SCENARIOS,
    ids=[s[0] for s in DROPOFF_SCENARIOS],
)
def test_dropoff_leg_sql_matches_primitive(
    db_cur, seed_decision_log, seed_offer_history, test_driver_id,
    label, odo, expected_dropoff_distance, trip_miles, expect_live,
):
    ref = datetime.datetime.now(datetime.timezone.utc)
    dl_id = seed_decision_log(test_driver_id)
    # dropoff leg requires actual_pickup_at IS NOT NULL
    offer_id = seed_offer_history(
        dl_id,
        actual_pickup_at=ref - datetime.timedelta(minutes=10),  # dropoff leg
        actual_dropoff_at=None,
        expected_dropoff_distance=expected_dropoff_distance,
        trip_miles=trip_miles,
        created_at=ref,
    )
    sql_live = _sql_says_live(db_cur, test_driver_id, offer_id, odo, ref)
    prim_live = _primitive_says_live(odo, expected_dropoff_distance, trip_miles)

    assert sql_live == prim_live, (
        f"[{label}] SQL says live={sql_live}, primitive says live={prim_live} "
        f"— hand-written SQL has DRIFTED from pudo_types.odometer_band"
    )
    assert sql_live == expect_live, (
        f"[{label}] expected live={expect_live}, SQL says {sql_live}"
    )


def test_noise_floor_is_single_owned_not_duplicated_in_sql():
    """Guard against the option-(b) drift we rejected: the SQL must NOT hardcode
    a 2.0 literal as the floor — it must bind ODOMETER_BAND_NOISE_FLOOR_MI as a
    param. We can't see the param value in the SQL text (it's a %s), so we assert
    the band clause contains GREATEST(...%s) for the floor, not GREATEST(...2.0).
    """
    sql = LIVE_OFFER_PREDICATE_SQL
    # The two GREATEST floor expressions must use a bound param (%s), not a literal.
    assert "GREATEST(0.15 * oh.pickup_miles, %s)" in sql, (
        "pickup-leg floor is not a bound param — possible hardcoded literal drift"
    )
    assert "GREATEST(0.15 * oh.trip_miles, %s)" in sql, (
        "dropoff-leg floor is not a bound param — possible hardcoded literal drift"
    )
    # And the primitive's floor is the single owner.
    assert ODOMETER_BAND_NOISE_FLOOR_MI == 2.0
