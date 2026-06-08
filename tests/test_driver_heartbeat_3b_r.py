"""Unit tests for Phase 2c.2 Item 3b.R caller-layer wiring.

Tests the five helpers added to driver_heartbeat.py that activate the
WAI brain's dual commit rule by assembling TAD context from offer_history:

  _to_utc                        — defensive tz-attach
  _assemble_per_offer_state      — fetch per-offer state via JOIN
  _get_last_known_anchor_id      — find most recent confirmed anchor
  _detect_lost_mode              — narrative_blindness detection (Bible Rule 7a)
  _build_tad_decision_context    — JSONB blob serializer (Bible Rule 5)

Coverage: 20 tests across 5 test classes. Targets test floor 521 → 541.

These tests run against the helpers in isolation via mocked psycopg2
RealDictCursor (cursor-level fakes; no DB). Higher-level integration
already lives in test_where_am_i.py (DiagnosticContext + dual commit
rule) and test_tad.py (evaluate_tad_gate semantics). 3b.R wires the
two halves together; this file proves the wiring helpers individually.
"""
# RULE VII MIGRATION 2026-05-12:
# _detect_lost_mode semantics changed. Old rule used
# `actual_pickup_at IS NULL` + `interval '2 hours'` as proxies.
# New rule uses LIVE_OFFER_PREDICATE_SQL — pure horizon physics.
#
# Existing tests in this file MAY need fixture review:
#   - Tests that fire a pickup and assert lost_mode=False should
#     still pass IF the offer's horizon hasn't blown, but the
#     semantic meaning has shifted: it's the horizon (not the
#     pickup fire) that decides.
#   - Tests that insert a recently-accepted no-pickup offer and
#     assert lost_mode=True should still pass (offer is in horizon,
#     so it's still flagged as orphan).
#
# See tests/test_lost_mode_horizon.py for the new canonical
# behavioral cases (Houston Miss, Calhoun Zombie, etc).
#

from __future__ import annotations

import datetime
import json
from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest

from driver_heartbeat import (
    _to_utc,
    _assemble_per_offer_state,
    _get_last_known_anchor_id,
    _detect_lost_mode,
    _build_tad_decision_context,
)
from tad import OfferTadState, TadVerdict


# ============================================================================
# Helpers
# ============================================================================

UTC = datetime.timezone.utc


import datetime as _dt
_T_NOW = _dt.datetime.now(_dt.timezone.utc)


def _aware(year, month, day, hour=0, minute=0):
    return datetime.datetime(year, month, day, hour, minute, tzinfo=UTC)


def _naive(year, month, day, hour=0, minute=0):
    return datetime.datetime(year, month, day, hour, minute)


def _row(**overrides):
    """Build an offer_history row with sensible defaults for tests."""
    base = {
        "id": 7771,
        "miles_at_offer_receipt": 145.5,
        "created_at": _aware(2026, 5, 8, 14, 0),
        "expected_pickup_arrival_time": _aware(2026, 5, 8, 14, 8),
        "expected_pickup_distance": 148.2,
        "actual_pickup_at": None,
        "cumulative_miles_at_pickup_fire": None,
        "pickup_exit_time": None,
        "exit_velocity_timeout": False,
        "expected_odometer_status": "active",  # F3: _assemble reads this; 'deferred' builds
                                               # a None-anchor state instead of excluding.
    }
    base.update(overrides)
    return base


def _mock_cursor(rows=None, fetchone_value=None):
    """Build a mock cursor with fetchall + fetchone return values seeded."""
    cur = MagicMock()
    cur.fetchall = MagicMock(return_value=rows or [])
    cur.fetchone = MagicMock(return_value=fetchone_value)
    return cur


# Minimal fakes for _build_tad_decision_context tests. We avoid pulling in
# the full DiagnosticContext / MatchOutcome / WAIMatch surface from
# where_am_i.py because that module is intentionally unmodified by 3b.R;
# touching its test fixtures would couple the test files unnecessarily.

@dataclass
class _FakeOutcome:
    confidence: float
    matched: bool = True
    poi_type_match: bool = False


@dataclass
class _FakeMatch:
    offer_id: str
    confidence: float
    location_type: str = "single_road"


class _FakeDiagnostics:
    """Minimal DiagnosticContext stand-in for serialization tests.

    Real DiagnosticContext is a frozen dataclass with 6 fields; we only
    need tad_verdicts and outcome_for() for _build_tad_decision_context.
    """

    def __init__(self, tad_verdicts, outcomes_by_offer):
        self.tad_verdicts = tad_verdicts
        self._outcomes_by_offer = outcomes_by_offer

    def outcome_for(self, match):
        return self._outcomes_by_offer.get(match.offer_id)


def _make_verdict(passed=True, time_boost=0.0, leg="pickup", reason=None):
    """Build a TadVerdict with sensible defaults for tests."""
    return TadVerdict(
        passed=passed,
        time_boost=time_boost,
        leg_evaluated=leg,
        lost_mode_reason=reason,
        distance_gate={"mode": "normal", "passed": passed, "completion": 0.92},
        time_signal={"mode": "normal", "applied": True, "completion": 0.95},
    )


# ============================================================================
# TestToUtc — defensive tz-attach
# ============================================================================


class TestToUtc:

    def test_none_passes_through(self):
        assert _to_utc(None) is None

    def test_naive_gets_utc_attached(self):
        naive = _naive(2026, 5, 8, 14, 0)
        result = _to_utc(naive)
        assert result.tzinfo == UTC
        # Wall-clock preserved when attaching UTC
        assert result.replace(tzinfo=None) == naive

    def test_aware_passes_through_in_utc(self):
        aware = _aware(2026, 5, 8, 14, 0)
        result = _to_utc(aware)
        assert result == aware
        assert result.tzinfo == UTC


# ============================================================================
# TestAssemblePerOfferState — SQL JOIN to offer_history via decision_log
# ============================================================================


class TestAssemblePerOfferState:

    def test_empty_queue_returns_empty_dict_no_sql(self):
        cur = _mock_cursor()
        result = _assemble_per_offer_state(cur, "driver-x", [])
        assert result == {}
        cur.execute.assert_not_called()

    def test_pre_pickup_state(self):
        cur = _mock_cursor(rows=[_row(id=7771)])
        result = _assemble_per_offer_state(cur, "driver-x", [7771])
        assert "7771" in result
        s = result["7771"]
        assert isinstance(s, OfferTadState)
        assert s.offer_id == "7771"
        assert s.miles_at_offer_receipt == 145.5
        assert s.actual_pickup_at is None
        assert s.cumulative_miles_at_pickup_fire is None
        assert s.exit_velocity_timeout is False
        assert s.accepted_at.tzinfo == UTC

    def test_post_pickup_state(self):
        cur = _mock_cursor(rows=[_row(
            id=7772,
            actual_pickup_at=_aware(2026, 5, 8, 14, 7),
            cumulative_miles_at_pickup_fire=148.0,
            pickup_exit_time=_aware(2026, 5, 8, 14, 9),
        )])
        result = _assemble_per_offer_state(cur, "driver-x", [7772])
        s = result["7772"]
        assert s.actual_pickup_at is not None
        assert s.actual_pickup_at.tzinfo == UTC
        assert s.cumulative_miles_at_pickup_fire == 148.0
        assert s.pickup_exit_time is not None
        assert s.pickup_exit_time.tzinfo == UTC

    def test_null_expected_columns_excludes_offer(self):
        """Pre-3b.W rows have NULL expected_*; they must be excluded.

        evaluate_tad_gate then records a missing_state failed verdict for
        them rather than crashing on None math.
        """
        cur = _mock_cursor(rows=[
            _row(id=7770, expected_pickup_arrival_time=None,
                 expected_pickup_distance=None),
            _row(id=7771),
        ])
        result = _assemble_per_offer_state(cur, "driver-x", [7770, 7771])
        assert "7770" not in result
        assert "7771" in result

    def test_deferred_offer_null_anchors_is_built_not_excluded_f3(self):
        """F3: a §5.5-deferred offer has NULL expected_* but is a CURRENT,
        spatially-matchable offer — it must be BUILT (with None anchors), not
        excluded like a legacy row, so _evaluate_pickup_leg can route it to the
        passed=None lost-mode verdict instead of TAD passed=False -> dispatch-skip."""
        cur = _mock_cursor(rows=[
            _row(id=7780, expected_odometer_status="deferred",
                 expected_pickup_arrival_time=None, expected_pickup_distance=None),
        ])
        result = _assemble_per_offer_state(cur, "driver-x", [7780])
        assert "7780" in result                                    # built, NOT excluded
        assert result["7780"].expected_pickup_distance is None     # None anchor preserved
        assert result["7780"].expected_pickup_arrival_time is None
        assert result["7780"].miles_at_offer_receipt == 145.5      # odometer anchor still set

    def test_naive_timestamps_get_utc_attached_defensively(self):
        """psycopg2 connection-config drift defense (Q5 ratification)."""
        cur = _mock_cursor(rows=[_row(
            id=7771,
            created_at=_naive(2026, 5, 8, 14, 0),
            expected_pickup_arrival_time=_naive(2026, 5, 8, 14, 8),
            actual_pickup_at=_naive(2026, 5, 8, 14, 7),
        )])
        result = _assemble_per_offer_state(cur, "driver-x", [7771])
        s = result["7771"]
        assert s.accepted_at.tzinfo == UTC
        assert s.expected_pickup_arrival_time.tzinfo == UTC
        assert s.actual_pickup_at.tzinfo == UTC


# ============================================================================
# TestGetLastKnownAnchorId — most recent offer with confirmed PUDO
# ============================================================================


class TestGetLastKnownAnchorId:

    def test_no_anchors_returns_none(self):
        cur = _mock_cursor(fetchone_value=None)
        assert _get_last_known_anchor_id(cur, "driver-x", None, _T_NOW) is None

    def test_anchor_present_returns_string(self):
        cur = _mock_cursor(fetchone_value={"id": 7700})
        result = _get_last_known_anchor_id(cur, "driver-x", None, _T_NOW)
        assert result == "7700"
        assert isinstance(result, str)

    def test_sql_uses_offer_history_not_pudo_decision_context(self):
        """Source-of-truth path: query offer_history actual_*_at directly,
        not the derivative pudo_decision_context.planner_action history."""
        cur = _mock_cursor(fetchone_value=None)
        _get_last_known_anchor_id(cur, "driver-x", None, _T_NOW)
        sql_text = cur.execute.call_args[0][0]
        assert "offer_history" in sql_text
        assert "actual_pickup_at" in sql_text
        assert "actual_dropoff_at" in sql_text

    def test_sql_orders_by_most_recent_pudo(self):
        cur = _mock_cursor(fetchone_value=None)
        _get_last_known_anchor_id(cur, "driver-x", None, _T_NOW)
        sql_text = cur.execute.call_args[0][0]
        assert "ORDER BY" in sql_text
        assert "DESC" in sql_text
        assert "LIMIT 1" in sql_text


# ============================================================================
# TestDetectLostMode — Bible Rule 7a narrative_blindness detection
# ============================================================================


class TestDetectLostMode:

    def test_no_orphan_returns_false(self):
        # 2026-05-31: predicate body extracted to _get_alive_unpicked_offer_ids
        # which reads cur.fetchall() (returns the SET of alive-unpicked ids),
        # not cur.fetchone(). _detect_lost_mode delegates and returns len > 0.
        # Empty rows -> empty set -> False.
        cur = _mock_cursor(rows=[])
        assert _detect_lost_mode(cur, "driver-x", [7771], None, _T_NOW) is False

    def test_orphan_present_returns_true(self):
        # 2026-05-31: see test_no_orphan_returns_false comment. Non-empty
        # rows -> non-empty set -> True.
        cur = _mock_cursor(rows=[{"id": 7770}])
        assert _detect_lost_mode(cur, "driver-x", [7771], None, _T_NOW) is True

    def test_sql_does_not_filter_by_app_verdict(self):
        """§XVIII: PUDO infrastructure is advice-blind. The decision
        engine's accept/decline advice is NOT consulted in lost-mode
        detection. The car's physical position is the sole sensor of
        driver intent per §0.B and §XV.
        """
        cur = _mock_cursor(fetchone_value=None)
        _detect_lost_mode(cur, "driver-x", [7771], None, _T_NOW)
        sql_text = cur.execute.call_args[0][0]
        assert "ACCEPT" not in sql_text
        assert "app_verdict" not in sql_text

    def test_sql_uses_live_offer_predicate(self):
        """§XVIII: orphan-recency is governed by LIVE_OFFER_PREDICATE_SQL
        (horizon physics) PLUS the §XVIII trigger bit `actual_pickup_at
        IS NULL` (pickup not yet observed). The crude wall-clock window
        from pre-2026-05-12 is gone.
        """
        cur = _mock_cursor(fetchone_value=None)
        _detect_lost_mode(cur, "driver-x", [7771], None, _T_NOW)
        sql_text = cur.execute.call_args[0][0]
        # Horizon predicate must be spliced in. Marker token updated for the
        # Step 6 band (ERRATUM §4): the retired receipt anchor
        # `oh.miles_at_offer_receipt` was replaced by the per-leg band, whose
        # pickup-leg center `oh.expected_pickup_distance` is the stable marker
        # proving the band predicate is present.
        assert "oh.actual_dropoff_at IS NULL" in sql_text
        assert "oh.expected_pickup_distance" in sql_text
        # §XVIII trigger bit 2: pickup must be unfired
        assert "actual_pickup_at IS NULL" in sql_text
        # Pre-§XVIII proxies must be absent
        assert "interval '2 hours'" not in sql_text


# ============================================================================
# TestBuildTadDecisionContext — JSONB blob serializer (Bible Rule 5)
# ============================================================================


class TestBuildTadDecisionContext:

    def test_empty_verdicts_returns_none(self):
        """Bridge state preservation: when caller invokes
        evaluate_with_diagnostics() without per_offer_state, tad_verdicts
        is empty {} and the JSONB column writes NULL — preserves the 521
        regression baseline."""
        diag = _FakeDiagnostics(tad_verdicts={}, outcomes_by_offer={})
        result = _build_tad_decision_context(
            diagnostics=diag, matches=[], lost_mode=False,
            last_known_anchor_id=None,
        )
        assert result is None

    def test_full_shape_round_trip(self):
        verdict = _make_verdict(passed=True, time_boost=0.05)
        outcome = _FakeOutcome(confidence=0.92, poi_type_match=False)
        match = _FakeMatch(offer_id="7771", confidence=0.92)
        diag = _FakeDiagnostics(
            tad_verdicts={"7771": verdict},
            outcomes_by_offer={"7771": outcome},
        )
        result = _build_tad_decision_context(
            diagnostics=diag, matches=[match], lost_mode=False,
            last_known_anchor_id="7770",
        )
        assert result is not None
        parsed = json.loads(result)
        assert parsed["lost_mode"] is False
        assert parsed["last_known_anchor_id"] == "7770"
        assert "7771" in parsed["verdicts"]
        v = parsed["verdicts"]["7771"]
        assert v["passed"] is True
        assert v["time_boost"] == 0.05
        assert v["leg_evaluated"] == "pickup"
        assert v["distance_gate"]["completion"] == 0.92
        assert len(parsed["committed"]) == 1
        assert parsed["committed"][0]["offer_id"] == "7771"
        # Fix B (2026-05-11): Normal Mode + verdict.passed=True →
        # "normal_floor". POI lift is not active (poi_type_match=False),
        # so no _with_poi_lift suffix. The old "normal_high" label and its
        # 0.90 threshold were retired when POI became a lifter, not a gate.
        assert parsed["committed"][0]["commit_rule"] == "normal_floor"

    def test_lost_mode_propagates(self):
        verdict = _make_verdict(passed=None, leg="pickup",
                                reason="narrative_blindness")
        diag = _FakeDiagnostics(
            tad_verdicts={"7771": verdict},
            outcomes_by_offer={},
        )
        result = _build_tad_decision_context(
            diagnostics=diag, matches=[], lost_mode=True,
            last_known_anchor_id="7700",
        )
        parsed = json.loads(result)
        assert parsed["lost_mode"] is True
        assert parsed["last_known_anchor_id"] == "7700"
        assert parsed["verdicts"]["7771"]["passed"] is None
        assert parsed["verdicts"]["7771"]["lost_mode_reason"] == "narrative_blindness"
        assert parsed["committed"] == []

    def test_commit_rule_classification_normal_floor_with_poi_lift(self):
        """Fix B (2026-05-11): passed=True + poi_type_match=True →
        normal_floor_with_poi_lift. Replaces the legacy normal_elevator
        label which was retired when POI became a lifter, not a gate.

        The fixture confidence (0.85) is well above COMMIT_NORMAL_FLOOR (0.40),
        so this would commit under Fix B even without POI lift. The label
        captures that POI corroboration was present at decision time —
        forensic visibility into POIs contribution per Phase 2g tuning input.
        """
        verdict = _make_verdict(passed=True)
        outcome = _FakeOutcome(confidence=0.85, poi_type_match=True)
        match = _FakeMatch(offer_id="7771", confidence=0.85)
        diag = _FakeDiagnostics(
            tad_verdicts={"7771": verdict},
            outcomes_by_offer={"7771": outcome},
        )
        result = _build_tad_decision_context(
            diagnostics=diag, matches=[match], lost_mode=False,
            last_known_anchor_id=None,
        )
        parsed = json.loads(result)
        assert parsed["committed"][0]["commit_rule"] == "normal_floor_with_poi_lift"

    def test_no_committed_matches_keeps_committed_empty(self):
        """TAD evaluation occurred but no candidate cleared the elevator —
        verdicts captured, committed is []."""
        verdict = _make_verdict(passed=False)  # below 85% distance window
        diag = _FakeDiagnostics(
            tad_verdicts={"7771": verdict},
            outcomes_by_offer={},
        )
        result = _build_tad_decision_context(
            diagnostics=diag, matches=[], lost_mode=False,
            last_known_anchor_id=None,
        )
        parsed = json.loads(result)
        assert parsed["committed"] == []
        assert parsed["verdicts"]["7771"]["passed"] is False
