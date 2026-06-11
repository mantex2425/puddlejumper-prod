"""Unit tests for the DSI (Drive Score Index) verdict override — spec §6.

Covers the PURE decision logic (`_dsi_verdict`) — no DB. The driver picks one of three
modes (UserPreferences -> settings.decision_mode): TRADITIONAL, DSI_OBSERVATIONAL,
DSI_ACTIVE. Only DSI_ACTIVE overrides the verdict; all three emit the DSI telemetry.
The correctness-critical behaviour (the comparison, the radar-less fallback, and the
switch-mode coherence the spec flags) lives here.
"""
from decisions.engine import (
    _dsi_verdict, DSI_FALLBACK_THRESHOLD,
    DSI_MODE_TRADITIONAL, DSI_MODE_OBSERVATIONAL, DSI_MODE_ACTIVE,
)
from dsi import compute_personal_dsi, DSI_MILE_WEIGHT, IRS_RATE_PER_MILE


# ── Personal DSI formula (dsi.py) ─────────────────────────────────────────
def test_personal_dsi_formula():
    assert compute_personal_dsi(20.0, 1.0, 0.45) == 20.0 + 12 * (1.0 - 0.45)


def test_personal_dsi_null_strict():
    assert compute_personal_dsi(None, 1.0, 0.5) is None
    assert compute_personal_dsi(20.0, None, 0.5) is None
    assert compute_personal_dsi(20.0, 1.0, None) is None   # missing cost -> None (never assume)


def test_personal_beats_standard_when_cost_below_irs():
    standard = 20.0 + DSI_MILE_WEIGHT * (1.0 - IRS_RATE_PER_MILE)
    assert compute_personal_dsi(20.0, 1.0, 0.45) > standard   # cheap car -> takes more (the edge)


# ── Mode behaviour (_dsi_verdict) ─────────────────────────────────────────
def test_traditional_keeps_sql_verdict_but_emits_telemetry():
    out = _dsi_verdict(30.0, 22.0, DSI_MODE_TRADITIONAL, {"verdict": "DECLINE"})
    assert "verdict" not in out                  # SQL verdict stands ($/hr & $/mi only)
    assert out["legacyVerdict"] == "DECLINE" and out["decisionMode"] == DSI_MODE_TRADITIONAL
    assert out["personalDsi"] == 30.0 and out["localMarketDsi"] == 22.0   # logged regardless


def test_observational_shows_numbers_keeps_sql_verdict():
    # OBSERVATIONAL: numbers surfaced (personalDsi/localMarketDsi), but the SQL verdict drives.
    out = _dsi_verdict(30.0, 22.0, DSI_MODE_OBSERVATIONAL, {"verdict": "DECLINE"})
    assert "verdict" not in out                  # NOT overridden — safe verdict still wins
    assert out["personalDsi"] == 30.0 and out["localMarketDsi"] == 22.0
    assert out["decisionMode"] == DSI_MODE_OBSERVATIONAL


def test_active_uncomputable_keeps_sql_verdict():
    out = _dsi_verdict(None, 22.0, DSI_MODE_ACTIVE, {"verdict": "ACCEPT"})
    assert "verdict" not in out


def test_active_accept_at_or_above_market():
    out = _dsi_verdict(30.0, 22.0, DSI_MODE_ACTIVE, {"verdict": "DECLINE"})
    assert out["verdict"] == "ACCEPT" and out["legacyVerdict"] == "DECLINE"   # disagreement preserved


def test_active_decline_below_market():
    assert _dsi_verdict(18.0, 22.0, DSI_MODE_ACTIVE, {"verdict": "ACCEPT"})["verdict"] == "DECLINE"


def test_active_accept_clears_switch():
    # Spec-flagged: SQL says DECLINE + switch markets; DSI ACCEPT must win AND clear the switch.
    sql = {"verdict": "DECLINE", "switchToMode": "FREESTYLE", "switchToMarketId": "mkt-xyz"}
    out = _dsi_verdict(30.0, 22.0, DSI_MODE_ACTIVE, sql)
    assert out["verdict"] == "ACCEPT"
    assert out["switchToMode"] is None and out["switchToMarketId"] is None


def test_active_decline_leaves_switch_intact():
    sql = {"verdict": "DECLINE", "switchToMode": "FREESTYLE", "switchToMarketId": "mkt-xyz"}
    out = _dsi_verdict(18.0, 22.0, DSI_MODE_ACTIVE, sql)
    assert out["verdict"] == "DECLINE"
    assert "switchToMode" not in out and "switchToMarketId" not in out


def test_active_empty_radar_falls_back_to_threshold():
    assert _dsi_verdict(25.0, None, DSI_MODE_ACTIVE, {"v": 1})["verdict"] == "ACCEPT"   # 25 >= 22
    assert _dsi_verdict(20.0, None, DSI_MODE_ACTIVE, {"v": 1})["verdict"] == "DECLINE"  # 20 < 22
    assert _dsi_verdict(DSI_FALLBACK_THRESHOLD, None, DSI_MODE_ACTIVE, {"v": 1})["verdict"] == "ACCEPT"
