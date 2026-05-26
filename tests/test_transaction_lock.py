"""§XVI.G Transaction Lock — TDD baseline (red).

Test families per Sprint A Step 2 design review:

  A. Schema integrity (sanity tests against app_private.driver_trip_locks)
  B. Lock acquisition (acquire_lock contract)
  C. Lock query and release (is_locked / release_lock contracts,
     including C6 forensic payload invariant per Gemini 2026-05-26)

All tests use the canonical db_cur fixture from tests/conftest.py per
§XIV.J Live-PG Test Floor. The fixture provides a SAVEPOINT-isolated
RealDictCursor; every test rolls back at teardown.

Test driver IDs use the test_driver_id fixture (sentinel-tagged
TEST_GC_<datestamp>_<uuid>) so the session janitor sweeps any rows
that escape rollback.

Authors: Andrew + Claude (paired-programming) + Gemini (ratified 2026-05-26)
Sprint: A Step 2. Authored RED. Sprint A Step 3 implements the module
and turns these green.
"""

from datetime import datetime, timedelta, timezone

import pytest
import psycopg2.errors

from decisions.transaction_lock import (
    LOCK_RELEASE_DISTANCE_M,
    LOCK_RELEASE_SPEED_STREAK_S,
    LockContext,
    acquire_lock,
    is_locked,
    release_lock,
)


# Fixed reference coordinates for spatial tests. Houston metro area;
# these coordinates are far from any test driver's actual rides so
# inserts here cannot collide with real production data.
_REF_LAT = 29.7604
_REF_LNG = -95.3698


# -----------------------------------------------------------------------------
# Family A — Schema integrity
# -----------------------------------------------------------------------------
# These tests exercise the database substrate directly without going
# through the transaction_lock module. They pin the migration's intent
# so future schema changes that drift from §XVI.G fail loudly.


def test_A1_table_exists_with_seven_columns(db_cur):
    """A1: driver_trip_locks has exactly the 7 columns the migration declared."""
    db_cur.execute("""
        SELECT column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_schema = 'app_private'
          AND table_name = 'driver_trip_locks'
        ORDER BY ordinal_position
    """)
    rows = db_cur.fetchall()
    columns = [(r['column_name'], r['data_type'], r['is_nullable']) for r in rows]
    assert columns == [
        ('driver_id', 'text', 'NO'),
        ('offer_id', 'text', 'NO'),
        ('pudo_type', 'text', 'NO'),
        ('fired_at', 'timestamp with time zone', 'NO'),
        ('fired_lat', 'double precision', 'NO'),
        ('fired_lng', 'double precision', 'NO'),
        ('fired_cumulative_miles', 'numeric', 'NO'),
    ]


def test_A2_pk_rejects_duplicate_composite(db_cur, test_driver_id):
    """A2: inserting two rows with the same (driver, offer, leg) PK fails.

    This is the foundational stacked-offer guarantee. If the PK doesn't
    reject duplicates, the lock state can be silently overwritten.
    """
    now = datetime.now(timezone.utc)
    db_cur.execute("""
        INSERT INTO app_private.driver_trip_locks
            (driver_id, offer_id, pudo_type, fired_at,
             fired_lat, fired_lng, fired_cumulative_miles)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    """, (test_driver_id, 'A2_offer', 'pickup', now, _REF_LAT, _REF_LNG, 100.0))

    with pytest.raises(psycopg2.errors.UniqueViolation):
        db_cur.execute("""
            INSERT INTO app_private.driver_trip_locks
                (driver_id, offer_id, pudo_type, fired_at,
                 fired_lat, fired_lng, fired_cumulative_miles)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (test_driver_id, 'A2_offer', 'pickup', now, _REF_LAT, _REF_LNG, 100.0))


def test_A3_check_constraint_rejects_bad_pudo_type(db_cur, test_driver_id):
    """A3: pudo_type domain is enforced at the storage boundary.

    Catches application-tier routing bugs that try to write 'p',
    'PICKUP', 'drop', or any value outside the canonical two-token
    domain.
    """
    now = datetime.now(timezone.utc)
    with pytest.raises(psycopg2.errors.CheckViolation):
        db_cur.execute("""
            INSERT INTO app_private.driver_trip_locks
                (driver_id, offer_id, pudo_type, fired_at,
                 fired_lat, fired_lng, fired_cumulative_miles)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (test_driver_id, 'A3_offer', 'invalid', now,
              _REF_LAT, _REF_LNG, 100.0))


def test_A4_stacked_offers_get_independent_rows(db_cur, test_driver_id):
    """A4: same driver, two offers, same leg => two independent rows.

    The composite PK includes offer_id, so locking offer A's pickup
    does not block locking offer B's pickup. Protects against the
    stacked-offer overwrite vulnerability identified during Sprint A
    design review.
    """
    now = datetime.now(timezone.utc)
    for offer in ('A4_offer_X', 'A4_offer_Y'):
        db_cur.execute("""
            INSERT INTO app_private.driver_trip_locks
                (driver_id, offer_id, pudo_type, fired_at,
                 fired_lat, fired_lng, fired_cumulative_miles)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (test_driver_id, offer, 'pickup', now,
              _REF_LAT, _REF_LNG, 100.0))

    db_cur.execute("""
        SELECT COUNT(*) AS n
        FROM app_private.driver_trip_locks
        WHERE driver_id = %s
    """, (test_driver_id,))
    assert db_cur.fetchone()['n'] == 2


# -----------------------------------------------------------------------------
# Family B — Lock acquisition (acquire_lock)
# -----------------------------------------------------------------------------


def test_B1_acquire_lock_inserts_one_row(db_cur, test_driver_id):
    """B1: acquire_lock on a clean key creates exactly one row."""
    fired_at = datetime.now(timezone.utc)
    acquire_lock(
        cur=db_cur,
        driver_id=test_driver_id,
        offer_id='B1_offer',
        pudo_type='pickup',
        fired_at=fired_at,
        fired_lat=_REF_LAT,
        fired_lng=_REF_LNG,
        fired_cumulative_miles=100.0,
    )
    db_cur.execute("""
        SELECT driver_id, offer_id, pudo_type, fired_at,
               fired_lat, fired_lng, fired_cumulative_miles
        FROM app_private.driver_trip_locks
        WHERE driver_id = %s AND offer_id = %s AND pudo_type = %s
    """, (test_driver_id, 'B1_offer', 'pickup'))
    row = db_cur.fetchone()
    assert row['driver_id'] == test_driver_id
    assert row['offer_id'] == 'B1_offer'
    assert row['pudo_type'] == 'pickup'
    assert row['fired_lat'] == pytest.approx(_REF_LAT)
    assert row['fired_lng'] == pytest.approx(_REF_LNG)
    assert float(row['fired_cumulative_miles']) == pytest.approx(100.0)


def test_B2_stacked_offers_two_independent_acquisitions(db_cur, test_driver_id):
    """B2: acquire_lock(A,X,pickup) then acquire_lock(A,Y,pickup) => 2 rows.

    Application-level confirmation of A4. The contract is that the
    second acquire does not disturb the first lock's spatial anchor.
    """
    fired_at = datetime.now(timezone.utc)
    for offer, lat in (('B2_offer_X', _REF_LAT), ('B2_offer_Y', _REF_LAT + 0.01)):
        acquire_lock(
            cur=db_cur,
            driver_id=test_driver_id,
            offer_id=offer,
            pudo_type='pickup',
            fired_at=fired_at,
            fired_lat=lat,
            fired_lng=_REF_LNG,
            fired_cumulative_miles=100.0,
        )
    db_cur.execute("""
        SELECT offer_id, fired_lat
        FROM app_private.driver_trip_locks
        WHERE driver_id = %s
        ORDER BY offer_id
    """, (test_driver_id,))
    rows = db_cur.fetchall()
    assert len(rows) == 2
    assert rows[0]['offer_id'] == 'B2_offer_X'
    assert rows[0]['fired_lat'] == pytest.approx(_REF_LAT)
    assert rows[1]['offer_id'] == 'B2_offer_Y'
    assert rows[1]['fired_lat'] == pytest.approx(_REF_LAT + 0.01)


def test_B3_acquire_lock_idempotent_no_op_preserves_anchor(db_cur, test_driver_id):
    """B3: re-acquiring an existing lock is a no-op (no overwrite).

    The latching-lock invariant: the original fire's spatial anchor
    is the source of truth for the release math. A second
    acquire_lock with different lat/lng must NOT update the row.
    """
    original_lat = _REF_LAT
    drifted_lat = _REF_LAT + 0.05  # ~3.4mi north — would falsely "re-anchor"

    fired_at = datetime.now(timezone.utc)
    acquire_lock(
        cur=db_cur,
        driver_id=test_driver_id,
        offer_id='B3_offer',
        pudo_type='dropoff',
        fired_at=fired_at,
        fired_lat=original_lat,
        fired_lng=_REF_LNG,
        fired_cumulative_miles=100.0,
    )
    # Second acquire with drifted coordinates — must be a no-op.
    acquire_lock(
        cur=db_cur,
        driver_id=test_driver_id,
        offer_id='B3_offer',
        pudo_type='dropoff',
        fired_at=fired_at + timedelta(seconds=2),
        fired_lat=drifted_lat,
        fired_lng=_REF_LNG,
        fired_cumulative_miles=101.0,
    )
    db_cur.execute("""
        SELECT fired_lat, fired_cumulative_miles
        FROM app_private.driver_trip_locks
        WHERE driver_id = %s AND offer_id = %s AND pudo_type = %s
    """, (test_driver_id, 'B3_offer', 'dropoff'))
    row = db_cur.fetchone()
    # Original anchor preserved; drift rejected.
    assert row['fired_lat'] == pytest.approx(original_lat)
    assert float(row['fired_cumulative_miles']) == pytest.approx(100.0)


# -----------------------------------------------------------------------------
# Family C — Lock query (is_locked) and release (release_lock)
# -----------------------------------------------------------------------------


def test_C1_is_locked_returns_context_when_within_horizon(db_cur, test_driver_id):
    """C1: is_locked returns LockContext when within 500ft and speed_streak < 10s."""
    fired_at = datetime.now(timezone.utc) - timedelta(seconds=8)
    acquire_lock(
        cur=db_cur,
        driver_id=test_driver_id,
        offer_id='C1_offer',
        pudo_type='dropoff',
        fired_at=fired_at,
        fired_lat=_REF_LAT,
        fired_lng=_REF_LNG,
        fired_cumulative_miles=100.0,
    )
    # Driver hasn't moved. is_locked must return a populated context.
    result = is_locked(
        cur=db_cur,
        driver_id=test_driver_id,
        offer_id='C1_offer',
        pudo_type='dropoff',
        current_lat=_REF_LAT,
        current_lng=_REF_LNG,
        current_cumulative_miles=100.0,
        current_speed_streak_s=0.0,
        now=datetime.now(timezone.utc),
    )
    assert result is not None
    assert isinstance(result, LockContext)


def test_C2_is_locked_releases_when_distance_exceeded(db_cur, test_driver_id):
    """C2: is_locked returns None and deletes the row when 500ft is crossed.

    Spatial release condition. _REF_LAT + 0.002 is roughly 222m
    (well over 152.4m / 500ft) — comfortably past the threshold.
    """
    fired_at = datetime.now(timezone.utc)
    acquire_lock(
        cur=db_cur,
        driver_id=test_driver_id,
        offer_id='C2_offer',
        pudo_type='dropoff',
        fired_at=fired_at,
        fired_lat=_REF_LAT,
        fired_lng=_REF_LNG,
        fired_cumulative_miles=100.0,
    )
    result = is_locked(
        cur=db_cur,
        driver_id=test_driver_id,
        offer_id='C2_offer',
        pudo_type='dropoff',
        current_lat=_REF_LAT + 0.002,
        current_lng=_REF_LNG,
        current_cumulative_miles=100.2,
        current_speed_streak_s=0.0,
        now=datetime.now(timezone.utc),
    )
    assert result is None
    # Row should also be cleaned up on auto-release.
    db_cur.execute("""
        SELECT COUNT(*) AS n
        FROM app_private.driver_trip_locks
        WHERE driver_id = %s AND offer_id = %s AND pudo_type = %s
    """, (test_driver_id, 'C2_offer', 'dropoff'))
    assert db_cur.fetchone()['n'] == 0


def test_C3_is_locked_releases_when_speed_streak_met(db_cur, test_driver_id):
    """C3: is_locked returns None when current_speed_streak_s >= 10."""
    fired_at = datetime.now(timezone.utc)
    acquire_lock(
        cur=db_cur,
        driver_id=test_driver_id,
        offer_id='C3_offer',
        pudo_type='dropoff',
        fired_at=fired_at,
        fired_lat=_REF_LAT,
        fired_lng=_REF_LNG,
        fired_cumulative_miles=100.0,
    )
    result = is_locked(
        cur=db_cur,
        driver_id=test_driver_id,
        offer_id='C3_offer',
        pudo_type='dropoff',
        current_lat=_REF_LAT,
        current_lng=_REF_LNG,
        current_cumulative_miles=100.0,
        current_speed_streak_s=float(LOCK_RELEASE_SPEED_STREAK_S),
        now=datetime.now(timezone.utc),
    )
    assert result is None


def test_C4_is_locked_returns_none_when_no_lock_exists(db_cur, test_driver_id):
    """C4: querying a non-existent lock returns None (fire is unblocked)."""
    result = is_locked(
        cur=db_cur,
        driver_id=test_driver_id,
        offer_id='C4_never_locked',
        pudo_type='dropoff',
        current_lat=_REF_LAT,
        current_lng=_REF_LNG,
        current_cumulative_miles=100.0,
        current_speed_streak_s=0.0,
        now=datetime.now(timezone.utc),
    )
    assert result is None


def test_C5_release_lock_idempotent(db_cur, test_driver_id):
    """C5: release_lock returns True on real deletion, False on no-op."""
    fired_at = datetime.now(timezone.utc)
    acquire_lock(
        cur=db_cur,
        driver_id=test_driver_id,
        offer_id='C5_offer',
        pudo_type='pickup',
        fired_at=fired_at,
        fired_lat=_REF_LAT,
        fired_lng=_REF_LNG,
        fired_cumulative_miles=100.0,
    )
    # First release: row existed, must return True.
    assert release_lock(
        cur=db_cur,
        driver_id=test_driver_id,
        offer_id='C5_offer',
        pudo_type='pickup',
    ) is True
    # Second release: row already gone, must return False (no error).
    assert release_lock(
        cur=db_cur,
        driver_id=test_driver_id,
        offer_id='C5_offer',
        pudo_type='pickup',
    ) is False


def test_C6_lock_context_carries_full_forensic_payload(db_cur, test_driver_id):
    """C6: LockContext fields match the §XVI.G forensic payload spec.

    Gemini 2026-05-26: when is_locked returns True, the calling
    context must receive the complete payload structure for the
    lock_suppressed PDC row. Specifically:
      - locked_offer_id, locked_pudo_type
      - lock_age_s (seconds since fired_at)
      - current_distance_delta_m, current_speed_streak_s
      - target_distance_delta_m (152.4), target_speed_streak_s (10)

    Without this, Sprint C re-validation cannot measure the lock's
    real-world behavior.
    """
    fired_at = datetime.now(timezone.utc) - timedelta(seconds=24)
    acquire_lock(
        cur=db_cur,
        driver_id=test_driver_id,
        offer_id='C6_offer',
        pudo_type='dropoff',
        fired_at=fired_at,
        fired_lat=_REF_LAT,
        fired_lng=_REF_LNG,
        fired_cumulative_miles=100.0,
    )
    # Driver moved 50m roughly east (longitude shift of ~0.00052 deg).
    # 50m is well under the 152.4m release threshold => lock holds.
    result = is_locked(
        cur=db_cur,
        driver_id=test_driver_id,
        offer_id='C6_offer',
        pudo_type='dropoff',
        current_lat=_REF_LAT,
        current_lng=_REF_LNG + 0.00052,
        current_cumulative_miles=100.03,
        current_speed_streak_s=3.5,
        now=datetime.now(timezone.utc),
    )
    assert result is not None
    assert isinstance(result, LockContext)
    assert result.locked_offer_id == 'C6_offer'
    assert result.locked_pudo_type == 'dropoff'
    # lock_age_s should be ~24s. Allow 2s tolerance for execution drift.
    assert 22 <= result.lock_age_s <= 26
    # current_distance_delta_m should reflect the ~50m driver shift.
    # Tolerance is wide because the exact distance depends on
    # app_private.distance_miles's conversion; we just verify "non-zero
    # and within the horizon."
    assert 30 < result.current_distance_delta_m < 80
    assert result.current_distance_delta_m < LOCK_RELEASE_DISTANCE_M
    assert result.current_speed_streak_s == pytest.approx(3.5)
    assert result.target_distance_delta_m == pytest.approx(LOCK_RELEASE_DISTANCE_M)
    assert result.target_speed_streak_s == LOCK_RELEASE_SPEED_STREAK_S
