# Phase E Progress Brief

**For:** A fresh Claude conversation resuming Phase E at Step 6.
**Author:** Phase E Step 5 closing session (commit 49ea6c8).
**Date authored:** 2026-04-26.
**Replaces:** prior version at commit 10eb654 (post Step 4, now stale).

---

## Read these in order before doing anything else

1. **`PHASE_E_KICKOFF.md`** at the repo root — original Phase E entry brief.
   Defines scope, non-goals, paired-programming protocol. Still authoritative
   for architectural ground truth.

2. **`PHASE_D_RETRO.md`** at the repo root — implementation record from Phase D.
   Lessons L-2, L-3, L-5, L-6, L-7, L-8 are protocol guardrails Phase E
   continues to follow. L-4 (State-Machine Side-Effect Guard) is restored in
   the institutional registry per Step 5 closeout consensus. New Phase E
   lessons L-9, L-10, L-11 are recorded in this document.

3. **`WHERE_AM_I_PROPOSAL_v2.md`** at the repo root — design RFC. v2.5
   amendment header explains the Phase D shipped state. Step 6 design
   includes a planned v2.6 amendment for `cluster_revisit` (see below).

4. **This file (`PHASE_E_PROGRESS.md`)** — captures Phase E's current
   state at end of Step 5. Use this as the entry point for Step 6
   planning.

5. **The last 7 commits** — `git log --oneline 10eb654^..HEAD` — Phase E
   Step 5's complete arc (skeleton + 5 sections + introspection close).

6. **`pudo_planner.py`** (1041 lines) and **`tests/test_pudo_planner.py`**
   (1650 lines) — the two artifacts Step 6 integration tests against.

---

## Current state of the world

```
HEAD:                49ea6c8 (Phase E Step 5.7 — Contract introspection. Step 5 closes.)
Branch:              patch-00566a-unified-refinement (clean working tree, in lockstep with origin)
Tests pytest:        250/250 passing
Tests integration:   Live count TBD via Step 6 sub-step 0 (legacy 22/61 figure used a counting convention that included gaps; live ID count is 47)
Live-PG smoke:       4/4 from Phase D Step 5.7.2 (not re-run in Phase E; no DB-coupled changes shipped)
pudo_planner.py:     1041 lines, 4 sections (A/B/C/D), 11 builders, 68-test pytest suite
test_pudo_planner.py: 1650 lines, 68 tests across 6 sub-step blocks
```

Phase E Steps 1-5 are all shipped. **Step 6 (integration bridge) is the next
concrete work.** Step 6 spans 2-3 sessions and is the largest remaining
Phase E step.

---

## Phase E commit lineage (cleanly stacked on Phase D's 60ed517)

| SHA       | Step      | Description                                            |
|-----------|-----------|--------------------------------------------------------|
| 9394449   | 2         | Add DriverStateSnapshot + PlannerDecision dataclasses  |
| 8aeb076   | 2.3       | scenarios/S33_calhoun_missed_pickup.toml               |
| 5277951   | 2.4       | Add fire_stacked_revert to PlannerDecision.action      |
| ed0bd0d   | 2.5       | scenarios/S34 + S35 bundle                             |
| 3fa9626   | 2.6       | PHASE_E_PROGRESS.md (initial)                          |
| 64dee6a   | 3.1       | pudo_planner.py module skeleton (195 lines)            |
| 048498e   | 3.2       | Section A: temporal pattern detection helpers (+236)   |
| 0afb1df   | 3.3       | Section C: decision builders (+312)                    |
| bfbea37   | 3.4       | Section D: consume() dispatch logic (+215 net)         |
| b46d8ef   | 4         | Section B: STACKED contradiction detection (+83 net)   |
| 10eb654   | (handoff) | PHASE_E_PROGRESS.md refresh (Step 4 close)             |
| 4ccde3e   | (retrofit)| PHASE_E_KICKOFF.md author (was missing all session)    |
| c35ec29   | 5.2       | tests/test_pudo_planner.py skeleton + 5 smoke tests    |
| 6289e98   | 5.3       | Section A — temporal pattern detection (24 tests)      |
| a6d64d0   | 5.4       | Section C — decision builders (13 tests, R1)           |
| e2e8165   | 5.5       | Section B — STACKED disambiguation (8 tests, R3-rev)   |
| 186f8c5   | 5.6       | Section D — consume() dispatch (15 tests)              |
| 49ea6c8   | 5.7       | Contract introspection (3 tests). Step 5 closes.       |

**Note on Step 5.1:** No commit exists for Step 5.1. The sub-step was the
test-suite design ratification round (Claude proposes → Gemini reviews →
consensus locks). Design rounds do not produce commits per
paired-programming protocol; only consensus locks. The c35ec29 commit body
records the Gemini ratification reference.

**Note on the two retrofit commits (10eb654, 4ccde3e):** Session-12 worked
through Steps 2-4 with PHASE_E_PROGRESS.md stale across Steps 3.1-3.4,
Step 4, the R3 architectural revision, and backlog items B-14 through
B-21. PHASE_E_KICKOFF.md was never authored despite being referenced for
multiple sessions. Both gaps were caught at session-12 handoff and
retrofitted. The protocol caught the gap; L-11 (below) commits to running
the doc-currency gate at session-open AND session-close every session, so
the gap surfaces earlier.

---

## Phase E Step 5 — closure summary

**Total tests added:** 68 across 7 sub-step commits.
**Floor advance:** 182 → 250 (+68).
**Floor history:** 246 (initial Gemini-ratified target) → 247 (Step 5.6
multi-driver isolation +1, Gemini re-ratified) → 250 (Step 5.7 +3).

### Sub-step floor walk

| Sub-step | Block                                       | Tests | Floor |
|----------|---------------------------------------------|-------|-------|
| 5.2      | Fixture skeleton + smoke                    | 5     | 187   |
| 5.3      | Section A — temporal pattern detection      | 24    | 211   |
| 5.4      | Section C — decision builders               | 13    | 224   |
| 5.5      | Section B — STACKED disambiguation          | 8     | 232   |
| 5.6      | Section D — consume() dispatch              | 15    | 247   |
| 5.7      | Contract introspection                      | 3     | 250   |

### Load-bearing tests promoted in Step 5

These tests are named load-bearing because their failure means a real
production behavior has drifted, not that the test is wrong. Future
maintainers should read the source before "fixing" any of these.

| Test                                                            | What it guards                                            |
|-----------------------------------------------------------------|-----------------------------------------------------------|
| `TestDispatchLongStop::test_long_stop_uses_last_observation_coords` | Houston Drift sentinel: retroactive fire uses last-hit coords |
| `TestStateStore::test_state_store_drivers_isolated`             | Multi-tenant safety: per-driver state isolation           |
| `TestBuildReconcileMissedPickup::test_r1_no_coordinate_synthesis` | R1: state-correction over coordinate-synthesis           |
| `TestBuildReconcileMissedDropoff::test_r1_no_coordinate_synthesis` | R1: same, dropoff direction                              |
| `TestContractIntrospection::test_action_literal_has_eleven_values` | Walk-the-type-system: PlannerDecision.action contract    |
| `TestContractIntrospection::test_all_eleven_builders_emit_distinct_action_literal_values` | Walk-the-builders: inverse-coverage of the contract |
| `TestDispatchStableMatch::test_three_stable_hits_pickup_fires`  | N_HEARTBEATS_TO_FIRE=3 boundary (production constant)    |

### Architectural rulings exercised by the suite

- **R1 (state-correction over coordinate-synthesis):** Two reconcile builder
  tests assert `corrected_lat=None, corrected_lng=None` explicitly. R1 is
  enforced at the contract level.
- **R3-revised (identity over proximity):** All four `TestSectionBCases`
  tests dispatch on `offer_id` identity, never on spatial geometry. WAI's
  confidence model owns the proximity verdict; PLAN reads identity.
- **R4 (TOML uppercase ↔ Python lowercase):** Not directly tested at the
  unit-test layer; deferred to Step 6 integration tests against TOML
  scenarios.

### Step 5 retrospective

**What worked.** The seven-commit sub-step cadence held throughout. Each
commit was independently reviewable, independently verifiable, and
independently revertible. The L-3 anchor-based patch script convention
caught Step 5.6's mid-stream comment-block bug without re-running the
heredoc — exactly the failure mode L-3 was designed for.

**What surfaced.** Three new lessons (L-9, L-10, L-11) were promoted from
Step 5 working notes and the Step 6 design conversation. L-9 and L-10
came from forensic discoveries about test-fixture and gate-threshold
provenance. L-11 came from the session-12 doc-retrofit incident.

**What grew.** Step 5 was estimated at "40-50 test functions" in the Step
4-era plan. Actual: 68 tests, 36-70% over plan. The expansion was driven
by Section A boundary cases (24 vs ~15 estimated) and Section D state-
store invariants (3 added based on Gemini's Step 5.1 ratification of
multi-driver isolation as load-bearing). The over-run was disciplined,
not scope creep.

**What stayed quiet.** L-2 (pre-commit paranoia gate), L-3 (anchor-based
patches), L-5 (trailing-newline guard), L-6 (read production artifacts),
L-7 (cross-check rulings) were all active and exercised. L-8 (live-PG
smoke) was inactive by design — Phase E ships database-blind code per
R2. L-8 reactivates at Step 7.

**Backlog items closed in Step 5.**

- **B-21 (cancel_candidate reachability)** — closes with documented finding:
  cancel_candidate is plausibly unreachable through the current
  consume() dispatch because `fire_retroactive` does the heavy lifting of
  closing dead candidates with a forensic write. The builder remains in
  Section C as a Dead Letter Office; if future dispatch logic emits it,
  the test in `TestStateStore::test_state_store_cleared_on_long_miss`
  already accepts both action values. No code change required.

**Backlog items closed in Step 6 design discussion (resolved before Step 6
implementation begins).**

- **B-20 (cumulative_miles to DriverStateSnapshot)** — closes. Round-trip
  detection is handled structurally via `WhereAmIResult.cluster_revisit`
  (the "Houston Loop" gate), not via odometer delta. No snapshot
  amendment needed. See Step 6 design below.

---

## The Five Pillars of Failure — current handling status

The S31-S35 series captures the canonical failure-mode taxonomy. Status
after Step 5 (unchanged from Step 4 close):

| ID  | Pillar     | Status      | Mechanism                                          |
|-----|------------|-------------|----------------------------------------------------|
| S31 | Geometric  | HANDLED     | WAI confidence model (since Phase D)               |
| S32 | Structural | HANDLED     | Step 4 Section B: fire_stacked_swap                |
| S33 | Temporal   | PARTIAL     | fire_retroactive handles long-stop-then-departure; |
|     |            |             | true reconciliation needs B-12 reconcile dispatch  |
|     |            |             | (Step 7 commitment)                                |
| S34 | Collapse   | PARTIAL     | Single-heartbeat fires correctly; double-tap is    |
|     |            |             | Phase F backlog B-13                               |
| S35 | Inverse    | HANDLED     | Step 4 Section B: fire_stacked_revert              |

Step 6 adds a **sixth class** — the round-trip ride ("Houston Loop") —
which is not strictly one of the Five Pillars but is a load-bearing
production class with its own gate (`cluster_revisit`).

---

## Contract — PlannerDecision.action (11 values, all type-checked)

```
noop                       arm_candidate              cancel_candidate
fire_pickup                fire_dropoff               fire_retroactive
fire_stacked_swap          fire_stacked_revert        cache_ghost
reconcile_missed_pickup    reconcile_missed_dropoff
```

All 11 are type-checked into the `Literal[...]` declaration of
`PlannerDecision.action` in pudo_types.py. Step 5.7's two introspection
tests assert this contract bidirectionally (Literal-walk and builder-walk).

---

## Architectural rulings locked through Phase E

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

**R5 (NEW, Step 6 design): Structural revisit over odometer delta.**
Round-trip detection (pickup address == dropoff address, driver returns
to PUDO after intermediate destination) uses topological evidence
(PUDO cluster → ≥200m intermediate cluster → PUDO cluster) rather than
odometer delta or duration thresholds. Empirically grounded — the
intermediate cluster IS the proof the round-trip happened, regardless
of intermediate-stop duration or distance. See Step 6 design below for
the gate spec.

---

## Phase E Step 6 — Integration Bridge (design ratified, implementation pending)

Step 6 is the largest remaining Phase E step. Spans 2-3 sessions.
Implementation begins next session.

### Pre-Step-6 micro-commit: UTC anchor patch

`tests/test_integration.sh` has three legacy `AT TIME ZONE 'America/Chicago'`
violations in T16, T30, T38 (test-only state backdating SQL). These
predate the rev `00491-mbd` UTC migration. Fix as a single anchor-patch
commit before Step 6 sub-step 0 takes the integration baseline. Reasoning
recorded by Gemini in Step 6 design ratification: "go with the micro-step
anchor patch... if a timing-related bug pops up in Step 6, you know it's
logic-driven, not timezone-drift driven."

### Sub-step 0 — Baseline + inventory + WAI source-read

Three deliverables in one session:

1. **Baseline run.** `bash tests/test_integration.sh 2>&1 | tee
   /tmp/integration_baseline.txt`. Count `❌ FAIL` and `✅ PASS` lines.
   Record actual live count. The legacy 22/61 figure used a counting
   convention that included gaps and commented-out IDs; live ID count
   is 47.
2. **Group E inventory.** Close the long-standing TODO in
   `tests/TEST_SUITE_STATUS.md` by categorizing all T01-T42 failures
   into Groups A-D (or new groups as needed).
3. **`where_am_i.py` cluster-history read.** Determine whether WAI today
   tracks per-driver cluster history. This decides whether sub-step 1's
   `cluster_revisit` amendment is small (extend existing tracking) or
   meaningful (build cluster history primitive). Documents finding in
   sub-step 0 commit body.

### Sub-step 1 — Contract amendments

Per Gemini Q2 ratification, sub-step 1 is **conditionally split** on
sub-step 0.3's WAI source-read finding:

- **1a (conditional):** WAI cluster-history primitive, only if 0.3
  reveals WAI is stateless against cluster history. Single commit if
  triggered.
- **1b (unconditional):** `WhereAmIResult.cluster_revisit: bool` field,
  `CLUSTER_REVISIT_MIN_GAP_M = 200` constant with L-10 provenance, v2.6
  amendment to WHERE_AM_I_PROPOSAL_v2.md. Single commit.

`DriverStateSnapshot` does NOT change. B-20 closed.

### Sub-step 2 — Five Pillars synthetic-heartbeat block (T70-T74)

Five integration tests, one per pillar. Each test feeds a synthetic
heartbeat sequence to assembled `WAI.evaluate() → PudoPlanner.consume()`
and asserts the planner emits the expected PlannerDecision.

| Test | Pillar | Asserts                                                     |
|------|--------|-------------------------------------------------------------|
| T70  | S31    | Geometric — Forum Park 7623 fixture replay → fire_pickup    |
| T71  | S32    | Structural — synth secondary pickup → fire_stacked_swap     |
| T72  | S33    | Temporal — long stop then departure → fire_retroactive      |
| T73  | S34    | Collapse — single-heartbeat fire (B-13 deferred)            |
| T74  | S35    | Inverse — synth primary pickup in STACKED → fire_stacked_revert |

T70 uses the existing `tests/fixtures/7623_heartbeats.json`. T71-T74
require new synthetic fixtures (no production capture available; Phase
F shadow-mode adds forensic replay later). All fixtures declare provenance
per L-9.

### Sub-step 3 — Round-trip block (T75-T79) — the "Houston Loop"

Five integration tests covering the round-trip dropoff class. The gate:

```
PUDO cluster (cluster 1)
  → ≥200m intermediate cluster (cluster 2)
  → PUDO cluster (cluster 3)
  ⟹ WAI returns cluster_revisit=True
  ⟹ Planner fires fire_dropoff
```

| Test | Asserts                                                                |
|------|------------------------------------------------------------------------|
| T75  | Pickup fires normally at PUDO when pickup_address == dropoff_address   |
| T76  | Outbound leg — driver leaves PUDO, planner emits noop                  |
| T77  | Return leg — cluster 3 forms, cluster_revisit=True, fire_dropoff emits |
| T78  | Houston Drift sentinel — fire_dropoff coords from cluster 3, not cluster 1 |
| T79  | Single-cluster guard — driver never leaves PUDO, no fire_dropoff       |

**T79 is the load-bearing safety test of the round-trip block.** If T79
fails, the system fires `fire_dropoff` at the pickup before the driver
has actually left, writing a corrupt audit row that says a trip
completed when no trip was driven. This is the production failure mode
the legacy BMOAR Path B detector exhibits on same-address rides.

All T75-T79 fixtures are authored from first principles per L-9. Legacy
Scenario 14-15 coordinates (`29.5068, -95.41` / `29.5984, -95.62`) are
NOT inherited — they were artifacts of the state-machine poisoning bug
this rewrite retires.

### Sub-step 4 — STACKED-via-WAI block (T80-T89)

Replaces the legacy T50/T60 STACKED scaffold. Ten test slots reserved
for breathing room. Initial allocation:

| Test | Replaces / Asserts                                                |
|------|-------------------------------------------------------------------|
| T80  | fire_stacked_swap (replaces T50d)                                 |
| T81  | fire_stacked_revert (no legacy analog; new behavior)              |
| T82  | Primary audit not poisoned (replaces T50e — PRESERVE-ASSERTION)   |
| T83  | current_offer_id swaps to secondary (replaces T50f — PRESERVE-ASSERTION) |
| T84  | Secondary cancellation while STACKED (replaces T60a-d)            |
| T85  | Guard: not_at_pudo while STACKED → fall through                   |
| T86  | Guard: secondary dropoff before secondary pickup → silent fall-through (B-14 placeholder) |
| T87-T89 | Reserved for STACKED forensic variations as production data surfaces |

### Sub-step 5 — Reconcile reservation (T90-T99)

Reconcile dispatch (B-12) lives in **Step 7**, not Step 6. Sub-step 5
reserves the T90-T99 block as placeholders only; tests are not authored
in Step 6. The reservation prevents future ID-collision when Step 7
implements the dispatch path that emits `reconcile_missed_pickup` and
`reconcile_missed_dropoff`.

### Sub-step 6a — Legacy triage classification (decision)

Per-ID classification of all 47 live IDs in `tests/test_integration.sh`
against the sub-step 0 baseline. Output: a new section in
`tests/TEST_SUITE_STATUS.md` with the per-ID classification table.

**This sub-step ships ZERO deletions.** Classification only. RETIRE
deletions land in sub-step 6b per Gemini Q4 ratification.

Triage rubric:

| Disposition           | Meaning                                                       |
|-----------------------|---------------------------------------------------------------|
| **PRESERVE**          | Test asserts unchanged contract; current code path satisfies  |
| **PRESERVE-ASSERTION**| Test text unchanged; new code path satisfies (e.g., T50e/f → T82/T83) |
| **REPLACE**           | Behavior carries forward; new T7x-T8x test authored for new arch |
| **RETIRE**            | Test was forensic artifact of a bug now eliminated; no successor |
| **CASCADE**           | Will pass automatically when its parent REPLACE clears        |

**Pre-classified examples** (full triage in sub-step 6a commit):
- T44-T49 → **RETIRE** (poisoned-state-machine forensic artifacts, per L-9)
- T50e, T50f → **PRESERVE-ASSERTION** (replaced by T82, T83)
- T50d → **REPLACE** (replaced by T80)
- T01, T15, T40-T42 → **PRESERVE** (decision endpoint, manual reset)
- T48 → **CASCADE** (passes when T47's REPLACE clears) — but T47 is
  RETIRE not REPLACE, so T48 reclassifies as RETIRE in cascade
- T17, T39 (ABORT guard) → **PRESERVE** (orthogonal safety net)
- T30, T31 (Watchdog A) → **PRESERVE** (orthogonal safety net)

### Sub-step 6b — RETIRE structural cleanup (action)

Delete legacy tests classified RETIRE in sub-step 6a from
`tests/test_integration.sh`. Per Gemini Q4: triage decisions and
structural actions ship as separate commits. L-3 anchor-based patch
script for the deletions. Forensic rationale per L-9 in commit body.

### Sub-step 7 — Two safety tests as standalone integration commits

**Double-fire safety test.** After `fire_pickup`, the next heartbeat at
the same position must NOT re-fire. Per Gemini Step 6 design ratification:
"the load-bearing bridge of Phase F." The test exercises the planner's
post-fire state behavior; if it fails, Phase F's heartbeat-loop integration
must clear temporal state post-fire.

**T79 single-cluster safety test** (re-emphasized as a separate commit
even though it lands in sub-step 3). The two safety tests together fence
the architecture from both the post-fire and pre-revisit failure modes.

### Sub-step 8 — Closeout doc refresh + Step 7 prep

Final commit of Step 6: refresh PHASE_E_PROGRESS.md (this file) to
reflect Step 6 closure, document Step 7 scope (live-PG smoke), and
prepare the next-session-starter block. Per L-11.

---

## Phase E Step 7 — Live-PG smoke (mirrors Phase D Step 5.7.2)

Run `pudo_planner` against real PostgreSQL via the same pattern as
`scripts/wai_smoke.py`. PLAN itself is database-blind, but Step 7
exercises the full assembly: snapshot construction (mocked from
production-shape rows) → WAI evaluate → PLAN consume → verify
PlannerDecision shape. Catches schema-shape assumptions before Phase F
wires production traffic. **L-8 reactivates here.**

Step 7 also includes **B-12 reconcile dispatch implementation** —
wiring the dispatch path that emits `reconcile_missed_pickup` and
`reconcile_missed_dropoff` so the builders are no longer Dead Letter
Office only. Tests for the dispatch path land in T90-T99.

---

## Phase F (next phase, separate session strongly recommended)

Heartbeat-loop integration. Modifies `driver_heartbeat.py` (production-
critical). Adds `address` field to `TargetSpec` so
`WhereAmIResult.target_address` populates. Adds `secondary_pickup` to
`Offer` dataclass. Builds shadow-mode logging surface. Wires
`WAI_PLANNER_ENABLED_DRIVERS` flag. **Audits atomic-swap snapshot
assembly** (the "wrong pickup" failure class flagged at Step 2.5;
the contract for that audit is now firm because Section B's identity-
based detection only works if Phase F builds the snapshot correctly).

Phase F is also where shadow-mode telemetry for `cluster_revisit`
gates the round-trip detection in production data (B-24).

---

## Phase G — BMOAR Path A/B deprecation

Once shadow-mode telemetry validates WAI/PLAN matches or exceeds BMOAR's
fire rate without false positives. Cannot ship before sufficient
production data exists.

---

## Backlog (current state)

| ID   | Item                                                                  | Status |
|------|----------------------------------------------------------------------|--------|
| B-11 | (carried from Phase D) — Implicit STACKED cancel                     | CLOSED at Step 4 |
| B-12 | Calhoun-class missed-pickup reconciliation logic                     | Step 7 |
| B-13 | Hot-swap collapse: Phase F double-tap WAI after fire                 | Phase F |
| B-14 | Sequence Violation Detector — secondary dropoff in STACKED before    | Phase F |
|      | secondary pickup; forensic alert (T86 placeholder)                   |        |
| B-15 | Phase F: add `address` field to TargetSpec; populate target_address  | Phase F |
| B-16 | Phase F: add `secondary_pickup` to Offer dataclass                   | Phase F |
| B-17 | Phase F: audit atomic-swap snapshot assembly                         | Phase F |
| B-18 | Phase G: T50/T60 legacy WatchdogB tests retire when BMOAR deprecates | Phase G |
| B-19 | Step 6: triage tests/test_integration.sh per REPLACE/RETIRE/PRESERVE | Step 6 sub-step 6 |
| B-20 | (closed) cumulative_miles to DriverStateSnapshot                     | CLOSED — replaced by R5 structural revisit gate |
| B-21 | (closed) cancel_candidate reachability                               | CLOSED — Dead Letter Office, fire_retroactive does the work |
| B-22 | WAI per-driver cluster history with 200m spatial filter              | Step 6 sub-step 1 |
| B-23 | Fast-pickup cluster minimum — investigate WAI 15s floor causing      | Phase F observability |
|      | pickup-nail failures on quick pickups (forensic, not gating)         |        |
| B-24 | Phase F shadow-mode logging of cluster_revisit_consideration events  | Phase F |
| B-25 | Fast-errand floor evaluation — counter to B-24 if production shows   | Phase G |
|      | legitimate intermediate stops below WAI cluster floor                |        |

---

## Lessons learned (Phase E new entries)

L-2, L-3, L-5 (Phase D Steps 1-4) and L-6, L-7, L-8 (Phase D Step 5.6-5.7)
remain authoritative. L-4 (State-Machine Side-Effect Guard) is restored
in the institutional registry per Step 5 closeout consensus. The three
new entries below are Phase E contributions.

### L-9 — Fixture provenance traceability (NEW, Phase E Step 5/6 transition)

**Observation:** Step 6 design discovered that legacy
`tests/test_integration.sh` Scenarios 14-15 used coordinates inherited
from a poisoned-state-machine bug. The "divergent geocode" framing in
the test comments was itself a forensic mis-attribution; the divergence
was cross-ride contamination from stale `offer_history` coords, not
geocoder behavior. Authoring new tests against those coordinates would
have re-imported the bug as a "specification."

**Protocol change:** Every fixture used in synthetic-heartbeat tests
declares its provenance in the fixture's docstring or the test class's
docstring. Acceptable provenance:
1. Authored from first principles for the new architecture, with the
   spec it tests written out in the docstring.
2. Forensic replay of a real production drive, with `offer_history`
   row ID + timestamp recorded.

No "borrowed" coordinates from prior test runs without declared
provenance. The provenance check is a verification gate at sub-step
authoring time.

### L-10 — Gate threshold provenance traceability (NEW, Phase E Step 6 design)

**Observation:** Step 6 design discussion surfaced a proposed 60-second
minimum-duration floor for cluster 2 in the round-trip gate. The floor
was paranoia-driven (theoretical concern about GPS multipath
teleportation), not data-grounded. Andrew vetoed it because production
GPS knowledge contradicted the theoretical concern, and because the
floor would have rejected legitimate fast-errand round-trips. The right
answer (200m spatial separation alone, no duration floor) was both
simpler and grounded in real Houston signal behavior.

**Protocol change:** Every numeric gate constant in production code
(radii, durations, count thresholds) declares its provenance in a
code comment:
1. **Production-data-grounded:** value derived from observed forensic
   data with the dataset/case named.
2. **Theoretical-with-shadow-mode-instrumentation:** value chosen for
   theoretical reasons but ships with telemetry that measures whether
   it's right.
3. **Paranoia:** value chosen because "it feels safe."

Paranoia numbers do not ship. They are either promoted to category 1 or
2 with evidence, or removed.

### L-11 — Documentation currency as session-bookend gate (NEW, Phase E Step 5 closeout)

**Observation:** Phase E sessions 11-12 worked through Steps 2-4
referencing PHASE_E_PROGRESS.md content that was stale across Steps
3.1-3.4, Step 4, the R3 architectural revision, and backlog items
B-14 through B-21. PHASE_E_KICKOFF.md was never authored despite being
referenced as architectural ground truth. Both gaps were caught at
session-12 handoff and retrofitted (commits 10eb654, 4ccde3e). The
protocol caught the gap; the cadence of catching it should be tighter.

**Protocol change:** Every session opens with a doc-currency check —
read PHASE_X_PROGRESS.md, verify the "Current state of the world" block
matches HEAD / pytest baseline / integration baseline. If mismatched,
refresh BEFORE proceeding to the work. Every session closes with a
doc-currency refresh — update PHASE_X_PROGRESS.md to reflect what
shipped, even if it duplicates content from the most recent commit body.
The progress doc is the single source of truth for cross-session
handoff; commits are the audit trail.

---

## Protocol — paired-programming, unchanged

The cycle that has worked through 18 Phase E commits:

1. Claude proposes (one step at a time, with design discussion when
   warranted)
2. Andrew runs the proposal past Gemini (Grok occasionally)
3. Gemini ratifies or pushes back
4. If ratified, Claude implements with verification gates
5. Andrew runs the gates, pastes output
6. Commit + push immediately, never bundle multiple steps

**Verification gates per step (Phase E baseline):**
- pytest count preserved or increased (current floor: **250**)
- Integration baseline preserved (live count TBD via Step 6 sub-step 0)
- Live-PG smoke when DB-coupled (per L-8) — none in Phase E so far;
  reactivates Step 7
- L-2 paranoia: `git status` before staging, after staging, after commit
- L-3: anchor-based patch scripts with sha-locked pre-conditions for
       any file >100 lines
- L-5: trailing-newline guard
- L-6: read production artifacts before authoring assertions
- L-7: cross-check architectural rulings against production conventions
- **L-9 (NEW): fixture provenance declared in docstring**
- **L-10 (NEW): gate threshold provenance declared in code comment**
- **L-11 (NEW): doc-currency check at session-open AND session-close**

**Predict-then-verify pattern.** Each verification gate names an
expected value before the gate runs. Mismatches between expectation and
observation are first-class signals — a passing gate with mismatched
prediction means scope drifted, even if structurally sound.

**Heredoc transit observation (Phase E session-13):** Large heredocs
(>200 lines of embedded content) consistently produce a terminal-render
artifact where content fragments visibly bleed into the prompt. The
file itself is always clean — verified by SYNTAX OK + head/tail
boundary check after the prompt returns. Step 6 should consider
base64-encoded patch script transfers when patch content exceeds
~200 lines. Anchor-script transfers (per L-3) are the preferred
mechanism for surgical edits regardless of size.

---

## Concrete first-message-of-new-chat starter

> I'm resuming Phase E at Step 6. Step 5 (test suite for pudo_planner.py)
> closed at commit 49ea6c8. The 68-test suite for `pudo_planner.py`
> shipped. Floor 250.
>
> Step 6 is the integration bridge — synthetic-heartbeat tests for the
> Five Pillars (T70-T74), the Round-trip block (T75-T79), STACKED-via-WAI
> (T80-T89), and the legacy REPLACE/RETIRE/PRESERVE/CASCADE/
> PRESERVE-ASSERTION triage of all 47 live IDs in
> `tests/test_integration.sh`. Step 6 spans 2-3 sessions.
>
> Please read in order:
>   1. PHASE_E_PROGRESS.md (this file — captures everything you need)
>   2. PHASE_E_KICKOFF.md (architectural ground truth)
>   3. tests/TEST_SUITE_STATUS.md (legacy debt ledger; Group E TODO open)
>   4. pudo_planner.py (1041 lines)
>   5. tests/test_pudo_planner.py (1650 lines, 68 tests)
>   6. WHERE_AM_I_PROPOSAL_v2.md (planned v2.6 amendment for cluster_revisit)
>
> Then propose Step 6 sub-step 0 — the UTC anchor-patch micro-commit
> followed by the integration-suite baseline + Group E inventory +
> `where_am_i.py` cluster-history read. The same paired-programming
> protocol that worked through Step 5 applies. The new lessons L-9
> (fixture provenance), L-10 (gate threshold provenance), and L-11
> (doc-currency session-bookend gate) are now active verification
> gates.
>
> Constraints: Step 6 is the largest remaining Phase E step. It will
> likely span 2-3 sessions. Reconcile dispatch (B-12) is Step 7.
> Backlog items B-22, B-24, B-25 are Phase F observability work.
