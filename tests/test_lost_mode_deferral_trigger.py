"""§XIV.J live-PG tests for the §5.5 lost-mode deferral TRIGGER (fix #2).

These are boundary tests by nature — the trigger's behavior IS its interaction
with the live predicate + driver_trip_state read. A mocked cursor here would test
the mock, not the trigger. So everything runs against the real cursor (db_cur,
SAVEPOINT-rolled-back per conftest), driving the real log_decision through a
no-commit conn so its writes stay inside the savepoint.

What is pinned:
  - Four-way decision tree: lost-mode→deferred (the fix; fired 0% pre-fix), idle→
    active (INV-A), stacked→active (disjoint from the bound-stack path),
    odometer-absent→deferred (R4 union safety net).
  - INV-A guard (its own named test): a genuine idle first offer must NOT defer and
    MUST compute the idle anchor — the case absence-defined logic rots quietly in.
  - Set-identity in the MOVING case (R3): the trigger's alive-unpicked set equals
    the heartbeat detector's, computed against effective_last_move — and a contrast
    assertion proves raw last_odometer_move_at would yield a DIFFERENT (subset) set.
    This is the assertion that distinguishes effective_last_move parity from the
    subset bug; without the moving case it would be decorative.
  - Error path (fail-closed): a psycopg2 detection failure lands the row 'deferred'
    in the DB (read-back), never a fabricated 'active' anchor.
"""
import datetime
import json

import psycopg2
import pytest
from unittest.mock import patch

from decisions.logger import log_decision
from driver_queue import _get_alive_unpicked_offer_ids, compute_effective_last_move
from driver_heartbeat import _detect_lost_mode

UTC = datetime.timezone.utc


def _dt(now, offset_min):
    return now + datetime.timedelta(minutes=offset_min)


class _NoCommitConn:
    """conn stand-in whose commit/rollback are no-ops, so log_decision's writes
    stay inside the db_cur SAVEPOINT (conftest rolls back at teardown). The real
    cursor (db_cur) does all SQL; only the transaction-control calls are stubbed."""
    def commit(self):
        pass

    def rollback(self):
        pass


def _seed_driver_trip_state(cur, driver_id, last_move_at, prior_cumulative_miles):
    """Seed the driver's driver_trip_state row that compute_effective_last_move
    reads (last_odometer_move_at + heartbeat.cumulative_miles). NOT-NULL columns
    leg_start_cumulative_miles / current_speed_streak_s carry DB defaults."""
    cur.execute(
        """
        INSERT INTO app_private.driver_trip_state
            (driver_id, last_odometer_move_at, heartbeat)
        VALUES (%s, %s, %s::jsonb)
        """,
        (driver_id, last_move_at,
         json.dumps({"cumulative_miles": prior_cumulative_miles})),
    )


def _full_ep(now, **overrides):
    """Complete ep dict for log_decision (every key the decision_log + offer_history
    INSERTs and the anchor compute read). Override per test."""
    base = {
        "fare": 12.0,
        "trip_miles": 5.5, "trip_min": 15.0,
        "pickup_miles": 2.5, "pickup_min": 6.0,
        "d_lat": 29.51, "d_lng": -95.52,
        "p_lat": 29.50, "p_lng": -95.51,
        "current_lat": 29.49, "current_lng": -95.50,
        "gps_age_sec": 1.5,
        "market_id": "M1", "market_name": "Houston",
        "towards_market_id": None,
        "towards_target_lat": None, "towards_target_lng": None,
        "ocr_confidence": None,
        "pickup_address": "123 Main St", "dropoff_address": "456 Oak Ave",
        "ride_type": "uberx",
        "is_surge": False, "is_priority": False, "is_reserve": False,
        "mode_name": "PUDDLE_JUMP",
        "offer_id": None,
        "_raw_trace": {}, "_arc_band_trace": None,
        "cumulative_miles": 100.0,
    }
    base.update(overrides)
    return base


def _result_stub():
    return {"verdict": "ACCEPT", "reason": "good rate",
            "hourlyRate": 25.0, "dollarsPerMile": 1.50}


def _status_of(cur, decision_log_id):
    cur.execute(
        """SELECT expected_odometer_status, expected_odometer
           FROM app_private.offer_history WHERE decision_log_id = %s""",
        (decision_log_id,),
    )
    return cur.fetchone()


# ============================================================================
# Set-identity in the MOVING case (R3) — the load-bearing parity proof
# ============================================================================

def test_set_identity_trigger_matches_detector_moving_case(
    db_cur, test_driver_id, seed_decision_log, seed_offer_history
):
    """The trigger's alive-unpicked set == the heartbeat detector's, computed
    against effective_last_move — IN THE MOVING CASE, where raw last_odometer_move_at
    would give a strict subset. This is the assertion that distinguishes
    effective_last_move parity (option 1) from the subset bug.

    Scenario engineered so the staleness gate is the ONLY axis that can differ:
      - driver_trip_state: last_odometer_move_at = 40 min ago (stale), stored
        cumulative_miles = 100.0
      - receipt odometer = 105.0  (≠ 100 → driver MOVED → effective_last_move = now)
      - peer offer: created 50 min ago, alive, unpicked, NULL band (permissive)

    With effective_last_move = now → staleness permissive → peer ALIVE → set = {peer}.
    With raw last_odometer_move_at = 40 min ago → staleness reaps (frozen >30 min,
    offer predates the freeze) → set = {} (the subset bug).
    """
    now = datetime.datetime.now(UTC)
    _seed_driver_trip_state(db_cur, test_driver_id,
                            last_move_at=_dt(now, -40), prior_cumulative_miles=100.0)

    dlog = seed_decision_log(test_driver_id)
    peer_id = str(seed_offer_history(
        dlog,
        created_at=_dt(now, -50),
        actual_pickup_at=None, actual_dropoff_at=None,
        expected_pickup_distance=None,   # NULL center → band permissive
        expected_odometer_status="active",
    ))

    receipt_cum = 105.0
    eff = compute_effective_last_move(db_cur, test_driver_id, receipt_cum, now)
    assert eff == now, (
        "driver moved this receipt (105 != stored 100) → effective_last_move must "
        "be `now`, not the stale stored timestamp"
    )

    trigger_set = _get_alive_unpicked_offer_ids(
        db_cur, test_driver_id, receipt_cum, now, eff)
    raw_set = _get_alive_unpicked_offer_ids(
        db_cur, test_driver_id, receipt_cum, now, _dt(now, -40))

    assert trigger_set == {peer_id}, (
        "under effective_last_move parity the moving driver's peer is ALIVE → the "
        "trigger sees it"
    )
    assert raw_set == frozenset(), (
        "under the RAW stale timestamp the staleness gate reaps the peer → empty "
        "set: this is the subset bug effective_last_move avoids"
    )
    assert trigger_set != raw_set, (
        "the two MUST differ in the moving case — else the test is decorative and "
        "proves nothing about why effective_last_move is required"
    )
    # Detector parity: _detect_lost_mode delegates to the same body with the same
    # effective_last_move → it sees the identical set (True).
    assert _detect_lost_mode(
        db_cur, test_driver_id, [], receipt_cum, now, eff) is True, (
        "the heartbeat detector evaluates the identical set against the same "
        "effective_last_move → lost-mode True"
    )


# ============================================================================
# Four-way decision tree (through the real log_decision)
# ============================================================================

def test_lost_mode_receipt_defers(
    db_cur, test_driver_id, seed_decision_log, seed_offer_history
):
    """prev_offer None + a pre-existing alive-unpicked peer + odometer present
    → 'deferred' (the fix; fired 0% in production pre-fix)."""
    now = datetime.datetime.now(UTC)
    _seed_driver_trip_state(db_cur, test_driver_id,
                            last_move_at=now, prior_cumulative_miles=100.0)

    # Pre-existing alive-unpicked peer (unpicked → NOT found by the prev_offer
    # chaining query, which requires actual_pickup_at NOT NULL → prev stays None).
    peer_dlog = seed_decision_log(test_driver_id)
    seed_offer_history(
        peer_dlog,
        created_at=_dt(now, -10),
        actual_pickup_at=None, actual_dropoff_at=None,
        expected_pickup_distance=None,
        expected_odometer_status="active",
    )

    ep = _full_ep(now, cumulative_miles=100.0)
    dlid = log_decision(db_cur, _NoCommitConn(), test_driver_id, {}, ep, _result_stub())

    row = _status_of(db_cur, dlid)
    assert row["expected_odometer_status"] == "deferred", (
        "lost-mode receipt (no chaining anchor + alive-unpicked peer) must defer, "
        "not fabricate the idle anchor"
    )
    assert row["expected_odometer"] is None, "deferred anchor must be NULL"


def test_idle_first_offer_stays_active_INV_A(
    db_cur, test_driver_id
):
    """INV-A: a genuine idle first offer — prev_offer None, NO alive-unpicked peer,
    odometer present — MUST land 'active' with the idle anchor computed, NOT defer.
    The over-defer guard; absence-defined cases rot quietly without it."""
    now = datetime.datetime.now(UTC)
    _seed_driver_trip_state(db_cur, test_driver_id,
                            last_move_at=now, prior_cumulative_miles=100.0)
    # No peer offer seeded → alive-unpicked set is empty → not lost-mode.

    ep = _full_ep(now, cumulative_miles=100.0)
    dlid = log_decision(db_cur, _NoCommitConn(), test_driver_id, {}, ep, _result_stub())

    row = _status_of(db_cur, dlid)
    assert row["expected_odometer_status"] == "active", (
        "genuine idle (no alive-unpicked peer) must NOT defer — INV-A over-defer guard"
    )
    assert row["expected_odometer"] is not None, (
        "idle anchor must be computed (current_odometer + pickup_miles), not NULL"
    )


def test_stacked_receipt_stays_active(
    db_cur, test_driver_id, seed_decision_log, seed_offer_history
):
    """A picked-up live prev offer → prev_offer non-None → the trigger is SKIPPED
    (it only runs when prev is None) → stacked chaining → 'active'. Proves the
    lost-mode trigger is disjoint from the bound-stack path."""
    now = datetime.datetime.now(UTC)
    # Picked-up, mid-trip prev offer: found by the chaining query (actual_pickup_at
    # NOT NULL, actual_dropoff_at NULL, expected_dropoff_arrival_time NOT NULL),
    # within horizon so the GC keeps it.
    prev_dlog = seed_decision_log(test_driver_id)
    seed_offer_history(
        prev_dlog,
        created_at=_dt(now, -3),
        actual_pickup_at=_dt(now, -2), actual_dropoff_at=None,
        expected_dropoff_arrival_time=_dt(now, 10),
        expected_dropoff_distance=120.0,
        miles_at_offer_receipt=99.0,
        expected_odometer_status="active",
    )

    ep = _full_ep(now, cumulative_miles=100.0)
    dlid = log_decision(db_cur, _NoCommitConn(), test_driver_id, {}, ep, _result_stub())

    row = _status_of(db_cur, dlid)
    assert row["expected_odometer_status"] == "active", (
        "stacked receipt (picked-up prev offer) chains forward → active; the "
        "lost-mode trigger must not fire when a chaining anchor exists"
    )
    assert row["expected_odometer"] is not None, "stacked anchor must be computed"


def test_odometer_absent_defers(db_cur, test_driver_id):
    """R4 union safety net: no odometer → no computable anchor → 'deferred',
    independent of the lost-mode branch (which is gated on odometer present)."""
    now = datetime.datetime.now(UTC)
    ep = _full_ep(now, cumulative_miles=None)
    dlid = log_decision(db_cur, _NoCommitConn(), test_driver_id, {}, ep, _result_stub())

    row = _status_of(db_cur, dlid)
    assert row["expected_odometer_status"] == "deferred", (
        "odometer-absent receipt must defer (anchor uncomputable) — R4 safety net"
    )
    assert row["expected_odometer"] is None


# ============================================================================
# Error path (fail-closed) — never fabricate on detection failure
# ============================================================================

def test_detection_error_defers_not_fabricates(
    db_cur, test_driver_id, seed_decision_log, seed_offer_history
):
    """Policy A fail-closed: when bit-2 detection raises a psycopg2 error on a
    HEALTHY connection, the row lands 'deferred' in the DB (read-back) — NEVER a
    fabricated 'active' idle anchor. Pins the rollback-then-write persistence seam.

    (Synthetic raise on a healthy cursor; the connection-loss branch — INSERT fails,
    no row, loud ERROR — is covered by propagation, not by this deferred write.)
    """
    now = datetime.datetime.now(UTC)
    _seed_driver_trip_state(db_cur, test_driver_id,
                            last_move_at=now, prior_cumulative_miles=100.0)

    ep = _full_ep(now, cumulative_miles=100.0)  # prev None + odometer present → trigger runs

    with patch(
        "decisions.logger._get_alive_unpicked_offer_ids",
        side_effect=psycopg2.OperationalError("synthetic detection failure"),
    ):
        dlid = log_decision(
            db_cur, _NoCommitConn(), test_driver_id, {}, ep, _result_stub())

    row = _status_of(db_cur, dlid)
    assert row is not None, (
        "fail-closed on a healthy connection must PERSIST a row (not lose the offer)"
    )
    assert row["expected_odometer_status"] == "deferred", (
        "detection error must land 'deferred' (fail-closed), never a fabricated "
        "'active' idle anchor — the §5.5 failure must not re-enter via the error path"
    )
    assert row["expected_odometer"] is None
