"""GC ↔ TAD anchor agreement contract.

The Bug B'-1 IAH miss of 2026-05-19 had a single root cause: the GC predicate
in driver_queue.py read miles_at_offer_receipt as the canonical distance
anchor, while TAD (tad.py) read expected_dropoff_distance — the
re-anchored prediction written at pickup fire. The two consumers disagreed
about the offer's physical envelope.

After the P10/P11 patch, GC consumes expected_dropoff_distance too (in the
post-pickup-fire branch of the CASE). This test locks the two to the same
anchor view going forward.

The contract (Step 6, ERRATUM 2026-06-05): GC and TAD agree because both
read the SAME per-leg odometer band, centered on the canonical anchor with
a trip-relative tolerance. The old "+ trip_miles * 0.25" buffer was a
PHANTOM (ERRATUM §2) — TAD never used it. TAD's real dropoff gate is the
band, and GC's liveness predicate now uses the identical band:

  dropoff-leg UPPER EDGE = expected_dropoff_distance
                           + max(0.15 * trip_miles, NOISE_FLOOR_MI)

  (the band is |odo - expected_dropoff_distance| <= max(0.15*trip_miles, 2.0);
   liveness reaps on the UPPER edge, so an offer is live iff
   odo <= expected_dropoff_distance + tolerance.)

The single-owner band is pudo_types.odometer_band; this file asserts GC's
SQL agrees with it. If a future change moves the tolerance or anchor in one
place but not the other, this test fails — making divergence impossible to
ship unnoticed.

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
    """The dropoff-leg band UPPER EDGE the liveness predicate uses for
    post-pickup-fire offers (Step 6, ERRATUM §4): the offer is live iff
    odometer <= expected_dropoff_distance + max(0.15*trip_miles, 2.0).

    Mirrors pudo_types.odometer_band's dropoff-leg tolerance. NOISE_FLOOR_MI
    is 2.0 (ODOMETER_BAND_NOISE_FLOOR_MI)."""
    from pudo_types import ODOMETER_BAND_NOISE_FLOOR_MI, ODOMETER_BAND_TOLERANCE_PCT
    tolerance = max(ODOMETER_BAND_TOLERANCE_PCT * trip_miles,
                    ODOMETER_BAND_NOISE_FLOOR_MI)
    return expected_dropoff_distance + tolerance


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

        # New band uses an INCLUSIVE upper edge (odo <= edge -> live).
        # Driver just OVER the edge -> overshot -> dead.
        alive = _eval_predicate(
            db_cur, offer_id,
            current_cumulative_miles=threshold + 0.001,
            reference_time=reference,
            last_odometer_move_at=reference,
        )
        assert alive is False, (
            f"{scenario['label']}: predicate evaluated TRUE just OVER the "
            f"band upper edge ({threshold + 0.001} > {threshold}). The SQL "
            f"band does not match expected_dropoff_distance + "
            f"max(0.15*trip_miles, 2.0). GC and the primitive have diverged."
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

        # New band INCLUSIVE upper edge: driver exactly AT the edge -> alive.
        alive = _eval_predicate(
            db_cur, offer_id,
            current_cumulative_miles=threshold,
            reference_time=reference,
            last_odometer_move_at=reference,
        )
        assert alive is True, (
            f"{scenario['label']}: predicate evaluated FALSE at exactly the "
            f"band upper edge ({threshold}). The band's upper edge is "
            f"inclusive (odo <= edge -> live); the SQL is more aggressive "
            f"than the primitive."
        )

    def test_pickup_leg_band_ignores_dropoff_anchor(self, db_cur):
        """When actual_pickup_at IS NULL, the band must center on
        expected_pickup_distance and must NOT read expected_dropoff_distance
        even when that column is populated.

        Defends against the regression where the pickup branch wrongly
        consults the dropoff anchor (ERRATUM §4: the pickup branch references
        ONLY expected_pickup_distance). Seed a pickup-leg offer with a SANE
        expected_pickup_distance and an ABSURD expected_dropoff_distance, then
        position the driver beyond the pickup band's upper edge. If the band
        correctly uses the pickup anchor -> dead. If it wrongly reads the
        absurd dropoff anchor -> the driver would be well within that huge
        band -> wrongly alive.
        """
        created_at = datetime.datetime(2026, 5, 19, 9, 0, 0, tzinfo=timezone.utc)
        reference = datetime.datetime(2026, 5, 19, 9, 30, 0, tzinfo=timezone.utc)

        db_cur.execute(
            """
            INSERT INTO app_private.decision_log (driver_id, created_at)
            VALUES (%s, %s)
            RETURNING id
            """,
            ("test_driver_pickup_leg_anchor", created_at),
        )
        decision_log_id = db_cur.fetchone()["id"]
        db_cur.execute(
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
            (decision_log_id, created_at,
             10, 30, 5.0, 25.0,
             10.0,
             None, None,
             50.0,     # sane pickup anchor: band upper edge = 50 + max(0.15*5, 2)
                       #                                       = 50 + 2.0 = 52.0
             999.0),   # absurd dropoff anchor — must be IGNORED on the pickup leg
        )
        offer_id = db_cur.fetchone()["id"]

        # Pickup-leg band upper edge = 50.0 + max(0.15*5.0, 2.0) = 52.0.
        # Driver at 60 mi -> over the pickup edge -> dead.
        # If the band wrongly read expected_dropoff_distance=999.0, the driver
        # at 60 would be far within that band -> wrongly alive. So `dead` proves
        # the pickup branch ignores the dropoff anchor.
        alive = _eval_predicate(
            db_cur, offer_id,
            current_cumulative_miles=60.0,
            reference_time=reference,
            last_odometer_move_at=reference,
        )
        assert alive is False, (
            "Pickup-leg offer evaluated alive past its pickup-band edge "
            "(52.0 mi). The band may be wrongly consulting "
            "expected_dropoff_distance (999.0) instead of "
            "expected_pickup_distance. ERRATUM §4 contract violated."
        )
