# Phase E Progress Brief

**For:** A fresh Claude conversation resuming Phase E at Step 6 sub-step 1c.
**Author:** Phase E Step 5 closing session (commit 49ea6c8).
**Date authored:** 2026-04-26.
**Updated:** 2026-04-27 — sub-step 1c closeout (commit 72cf951).
**Replaces:** prior version at commit a68447b (post sub-step 1b.3, pre 1c closeout).

---

## Read these in order before doing anything else

1. **`PHASE_E_KICKOFF.md`** at the repo root — original Phase E entry brief.
   Defines scope, non-goals, paired-programming protocol. Still authoritative
   for architectural ground truth.

2. **`PHASE_D_RETRO.md`** at the repo root — implementation record from Phase D.
   Lessons L-2, L-3, L-5, L-6, L-7, L-8 are protocol guardrails Phase E
   continues to follow. L-4 (State-Machine Side-Effect Guard) is restored in
   the institutional registry per Step 5 closeout consensus. New Phase E
   lessons L-9, L-10, L-11 + L-6 corollary are recorded in this document.

3. **`WHERE_AM_I_PROPOSAL_v2.md`** at the repo root — design RFC. v2.5
   amendment header explains the Phase D shipped state. v2.6 amendment
   for `cluster_revisit` is live (commit `aa8e667`).

4. **`PHASE_E_STEP_6_DESIGN.md`** at the repo root — Step 6 design proposal,
   originally ratified by Gemini 2026-04-26, **amended 2026-04-27 with
   Amendment 1 (Offer-Anchor Lookback)**. Read the top-of-doc Amendment 1
   notice first, then the body, then the full Amendment 1 spec at the end.

5. **This file (`PHASE_E_PROGRESS.md`)** — captures Phase E's current
   state at end of sub-step 1c. Use this as the entry point for
   sub-step 2 authoring.

6. **The commits since 49ea6c8** — `git log --oneline 49ea6c8..HEAD`
   covers Step 5 closeout + Step 6 design + Step 6 sub-step 0 +
   Amendment 1 + sub-step 1a + sub-step 1b (1b.1 + 1b.2 + 1b.3) +
   sub-step 1b.3 doc refresh + sub-step 1c (Memory–Signal–Latch trilogy
   complete).

7. **`pudo_planner.py`** (1159 lines) and **`tests/test_pudo_planner.py`**
   (1892 lines) — the Phase E artifacts.

8. **`cluster_detection.py`** (316 lines) and **`tests/test_cluster_detection.py`**
   — houses sub-step 1a's `get_recent_clusters()` primitive per
   Amendment 1.

9. **`where_am_i.py`** (1286 lines) and **`tests/test_where_am_i.py`**
   (1804 lines) — the WAI consumer; sub-step 1b houses `cluster_revisit`
   topology (Step 3.5 in `evaluate()`) plus the verification suite.
   Sub-step 1c renamed `_haversine_meters → haversine_meters` (4 internal
   call sites + 4 test sites updated; L-6 corollary extension second strike).

---

## Current state of the world

```
HEAD:                 72cf951 (Phase E Step 6 sub-step 1c — Memory–Signal–Latch trilogy complete; B-26 PLAN-side latch shipped)
Branch:               patch-00566a-unified-refinement (in lockstep with origin; untracked patch/refresh scripts + tmp/ + session-handoff doc — see `git status` for current set)
Tests pytest:         290/290 passing (floor preserved through 1c — +13 tests for B-26 latch: TestIsSamePudoColocation 7 + TestSamePudoColocationLatch 6)
Tests integration:    22/61 passing (39 failing) per sub-step 0.1 baseline 2026-04-26 23:46:19 UTC (preserved through 1c — test-only contract addition, no DB-coupled changes)
Live-PG smoke:        4/4 from Phase D Step 5.7.2 (not re-run in Phase E; no DB-coupled changes shipped)
pudo_planner.py:      1159 lines, 4 sections (A/B/C/D), 12 builders (1c added _build_noop_same_pudo_no_revisit), 81-test pytest suite
test_pudo_planner.py: 1892 lines (1b.1 added cluster_revisit=False to 1 WhereAmIResult site; 1c added TestIsSamePudoColocation + TestSamePudoColocationLatch, +13 tests / +242 lines)
cluster_detection.py: 316 lines (sub-step 1a SHIPPED — get_recent_clusters() + Cluster.latest; unchanged in 1c)
test_cluster_detection.py: 471 lines (8 new T1-T8 tests appended in sub-step 1a; unchanged in 1c)
where_am_i.py:        1286 lines (1b.2 SHIPPED +117 lines: cluster_revisit topology; 1c renamed _haversine_meters → haversine_meters as cross-module helper, +11 lines)
test_where_am_i.py:   1804 lines (1b.3 +420 lines: TestComputeClusterRevisit + TestEvaluateClusterRevisit + _FakeCursor.fetchall() fix + cluster_revisit=False assertion in standard-case test; 1c +rename inventory updates: 1 import + 3 test calls per haversine_meters rename, L-6 corollary extension second strike)
pudo_types.py:        326 lines. Offer + accepted_at:datetime (1b.1), WhereAmIResult + cluster_revisit:bool (1b.1), PlannerDecision.action expanded to 12-value Literal with noop_same_pudo_no_revisit (1c)
```

**Sub-step 2 is the next concrete work** — Five Pillars synthetic-heartbeat
block (T70-T74) in `tests/test_pudo_planner.py`. Five integration tests, one
per pillar (S31-S35), each feeding a synthetic heartbeat sequence to assembled
`WAI.evaluate() → PudoPlanner.consume()` and asserting the planner emits the
expected PlannerDecision. T70 reuses `tests/fixtures/7623_heartbeats.json`
(Forum Park 7623 fixture replay → fire_pickup); T71-T74 require new synthetic
fixtures (no production capture available; Phase F shadow-mode adds forensic
replay later). All fixtures declare provenance per L-9. T70-T74 are the FIRST
integration-level tests of the assembled WAI→PLAN dispatch chain — they prove
the Memory–Signal–Latch trilogy composes correctly before Phase F wires the
trilogy into `driver_heartbeat.py`. Treat as load-bearing.

Phase E Steps 1-5 shipped. Step 6 sub-step 0 shipped (0.1 baseline, 0.2 Group E
inventory closure, 0.3 WAI source-read finding). Step 6 design ratified, then
amended 2026-04-27 with Amendment 1 (Offer-Anchor Lookback) — the fixed
30-minute lookback for `get_recent_clusters()` was rejected as paranoia-class
per L-10 and replaced with an event-relative window pinned to
`offer_history.accepted_at` + 60s pre-roll. **Sub-step 1a SHIPPED at commit 6fe454a**
(`get_recent_clusters()` + `Cluster.latest`, 250 → 258 floor). **Sub-step 1b
SHIPPED across three commits** — `6e1d60f` (1b.1 contract migration:
`Offer.accepted_at` + `WhereAmIResult.cluster_revisit`, 9 sites / 6 files,
258 floor preserved), `6522ba5` (1b.2 WAI wiring: `_compute_cluster_revisit()`
helper + Step 3.5 in `evaluate()`, +117 lines in `where_am_i.py`, 258 floor
preserved), `a68447b` (1b.3 verification: 19 new tests + adjunct `_FakeCursor`
fix, 258 → 277 floor). **Sub-step 1c SHIPPED at commit 72cf951** — B-26
PLAN-side latch with Path A (coordinate-only) design, `PUDO_COLOCATION_THRESHOLD_M = 30.0`
module-level constant per L-10 cat-2 provenance, `_is_same_pudo_colocation`
pure helper, `_build_noop_same_pudo_no_revisit` builder (12th action variant),
dispatch-level latch guard (sidesteps L-6 corollary by avoiding signature
change), 13 new tests (TestIsSamePudoColocation 7 + TestSamePudoColocationLatch
6), boundary triple at 29.5/30.0/30.5m via REPL probe per L-9 corollary
(80-iteration convergence). The Memory–Signal–Latch trilogy of Sub-step 1 is
**COMPLETE.** Sub-step 2 (Five Pillars synthetic-heartbeat block, T70-T74) is
the first integration-level proof that the trilogy composes correctly under
runtime dispatch.

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
| bcd3b8a   | 5 close   | PHASE_E_PROGRESS.md refresh (Step 5 closeout per L-11) |
| 46dea00   | 6 design  | PHASE_E_STEP_6_DESIGN.md authored (Gemini ratified)    |
| 1f04d23   | Pre-6     | UTC anchor patch on tests/test_integration.sh (T16/T30/T38) |
| 86ea117   | 6.0.1     | Sub-step 0.1 — live integration baseline 22/61 (L-6 corollary) |
| 5a86f2e   | 6.0.2     | Sub-step 0.2 — Group E inventory closure (39 failures classified) |
| 7863b11   | 6.0.3     | Sub-step 0.3 — WAI source-read: stateless against cluster history |
| 7a8616a   | 6 amend   | Step 6 Amendment 1 (Offer-Anchor Lookback) ratified |
| 89367ff   | 6 doc     | PHASE_E_PROGRESS.md refresh per L-11 (post-Amendment 1) |
| 6fe454a | 6.1a      | Sub-step 1a — get_recent_clusters() + Cluster.latest (258 floor) |
| 78f1b5e | 6.1a doc  | PHASE_E_PROGRESS.md SHA placeholder substitution (bookkeeping) |
| aa8e667 | 6 amend   | WHERE_AM_I_PROPOSAL_v2.md v2.6 amendment (cluster_revisit topology) |
| 6e1d60f | 6.1b.1    | Contract migration: Offer.accepted_at + WhereAmIResult.cluster_revisit (9 sites, 6 files, 258 floor) |
| 6522ba5 | 6.1b.2    | WAI wiring: cluster_revisit topology live in evaluate() (11 patches, 1 file, 258 floor) |
| a68447b | 6.1b.3    | Verification gap closed: TestComputeClusterRevisit (12) + TestEvaluateClusterRevisit (7) + _FakeCursor.fetchall fix (258 → 277 floor) |
| 840ae49 | 6.1b.3 doc | PHASE_E_PROGRESS.md refresh for sub-step 1b.3 closeout per L-11 |
| 72cf951 | 6.1c       | B-26 PLAN-side latch — Memory-Signal-Latch trilogy complete (290 floor) |

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

**Note on Amendment 1 (7a8616a, 2026-04-27):** Step 6 design was originally
ratified 2026-04-26 with `CLUSTER_HISTORY_LOOKBACK_SEC = 1800` (30 min) as
the cluster-history lookback default. During the sub-step 1a design
ratification round (post-7863b11), Gemini identified the fixed window as
paranoia-class per L-10 and clock-drift-risky on the lookback boundary.
The refined design uses an event-relative window pinned to
`offer_history.accepted_at` + 60s pre-roll, sourced from the database
(never Python clock). Amendment 1 also introduces a same-address PLAN-side
latch (B-26) lifting T79's test-time assertion to a runtime gate. Original
ratified design is preserved verbatim in PHASE_E_STEP_6_DESIGN.md;
Amendment 1 is appended at end of that doc with a top-of-doc notice.

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

**R5 (rev, 2026-04-27 Amendment 1): Structural revisit, offer-anchored.**
Round-trip detection (pickup address == dropoff address, driver returns
to PUDO after intermediate destination) uses topological evidence
(PUDO cluster → ≥200m intermediate cluster → PUDO cluster) within a
window pinned to the current offer's database-recorded `accepted_at`
timestamp + 60s pre-roll buffer. No fixed time lookback. No odometer
dependency. The intermediate cluster IS the proof, anchored by the
database-recorded offer-acceptance event. See PHASE_E_STEP_6_DESIGN.md
Amendment 1 for full spec — `get_recent_clusters()` signature, the new
`CLUSTER_HISTORY_PREROLL_SEC = 60` constant (L-10 category 2:
theoretical-with-shadow-mode-instrumentation), and the R5-corollary
same-address PLAN-side latch in `pudo_planner.py` (B-26).

The `CLUSTER_REVISIT_MIN_GAP_M = 200` value is unchanged across the
amendment. Per Gemini's framing it is a **Structural Noise Floor**
(Houston GPS multipath wobble), not a policy threshold — adjusting it
requires a physics-of-the-environment justification, not a behavioral
preference.

---

## Phase E Step 6 — Integration Bridge (sub-step 0 closed, sub-step 1a next)

Step 6 spans 2-3 sessions. Sub-step 0 closed 2026-04-27 (commit `7863b11`).
Step 6 Amendment 1 ratified 2026-04-27 (commit `7a8616a`). Sub-step 1a
authoring opens next session.

### Pre-Step-6 micro-commit: UTC anchor patch — SHIPPED at `1f04d23`

`tests/test_integration.sh` had three legacy `AT TIME ZONE 'America/Chicago'`
violations in T16, T30, T38 (test-only state backdating SQL). Predated the
rev `00491-mbd` UTC migration. Fixed via single anchor-patch commit before
sub-step 0.1 baseline run. Reasoning recorded by Gemini in Step 6 design
ratification: "go with the micro-step anchor patch... if a timing-related
bug pops up in Step 6, you know it's logic-driven, not timezone-drift
driven."

### Sub-step 0 — Baseline + inventory + WAI source-read — CLOSED 2026-04-27

Three deliverables shipped across three commits:

1. **Sub-step 0.1 (`86ea117`).** Live integration baseline run:
   `bash tests/test_integration.sh` returned 22 passing / 39 failing /
   61 total. Confirmed the long-standing 22/61 figure is canonical;
   the Step-4-era forensic read undercounting at 47 was retired. L-6
   corollary committed (forensic counts must be sourced from live
   runs, not pattern-grepped reads).

2. **Sub-step 0.2 (`5a86f2e`).** Group E inventory closure. All 39 failing
   tests categorized into Groups A/B/C/D + new Group F (setup-cascade
   from Group A). Distribution: A=13, B=8, C=3, D=11, F=4. The long-
   standing TODO in `tests/TEST_SUITE_STATUS.md` is closed.

3. **Sub-step 0.3 (`7863b11`).** WAI cluster-history source-read finding:
   **WAI is stateless against cluster history.** `WhereAmI.__init__`
   (where_am_i.py:770–794) stores only `self.cur`, `self._cluster_fn`,
   `self._pivot_fn` — no cluster-history attribute. `WhereAmI.evaluate()`
   (where_am_i.py:795–844) calls `self._cluster_fn(driver_id, self.cur)`
   once per invocation at line 811, binds the cluster to a local
   variable, discards on return. Class docstring at line 757 ("Pure
   DIAGNOSE per the 4-Box Controller. Reads only.") and Q12 comment at
   line 842 make statelessness an architectural lock. Sub-step 1
   therefore splits firm into 1a + 1b (no longer conditional).

### Sub-step 1a — get_recent_clusters() + Cluster.latest — CLOSED 2026-04-27

Single-commit ship at `6fe454a`. Three artifacts:

1. **`Cluster` dataclass extended** with `latest: datetime` field (sixth field
   appended). The data was already aggregated by detect_cluster()'s SQL via
   `MAX(logged_at)` — the field exposes it. `detect_cluster()`'s return
   updated to populate it; no SQL change. 5 external call sites updated for
   the now-required `latest=` kwarg (test_scenarios, test_where_am_i factory,
   test_pudo_planner ×2, scripts/wai_smoke).

2. **`get_recent_clusters()` authored** in `cluster_detection.py` per
   Amendment 1's offer-anchored signature. Window: `[accepted_at_anchor -
   preroll_sec, NOW()]`. SQL is a **gaps-and-islands extension** of
   `detect_cluster()`'s `breaks_before` pattern: where `detect_cluster()`
   filters `breaks_before = 0` to keep only the most-recent island, this
   GROUPs on `breaks_before` so each distinct value forms one chronologically-
   contiguous low-speed run. Three deliberate departures from
   `detect_cluster()`: single query (not two), late spread filter (drops
   only the offending island), explicit `<= NOW()` upper bound for synthetic
   test safety. Ratified by Gemini 2026-04-27 with no pushback.

3. **8 new T1-T8 unit tests** appended to `tests/test_cluster_detection.py`
   following the file's mock-cursor convention (MagicMock with
   `fetchall.return_value`, hand-computing what SQL would return). Tests
   T4 (gaps-and-islands behavior) and T5 (lookback boundary) softened to
   function-level claims — SQL correctness is validated at integration
   (`tests/test_integration.sh`, current floor 22/61), not in unit-mock
   tests. T6 locks the parameter binding contract; T8 locks default-kwarg
   parity with `detect_cluster()`. Floor: 250 → 258 passing.

**Apply-time bugs caught in `apply_substep_1a.py`** (forensic record;
documented in the script's `ensure_imports` docstring). All three were
import-detection bugs in the patch helper, all repaired live without
requiring a second commit:

- Substring probe false-positive: `tests/test_pudo_planner.py` already
  had `from datetime import timezone` (no `datetime`), which matched the
  probe `"from datetime import"` and suppressed insertion. Repaired by
  switching the file's two new call sites to `datetime.datetime(...)`
  (matching the file's existing convention via `import datetime` at line 21).
- Open-paren confusion: `tests/test_where_am_i.py` had `from where_am_i
  import (` as the last `from` line in the first 50, so insertion landed
  inside the open parens. Repaired by relocating the import.
- Module-vs-class shadowing: same file has `import datetime` at line 940,
  rebinding the name back to the module after our `from datetime import
  datetime`. Repaired by aliasing to `_dt`/`_tz` in lines 27 and 65.

**Architectural rulings exercised:** L-2 (predict-then-verify on every
gate), L-3 (anchor-based patch script), L-5 (trailing-newline guard
implicit in test additions), L-6 (read `cluster_detection.py` and
`test_cluster_detection.py` verbatim before authoring), L-6 corollary
(reading external `Cluster()` call sites before patching), L-7 (UTC
ruling reconciled with bare-`NOW()` engine convention via Gemini Gate A),
L-9 (synthetic Null Island fixtures fine for unit tests; integration
gets live data), L-10 (default kwargs locked as single source of truth
across `detect_cluster()` and `get_recent_clusters()` via T8), L-11
(this entry).

### Sub-step 1b — cluster_revisit topology: the "Signal" of the Memory–Signal–Latch trilogy — CLOSED 2026-04-27

Three-commit ship across `6e1d60f` → `6522ba5` → `a68447b`. Together with sub-step 1a (Memory) and sub-step 1c (Latch, NEXT), constitutes the contract-amendments family of Step 6 sub-step 1.

**1b.1 — Contract migration (`6e1d60f`).** `Offer.accepted_at: datetime` and `WhereAmIResult.cluster_revisit: bool` fields added to `pudo_types.py`. Nine call sites across six files updated for the new required fields. 258 floor preserved (placeholder `cluster_revisit=False` at all WAI return paths).

**1b.2 — WAI wiring (`6522ba5`).** `+117 lines` in `where_am_i.py`. The "Signal" of the Houston Loop is now live in code:
- Import: `+ get_recent_clusters from cluster_detection`.
- Constant: `CLUSTER_REVISIT_MIN_GAP_M = 200.0` with L-10 cat-1 provenance comment (production-data-grounded structural noise floor; Houston GPS multipath wobble; not a policy threshold).
- New pure helper: `_compute_cluster_revisit(active_cluster, recent_clusters)`. O(n²) nested-loop topology check using `_haversine_meters` for distance. Returns True iff a non-active cluster within `MIN_GAP_M` of the active centroid AND ≥1 intermediate cluster ≥ `MIN_GAP_M` from BOTH (the active cluster AND the prior PUDO). The "from both" framing protects against GPS multipath shuffling at one address being mistaken for a true round-trip.
- `WhereAmI.__init__`: `+ _recent_clusters_fn=get_recent_clusters` as a third keyword-only injected callable, mirroring `_cluster_fn` / `_pivot_fn`.
- `evaluate()` body: new Step 3.5 (cluster history topology) between Step 3 (stop_context) and Step 4 (current-ride PUDO matching). Computes `cluster_revisit` only when `current_offer is not None` (Q2 ratification — ride-scoped, not driver-scoped).
- 3 builder signatures expanded with `cluster_revisit: bool`: `_match_ghost_cache`, `_at_unknown_pudo`, `_build_current_result`. Three placeholder `cluster_revisit=False` values inside builder bodies replaced with `cluster_revisit=cluster_revisit`. `_not_at_pudo`'s placeholder stays `False` permanently per Q1 (no active cluster → revisit physically impossible).
- 258 floor preserved.

**Design ratifications captured (Gemini 2026-04-27):**
- **Q1** — `_not_at_pudo` (no active cluster): `cluster_revisit=False` permanently. No call to `get_recent_clusters()` in this case (performance + logic).
- **Q2** — `current_offer is None`: `cluster_revisit=False` permanently. No fallback time anchor; the signal is ride-scoped not driver-scoped.
- **Q3** — Ghost-cache hit with no offer = False per Q2. The "we've seen this stop before" signal is `status='at_previous_pudo'` (existing); `cluster_revisit` is reserved for same-ride round-trip. Phase F telemetry (B-24) joint-distributes (cluster_revisit, status, current_offer presence, ghost_id presence) for forensic disambiguation.

**1b.3 — Verification (`a68447b`).** Closes the verification gap on the topology check shipped at 1b.2. Single-file edit on `tests/test_where_am_i.py` (+420 lines). Test-only commit; no production code touches.

- **Block A — `TestComputeClusterRevisit` (12 pure-function unit tests):** Empty / single history (no-pair short-circuit); no prior_pudo within MIN_GAP (prior gate exclusion); near-both intermediate (Q3 GPS-multipath guard); Houston Loop classic (canonical positive case); both boundary cases probed independently (prior at exactly 200m → excluded via `>=` continue gate at line 827; intermediate at exactly 200m from both → included via `>=` inclusion gate at lines 840-841); chronological ordering (intermediate must be after prior); multiple-prior fan-out (first valid pair wins); tuple-key filter for active cluster (equality-by-content, not by `is`); A12: same coords, different `latest` → NOT filtered (Q4 ratification).

- **Block B — `TestEvaluateClusterRevisit` (7 integration tests via injected fakes):** No offer → `cluster_revisit` unconditionally False (Q2 ride-scoped); no cluster → `not_at_pudo` path → False; offer + degenerate history → False; offer + Houston Loop → True (full wiring confirmation); `accepted_at` forwarded as anchor (call-site capture); `at_unknown_pudo` path carries `cluster_revisit` (Step 6); `at_previous_pudo` path carries `cluster_revisit` (Step 5, ghost match).

- **Adjunct fix — `_FakeCursor.fetchall() → []`.** Direct REPL probe confirmed pre-1b.3 silent test pollution: existing `TestEvaluate` tests with non-None offer raised `AttributeError` inside `get_recent_clusters`, swallowed by its bare `except Exception`, returning `[]` → `cluster_revisit` defaulted to False → tests passed for the wrong reason. The fixture extension removes the false-negative path. Q1 ratified.

- **Existing test tightened (Q2 ratification):** `test_cluster_at_target_returns_at_current_pudo` now asserts `cluster_revisit is False` on the standard "first arrival" case, proving the wiring doesn't accidentally fire on default `_FakeCursor` inputs.

- **Boundary precision lesson surfaced (now L-9 corollary in lessons-learned section):** First implementation used 0.001797 deg lng for the 200m boundary based on a 111,320 m/deg desk approximation. Live `_haversine_meters()` (R=6371000) gives 199.82m for that offset, failing both A7 and A8 in opposite directions. Recalibrated to 0.001800 deg → 200.15m via REPL probe; both gates clear.

- **B7 setup correction:** ENROUTE state caused a false-positive `at_current_pudo` because `_target`'s default `named_roads` + `_fake_pivot`'s default `current_road` produced `breadcrumb_match` and `on_target_road` signals against `far_pickup`, firing the intersection matcher before Step 5 could run. Switched to UNCOMMITTED so `_match_current_pudo` returns None (no targets for UNCOMMITTED state), letting the ghost path fire.

- **Floor counts:** pytest 258 → 277 (+19 new tests; 18 added to count, 1 modified). Integration unchanged at 22/61 (test-only commit, no DB-coupled changes).

**Architectural rulings exercised across 1b:** L-2 (predict-then-verify on every gate), L-3 (anchor-based patch script — `apply_substep_1b2.py` 572 lines in-tree), L-5 (trailing-newline guard), L-6 (read production artifacts before authoring), L-6 corollary extended to method-invocation sites (1b.2 lesson, now in lessons-learned section), L-7 (cross-check architectural rulings), L-9 (fixture provenance declared), L-9 corollary added (live-haversine vs desk approximation, 1b.3 lesson, now in lessons-learned section), L-10 (CLUSTER_REVISIT_MIN_GAP_M cat-1 provenance), L-11 (this entry).

### Sub-step 1c — B-26 PLAN-side latch: the "Latch" of the Memory–Signal–Latch trilogy — CLOSED 2026-04-27

Single-commit ship at `72cf951`. Together with sub-step 1a (Memory: `get_recent_clusters()` + `Cluster.latest`) and sub-step 1b (Signal: `cluster_revisit` topology), closes the Memory–Signal–Latch trilogy of Step 6 sub-step 1. Lifts T79's test-time assertion to a runtime gate independent of WAI confidence: same-PUDO geometric colocation is now a structural refusal, not a heuristic.

**Implementation (`pudo_planner.py`).** Path A coordinate-only design ratified at session-open: B-15 (address-string equality) deferred to a future sub-step on grounds that geocoded coordinates are the canonical truth and string equality is fragile against geocoder normalization. The latch operates entirely in coordinate space.

- Constant: `PUDO_COLOCATION_THRESHOLD_M = 30.0` with L-10 cat-2 provenance comment (theoretical-with-shadow-mode; geocoder-noise floor expected to converge with B-24 telemetry from Phase F shadow runs). Module-level for tunability symmetry with `CLUSTER_REVISIT_MIN_GAP_M`.
- New pure helper: `_is_same_pudo_colocation(pickup_lat, pickup_lng, dropoff_lat, dropoff_lng)`. Returns True iff `haversine_meters(...) <= PUDO_COLOCATION_THRESHOLD_M`. Uses `<=` semantics (boundary inclusive).
- New builder: `_build_noop_same_pudo_no_revisit()` — the 12th action variant. Emits `PlannerDecision.action='noop_same_pudo_no_revisit'` for shadow-mode telemetry attribution (B-24 implication). Required `Literal[...]` expansion from 11 → 12 values in `pudo_types.py`; the contract introspection test bumped from `test_action_literal_has_eleven_values` → `_twelve_values`.
- Latch guard placed at dispatch level inside `consume()`, **not** as a builder parameter — sidesteps the L-6 corollary by avoiding any signature change to existing builders. Guard runs immediately before each `fire_dropoff` emission site: if `_is_same_pudo_colocation(...)` AND `cluster_revisit is not True`, emit `_build_noop_same_pudo_no_revisit()` instead.

**Cross-module rename.** `_haversine_meters → haversine_meters` in `where_am_i.py`. Promoted from private to public so `pudo_planner.py` can import it without reaching across the underscore boundary. 4 internal call sites in `where_am_i.py` updated; 4 sites in `tests/test_where_am_i.py` updated (1 import + 3 test calls). The test-side inventory was missed in the original apply and surfaced at apply-time as `pytest` collection `ImportError` — promoted to L-6 corollary extension SECOND STRIKE (now a hard CHECKLIST item; see lessons-learned section).

**Tests in `tests/test_pudo_planner.py` (+13 tests / +242 lines).**

- **`TestIsSamePudoColocation` (7 pure-function unit tests):** Identical coords → True; far apart (>1000m) → False; boundary triple at 29.5m / 30.0m / 30.5m → True / True / False respectively (sharp cutoff verified, `<=` semantics confirmed); typical Houston block (~80m) → False; antipodes → False (sanity); zero-distance Null Island → True. Boundary-triple coordinates derived via REPL probe of `haversine_meters` with 80-iteration binary search converging to `|d - 30.0| < 1e-10` per L-9 corollary refinement.

- **`TestSamePudoColocationLatch` (6 dispatch-integration tests):** Same-PUDO colocation + `cluster_revisit=False` → latch fires (12th action emitted); same-PUDO colocation + `cluster_revisit=True` → fire_dropoff permitted (Houston Loop case); different-PUDO (>30m) + `cluster_revisit` irrespective → fire_dropoff permitted; pickup-branch unaffected by latch (safety property — the latch must not contaminate pickup dispatch); state preservation when latch fires (no spurious side effects); decision-trace contains latch-firing rationale (forensic provenance).

**Floor counts:** pytest 277 → 290 (+13 new tests; 0 modified). Integration unchanged at 22/61 (test-only contract addition; no DB-coupled changes).

**Lessons surfaced.**
- **L-9 corollary refinement (80-iteration convergence):** First boundary fixture used 50-iteration binary search and gave `+2e-6` over the 30.0m threshold — a fixture that violated `<=` semantics. 80-iteration probe converged to `-1.6e-10`. Promoted: REPL probes for boundary fixtures must converge below floating-point precision noise floor (`< 1e-10`).
- **L-6 corollary extension SECOND STRIKE:** Rename of `_haversine_meters → haversine_meters` inventoried 4 internal sites correctly but missed 4 test sites. Promoted: any patch renaming a public/private symbol or modifying a method signature must `grep -rn "<symbol>" --include="*.py"` against the entire repo before applying — now a hard CHECKLIST item in L-3 anchor-script authoring, not a soft "should."

**Apply-script forensic record.** `apply_substep_1c.py` (14-patch L-3 anchor script) + `apply_substep_1c_fix.py` (rename-inventory recovery) live in the working tree as untracked files per L-3 backlog discretion.

**Architectural rulings exercised across 1c:** L-2 (predict-then-verify on every gate), L-3 (14-patch anchor-based apply script), L-5 (trailing-newline guard), L-6 (read production artifacts before authoring), L-6 corollary extension SECOND STRIKE (rename inventory generalization, now CHECKLIST), L-7 (cross-check architectural rulings), L-9 (fixture provenance declared in REPL-probe docstrings), L-9 corollary refinement (80-iteration convergence), L-10 (PUDO_COLOCATION_THRESHOLD_M cat-2 provenance), L-11 (this entry).

### Sub-step 2 — Five Pillars synthetic-heartbeat block (T70-T74) — NEXT

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

Five integration tests covering the round-trip dropoff class. The gate
(per R5 (rev) + Amendment 1):

```
PUDO cluster (cluster 1, formed within accepted_at - 60s window)
  → ≥200m intermediate cluster (cluster 2)
  → PUDO cluster (cluster 3)
  ⟹ WAI returns cluster_revisit=True
  ⟹ Planner fires fire_dropoff (gated by 1c same-address latch)
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
the legacy BMOAR Path B detector exhibits on same-address rides. The 1c
runtime latch defends against this independently of WAI confidence.

All T75-T79 fixtures are authored from first principles per L-9. Legacy
Scenario 14-15 coordinates (`29.5068, -95.41` / `29.5984, -95.62`) are
NOT inherited — they were artifacts of the state-machine poisoning bug
this rewrite retires. Q5 Null Island convention (cluster 1/3 at
`(0.0001, 0.0001)`, cluster 2 at `(0.005, 0.005)` ~600m offset)
applies unless Q6 re-verification surfaces a clean forensic candidate.

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

Per-ID classification of all 61 live IDs in `tests/test_integration.sh`
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
`Offer` dataclass. Per Amendment 1, also adds `accepted_at` end-to-end
plumbing if `Offer` doesn't already carry it (verified at sub-step 1b
authoring per L-6). Builds shadow-mode logging surface. Wires
`WAI_PLANNER_ENABLED_DRIVERS` flag. **Audits atomic-swap snapshot
assembly** (the "wrong pickup" failure class flagged at Step 2.5;
the contract for that audit is now firm because Section B's identity-
based detection only works if Phase F builds the snapshot correctly).

Phase F is also where shadow-mode telemetry for `cluster_revisit`
gates the round-trip detection in production data (B-24), and
validates `CLUSTER_HISTORY_PREROLL_SEC = 60` against real
offer-acceptance latency distributions.

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
| B-15 | Phase F: add `address` field to TargetSpec; populate target_address. | Phase F |
|      | Per Amendment 1, also verify `accepted_at` is on `Offer`; if not,    |        |
|      | extend B-15 to plumb it through (resolved at sub-step 1b per L-6)    |        |
| B-16 | Phase F: add `secondary_pickup` to Offer dataclass                   | Phase F |
| B-17 | Phase F: audit atomic-swap snapshot assembly                         | Phase F |
| B-18 | Phase G: T50/T60 legacy WatchdogB tests retire when BMOAR deprecates | Phase G |
| B-19 | Step 6: triage tests/test_integration.sh per REPLACE/RETIRE/PRESERVE | Step 6 sub-step 6 |
| B-20 | (closed) cumulative_miles to DriverStateSnapshot                     | CLOSED — replaced by R5 (rev) structural revisit gate |
| B-21 | (closed) cancel_candidate reachability                               | CLOSED — Dead Letter Office, fire_retroactive does the work |
| B-22 | `get_recent_clusters()` in `cluster_detection.py` with offer-anchored | Step 6 sub-step 1a |
|      | window per Amendment 1 (was: WAI per-driver cluster history with     |        |
|      | 200m spatial filter; supersedes pre-Amendment-1 framing)             |        |
| B-23 | Fast-pickup cluster minimum — investigate WAI 15s floor causing      | Phase F observability |
|      | pickup-nail failures on quick pickups (forensic, not gating)         |        |
| B-24 | Phase F shadow-mode logging of cluster_revisit_consideration events; | Phase F |
|      | also validates `CLUSTER_HISTORY_PREROLL_SEC = 60` against real       |        |
|      | offer-acceptance latency distributions per Amendment 1               |        |
| B-25 | Fast-errand floor evaluation — counter to B-24 if production shows   | Phase G |
|      | legitimate intermediate stops below WAI cluster floor                |        |
| B-26 | Same-address PLAN-side latch in `pudo_planner.py` (NEW per           | CLOSED — Sub-step 1c at 72cf951 |
|      | Amendment 1). Refuses fire_dropoff if pickup_address == dropoff_address |     |
|      | AND cluster_revisit IS NOT True. Lifts T79's test-time assertion to  |        |
|      | runtime gate. Must ship before T75-T79 integration tests             |        |
| B-27 | POI Adjacency Matrix + Cooperative Cache (NEW per 2026-04-27 Houston | Phase F sub-step (TBD) |
|      | field shift; surfaced 3 corner-lot S32 failures in single shift,     |        |
|      | including same-structure inversion proof Planet Fitness/Target).     |        |
|      | Inverts S32 from `current_road == target_road` (string equality) to  |        |
|      | `current_road IN adjacency_set` (membership), where adjacency_set is |        |
|      | computed once at offer-capture via Google Places viewport + Roads    |        |
|      | API nearestRoads. Four-layer cache architecture: per-offer / driver- |        |
|      | local persistent / fleet cooperative (mirrors community_offers       |        |
|      | pattern; positive feedback loop converts mapping gaps into           |        |
|      | proprietary fleet asset) / adjacency learning (shadow-mode telemetry |        |
|      | refines the cache). Cache key must be building-footprint not POI     |        |
|      | alone — Planet Fitness + Target shared structure proof. PREREQUISITE:|        |
|      | `routing.houston_ways` data integrity audit (see open question Q1).  |        |
|      | Forensic source: FORENSIC_2026_04_27_HOUSTON_SHIFT.md.               |        |

---

## Lessons learned (Phase E new entries)

L-2, L-3, L-5 (Phase D Steps 1-4) and L-6, L-7, L-8 (Phase D Step 5.6-5.7)
remain authoritative. L-4 (State-Machine Side-Effect Guard) is restored
in the institutional registry per Step 5 closeout consensus. The four
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

### L-9 corollary — Live haversine vs desk approximation (NEW, Phase E Step 6 sub-step 1b.3)

**Observation:** Sub-step 1b.3's first test fixture used 0.001797 deg lng for the 200m boundary case, derived from a 111,320 m/deg desk approximation (1/111320 ≈ 0.000008983 per meter; 200 × that ≈ 0.0017966). But `_haversine_meters()` in `where_am_i.py` uses R=6371000m and gives 199.82m for that offset — failing A7 (boundary excluded by `>=` continue gate) and A8 (boundary included by `>=` inclusion gate) in opposite directions. Recalibrated to 0.001800 deg → 200.15m via live REPL probe of `_haversine_meters`; both gates clear.

**Protocol change:** Fixture provenance comments (per L-9) must declare distances computed from the actual production helper, not from desk approximations. When a fixture targets a precise boundary value of a gate constant, the fixture math must invoke the same distance function the production code uses, captured via REPL probe and recorded in the fixture docstring.

**Generalization:** Any test fixture that probes a numerical boundary in production code must use the exact same numerical method (function, constants, precision) as the code under test. Approximations introduce false negatives and false positives that no amount of unit-test mock-cursor scaffolding can catch.

**Convergence requirement (sub-step 1c lesson):** REPL probes used to derive boundary-fixture values must converge to floating-point precision noise floor (`|haversine_meters - threshold| < 1e-10`). The 50-iteration binary search used in sub-step 1c authoring gave +2e-6 above threshold and produced a fixture that violated `<=` semantics; an 80-iteration probe converged to -1.6e-10 and the fixture passed. Going forward, boundary-fixture binary searches use 80 iterations as the default.

### L-6 corollary — Forensic count provenance (NEW, Phase E Step 6 sub-step 0.1)

**Parent lesson:** L-6 (Inspect production artifacts before authoring assertions) lives in PHASE_D_RETRO.md. This corollary extends L-6 to cover forensic counts asserted in design documents.

**Observation:** Step 4-era forensic read of `tests/test_integration.sh` undercounted live test IDs at 47. The 47 figure was asserted in PHASE_E_PROGRESS.md and PHASE_E_STEP_6_DESIGN.md, both ratified by Gemini 2026-04-26. Sub-step 0.1's live baseline run revealed the true count is 61 (matching the long-standing 22/61 figure that had been catalogued as "stale" in the design but was actually canonical). The design documents asserted the wrong count for ~12 hours.

**Protocol change:** Forensic counts asserted in design documents must be sourced from a live run or a binary-locked tool, not from a pattern-grepped read of source text. Pattern-grepping a 850-line shell script for ID strings produced a 23% undercount; the live run produced the truth in 57 seconds. For any count claim in a ratified design document: run the canonical script that produces the count, paste the output verbatim, cite the run timestamp.

### L-6 corollary extension — Method-invocation site inventory (NEW, Phase E Step 6 sub-step 1b.2)

**Promoted from memory entry #15 and the `6522ba5` commit body.**

**Observation:** Sub-step 1b.2 inventoried construction sites for the modified dataclasses (`Offer`, `WhereAmIResult`) but did NOT inventory invocation sites for the methods whose signatures were extended (`_match_ghost_cache`, `_at_unknown_pudo`, `_build_current_result`). Three `TestMatchGhostCache` tests then failed at apply-time with `TypeError` on the missing `cluster_revisit` positional argument. Live diagnosis was clean (no rollback required) but the gap should have been caught before the apply.

**Protocol change:** Any patch that adds a required parameter to a method signature must inventory ALL direct invocation sites of that method, not just the constructor sites of the types it produces or consumes. Extends the L-6 "read external `Cluster()` call sites before patching" rule from sub-step 1a to method-invocation sites.

**Verification gate (now part of L-3 anchor-script authoring):** Before applying any patch that modifies a method signature, grep the test suite for all direct invocation sites of that method name, list them in the patch script's docstring, and explicitly note whether each site is updated by the patch or already compatible.

**Second strike (sub-step 1c, 2026-04-27):** Rename of `_haversine_meters → haversine_meters` in `where_am_i.py` inventoried 4 internal call sites correctly but missed 4 sites in `tests/test_where_am_i.py` (1 import + 3 test calls). Caught at apply-time via pytest `ImportError`. Generalizes the protocol: any patch that renames a public/private symbol or modifies a method signature must `grep -rn "<symbol>" --include="*.py"` against the entire repo before applying, not just within the modifying module. This is now a hard CHECKLIST item in L-3 anchor-script authoring, not a soft "should.

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

**L-10 second application (2026-04-27 Amendment 1):** Step 6 sub-step 1a
design brief proposed `CLUSTER_HISTORY_LOOKBACK_SEC = 1800` (30 min)
as the cluster-history default lookback. Gemini identified this as
paranoia-class per L-10 — no production data grounded the 30-minute
number; it was "feels safe." The fix was structural rather than
empirical: replace the fixed window with an event-anchored one
(`offer_history.accepted_at + 60s pre-roll`). The 60s pre-roll itself
ships as L-10 category 2 (theoretical with Phase F shadow-mode
telemetry validation per B-24).

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

The cycle that has worked through 25 Phase E commits:

1. Claude proposes (one step at a time, with design discussion when
   warranted)
2. Andrew runs the proposal past Gemini (Grok occasionally)
3. Gemini ratifies or pushes back
4. If ratified, Claude implements with verification gates
5. Andrew runs the gates, pastes output
6. Commit + push immediately, never bundle multiple steps

**Verification gates per step (Phase E baseline):**
- pytest count preserved or increased (current floor: **250**)
- Integration baseline preserved (current floor: **22/61** per sub-step
  0.1 baseline 2026-04-26 23:46:19 UTC)
- Live-PG smoke when DB-coupled (per L-8) — none in Phase E so far;
  reactivates Step 7
- L-2 paranoia: `git status` before staging, after staging, after commit
- L-3: anchor-based patch scripts with sha-locked pre-conditions for
       any file >100 lines (heredoc regenerate acceptable for log
       files per Andrew's call at sub-step 0.3 closeout)
- L-5: trailing-newline guard
- L-6: read production artifacts before authoring assertions
- L-6 corollary: forensic counts in design docs sourced from live runs,
       not pattern-grepped reads
- L-7: cross-check architectural rulings against production conventions
- L-9: fixture provenance declared in docstring
- L-10: gate threshold provenance declared in code comment
- L-11: doc-currency check at session-open AND session-close

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
mechanism for surgical edits regardless of size. For full-file
regenerates of log/progress documents, file-based transfer via
`/mnt/user-data/outputs` + present_files + scp to VM is the streamlined
alternative.

---

## Concrete first-message-of-new-chat starter

> I'm resuming Phase E at Step 6 sub-step 2. Sub-step 1c closed at
> commit `72cf951` (B-26 PLAN-side latch — Memory–Signal–Latch trilogy
> complete; `_is_same_pudo_colocation` pure helper +
> `_build_noop_same_pudo_no_revisit` 12th action variant + dispatch-level
> latch guard; PUDO_COLOCATION_THRESHOLD_M=30.0 with L-10 cat-2 provenance;
> 13 new tests via TestIsSamePudoColocation 7 + TestSamePudoColocationLatch
> 6; 277 → 290 floor; integration unchanged at 22/61). HEAD is `72cf951`.
> Sub-step 1 (1a + 1b + 1c) is COMPLETE — Memory–Signal–Latch trilogy
> shipped end-to-end at the unit and direct-dispatch level.
>
> Floor: pytest 290/290, integration 22/61.
>
> Sub-step 2 ships the Five Pillars synthetic-heartbeat block (T70-T74) —
> the FIRST integration tests of the assembled `WAI.evaluate() →
> PudoPlanner.consume()` dispatch chain. Five tests, one per pillar
> (S31-S35), each feeding a synthetic heartbeat sequence to the assembled
> chain and asserting the planner emits the expected PlannerDecision:
>
> | Test | Pillar | Asserts                                                       |
> |------|--------|---------------------------------------------------------------|
> | T70  | S31    | Geometric — Forum Park 7623 fixture replay → fire_pickup      |
> | T71  | S32    | Structural — synth secondary pickup → fire_stacked_swap       |
> | T72  | S33    | Temporal — long stop then departure → fire_retroactive        |
> | T73  | S34    | Collapse — single-heartbeat fire (B-13 deferred)              |
> | T74  | S35    | Inverse — synth primary pickup STACKED → fire_stacked_revert  |
>
> T70 reuses the existing `tests/fixtures/7623_heartbeats.json`. T71-T74
> require new synthetic fixtures (no production capture available; Phase F
> shadow-mode adds forensic replay later). All fixtures declare provenance
> per L-9. T70-T74 are load-bearing — they prove the trilogy composes
> correctly at the integration level before Phase F wires it into
> `driver_heartbeat.py`.
>
> Single commit ship target. Must land before sub-step 3 (T75-T79
> Round-trip block, the "Houston Loop").
>
> **Sub-step 2 work outline (Gemini-ratifiable):**
>
> Block A — Synthetic fixtures in `tests/fixtures/`:
>   - T71-T74 each need a heartbeat JSON in the same shape as
>     `7623_heartbeats.json`. Per-fixture docstring declares provenance
>     per L-9 (synthetic, not production capture; Phase F shadow-mode
>     forensic replay deferred).
>   - Q1 below: per-test JSON files vs inline Python data structures.
>
> Block B — Tests in `tests/test_pudo_planner.py`:
>   - Dedicated `# Sub-step 2 — Five Pillars synthetic-heartbeat block`
>     section header for discoverability.
>   - One TestClass per pillar (TestT70Geometric, TestT71Structural, etc.)
>     OR one combined TestFivePillars class — Q3 below.
>   - Each test: load fixture → assemble WAI → drive heartbeats through
>     `WAI.evaluate() → PudoPlanner.consume()` → assert PlannerDecision.
>
> Floor target: 290 → 295 (5 new tests, no modifications). Single commit.
>
> **Open design questions for session-open (resolve before authoring):**
>
> 1. **Fixture file layout.** Per-test JSON files in `tests/fixtures/`
>    (matches 7623 precedent; discoverable via filesystem) vs inline
>    Python data structures in the test class (more readable;
>    docstring-friendly for L-9 provenance). Recommendation TBD at
>    session-open.
>
> 2. **Per-test heartbeat sequence length.** 7623 fixture's actual length
>    determines the parity target for T71-T74. Confirm at session-open
>    via `wc -l tests/fixtures/7623_heartbeats.json` and a structural
>    inspection. Likely 8-12 heartbeats per fixture to exercise
>    `N_HEARTBEATS_TO_FIRE=3` plus surrounding context.
>
> 3. **Test class organization.** Five separate classes
>    (TestT70Geometric, TestT71Structural, ...) for granular pytest -k
>    filtering, OR one TestFivePillars class with five methods for
>    discoverability under a single section header. Both viable;
>    recommendation TBD. Whichever choice, the T-number stays in
>    test names or comment annotations for cross-reference to
>    PHASE_E_PROGRESS.md.
>
> 4. **WAI cluster-history mock data per test.** T72 (Temporal — long
>    stop then departure) inherently needs cluster history. T70/T71/T73/T74
>    likely don't. Each fixture's docstring declares what cluster history
>    (if any) it injects via the `_FakeCursor` precedent established in
>    sub-step 1b.3.
>
> 5. **T73 (Collapse) scope.** B-13 (single-heartbeat fire) is deferred
>    to Phase F; T73 must therefore test the dispatch path WITHOUT
>    asserting B-13's behavior. Test could either (a) assert the planner
>    correctly delays the fire pending stable-match, or (b) be marked
>    xfail with B-13 reference. Recommendation TBD.
>
> Please read in order:
>   1. PHASE_E_PROGRESS.md (this file — sub-step 2 section + sub-step 1
>      SHIPPED sections for trilogy context)
>   2. PHASE_E_KICKOFF.md (architectural ground truth)
>   3. PHASE_E_STEP_6_DESIGN.md — Five Pillars taxonomy reference
>   4. `tests/fixtures/7623_heartbeats.json` — fixture shape precedent
>   5. `tests/test_pudo_planner.py` — existing dispatch-integration test
>      idioms (TestSamePudoColocationLatch from sub-step 1c is the most
>      recent precedent for assembled-chain testing)
>   6. `tests/test_where_am_i.py` — `_FakeCursor` cluster-history mock
>      pattern + TestEvaluate idioms
>   7. `where_am_i.py` — WAI.evaluate() interface for assembly point
>   8. `pudo_planner.py` — consume() and _decide() for the planner side
>      of the dispatch chain
>
> **Gates before authoring sub-step 2:**
>   - L-11 doc-currency check: HEAD must be at the doc-refresh commit
>     on top of `72cf951`, pytest 290, integration 22/61, working tree
>     clean except untracked patch scripts
>   - L-6: read `WAI.evaluate()` and `PudoPlanner.consume()` verbatim
>     before authoring any fixture or test scaffolding
>   - L-9: every synthetic fixture declares provenance in its docstring
>   - L-9 corollary refinement (1c lesson): if any test depends on a
>     numerical boundary, REPL-probe with 80-iteration convergence
>   - L-6 corollary extension SECOND STRIKE (1c lesson, now CHECKLIST):
>     if any test imports a renamed symbol, `grep -rn` the entire repo
>     before applying
>
> Same paired-programming protocol that worked through sub-steps 0/1a/1b/1c
> applies. Active verification gates: L-2 / L-3 / L-5 / L-6 / L-6 corollary /
> L-6 corollary extension SECOND STRIKE / L-7 / L-9 / L-9 corollary
> refinement / L-10 / L-11. L-8 reactivates at Step 7.
>
> Constraints unchanged: Reconcile dispatch (B-12) is Step 7. Phase F
> heartbeat-loop integration is gated on Phase E sub-steps 2-8 + Step 7
> complete; the trilogy is dormant in code until then. Phase F
> observability (B-23, B-24, B-25) deferred. B-27 (POI Adjacency Matrix +
> Cooperative Cache, NEW per 2026-04-27 Houston field shift) is a Phase F
> sub-step; see backlog table and `FORENSIC_2026_04_27_HOUSTON_SHIFT.md`.
