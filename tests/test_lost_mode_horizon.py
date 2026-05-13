"""Horizon-based lost_mode behavioral spec.

Canonical behavioral cases for the post-2026-05-12 _detect_lost_mode
implementation. The rule is PHYSICS, not state proxies:

    An offer triggers lost_mode iff
      accepted
      AND not in the active queue
      AND still inside its time/distance horizon

Pickup and dropoff fire state are NOT inputs to the rule.
LIVE_OFFER_PREDICATE_SQL is the contract.

This test class is a SPEC. The five tests are stubbed pending
fixture wiring — the conftest in tests/ likely provides
`db_cur`-style fixtures; adapt these to your conventions. The
ASSERTIONS are what matter; they capture the rule.
"""
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture
def now_utc():
    return datetime.now(timezone.utc)


class TestHorizonBasedLostMode:

    def test_houston_miss_not_orphan(self, now_utc):
        """Case A — Houston Miss (today's 7848, 7853 case).

        AAI missed the pickup observation but dropoff fired cleanly.
        Once dropoff fires, LIVE_OFFER_PREDICATE_SQL's first clause
        (`actual_dropoff_at IS NULL`) excludes the offer entirely.

        Fixture intent:
          offer accepted 3 hours ago
          actual_pickup_at = NULL
          actual_dropoff_at = 2.5 hours ago
        Expected: _detect_lost_mode returns False
        """
        pytest.skip("Fixture wiring — adapt to conftest")

    def test_calhoun_zombie_post_horizon_not_orphan(self, now_utc):
        """Case B — Calhoun Zombie, horizon blown.

        Pickup fired but dropoff was never observable (address
        doesn't exist). Driver has driven far enough that the
        distance horizon has blown.

        Fixture intent:
          offer accepted 4 hours ago
          miles_at_offer_receipt = 100.0
          pickup_miles = 1.5, trip_miles = 5.0
          actual_pickup_at = 3.5 hours ago
          actual_dropoff_at = NULL
          current_cumulative_miles = 120.0  (horizon was 100 + 6.5*1.25 ≈ 108)
        Expected: _detect_lost_mode returns False
        """
        pytest.skip("Fixture wiring — adapt to conftest")

    def test_calhoun_zombie_pre_horizon_is_orphan(self, now_utc):
        """Case C — Calhoun Zombie, horizon not yet blown.

        Same as B but the driver hasn't crossed the horizon yet.
        System genuinely doesn't know if the driver is still en
        route to a phantom dropoff. Cautious lost_mode is correct.

        Fixture intent:
          offer accepted 5 minutes ago
          actual_pickup_at = 2 minutes ago
          actual_dropoff_at = NULL
          horizon still active
        Expected: _detect_lost_mode returns True
        """
        pytest.skip("Fixture wiring — adapt to conftest")

    def test_accepted_recent_no_fires_is_orphan(self, now_utc):
        """Case D — genuine orphan.

        Offer accepted recently, no pickup, no dropoff. Still in
        horizon. The unambiguous lost_mode case.

        Fixture intent:
          offer accepted 5 minutes ago
          no fires
          horizon still active
        Expected: _detect_lost_mode returns True
        """
        pytest.skip("Fixture wiring — adapt to conftest")

    def test_offer_in_queue_not_orphan(self, now_utc):
        """Case E — offer in active evaluation queue.

        Excluded by `oh.id != ALL(queue)` regardless of all other
        state. The dispatcher is currently evaluating it.

        Fixture intent:
          offer accepted 5 minutes ago, in queue_offer_ids
        Expected: _detect_lost_mode returns False
        """
        pytest.skip("Fixture wiring — adapt to conftest")
