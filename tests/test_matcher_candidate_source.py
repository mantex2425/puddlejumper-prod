"""
Test: matcher candidate-building loop in driver_heartbeat.py is in the
canonical §XVI.C (amended 2026-05-22) shape.

This test locks the invariants ratified across:

  - P15 / P15.1 / P15.2 → P15-final (2026-05-19): the matcher's source
    of truth is diagnostics.per_target_outcomes (WAI's spatial-scoring
    results, queue-grounded), NOT diagnostics.tad_verdicts.items()
    (the pre-P15 broken iterator that produced matcher blindness when
    TAD didn't run).

  - §XVI.C amendment (2026-05-22): TAD is input to WAI's confidence
    calculation, not a separate gate. An offer with WAI >=
    WAI_CONFIDENCE_THRESHOLD is a candidate regardless of TAD verdict.
    The previous gate-era constructs (`tad_passed_any` flag, dual-
    handle `verdict_for_offer` / `verdict` locals, `if not lost_mode
    and verdict.distance_gate.get('passed') is not True` skip) are
    all removed. This test prevents their reintroduction.

Why source inspection: the alternative — a behavioral fixture standing
up the full WAI/TAD/heartbeat pipeline — is heavy for what is
fundamentally a one-block structural invariant. The source-inspection
sentinel catches the exact regression class in ~50 lines of test
instead of ~500 lines of fixture wiring.

Provenance: §XVI.C amendment ratified 2026-05-22 via Andrew + Claude
+ Gemini paired-programming protocol. TAD demoted from gate to WAI
input; the matcher commit gate is now `WAI >= 0.40 AND arrest_counter
>= 5s` orthogonally to TAD's verdict.
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


# ── Surviving invariant (P15-final, still load-bearing) ─────────────


def test_candidate_loop_iterates_per_target_outcomes(heartbeat_src):
    """
    P15-final invariant: the candidate-building loop iterates
    per_target_outcomes — the queue-grounded matcher source of truth.

    The pre-P15 form iterated diagnostics.tad_verdicts.items() which
    produced matcher blindness when TAD didn't run (the 2026-05-19
    bug). The §XVI.C amendment (2026-05-22) does not affect this
    invariant: WAI's per_target_outcomes is still the iteration
    source.
    """
    new_pattern = re.compile(
        r"for\s+offer_id,\s+leg,\s+outcome\s+in\s+"
        r"diagnostics\.per_target_outcomes\s*:"
    )
    new_matches = new_pattern.findall(heartbeat_src)
    assert len(new_matches) == 1, (
        f"Expected exactly 1 candidate-building loop iterating "
        f"diagnostics.per_target_outcomes, found {len(new_matches)}. "
        f"Per §XVIII.C the matcher MUST iterate WAI's scoring results, "
        f"not TAD's verdict dict."
    )

    old_pattern = re.compile(
        r"for\s+offer_id,\s+verdict\s+in\s+"
        r"diagnostics\.tad_verdicts\.items\(\)\s*:"
    )
    old_matches = old_pattern.findall(heartbeat_src)
    assert len(old_matches) == 0, (
        f"Found {len(old_matches)} instance(s) of the pre-P15 candidate "
        f"loop iterating diagnostics.tad_verdicts.items(). This is the "
        f"2026-05-19 matcher-blindness bug — see P15-final commit and "
        f"docs/CANONICAL_RULES.md §XVIII.C."
    )


# ── §XVI.C amendment regression guards (2026-05-22) ─────────────────


def test_tad_passed_any_does_not_appear(heartbeat_src):
    """
    §XVI.C regression guard: `tad_passed_any` was a gate-era state
    variable used by the `unmatched_reason` classifier to distinguish
    'tad_failed' from 'wai_below_floor'. Both labels have collapsed
    into 'wai_below_floor' (TAD is no longer a gate), so the variable
    is dead. This test prevents reintroduction.
    """
    pattern = re.compile(r"\btad_passed_any\b")
    matches = pattern.findall(heartbeat_src)
    assert len(matches) == 0, (
        f"Found {len(matches)} reference(s) to `tad_passed_any` in "
        f"driver_heartbeat.py. This variable was removed by the §XVI.C "
        f"amendment (2026-05-22). Its reintroduction implies TAD is "
        f"being treated as a separate gate again — reread §XVI.C and "
        f"refuse, or amend §XVI.C explicitly."
    )


def test_tad_distance_gate_branch_not_present(heartbeat_src):
    """
    §XVI.C regression guard: the `if not lost_mode and verdict is not
    None and verdict.distance_gate.get('passed') is not True: continue`
    branch was the candidate-level TAD gate. It was removed by Apply
    Script 1 (2026-05-22). Reintroducing this pattern would re-couple
    candidate inclusion to TAD verdict, violating §XVI.C.
    """
    pattern = re.compile(
        r"if\s+not\s+lost_mode\s+and\s+verdict\s+is\s+not\s+None\s+and\s+"
        r"verdict\.distance_gate"
    )
    matches = pattern.findall(heartbeat_src)
    assert len(matches) == 0, (
        f"Found {len(matches)} instance(s) of the gate-era TAD branch "
        f"`if not lost_mode and verdict.distance_gate.get('passed') is "
        f"not True`. Per §XVI.C (2026-05-22), TAD is input to WAI, not "
        f"a separate gate. This pattern's reintroduction would revert "
        f"the amendment."
    )


def test_tad_failed_unmatched_reason_not_emitted(heartbeat_src):
    """
    §XVI.C regression guard: the `unmatched_reason = 'tad_failed'`
    emission was removed by Apply Script 1 (2026-05-22). The label
    `tad_failed` is deprecated; non-lost-mode misses now emit
    `wai_below_floor` unconditionally.

    Comment references are permitted (deprecation notes). The check
    targets the literal string assignment.
    """
    pattern = re.compile(
        r"unmatched_reason\s*=\s*['\"]tad_failed['\"]"
    )
    matches = pattern.findall(heartbeat_src)
    assert len(matches) == 0, (
        f"Found {len(matches)} emission(s) of `unmatched_reason = "
        f"'tad_failed'`. This label was deprecated by the §XVI.C "
        f"amendment (2026-05-22). Non-lost-mode misses now emit "
        f"`wai_below_floor` unconditionally."
    )


def test_tad_and_wai_match_signal_not_emitted(heartbeat_src):
    """
    §XVI.C regression guard: the `match_signal = 'tad_and_wai'`
    emission was renamed to 'wai_above_floor' by Apply Script 1
    (2026-05-22). The label `tad_and_wai` is deprecated; non-lost
    single-match fires now emit `wai_above_floor`.

    Comment references are permitted (deprecation notes).
    """
    pattern = re.compile(
        r"match_signal\s*=\s*['\"]tad_and_wai['\"]"
    )
    matches = pattern.findall(heartbeat_src)
    assert len(matches) == 0, (
        f"Found {len(matches)} emission(s) of `match_signal = "
        f"'tad_and_wai'`. This label was renamed to `wai_above_floor` "
        f"by the §XVI.C amendment (2026-05-22)."
    )
