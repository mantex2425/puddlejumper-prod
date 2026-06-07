"""§XIV.J test floor for event_ledger.py (Phase-1 emit core, UNWIRED).

Two layers:
  PURE (no DB): classify_reap fallbacks, compute_diff, gather_ledger_events
    (cold-start, keyframe reset-to-0 overflow, unprobeable-in-left, matcher inflection).
  LIVE-PG (conftest db_cur, SAVEPOINT-isolated, runs as the atjb runtime role):
    - CLAUSE-MAPPING / DRIFT-PIN: for each of the six predicate clauses, an offer that
      fails ONLY that clause is (a) reaped by the real LIVE_OFFER_PREDICATE_SQL and
      (b) labeled with that exact reason by classify_reap. This pins the Python labeler
      to the SQL predicate (the anti-drift guard for the §XIV.H observability labeler).
    - emit/seed roundtrip.
    - KEYSTONE runtime-true: atjb INSERT ok, UPDATE denied (on the populated child).

Deferred to the wiring step (need post_heartbeat): batch-atomicity/silent-loss in the
orchestrator, and the no-uncaught-raise structural guard. Noted, not skipped silently.
"""
import datetime
from datetime import timezone, timedelta
from types import SimpleNamespace

import psycopg2
import pytest

from driver_queue import LIVE_OFFER_PREDICATE_SQL, live_offer_predicate_params
import event_ledger as EL


# =============================================================================
# PURE tests (no DB)
# =============================================================================

def test_classify_reap_none_is_unprobeable():
    assert EL.classify_reap(None, reference_time=None, cumulative_miles=None,
                            effective_last_move=None) == EL.REAP_UNPROBEABLE


def test_compute_diff_entered_and_left():
    entered, left = EL.compute_diff(prior_ids=["a", "b"], current_ids=["b", "c"])
    assert entered == ["c"]
    assert left == ["a"]


def test_gather_cold_start_emits_only_keyframe():
    now = datetime.datetime(2026, 6, 7, 12, 0, tzinfo=timezone.utc)
    events, seed = EL.gather_ledger_events(
        prior_seed=None, current_ids={"9001", "9002"},
        current_status={"9001": {"status": "active"}, "9002": {"status": "active"}},
        reap_rows={}, reference_time=now, cumulative_miles=100.0, effective_last_move=now,
        matches=[], executed_actions=[], arrest_started_at=None, arrest_counter_s=None,
        cluster=None, cadence_target_hz=0.2, now=now,
    )
    # cold start: ONE keyframe, no entered, no left
    assert [e["event_type"] for e in events] == ["keyframe"]
    assert seed["keyframe_count"] == 0
    assert seed["last_queue_snapshot_ids"] == ["9001", "9002"]


def test_gather_keyframe_overflow_resets_count_to_zero():
    now = datetime.datetime(2026, 6, 7, 12, 0, tzinfo=timezone.utc)
    prior = {"last_queue_snapshot_ids": [], "last_matcher_snapshot": {},
             "keyframe_count": 49, "last_keyframe_at": now.isoformat()}
    events, seed = EL.gather_ledger_events(
        prior_seed=prior, current_ids={"9001", "9002"},   # 2 entered -> count 49+ >= 50
        current_status={}, reap_rows={}, reference_time=now, cumulative_miles=100.0,
        effective_last_move=now, matches=[], executed_actions=[], arrest_started_at=None,
        arrest_counter_s=None, cluster=None, cadence_target_hz=0.2, now=now,
    )
    assert any(e["event_type"] == "keyframe" for e in events)
    assert seed["keyframe_count"] == 0          # reset-to-0, NOT 51 mod 50 == 1


def test_gather_left_with_no_probe_row_is_unprobeable_and_seed_advances():
    now = datetime.datetime(2026, 6, 7, 12, 0, tzinfo=timezone.utc)
    prior = {"last_queue_snapshot_ids": ["9001"], "last_matcher_snapshot": {},
             "keyframe_count": 0, "last_keyframe_at": now.isoformat()}
    events, seed = EL.gather_ledger_events(
        prior_seed=prior, current_ids=set(),       # 9001 left, no probe row for it
        current_status={}, reap_rows={}, reference_time=now, cumulative_miles=100.0,
        effective_last_move=now, matches=[], executed_actions=[], arrest_started_at=None,
        arrest_counter_s=None, cluster=None, cadence_target_hz=0.2, now=now,
    )
    left = [e for e in events if e["event_type"] == "offer_left_queue"]
    assert len(left) == 1
    assert left[0]["queue_delta"]["left"][0]["reason"] == EL.REAP_UNPROBEABLE
    # poison-pill defense: seed advances past 9001 (no permanent stuck seed)
    assert "9001" not in seed["last_queue_snapshot_ids"]


def test_gather_matcher_inflection_on_tier_change():
    now = datetime.datetime(2026, 6, 7, 12, 0, tzinfo=timezone.utc)
    prior = {"last_queue_snapshot_ids": ["9001"],
             "last_matcher_snapshot": {"top_candidate_offer_id": "9001", "confidence_tier": "below"},
             "keyframe_count": 0, "last_keyframe_at": now.isoformat()}
    matches = [SimpleNamespace(offer_id="9001", confidence=0.55)]   # below -> floor tier cross
    events, _ = EL.gather_ledger_events(
        prior_seed=prior, current_ids={"9001"}, current_status={}, reap_rows={},
        reference_time=now, cumulative_miles=100.0, effective_last_move=now,
        matches=matches, executed_actions=[], arrest_started_at=None, arrest_counter_s=None,
        cluster=None, cadence_target_hz=1.0, now=now,
    )
    me = [e for e in events if e["event_type"] == "matcher_eval"]
    assert len(me) == 1
    assert me[0]["matcher_snapshot"]["confidence_tier"] == "floor"


# =============================================================================
# LIVE-PG: clause-mapping / drift-pin (the anti-drift guard)
# =============================================================================

_REF = datetime.datetime(2026, 6, 7, 12, 0, 0, tzinfo=timezone.utc)


def _reaped_and_reason(db_cur, driver_id, offer_id, *, cumulative_miles, effective_last_move):
    """Run the REAL predicate; return (is_reaped, classify_reason). Pins classifier⇔SQL."""
    db_cur.execute(
        f"""
        SELECT id::text AS id FROM app_private.offer_history oh
        WHERE decision_log_id IN (SELECT id FROM app_private.decision_log WHERE driver_id = %s)
          AND {LIVE_OFFER_PREDICATE_SQL}
        """,
        (driver_id,) + live_offer_predicate_params(cumulative_miles, _REF, effective_last_move),
    )
    live_ids = {r["id"] for r in db_cur.fetchall()}
    is_reaped = str(offer_id) not in live_ids
    probe = EL.probe_reaped_offers(db_cur, driver_id, [str(offer_id)])
    reason = EL.classify_reap(
        probe.get(str(offer_id)),
        reference_time=_REF, cumulative_miles=cumulative_miles, effective_last_move=effective_last_move,
    )
    return is_reaped, reason


def test_clause_terminated(db_cur, test_driver_id, seed_decision_log, seed_offer_history):
    dl = seed_decision_log(test_driver_id)
    oid = seed_offer_history(dl, created_at=_REF, actual_dropoff_at=_REF)
    reaped, reason = _reaped_and_reason(db_cur, test_driver_id, oid,
                                        cumulative_miles=100.0, effective_last_move=_REF)
    assert reaped and reason == EL.REAP_TERMINATED


def test_clause_causality(db_cur, test_driver_id, seed_decision_log, seed_offer_history):
    dl = seed_decision_log(test_driver_id)
    oid = seed_offer_history(dl, created_at=_REF + timedelta(hours=1))   # future offer
    reaped, reason = _reaped_and_reason(db_cur, test_driver_id, oid,
                                        cumulative_miles=100.0, effective_last_move=_REF)
    assert reaped and reason == EL.REAP_CAUSALITY


def test_clause_ceiling(db_cur, test_driver_id, seed_decision_log, seed_offer_history):
    dl = seed_decision_log(test_driver_id)
    oid = seed_offer_history(dl, created_at=_REF - timedelta(hours=5))   # older than 4h
    reaped, reason = _reaped_and_reason(db_cur, test_driver_id, oid,
                                        cumulative_miles=100.0, effective_last_move=_REF)
    assert reaped and reason == EL.REAP_CEILING


def test_clause_staleness(db_cur, test_driver_id, seed_decision_log, seed_offer_history):
    dl = seed_decision_log(test_driver_id)
    oid = seed_offer_history(dl, created_at=_REF - timedelta(hours=1))   # within ceiling, predates freeze
    reaped, reason = _reaped_and_reason(
        db_cur, test_driver_id, oid,
        cumulative_miles=100.0, effective_last_move=_REF - timedelta(minutes=45),  # frozen >30m
    )
    assert reaped and reason == EL.REAP_STALENESS


def test_clause_band_overshot(db_cur, test_driver_id, seed_decision_log, seed_offer_history):
    dl = seed_decision_log(test_driver_id)
    oid = seed_offer_history(
        dl, created_at=_REF, expected_pickup_distance=100.0, pickup_miles=10.0,
        actual_pickup_at=None,   # pickup leg
    )
    reaped, reason = _reaped_and_reason(
        db_cur, test_driver_id, oid,
        cumulative_miles=130.0,        # well over 100 + max(0.15*10, floor)
        effective_last_move=_REF,
    )
    assert reaped and reason.startswith(EL.REAP_BAND) and reason.endswith("pickup")


def test_clause_abandoned(db_cur, test_driver_id, seed_decision_log, seed_offer_history):
    dl = seed_decision_log(test_driver_id)
    oid = seed_offer_history(dl, created_at=_REF, expected_odometer_status="abandoned")
    reaped, reason = _reaped_and_reason(db_cur, test_driver_id, oid,
                                        cumulative_miles=100.0, effective_last_move=_REF)
    assert reaped and reason == EL.REAP_ABANDONED


# =============================================================================
# LIVE-PG: emit/seed roundtrip + keystone runtime-true
# =============================================================================

def test_emit_event_and_seed_roundtrip(db_cur, test_driver_id):
    # driver_trip_state row required for the seed UPDATE (PK driver_id)
    db_cur.execute(
        "INSERT INTO app_private.driver_trip_state (driver_id) VALUES (%s) "
        "ON CONFLICT (driver_id) DO NOTHING",
        (test_driver_id,),
    )
    EL.emit_event(db_cur, test_driver_id,
                  {"event_type": "keyframe", "queue_snapshot": {"ids": ["9001"]}})
    db_cur.execute(
        "SELECT event_type, queue_snapshot FROM app_private.event_ledger WHERE driver_id = %s",
        (test_driver_id,),
    )
    row = db_cur.fetchone()
    assert row["event_type"] == "keyframe"
    assert row["queue_snapshot"]["ids"] == ["9001"]

    seed = {"last_queue_snapshot_ids": ["9001"], "keyframe_count": 0}
    EL.advance_ledger_seed(db_cur, test_driver_id, seed)
    assert EL.read_ledger_seed(db_cur, test_driver_id)["last_queue_snapshot_ids"] == ["9001"]


def test_keystone_runtime_true_atjb_insert_ok_update_denied(db_cur, test_driver_id):
    """The proof catalog-true can't give: as the atjb runtime role, INSERT succeeds but
    UPDATE on the populated child partition is denied. Skips if not run as a non-superuser
    (the keystone only constrains the runtime role)."""
    db_cur.execute("SELECT current_user AS u, (SELECT rolsuper FROM pg_roles WHERE rolname=current_user) AS super")
    who = db_cur.fetchone()
    if who["super"]:
        pytest.skip(f"keystone test must run as the non-super runtime role, not {who['u']}")

    EL.emit_event(db_cur, test_driver_id, {"event_type": "keyframe"})   # INSERT ok as atjb
    db_cur.execute("SAVEPOINT ks")
    with pytest.raises(psycopg2.errors.InsufficientPrivilege):
        db_cur.execute("UPDATE app_private.event_ledger SET summary='x' WHERE driver_id = %s",
                       (test_driver_id,))
    db_cur.execute("ROLLBACK TO SAVEPOINT ks")
    db_cur.execute("SAVEPOINT ks2")
    with pytest.raises(psycopg2.errors.InsufficientPrivilege):
        db_cur.execute("DELETE FROM app_private.event_ledger WHERE driver_id = %s", (test_driver_id,))
    db_cur.execute("ROLLBACK TO SAVEPOINT ks2")
