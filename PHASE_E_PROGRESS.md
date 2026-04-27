# Phase E Progress Brief

**For:** A fresh Claude conversation resuming Phase E at Step 6 sub-step 1a.
**Author:** Phase E Step 5 closing session (commit 49ea6c8).
**Date authored:** 2026-04-26.
**Updated:** 2026-04-27 — sub-step 0.3 + Amendment 1 closeout (commit 7a8616a).
**Replaces:** prior version at commit 7863b11 (post sub-step 0.3, pre-Amendment 1).

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
   amendment header explains the Phase D shipped state. Sub-step 1b ships
   the v2.6 amendment for `cluster_revisit`.

4. **`PHASE_E_STEP_6_DESIGN.md`** at the repo root — Step 6 design proposal,
   originally ratified by Gemini 2026-04-26, **amended 2026-04-27 with
   Amendment 1 (Offer-Anchor Lookback)**. Read the top-of-doc Amendment 1
   notice first, then the body, then the full Amendment 1 spec at the end.

5. **This file (`PHASE_E_PROGRESS.md`)** — captures Phase E's current
   state at end of sub-step 0.3 + Amendment 1. Use this as the entry point
   for sub-step 1a authoring.

6. **The commits since 49ea6c8** — `git log --oneline 49ea6c8..HEAD`
   covers Step 5 closeout + Step 6 design + Step 6 sub-step 0 + Amendment 1.

7. **`pudo_planner.py`** (1041 lines) and **`tests/test_pudo_planner.py`**
   (1650 lines) — the Phase E artifacts.

8. **`cluster_detection.py`** (200 lines) and **`tests/test_cluster_detection.py`**
   — the home for sub-step 1a's `get_recent_clusters()` primitive per
   Amendment 1.

9. **`where_am_i.py`** (1154 lines) and **`tests/test_where_am_i.py`** —
   the WAI consumer that sub-step 1b wires `cluster_revisit` into.

---

## Current state of the world

```
HEAD:                 7a8616a (Phase E Step 6 — Amendment 1 (Offer-Anchor Lookback) ratified)
Branch:               patch-00566a-unified-refinement (clean working tree, in lockstep with origin)
Tests pytest:         250/250 passing
Tests integration:    22/61 passing (39 failing) per sub-step 0.1 baseline 2026-04-26 23:46:19 UTC
Live-PG smoke:        4/4 from Phase D Step 5.7.2 (not re-run in Phase E; no DB-coupled changes shipped)
pudo_planner.py:      1041 lines, 4 sections (A/B/C/D), 11 builders, 68-test pytest suite
test_pudo_planner.py: 1650 lines, 68 tests across 6 sub-step blocks
cluster_detection.py: 200 lines (sub-step 1a target — get_recent_clusters() + Cluster.latest field)
where_am_i.py:        1154 lines (sub-step 1b target — cluster_revisit field)
```

Phase E Steps 1-5 shipped. Step 6 sub-step 0 shipped (0.1 baseline, 0.2 Group E
inventory closure, 0.3 WAI source-read finding). Step 6 design ratified, then
amended 2026-04-27 with Amendment 1 (Offer-Anchor Lookback) — the fixed
30-minute lookback for `get_recent_clusters()` was rejected as paranoia-class
per L-10 and replaced with an event-relative window pinned to
`offer_history.accepted_at` + 60s pre-roll. **Sub-step 1a is the next
concrete work** — authoring `get_recent_clusters()` per the offer-anchor
signature.

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
| 86ea117   | 6.0.1     | Integration baseline 22/61; "47 IDs"→61 correction; L-6 corollary |
| 5a86f2e   | 6.0.2     | Group E inventory closure (39/39 categorized)          |
| 7863b11   | 6.0.3     | WAI cluster-history source-read finding (stateless)    |
| 7a8616a   | 6.A1      | Step 6 Amendment 1 — Offer-Anchor Lookback (Gemini ratified) |

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

### Sub-step 1 — Contract amendments (NEXT SESSION's work)

Per Gemini Q2 ratification + sub-step 0.3 finding (2026-04-27) +
Amendment 1 (2026-04-27), sub-step 1 splits firm into 1a + 1b. WAI
confirmed stateless against cluster history; cluster-history primitive
lives in `cluster_detection.py` (Option A from sub-step 1a design
brief, Gemini-ratified). Sub-step 1c (NEW per Amendment 1) ships the
same-address PLAN-side latch.

- **1a (firm):** WAI cluster-history primitive `get_recent_clusters()` in
  `cluster_detection.py`. Per Amendment 1 signature:

  ```python
  def get_recent_clusters(
      driver_id: str,
      cur,
      accepted_at_anchor: datetime,
      preroll_sec: int = 60,
      min_samples: int = 3,
      max_speed_mph: float = 10.0,
      max_spread_m: float = 25.0,
  ) -> list[Cluster]:
  ```

  Window: `[accepted_at_anchor - preroll_sec, NOW()]`. SQL approach:
  gaps-and-islands extension of `detect_cluster()`'s `breaks_before = 0`
  pattern. `Cluster` dataclass extension: add `latest: datetime` field
  (data already computed in SQL as `MAX(logged_at)`; just needs to be
  exposed). Single commit. Test floor projected 250 → 254-258.

  **Q6 carries forward to 1a authoring** — re-verify against current
  data whether a clean production round-trip exists (yesterday's 14-day
  scan returned zero; two days of additional driving since). Per L-6
  corollary, the call is data-driven not memory-driven. Re-verification
  query is in SUBSTEP_1A_DESIGN_BRIEF (chat archive only) and reads
  `app_private.offer_history` joined with `app_private.decision_log`
  filtering for same-address-or-≤30m candidates with both `pickup_fired`
  and `dropoff_fired = true`. If zero, T75-T79 ships synthetic per L-9
  Null Island convention; if non-zero, evaluate per L-9 fixture
  provenance discipline.

- **1b (firm):** `WhereAmIResult.cluster_revisit: bool` field,
  `CLUSTER_REVISIT_MIN_GAP_M = 200` constant with refined L-10
  provenance (production-data-grounded structural noise floor),
  `CLUSTER_HISTORY_PREROLL_SEC = 60` constant in `cluster_detection.py`,
  v2.6 amendment to `WHERE_AM_I_PROPOSAL_v2.md`. Resolves Phase F
  `accepted_at` plumbing question per L-6 source-read at authoring time
  (does the `Offer` dataclass already carry `accepted_at`, or does
  Phase F need to add it like B-15 added `target_address`?). Single
  commit.

- **1c (NEW per Amendment 1):** Same-address PLAN-side latch in
  `pudo_planner.py` per B-26. Lifts T79's test-time assertion to a
  runtime gate independent of WAI confidence:

  ```
  IF pickup_address == dropoff_address (or coords <= 30m geocoder noise)
     AND cluster_revisit IS NOT True:
         REFUSE to emit fire_dropoff
         Hold state, await structural confirmation
  ```

  Sub-step assignment alternative: fold into early sub-step 2 if commit
  shape merits. Must ship before T75-T79 integration tests in sub-step
  3 (T79 tests this latch end-to-end).

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
| B-26 | Same-address PLAN-side latch in `pudo_planner.py` (NEW per           | Step 6 sub-step 1c |
|      | Amendment 1). Refuses fire_dropoff if pickup_address == dropoff_address |     |
|      | AND cluster_revisit IS NOT True. Lifts T79's test-time assertion to  |        |
|      | runtime gate. Must ship before T75-T79 integration tests             |        |

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

### L-6 corollary — Forensic count provenance (NEW, Phase E Step 6 sub-step 0.1)

**Parent lesson:** L-6 (Inspect production artifacts before authoring assertions) lives in PHASE_D_RETRO.md. This corollary extends L-6 to cover forensic counts asserted in design documents.

**Observation:** Step 4-era forensic read of `tests/test_integration.sh` undercounted live test IDs at 47. The 47 figure was asserted in PHASE_E_PROGRESS.md and PHASE_E_STEP_6_DESIGN.md, both ratified by Gemini 2026-04-26. Sub-step 0.1's live baseline run revealed the true count is 61 (matching the long-standing 22/61 figure that had been catalogued as "stale" in the design but was actually canonical). The design documents asserted the wrong count for ~12 hours.

**Protocol change:** Forensic counts asserted in design documents must be sourced from a live run or a binary-locked tool, not from a pattern-grepped read of source text. Pattern-grepping a 850-line shell script for ID strings produced a 23% undercount; the live run produced the truth in 57 seconds. For any count claim in a ratified design document: run the canonical script that produces the count, paste the output verbatim, cite the run timestamp.

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

> I'm resuming Phase E at Step 6 sub-step 1a. Sub-step 0 closed at
> commit 7863b11 (WAI source-read finding: WAI is stateless against
> cluster history). Step 6 Amendment 1 (Offer-Anchor Lookback) shipped
> at commit 7a8616a. HEAD is 7a8616a. Floor: pytest 250/250,
> integration 22/61.
>
> Sub-step 1a authors `get_recent_clusters()` in `cluster_detection.py`
> per Amendment 1's offer-anchored signature:
>
>     def get_recent_clusters(
>         driver_id: str, cur,
>         accepted_at_anchor: datetime,
>         preroll_sec: int = 60,
>         min_samples: int = 3,
>         max_speed_mph: float = 10.0,
>         max_spread_m: float = 25.0,
>     ) -> list[Cluster]
>
> Window: [accepted_at_anchor - preroll_sec, NOW()]. SQL approach:
> gaps-and-islands extension of detect_cluster()'s breaks_before=0
> pattern. Cluster dataclass extension: add `latest: datetime`. Test
> floor projected 250 → 254-258. Single commit.
>
> Please read in order:
>   1. PHASE_E_PROGRESS.md (this file)
>   2. PHASE_E_KICKOFF.md (architectural ground truth)
>   3. PHASE_E_STEP_6_DESIGN.md — read the top-of-doc Amendment 1
>      notice, then the body, then the full Amendment 1 spec at end
>   4. cluster_detection.py (200 lines, target file)
>   5. tests/test_cluster_detection.py
>   6. WHERE_AM_I_PROPOSAL_v2.md (planned v2.6 amendment ships in 1b)
>
> **Gates before authoring 1a:**
>   - L-11 doc-currency check: HEAD must be 7a8616a, pytest 250, working
>     tree clean
>   - Q6 re-verification query: run the same-address forensic search
>     from sub-step 1a design brief against current data. If zero
>     candidates, T75-T79 ships synthetic per L-9 Null Island
>     convention. If non-zero, evaluate per L-9 fixture provenance.
>     Yesterday's 14-day search returned zero clean candidates (Manvel
>     test reruns, Transco OCR corruption, Cunningham never-engaged) —
>     two days additional driving since.
>
> Same paired-programming protocol that worked through 25 Phase E
> commits applies. Active verification gates: L-2 / L-3 / L-5 / L-6 /
> L-6 corollary / L-7 / L-9 / L-10 / L-11. L-8 reactivates at Step 7.
>
> Constraints: Reconcile dispatch (B-12) is Step 7. Same-address PLAN-
> side latch (B-26) is sub-step 1c (or folded into early sub-step 2).
> Phase F observability (B-23, B-24, B-25) deferred.
