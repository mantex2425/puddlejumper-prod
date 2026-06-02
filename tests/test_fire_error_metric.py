"""Live-PG regression tests for fire-time `offer_history.pickup_error_m`
and `offer_history.dropoff_error_m` writes.

These columns went dark in the May-1 demolition and were re-wired
2026-06-02 (see docs/RECON_fire_error_metric_dark_2026-06-02.md). At
all four fire sites in `_execute_action` (FirePickup normal path,
FireDropoff, FirePickupObservation, FireDropoffObservation), the
intended (offer-time) PUDO coords are fetched from `offer_history`
and the great-circle distance to the fired nail point is computed
via `where_am_i.haversine_meters` and written to the corresponding
`*_error_m` column.

Each fire path has two scenarios:
  - valid intended coords → assert the column equals
    haversine_meters(intended, nail) within float tolerance.
  - NULL intended coords (lat/lng missing on the offer_history row)
    → assert the column is written as NULL and no exception is raised.

The catch-up branch in FirePickup (where `actual_pickup_at` is already
non-NULL from a prior Observation) is intentionally NOT covered here:
Rule XV requires it to short-circuit before reaching the offer_history
UPDATE, so it does not write pickup_error_m. The catch-up branch is
covered by tests/test_fire_pickup_catchup_guard.py.

Real RealDictCursor required — the compute path reads dict keys
('pickup_lat', 'dropoff_lat') from cur.fetchone(); a tuple mock would
mask shape regressions. Uses the db_cur fixture from conftest.py
(SAVEPOINT/ROLLBACK isolation against the production DB).
"""
from __future__ import annotations

import pytest

from dispatch import (
    FireDropoff,
    FireDropoffObservation,
    FirePickup,
    FirePickupObservation,
)
from driver_heartbeat import _execute_action
from driver_queue import DriverQueue
from where_am_i import haversine_meters


# Sentinel coordinates — INTENDED_* are seeded onto offer_history (the
# matcher-supplied geocode), NAIL_* is the cluster centroid the fire is
# triggered at (the driver's observed pickup/dropoff location).
INTENDED_PICKUP_LAT = 29.7604
INTENDED_PICKUP_LNG = -95.3698
INTENDED_DROPOFF_LAT = 29.7000
INTENDED_DROPOFF_LNG = -95.3000
NAIL_LAT = 29.7500
NAIL_LNG = -95.3500


class _FakeCluster:
    median_lat = NAIL_LAT
    median_lng = NAIL_LNG


def _seed_driver_trip_state(db_cur, driver_id, current_offer_id=None):
    db_cur.execute(
        """
        INSERT INTO app_private.driver_trip_state (driver_id, current_offer_id)
        VALUES (%s, %s)
        """,
        (driver_id, current_offer_id),
    )


def _read_error_metrics(db_cur, oh_id):
    db_cur.execute(
        """
        SELECT pickup_error_m, dropoff_error_m
        FROM app_private.offer_history
        WHERE id = %s
        """,
        (oh_id,),
    )
    return db_cur.fetchone()


def _exec(action, db_cur, driver_id):
    queue = DriverQueue(driver_id)
    return _execute_action(
        action, db_cur, db_cur.connection, driver_id, queue,
        cluster=_FakeCluster(),
        cumulative_miles=145.5,
    )


def _expected_pickup_error_m():
    return haversine_meters(
        INTENDED_PICKUP_LAT, INTENDED_PICKUP_LNG, NAIL_LAT, NAIL_LNG,
    )


def _expected_dropoff_error_m():
    return haversine_meters(
        INTENDED_DROPOFF_LAT, INTENDED_DROPOFF_LNG, NAIL_LAT, NAIL_LNG,
    )


# ============================================================================
# FirePickup — normal narrative fire (catch-up path covered elsewhere)
# ============================================================================

def test_fire_pickup_valid_coords_writes_haversine(
    db_cur, test_driver_id, seed_decision_log, seed_offer_history,
):
    _seed_driver_trip_state(db_cur, test_driver_id)
    dl_id = seed_decision_log(test_driver_id)
    oh_id = seed_offer_history(
        dl_id,
        pickup_lat=INTENDED_PICKUP_LAT,
        pickup_lng=INTENDED_PICKUP_LNG,
    )

    executed, err = _exec(FirePickup(offer_id=str(oh_id)), db_cur, test_driver_id)
    assert executed is True, f"FirePickup execution failed: err={err!r}"
    assert err is None

    metrics = _read_error_metrics(db_cur, oh_id)
    expected = _expected_pickup_error_m()
    assert metrics["pickup_error_m"] == pytest.approx(expected, abs=0.001), (
        f"FirePickup pickup_error_m mismatch: got "
        f"{metrics['pickup_error_m']!r}, expected ≈{expected:.6f}m "
        f"(haversine intended→nail). Dark column regression."
    )


def test_fire_pickup_null_intended_coords_writes_null(
    db_cur, test_driver_id, seed_decision_log, seed_offer_history,
):
    _seed_driver_trip_state(db_cur, test_driver_id)
    dl_id = seed_decision_log(test_driver_id)
    oh_id = seed_offer_history(dl_id, pickup_lat=None, pickup_lng=None)

    executed, err = _exec(FirePickup(offer_id=str(oh_id)), db_cur, test_driver_id)
    assert executed is True, (
        f"FirePickup with NULL intended coords must complete without "
        f"raising; got err={err!r}. None-guard at the compute site "
        f"likely missing."
    )
    assert err is None

    metrics = _read_error_metrics(db_cur, oh_id)
    assert metrics["pickup_error_m"] is None, (
        f"NULL intended coords must write pickup_error_m=NULL; got "
        f"{metrics['pickup_error_m']!r}."
    )


# ============================================================================
# FireDropoff
# ============================================================================

def test_fire_dropoff_valid_coords_writes_haversine(
    db_cur, test_driver_id, seed_decision_log, seed_offer_history,
):
    _seed_driver_trip_state(db_cur, test_driver_id)
    dl_id = seed_decision_log(test_driver_id)
    oh_id = seed_offer_history(
        dl_id,
        dropoff_lat=INTENDED_DROPOFF_LAT,
        dropoff_lng=INTENDED_DROPOFF_LNG,
    )

    executed, err = _exec(FireDropoff(offer_id=str(oh_id)), db_cur, test_driver_id)
    assert executed is True, f"FireDropoff execution failed: err={err!r}"
    assert err is None

    metrics = _read_error_metrics(db_cur, oh_id)
    expected = _expected_dropoff_error_m()
    assert metrics["dropoff_error_m"] == pytest.approx(expected, abs=0.001), (
        f"FireDropoff dropoff_error_m mismatch: got "
        f"{metrics['dropoff_error_m']!r}, expected ≈{expected:.6f}m "
        f"(haversine intended→nail). Dark column regression."
    )


def test_fire_dropoff_null_intended_coords_writes_null(
    db_cur, test_driver_id, seed_decision_log, seed_offer_history,
):
    _seed_driver_trip_state(db_cur, test_driver_id)
    dl_id = seed_decision_log(test_driver_id)
    oh_id = seed_offer_history(dl_id, dropoff_lat=None, dropoff_lng=None)

    executed, err = _exec(FireDropoff(offer_id=str(oh_id)), db_cur, test_driver_id)
    assert executed is True, (
        f"FireDropoff with NULL intended coords must complete without "
        f"raising; got err={err!r}. None-guard at the compute site "
        f"likely missing."
    )
    assert err is None

    metrics = _read_error_metrics(db_cur, oh_id)
    assert metrics["dropoff_error_m"] is None, (
        f"NULL intended coords must write dropoff_error_m=NULL; got "
        f"{metrics['dropoff_error_m']!r}."
    )


# ============================================================================
# FirePickupObservation
# ============================================================================

def test_fire_pickup_observation_valid_coords_writes_haversine(
    db_cur, test_driver_id, seed_decision_log, seed_offer_history,
):
    _seed_driver_trip_state(db_cur, test_driver_id)
    dl_id = seed_decision_log(test_driver_id)
    oh_id = seed_offer_history(
        dl_id,
        pickup_lat=INTENDED_PICKUP_LAT,
        pickup_lng=INTENDED_PICKUP_LNG,
    )

    executed, err = _exec(
        FirePickupObservation(offer_id=str(oh_id)), db_cur, test_driver_id,
    )
    assert executed is True, f"FirePickupObservation execution failed: err={err!r}"
    assert err is None

    metrics = _read_error_metrics(db_cur, oh_id)
    expected = _expected_pickup_error_m()
    assert metrics["pickup_error_m"] == pytest.approx(expected, abs=0.001), (
        f"FirePickupObservation pickup_error_m mismatch: got "
        f"{metrics['pickup_error_m']!r}, expected ≈{expected:.6f}m "
        f"(haversine intended→nail). Dark column regression."
    )


def test_fire_pickup_observation_null_intended_coords_writes_null(
    db_cur, test_driver_id, seed_decision_log, seed_offer_history,
):
    _seed_driver_trip_state(db_cur, test_driver_id)
    dl_id = seed_decision_log(test_driver_id)
    oh_id = seed_offer_history(dl_id, pickup_lat=None, pickup_lng=None)

    executed, err = _exec(
        FirePickupObservation(offer_id=str(oh_id)), db_cur, test_driver_id,
    )
    assert executed is True, (
        f"FirePickupObservation with NULL intended coords must complete "
        f"without raising; got err={err!r}. None-guard at the compute "
        f"site likely missing."
    )
    assert err is None

    metrics = _read_error_metrics(db_cur, oh_id)
    assert metrics["pickup_error_m"] is None, (
        f"NULL intended coords must write pickup_error_m=NULL; got "
        f"{metrics['pickup_error_m']!r}."
    )


# ============================================================================
# FireDropoffObservation
# ============================================================================

def test_fire_dropoff_observation_valid_coords_writes_haversine(
    db_cur, test_driver_id, seed_decision_log, seed_offer_history,
):
    _seed_driver_trip_state(db_cur, test_driver_id)
    dl_id = seed_decision_log(test_driver_id)
    oh_id = seed_offer_history(
        dl_id,
        dropoff_lat=INTENDED_DROPOFF_LAT,
        dropoff_lng=INTENDED_DROPOFF_LNG,
    )

    executed, err = _exec(
        FireDropoffObservation(offer_id=str(oh_id)), db_cur, test_driver_id,
    )
    assert executed is True, f"FireDropoffObservation execution failed: err={err!r}"
    assert err is None

    metrics = _read_error_metrics(db_cur, oh_id)
    expected = _expected_dropoff_error_m()
    assert metrics["dropoff_error_m"] == pytest.approx(expected, abs=0.001), (
        f"FireDropoffObservation dropoff_error_m mismatch: got "
        f"{metrics['dropoff_error_m']!r}, expected ≈{expected:.6f}m "
        f"(haversine intended→nail). Dark column regression."
    )


def test_fire_dropoff_observation_null_intended_coords_writes_null(
    db_cur, test_driver_id, seed_decision_log, seed_offer_history,
):
    _seed_driver_trip_state(db_cur, test_driver_id)
    dl_id = seed_decision_log(test_driver_id)
    oh_id = seed_offer_history(dl_id, dropoff_lat=None, dropoff_lng=None)

    executed, err = _exec(
        FireDropoffObservation(offer_id=str(oh_id)), db_cur, test_driver_id,
    )
    assert executed is True, (
        f"FireDropoffObservation with NULL intended coords must complete "
        f"without raising; got err={err!r}. None-guard at the compute "
        f"site likely missing."
    )
    assert err is None

    metrics = _read_error_metrics(db_cur, oh_id)
    assert metrics["dropoff_error_m"] is None, (
        f"NULL intended coords must write dropoff_error_m=NULL; got "
        f"{metrics['dropoff_error_m']!r}."
    )
