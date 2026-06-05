"""Tests for driver_queue.py — DriverQueue API + L-19 invariant.

Mocking pattern: MagicMock cursor with fetchone/fetchall return values
shaped as RealDictCursor would deliver them (psycopg2.extras.RealDictRow,
which dict-likes work for at the test layer).

Coverage:
  - Construction & guards (RuntimeError when builder missing)
  - offer_ids_only: empty / single / multiple, query shape
  - bound_offer_id: None / NULL / populated
  - snapshot: happy path / empty / L-19 INVARIANT_VIOLATION / unbuildable geocode
  - offers: happy / empty
  - bind / unbind: UPDATE issued correctly, idempotency
  - force_bind: UPSERT issued, INFO log emitted
  - Offer / QueueSnapshot dataclass behavior
"""

from __future__ import annotations

import inspect
import logging
import re
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from driver_queue import (
    DriverQueue,
    Offer,
    QueueSnapshot,
)


# =============================================================================
# Fixtures
# =============================================================================

DRIVER_ID = "test_driver_uid_xyz"


def make_cursor():
    """A bare MagicMock cursor. Tests configure fetchone / fetchall as needed."""
    cur = MagicMock()
    cur.fetchone = MagicMock()
    cur.fetchall = MagicMock()
    cur.execute = MagicMock()
    return cur


def make_offer_row(offer_id, address_prefix="123 Main St", lat=29.76, lng=-95.37,
                   created_at=None, pickup_minutes=10, trip_minutes=20):
    """Shape the SELECT in _project_offers returns. RealDictRow is dict-compatible."""
    return {
        "id": int(offer_id),
        "pickup_address": f"{address_prefix} pickup",
        "dropoff_address": f"{address_prefix} dropoff",
        "pickup_lat": lat,
        "pickup_lng": lng,
        "dropoff_lat": lat + 0.01,
        "dropoff_lng": lng + 0.01,
        "created_at": created_at or datetime.now(timezone.utc),
    }


def passthrough_builder(address, lat, lng):
    """Test builder: returns a sentinel dict that DriverQueue treats as a TargetSpec."""
    if address is None:
        return None
    return {"address": address, "lat": lat, "lng": lng}


def null_returning_builder(address, lat, lng):
    """Test builder that always returns None (simulates unbuildable geocode)."""
    return None


# =============================================================================
# Construction & guards
# =============================================================================

import datetime as _datetime
_NOW_FOR_TEST = _datetime.datetime.now(_datetime.timezone.utc)


def test_construct_without_builder_succeeds():
    q = DriverQueue(DRIVER_ID)
    assert q.driver_id == DRIVER_ID
    assert q._build_target_spec is None


def test_construct_with_builder_stores_it():
    q = DriverQueue(DRIVER_ID, target_spec_builder=passthrough_builder)
    assert q._build_target_spec is passthrough_builder


def test_snapshot_without_builder_raises_runtime_error():
    q = DriverQueue(DRIVER_ID)
    cur = make_cursor()
    with pytest.raises(RuntimeError, match="target_spec_builder"):
        q.snapshot(cur, current_cumulative_miles=None, last_odometer_move_at=None)


def test_offers_without_builder_raises_runtime_error():
    q = DriverQueue(DRIVER_ID)
    cur = make_cursor()
    with pytest.raises(RuntimeError, match="target_spec_builder"):
        q.offers(cur, current_cumulative_miles=None, last_odometer_move_at=None)


# =============================================================================
# offer_ids_only — no builder needed
# =============================================================================

def test_offer_ids_only_empty_queue():
    q = DriverQueue(DRIVER_ID)
    cur = make_cursor()
    cur.fetchall.return_value = []

    result = q.offer_ids_only(cur, current_cumulative_miles=None, last_odometer_move_at=None)

    assert result == ()
    cur.execute.assert_called_once()
    sql, params = cur.execute.call_args[0]
    assert "FROM app_private.offer_history" in sql
    assert "actual_dropoff_at IS NULL" in sql
    assert params[0] == DRIVER_ID  # driver_id binding position


def test_offer_ids_only_single_offer():
    q = DriverQueue(DRIVER_ID)
    cur = make_cursor()
    cur.fetchall.return_value = [{"offer_id": "12345"}]

    result = q.offer_ids_only(cur, current_cumulative_miles=None, last_odometer_move_at=None)

    assert result == ("12345",)


def test_offer_ids_only_multiple_offers_preserves_order():
    """The SQL ORDER BY created_at DESC; we just pass through whatever rows return."""
    q = DriverQueue(DRIVER_ID)
    cur = make_cursor()
    cur.fetchall.return_value = [
        {"offer_id": "9999"},
        {"offer_id": "9998"},
        {"offer_id": "9997"},
    ]

    result = q.offer_ids_only(cur, current_cumulative_miles=None, last_odometer_move_at=None)

    assert result == ("9999", "9998", "9997")


def test_offer_ids_only_query_uses_canonical_gc_constants():
    """Regression guard: the GC tuning constants must thread into the query
    via the canonical LIVE_OFFER_PREDICATE_SQL params helper.

    Updated 2026-05-10 (P0 GC predicate apply): previously asserted a
    6-element tuple matching the inline GC clause. Post-apply,
    offer_ids_only routes through live_offer_predicate_params(), so the
    bind tuple is driver_id plus the 12-element canonical params
    (5 time-axis + 7 distance-axis; distance short-circuits to None
    when current_cumulative_miles is None).
    """
    q = DriverQueue(DRIVER_ID)
    cur = make_cursor()
    cur.fetchall.return_value = []

    q.offer_ids_only(cur, current_cumulative_miles=None, last_odometer_move_at=None)

    _, params = cur.execute.call_args[0]
    # Structural shape check: production captures _now() at call
    # time, so timestamp slots are non-deterministic at the
    # microsecond level. Tuple shape (post-P10/P11 2026-05-19,
    # staleness gate replaces time horizon):
    #   [0]      DRIVER_ID
    #   [1]      reference_time (Causality Guard)
    #   [2]      last_odometer_move_at (NULL guard)
    #   [3]      last_odometer_move_at (age comparison LHS)
    #   [4]      reference_time (age comparison RHS)
    #   [5]      GC_ODOMETER_FREEZE_MINUTES
    #   [6]      reference_time (new-offer exemption RHS)
    #   [7]      GC_ODOMETER_FREEZE_MINUTES
    #   [8]      current_cumulative_miles (NULL guard)
    #   [9]      current_cumulative_miles (math)
    #   [10:15]  5 distance-axis constants
    # The 14-element params helper tuple plus DRIVER_ID = 15 total.
    import datetime as _dt
    from driver_queue import GC_ODOMETER_FREEZE_MINUTES as _GC_FREEZE
    expected = (DRIVER_ID,) + _dq_module.live_offer_predicate_params(None, _NOW_FOR_TEST)
    assert len(params) == len(expected), (
        f"tuple length mismatch: got {len(params)}, expected {len(expected)}"
    )
    assert params[0] == expected[0]

    # Causality Guard reference_time slot
    assert isinstance(params[1], _dt.datetime), "causality reference_time slot must be datetime"
    assert params[1].tzinfo is not None, "causality reference_time must be timezone-aware"
    drift_causality = abs((params[1] - _NOW_FOR_TEST).total_seconds())
    assert drift_causality < 60, f"causality reference_time drift {drift_causality}s — capture race?"

    # Staleness gate slots [2:8] — replaces the deprecated time-horizon
    # axis as the offer-liveness time signal. Since offer_ids_only does
    # not pass last_odometer_move_at, slots [2] and [3] default to None
    # (graceful degradation — staleness gate evaluates permissively).
    # Reference_time slots [4] and [6] must bind the SAME captured value
    # as slot [1]. GC_ODOMETER_FREEZE_MINUTES at [5] and [7] must match
    # the canonical module constant.
    assert params[4] is None, "staleness NULL-guard slot must be None when last_odometer_move_at unset"
    assert params[5] is None, "staleness age-LHS slot must be None when last_odometer_move_at unset"

    assert isinstance(params[6], _dt.datetime), "staleness age-RHS slot must be datetime"
    assert params[6].tzinfo is not None, "staleness age-RHS slot must be timezone-aware"

    assert params[7] == _GC_FREEZE, (
        f"staleness interval slot drifted: got {params[7]}, expected {_GC_FREEZE}"
    )

    assert isinstance(params[8], _dt.datetime), "staleness new-offer-exemption slot must be datetime"
    assert params[8].tzinfo is not None, "staleness new-offer-exemption slot must be timezone-aware"

    assert params[9] == _GC_FREEZE, (
        f"new-offer-exemption interval slot drifted: got {params[9]}, expected {_GC_FREEZE}"
    )

    # Shared-reference_time invariant: all three reference_time slots
    # in a single helper call must bind the SAME _now() capture. If they
    # diverge, the helper accidentally called _now() multiple times
    # instead of using the passed-in reference_time argument once.
    assert params[1] == params[2] == params[6] == params[8], (
        "Causality Guard, staleness age-RHS, and new-offer-exemption "
        "reference_time slots must bind the same _now() capture (all "
        "come from the single reference_time argument to "
        "live_offer_predicate_params)"
    )

    # Distance-axis params (shifted to [8:] under new architecture)
    assert params[10:] == expected[10:], "distance-axis params drifted"


# =============================================================================
# bound_offer_id — read-only hint access
# =============================================================================

def test_bound_offer_id_no_row_returns_none():
    q = DriverQueue(DRIVER_ID)
    cur = make_cursor()
    cur.fetchone.return_value = None

    assert q.bound_offer_id(cur) is None


def test_bound_offer_id_null_value_returns_none():
    q = DriverQueue(DRIVER_ID)
    cur = make_cursor()
    cur.fetchone.return_value = {"current_offer_id": None}

    assert q.bound_offer_id(cur) is None


def test_bound_offer_id_populated_returns_str():
    q = DriverQueue(DRIVER_ID)
    cur = make_cursor()
    cur.fetchone.return_value = {"current_offer_id": "7712"}

    assert q.bound_offer_id(cur) == "7712"


def test_bound_offer_id_int_value_coerced_to_str():
    """driver_trip_state.current_offer_id is text, but defensive coercion
    matches _project_queue's behavior — `str(val) if val else None`."""
    q = DriverQueue(DRIVER_ID)
    cur = make_cursor()
    cur.fetchone.return_value = {"current_offer_id": 7712}

    assert q.bound_offer_id(cur) == "7712"


# =============================================================================
# snapshot — the L-19 vaccine
# =============================================================================

def test_snapshot_happy_path_bound_in_queue():
    """Standard case: queue has the offer, bound points at it, no warning."""
    q = DriverQueue(DRIVER_ID, target_spec_builder=passthrough_builder)
    cur = make_cursor()
    # Fetchall returns offers; fetchone returns the bound row
    cur.fetchall.return_value = [make_offer_row("7712")]
    cur.fetchone.return_value = {"current_offer_id": "7712"}

    snap = q.snapshot(cur, current_cumulative_miles=None, last_odometer_move_at=None)

    assert isinstance(snap, QueueSnapshot)
    assert len(snap.offers) == 1
    assert snap.offers[0].offer_id == "7712"
    assert snap.bound_offer_id == "7712"


def test_snapshot_empty_queue_null_bound():
    """Pre-pickup natural state: no offers seen, no binding."""
    q = DriverQueue(DRIVER_ID, target_spec_builder=passthrough_builder)
    cur = make_cursor()
    cur.fetchall.return_value = []
    cur.fetchone.return_value = {"current_offer_id": None}

    snap = q.snapshot(cur, current_cumulative_miles=None, last_odometer_move_at=None)

    assert snap.is_empty
    assert snap.bound_offer_id is None
    assert snap.offer_ids == frozenset()


def test_snapshot_l19_invariant_violation_self_heals(caplog):
    """THE MARQUEE TEST: bound_offer_id points at an offer not in queue.

    L-19's exact failure mode: priming current_offer_id without a fresh
    offer_history row. snapshot() must (a) return None for the hint and
    (b) emit a WARNING tagged INVARIANT_VIOLATION carrying the full
    forensic payload.

    Updated 2026-05-31: snapshot() now also consults
    _is_offer_definitively_dead when the invariant trips. Two fetchone
    returns are wired via side_effect — first the bound row (matches
    pre-amendment behavior), then None (offer absent from
    offer_history → TRANSIENT, NOT dead). The original WARNING
    semantics are preserved on the transient branch; the dead-and-
    reconcile branch is covered by tests in
    tests/test_driver_queue_reconcile_stale_pointer.py.
    """
    q = DriverQueue(DRIVER_ID, target_spec_builder=passthrough_builder)
    cur = make_cursor()
    # Queue has offer 9000, but bound_offer_id points at 7712 (stale)
    cur.fetchall.return_value = [make_offer_row("9000")]
    cur.fetchone.side_effect = [
        {"current_offer_id": "7712"},  # _select_bound_offer_id
        None,                          # _is_offer_definitively_dead -> absent -> transient
    ]

    with caplog.at_level(logging.WARNING, logger="driver_queue"):
        snap = q.snapshot(cur, current_cumulative_miles=None, last_odometer_move_at=None)

    # The hint is self-healed to None; the queue itself is preserved.
    assert snap.bound_offer_id is None
    assert len(snap.offers) == 1
    assert snap.offers[0].offer_id == "9000"

    # The forensic warning fired with all the diagnostic data L-19's hunt
    # would have wanted in a single line.
    assert len(caplog.records) == 1
    msg = caplog.records[0].getMessage()
    assert "INVARIANT_VIOLATION" in msg
    assert "driver_id=" + DRIVER_ID in msg
    assert "bound_offer_id=7712" in msg
    assert "queue_size=1" in msg
    assert "queue_offer_ids=[9000]" in msg


def test_snapshot_l19_violation_with_empty_queue(caplog):
    """The exact L-19 scenario: queue is empty (offers aged out), bound
    still set. Self-heal to None, log the full payload including empty
    queue list.

    Updated 2026-05-31: see test_snapshot_l19_invariant_violation_self_heals
    comment for the fetchone.side_effect rationale.
    """
    q = DriverQueue(DRIVER_ID, target_spec_builder=passthrough_builder)
    cur = make_cursor()
    cur.fetchall.return_value = []
    cur.fetchone.side_effect = [
        {"current_offer_id": "7712"},  # _select_bound_offer_id
        None,                          # _is_offer_definitively_dead -> absent
    ]

    with caplog.at_level(logging.WARNING, logger="driver_queue"):
        snap = q.snapshot(cur, current_cumulative_miles=None, last_odometer_move_at=None)

    assert snap.bound_offer_id is None
    assert snap.is_empty
    msg = caplog.records[0].getMessage()
    assert "queue_size=0" in msg
    assert "queue_offer_ids=[]" in msg


def test_snapshot_l19_violation_logs_sorted_ids(caplog):
    """Forensic stability: queue ID list in the warning is sorted, so a
    grep for a known-offending ID matches deterministically regardless
    of created_at ordering.

    Updated 2026-05-31: see test_snapshot_l19_invariant_violation_self_heals
    comment for the fetchone.side_effect rationale.
    """
    q = DriverQueue(DRIVER_ID, target_spec_builder=passthrough_builder)
    cur = make_cursor()
    cur.fetchall.return_value = [
        make_offer_row("9999"),
        make_offer_row("1111"),
        make_offer_row("5555"),
    ]
    cur.fetchone.side_effect = [
        {"current_offer_id": "7712"},  # _select_bound_offer_id
        None,                          # _is_offer_definitively_dead -> absent
    ]

    with caplog.at_level(logging.WARNING, logger="driver_queue"):
        q.snapshot(cur, current_cumulative_miles=None, last_odometer_move_at=None)

    assert "queue_offer_ids=[1111,5555,9999]" in caplog.records[0].getMessage()


def test_snapshot_unbuildable_geocode_skipped(caplog):
    """Behavior preservation from _project_queue: offers with null
    pickup or dropoff specs get logged and excluded."""
    q = DriverQueue(DRIVER_ID, target_spec_builder=null_returning_builder)
    cur = make_cursor()
    cur.fetchall.return_value = [make_offer_row("7712")]
    cur.fetchone.return_value = {"current_offer_id": None}

    with caplog.at_level(logging.WARNING, logger="driver_queue"):
        snap = q.snapshot(cur, current_cumulative_miles=None, last_odometer_move_at=None)

    assert snap.is_empty
    # Warning fired, but it's the geocode warning, not invariant violation
    msgs = [r.getMessage() for r in caplog.records]
    assert any("unbuildable geocode" in m for m in msgs)
    assert not any("INVARIANT_VIOLATION" in m for m in msgs)


# =============================================================================
# offers — projection without invariant
# =============================================================================

def test_offers_returns_full_projection():
    q = DriverQueue(DRIVER_ID, target_spec_builder=passthrough_builder)
    cur = make_cursor()
    cur.fetchall.return_value = [make_offer_row("7712"), make_offer_row("8000")]

    result = q.offers(cur, current_cumulative_miles=None, last_odometer_move_at=None)

    assert len(result) == 2
    assert isinstance(result, tuple)
    assert all(isinstance(o, Offer) for o in result)
    assert {o.offer_id for o in result} == {"7712", "8000"}


def test_offers_empty():
    q = DriverQueue(DRIVER_ID, target_spec_builder=passthrough_builder)
    cur = make_cursor()
    cur.fetchall.return_value = []

    assert q.offers(cur, current_cumulative_miles=None, last_odometer_move_at=None) == ()


# =============================================================================
# bind / unbind — write surface
# =============================================================================

def test_bind_issues_update():
    q = DriverQueue(DRIVER_ID)
    cur = make_cursor()

    q.bind("7712", cur)

    cur.execute.assert_called_once()
    sql, params = cur.execute.call_args[0]
    assert "UPDATE app_private.driver_trip_state" in sql
    assert "SET current_offer_id = %s" in sql
    assert "WHERE driver_id = %s" in sql
    assert params == ("7712", DRIVER_ID)


def test_bind_idempotent_reissues_update():
    """Re-binding to the same offer is a no-op semantically but still issues
    the UPDATE (Postgres treats it as a normal write that changes no values).
    Verifies we don't silently skip — the call always reaches the DB."""
    q = DriverQueue(DRIVER_ID)
    cur = make_cursor()

    q.bind("7712", cur)
    q.bind("7712", cur)

    assert cur.execute.call_count == 2


def test_unbind_issues_update_with_null():
    q = DriverQueue(DRIVER_ID)
    cur = make_cursor()

    q.unbind(cur)

    cur.execute.assert_called_once()
    sql, params = cur.execute.call_args[0]
    assert "UPDATE app_private.driver_trip_state" in sql
    assert "SET current_offer_id = NULL" in sql
    assert "WHERE driver_id = %s" in sql
    assert params == (DRIVER_ID,)


def test_unbind_idempotent():
    q = DriverQueue(DRIVER_ID)
    cur = make_cursor()

    q.unbind(cur)
    q.unbind(cur)

    assert cur.execute.call_count == 2


def test_unbind_default_path_silent(caplog):
    """Default unbind (clear_heartbeat=False) issues NO log records.

    Regression guard: FireDropoff fires unbind() on every dropoff,
    ~thousands/day in production. The default path must stay silent
    to avoid drowning INFO logs. The clear_heartbeat=True branch is
    the only one that logs (rarer, used only by test infrastructure).
    """
    q = DriverQueue(DRIVER_ID)
    cur = make_cursor()

    with caplog.at_level(logging.INFO, logger="driver_queue"):
        q.unbind(cur)

    assert len(caplog.records) == 0


def test_unbind_with_clear_heartbeat_clears_three_columns():
    """clear_heartbeat=True: same single UPDATE, but clears
    current_offer_id, heartbeat, AND heartbeat_at on one row.
    Used by /api/v1/test/reset_driver to route the multi-column
    teardown through the queue API instead of raw SQL."""
    q = DriverQueue(DRIVER_ID)
    cur = make_cursor()

    q.unbind(cur, clear_heartbeat=True)

    cur.execute.assert_called_once()
    sql, params = cur.execute.call_args[0]
    assert "UPDATE app_private.driver_trip_state" in sql
    assert "current_offer_id = NULL" in sql
    assert "heartbeat = NULL" in sql
    assert "heartbeat_at = NULL" in sql
    assert "WHERE driver_id = %s" in sql
    assert params == (DRIVER_ID,)


def test_unbind_with_clear_heartbeat_emits_info_log(caplog):
    """clear_heartbeat=True emits an INFO log with the +heartbeat
    qualifier so forensic timeline reconstruction can distinguish a
    normal dropoff unbind from a test reset."""
    q = DriverQueue(DRIVER_ID)
    cur = make_cursor()

    with caplog.at_level(logging.INFO, logger="driver_queue"):
        q.unbind(cur, clear_heartbeat=True)

    msgs = [r.getMessage() for r in caplog.records]
    assert any(
        "unbind+heartbeat" in m and DRIVER_ID in m for m in msgs
    ), f"Expected unbind+heartbeat log with driver_id; got: {msgs}"


# =============================================================================
# force_bind — test-only escape hatch
# =============================================================================

def test_force_bind_issues_upsert():
    q = DriverQueue(DRIVER_ID)
    cur = make_cursor()

    q.force_bind("7712", cur)

    cur.execute.assert_called_once()
    sql, params = cur.execute.call_args[0]
    assert "INSERT INTO app_private.driver_trip_state" in sql
    assert "ON CONFLICT (driver_id) DO UPDATE" in sql
    assert params == (DRIVER_ID, "7712")


def test_force_bind_emits_info_log(caplog):
    q = DriverQueue(DRIVER_ID)
    cur = make_cursor()

    with caplog.at_level(logging.INFO, logger="driver_queue"):
        q.force_bind("7712", cur)

    msgs = [r.getMessage() for r in caplog.records]
    assert any("force_bind" in m and "7712" in m and DRIVER_ID in m for m in msgs)


# =============================================================================
# Dataclass behavior
# =============================================================================

def test_offer_is_frozen():
    o = Offer(offer_id="x", accepted_at=datetime.now(timezone.utc),
              pickup={}, dropoff={})
    with pytest.raises(Exception):  # FrozenInstanceError
        o.offer_id = "y"


def test_queue_snapshot_is_frozen():
    snap = QueueSnapshot(offers=(), bound_offer_id=None)
    with pytest.raises(Exception):
        snap.offers = ()


def test_queue_snapshot_offer_ids_property():
    o1 = Offer(offer_id="a", accepted_at=datetime.now(timezone.utc),
               pickup={}, dropoff={})
    o2 = Offer(offer_id="b", accepted_at=datetime.now(timezone.utc),
               pickup={}, dropoff={})
    snap = QueueSnapshot(offers=(o1, o2), bound_offer_id="a")
    assert snap.offer_ids == frozenset({"a", "b"})


def test_queue_snapshot_is_empty_property():
    empty = QueueSnapshot(offers=(), bound_offer_id=None)
    assert empty.is_empty

    o = Offer(offer_id="a", accepted_at=datetime.now(timezone.utc),
              pickup={}, dropoff={})
    nonempty = QueueSnapshot(offers=(o,), bound_offer_id=None)
    assert not nonempty.is_empty


# =============================================================================
# Drift detector — Sub-commit 1b (Option 3 compromise per Gemini's review)
# =============================================================================
#
# `offer_ids_only` and `_project_offers` carry parallel WHERE clauses
# against `app_private.offer_history`. Both implement the GC-survivor
# predicate: actual_dropoff_at IS NULL AND created_at within the
# per-offer Houston Tax window. If they ever drift, snapshot()'s L-19
# invariant (which compares bound_offer_id against the _project_offers
# result) becomes inconsistent with what offer_ids_only callers
# (monitor, status, drive_review) see — silently. This test extracts
# the WHERE...ORDER BY block from each method's SQL and asserts they
# match after lowercase + whitespace normalization. Catches predicate
# changes; ignores indentation and keyword case so cosmetic edits
# don't break the build.

import driver_queue as _dq_module


_WHERE_BLOCK_PATTERN = re.compile(
    r"(WHERE\s+decision_log_id\s+IN.*?)\s+ORDER\s+BY\s+created_at",
    re.DOTALL | re.IGNORECASE,
)


def _extract_normalized_where(method_source: str) -> str:
    """Extract the WHERE...ORDER BY block from a method's source and
    return it lowercased with whitespace runs collapsed to single spaces."""
    match = _WHERE_BLOCK_PATTERN.search(method_source)
    if not match:
        raise AssertionError(
            f"Drift detector could not locate WHERE...ORDER BY block. "
            f"This means the SQL structure changed in a way the regex "
            f"doesn't recognize — update the regex AND verify the queries "
            f"still match. Source searched:\n{method_source}"
        )
    raw = match.group(1)
    return re.sub(r"\s+", " ", raw.lower()).strip()


def test_offer_ids_only_and_project_offers_share_where_clause():
    """Drift detector. Both methods filter offer_history with the same
    GC-survivor predicate; if they ever diverge, snapshot's L-19 invariant
    becomes unreliable.

    Failure here means a developer edited one method's SQL without
    updating the other. Either propagate the change to both methods,
    or — if the divergence is intentional — extract the shared predicate
    into a constant so there's only one source of truth.
    """
    src_offer_ids = inspect.getsource(_dq_module.DriverQueue.offer_ids_only)
    src_project = inspect.getsource(_dq_module.DriverQueue._project_offers)

    where_offer_ids = _extract_normalized_where(src_offer_ids)
    where_project = _extract_normalized_where(src_project)

    assert where_offer_ids == where_project, (
        "GC-survivor WHERE clauses have drifted between offer_ids_only "
        "and _project_offers.\n\n"
        f"offer_ids_only WHERE:\n{where_offer_ids}\n\n"
        f"_project_offers WHERE:\n{where_project}"
    )
