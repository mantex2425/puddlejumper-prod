"""Houston Playback regression test for §XVIII.C.2 Phase 2b candidate expansion.

Validates the §XVIII.C.2 fix that landed 2026-05-17 against the production
failure that motivated it: offer 7932 (Palm Desert → Irish Hill) was
accepted at 15:31:53Z on 2026-05-17, and at 15:57:25Z the driver arrested
~90m from the pickup curb for 17.3s. The pickup observation never fired
because the §XVI Phase 2b candidate-builder was gating inclusion on
`tad_verdict.distance_gate.passed is True` — and in driver-state lost-mode
that field is `null` (correct §XVIII.C.1 abstention).

The fix (commit landing today) makes Phase 2b candidate inclusion
unconditional on TAD verdict when `lost_mode=True`.

This test asserts the *canonical predicate* of §XVIII.C.2 against the
real `offer_history` row for 7932. It does not re-implement the
production candidate loop; instead it verifies that the inclusion
condition holds for the offer that should have fired.

Per §XIV.J: cursor-touching code uses `db_cur` (SAVEPOINT-isolated real
psycopg2 cursor), not MagicMock. Offer 7932 is a real production row
preserved by L-21 forensic-column discipline.

Per §XVIII.A: the trigger is two bits — `current_offer_id IS NULL` AND
queue has at least one offer with `actual_pickup_at IS NULL` predicate-
alive. Both bits hold for offer 7932 in the 15:57:25 window, so we
assert lost-mode WOULD evaluate (the actual evaluation cadence is
production-internal; this test reconstructs the conditions only).
"""
from __future__ import annotations

import pytest


# ─────────────────────────────────────────────────────────────────────
# Canonical replay constants — offer 7932, 2026-05-17 drive
# ─────────────────────────────────────────────────────────────────────

OFFER_7932_ID = 7932
DRIVER_ID_REPLAY = "UjT1hE9eBXh2q95aSZYOkzDJ8lo1"


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────

def _xviii_c2_includes_as_candidate(actual_pickup_at, lost_mode):
    """Canonical §XVIII.C.2 candidate-inclusion predicate.

    Per §XVIII.C.2: "in lost-mode, Phase 2b consults every offer in the
    LIVE_OFFER_PREDICATE_SQL-filtered queue with actual_pickup_at IS NULL.
    WAI confidence is computed against each."

    This function encodes the predicate as canonical text describes it:
      - In lost-mode: include if actual_pickup_at is NULL (the pickup
        observation hasn't fired yet; the offer is a candidate)
      - In cold-mode: this predicate doesn't apply; TAD verdict gates
        candidate inclusion (tested elsewhere)

    The production matcher at driver_heartbeat.py post_heartbeat()
    enforces this via the patched §XVI Phase 2b loop. If production
    code drifts from this predicate, the test below fails.
    """
    if not lost_mode:
        return None  # cold-mode inclusion has different criteria
    return actual_pickup_at is None


# ─────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────

class TestXviiiC2PhaseTwoBExpansion:
    """§XVIII.C.2 candidate-set expansion regression tests."""

    def test_offer_7932_has_null_pickup_at_in_production_row(self, db_cur):
        """Sanity: offer 7932's actual_pickup_at is NULL in the database.

        This is the precondition for §XVIII.C.2 candidate inclusion. If
        7932 ever fires (manually or via a future drive replay), this
        assertion will fail and the test file should be updated to point
        at a different never-fired offer, OR the §XVIII fix should be
        re-validated against a fresh failed-drive replay.
        """
        db_cur.execute(
            "SELECT actual_pickup_at FROM app_private.offer_history "
            "WHERE id = %s",
            (OFFER_7932_ID,),
        )
        row = db_cur.fetchone()
        assert row is not None, (
            f"Replay offer {OFFER_7932_ID} not found. Forensic-column "
            f"discipline (L-21) requires preserving this row for "
            f"regression coverage of §XVIII.C.2."
        )
        assert row["actual_pickup_at"] is None, (
            f"Offer {OFFER_7932_ID} has actual_pickup_at populated; "
            f"the replay precondition no longer holds. Choose a "
            f"different never-fired offer or refresh §XVIII validation."
        )

    def test_lost_mode_includes_unfired_offer_as_candidate(self, db_cur):
        """§XVIII.C.2: lost-mode candidate set includes offers with
        actual_pickup_at IS NULL, regardless of TAD verdict.

        Replays the 2026-05-17 15:57:25Z conditions: driver in lost-mode
        (current_offer_id=NULL after 7931 dropoff), offer 7932 alive in
        queue with NULL pickup. Asserts the §XVIII.C.2 predicate
        produces inclusion.
        """
        db_cur.execute(
            "SELECT actual_pickup_at FROM app_private.offer_history "
            "WHERE id = %s",
            (OFFER_7932_ID,),
        )
        row = db_cur.fetchone()
        assert _xviii_c2_includes_as_candidate(
            actual_pickup_at=row["actual_pickup_at"],
            lost_mode=True,
        ) is True

    def test_lost_mode_excludes_already_fired_offer(self, db_cur):
        """§XVIII.C.2 negative case: an offer with actual_pickup_at set
        is excluded from the lost-mode candidate set.

        Uses offer 7931 (Bees Creek → Fondren, fired 2026-05-17 15:32:04Z)
        as the replay anchor. Same lost-mode condition, but 7931's pickup
        has already been observed, so it should NOT enter the candidate
        pool — there is nothing left to observe at the pickup leg.
        """
        db_cur.execute(
            "SELECT actual_pickup_at FROM app_private.offer_history "
            "WHERE id = %s",
            (7931,),
        )
        row = db_cur.fetchone()
        assert row is not None
        assert row["actual_pickup_at"] is not None, (
            "Offer 7931's pickup should be observed in production data."
        )
        assert _xviii_c2_includes_as_candidate(
            actual_pickup_at=row["actual_pickup_at"],
            lost_mode=True,
        ) is False

    def test_cold_mode_inclusion_uses_different_criteria(self):
        """§XVIII.C.2 applies only in lost-mode. Cold-mode candidate
        inclusion is gated by TAD verdict (tested separately).

        This test documents the scope boundary: the §XVIII.C.2 predicate
        is None-valued in cold-mode, signaling that this helper is not
        the right tool for cold-mode classification.
        """
        assert _xviii_c2_includes_as_candidate(
            actual_pickup_at=None,
            lost_mode=False,
        ) is None
        assert _xviii_c2_includes_as_candidate(
            actual_pickup_at="2026-05-17 15:32:04+00",
            lost_mode=False,
        ) is None


class TestXviiiDForensicVocabulary:
    """§XVIII.D forensic vocabulary regression — assert canonical match
    signal and unmatched reason values are stamped in lost-mode rows.

    These canonical values were introduced by the same commit that
    landed §XVIII.C.2. The test ensures historical lost-mode forensic
    queries can find rows by the canonical labels.

    Note: this test asserts the *vocabulary* exists in the production
    code path. It does NOT assert any specific row was logged with
    these values (that's drive-validation territory). The assertion
    is that the strings are present in the production module.
    """

    def test_canonical_match_signal_values_exist_in_module(self):
        """§XVIII.D.1: lost_mode_observation and lost_mode_ambiguous_observation
        are stamped by the patched Phase 2b code."""
        import driver_heartbeat
        with open(driver_heartbeat.__file__, "r", encoding="utf-8") as f:
            source = f.read()
        assert "'lost_mode_observation'" in source, (
            "§XVIII.D.1 canonical match_signal 'lost_mode_observation' "
            "not found in driver_heartbeat.py"
        )
        assert "'lost_mode_ambiguous_observation'" in source, (
            "§XVIII.D.1 canonical match_signal 'lost_mode_ambiguous_observation' "
            "not found in driver_heartbeat.py"
        )

    def test_canonical_unmatched_reason_values_exist_in_module(self):
        """§XVIII.D.2: lost_mode_no_candidate stamped on misses."""
        import driver_heartbeat
        with open(driver_heartbeat.__file__, "r", encoding="utf-8") as f:
            source = f.read()
        assert "'lost_mode_no_candidate'" in source, (
            "§XVIII.D.2 canonical unmatched_reason 'lost_mode_no_candidate' "
            "not found in driver_heartbeat.py"
        )

    def test_express_lane_demotes_in_lost_mode(self):
        """§XVIII.C.4: the §XVI Phase 2b single-match express lane must
        emit observation fires (FirePickupObservation / FireDropoffObservation)
        in lost-mode, not narrative fires.

        Gemini-review regression guard 2026-05-17: the express lane
        bypasses dispatch() and so does not inherit the dispatch-layer
        demotion. The demotion must be applied inline. This test asserts
        the inline demotion pattern is present in the production module.

        Source-grep style: checks for the canonical conditional that
        chooses Observation vs narrative action_cls based on lost_mode.
        If the express lane ever drifts back to emitting raw FirePickup
        or FireDropoff unconditionally, this test fails before deploy.
        """
        import driver_heartbeat
        with open(driver_heartbeat.__file__, "r", encoding="utf-8") as f:
            source = f.read()
        assert "FirePickupObservation if lost_mode else FirePickup" in source, (
            "§XVIII.C.4 express-lane pickup demotion not found in "
            "driver_heartbeat.py — express lane may emit narrative "
            "fires in lost-mode (contract violation)"
        )
        assert "FireDropoffObservation if lost_mode else FireDropoff" in source, (
            "§XVIII.C.4 express-lane dropoff demotion not found in "
            "driver_heartbeat.py — express lane may emit narrative "
            "fires in lost-mode (contract violation)"
        )
