"""§XIV.H regression — re-anchored distance gate + odometer staleness gate.

Canonical fixture: IAH offer 7975 from 2026-05-19 drive (Texas Medical Center
→ IAH Terminal E), the Bug B'-1 miss that motivated P10/P11.

Verifies four behavioral cases against the actual production predicate via
real Postgres (§XIV.J test floor — no mocks for SQL behavior).

  Case 1: IAH-shape ride survives the new predicate (re-anchored distance
          + driver actively moving) — the headline fix
  Case 2: distance over the re-anchored budget kills the offer (sanity check
          on the upper bound)
  Case 3: 90-min odometer staleness kills the offer (the abandonment gate fires)
  Case 4: 10-min staleness leaves the offer alive (the gate is permissive
          below threshold)

Per §VII the predicate is a pure function of its inputs; these tests
parameterize the inputs deterministically and assert the outputs.
"""
import datetime
import pytest
from datetime import timezone

from driver_queue import LIVE_OFFER_PREDICATE_SQL, live_offer_predicate_params


# IAH 7975 canonical fixture values (from offer_history + heartbeat_log
# forensics on 2026-05-19 drive)
IAH_CREATED_AT = datetime.datetime(2026, 5, 19, 9, 29, 20, tzinfo=timezone.utc)
IAH_PICKUP_AT = datetime.datetime(2026, 5, 19, 9, 52, 13, tzinfo=timezone.utc)
IAH_ARREST_AT = datetime.datetime(2026, 5, 19, 10, 35, 45, tzinfo=timezone.utc)

IAH_PICKUP_MINUTES = 12
IAH_TRIP_MINUTES = 34
IAH_PICKUP_MILES = 5.6
IAH_TRIP_MILES = 27.1
IAH_MILES_AT_OFFER_RECEIPT = 40.43
IAH_EXPECTED_DROPOFF_DISTANCE = 80.12  # = 53.02 (pickup odometer) + 27.1 (trip)
IAH_ARREST_CUMULATIVE_MILES = 83.30


@pytest.fixture
def iah_offer_id(db_cur):
    """Seed the IAH 7975 fixture into offer_history + decision_log.

    Returns the offer_history.id. SAVEPOINT isolation per conftest's
    db_cur fixture rolls this back after the test.
    """
    # Need a decision_log row first (FK). Auto-increment sequences
    # assign IDs; capture via RETURNING.
    driver_id = "test_driver_iah_p10"

    db_cur.execute(
        """
        INSERT INTO app_private.decision_log (driver_id, created_at)
        VALUES (%s, %s)
        RETURNING id
        """,
        (driver_id, IAH_CREATED_AT),
    )
    decision_log_id = db_cur.fetchone()["id"]

    db_cur.execute(
        """
        INSERT INTO app_private.offer_history (
            decision_log_id, created_at,
            pickup_minutes, trip_minutes, pickup_miles, trip_miles,
            miles_at_offer_receipt,
            actual_pickup_at, actual_dropoff_at,
            expected_dropoff_distance
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (decision_log_id, IAH_CREATED_AT,
         IAH_PICKUP_MINUTES, IAH_TRIP_MINUTES,
         IAH_PICKUP_MILES, IAH_TRIP_MILES,
         IAH_MILES_AT_OFFER_RECEIPT,
         IAH_PICKUP_AT, None,
         IAH_EXPECTED_DROPOFF_DISTANCE),
    )

    return db_cur.fetchone()["id"]


def _eval_predicate(cur, offer_id, current_cumulative_miles, reference_time,
                    last_odometer_move_at):
    """Evaluate LIVE_OFFER_PREDICATE_SQL against a specific offer row.

    Returns True if the predicate evaluates to true (offer alive), False
    otherwise.
    """
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


class TestIAHReanchorAndStaleness:

    def test_case_1_iah_survives_with_reanchor(self, db_cur, iah_offer_id):
        """Case 1 (HEADLINE): the IAH 7975 ride survives the new predicate.

        Under the old predicate:
          threshold = 40.43 + (5.6+27.1)*1.25 = 81.305
          83.30 > 81.305 → distance axis FALSE → predicate FALSE → REAPED

        Under the new predicate (re-anchored):
          threshold = 80.12 + 27.1*0.25 = 86.895
          83.30 < 86.895 → distance axis TRUE
          Driver actively moving (effective_last_move = arrest_time)
            → staleness gate permissive
          → predicate TRUE → SURVIVES → matcher engages → §XVII fires
        """
        alive = _eval_predicate(
            db_cur, iah_offer_id,
            current_cumulative_miles=IAH_ARREST_CUMULATIVE_MILES,
            reference_time=IAH_ARREST_AT,
            last_odometer_move_at=IAH_ARREST_AT,  # driver moving on this tick
        )
        assert alive is True, (
            "IAH 7975 must survive the new predicate. This is the Bug B'-1 "
            "fix; if this fails, the re-anchor formula is wrong."
        )

    def test_case_2_distance_over_budget_kills(self, db_cur, iah_offer_id):
        """Case 2: well over the re-anchored budget reaps the offer (sanity).

        Driver odometer = 88.00 mi > 86.895 budget. Expected: FALSE.
        """
        alive = _eval_predicate(
            db_cur, iah_offer_id,
            current_cumulative_miles=88.00,
            reference_time=IAH_ARREST_AT,
            last_odometer_move_at=IAH_ARREST_AT,
        )
        assert alive is False, (
            "Distance over the re-anchored budget must kill the offer; "
            "if this fails, the upper-bound check is broken."
        )

    def test_case_3_long_staleness_kills(self, db_cur, iah_offer_id):
        """Case 3: 90-min odometer freeze reaps the offer (staleness gate fires).

        Driver hasn't moved in 90 min. last_odometer_move_at = 9:30:00.
        reference_time = 11:00:00 → 90 min stale > 30 min threshold.
        Distance is within budget (83.30 < 86.895) — distance gate would
        keep it alive, but staleness gate overrides.
        Offer created at 9:29:20, freeze began at 9:30:00 → 1.5 hr ago
        → offer existed before freeze, so new-offer exemption doesn't apply.
        Expected: FALSE (staleness gate fires).
        """
        alive = _eval_predicate(
            db_cur, iah_offer_id,
            current_cumulative_miles=IAH_ARREST_CUMULATIVE_MILES,
            reference_time=datetime.datetime(2026, 5, 19, 11, 0, 0, tzinfo=timezone.utc),
            last_odometer_move_at=datetime.datetime(2026, 5, 19, 9, 30, 0, tzinfo=timezone.utc),
        )
        assert alive is False, (
            "90 min of odometer freeze must reap the offer via staleness "
            "gate. If this fails, the staleness clause is broken."
        )

    def test_case_4_short_staleness_alive(self, db_cur, iah_offer_id):
        """Case 4: 10-min freeze leaves the offer alive (gate permissive).

        Driver hasn't moved in 10 min. Below the 30-min threshold → staleness
        gate permissive. Distance is within budget. Expected: TRUE.
        """
        alive = _eval_predicate(
            db_cur, iah_offer_id,
            current_cumulative_miles=IAH_ARREST_CUMULATIVE_MILES,
            reference_time=datetime.datetime(2026, 5, 19, 11, 0, 0, tzinfo=timezone.utc),
            last_odometer_move_at=datetime.datetime(2026, 5, 19, 10, 50, 0, tzinfo=timezone.utc),
        )
        assert alive is True, (
            "10 min of freeze (below 30 min threshold) must NOT reap. "
            "If this fails, the gate is too aggressive."
        )

    def test_case_5_pre_pickup_fire_uses_pickup_band(self, db_cur):
        """Case 5 (Step 6, ERRATUM §4): pre-pickup-fire offers use the
        PICKUP-leg band, centered on expected_pickup_distance with tolerance
        max(0.15*pickup_miles, 2.0). The receipt-anchor + 1.25-buffer formula
        is retired (the buffer was a phantom, ERRATUM §2).

        IAH pickup odometer anchor = 53.02 (= 80.12 dropoff - 27.1 trip).
        Pickup band upper edge = 53.02 + max(0.15*5.6, 2.0)
                               = 53.02 + 2.0 = 55.02  (0.84 < 2.0 floor).
        """
        driver_id = "test_driver_pre_pickup_p10"
        cur = db_cur

        cur.execute(
            """
            INSERT INTO app_private.decision_log (driver_id, created_at)
            VALUES (%s, %s)
            RETURNING id
            """,
            (driver_id, IAH_CREATED_AT),
        )
        decision_log_id = cur.fetchone()["id"]

        cur.execute(
            """
            INSERT INTO app_private.offer_history (
                decision_log_id, created_at,
                pickup_minutes, trip_minutes, pickup_miles, trip_miles,
                miles_at_offer_receipt,
                actual_pickup_at, actual_dropoff_at,
                expected_pickup_distance, expected_dropoff_distance
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (decision_log_id, IAH_CREATED_AT,
             IAH_PICKUP_MINUTES, IAH_TRIP_MINUTES,
             IAH_PICKUP_MILES, IAH_TRIP_MILES,
             IAH_MILES_AT_OFFER_RECEIPT,
             None, None,
             53.02,    # expected_pickup_distance (IAH pickup odometer anchor)
             None),    # expected_dropoff_distance NULL pre-pickup (correct)
        )
        offer_id = cur.fetchone()["id"]

        # Pickup band upper edge = 55.02. Driver at 55.0 -> within band -> alive.
        alive = _eval_predicate(
            cur, offer_id,
            current_cumulative_miles=55.00,
            reference_time=IAH_ARREST_AT,
            last_odometer_move_at=IAH_ARREST_AT,
        )
        assert alive is True, (
            "Pre-pickup offer within the pickup band (odo 55.0 <= edge 55.02) "
            "must remain alive."
        )

        # Driver at 56.0 -> over the pickup band edge (55.02) -> dead.
        alive = _eval_predicate(
            cur, offer_id,
            current_cumulative_miles=56.00,
            reference_time=IAH_ARREST_AT,
            last_odometer_move_at=IAH_ARREST_AT,
        )
        assert alive is False, (
            "Pre-pickup offer past the pickup band upper edge (odo 56.0 > "
            "55.02) must die (overshot the pickup leg)."
        )
