"""Tests for driver_status.py's planner_queue threading.

Regression coverage for the 2026-06-01 monitor-queue fix
(docs/FIX_PROPOSAL_QUEUE_NO_REAP_2026-06-01.md): the /driver/status
endpoint must thread current_cumulative_miles + last_odometer_move_at
into DriverQueue.offer_ids_only so the predicate's distance and
staleness gates evaluate against real per-tick state.

Strategy: use the production X-Internal-Replay header bypass
(utils.require_firebase_auth + verify_and_get_user_id) to skip firebase
without monkeypatching, patch DriverQueue and get_db, and assert on the
kwargs the route passes plus the JSON payload. Three scenarios per fix
proposal §2.1.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from flask import Flask

import driver_status as ds_mod


DRIVER_ID = "test_driver_uid_xyz"
REPLAY_HEADERS = {
    "X-Internal-Replay": "puddlejumper-replay-2026",
    "X-Driver-Id": DRIVER_ID,
}


@pytest.fixture
def app():
    """Flask app with driver_status_bp mounted. Auth bypass via the
    X-Internal-Replay headers (production-supported replay path).
    """
    app = Flask(__name__)
    app.register_blueprint(ds_mod.driver_status_bp, url_prefix="/api/v1")
    return app


def _build_cursor(state_row):
    """Build a MagicMock cursor that satisfies driver_status's 4 SELECTs.

    Order of queries inside get_driver_status:
      1. driver_trip_state SELECT  -> fetchone -> state_row
      2. decision_log SELECT       -> fetchone -> None (last_decision absent is fine)
      3. recent_offers SELECT      -> fetchall -> []
      4. pudo_decision_context SELECT -> fetchall -> []

    planner_queue is sourced through DriverQueue.offer_ids_only, which
    we patch separately on the driver_status import — this cursor
    fixture only needs to feed the four scalar SELECTs.
    """
    cur = MagicMock()
    cur.fetchone.side_effect = [state_row, None]
    cur.fetchall.side_effect = [[], []]
    return cur


def _invoke_status(app, cur, planner_queue_return):
    """GET /api/v1/driver/status with patched get_db + DriverQueue.
    Returns (response, fake_queue_instance).
    """
    fake_conn = MagicMock()
    fake_conn.cursor.return_value = cur

    fake_queue_instance = MagicMock()
    fake_queue_instance.offer_ids_only.return_value = planner_queue_return

    with patch("driver_status.get_db", return_value=fake_conn), \
         patch("driver_status.DriverQueue", return_value=fake_queue_instance):
        client = app.test_client()
        resp = client.get("/api/v1/driver/status", headers=REPLAY_HEADERS)
    return resp, fake_queue_instance


# =============================================================================
# Scenario A: fresh lom + cum past every envelope -> empty queue
# =============================================================================

def test_scenarioA_fresh_state_past_envelope_threads_real_kwargs(app):
    """Heartbeat has fresh state and cumulative_miles past every offer's
    envelope. planner_queue must reflect what offer_ids_only returns
    when threaded with the real state — empty in this case.
    """
    state_row = {
        "current_offer_id": None,
        "pickup_lat": None, "pickup_lng": None,
        "dropoff_lat": None, "dropoff_lng": None,
        "potential_cancellation": False,
        "heartbeat": {"cumulative_miles": 362.36, "lat": 29.5, "lng": -95.5},
        "heartbeat_at": None,
        "last_odometer_move_at": "2026-06-01T18:14:28+00:00",
    }
    cur = _build_cursor(state_row)
    resp, fake_queue = _invoke_status(app, cur, planner_queue_return=())

    assert resp.status_code == 200, resp.data
    body = resp.get_json()
    assert body["planner_queue"] == []

    fake_queue.offer_ids_only.assert_called_once()
    _, kwargs = fake_queue.offer_ids_only.call_args
    assert kwargs["current_cumulative_miles"] == 362.36
    assert kwargs["last_odometer_move_at"] == "2026-06-01T18:14:28+00:00"


# =============================================================================
# Scenario B: NULL lom + NULL cum (brand-new driver) -> time-only fallback
# =============================================================================

def test_scenarioB_null_state_threads_nones_for_time_only_fallback(app):
    """Brand-new driver: heartbeat jsonb absent OR cumulative_miles
    missing, last_odometer_move_at NULL. offer_ids_only must still be
    called — with explicit None for both kwargs — preserving the
    time-only fallback that pre-fix behavior produced.
    """
    state_row = {
        "current_offer_id": None,
        "pickup_lat": None, "pickup_lng": None,
        "dropoff_lat": None, "dropoff_lng": None,
        "potential_cancellation": False,
        "heartbeat": {},                     # no cumulative_miles key
        "heartbeat_at": None,
        "last_odometer_move_at": None,
    }
    cur = _build_cursor(state_row)
    resp, fake_queue = _invoke_status(
        app, cur, planner_queue_return=("offer1", "offer2"),
    )

    assert resp.status_code == 200, resp.data
    body = resp.get_json()
    assert body["planner_queue"] == ["offer1", "offer2"]

    fake_queue.offer_ids_only.assert_called_once()
    _, kwargs = fake_queue.offer_ids_only.call_args
    assert kwargs["current_cumulative_miles"] is None
    assert kwargs["last_odometer_move_at"] is None


# =============================================================================
# Scenario C: partial split (one offer survives the envelope, others reaped)
# =============================================================================

def test_scenarioC_partial_envelope_surfaces_only_survivor(app):
    """cum_miles sits between two offers' envelope caps. offer_ids_only
    is responsible for the actual filtering; planner_queue must reflect
    its return value verbatim. This test asserts the threading and the
    pass-through — it does NOT re-test predicate semantics.
    """
    state_row = {
        "current_offer_id": None,
        "pickup_lat": None, "pickup_lng": None,
        "dropoff_lat": None, "dropoff_lng": None,
        "potential_cancellation": False,
        "heartbeat": {"cumulative_miles": 320.0, "lat": 29.9, "lng": -95.5},
        "heartbeat_at": None,
        "last_odometer_move_at": "2026-06-01T18:14:28+00:00",
    }
    cur = _build_cursor(state_row)
    resp, fake_queue = _invoke_status(
        app, cur, planner_queue_return=("survivor_8743",),
    )

    assert resp.status_code == 200, resp.data
    body = resp.get_json()
    assert body["planner_queue"] == ["survivor_8743"]

    fake_queue.offer_ids_only.assert_called_once()
    _, kwargs = fake_queue.offer_ids_only.call_args
    assert kwargs["current_cumulative_miles"] == 320.0
    assert kwargs["last_odometer_move_at"] == "2026-06-01T18:14:28+00:00"
