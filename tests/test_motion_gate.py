"""Unit tests for motion_gate.evaluate_motion_gate (Sprint A §7).

Covers the four motion verdicts: closed / moving / transient / no_cluster.
Uses dataclass fakes (Cluster duck-typed by motion_gate, no real Cluster
import needed — keeps these tests independent of where_am_i.py / clustering
modules so they survive matcher-side refactors).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pytest

from motion_gate import (
    MOTION_MAX_SPEED_MPH,
    MOTION_MIN_DURATION_S,
    evaluate_motion_gate,
)


@dataclass
class FakeCluster:
    """Duck-typed Cluster for gate testing.

    motion_gate prefers .max_recent_speed_mph (per §7 wording); we
    expose it as the canonical field. test_speed_field_fallback verifies
    the .speed_mph fallback path.
    """
    duration_s: float
    max_recent_speed_mph: Optional[float] = None
    speed_mph: Optional[float] = None  # fallback


# =============================================================================
# Verdict: "closed"
# =============================================================================

class TestClosed:
    def test_below_speed_above_duration(self):
        c = FakeCluster(duration_s=20.0, max_recent_speed_mph=0.0)
        assert evaluate_motion_gate(c) == "closed"

    def test_at_duration_threshold_exactly(self):
        # Threshold is >=, so exactly 20.0 should pass.
        c = FakeCluster(duration_s=20.0, max_recent_speed_mph=1.5)
        assert evaluate_motion_gate(c) == "closed"

    def test_well_above_duration(self):
        c = FakeCluster(duration_s=300.0, max_recent_speed_mph=0.0)
        assert evaluate_motion_gate(c) == "closed"

    def test_speed_just_below_threshold(self):
        # Threshold is <, so 1.999... passes, 2.0 doesn't.
        c = FakeCluster(duration_s=30.0, max_recent_speed_mph=1.99)
        assert evaluate_motion_gate(c) == "closed"


# =============================================================================
# Verdict: "moving"
# =============================================================================

class TestMoving:
    def test_speed_at_threshold_holds(self):
        # Threshold is <, so 2.0 exactly is moving.
        c = FakeCluster(duration_s=30.0, max_recent_speed_mph=MOTION_MAX_SPEED_MPH)
        assert evaluate_motion_gate(c) == "moving"

    def test_speed_above_threshold_holds(self):
        c = FakeCluster(duration_s=120.0, max_recent_speed_mph=8.3)
        assert evaluate_motion_gate(c) == "moving"

    def test_speed_dominates_over_short_duration(self):
        # Both gates would fail; speed checked first, returns "moving"
        c = FakeCluster(duration_s=2.0, max_recent_speed_mph=15.0)
        assert evaluate_motion_gate(c) == "moving"

    def test_30mph_drive_by(self):
        # The kickoff's "drive-by at 30mph past strip mall" scenario.
        c = FakeCluster(duration_s=5.0, max_recent_speed_mph=30.0)
        assert evaluate_motion_gate(c) == "moving"


# =============================================================================
# Verdict: "transient"
# =============================================================================

class TestTransient:
    def test_below_speed_below_duration(self):
        # Stopped, but not long enough.
        c = FakeCluster(duration_s=5.0, max_recent_speed_mph=0.0)
        assert evaluate_motion_gate(c) == "transient"

    def test_below_duration_threshold_by_one_second(self):
        c = FakeCluster(duration_s=MOTION_MIN_DURATION_S - 1, max_recent_speed_mph=0.0)
        assert evaluate_motion_gate(c) == "transient"

    def test_brief_5s_tap(self):
        # The kickoff's "Synthetic: brief 5s tap" scenario.
        c = FakeCluster(duration_s=5.0, max_recent_speed_mph=0.0)
        assert evaluate_motion_gate(c) == "transient"


# =============================================================================
# Verdict: "no_cluster"
# =============================================================================

class TestNoCluster:
    def test_none_returns_no_cluster(self):
        assert evaluate_motion_gate(None) == "no_cluster"


# =============================================================================
# Defensive: missing fields, custom thresholds, fallback
# =============================================================================

class TestDefensive:
    def test_missing_speed_falls_to_moving(self):
        # Defensive policy: gate fails closed when speed unavailable.
        c = FakeCluster(duration_s=120.0, max_recent_speed_mph=None, speed_mph=None)
        assert evaluate_motion_gate(c) == "moving"

    def test_missing_duration_falls_to_moving(self):
        c = FakeCluster(duration_s=None, max_recent_speed_mph=0.0)  # type: ignore[arg-type]
        assert evaluate_motion_gate(c) == "moving"

    def test_speed_fallback_to_speed_mph(self):
        # When max_recent_speed_mph is missing, use speed_mph.
        c = FakeCluster(duration_s=30.0, max_recent_speed_mph=None, speed_mph=1.0)
        assert evaluate_motion_gate(c) == "closed"

    def test_max_recent_takes_precedence_over_speed_mph(self):
        # When both present, max_recent_speed_mph wins (the canonical
        # field per §7 "max recent heartbeat speed" wording).
        c = FakeCluster(duration_s=30.0, max_recent_speed_mph=0.0, speed_mph=10.0)
        assert evaluate_motion_gate(c) == "closed"


class TestCustomThresholds:
    def test_override_max_speed(self):
        c = FakeCluster(duration_s=30.0, max_recent_speed_mph=5.0)
        # Default threshold (2.0) -> moving.
        assert evaluate_motion_gate(c) == "moving"
        # Permissive threshold -> closed.
        assert evaluate_motion_gate(c, max_speed_mph=10.0) == "closed"

    def test_override_min_duration(self):
        c = FakeCluster(duration_s=10.0, max_recent_speed_mph=0.0)
        # Default threshold (20s) -> transient.
        assert evaluate_motion_gate(c) == "transient"
        # Permissive threshold -> closed.
        assert evaluate_motion_gate(c, min_duration_s=5.0) == "closed"


# =============================================================================
# Threshold provenance sanity check
# =============================================================================

class TestProvenance:
    def test_speed_threshold_matches_section_7(self):
        # Per SIMPLIFIED_ARCHITECTURE.md §7: "speed_mph < 2"
        assert MOTION_MAX_SPEED_MPH == 2.0

    def test_duration_threshold_matches_section_7_default(self):
        # Per §7: "Default MOTION_GATE_DURATION_S = 20"
        assert MOTION_MIN_DURATION_S == 20.0


# =============================================================================
# Production Cluster shape regression (Sprint A bugfix 2026-05-06)
# =============================================================================
#
# Background: the production Cluster dataclass in cluster_detection.py
# exposes only geometry fields (n, median_lat, median_lng, spread_m,
# duration_s, latest) — NO speed field. The synthetic FakeCluster used
# above carries a max_recent_speed_mph attribute that production never
# had. Pre-fix, evaluate_motion_gate's getattr fallback returned None,
# tripping the defensive "moving" branch on every real heartbeat and
# disabling Auto Nail It in production.
#
# Fix: thread speed_mph from the heartbeat request body explicitly into
# evaluate_motion_gate. Cluster stays a pure geometry object.
#
# These tests use a Cluster mock that EXACTLY matches the production
# shape — no speed field at all — and assert that explicit speed_mph
# kwarg drives the verdict.


@dataclass
class ProductionShapeCluster:
    """Mirror of cluster_detection.Cluster (no speed field).

    Field set must stay in lockstep with cluster_detection.py:42-71.
    If that dataclass adds or removes a field, this mirror must too,
    or this test stops protecting against the regression class.
    """
    n: int
    median_lat: float
    median_lng: float
    spread_m: float
    duration_s: float
    # NB: deliberately no max_recent_speed_mph, no speed_mph.
    # latest: datetime  -- not needed for gate evaluation, omitted.


class TestProductionClusterShape:
    """Regression tests for the 2026-05-06 mock-mirage bug.

    A stationary driver with a tight, sustained cluster MUST verdict
    closed. Pre-fix, this class would have produced 'moving' on every
    test because the cluster has no speed field for getattr to find.
    """

    def _stationary_cluster(self, duration_s=60.0):
        """Tight cluster, well past min_duration_s threshold."""
        return ProductionShapeCluster(
            n=11,
            median_lat=29.5063919,
            median_lng=-95.5022621,
            spread_m=8.0,
            duration_s=duration_s,
        )

    def test_stationary_speed_with_no_cluster_speed_field_yields_closed(self):
        """The exact production scenario: 0.5mph request body, no speed
        field on cluster, duration well past 20s."""
        cluster = self._stationary_cluster(duration_s=60.0)
        verdict = evaluate_motion_gate(cluster, speed_mph=0.5)
        assert verdict == "closed"

    def test_stationary_zero_speed_yields_closed(self):
        cluster = self._stationary_cluster(duration_s=60.0)
        verdict = evaluate_motion_gate(cluster, speed_mph=0.0)
        assert verdict == "closed"

    def test_real_motion_speed_yields_moving(self):
        """Driver actually moving — speed crosses threshold even with
        a tight cluster (cluster might be a momentary stop blip)."""
        cluster = self._stationary_cluster(duration_s=60.0)
        verdict = evaluate_motion_gate(cluster, speed_mph=15.0)
        assert verdict == "moving"

    def test_borderline_speed_at_threshold_yields_moving(self):
        """Exactly at MOTION_MAX_SPEED_MPH — the >= comparison must hold."""
        cluster = self._stationary_cluster(duration_s=60.0)
        verdict = evaluate_motion_gate(cluster, speed_mph=MOTION_MAX_SPEED_MPH)
        assert verdict == "moving"

    def test_short_duration_with_low_speed_yields_transient(self):
        """Stopped briefly (red light, rolling stop): low speed, short
        duration. Must be transient, not closed and not moving."""
        cluster = self._stationary_cluster(duration_s=5.0)
        verdict = evaluate_motion_gate(cluster, speed_mph=0.5)
        assert verdict == "transient"

    def test_explicit_speed_kwarg_overrides_cluster_attribute(self):
        """Even if a cluster mock somehow exposes max_recent_speed_mph,
        the explicit speed_mph kwarg from the request body wins.
        Source-of-truth ordering: heartbeat request body > cluster shape."""
        cluster = self._stationary_cluster(duration_s=60.0)
        # Synthesize a hybrid: production shape + a phantom speed field.
        # The kwarg should still drive the verdict.
        object.__setattr__(cluster, 'max_recent_speed_mph', 99.0)
        verdict = evaluate_motion_gate(cluster, speed_mph=0.5)
        assert verdict == "closed", (
            "speed_mph kwarg must take precedence over cluster attribute"
        )

    def test_missing_speed_kwarg_falls_back_to_cluster_attribute(self):
        """Backward-compat: legacy callers passing only cluster (no
        speed_mph) still hit the getattr fallback. Production caller
        always passes speed_mph; this is for synthetic harnesses."""
        cluster = self._stationary_cluster(duration_s=60.0)
        # Production-shape cluster has no speed field; no kwarg either.
        # Defensive branch fires -> "moving" (the very bug we just fixed,
        # but legacy mode preserves it for opt-in callers).
        verdict = evaluate_motion_gate(cluster)
        assert verdict == "moving", (
            "Without speed_mph kwarg AND without cluster speed field, "
            "the defensive branch must still fire — that contract is "
            "preserved for backward compat with legacy synthetic mocks."
        )
