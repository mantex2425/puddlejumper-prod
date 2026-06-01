"""Live-PG regression tests for the §XVIII spatial-local bind gate.

Per docs/FIX_PROPOSAL_BIND_SPATIAL_LOCAL_2026-06-01.md §3.1 (and the
2026-06-01 scenario audit finding that test_lost_mode_cold_start_bind.py
is DEFENDED-MOCK-ONLY and therefore a false-confidence net under §XIV.J):
this file is the real-PG safety net for the spatial-local bind. Uses the
db_cur fixture (conftest.py) against the production database with
SAVEPOINT/ROLLBACK isolation.

Coverage maps to proposal §3.1 + §3.2:
  - test_scenarioA_morning_case_solo_floor_clearer_binds       (§3.1 A)
  - test_scenarioB_shared_curb_two_floor_clearers_withhold     (§3.1 B)
  - test_scenarioC_leg_retirement_intersection_unblocks_bind   (§3.1 C)
  - test_scenarioD_normal_mode_floor_via_commits               (§3.1 D)
  - test_gate_delegates_to_commits_structural                  (§3.2)

The four scenario tests exercise the end-to-end FPO write path through
real Postgres. They seed minimal real rows (decision_log, offer_history,
driver_trip_state, pickup_market_signals) and assert that
driver_trip_state.current_offer_id ends up where the gate says it
should. The fifth test enforces single-source-of-truth: the gate's
precompute must delegate to _commits, never to a hardcoded floor.

What this file DOES NOT cover (deliberate, per audit + proposal §5):
  - The cache write SQL itself (covered by test_execute_action_id_fix.py).
  - The mock-level gate algebra (covered by test_lost_mode_cold_start_bind.py
    after its 2026-06-01 migration to spatial-local semantics).
  - The dispatch decision logic upstream of _execute_action (covered by
    test_heartbeat_dispatch.py).
"""
from __future__ import annotations

import datetime
import re
from collections import namedtuple
from pathlib import Path
from unittest.mock import patch

import pytest

from dispatch import FirePickupObservation
from driver_heartbeat import _execute_action
from driver_queue import DriverQueue
from where_am_i import MatchOutcome, _commits


# ============================================================================
# Helpers
# ============================================================================

REPO_ROOT = Path(__file__).resolve().parent.parent


class _FakeCluster:
    """Stand-in for diagnostics.cluster — only the centroid fields matter."""
    median_lat = 29.7604
    median_lng = -95.3698


def _seed_driver_trip_state(db_cur, driver_id, current_offer_id=None):
    """Insert a driver_trip_state row so queue.bind's UPDATE has a target."""
    db_cur.execute(
        """
        INSERT INTO app_private.driver_trip_state (driver_id, current_offer_id)
        VALUES (%s, %s)
        """,
        (driver_id, current_offer_id),
    )


def _read_current_offer_id(db_cur, driver_id):
    """SELECT driver_trip_state.current_offer_id for the test driver."""
    db_cur.execute(
        """
        SELECT current_offer_id
        FROM app_private.driver_trip_state
        WHERE driver_id = %s
        """,
        (driver_id,),
    )
    row = db_cur.fetchone()
    return row["current_offer_id"] if row else None


def _exec_fpo_live(db_cur, driver_id, offer_id, *,
                   alive_unpicked_offer_ids,
                   pickup_floor_clearers):
    """Invoke the FPO handler against the real DB.

    acquire_lock runs for real (its INSERT is rolled back by the
    SAVEPOINT in db_cur teardown). conn is the underlying connection
    behind the cursor (db_cur.connection).
    """
    queue = DriverQueue(driver_id)
    action = FirePickupObservation(offer_id=str(offer_id))
    return _execute_action(
        action, db_cur, db_cur.connection, driver_id, queue,
        cluster=_FakeCluster(),
        cumulative_miles=145.5,
        alive_unpicked_offer_ids=alive_unpicked_offer_ids,
        pickup_floor_clearers=pickup_floor_clearers,
    )


def _make_outcome(confidence, *, matched=True, poi_type_match=None):
    """Build a MatchOutcome with only the fields the gate's precompute
    reads. The precompute calls _commits(outcome, verdict); _commits
    reads outcome.matched, outcome.confidence, and outcome.poi_type_match.
    Other MatchOutcome fields are forensic and irrelevant here.
    """
    return MatchOutcome(
        matched=matched,
        confidence=confidence,
        corrected_lat=None,
        corrected_lng=None,
        reason="test_fixture",
        pudo_type="pickup",
        target_address="test fixture address",
        signals=None,
        poi_type_match=poi_type_match,
    )


# Minimal verdict shape used by _commits: it reads only `.passed`.
_Verdict = namedtuple("_Verdict", ["passed"])


# ============================================================================
# Scenario A — the 2026-06-01 morning case (binds)
# ============================================================================

def test_scenarioA_morning_case_solo_floor_clearer_binds(
    db_cur, test_driver_id, seed_decision_log, seed_offer_history,
):
    """Replays the 2026-06-01 H-A regression at the live-PG layer.

    Three offers alive-unpicked (8736, 8737, 8739 analogues). Driver is
    geographically at 8739-analogue's pickup zone — only that offer
    cleared the WAI pickup floor at this heartbeat. Pre-fix the global
    gate withheld the bind because |alive_unpicked| == 3 != 1. New
    gate: local_competing = {clearer} & {three} = {clearer} == {clearer}
    → bind fires; driver_trip_state.current_offer_id lands on clearer.
    """
    _seed_driver_trip_state(db_cur, test_driver_id, current_offer_id=None)

    # Seed three alive-unpicked offers (no actual_pickup_at).
    dl_a = seed_decision_log(test_driver_id)
    dl_b = seed_decision_log(test_driver_id)
    dl_c = seed_decision_log(test_driver_id)
    oh_a = seed_offer_history(dl_a)  # stranded, miles away
    oh_b = seed_offer_history(dl_b)  # stranded, miles away
    oh_c = seed_offer_history(dl_c)  # the floor-clearer at this curb

    executed, err = _exec_fpo_live(
        db_cur, test_driver_id, oh_c,
        alive_unpicked_offer_ids=frozenset({str(oh_a), str(oh_b), str(oh_c)}),
        pickup_floor_clearers=frozenset({str(oh_c)}),
    )

    assert executed is True
    assert err is None
    bound = _read_current_offer_id(db_cur, test_driver_id)
    assert str(bound) == str(oh_c), (
        f"morning case: expected bind to land on solo floor-clearer "
        f"oh_c={oh_c}, got bound={bound}. Pre-fix this was None "
        f"because |alive_unpicked|=3 falsified the global singleton."
    )


# ============================================================================
# Scenario B — shared-curb two floor-clearers (withholds)
# ============================================================================

def test_scenarioB_shared_curb_two_floor_clearers_withhold(
    db_cur, test_driver_id, seed_decision_log, seed_offer_history,
):
    """§XIV.I §5.3 protection preserved: two offers both clearing the
    pickup floor at one cluster → genuine shared-curb ambiguity →
    bind withheld. current_offer_id stays NULL.
    """
    _seed_driver_trip_state(db_cur, test_driver_id, current_offer_id=None)

    dl_a = seed_decision_log(test_driver_id)
    dl_b = seed_decision_log(test_driver_id)
    oh_a = seed_offer_history(dl_a)
    oh_b = seed_offer_history(dl_b)

    executed, err = _exec_fpo_live(
        db_cur, test_driver_id, oh_a,
        alive_unpicked_offer_ids=frozenset({str(oh_a), str(oh_b)}),
        pickup_floor_clearers=frozenset({str(oh_a), str(oh_b)}),
    )

    assert executed is True
    assert err is None
    bound = _read_current_offer_id(db_cur, test_driver_id)
    assert bound is None, (
        f"shared-curb ambiguity: expected current_offer_id to stay NULL, "
        f"got bound={bound}. §XIV.I §5.3 fail-closed protection broken."
    )


# ============================================================================
# Scenario C — leg-retirement via intersection (§1.3)
# ============================================================================

def test_scenarioC_leg_retirement_intersection_unblocks_bind(
    db_cur, test_driver_id, seed_decision_log, seed_offer_history,
):
    """Proposal §1.3: the intersection with alive_unpicked_offer_ids
    excludes already-picked offers, so WAI re-scoring a retired pickup
    leg (the 2026-06-01 §4 anomaly) cannot block a fresh bind on a
    DIFFERENT offer that is the lone alive-unpicked floor-clearer.

    Setup:
      - Offer X: actual_pickup_at already set (picked); WAI keeps
        scoring its pickup leg above floor → in pickup_floor_clearers.
      - Offer Y: alive-unpicked; the lone unpicked floor-clearer at
        this heartbeat → should bind.

    Expected: bind fires for Y. Pre-§1.3 reasoning (without the
    intersect), the gate would see |floor_clearers|=2 and withhold.
    """
    _seed_driver_trip_state(db_cur, test_driver_id, current_offer_id=None)

    now = datetime.datetime.now(datetime.timezone.utc)
    dl_x = seed_decision_log(test_driver_id)
    dl_y = seed_decision_log(test_driver_id)
    # X is already picked (actual_pickup_at set 10 minutes ago).
    oh_x = seed_offer_history(
        dl_x,
        actual_pickup_at=now - datetime.timedelta(minutes=10),
    )
    # Y is alive-unpicked.
    oh_y = seed_offer_history(dl_y)

    executed, err = _exec_fpo_live(
        db_cur, test_driver_id, oh_y,
        # Y alive-unpicked. X NOT alive-unpicked (already picked).
        alive_unpicked_offer_ids=frozenset({str(oh_y)}),
        # WAI keeps re-scoring X's pickup leg above floor too.
        pickup_floor_clearers=frozenset({str(oh_x), str(oh_y)}),
    )

    assert executed is True
    assert err is None
    bound = _read_current_offer_id(db_cur, test_driver_id)
    assert str(bound) == str(oh_y), (
        f"leg-retirement: expected bind to land on alive-unpicked clearer "
        f"oh_y={oh_y}, got bound={bound}. The intersection with "
        f"alive_unpicked_offer_ids did not exclude the retired offer."
    )


# ============================================================================
# Scenario D — normal-mode floor via _commits (no DB needed)
# ============================================================================

def test_scenarioD_normal_mode_floor_via_commits():
    """Pins single-source-of-truth: the precompute delegates to _commits,
    which switches between the 0.40 normal floor and the 0.55 lost floor
    based on verdict.passed. A confidence-0.42 outcome must clear under
    a Normal verdict (passed=True) and must NOT clear under a Lost verdict
    (passed=None). If the precompute hardcoded the lost-mode 0.55 floor,
    the Normal-mode case would silently break.

    No DB needed — the precompute is a pure expression of (outcome,
    verdict). The expression here mirrors the one in driver_heartbeat.py
    (the structural pin below catches drift between the two).
    """
    outcome = _make_outcome(confidence=0.42)
    per_target_outcomes = [("offer_A", "pickup", outcome)]

    # Normal mode: passed=True → 0.40 normal floor → 0.42 clears.
    tad_verdicts_normal = {"offer_A": _Verdict(passed=True)}
    pickup_floor_clearers_normal = frozenset(
        str(offer_id)
        for offer_id, leg, outcome in per_target_outcomes
        if leg == 'pickup'
        and _commits(outcome, tad_verdicts_normal.get(offer_id))
    )
    assert pickup_floor_clearers_normal == frozenset({"offer_A"}), (
        "Normal-mode (verdict.passed=True) at conf 0.42 must clear "
        "0.40 normal floor. If this fails, the precompute is using "
        "the wrong floor (probably the 0.55 lost floor)."
    )

    # Lost mode: passed=None → 0.55 lost floor → 0.42 does NOT clear.
    tad_verdicts_lost = {"offer_A": _Verdict(passed=None)}
    pickup_floor_clearers_lost = frozenset(
        str(offer_id)
        for offer_id, leg, outcome in per_target_outcomes
        if leg == 'pickup'
        and _commits(outcome, tad_verdicts_lost.get(offer_id))
    )
    assert pickup_floor_clearers_lost == frozenset(), (
        "Lost-mode (verdict.passed=None) at conf 0.42 must NOT clear "
        "0.55 lost floor. If this fails, the precompute is using "
        "the wrong floor (probably the 0.40 normal/bridge floor)."
    )


# ============================================================================
# §3.2 — _commits delegation pin (structural + behavioral)
# ============================================================================

def test_gate_delegates_to_commits_structural():
    """Single-source-of-truth structural pin: the precompute in
    driver_heartbeat.py must reference _commits AND must NOT compare
    confidence to a hardcoded literal or to the floor constants by
    name. If a future edit inlines `outcome.confidence >= 0.55` or
    similar, the bind gate stops tracking _commits changes (e.g.,
    POI_ELEVATOR_LIFT additions) and this pin breaks.

    Mirrors the discipline of test_live_offer_predicate_imports.py's
    source-text gates.

    Extraction strategy: balanced-paren scan from the `frozenset(`
    after `pickup_floor_clearers =`. A regex with non-greedy `(.*?)`
    would truncate at the inner `)` of `tad_verdicts.get(offer_id)`
    and miss `_commits` further out.
    """
    text = (REPO_ROOT / "driver_heartbeat.py").read_text()

    anchor = "pickup_floor_clearers = frozenset("
    start = text.find(anchor)
    assert start != -1, (
        "pickup_floor_clearers precompute not found in driver_heartbeat.py — "
        "did someone remove or rename the spatial-local gate's input?"
    )
    # Walk forward from just inside the opening paren, tracking depth
    # until the matching close.
    paren_start = start + len(anchor)
    depth = 1
    pos = paren_start
    while pos < len(text) and depth > 0:
        ch = text[pos]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        pos += 1
    assert depth == 0, (
        "pickup_floor_clearers precompute is malformed — unbalanced "
        "parentheses starting at offset {start}."
    )
    body = text[paren_start:pos - 1]  # exclude the closing paren

    # Positive: must delegate to _commits with both args.
    assert "_commits(" in body, (
        f"pickup_floor_clearers precompute does not call _commits — "
        f"single-source-of-truth broken; the gate is reimplementing "
        f"the floor predicate instead of delegating. Body was:\n{body}"
    )

    # Positive: must filter by pickup leg.
    assert "leg == 'pickup'" in body or "leg == \"pickup\"" in body, (
        f"pickup_floor_clearers precompute does not filter by pickup "
        f"leg — the gate would incorrectly include dropoff-leg scores. "
        f"Body was:\n{body}"
    )

    # Negative: must NOT hardcode the floor constants.
    forbidden_literals = [
        "0.40", "0.55",                        # numeric literals
        "WAI_CONFIDENCE_THRESHOLD",            # 0.40 bridge floor
        "COMMIT_NORMAL_FLOOR",                 # 0.40 normal floor
        "COMMIT_LOST_FLOOR",                   # 0.55 lost floor
    ]
    offenders = [lit for lit in forbidden_literals if lit in body]
    assert not offenders, (
        f"pickup_floor_clearers precompute references forbidden floor "
        f"literal(s) {offenders!r} — must delegate to _commits "
        f"exclusively so future floor changes flow automatically."
    )
