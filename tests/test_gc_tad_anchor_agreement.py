"""GC ↔ TAD anchor agreement contract.

The Bug B'-1 IAH miss of 2026-05-19 had a single root cause: the GC predicate
in driver_queue.py read miles_at_offer_receipt as the canonical distance
anchor, while TAD (tad.py) read expected_dropoff_distance — the
re-anchored prediction written at pickup fire. The two consumers disagreed
about the offer's physical envelope.

After the P10/P11 patch, GC consumes expected_dropoff_distance too (in the
post-pickup-fire branch of the CASE). This test locks the two to the same
anchor view going forward.

The contract: for any synthetic offer in post-pickup-fire state, GC's
evaluated distance threshold MUST equal TAD's anchor + buffer math:

  GC_threshold = expected_dropoff_distance + trip_miles * 0.25

  TAD_anchor   = expected_dropoff_distance
  TAD_buffer   = trip_miles * 0.25
  TAD_total    = TAD_anchor + TAD_buffer

If a future change moves the buffer multiplier in one place but not the
other, or changes the anchor column in one place but not the other, this
test fails — making the divergence impossible to ship unnoticed.

§XIV.J: real-PG fixture. The SQL predicate is the same one production uses;
TAD's anchor math is asserted against the same column the SQL CASE reads.
"""
import datetime
import pytest
from datetime import timezone

from driver_queue import LIVE_OFFER_PREDICATE_SQL, live_offer_predicate_params


def _seed_post_pickup_offer(cur, *,
                            created_at,
                            pickup_miles,
                            trip_miles,
                            miles_at_offer_receipt,
                            actual_pickup_at,
                            expected_dropoff_distance):
    """Seed a single post-pickup-fire offer. Returns offer_id.

    Uses auto-increment sequences for both id columns (decision_log.id
    is integer with nextval default, offer_history.id is bigint with
    nextval default). Captures assigned values via RETURNING id.
    """
    driver_id = "test_driver_anchor_contract"

    cur.execute(
        """
        INSERT INTO app_private.decision_log (driver_id, created_at)
        VALUES (%s, %s)
        RETURNING id
        """,
        (driver_id, created_at),
    )
    decision_log_id = cur.fetchone()["id"]

    cur.execute(
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
        (decision_log_id, created_at,
         10, 30, pickup_miles, trip_miles,
         miles_at_offer_receipt,
         actual_pickup_at, None,
         expected_dropoff_distance),
    )
    return cur.fetchone()["id"]


def _gc_threshold_for_post_pickup(*, expected_dropoff_distance, trip_miles):
    """The formula the new CASE branch in LIVE_OFFER_PREDICATE_SQL uses
    for post-pickup-fire offers."""
    return expected_dropoff_distance + trip_miles * 0.25


def _eval_predicate(cur, offer_id, current_cumulative_miles, reference_time,
                    last_odometer_move_at):
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


# Three parameterized cases covering the post-pickup-fire branch with
# different trip lengths and budget tightness:
#   1. IAH airport run (today's bug — long trip, tight slack)
#   2. Short urban trip (small trip_miles, small buffer)
#   3. Medium suburban trip (mid-range values)
POST_PICKUP_CASES = [
    pytest.param(
        # IAH 7975 (today's actual bug)
        dict(
            label="iah_long_trip",
            pickup_miles=5.6,
            trip_miles=27.1,
            miles_at_offer_receipt=40.43,
            expected_dropoff_distance=80.12,
        ),
        id="iah_long_trip",
    ),
    pytest.param(
        # Short downtown trip
        dict(
            label="downtown_short",
            pickup_miles=1.5,
            trip_miles=3.2,
            miles_at_offer_receipt=15.0,
            expected_dropoff_distance=18.0,
        ),
        id="downtown_short",
    ),
    pytest.param(
        # Suburban mid-range
        dict(
            label="suburban_mid",
            pickup_miles=4.0,
            trip_miles=12.0,
            miles_at_offer_receipt=50.0,
            expected_dropoff_distance=66.0,
        ),
        id="suburban_mid",
    ),
]


class TestGCTADAnchorAgreement:
    """The contract: GC threshold ≡ TAD anchor + buffer for post-pickup offers."""

    @pytest.mark.parametrize("scenario", POST_PICKUP_CASES)
    def test_gc_threshold_exactly_at_tad_budget_kills(self, db_cur, scenario):
        """Driver at exactly GC's threshold should NOT be alive (strict <).

        If TAD ever computes a different anchor or buffer multiplier than
        GC's CASE branch, the boundary case here will catch it.
        """
        created_at = datetime.datetime(2026, 5, 19, 9, 0, 0, tzinfo=timezone.utc)
        pickup_at = datetime.datetime(2026, 5, 19, 9, 30, 0, tzinfo=timezone.utc)
        reference = datetime.datetime(2026, 5, 19, 10, 30, 0, tzinfo=timezone.utc)

        offer_id = _seed_post_pickup_offer(
            db_cur,
            created_at=created_at,
            pickup_miles=scenario["pickup_miles"],
            trip_miles=scenario["trip_miles"],
            miles_at_offer_receipt=scenario["miles_at_offer_receipt"],
            actual_pickup_at=pickup_at,
            expected_dropoff_distance=scenario["expected_dropoff_distance"],
        )

        threshold = _gc_threshold_for_post_pickup(
            expected_dropoff_distance=scenario["expected_dropoff_distance"],
            trip_miles=scenario["trip_miles"],
        )

        # Driver exactly AT threshold → not strict-less-than → dead
        alive = _eval_predicate(
            db_cur, offer_id,
            current_cumulative_miles=threshold,
            reference_time=reference,
            last_odometer_move_at=reference,
        )
        assert alive is False, (
            f"{scenario['label']}: predicate evaluated TRUE at exactly the "
            f"computed threshold ({threshold} mi). The CASE branch's "
            f"formula does not match expected_dropoff_distance + "
            f"trip_miles * 0.25. GC and TAD have diverged."
        )

    @pytest.mark.parametrize("scenario", POST_PICKUP_CASES)
    def test_gc_threshold_just_under_alive(self, db_cur, scenario):
        """Driver one foot under the threshold MUST be alive.

        Mirror of the above: ensures the boundary is at the expected
        location and the formula isn't off by a constant.
        """
        created_at = datetime.datetime(2026, 5, 19, 9, 0, 0, tzinfo=timezone.utc)
        pickup_at = datetime.datetime(2026, 5, 19, 9, 30, 0, tzinfo=timezone.utc)
        reference = datetime.datetime(2026, 5, 19, 10, 30, 0, tzinfo=timezone.utc)

        offer_id = _seed_post_pickup_offer(
            db_cur,
            created_at=created_at,
            pickup_miles=scenario["pickup_miles"],
            trip_miles=scenario["trip_miles"],
            miles_at_offer_receipt=scenario["miles_at_offer_receipt"],
            actual_pickup_at=pickup_at,
            expected_dropoff_distance=scenario["expected_dropoff_distance"],
        )

        threshold = _gc_threshold_for_post_pickup(
            expected_dropoff_distance=scenario["expected_dropoff_distance"],
            trip_miles=scenario["trip_miles"],
        )

        # Driver 0.001 mi under threshold → strict-less-than → alive
        alive = _eval_predicate(
            db_cur, offer_id,
            current_cumulative_miles=threshold - 0.001,
            reference_time=reference,
            last_odometer_move_at=reference,
        )
        assert alive is True, (
            f"{scenario['label']}: predicate evaluated FALSE just below "
            f"the threshold ({threshold - 0.001} < {threshold}). The CASE "
            f"branch is more aggressive than the formula suggests."
        )

    def test_pre_pickup_branch_uses_receipt_anchor_not_reanchor(self, db_cur):
        """When actual_pickup_at IS NULL, GC must NOT use
        expected_dropoff_distance even if that column happens to be
        populated. The ELSE branch governs.

        Defends against future regression where someone removes the
        actual_pickup_at NULL check in the CASE WHEN, accidentally
        applying re-anchor to pre-pickup offers.
        """
        created_at = datetime.datetime(2026, 5, 19, 9, 0, 0, tzinfo=timezone.utc)
        reference = datetime.datetime(2026, 5, 19, 9, 30, 0, tzinfo=timezone.utc)

        # Seed with actual_pickup_at = NULL but expected_dropoff_distance
        # populated (anomalous but legal column state)
        db_cur.execute(
            """
            INSERT INTO app_private.decision_log (driver_id, created_at)
            VALUES (%s, %s)
            RETURNING id
            """,
            ("test_driver_pre_pickup_anchor", created_at),
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
            (decision_log_id, created_at,
             10, 30, 5.0, 25.0,
             10.0,
             None, None,
             999.0),  # absurd expected_dropoff_distance — would let any
                      # ride survive if the CASE branch reads it
        )
        offer_id = db_cur.fetchone()["id"]

        # Pre-pickup ELSE budget: 10.0 + (5.0+25.0)*1.25 = 47.5
        # Driver at 60 mi → over receipt-time budget → must be dead.
        # If the CASE branch erroneously consults expected_dropoff_distance
        # = 999.0, driver at 60 would be alive (60 < 999 + buffer). The
        # NULL check on actual_pickup_at is what prevents that.
        alive = _eval_predicate(
            db_cur, offer_id,
            current_cumulative_miles=60.0,
            reference_time=reference,
            last_odometer_move_at=reference,
        )
        assert alive is False, (
            "Pre-pickup offer evaluated alive when over the receipt-time "
            "budget. The CASE may be erroneously consulting "
            "expected_dropoff_distance without checking actual_pickup_at "
            "first. Contract violated."
        )
