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
from pudo_types import States, TargetSpec, Offer


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

    The canonical motivating case for WAI: BMOAR's 200m proximity gate
    failed at this 30-second stop, but the cluster math at T+25s would
    have produced a strong cluster at the actual pickup. WAI v1.0 must
    reproduce a successful diagnosis on this exact data.

    We construct the cluster and topology from the fixture's documented
    actual_stop_location, then run evaluate() and assert against S31's
    [fixture] block expectations.
    """
    fixture = scenario_data["fixture"]
    metadata = heartbeats["metadata"]
    stop = heartbeats["actual_stop_location"]

    # --- Construct the Cluster ---------------------------------------------
    # Fixture documents: 30 seconds, 4 heartbeats observed, GPS coords stable
    # to 7 decimal places (so spread is effectively zero — very tight).
    # We use spread_m=15 (the tight threshold) so cluster_tightness=1.0,
    # matching the documented expected_signals.
    cluster = Cluster(
        n=stop["last_observed_row_index"] - stop["first_observed_row_index"] + 1,
        median_lat=stop["lat"],
        median_lng=stop["lng"],
        spread_m=15.0,
        duration_s=float(stop["duration_s"]),
    )

    # --- Construct the Offer with canonical pickup intersection -----------
    # The fixture's metadata gives us the actual pickup address verbatim:
    # "Joan St & Settemont Rd, Houston, Texas"
    # The recorded pickup coords are where the driver manual-nailed (~1100m
    # off); we use the actual stop coords as proxy for where the geocoded
    # intersection should be. For the replay, we offset slightly to recreate
    # the documented 205m-away condition that motivates the test.
    pickup = TargetSpec(
        lat=stop["lat"] + 0.00184,  # ~205m north — produces proximity≈0.18
        lng=stop["lng"],
        address_class="intersection",
        named_roads=("Settemont Rd", "Joan St"),
    )
    # Dropoff is irrelevant for ENROUTE state but the Offer dataclass
    # requires it — use a placeholder.
    dropoff = TargetSpec(
        lat=29.7000, lng=-95.4000,
        address_class="number_on_street",
        named_roads=("Anywhere St",),
    )
    offer = Offer(
        offer_id=metadata["current_offer_id_text"],
        pickup=pickup,
        dropoff=dropoff,
    )

    # --- Construct the topology (happy-path: Settemont on-wire) -----------
    # Per Gemini Step 5.6 ratification: the happy-path topology is what S31
    # documents as the expected case. Houston-gap topology variants belong
    # in Step 5.7's live-PG smoke tests.
    def fake_cluster_fn(driver_id, cur):
        return cluster

    def fake_pivot_fn(driver_id, cur, anchor_time=None):
        return {
            "on_wire": True,
            "current_road": "Settemont Road",
            "last_named_road": "Settemont Road",
            "pivot_time": None,
            "breadcrumb": ["Settemont Road", "Fondren Road"],
        }

    # --- Run evaluate() ---------------------------------------------------
    # cur=None is OK: ghost-cache SELECT is reachable only when the current
    # PUDO match fails, and S31's expected outcome is a successful current-
    # PUDO match. If the test ever falls through to ghost-cache, that's a
    # regression and the AttributeError will fail the test loudly.
    wai = WhereAmI(cur=None, _cluster_fn=fake_cluster_fn, _pivot_fn=fake_pivot_fn)
    result = wai.evaluate(
        driver_id=metadata["driver_id"],
        current_offer=offer,
        state=States.ENROUTE,
    )

    # --- Assert against S31's [fixture] block -----------------------------
    # The fixture's expectations are fully data-driven (no hardcoded values
    # in test code). All assertion targets come from the TOML.

    # 1. Status: read from fixture (production schema: expected_status)
    expected_status = fixture["expected_status"]
    assert result.status == expected_status, (
        f"S31 vindication failed: expected status={expected_status!r}, got "
        f"status={result.status!r}, reason={result.reason!r}"
    )

    # 2. Pudo type: read from fixture
    expected_pudo_type = fixture["expected_pudo_type"]
    assert result.pudo_type == expected_pudo_type

    # 3. Offer ID: from heartbeat metadata (the canonical 7623)
    assert result.offer_id == metadata["current_offer_id_text"]

    # 4. Confidence: must clear the documented floor
    expected_min = fixture["expected_confidence_min"]
    assert result.confidence >= expected_min, (
        f"S31 confidence {result.confidence:.3f} below expected_confidence_min "
        f"{expected_min}. Reason: {result.reason}"
    )

    # 5. Per-signal floor checks. WAI's public contract surfaces the signal
    # breakdown in the rendered reason string (per Step 4 Q5: signals dict
    # lives on _MatchOutcome internally; WhereAmIResult exposes the rendered
    # form). For each documented minimum, parse the rendered .2f value out
    # of the reason string and assert >= the floor.
    #
    # Reason format from _render_reason:
    #   "intersection conf=0.79 [breadcrumb_match=1.00, on_target_road=1.00, ...]"
    expected_signals_minimum = fixture["expected_signals_minimum"]
    for signal_name, floor_val in expected_signals_minimum.items():
        actual_val = _parse_signal_from_reason(result.reason, signal_name)
        assert actual_val is not None, (
            f"S31 expected signal {signal_name!r} not found in reason: "
            f"{result.reason!r}"
        )
        assert actual_val >= floor_val, (
            f"S31 signal {signal_name}: {actual_val:.2f} below floor "
            f"{floor_val}. Reason: {result.reason}"
        )

    # 6. on_target_road must be True (Settemont matches)
    assert result.on_target_road is True

    # 7. The cluster used in the result is the one we constructed
    assert result.cluster is cluster


def _parse_signal_from_reason(reason: str, signal_name: str) -> float | None:
    """Extract a signal's rendered value from a _render_reason string.

    Reason format: "intersection conf=0.79 [breadcrumb_match=1.00, ...]"

    Returns the float value of `signal_name` (e.g., 1.00 for breadcrumb_match
    in the example) or None if the signal isn't in the reason string.

    Used by forensic replay tests to assert against fixture-documented
    floor values without coupling to the internal _MatchOutcome.signals dict.
    """
    # Find the bracketed signals block
    open_bracket = reason.find("[")
    close_bracket = reason.rfind("]")
    if open_bracket == -1 or close_bracket == -1:
        return None
    signals_block = reason[open_bracket + 1:close_bracket]

    # Walk comma-separated tokens of the form "name=value"
    for token in signals_block.split(","):
        token = token.strip()
        if "=" not in token:
            continue
        name, _, value = token.partition("=")
        if name.strip() == signal_name:
            try:
                return float(value.strip())
            except ValueError:
                return None
    return None
