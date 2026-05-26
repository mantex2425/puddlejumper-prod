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
Sprint: A Step 2 (TDD baseline). Implementation lands in Step 3.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


# §XVI.G release-condition constants. Tuning these requires a canonical
# rule amendment per the Sprint A design discussion (Decision D). See
# CANONICAL_RULES.md §XVI.G.
LOCK_RELEASE_DISTANCE_M: float = 152.4  # 500 feet
LOCK_RELEASE_SPEED_MPH: float = 5.0
LOCK_RELEASE_SPEED_STREAK_S: int = 10


@dataclass(frozen=True)
class LockContext:
    """Forensic payload returned by ``is_locked`` when a lock is active.

    Carries enough state for the caller to write a ``lock_suppressed``
    PDC row with full release-metrics visibility per §XVI.J (every
    detected stop produces a row, including suppressed ones).

    All fields are required; the dataclass is frozen to enforce
    immutability across the heartbeat boundary.
    """

    locked_offer_id: str
    locked_pudo_type: str  # 'pickup' | 'dropoff'
    lock_age_s: float
    current_distance_delta_m: float
    current_speed_streak_s: float
    target_distance_delta_m: float = LOCK_RELEASE_DISTANCE_M
    target_speed_streak_s: int = LOCK_RELEASE_SPEED_STREAK_S


def acquire_lock(
    cur,
    driver_id: str,
    offer_id: str,
    pudo_type: str,
    fired_at: datetime,
    fired_lat: float,
    fired_lng: float,
    fired_cumulative_miles: float,
) -> None:
    """Engage the §XVI.G transaction lock for a fired (offer, leg).

    Idempotent by design: ``INSERT ... ON CONFLICT (driver_id, offer_id,
    pudo_type) DO NOTHING``. If a lock already exists for the same tuple
    (which would only happen if the matcher's emission contract was
    violated by a race), the existing lock state is preserved — its
    original spatial anchor must remain authoritative for the release
    math (per Gemini Decision A 2026-05-26: latching lock anchors to
    the first physical event).

    Args:
        cur: psycopg2 cursor. Caller owns transaction lifecycle.
        driver_id: Firebase UID of the locked driver.
        offer_id: Offer being locked.
        pudo_type: 'pickup' or 'dropoff'. CHECK constraint enforces
            this at the storage boundary.
        fired_at: UTC timestamp of the fire (timezone-aware).
        fired_lat: Driver latitude at fire moment.
        fired_lng: Driver longitude at fire moment.
        fired_cumulative_miles: Driver odometer at fire moment. Backup
            spatial-release signal when GPS noise makes the distance
            computation unreliable.

    Raises:
        NotImplementedError: until Sprint A Step 3 lands.
    """
    raise NotImplementedError("§XVI.G acquire_lock — Sprint A Step 3")


def is_locked(
    cur,
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

    Lock release conditions, evaluated against the stored fire anchor:
      * Spatial: app_private.distance_miles(fired_lat, fired_lng,
        current_lat, current_lng) >= LOCK_RELEASE_DISTANCE_M, OR
      * Temporal: current_speed_streak_s >= LOCK_RELEASE_SPEED_STREAK_S

    If either release condition is met, the row is deleted (lock
    auto-releases) and this function returns ``None``.

    If neither release condition is met, returns a ``LockContext``
    populated with the diagnostic state the caller needs to write a
    ``lock_suppressed`` PDC row.

    If no lock row exists for the tuple, returns ``None`` immediately
    (fire is unblocked).

    Args:
        cur: psycopg2 cursor.
        driver_id, offer_id, pudo_type: composite key into
            driver_trip_locks.
        current_lat, current_lng: driver's live GPS position.
        current_cumulative_miles: driver's live odometer reading.
        current_speed_streak_s: contiguous seconds above
            LOCK_RELEASE_SPEED_MPH. Caller computes from heartbeat
            history.
        now: UTC current time (timezone-aware). Used to compute
            ``lock_age_s`` for the forensic payload.

    Returns:
        None if not locked (fire away). LockContext if locked (suppress
        the fire, write a lock_suppressed PDC row with this context's
        fields).

    Raises:
        NotImplementedError: until Sprint A Step 3 lands.
    """
    raise NotImplementedError("§XVI.G is_locked — Sprint A Step 3")


def release_lock(
    cur,
    driver_id: str,
    offer_id: str,
    pudo_type: str,
) -> bool:
    """Explicitly delete a lock row for (driver, offer, leg).

    Called by the matcher when an offer leaves the live queue
    (actual_dropoff_at stamped) or by ``is_locked`` itself when a
    release condition is met.

    Idempotent: deleting a non-existent row is a no-op (returns False).

    Args:
        cur: psycopg2 cursor.
        driver_id, offer_id, pudo_type: composite key.

    Returns:
        True if a row was deleted, False if no row existed.

    Raises:
        NotImplementedError: until Sprint A Step 3 lands.
    """
    raise NotImplementedError("§XVI.G release_lock — Sprint A Step 3")
