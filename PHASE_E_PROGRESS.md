# Phase E Progress Brief

**For:** A fresh Claude conversation resuming Phase E at Step 5.
**Author:** Phase E Step 4 closing session (commit b46d8ef).
**Date authored:** 2026-04-26.
**Replaces:** prior version at commit 3fa9626 (post Step 2.6, now stale).

---

## Read these in order before doing anything else

1. **`PHASE_E_KICKOFF.md`** at the repo root — original Phase E entry brief.
   Defines scope, non-goals, paired-programming protocol. Still authoritative
   for architectural ground truth.

2. **`PHASE_D_RETRO.md`** at the repo root — implementation record from Phase D.
   Lessons L-2 through L-8 are protocol guardrails Phase E continues to follow.

3. **`WHERE_AM_I_PROPOSAL_v2.md`** at the repo root — design RFC. v2.5
   amendment header explains the Phase D shipped state.

4. **This file (`PHASE_E_PROGRESS.md`)** — captures Phase E's current
   state at end of Step 4. Use this as the entry point for Step 5
   planning.

5. **The last 12 commits** — `git log --oneline 60ed517^..HEAD` — the
   entire Phase E arc through Step 4.

6. **`pudo_planner.py`** — read it. It's 1041 lines. The Section
   structure (A/B/C/D) is the architectural shape Step 5 must test.

---

## Current state of the world
HEAD:                b46d8ef (Phase E Step 4 — Section B: STACKED detection)
Branch:              patch-00566a-unified-refinement (clean working tree, in lockstep with origin)
Tests pytest:        182/182 passing
Tests integration:   22/61 (39 known pre-existing failures — see Step 6 plan below)
Live-PG smoke:       4/4 from Phase D Step 5.7.2 (not re-run in Phase E; no DB-coupled changes yet)
pudo_planner.py:     1041 lines, 4 sections (A/B/C/D), 11 builders, smoke-tested inline

Phase E Step 1 (architectural design), Step 2 (PLAN/EXECUTE contract +
five-pillar Evidence Locker), Step 3 (planner module: skeleton +
temporal helpers + decision builders + dispatch logic), and Step 4
(Section B / S32 / S35 contradiction detection) are all shipped.

**Step 5 (planner unit test suite) is the next concrete work.**

---

## Phase E commit lineage (cleanly stacked on Phase D's 60ed517)

| SHA       | Step  | Description                                            |
|-----------|-------|--------------------------------------------------------|
| 9394449   | 2     | Add DriverStateSnapshot + PlannerDecision dataclasses  |
| 8aeb076   | 2.3   | scenarios/S33_calhoun_missed_pickup.toml               |
| 5277951   | 2.4   | Add fire_stacked_revert to PlannerDecision.action      |
| ed0bd0d   | 2.5   | scenarios/S34 + S35 bundle                             |
| 3fa9626   | 2.6   | PHASE_E_PROGRESS.md (this file's predecessor)          |
| 64dee6a   | 3.1   | pudo_planner.py module skeleton (195 lines)            |
| 048498e   | 3.2   | Section A: temporal pattern detection helpers (+236)   |
| 0afb1df   | 3.3   | Section C: decision builders (+312)                    |
| bfbea37   | 3.4   | Section D: consume() dispatch logic (+215 net)         |
| b46d8ef   | 4     | Section B: STACKED contradiction detection (+83 net)   |

Final pudo_planner.py: 1041 lines.

---

## The Five Pillars of Failure — current handling status

The S31-S35 series captures the canonical failure-mode taxonomy. Status
after Step 4:

| ID  | Pillar     | Status      | Mechanism                                          |
|-----|------------|-------------|----------------------------------------------------|
| S31 | Geometric  | HANDLED     | WAI confidence model (since Phase D)               |
| S32 | Structural | HANDLED     | Step 4 Section B: fire_stacked_swap                |
| S33 | Temporal   | PARTIAL     | fire_retroactive handles long-stop-then-departure; |
|     |            |             | true reconciliation needs Step 5/6 cross-checks    |
| S34 | Collapse   | PARTIAL     | Single-heartbeat fires correctly; double-tap is    |
|     |            |             | Phase F backlog B-13                               |
| S35 | Inverse    | HANDLED     | Step 4 Section B: fire_stacked_revert              |

S33/S34/S35 do NOT have heartbeat fixtures (deferred until reproducible
production capture). They validate via TOML schema parse only at the
test layer. S31 has a forensic replay fixture at
`tests/fixtures/7623_heartbeats.json`.

---

## Contract — PlannerDecision.action (11 values, all type-checked)
noop                       arm_candidate              cancel_candidate
fire_pickup                fire_dropoff               fire_retroactive
fire_stacked_swap          fire_stacked_revert        cache_ghost
reconcile_missed_pickup    reconcile_missed_dropoff

All 11 are type-checked into the `Literal[...]` declaration of
`PlannerDecision.action` in pudo_types.py. Step 5 will assert this
introspection in the test suite.

---

## Architectural rulings locked during Phase E

**R1 (state-correction over coordinate-synthesis):** When a dropoff
fires successfully but the pickup was never confirmed, PLAN emits
`reconcile_missed_pickup` with a `reconciliation_payload`. EXECUTE
updates offer_history truthfully (`pickup_missed=true`, leave
`actual_pickup_lat/lng` NULL). PLAN does NOT synthesize a pickup at
the cluster median. Audit-trail integrity wins. Enforced at the
contract level — `_build_reconcile_missed_pickup` explicitly passes
`corrected_lat=None, corrected_lng=None`.

**R2 (database-blind PLAN):** No DB writes anywhere in `pudo_planner.py`.
Even ghost-cache INSERTs go through `cache_ghost` action with a
`ghost_insert_payload` that EXECUTE acts on. No I/O in `consume()` —
not even logging. Phase F's heartbeat loop wraps `consume()` in its
existing structured-logging pipeline.

**R3 REVISED (identity over proximity):** Original R3 in Step 2
proposed coordinate-only proximity check for STACKED disambiguation.
Step 4 implementation revealed this was a mis-derivation. WAI's
confidence model already incorporates proximity, breadcrumb_match,
cluster_tightness, on_target_road into ONE verdict and reports the
winning offer_id. PLAN interprets the identity, not re-derives the
distance. The four-case identity dispatch in Section B is the canonical
implementation.

**R4 (TOML <-> Python verdict mapping):** TOML uppercase verdicts map
to Python lowercase actions. Established at S33 ratification. Example:
`RECONCILE_MISSED_PICKUP` <-> `reconcile_missed_pickup`.

---

## Phase E Step 5 — what owns it

Step 5 ships `tests/test_pudo_planner.py` as a proper pytest test file.
Replaces the inline smoke tests we ran during Steps 3.1-3.4 and Step 4
with parameterized, named test cases that survive into Phase F.

**Required test coverage** (from inline smoke tests, formalized):
Section A — temporal pattern detection
test_make_observation
test_is_stable_match (5 cases)
test_is_brief_disappearance (3 cases)
test_is_long_stop_then_departure (3 cases)
test_advance_armed_state_case_1_through_6 (parameterized)
Section C — decision builders
test_build_noop
test_build_arm_candidate
test_build_cancel_candidate
test_build_fire_pickup
test_build_fire_dropoff
test_build_fire_retroactive
test_build_fire_stacked_swap
test_build_fire_stacked_revert
test_build_cache_ghost
test_build_reconcile_missed_pickup (R1 enforcement: no coord synthesis)
test_build_reconcile_missed_dropoff
test_all_builders_return_frozen_planner_decision
Section D — consume() dispatch
test_dispatch_cold_start_not_at_pudo_returns_noop
test_dispatch_cold_start_at_current_pudo_arms_candidate
test_dispatch_three_stable_hits_pickup_fires_pickup
test_dispatch_three_stable_hits_dropoff_fires_dropoff
test_dispatch_at_unknown_pudo_caches_ghost
test_dispatch_malformed_at_unknown_pudo_returns_noop
test_dispatch_long_stop_then_departure_fires_retroactive
test_dispatch_brief_miss_keeps_armed
test_dispatch_different_offer_rearms_fresh
test_dispatch_returns_frozen_decision
Section B — STACKED contradiction detection
test_section_b_primary_dropoff_falls_through_to_arm
test_section_b_secondary_pickup_fires_stacked_swap (S32)
test_section_b_primary_pickup_fires_stacked_revert (S35)
test_section_b_secondary_dropoff_falls_through_b14
test_section_b_guard_not_stacked
test_section_b_guard_at_unknown_pudo_caches_ghost
test_section_b_guard_missing_primary_offer_id
Contract introspection
test_planner_decision_action_literal_has_11_values
test_all_action_values_present

Estimated count: ~40-50 test functions. Some parameterized.

**Implementation pattern:** mirror tests/test_where_am_i.py from Phase D.
Use injected `_now_fn` and `_state_store` for determinism. No DB
fixtures needed — planner is database-blind.

**Step 5 does NOT alter `pudo_planner.py`.** It ONLY adds a test file.
This is important: any change to `pudo_planner.py` must be its own step
with its own justification.

---

## Phase E Step 6 — Integration Bridge (NEW commitment from Step 4)

The 61-test legacy integration suite (`tests/test_integration.sh`)
cannot be naively passed by the new architecture. Step 6 commits to:

1. **Author `tests/test_planner_integration.py`** (or shell equivalent)
   that synthesizes heartbeat sequences for:
     - The Five Pillars (S31-S35)
     - WAI/PLAN-relevant subset of T01-T60 (GPS jitter at pickup, S11
       override, circular-trip odometer gate, etc.)
     - New T70-T79 numbering for STACKED-via-WAI behaviors

2. **Triage every currently-failing T01-T60 test** in PHASE_E_PROGRESS.md
   end-of-phase update:
     - REPLACE: needs new T70-T79 test exercising new architecture
     - RETIRE: asserts a legacy mechanism (Phase G deprecation)
     - PRESERVE: tests plumbing that must continue to work

3. **Empirically validate the odometer-gate question** (T48/T49). If
   WAI's `breadcrumb_match` signal correctly fails confidence on
   premature dropoff at pickup pin, no contract amendment needed. If
   it doesn't, justify adding `cumulative_miles` to `DriverStateSnapshot`
   with empirical evidence.

Phase E ends after Step 6, with the planner architecturally complete
and integration-validated against synthetic heartbeats.

---

## Phase E Step 7 — Live-PG smoke (mirrors Phase D Step 5.7.2)

Run `pudo_planner` against real PostgreSQL via the same pattern as
`scripts/wai_smoke.py`. PLAN itself is database-blind, but Step 7
exercises the full assembly: snapshot construction (mocked from
production-shape rows) -> WAI evaluate -> PLAN consume -> verify
PlannerDecision shape. Catches schema-shape assumptions before Phase F
wires production traffic.

---

## Phase F (next phase, separate session strongly recommended)

Heartbeat-loop integration. Modifies `driver_heartbeat.py` (production-
critical). Adds `address` field to `TargetSpec` so
`WhereAmIResult.target_address` populates. Adds `secondary_pickup` to
`Offer` dataclass. Builds shadow-mode logging surface. Wires
`WAI_PLANNER_ENABLED_DRIVERS` flag. **Audits atomic-swap snapshot
assembly** (the "wrong pickup" failure class Andrew flagged at Step 2.5;
the contract for that audit is now firm because Section B's identity-
based detection only works if Phase F builds the snapshot correctly).

---

## Phase G — BMOAR Path A/B deprecation

Once shadow-mode telemetry validates WAI/PLAN matches or exceeds BMOAR's
fire rate without false positives. Cannot ship before sufficient
production data exists.

---

## Backlog accumulated through Step 4

| ID   | Item                                                                  |
|------|----------------------------------------------------------------------|
| B-11 | (carried from Phase D) — Implicit STACKED cancel CLOSED at Step 4    |
| B-12 | Calhoun-class missed-pickup reconciliation logic — Step 5/6          |
| B-13 | Hot-swap collapse: Phase F double-tap WAI after fire                 |
| B-14 | Sequence Violation Detector — secondary dropoff in STACKED before    |
|      | secondary pickup; forensic alert (currently falls through silently)  |
| B-15 | Phase F: add `address` field to TargetSpec; populate target_address  |
|      | in WhereAmIResult (currently always None)                            |
| B-16 | Phase F: add `secondary_pickup` to Offer dataclass                   |
| B-17 | Phase F: audit atomic-swap snapshot assembly                         |
| B-18 | Phase G: T50/T60 legacy WatchdogB tests retire when BMOAR deprecates |
| B-19 | Step 6: triage tests/test_integration.sh per REPLACE/RETIRE/PRESERVE |
| B-20 | Step 6: empirical odometer-gate decision (T48/T49) — add             |
|      | `cumulative_miles` to DriverStateSnapshot only with evidence         |
| B-21 | Architectural observation (Step 3.4 retro): cancel_candidate may be  |
|      | unreachable through current dispatch — Step 4's Section B may        |
|      | introduce paths that reach it. Re-examine in Step 6.                 |

---

## Protocol — paired-programming, unchanged

The cycle that has worked through 11 Phase E commits:

1. Claude proposes (one step at a time, with design discussion when
   warranted)
2. Andrew runs the proposal past Gemini
3. Gemini ratifies or pushes back
4. If ratified, Claude implements with verification gates
5. Andrew runs the gates, pastes output
6. Commit + push immediately, never bundle multiple steps

**Verification gates per step:**
- pytest count preserved or increased (current floor: 182)
- Integration baseline preserved (current floor: 22/61)
- Live-PG smoke when DB-coupled (per L-8) — none in Phase E so far
- L-2 paranoia: `git status` before staging, after staging, after commit
- L-3: anchor-based patch scripts with sha-locked pre-conditions for
       any file >100 lines
- L-5: trailing-newline guard
- L-6: read production artifacts before authoring assertions
- L-7: cross-check architectural rulings against production conventions

**Heredoc transit observation (new this session):** large heredocs
(>200 lines of embedded content) consistently produce a terminal-render
artifact where content fragments visibly bleed into the prompt. The
file itself is always clean — verified by SYNTAX OK + head/tail
boundary check after the prompt returns. For Phase F we should consider
base64-encoding patch script transfers to bypass the rendering issue
entirely.

---

## Concrete first-message-of-new-chat starter

> I'm resuming Phase E at Step 5. Step 4 (Section B / S32 / S35
> contradiction detection) shipped at commit b46d8ef. The planner is
> architecturally complete for the Five Pillars; Step 5 ships the
> proper pytest test suite for `pudo_planner.py`.
>
> Please read in order:
>   1. PHASE_E_PROGRESS.md (this file — captures everything you need)
>   2. PHASE_E_KICKOFF.md (architectural ground truth)
>   3. pudo_planner.py (1041 lines — the artifact you'll be testing)
>   4. tests/test_where_am_i.py (Phase D's test pattern to mirror)
>
> Then propose Phase E Step 5 — `tests/test_pudo_planner.py` — with
> the same paired-programming protocol that worked through Step 4.
> The test coverage list in PHASE_E_PROGRESS.md is a starting checklist;
> review and expand. I'll review your proposal with Gemini before locking.
>
> Constraints: Step 5 ONLY adds the test file. No changes to
> pudo_planner.py. The integration bridge (Step 6) and any further
> contract amendments are separate steps.
