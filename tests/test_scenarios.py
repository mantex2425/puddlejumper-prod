"""
Phase D Step 5.6 — TOML scenario test harness + S31 forensic replay.

Integration tests parameterized over scenarios/*.toml. Two layers:

1. **Inventory tests** — every TOML loads as valid TOML, has the required
   top-level keys (id, description, status). Parameterized over all
   scenarios; one test invocation per file.

2. **Forensic replay tests** — scenarios with a [fixture] block run their
   heartbeat fixture through WhereAmI.evaluate() and assert documented
   expected outputs. Parameterized over only the fixture-bearing scenarios.

Per Step 5.6 architectural locks (Gemini Step 5.6 ratification):
- We do NOT replay raw heartbeats through pivot_context / detect_cluster.
  WAI's contract is "given a cluster and topology, produce a result."
  Cluster/topology derivation is tested in their own modules and in the
  Step 5.7 functional smoke against live PG.
- We construct cluster + topology from the fixture's documented
  `actual_stop_location` block, then inject via WhereAmI(_cluster_fn=...,
  _pivot_fn=...) — same dependency-injection seam unit tests use.
- S31 happy-path topology only (Settemont on-wire). "Houston street gaps"
  variants are deferred to Step 5.7.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from where_am_i import WhereAmI, STRONG_MATCH_CONFIDENCE
from cluster_detection import Cluster
from pudo_types import TargetSpec, Offer
from datetime import datetime, timezone


# Resolve scenarios/ and tests/fixtures/ relative to this file's location.
# tests/test_scenarios.py -> ../scenarios/ and ./fixtures/
SCENARIO_DIR = Path(__file__).parent.parent / "scenarios"
FIXTURE_DIR = Path(__file__).parent / "fixtures"


def _load_all_scenarios() -> list[tuple[str, Path, dict]]:
    """Discover all TOML scenarios. Returns (id, path, parsed_dict) triples,
    sorted by file path for deterministic test ordering.
    """
    out = []
    for path in sorted(SCENARIO_DIR.glob("S*.toml")):
        with open(path, "rb") as f:
            data = tomllib.load(f)
        out.append((data["id"], path, data))
    return out


def _replayable_scenarios() -> list[tuple[str, dict]]:
    """Scenarios with [fixture] blocks — those that can be forensically
    replayed against WhereAmI.evaluate().
    """
    return [
        (sid, data)
        for sid, _path, data in _load_all_scenarios()
        if "fixture" in data
    ]


# Cache the loaded scenarios at module import time so parametrize() can
# enumerate them. Anything that needs to vary at test time should re-load.
_ALL_SCENARIOS = _load_all_scenarios()
_REPLAYABLE = _replayable_scenarios()


# ============================================================================
# Inventory tests — parameterized over all TOML scenarios
# ============================================================================


@pytest.mark.parametrize(
    "scenario_id,scenario_path,scenario_data",
    _ALL_SCENARIOS,
    ids=[sid for sid, _, _ in _ALL_SCENARIOS],
)
class TestScenarioInventory:
    """Every TOML in scenarios/ must load cleanly and have minimum
    required structure. One test invocation per scenario.
    """

    def test_required_top_level_keys(self, scenario_id, scenario_path, scenario_data):
        # The contract for any scenario in the evidence locker
        for required_key in ("id", "description", "status"):
            assert required_key in scenario_data, (
                f"{scenario_path.name}: missing required key {required_key!r}"
            )

    def test_id_matches_filename_prefix(self, scenario_id, scenario_path, scenario_data):
        # The scenario's id field should match the filename's S-prefix.
        # File: S31.toml          -> id = "S31"
        # File: S32_foo_bar.toml  -> id = "S32"
        # Extract the S-prefix from the filename
        stem = scenario_path.stem  # "S31" or "S32_implicit_stacked_cancel"
        s_prefix = stem.split("_")[0]
        assert scenario_data["id"] == s_prefix, (
            f"{scenario_path.name}: id={scenario_data['id']!r} doesn't match "
            f"filename prefix {s_prefix!r}"
        )


# ============================================================================
# Forensic replay tests — parameterized over fixture-bearing scenarios only
# ============================================================================


# Skip-marker for scenarios whose replay logic isn't implemented yet
_REPLAY_HANDLERS_IMPLEMENTED = {"S31"}


@pytest.mark.parametrize(
    "scenario_id,scenario_data",
    _REPLAYABLE,
    ids=[sid for sid, _ in _REPLAYABLE],
)
def test_scenario_forensic_replay(scenario_id, scenario_data):
    """Run a TOML scenario's heartbeat fixture through WhereAmI.evaluate()
    and assert the documented expected outputs.

    Each replayable scenario gets its own _replay_<id>() handler. New
    scenarios with fixtures need a new handler added below the dispatch.
    """
    if scenario_id not in _REPLAY_HANDLERS_IMPLEMENTED:
        pytest.skip(
            f"{scenario_id}: replay handler not yet implemented "
            f"(scenario has [fixture] block but no _replay_{scenario_id} function)"
        )

    fixture = scenario_data["fixture"]

    # Resolve fixture file path relative to repo root
    repo_root = Path(__file__).parent.parent
    hb_path = repo_root / fixture["heartbeats"]
    with open(hb_path) as f:
        heartbeats = json.load(f)

    # Dispatch to the per-scenario replay handler
    if scenario_id == "S31":
        _replay_S31(scenario_data, heartbeats)


# ============================================================================
# Per-scenario replay handlers
# ============================================================================


def _replay_S31(scenario_data: dict, heartbeats: dict) -> None:
    """Forum Park 7623 forensic replay.

    Cut B2: assert via evaluate_with_diagnostics(). Reads structured signals
    from DiagnosticContext.per_target_outcomes instead of parsing the
    rendered reason string.

    The canonical motivating case for WAI: BMOAR's 200m proximity gate
    failed at this 30-second stop, but the cluster math at T+25s would
    have produced a strong cluster at the actual pickup. WAI must
    reproduce a successful diagnosis on this exact data.
    """
    fixture = scenario_data["fixture"]
    metadata = heartbeats["metadata"]
    stop = heartbeats["actual_stop_location"]

    # --- Construct the Cluster ---------------------------------------------
    cluster = Cluster(
        n=stop["last_observed_row_index"] - stop["first_observed_row_index"] + 1,
        median_lat=stop["lat"],
        median_lng=stop["lng"],
        spread_m=15.0,
        duration_s=float(stop["duration_s"]),
        latest=datetime(2026, 4, 23, 20, 52, 16, tzinfo=timezone.utc),
    )

    # --- Construct the Offer with canonical pickup intersection -----------
    pickup = TargetSpec(
        lat=stop["lat"] + 0.00184,  # ~205m north - produces proximity~0.18
        lng=stop["lng"],
        address_class="intersection",
        named_roads=("Settemont Rd", "Joan St"),
    )
    dropoff = TargetSpec(
        lat=29.7000, lng=-95.4000,
        address_class="number_on_street",
        named_roads=("Anywhere St",),
    )
    offer = Offer(
        offer_id=metadata["current_offer_id_text"],
        accepted_at=datetime(2026, 4, 23, 20, 45, 0, tzinfo=timezone.utc),
        pickup=pickup,
        dropoff=dropoff,
    )

    # --- Construct the topology (happy-path: Settemont on-wire) -----------
    def fake_cluster_fn(driver_id, cur):
        return cluster

    _now = datetime.now(timezone.utc)
    _breadcrumb_segments = [
        {"road_name": name, "entered_at": _now, "exited_at": _now}
        for name in ("Settemont Road", "Fondren Road")
    ]

    def fake_pivot_fn(driver_id, cur, anchor_time=None):
        return {
            "on_wire": True,
            "current_road": "Settemont Road",
            "last_named_road": "Settemont Road",
            "pivot_time": None,
            "breadcrumb": _breadcrumb_segments,
        }

    # --- Run evaluate_with_diagnostics ------------------------------------
    wai = WhereAmI(cur=None, _cluster_fn=fake_cluster_fn, _pivot_fn=fake_pivot_fn)
    matches, diagnostics = wai.evaluate_with_diagnostics(
        driver_id=metadata["driver_id"],
        queue=[offer],
    )

    # --- Find the pickup match for this offer -----------------------------
    # The Map-Reduce contract returns the highest-confidence candidate(s)
    # above threshold. For S31, only the pickup target is geographically
    # close; dropoff is far. So a single-element matches list with
    # location_type=="pickup" is the expected outcome.
    pickup_match = next(
        (m for m in matches
         if m.offer_id == offer.offer_id and m.location_type == "pickup"),
        None,
    )
    assert pickup_match is not None, (
        f"S31 vindication failed: no pickup match in matches={matches!r}. "
        f"per_target_outcomes={diagnostics.per_target_outcomes!r}"
    )

    # --- Find the corresponding outcome for signal-floor checks -----------
    # DiagnosticContext.per_target_outcomes is the structured analog of the
    # old WhereAmIResult.reason string. Each entry is
    # (offer_id, location_type, MatchOutcome).
    pickup_outcome = next(
        (outcome for (oid, ltype, outcome) in diagnostics.per_target_outcomes
         if oid == offer.offer_id and ltype == "pickup"),
        None,
    )
    assert pickup_outcome is not None, (
        f"S31: pickup match present but pickup outcome missing from "
        f"per_target_outcomes. matches={matches!r} diagnostics={diagnostics!r}"
    )

    # --- Assert against fixture-documented floors -------------------------

    # 1. Confidence floor (TOML: expected_confidence_min)
    expected_min = fixture["expected_confidence_min"]
    assert pickup_match.confidence >= expected_min, (
        f"S31 confidence {pickup_match.confidence:.3f} below "
        f"expected_confidence_min {expected_min}. "
        f"reason={pickup_outcome.reason!r}"
    )

    # 2. Per-signal floors (TOML: expected_signals_minimum). Read directly
    # from the structured signals dict on MatchOutcome - no string parsing.
    expected_signals_minimum = fixture["expected_signals_minimum"]
    for signal_name, floor_val in expected_signals_minimum.items():
        actual_val = pickup_outcome.signals.get(signal_name) if pickup_outcome.signals else None
        assert actual_val is not None, (
            f"S31 expected signal {signal_name!r} not found in signals dict: "
            f"{pickup_outcome.signals!r}"
        )
        assert actual_val >= floor_val, (
            f"S31 signal {signal_name}: {actual_val:.2f} below floor "
            f"{floor_val}. signals={pickup_outcome.signals!r}"
        )

    # 3. on_target_road must be at full strength (Settemont matches)
    on_target_road = (
        pickup_outcome.signals.get("on_target_road", 0.0)
        if pickup_outcome.signals else 0.0
    )
    assert on_target_road >= 1.0, (
        f"S31 on_target_road={on_target_road} below 1.0; "
        f"signals={pickup_outcome.signals!r}"
    )

    # 4. The cluster used in the result is the one we constructed
    assert diagnostics.cluster is cluster

    # NOTE: TOML fixture fields expected_status and expected_pudo_type have
    # no direct analog in the naked-list contract. expected_pudo_type maps
    # to pickup_match.location_type ("pickup") which we already asserted via
    # the next() filter. expected_status was a WhereAmIResult string and is
    # left unused by this rewrite. Future TOML cleanup is out of Cut B2 scope.


