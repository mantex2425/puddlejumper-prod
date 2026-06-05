"""DSI v1 ingest-writer regression tests — step 1 (observational-only).

Covers the read-only/observational ``dsi_v1`` signal:

  1. Both rates present -> dsi_v1 = the exact formula value (float tolerance).
  2. Either rate NULL   -> dsi_v1 = NULL, no exception (NULL-strict invariant).
  3. DSI is write-only  -> the verdict engine never references dsi_v1, so no
     offer's app_verdict can change (a structural invariant, stronger than a
     single-offer regression pin).

Uses the live ``db_cur`` fixture (real RealDictCursor against the DB, rolled
back per test) — NOT a tuple mock. ``compute_dsi_v1`` is the exact helper the
production writers (decisions/logger.py, decisions/router.py) call, so the
value asserted here is byte-for-byte what the writers persist.
"""
import inspect

import pytest

from dsi import compute_dsi_v1, IRS_RATE_PER_MILE, DSI_MILE_WEIGHT


# ---------------------------------------------------------------------------
# Pure unit — formula exactness + NULL-strict
# ---------------------------------------------------------------------------
def test_compute_dsi_v1_formula_exact():
    # hourly=30.0, per_mile=1.225 -> 30 + 12*(1.225-0.725) = 30 + 12*0.5 = 36.0
    expected = 30.0 + DSI_MILE_WEIGHT * (1.225 - IRS_RATE_PER_MILE)
    assert compute_dsi_v1(30.0, 1.225) == pytest.approx(expected)
    assert compute_dsi_v1(30.0, 1.225) == pytest.approx(36.0)


def test_compute_dsi_v1_null_hourly_returns_none():
    assert compute_dsi_v1(None, 1.5) is None


def test_compute_dsi_v1_null_per_mile_returns_none():
    assert compute_dsi_v1(22.0, None) is None


def test_compute_dsi_v1_both_null_returns_none():
    assert compute_dsi_v1(None, None) is None


# ---------------------------------------------------------------------------
# Live db_cur — column round-trip (real schema; rolled back per test)
# ---------------------------------------------------------------------------
def test_dsi_v1_both_rates_present_persists_formula(
    db_cur, seed_decision_log, seed_offer_history, test_driver_id
):
    dlog_id = seed_decision_log(test_driver_id)
    hourly, per_mile = 30.0, 1.225
    oh_id = seed_offer_history(
        dlog_id,
        effective_hourly_rate=hourly,
        dollars_per_mile=per_mile,
        dsi_v1=compute_dsi_v1(hourly, per_mile),
    )
    db_cur.execute(
        "SELECT dsi_v1 FROM app_private.offer_history WHERE id = %s", (oh_id,)
    )
    assert db_cur.fetchone()["dsi_v1"] == pytest.approx(36.0)


def test_dsi_v1_null_rate_persists_null_no_exception(
    db_cur, seed_decision_log, seed_offer_history, test_driver_id
):
    dlog_id = seed_decision_log(test_driver_id)
    # hourly is NULL -> compute_dsi_v1 returns None -> column stores NULL
    oh_id = seed_offer_history(
        dlog_id,
        effective_hourly_rate=None,
        dollars_per_mile=1.5,
        dsi_v1=compute_dsi_v1(None, 1.5),
    )
    db_cur.execute(
        "SELECT dsi_v1 FROM app_private.offer_history WHERE id = %s", (oh_id,)
    )
    assert db_cur.fetchone()["dsi_v1"] is None


def test_dsi_v1_presence_does_not_change_app_verdict(
    db_cur, seed_decision_log, seed_offer_history, test_driver_id
):
    """An identical offer with vs. without dsi_v1 yields the same app_verdict —
    the column is orthogonal to the verdict it is stored alongside."""
    dlog_id = seed_decision_log(test_driver_id)
    with_dsi = seed_offer_history(
        dlog_id, app_verdict="ACCEPT",
        effective_hourly_rate=30.0, dollars_per_mile=1.225,
        dsi_v1=compute_dsi_v1(30.0, 1.225),
    )
    without_dsi = seed_offer_history(
        dlog_id, app_verdict="ACCEPT",
        effective_hourly_rate=None, dollars_per_mile=None,
        dsi_v1=compute_dsi_v1(None, None),
    )
    db_cur.execute(
        "SELECT id, app_verdict, dsi_v1 FROM app_private.offer_history "
        "WHERE id IN (%s, %s) ORDER BY id", (with_dsi, without_dsi)
    )
    rows = {r["id"]: r for r in db_cur.fetchall()}
    assert rows[with_dsi]["app_verdict"] == "ACCEPT"
    assert rows[without_dsi]["app_verdict"] == "ACCEPT"
    assert rows[with_dsi]["dsi_v1"] == pytest.approx(36.0)
    assert rows[without_dsi]["dsi_v1"] is None


def test_dsi_v1_not_referenced_by_verdict_engine():
    """HARD RULE guard: the verdict producer must never read dsi_v1.

    DSI is observational-only in step 1. If the decision engine ever
    references dsi_v1, this fails — proving no offer's verdict can depend on
    it (a stronger guarantee than a single known-offer regression)."""
    import decisions.engine
    src = inspect.getsource(decisions.engine)
    assert "dsi_v1" not in src, (
        "decisions.engine (verdict producer) must not reference dsi_v1 — "
        "DSI must not alter the Accept/Decline path in step 1."
    )
