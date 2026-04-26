# Phase E Progress Brief

**For:** A fresh Claude conversation resuming Phase E mid-construction.
**Author:** Phase E Step 2.x closing (commit ed0bd0d, Step 2.5).
**Date authored:** 2026-04-26.

---

## Read first

1. **`PHASE_E_KICKOFF.md`** — original Phase E entry brief. Defines scope,
   non-goals, paired-programming protocol. Still authoritative for the
   architectural ground truth.
2. **`PHASE_D_RETRO.md`** — implementation record from Phase D. Lessons
   L-2 through L-8 are protocol guardrails Phase E continues to follow.
3. **`WHERE_AM_I_PROPOSAL_v2.md`** — the design RFC. v2.5 amendment header
   explains the Phase D shipped state.
4. **This file** — captures Phase E's mid-flight state so the next session
   can resume at Step 3 (planner skeleton).
5. **The last 5 commits** — `git log --oneline 60ed517^..HEAD` — the entire
   Phase E Step 2 arc.

---

## Current state of the world
HEAD:                ed0bd0d (Phase E Step 2.5 — S34 + S35 bundle)
Branch:              patch-00566a-unified-refinement (clean working tree, in lockstep with origin)
Tests pytest:        182/182 passing (Phase D's 176 + 6 from S33/S34/S35 parameterization)
Tests integration:   22/61 passing (39 known pre-existing failures, see below)
Live-PG smoke:       4/4 from Phase D Step 5.7.2 (not re-run in Phase E; no DB-coupled changes)

Phase E Step 2 (the PLAN/EXECUTE contract + scenario locker) is shipped.
Phase E Step 3 (pudo_planner.py skeleton + temporal logic) is the next
concrete work.

---

## Phase E Step 2 commits (cleanly stacked on Phase D's 60ed517)

| SHA       | Step | Description                                            |
|-----------|------|--------------------------------------------------------|
| 9394449   | 2    | Add DriverStateSnapshot + PlannerDecision dataclasses  |
| 8aeb076   | 2.3  | scenarios/S33_calhoun_missed_pickup.toml               |
| 5277951   | 2.4  | Add fire_stacked_revert to PlannerDecision.action      |
| ed0bd0d   | 2.5  | scenarios/S34_hot_swap.toml + S35_stacked_uber_reaward.toml |

---

## The Five Pillars of Failure (Evidence Locker, complete)

The S31-S35 series captures the canonical failure-mode taxonomy ratified
by Gemini during Step 2. These are the cases the Phase E planner must
handle correctly.

| ID  | Pillar     | Failure mode                                                  |
|-----|------------|---------------------------------------------------------------|
| S31 | Geometric  | BMOAR distance gate too tight (Forum Park 7623, 205m)         |
| S32 | Structural | WAI<->state machine contradiction at a moment (implicit cancel) |
| S33 | Temporal   | Post-hoc reconciliation of unobserved PUDO (Calhoun)          |
| S34 | Collapse   | Two PUDO events collapse into one cluster (hot swap)          |
| S35 | Inverse    | Uber-side re-award (structural inverse of S32)                |

S33/S34/S35 do NOT have heartbeat fixtures (deferred until reproducible
production capture); they validate via TOML schema parse only. S31 is the
only fixture-bearing scenario currently — its forensic replay is in
tests/fixtures/7623_heartbeats.json.

---

## Contract — PlannerDecision.action (11 values, all type-checked into Literal)
noop                       arm_candidate              cancel_candidate
fire_pickup                fire_dropoff               fire_retroactive
fire_stacked_swap          fire_stacked_revert        cache_ghost
reconcile_missed_pickup    reconcile_missed_dropoff

Architectural rulings locked during Step 2 (Gemini paired-programming
consensus 2026-04-26):

**R1 (state-correction over coordinate-synthesis):** When a dropoff fires
successfully but the pickup was never confirmed, PLAN corrects the state
machine and labels honestly (`pickup_missed=true`, leave actual_pickup_lat/lng
NULL). It does NOT retroactively synthesize a pickup at the cluster median.
Audit-trail integrity wins over apparent system perfection.

**R2 (database-blind PLAN):** PLAN produces PlannerDecision objects.
EXECUTE acts on them. PLAN never writes to the DB — even for the ghost
cache, EXECUTE owns the suspected_pudos INSERT via ghost_insert_payload.
This preserves the canonical EXECUTE-only-write rule.

**R3 (coordinate-only proximity for STACKED disambiguation):** Both S32
(fire_stacked_swap) and S35 (fire_stacked_revert) detect via the same
primitive — compare WAI corrected_lat/lng against snapshot's pickup/dropoff
coords; whichever is closest determines the resolution. Address strings
are not used (TargetSpec.address is None until Phase F).

**R4 (Calhoun naming convention):** TOML uppercase verdicts map to
Python lowercase actions (e.g. RECONCILE_MISSED_PICKUP <->
reconcile_missed_pickup). Established with S33 ratification.

---

## Phase E Step 3 — what owns it

Step 3 is `pudo_planner.py` itself: the module skeleton, temporal logic,
and the per-action decision builders.

Module structure (ratified Step 1, Gemini consensus 2026-04-26):
pudo_planner.py (~600 lines estimated)
├── Module constants (N_HEARTBEATS_TO_FIRE=3, CANDIDATE_STALE_S=15, ...)
├── _DriverTemporalState dataclass (PRIVATE to this module per Gemini)
├── Section A — Temporal pattern detection
│   ├── _is_stable_match
│   ├── _is_brief_disappearance
│   ├── _is_long_stop_then_departure
│   └── _advance_armed_state
├── Section B — B-11 STACKED contradiction detection
│   └── _detect_implicit_stacked_cancel (handles BOTH S32 and S35)
├── Section C — Decision builders (one small builder per action)
├── Section D — PudoPlanner orchestrator
│   ├── init(cur, *, _now_fn=None, _state_store=None)
│   └── consume(driver_id, wai_result, *, driver_state_snapshot) -> PlannerDecision
└── Section E — _state_store injection seam (in-memory dict for v1.0)

Public surface:

```python
class PudoPlanner:
    def __init__(self, cur, *, _now_fn=None, _state_store=None): ...
    def consume(
        self,
        driver_id: str,
        wai_result: WhereAmIResult,
        *,
        driver_state_snapshot: DriverStateSnapshot,
    ) -> PlannerDecision: ...
```

`consume()` is pure logic. No DB writes, no DB reads (all context is in
the snapshot). Phase F's heartbeat loop assembles the snapshot, calls
WAI, then calls planner.consume().

---

## Phase E Step 4-7 — sketched, not designed

Step 4: Implement the action-specific logic (fire_stacked_swap detection,
        fire_stacked_revert detection, the post-fire re-evaluation that
        S34 hot-swap requires, the reconciliation triggers for S33).
Step 5: Unit tests in tests/test_pudo_planner.py.
Step 6: Integration tests for the planner path (T70-T79 numbering, separate
        from the legacy T50/T60 watchdog tests which Phase G will deprecate).
Step 7: Live-PG smoke (mirroring Phase D Step 5.7.2 pattern).

Phases F (driver_heartbeat.py integration + shadow-mode wiring + atomic-swap
snapshot audit) and G (BMOAR Path A/B deprecation) follow.

---

## Backlog accumulated during Phase E Step 2

| ID   | Item                                                                  |
|------|----------------------------------------------------------------------|
| B-11 | (carried from Phase D) Implicit STACKED cancel — S32 detection logic |
| B-12 | Calhoun-class missed-pickup reconciliation — S33 detection logic     |
| B-13 | Hot-swap collapse handling — S34 post-fire re-evaluation             |
| B-14 | Uber stacked re-award — S35 detection (structural inverse of S32)    |
| B-15 | Phase F: add `address` field to TargetSpec; populate target_address   |
|      | in WhereAmIResult (currently always None)                            |
| B-16 | Phase F: add `secondary_pickup` to Offer dataclass (mirror of        |
|      | DriverStateSnapshot.secondary_pickup_lat/lng)                        |
| B-17 | Phase F: audit atomic-swap snapshot assembly (the "wrong pickup"     |
|      | failure class Andrew flagged); ensure DriverStateSnapshot is         |
|      | constructed correctly in STACKED state                               |
| B-18 | Phase G: T50/T60 legacy WatchdogB tests will go green (or be         |
|      | deprecated/removed) when BMOAR Path A/B is removed                   |
| B-19 | Audit tests/test_integration.sh for unrepresented production         |
|      | scenarios (Andrew's request, deferred from Step 2.5)                 |

---

## Protocol — paired-programming, unchanged from Phase D

The cycle that worked for Phase D and Phase E Step 2:

1. Claude proposes (one step at a time)
2. Andrew runs the proposal past Gemini
3. Gemini ratifies or pushes back
4. If ratified, Claude implements with verification gates
5. Andrew runs the gates, pastes output
6. Commit + push immediately, never bundle multiple steps

Verification gates per step:
- pytest count preserved or increased (current floor: 182)
- Integration baseline preserved (current floor: 22/61)
- Live-PG smoke when DB-coupled (per L-8)
- L-2 paranoia: git status before staging, after staging, after commit
- L-3: anchor-based patch scripts with sha-locked pre-conditions for
       any file >100 lines
- L-5: trailing-newline guard
- L-6: read production artifacts before authoring assertions
- L-7: cross-check architectural rulings against production conventions

---

## Concrete first-message-of-new-chat starter

> I'm resuming Phase E in the middle. Step 2 (the PLAN/EXECUTE contract +
> Five Pillars scenario locker) shipped at commit ed0bd0d. Please read
> PHASE_E_PROGRESS.md, PHASE_E_KICKOFF.md, and PHASE_D_RETRO.md before
> responding. Then propose Phase E Step 3 — the pudo_planner.py module
> skeleton — using the same paired-programming protocol that worked for
> Phase D and Step 2. I'll review your proposal with Gemini before locking.
