"""
Test: matcher candidate-building loop in driver_heartbeat.py is in the
canonical P15-final shape.

This test locks the §XVIII.C invariants uncovered by the 2026-05-19
forensic and ratified across P15 / P15.1 / P15.2 → consolidated as
P15-final. Four invariants are enforced via source-code inspection:

  (1) Iteration source: the loop iterates
      `diagnostics.per_target_outcomes` (WAI's spatial-scoring results,
      queue-grounded), NOT `diagnostics.tad_verdicts.items()` (the
      pre-P15 broken iterator that produced matcher blindness when TAD
      didn't run).

  (2) tad_passed_any positioning: the flip `tad_passed_any = True`
      appears BEFORE the WAI confidence skip, so the downstream
      unmatched_reason classifier can distinguish 'wai_below_floor'
      from 'tad_failed'.

  (3) Leg alignment: `verdict_for_offer` (raw lookup) and `verdict`
      (leg-aligned form) are BOTH present, and the loop guards against
      the cross-leg authorization leak via the `verdict_for_offer is
      not None and verdict is None` continue.

  (4) Lost-mode preservation: both TAD-related skips are guarded by
      `not lost_mode` per §XVIII.C.2.

Why source inspection: the alternative — a behavioral fixture standing
up the full WAI/TAD/heartbeat pipeline with cross-leg per_target_outcome
seeding — is heavy for what is fundamentally a one-block structural
invariant. The source-inspection sentinel catches the exact regression
class we just fixed in ~50 lines of test instead of ~500 lines of
fixture wiring.
"""

import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).parent.parent
HEARTBEAT_PATH = REPO_ROOT / "driver_heartbeat.py"


@pytest.fixture(scope="module")
def heartbeat_src():
    """Read driver_heartbeat.py once per test module."""
    return HEARTBEAT_PATH.read_text()


def test_candidate_loop_iterates_per_target_outcomes(heartbeat_src):
    """
    Invariant (1): the candidate-building loop iterates
    per_target_outcomes — the queue-grounded matcher source of truth.
    """
    new_pattern = re.compile(
        r"for\s+offer_id,\s+leg,\s+outcome\s+in\s+diagnostics\.per_target_outcomes\s*:"
    )
    new_matches = new_pattern.findall(heartbeat_src)
    assert len(new_matches) == 1, (
        f"Expected exactly 1 candidate-building loop iterating "
        f"diagnostics.per_target_outcomes, found {len(new_matches)}. "
        f"Per §XVIII.C the matcher MUST iterate WAI's scoring results, "
        f"not TAD's verdict dict."
    )

    old_pattern = re.compile(
        r"for\s+offer_id,\s+verdict\s+in\s+diagnostics\.tad_verdicts\.items\(\)\s*:"
    )
    old_matches = old_pattern.findall(heartbeat_src)
    assert len(old_matches) == 0, (
        f"Found {len(old_matches)} instance(s) of the pre-P15 candidate "
        f"loop iterating diagnostics.tad_verdicts.items(). This is the "
        f"2026-05-19 matcher-blindness bug — see P15-final commit and "
        f"docs/CANONICAL_RULES.md §XVIII.C."
    )


def test_tad_passed_any_flip_precedes_wai_confidence_skip(heartbeat_src):
    """
    Invariant (2): tad_passed_any = True is set BEFORE the
    `outcome.confidence < WAI_CONFIDENCE_THRESHOLD` continue. This
    preserves the downstream unmatched_reason classifier's ability to
    distinguish 'wai_below_floor' from 'tad_failed'.
    """
    flip_match = re.search(r"\n\s+tad_passed_any\s*=\s*True\s*\n", heartbeat_src)
    skip_match = re.search(
        r"if\s+outcome\s+is\s+None\s+or\s+outcome\.confidence\s*<\s*WAI_CONFIDENCE_THRESHOLD",
        heartbeat_src,
    )
    assert flip_match is not None, "tad_passed_any = True not found in driver_heartbeat.py"
    assert skip_match is not None, "WAI confidence skip not found in driver_heartbeat.py"
    assert flip_match.start() < skip_match.start(), (
        "tad_passed_any = True must appear BEFORE the WAI confidence skip. "
        "Current ordering would make the unmatched_reason classifier "
        "produce misleading 'tad_failed' labels when TAD actually passed "
        "the distance gate but WAI scored below floor."
    )


def test_leg_alignment_guards_against_cross_leg_leak(heartbeat_src):
    """
    Invariant (3): the loop maintains BOTH a raw `verdict_for_offer`
    lookup and a leg-aligned `verdict` form, and uses both to guard
    against cross-leg authorization leakage.
    """
    raw_lookup = re.search(r"verdict_for_offer\s*=", heartbeat_src)
    assert raw_lookup is not None, (
        "verdict_for_offer lookup not found. P15-final requires the dual-handle "
        "separation between raw lookup (verdict_for_offer) and leg-aligned form "
        "(verdict) to guard against the cross-leg authorization leak."
    )

    leg_align = re.search(
        r"verdict_for_offer\.leg_evaluated\s*==\s*leg",
        heartbeat_src,
    )
    assert leg_align is not None, (
        "Leg alignment check (verdict_for_offer.leg_evaluated == leg) not found. "
        "Required to prevent the inactive leg of an offer from inheriting "
        "the active leg's TAD distance-gate authorization."
    )

    cross_leg_guard = re.search(
        r"if\s+not\s+lost_mode\s+and\s+verdict_for_offer\s+is\s+not\s+None\s+and\s+verdict\s+is\s+None\s*:",
        heartbeat_src,
    )
    assert cross_leg_guard is not None, (
        "Cross-leg-leak guard not found. The loop must `continue` when "
        "TAD is active on a different leg than the one being matched in "
        "this iteration (verdict_for_offer is not None AND verdict is None). "
        "Without this guard, the inactive leg would be authorized whenever "
        "the active leg cleared TAD."
    )


def test_lost_mode_path_unconditional_on_tad(heartbeat_src):
    """
    Invariant (4): §XVIII.C.2 — in lost-mode, candidate inclusion is
    unconditional on TAD. The TAD-failed skip must be guarded by
    `not lost_mode`.
    """
    tad_fail_guard = re.search(
        r"if\s+not\s+lost_mode\s+and\s+verdict\s+is\s+not\s+None\s+and\s+"
        r"verdict\.distance_gate\.get\(['\"]passed['\"]\)\s+is\s+not\s+True",
        heartbeat_src,
    )
    assert tad_fail_guard is not None, (
        "TAD-failed skip must be guarded by `not lost_mode AND verdict is "
        "not None AND verdict.distance_gate.get('passed') is not True`. "
        "Pattern not found — lost-mode abstention may not be preserved."
    )
