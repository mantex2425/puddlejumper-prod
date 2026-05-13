"""Houston Playback live-DB regression test for _detect_lost_mode.

Replays the 2026-05-12 afternoon drive against the real offer_history
rows for driver UjT1hE9eBXh2q95aSZYOkzDJ8lo1. Anchors the playback at
synthetic timestamps and asserts the predicate behaves correctly at
each inflection point of the drive.

WHY THIS TEST EXISTS
====================

On 2026-05-12 the production driver_heartbeat code shipped a refactor
of _detect_lost_mode that replaced an `actual_pickup_at IS NULL` proxy
+ wall-clock 2-hour window with the canonical LIVE_OFFER_PREDICATE_SQL.
The old rule had two failure modes the new rule eliminates:

  HOUSTON MISS  AAI missed pickup observation but dropoff fired
                cleanly. Old rule's `actual_pickup_at IS NULL` clause
                treated the offer as a permanent ghost for 2 hours
                AFTER dropoff fired, poisoning every subsequent
                heartbeat with lost_mode=true.

  CALHOUN ZOMBIE  Pickup fires but the dropoff address Uber gave
                  doesn't exist where navigation takes you. AAI
                  never sees dropoff. Old rule's `actual_pickup_at
                  IS NULL` clause excluded the offer entirely —
                  wrong direction; Calhoun IS the ghost the rule
                  is meant to catch.

The new rule uses the predicate's first clause (`actual_dropoff_at IS
NULL`) so Houston Miss exits the live set on dropoff fire automatically.
Pickup-fired-no-dropoff offers (Calhoun) stay alive until time/distance
horizons blow — physics terminates the narrative, not fire state.

ROW 10 IS THE KILL SHOT
=======================

Same timestamp/odometer as row 9, but synthetic empty queue. Encodes
the bug: 7852 is predicate-alive (Calhoun pattern, pickup fired, no
dropoff, horizons not blown) but the heartbeat queue doesn't include
it. Old rule said False (pickup fired = not orphan). New rule must
say True (predicate-alive + not in queue = orphan).

CANONICAL HORIZON TABLE (locked in recon_b7 against real DB rows)
=================================================================

  offer  accepted UTC   dropoff fires  time horizon  time cutoff UTC  dist hzn  anchor odo  dist cutoff
  7848   18:06:28      18:38:48 OK    46.25 min     (irrelevant)     30.88 mi  0.27        (irrelevant)
  7850   18:35:15      never          17.50 min     18:52:45         4.75 mi   22.61       27.36
  7852   18:47:16      never          53.75 min     19:41:01         28.38 mi  27.30       55.68
  7853   19:36:09      20:14:20 OK    48.75 min     (irrelevant)     32.38 mi  52.60       (irrelevant)
  7854   20:10:54      never          27.50 min     20:38:24         10.00 mi  77.05       87.05

DEV-MODE NOISE EXCLUDED
=======================

Offers 7846 and 7847 (17:03 and 18:04 UTC) are dev exercises Andrew ran
through the system before driving, not real trips. Playback anchors at
18:06:00 UTC (just before 7848 accepts), treating 7848 as the first
real offer of the drive. The dev rows still exist in offer_history but
will not be in any predicate window the playback timestamps reach.

THE QUEUE PARAMETER
===================

`queue_offer_ids` in each test row is SYNTHESIZED, not extracted from
production heartbeat logs. The test asserts the predicate's correctness
given a queue, not the heartbeat handler's correctness in building the
queue. Queue contents reflect logical expectations: an accepted offer
is in queue until it fires dropoff. Row 10 deliberately diverges from
this convention (empty queue while 7852 is alive) to encode the bug
surface.
"""

import datetime

import pytest

from driver_heartbeat import _detect_lost_mode


REAL_DRIVER_ID = "UjT1hE9eBXh2q95aSZYOkzDJ8lo1"


# Each row: (utc_iso, scenario_label, queue_offer_ids, expected_lost_mode)
PLAYBACK_CASES = [
    # ----- Row 1 -----
    (
        "2026-05-12T18:06:00+00:00",
        "pre-7848 accept: no real offers yet (dev offers 7846/7847 horizon-blown)",
        [],
        False,
    ),
    # ----- Row 2 -----
    (
        "2026-05-12T18:06:30+00:00",
        "7848 just accepted, in queue",
        ["7848"],
        False,
    ),
    # ----- Row 3 -----
    (
        "2026-05-12T18:35:30+00:00",
        "7850 accepted, 7848 still pre-pickup, both in queue",
        ["7848", "7850"],
        False,
    ),
    # ----- Row 4 -----
    (
        "2026-05-12T18:38:48+00:00",
        "7848 dropoff fires -> exits predicate via actual_dropoff_at IS NULL clause",
        ["7850"],
        False,
    ),
    # ----- Row 5 -----
    (
        "2026-05-12T18:47:30+00:00",
        "7852 accepted, 7850 still in queue",
        ["7850", "7852"],
        False,
    ),
    # ----- Row 6 -----
    (
        "2026-05-12T18:52:46+00:00",
        "7850 time-axis horizon blown (distance axis still live, but predicate requires both)",
        ["7852"],
        False,
    ),
    # ----- Row 7 -----
    (
        "2026-05-12T18:55:42+00:00",
        "7852 pickup fires (Calhoun pattern: pickup observed, dropoff still pending)",
        ["7852"],
        False,
    ),
    # ----- Row 8 -----
    (
        "2026-05-12T19:36:30+00:00",
        "7853 accepted while 7852 still pickup-fired-no-dropoff",
        ["7852", "7853"],
        False,
    ),
    # ----- Row 9 -----
    (
        "2026-05-12T20:14:20+00:00",
        "7853 dropoff fires -> exits predicate; 7852 dead by horizon; 7854 alive in queue",
        ["7854"],
        False,
    ),
    # ----- Row 10 -----  *** KILL SHOT ***
    (
        "2026-05-12T19:00:00+00:00",
        "KILL SHOT: 7852 mid-Calhoun-window (pickup-fired 18:55:42, horizon 19:41:01), "
        "synthetic empty queue -> orphan detected",
        [],  # <-- the bug surface: empty queue while Calhoun ghost is alive
        True,
    ),
    # ----- Row 11 -----
    (
        "2026-05-12T20:35:00+00:00",
        "Calhoun self-resolves: 7852 dead by both horizons; 7854 alive in queue",
        ["7854"],
        False,
    ),
]


def _read_odometer_at(cur, driver_id, ref_time):
    """Read the odometer reading from the pudo_decision_context heartbeat
    nearest to ref_time. Extracts the 'odo' value from the first offer's
    pickup branch in odometer_gate_result.

    The odo value is the driver's current cumulative miles at heartbeat
    time, identical across all offer branches in the blob (it's a
    driver-level reading, not per-offer).

    Returns None if no heartbeat near ref_time has a populated
    odometer_gate_result. The live_offer_predicate_params contract
    treats None as "no distance constraint" (distance axis short-circuits
    to TRUE), which is the correct semantic for the pre-acceptance phase.
    """
    cur.execute(
        """
        SELECT odometer_gate_result
        FROM app_private.pudo_decision_context
        WHERE driver_id = %s
          AND created_at BETWEEN %s::timestamptz - INTERVAL '30 seconds'
                             AND %s::timestamptz + INTERVAL '30 seconds'
          AND odometer_gate_result IS NOT NULL
          AND odometer_gate_result <> '{}'::jsonb
        ORDER BY ABS(EXTRACT(EPOCH FROM (created_at - %s::timestamptz)))
        LIMIT 1
        """,
        (driver_id, ref_time, ref_time, ref_time),
    )
    row = cur.fetchone()
    if row is None:
        return None

    blob = row['odometer_gate_result']  # RealDictCursor returns dict-like rows
    if not blob:
        return None

    # Take the first offer's pickup.odo — all offers share the same odo
    # value at any given heartbeat (driver-level reading).
    for offer_id, branches in blob.items():
        pickup = branches.get("pickup") or {}
        odo = pickup.get("odo")
        if odo is not None:
            return float(odo)

    return None


@pytest.mark.parametrize(
    "utc_iso,scenario,queue,expected",
    PLAYBACK_CASES,
    ids=[case[1][:60] for case in PLAYBACK_CASES],
)
def test_houston_playback(db_cur, utc_iso, scenario, queue, expected):
    """Replay one inflection point of the 2026-05-12 Houston drive.

    For each parametrized row, we:
      1. Convert utc_iso to a UTC datetime as the predicate's reference_time
      2. Read the real odometer from pudo_decision_context at that moment
      3. Call _detect_lost_mode against the live offer_history rows
      4. Assert the actual result matches the expected lost_mode boolean

    The assertion message includes scenario_label, ref_time, odo, and
    queue so a failure makes the "what was happening at that moment"
    immediately visible without re-reading the parametrization.
    """
    ref_time = datetime.datetime.fromisoformat(utc_iso)
    odo = _read_odometer_at(db_cur, REAL_DRIVER_ID, ref_time)

    actual = _detect_lost_mode(
        db_cur,
        REAL_DRIVER_ID,
        queue,
        odo,
        ref_time,
    )

    assert actual == expected, (
        f"\nHouston Playback row failed:\n"
        f"  scenario: {scenario}\n"
        f"  ref_time: {ref_time.isoformat()}\n"
        f"  odo:      {odo}\n"
        f"  queue:    {queue}\n"
        f"  expected: {expected}\n"
        f"  got:      {actual}\n"
    )
