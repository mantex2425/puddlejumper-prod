"""§XVI.G Transaction Lock — application API.

Implements the canonical Transaction Lock specified in CANONICAL_RULES.md
§XVI.G. The lock prevents same-leg refire on the same offer until the
driver moves 500ft from the fire location OR sustains speed_mph > 5 for
10 contiguous seconds.

State storage: app_private.driver_trip_locks (composite PK on
driver_id, offer_id, pudo_type). One row per locked (driver, offer, leg).
Independent rows protect against stacked-offer overwrite per Gemini
Sprint A design review.

Architectural placement: PLAN box per §VIII. This module owns the DAO
pattern for lock state; where_am_i.py imports it as an external,
coordinate-independent gate at the front of its emission pass.

Authors: Andrew + Claude (paired-programming) + Gemini (ratified 2026-05-26)
Sprint: A Step 3 (implementation). Turns Step 2's red baseline green.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional


# §XVI.G release-condition constants. Tuning these requires a canonical
# rule amendment per the Sprint A design discussion (Decision D). See
# CANONICAL_RULES.md §XVI.G.
LOCK_RELEASE_DISTANCE_M: float = 152.4  # 500 feet
LOCK_RELEASE_SPEED_MPH: float = 5.0
LOCK_RELEASE_SPEED_STREAK_S: int = 10


@dataclass(frozen=True)
class LockContext:
    """Forensic payload returned by is_locked when a lock is active.

    Carries enough state for the caller to write a lock_suppressed PDC
    row with full release-metrics visibility per §XVI.J (every detected
    stop produces a row, including suppressed ones).
    """

    locked_offer_id: str
    locked_pudo_type: str
    lock_age_s: float
    current_distance_delta_m: float
    current_speed_streak_s: float
    target_distance_delta_m: float = LOCK_RELEASE_DISTANCE_M
    target_speed_streak_s: int = LOCK_RELEASE_SPEED_STREAK_S


def acquire_lock(
    cur: Any,
    driver_id: str,
    offer_id: str,
    pudo_type: str,
    fired_at: datetime,
    fired_lat: float,
    fired_lng: float,
    fired_cumulative_miles: float,
) -> bool:
    """Engage the §XVI.G transaction lock for a fired (offer, leg).

    Idempotent: INSERT ... ON CONFLICT DO NOTHING. The latching-lock
    invariant requires that the original fire's spatial anchor remain
    authoritative for the release math, so a second call with drifted
    coordinates is a deliberate no-op (NOT an overwrite).

    psycopg2.errors.CheckViolation bubbles up unchanged when pudo_type
    is outside the canonical domain. Defense-in-depth surfaces routing
    bugs as exceptions rather than silent corruption.

    Returns True if a new row was inserted, False if the lock already
    existed for this composite key.
    """
    cur.execute(
        """
        INSERT INTO app_private.driver_trip_locks (
            driver_id, offer_id, pudo_type, fired_at,
            fired_lat, fired_lng, fired_cumulative_miles
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (driver_id, offer_id, pudo_type) DO NOTHING
        """,
        (
            driver_id, offer_id, pudo_type, fired_at,
            fired_lat, fired_lng, fired_cumulative_miles,
        ),
    )
    return cur.rowcount > 0


def is_locked(
    cur: Any,
    driver_id: str,
    offer_id: str,
    pudo_type: str,
    current_lat: float,
    current_lng: float,
    current_cumulative_miles: float,
    current_speed_streak_s: float,
    now: datetime,
) -> Optional[LockContext]:
    """Check whether a (driver, offer, leg) is currently §XVI.G-locked.

    Release conditions (evaluated together, not short-circuited):
      - Spatial: distance from fire anchor >= 152.4m (500ft)
      - Temporal: current_speed_streak_s >= 10s

    If either condition holds, the lock row is deleted and None is
    returned (fire is unblocked). If neither holds, returns a populated
    LockContext for the caller to record on the lock_suppressed PDC row.

    If no lock row exists for the tuple, returns None immediately.

    Distance math centralizes in Postgres via app_private.distance_miles
    per §I; the * 1609.344 converts miles to meters once at the boundary.
    """
    cur.execute(
        """
        SELECT
            EXTRACT(EPOCH FROM (%s - fired_at)) AS lock_age_s,
            app_private.distance_miles(
                fired_lat, fired_lng, %s, %s
            ) * 1609.344 AS distance_delta_m
        FROM app_private.driver_trip_locks
        WHERE driver_id = %s
          AND offer_id  = %s
          AND pudo_type = %s
        """,
        (now, current_lat, current_lng, driver_id, offer_id, pudo_type),
    )
    row = cur.fetchone()
    if row is None:
        return None

    lock_age_s = float(row["lock_age_s"])
    distance_delta_m = (
        float(row["distance_delta_m"])
        if row["distance_delta_m"] is not None
        else 0.0
    )

    # Evaluate both release axes without short-circuiting so the forensic
    # record can answer "which condition released first" even though the
    # function only needs to return at the OR boundary.
    released_by_distance = distance_delta_m >= LOCK_RELEASE_DISTANCE_M
    released_by_speed = current_speed_streak_s >= LOCK_RELEASE_SPEED_STREAK_S

    if released_by_distance or released_by_speed:
        release_lock(cur, driver_id, offer_id, pudo_type)
        return None

    return LockContext(
        locked_offer_id=offer_id,
        locked_pudo_type=pudo_type,
        lock_age_s=lock_age_s,
        current_distance_delta_m=distance_delta_m,
        current_speed_streak_s=current_speed_streak_s,
    )


def release_lock(
    cur: Any,
    driver_id: str,
    offer_id: str,
    pudo_type: str,
) -> bool:
    """Explicitly delete a lock row for (driver, offer, leg).

    Called by the matcher when an offer leaves the live queue
    (actual_dropoff_at stamped) or by is_locked itself when a release
    condition is met.

    Idempotent: deleting a non-existent row is a no-op (returns False).
    """
    cur.execute(
        """
        DELETE FROM app_private.driver_trip_locks
        WHERE driver_id = %s
          AND offer_id  = %s
          AND pudo_type = %s
        """,
        (driver_id, offer_id, pudo_type),
    )
    return cur.rowcount > 0
