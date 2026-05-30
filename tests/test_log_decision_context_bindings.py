"""Unit tests for _log_decision_context POI bindings + regression guard.

Phase 2c.2 commit 2: establishes baseline pytest coverage for the
pudo_decision_context INSERT writer, focused on the three POI columns
this sprint wires (poi_lookup_source, poi_match_score, poi_top_names)
plus a regression guard that asserts existing wai_* bindings continue
to populate correctly.

§XVII Patch 4 (2026-05-14, follow-up Patch 4a): poi_lookup_source binding
was corrected to receive top_outcome.semantic_lookup_source (the POI-data
source: semantic_cache_hit / semantic_api_call / semantic_api_error)
instead of top_outcome.poi_witness (a Head-1 match-witness string). The
test test_pdc_poi_lookup_source_populated was rewritten to codify the
corrected invariant. The _StandinMatchOutcome fake was extended with the
5 fields it had been missing across Item 2 + Patch 2 + Patch 4.

Pre-sprint state: zero pytest coverage existed for _log_decision_context;
all writer-level testing was via integration drives. This file is the
unit-level floor going forward.

Pattern: mock cursor (MagicMock with execute capture). We assert on the
SQL parameter tuple the cursor receives — the function's contract is
"build the right INSERT params from MatchOutcome + DiagnosticContext +
dispatch state." We do not test the SQL itself; that's integration's
job (tests/test_integration.sh).

Test count: 6
  - test_pdc_poi_match_score_populated
  - test_pdc_poi_lookup_source_populated
  - test_pdc_poi_top_names_populated
  - test_pdc_poi_top_names_empty_logs_null
  - test_pdc_poi_fields_null_when_no_top_match
  - test_pdc_existing_wai_bindings_unchanged_regression_guard

Pytest floor: 558 -> 564.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional
from unittest.mock import MagicMock

import pytest

from driver_heartbeat import _log_decision_context


# ============================================================================
# Fixtures
# ============================================================================
#
# Real DiagnosticContext / MatchOutcome would pull a heavier import graph
# (frozen dataclasses, Cluster, RoadTopology, etc). The writer only reads
# attributes, so duck-typed stand-ins are sufficient and faster. L-9
# provenance: these stand-ins exist to test the writer's attribute reads,
# not to substitute for real WAI evaluation.


@dataclass
class _StandinCluster:
    median_lat: float = 29.50628
    median_lng: float = -95.50231
    n: int = 7
    duration_s: int = 34
    started_at: object = None


@dataclass
class _StandinTopo:
    current_road: Optional[str] = "McKeever Rd"
    current_road_class: Optional[str] = "residential"
    off_wire_duration_s: Optional[int] = 0


@dataclass
class _StandinMatchOutcome:
    """Duck-types where_am_i.MatchOutcome for writer-level tests."""
    location_type: Optional[str] = "pickup"
    offer_id: Optional[str] = "7771"
    confidence: Optional[float] = 0.85
    target_address: Optional[str] = "7623 Forum Park Dr"
    reason: Optional[str] = "matched on poi-branded fuzzy"
    signals: Optional[dict] = None
    poi_match: Optional[float] = None
    poi_witness: Optional[str] = None
    # Item 2 fields (Phase 2c.2, 2026-05-08): Head 4 poi_type witnesses.
    poi_type_match: Optional[bool] = None
    poi_type_witness: Optional[str] = None
    # §XVII Patch 2 fields (2026-05-14): Head 5 semantic anchor signal.
    semantic_anchor_score: Optional[float] = None
    semantic_anchor_witness: Optional[str] = None
    # §XVII Patch 4 field (2026-05-14): POILookupResult.source plumbed
    # through matcher pipeline. Values: semantic_cache_hit,
    # semantic_api_call, semantic_api_error, or None.
    semantic_lookup_source: Optional[str] = None


@dataclass
class _StandinMatch:
    location_type: str = "pickup"
    offer_id: str = "7771"
    confidence: float = 0.85


@dataclass
class _StandinDiagnostics:
    """Duck-types where_am_i.DiagnosticContext for writer-level tests.

    per_target_outcomes mirrors where_am_i.DiagnosticContext's field of
    the same name (where_am_i.py:1742): list[tuple[offer_id, leg,
    MatchOutcome]]. Added 2026-05-30 alongside the wai_per_offer_scores
    telemetry column — _build_wai_per_offer_scores in driver_heartbeat.py
    reads diagnostics.per_target_outcomes unconditionally, so the standin
    must define it or AttributeError fires before INSERT runs. Default
    empty list: helper returns None for empty input (same path as a
    heartbeat with no offers in queue), which matches what these
    writer-level tests want — they assert on flat wai_* / poi_* columns,
    not on the JSONB blob.
    """
    cluster: Optional[_StandinCluster] = field(default_factory=_StandinCluster)
    topology: Optional[_StandinTopo] = field(default_factory=_StandinTopo)
    cluster_revisit: bool = False
    tad_verdicts: dict = field(default_factory=dict)
    cluster_poi_names: list = field(default_factory=list)
    per_target_outcomes: list = field(default_factory=list)
    _outcomes_by_match: dict = field(default_factory=dict)

    def outcome_for(self, match):
        key = (match.offer_id, match.location_type)
        return self._outcomes_by_match.get(key)


def _make_diagnostics(outcome=None, cluster_poi_names=None):
    """Build a DiagnosticContext stand-in wired to a MatchOutcome.

    If `outcome` provided, registers it for the canonical match
    (offer_id="7771", location_type="pickup") so outcome_for() returns it.
    If `cluster_poi_names` provided, sets the cluster-scoped POI list.
    """
    diag = _StandinDiagnostics()
    if cluster_poi_names is not None:
        diag.cluster_poi_names = cluster_poi_names
    if outcome is not None:
        diag._outcomes_by_match[("7771", "pickup")] = outcome
    return diag


def _capture_insert_params(cur):
    """Extract the params tuple from cur.execute(sql, params).

    The writer calls cur.execute exactly once with the INSERT. This
    helper grabs the params positional-arg of that call.
    """
    assert cur.execute.call_count == 1, (
        f"expected exactly 1 cur.execute call, got {cur.execute.call_count}"
    )
    args, kwargs = cur.execute.call_args
    # cur.execute(sql, params) — params is args[1]
    assert len(args) == 2, f"expected (sql, params), got {len(args)} args"
    return args[1]


def _call_log(diagnostics, matches, **overrides):
    """Invoke _log_decision_context with sane defaults; return params tuple."""
    cur = MagicMock()
    defaults = dict(
        cur=cur,
        driver_id="UjT1hE9eBXh2q95aSZYOkzDJ8lo1",
        body={"heading": 90, "gpsAgeSec": 1.2},
        current_lat=29.50628,
        current_lng=-95.50231,
        speed_mph=0.0,
        gps_accuracy_m=5.0,
        current_offer_id="7771",
        diagnostics=diagnostics,
        matches=matches,
        executed_actions=[],
        dispatch_executed=False,
        dispatch_error_msg=None,
        lost_mode=False,
        last_known_anchor_id=None,
    )
    defaults.update(overrides)
    _log_decision_context(**defaults)
    return _capture_insert_params(cur)


# Column index map — mirrors the INSERT column list order in
# driver_heartbeat.py:_log_decision_context. Maintained alongside the
# writer; if the column order ever changes, this map updates with it.
COL = {
    "driver_id": 0,
    "current_offer_id_at_eval": 1,
    "current_offer_id": 2,
    "lat": 3,
    "lng": 4,
    "speed_mph": 5,
    "heading": 6,
    "gps_accuracy_m": 7,
    "gps_age_s": 8,
    "wai_pudo_type": 9,
    "wai_offer_id": 10,
    "wai_confidence": 11,
    "wai_target_address": 12,
    "wai_reason": 13,
    "wai_on_target_road": 14,
    "wai_current_road": 15,
    "wai_current_road_class": 16,
    "wai_off_wire_duration_s": 17,
    "wai_cluster_revisit": 18,
    "cluster_lat": 19,
    "cluster_lng": 20,
    "cluster_size": 21,
    "cluster_duration_s": 22,
    "cluster_started_at": 23,
    "poi_lookup_source": 24,
    "poi_match_score": 25,
    "poi_top_names": 26,
    "planner_action": 27,
    "dispatch_executed": 28,
    "dispatch_error": 29,
    "motion_gate_result": 30,
    "tad_decision_context": 34,
}


# ============================================================================
# POI binding tests (the new wiring)
# ============================================================================

class TestPoiBindings:
    """The three POI columns must populate from MatchOutcome + diagnostics."""

    def test_pdc_poi_match_score_populated(self):
        """top_outcome.poi_match -> poi_match_score column."""
        outcome = _StandinMatchOutcome(poi_match=0.85)
        match = _StandinMatch()
        diag = _make_diagnostics(outcome=outcome)
        params = _call_log(diag, [match])
        assert params[COL["poi_match_score"]] == 0.85

    def test_pdc_poi_lookup_source_populated(self):
        """top_outcome.semantic_lookup_source -> poi_lookup_source column.

        §XVII Patch 4 (2026-05-14) corrected the binding here. Pre-Patch-4,
        the column was bound to top_outcome.poi_witness — a Head-1 witness
        string like 'fuzzy:Pappadeaux'. That binding had drifted: the
        column is *meant* to record where the POI data came from, not
        which head won the match. The column had been 100% NULL in
        production for 7 days because Head 1 was dormant; the drift
        only would have started producing wrong rows the moment Head 1
        or Head 5 fired in volume.

        Patch 4 routes top_outcome.semantic_lookup_source to the column,
        with values: 'semantic_cache_hit', 'semantic_api_call',
        'semantic_api_error', or None. Head 5 wins the tie-break when
        its score >= Head 1's score (None as 0), so this test sets
        Head 5 score positive and leaves Head 1 unset.
        """
        outcome = _StandinMatchOutcome(
            semantic_anchor_score=0.85,
            semantic_anchor_witness="semantic_anchor:United/transportation_service (88m)",
            semantic_lookup_source="semantic_cache_hit",
        )
        match = _StandinMatch()
        diag = _make_diagnostics(outcome=outcome)
        params = _call_log(diag, [match])
        assert params[COL["poi_lookup_source"]] == "semantic_cache_hit"

    def test_pdc_poi_top_names_populated(self):
        """diagnostics.cluster_poi_names -> poi_top_names column.

        Format is 'Name (Xm)' per Gemini's psql-scannability ratification.
        """
        outcome = _StandinMatchOutcome(poi_match=0.5, poi_witness="fuzzy:CVS")
        match = _StandinMatch()
        names = ["Starbucks (12m)", "Chase Bank (38m)", "CVS (47m)"]
        diag = _make_diagnostics(outcome=outcome, cluster_poi_names=names)
        params = _call_log(diag, [match])
        assert params[COL["poi_top_names"]] == names


# ============================================================================
# Edge cases
# ============================================================================

class TestPoiEdgeCases:
    """Empty-list semantics and no-top-match path."""

    def test_pdc_poi_top_names_empty_logs_null(self):
        """cluster_poi_names=[] coerces to SQL NULL, not empty Postgres array.

        Distinguishes 'no POIs at this cluster' (legitimate forensic
        signal) from 'POIs present but didn't score'. The writer's
        guard is `... if diagnostics.cluster_poi_names else None`.
        """
        outcome = _StandinMatchOutcome(poi_match=0.0, poi_witness=None)
        match = _StandinMatch()
        diag = _make_diagnostics(outcome=outcome, cluster_poi_names=[])
        params = _call_log(diag, [match])
        assert params[COL["poi_top_names"]] is None

    def test_pdc_poi_fields_null_when_no_top_match(self):
        """Empty matches list -> all three POI columns NULL.

        Mirrors existing wai_* behavior: the writer derives top_match
        from matches[0]; with no matches, top_outcome is None and the
        defensive guards on poi_match/poi_witness yield None. The
        cluster_poi_names path runs independently of matches.
        """
        diag = _make_diagnostics(outcome=None, cluster_poi_names=[])
        params = _call_log(diag, matches=[])
        assert params[COL["poi_lookup_source"]] is None
        assert params[COL["poi_match_score"]] is None
        assert params[COL["poi_top_names"]] is None


# ============================================================================
# Regression guard
# ============================================================================

class TestRegressionGuard:
    """Existing wai_* bindings must continue to populate after the patch.

    Insurance against an apply-script slip damaging adjacent INSERT
    bindings (the POI lines sit immediately after wai_target_address etc.,
    so a misaligned str_replace could silently shift columns).
    """

    def test_pdc_existing_wai_bindings_unchanged_regression_guard(self):
        outcome = _StandinMatchOutcome(
            target_address="7623 Forum Park Dr",
            reason="matched on poi-branded fuzzy",
            confidence=0.85,
        )
        match = _StandinMatch(confidence=0.85)
        diag = _make_diagnostics(outcome=outcome)
        params = _call_log(diag, [match])

        # The full wai_* family — values must come through unchanged.
        assert params[COL["wai_pudo_type"]] == "pickup"
        assert params[COL["wai_offer_id"]] == "7771"
        assert params[COL["wai_confidence"]] == 0.85
        assert params[COL["wai_target_address"]] == "7623 Forum Park Dr"
        assert params[COL["wai_reason"]] == "matched on poi-branded fuzzy"
        assert params[COL["wai_current_road"]] == "McKeever Rd"
        assert params[COL["wai_current_road_class"]] == "residential"
        assert params[COL["wai_off_wire_duration_s"]] == 0
        assert params[COL["wai_cluster_revisit"]] is False

        # Cluster bindings sit between wai_* and poi_* — verify they're
        # also intact.
        assert params[COL["cluster_lat"]] == 29.50628
        assert params[COL["cluster_lng"]] == -95.50231
        assert params[COL["cluster_size"]] == 7
        assert params[COL["cluster_duration_s"]] == 34
