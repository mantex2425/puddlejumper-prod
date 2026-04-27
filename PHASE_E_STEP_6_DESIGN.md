# Phase E Step 6 — Integration Bridge: Design Proposal

**For:** Gemini ratification before Step 6 implementation begins.
**Author:** Phase E Step 5 closing session (commit 49ea6c8).
**Date:** 2026-04-26.
**Status:** Design draft. Implementation begins after Gemini ratification.

**Amended:** 2026-04-27 — see "AMENDMENT 1 (2026-04-27) — Offer-Anchor
Lookback (full spec)" at end of document. Original design ratified
2026-04-26 remains in effect except as superseded by the amendment.

---

## AMENDMENT 1 (2026-04-27) — Offer-Anchor Lookback (notice)

Gemini ratified an offer-anchor refinement to sub-step 1a's lookback
window during the sub-step 0.3 closeout session (post-commit `7863b11`).
The fixed `CLUSTER_HISTORY_LOOKBACK_SEC = 1800` constant proposed in the
sub-step 1a design brief is replaced by an event-relative window pinned
to `offer_history.accepted_at` with a 60-second pre-roll buffer. This
eliminates the "is N minutes the right number?" question entirely and
removes Python/Postgres clock-drift risk from the lookback boundary.

**Sections of this document superseded by Amendment 1:**
- Sub-step 1a's `lookback_sec` parameter (replaced by `accepted_at_anchor`
  + `preroll_sec`)
- Q3 lookback-window ratification (A3 fixed-window → event-anchored)
- Sub-step 1a "in `where_am_i.py`" location reference (now
  `cluster_detection.py`, per sub-step 0.3 finding + sub-step 1a design)
- "CONDITIONAL on 0.3" framing for sub-step 1a (resolved to firm by
  sub-step 0.3 finding, commit `7863b11`)
- Architectural ruling R5 (refined to R5 (rev) — see Amendment 1 full spec)

**Sections NOT changed by Amendment 1:**
- `CLUSTER_REVISIT_MIN_GAP_M = 200` (value unchanged; provenance
  comment refined per Gemini's "Structural Noise Floor" framing)
- All sub-step ordering (1a → 1b → 2 → 3 → 4 → 5 → 6a → 6b → 7 → 8)
- Q5 Null Island fixture convention for T75-T79
- T70-T74 Five Pillars block specs
- T75-T79 round-trip test gate logic (the 3-cluster topology is
  unchanged; only the window source for cluster history changed)
- Legacy triage rubric (PRESERVE/PRESERVE-ASSERTION/REPLACE/RETIRE/CASCADE)

**New backlog item:** B-26 — same-address PLAN-side latch (see Amendment 1
full spec). Sub-step assignment: between 1b and 2 (likely 1c or folded
into early sub-step 2).

Read the full Amendment 1 spec at end of document before authoring 1a.

---

## Why this document exists

Phase E Step 5 closed the unit-test suite for `pudo_planner.py` at commit
49ea6c8 (250 tests, 68 added). Step 6 is the **integration bridge** —
synthetic-heartbeat tests that exercise the assembled `WAI.evaluate() →
PudoPlanner.consume()` pipeline against scenario classes the unit-test
suite cannot cover.

Step 6 is the largest remaining Phase E step. It will span 2-3 sessions.
This document is the design that locks before implementation begins, per
the paired-programming protocol that has carried Phase E through 18 commits.

---

## What Step 6 ships

1. **Three contract amendments**, all locked from the Step 5 closeout
   design discussion:
   - `WhereAmIResult.cluster_revisit: bool` (new field)
   - `where_am_i.py` per-driver cluster history with 200m spatial filter
   - **No changes** to `DriverStateSnapshot` (B-20 closes per R5)

2. **Five integration test blocks** authored over 2-3 sessions:
   - **T70-T74:** Five Pillars synthetic-heartbeat replay
   - **T75-T79:** Round-trip ("Houston Loop") block
   - **T80-T89:** STACKED-via-WAI block (replaces legacy T50/T60)
   - **T90-T99:** Reconcile reservation (placeholders only; dispatch is Step 7)
   - **Two safety tests:** Double-fire safety + T79 single-cluster guard

3. **Legacy triage** of all 61 live IDs in `tests/test_integration.sh`
   per the REPLACE/RETIRE/PRESERVE/CASCADE/PRESERVE-ASSERTION rubric.

4. **One micro-commit** (UTC anchor patch on T16/T30/T38) BEFORE Step 6
   sub-step 0 takes the integration-suite baseline.

5. **Group E closure** — the long-standing `tests/TEST_SUITE_STATUS.md`
   TODO that was never inventoried (Phase C deploy 2026-04-25).

6. **Doc handoff refresh** at end of Step 6, per L-11.

---

## What Step 6 does NOT ship

- **Reconcile dispatch (B-12).** Step 7 work. The dispatch path that emits
  `reconcile_missed_pickup` and `reconcile_missed_dropoff` is not
  implemented in Step 6. T90-T99 are reserved as placeholders only.
- **Phase F shadow-mode telemetry.** B-22, B-24, B-25 are Phase F
  observability work, not Step 6.
- **Live-PG smoke.** Step 7 work (L-8 reactivation).
- **Phase G BMOAR Path A/B deprecation.** Cannot ship before shadow-mode
  telemetry validates WAI/PLAN equivalence.

---

## Architectural ruling locked at Step 6 design

### R5 — Structural revisit over odometer delta

Round-trip detection (pickup address == dropoff address, driver returns
to PUDO after intermediate destination) uses **topological evidence**,
not odometer delta or duration thresholds.

**The gate:**

```
Cluster 1 forms at PUDO coords (the pickup, fires fire_pickup)
  ↓
Cluster 2 forms at coords ≥200m from cluster 1 center (the actual destination)
  ↓
Cluster 3 forms at PUDO coords (within geocoder noise of cluster 1)
  ⟹ WAI marks cluster_revisit=True on the cluster-3 result
  ⟹ Planner emits fire_dropoff (target_state=UNCOMMITTED)
```

**The three signals all required:**
1. Cluster 1 confirmed (existing WAI cluster minimum, 15s).
2. Cluster 2 ≥200m spatial separation from cluster 1 center.
3. Cluster 3 returns to within geocoder noise of cluster 1 (existing
   WAI matching logic).

**No duration floor on cluster 2.** Empirically grounded after design
discussion: a duration floor would reject legitimate fast-errand
round-trips (passenger leans out window for a 25-second package
handoff) without meaningfully reducing the false-positive surface area
(GPS multipath teleports tend to bounce, not cluster stably). Phase F
shadow-mode telemetry (B-24) measures the actual false-positive rate
and informs whether a floor is needed in a follow-up amendment.

**No odometer dependency.** The intermediate cluster IS the proof the
round-trip happened, regardless of distance. Same-block round-trips
(driver to corner store) and cross-town round-trips both fire on the
same gate. B-20 closes — `DriverStateSnapshot` does not gain
`cumulative_miles`.

**Provenance per L-10:** the `CLUSTER_REVISIT_MIN_GAP_M = 200` constant
is **production-data-grounded.** Houston GPS cluster behavior has been
forensically characterized (Forum Park 7623 case, ongoing operational
data). 200m exceeds GPS noise (~30-50m typical, ~100-150m worst-case
urban canyon) by a comfortable margin, while remaining tight enough
that any actual destination registers as "elsewhere."

---

## Pre-Step-6: UTC Anchor Patch (micro-commit)

`tests/test_integration.sh` contains three legacy `AT TIME ZONE
'America/Chicago'` violations:

- **T16** (Scenario 5, ABORT Guard): `UPDATE app_private.driver_trip_state
  SET state_updated_at = NOW() AT TIME ZONE 'America/Chicago' - INTERVAL
  '90 seconds'`
- **T30** (Scenario 9, Watchdog Auto-Reset): same pattern, 95-minute
  backdate
- **T38** (Scenario 12, ABORT without pickup nail): same pattern, 90-second
  backdate

These predate the rev `00491-mbd` UTC migration sprint. They violate the
canonical UTC rule. Per Gemini Step 6 design ratification: "go with the
micro-step anchor patch... if a timing-related bug pops up in Step 6,
you know it's logic-driven, not timezone-drift driven."

**Implementation:** L-3 anchor-based Python patch script with three
`(old, new)` tuples and SHA-locked precondition. Single commit on its
own. Lands BEFORE Step 6 sub-step 0 takes the integration baseline so
the baseline reflects a UTC-canonical suite.

**Verification gates (the patch micro-commit):**
- Pre-condition SHA on `tests/test_integration.sh` matches commit 49ea6c8 baseline
- Patch script runs `--dry-run` first, `--apply` second
- All three anchors match (script aborts hard if any miss)
- Post-patch SHA recorded in commit body
- `bash tests/test_integration.sh 2>&1 | tail -3` runs to completion
  (the patch shouldn't change pass/fail counts; we're fixing test SQL,
  not test logic)

---

## Step 6 sub-step plan (2-3 sessions)

### Sub-step 0 — Baseline + inventory + WAI source-read (Session 1)

Three deliverables, one commit at the end (or three commits if any
deliverable surfaces something requiring its own commit).

**0.1 Baseline run.**
```
bash tests/test_integration.sh 2>&1 | tee /tmp/integration_baseline.txt
grep -c "❌ FAIL" /tmp/integration_baseline.txt
grep -c "✅ PASS" /tmp/integration_baseline.txt
```
Output recorded in commit body. The PHASE_E_PROGRESS.md "Current state of
the world" block updates to reflect the live count (replaces the legacy
22/61 figure).

**0.2 Group E inventory.** Close the long-standing TODO in
`tests/TEST_SUITE_STATUS.md`. Categorize all T01-T42 failures into
existing groups A-D or new groups as needed. Updates TEST_SUITE_STATUS.md.

**0.3 `where_am_i.py` cluster-history read.** Read the file. Determine:
- Does WAI today track per-driver cluster history, or is each
  `evaluate()` call stateless against cluster history?
- If stateful: where does the history live, what are its eviction
  semantics, can it be extended for `cluster_revisit` detection?
- If stateless: what is the smallest amendment to make it stateful for
  this use case?

The finding determines whether sub-step 1's amendment is small (extend
existing tracking) or meaningful (build cluster-history primitive). The
finding is recorded in sub-step 0's commit body.

**Verification gates (sub-step 0):**
- Baseline run completes (live ❌/✅ counts recorded)
- Group E inventory captured (61 - already-categorized = new
  classifications)
- `where_am_i.py` finding documented in commit body with file:line
  references
- TEST_SUITE_STATUS.md updated
- PHASE_E_PROGRESS.md updated with live integration count
- L-2/L-3/L-5/L-11 gates: clean

---

### Sub-step 1 — Contract amendments (Session 1 or 2)

Per Gemini Q2 ratification, sub-step 1 is **conditionally split** based
on sub-step 0.3's WAI source-read finding:

- **If WAI is already stateful enough to support `cluster_revisit`
  detection** (existing cluster history, existing per-driver tracking):
  sub-step 1 ships as a single commit (1b only).
- **If WAI is currently stateless against cluster history**: sub-step 1
  splits into **1a (Primitive)** and **1b (Contract)**, two commits.

#### Sub-step 1a — WAI cluster-history primitive (CONDITIONAL on 0.3)

Authors per-driver cluster history in `where_am_i.py`. Eviction semantics,
storage shape, integration with the existing `evaluate()` flow. Single
commit. No contract surface change yet — this is internal infrastructure.

**Verification gates (sub-step 1a):**
- pytest count preserved (existing WAI tests pass against extended
  internals; no new tests yet — 1b adds them)
- New WAI tests for cluster-history primitive (~3-5 tests typical)
- L-2/L-3/L-5/L-7 gates: clean (L-7 specifically: cross-check that
  WAI's existing cluster-tracking conventions are honored, not
  reinvented)

#### Sub-step 1b — Contract amendment (UNCONDITIONAL)

**1b.1 `WhereAmIResult.cluster_revisit: bool` field.** Default False.
Tests in `tests/test_where_am_i.py` extend to assert default and
exercise the field.

**1b.2 200m spatial filter constant.** `CLUSTER_REVISIT_MIN_GAP_M = 200`
declared in WAI module with provenance comment per L-10:
```python
# Provenance: production-data-grounded. Houston GPS cluster behavior
# forensically characterized via Forum Park 7623 case + operational
# data 2026-04-26. 200m exceeds GPS noise (~30-50m typical, ~100-150m
# worst-case urban canyon) by margin sufficient to filter cluster
# wobble while admitting any genuine intermediate destination.
CLUSTER_REVISIT_MIN_GAP_M = 200
```

**1b.3 v2.6 amendment to WHERE_AM_I_PROPOSAL_v2.md.** Documents the
`cluster_revisit` field and its gate semantics. Documentation only;
the code change ships in 1b.1 and 1b.2.

**`DriverStateSnapshot` does NOT change.** B-20 closed.

**Verification gates (sub-step 1b):**
- pytest count preserved or increased (floor: 250 + 1a tests + 1b tests)
- All WAI smoke tests still pass (Phase D's 4/4 live-PG smoke if
  re-run; not required, but recommended if cluster-history changes
  touch DB queries)
- L-9: every new test fixture declares provenance
- L-10: every new constant declares provenance (200m gate)
- L-2/L-3/L-5/L-7 gates: clean

---

### Sub-step 2 — Five Pillars synthetic-heartbeat block T70-T74 (Session 2)

Five integration tests in a new file `tests/test_planner_integration.py`.
Each test feeds a synthetic heartbeat sequence to assembled
`WAI.evaluate() → PudoPlanner.consume()` and asserts the planner emits
the expected PlannerDecision.

| Test | Pillar | Source                    | Asserts                                              |
|------|--------|---------------------------|------------------------------------------------------|
| T70  | S31    | tests/fixtures/7623_heartbeats.json | Forum Park geometric fire → fire_pickup       |
| T71  | S32    | New synthetic fixture     | STACKED secondary pickup match → fire_stacked_swap   |
| T72  | S33    | New synthetic fixture     | Long stop then departure → fire_retroactive          |
| T73  | S34    | New synthetic fixture     | Single-heartbeat fire (B-13 deferred for double-tap) |
| T74  | S35    | New synthetic fixture     | STACKED primary pickup match → fire_stacked_revert   |

**T70 uses production fixture.** T71-T74 use synthetic fixtures authored
from first principles per L-9. Each fixture's docstring declares:
- The pillar it exercises
- The exact heartbeat sequence (timestamps, coords, status)
- The expected PlannerDecision and why

**Verification gates (sub-step 2):**
- pytest count preserved or increased (5 new tests)
- All five tests pass with the assembled WAI → PLAN pipeline
- L-9 check: every fixture's docstring declares provenance
- L-2/L-3/L-5 gates: clean

---

### Sub-step 3 — Round-trip block T75-T79 (Session 2 or 3)

Five integration tests for the Houston Loop. Authored from first
principles per L-9 — no inheritance from legacy Scenarios 14-15.

```
Setup for T75-T79:
  pickup_address  = "Manvel example: same-address round-trip"
  dropoff_address = (same as pickup_address)
  pickup_lat/lng  = pickup pin coords
  dropoff_lat/lng = (within geocoder noise of pickup_lat/lng, ≤30m)
```

| Test | Asserts                                                                  |
|------|--------------------------------------------------------------------------|
| T75  | Pickup fires normally at PUDO. Same as Forum Park except same-address.   |
| T76  | Outbound leg — driver leaves PUDO. Planner emits noop. cluster_revisit=False. |
| T77  | Return leg — cluster 3 forms at PUDO. cluster_revisit=True. fire_dropoff. |
| T78  | Houston Drift sentinel — fire_dropoff coords from cluster 3 median, not cluster 1. |
| T79  | Single-cluster guard — driver never leaves PUDO. cluster_revisit=False. NO fire_dropoff. |

**T79 is the load-bearing safety test of the round-trip block.** If T79
fails, the system fires `fire_dropoff` at the pickup before the driver
has actually left, writing a corrupt audit row. T79 is also re-emphasized
as a standalone safety-test commit in sub-step 7.

**Synthetic heartbeat sequence templates** (sketched, locked in commit;
coordinates per Gemini Q5 Null Island convention):

T75 — three stable hits at PUDO `(0.0001, 0.0001)`, 5s spacing → fire_pickup.

T76 — three hits at PUDO (state IN_TRIP after T75 fires), then four hits
at intermediate destination `(0.005, 0.005)` (~600m offset, well past
the 200m gate), 5s spacing. Asserts noop throughout.

T77 — T76's sequence plus three hits back at PUDO `(0.0001, 0.0001)`. WAI
assertion: cluster_revisit=True on the third return-leg hit. Planner:
fire_dropoff.

T78 — T77's sequence with the return-leg coords offset 25m from cluster
1 (within geocoder noise: cluster 3 at `(0.00033, 0.00033)`). Asserts
fire_dropoff.corrected_lat/lng comes from the return-leg cluster median,
not the pickup-fire coords.

T79 — eight stable hits at PUDO `(0.0001, 0.0001)`, 5s spacing. State
stays IN_TRIP (T75 fires on hit 3). cluster_revisit must remain False.
Planner emits arm or noop, never fire_dropoff.

**Fixture provenance declaration (L-9, mandatory in every T75-T79 test
class docstring):**

```
Coordinates intentionally near Null Island. These are synthetic
specification fixtures per L-9, not forensic replays. If any reader
recognizes these as a real Houston street, the test has been
corrupted. The pickup/dropoff offset (cluster 3 vs cluster 1) is
~25m to exercise the geocoder-noise tolerance; the intermediate
cluster offset is ~600m to clear the 200m CLUSTER_REVISIT_MIN_GAP_M
threshold per L-10.
```

**Verification gates (sub-step 3):**
- pytest count preserved or increased (5 new tests)
- T79 explicitly asserted to FAIL on a control build (with
  cluster_revisit forced True), proving the test bites
- L-9 check: round-trip fixtures declare provenance as
  "authored from first principles per Step 6 design 2026-04-26;
  intentionally NOT inherited from legacy Scenarios 14-15 which were
  poisoned-state-machine forensic artifacts"
- L-10 check: 200m constant referenced from sub-step 1
- L-2/L-3/L-5 gates: clean

---

### Sub-step 4 — STACKED-via-WAI block T80-T89 (Session 2 or 3)

Replaces the legacy T50/T60 STACKED scaffold. Ten test slots reserved
for breathing room per Gemini ratification.

| Test    | Replaces / Asserts                                                |
|---------|-------------------------------------------------------------------|
| T80     | fire_stacked_swap end-to-end (replaces T50d)                      |
| T81     | fire_stacked_revert end-to-end (no legacy analog)                 |
| T82     | Primary audit not poisoned (replaces T50e — PRESERVE-ASSERTION)   |
| T83     | current_offer_id swaps to secondary (replaces T50f — PRESERVE-ASSERTION) |
| T84     | Secondary cancellation while STACKED (replaces T60a-d)            |
| T85     | Guard: not_at_pudo while STACKED → fall through                   |
| T86     | Guard: secondary dropoff before secondary pickup → silent fall-through (B-14 placeholder) |
| T87-T89 | Reserved for STACKED forensic variations as production data surfaces |

T82 and T83 are the **PRESERVE-ASSERTION** dispositions: legacy T50e/T50f
made correct assertions but the path that satisfied them changed. T82/T83
re-state the same assertions against the new path (fire_stacked_swap
reconciliation_payload + EXECUTE atomic-swap snapshot).

**Verification gates (sub-step 4):**
- pytest count preserved or increased (~7 new tests, T87-T89 reserved)
- T80/T81 assert PlannerDecision shape AND the EXECUTE-layer effect
  (audit row written, current_offer_id swapped) — these are the bridge
  tests that prove Phase F's atomic-swap implementation matches the
  contract Section B locked in Step 4
- L-9 check: STACKED fixtures declare provenance
- L-2/L-3/L-5 gates: clean

---

### Sub-step 5 — Reconcile reservation T90-T99 (Session 3)

Reserve T90-T99 as placeholders. No tests authored in Step 6. The
reservation is a one-line stub in `tests/test_planner_integration.py`:

```python
# T90-T99 reserved for B-12 reconcile dispatch tests.
# Implementation: Step 7. Do not author until reconcile dispatch ships.
```

**Verification gates (sub-step 5):**
- The stub commits cleanly
- No test count change
- PHASE_E_PROGRESS.md updated to reflect reservation

---

### Sub-step 6a — Legacy triage classification (Session 3)

Per-ID classification of all 61 live IDs in `tests/test_integration.sh`
against the sub-step 0 baseline. Output: a new section in
`tests/TEST_SUITE_STATUS.md` with the per-ID classification table.

**This sub-step ships ZERO deletions.** Classification only. RETIRE
deletions land in sub-step 6b per Gemini Q4 ratification — keeping
the triage logic auditable in isolation in `git log`.

**Triage rubric:**

| Disposition           | Meaning                                                       | Action |
|-----------------------|---------------------------------------------------------------|--------|
| **PRESERVE**          | Test asserts unchanged contract; current code path satisfies. | Keep test as-is. |
| **PRESERVE-ASSERTION**| Test text unchanged; new code path satisfies.                 | Keep test as-is. New T7x-T8x tests cover the new path; this test remains as historical contract validation. |
| **REPLACE**           | Behavior carries forward; new T7x-T8x test authored.          | New test in test_planner_integration.py. Legacy test deleted from test_integration.sh when Phase F shadow-mode validates new path. |
| **RETIRE**            | Test was forensic artifact of a bug now eliminated.           | Delete legacy test outright. No successor. Reasoning recorded in commit body per L-9. |
| **CASCADE**           | Will pass automatically when its parent REPLACE clears.       | No work. Verify post-Phase-F. |

**Pre-classified examples** (full triage in sub-step 6 commit; the
inventory below is the design-time prediction Gemini ratifies):

| Disposition             | IDs (pre-classified)                                       |
|-------------------------|------------------------------------------------------------|
| PRESERVE                | T01, T02, T15, T40, T41, T42, T17, T39, T30, T31, T27, T28, T11, T10 (decision endpoint, manual reset, ABORT guard, Watchdog A, idempotency, S04 implicit cancel) |
| PRESERVE-ASSERTION      | T50e, T50f (replaced by T82, T83)                         |
| REPLACE                 | T03, T04, T05, T06, T06b, T07, T08, T09, T09b, T13, T13b, T13c, T18, T19, T20, T21, T22, T23, T24, T25, T26, T32, T33, T34, T34b, T35, T50a, T50b, T50c, T50d, T60a, T60b, T60c, T60d (heartbeat-driven state transitions, S11 override, full-stacked-trip, no-show, atomic-swap behavior) |
| RETIRE                  | T44, T45, T46, T47, T48, T49 (poisoned-state-machine forensic artifacts per L-9; legacy "divergent geocode" framing was forensic mis-attribution of cross-ride state contamination) |
| CASCADE                 | T48 alternative classification — currently catalogued under RETIRE because its parent T47 is RETIRE-not-REPLACE |
| EMPIRICAL DECISION POINT (resolves to REPLACE or RETIRE) | none — round-trip class moves entirely to T75-T79 + RETIRE on legacy IDs |

**Note on T44-T49 RETIRE.** These six IDs lose their successor. The
round-trip class is covered by T75-T79 authored from first principles.
The retro should record (per L-9) that the legacy coordinates were
artifacts of a state-machine bug, not specifications. Future maintainers
reading `git log` will see the rationale.

**Verification gates (sub-step 6a):**
- All 61 IDs classified
- TEST_SUITE_STATUS.md updated with the full per-ID table
- **No deletions in this commit** — triage decision only
- L-9 check: RETIRE rationale recorded for T44-T49 in TEST_SUITE_STATUS.md
  (the rationale informs sub-step 6b's deletion commit body)
- L-2/L-3/L-5/L-11 gates: clean

---

### Sub-step 6b — RETIRE structural cleanup (Session 3)

Delete legacy tests classified RETIRE in sub-step 6a from
`tests/test_integration.sh`. Per Gemini Q4 ratification: triage decisions
and structural actions ship as separate commits for forensic clarity.

**Pre-classified RETIRE deletions** (locked in sub-step 6a, executed here):
- T44 — Nail pickup at circular origin → IN_TRIP
- T45 — Heartbeat near pickup/dropoff pin (1.5mi driven) → REFINE_DROPOFF armed
- T46 — Stopped at circular dropoff → stays REFINE_DROPOFF
- T47 — Round-trip setup: IN_TRIP after pickup nail
- T48 — Round-trip gate holds at 0.5mi (cascade from T47)
- T49 — Round-trip gate clears at 1.1mi → REFINE_DROPOFF armed

Plus any additional RETIRE classifications surfaced by Group E inventory.

**Implementation:** L-3 anchor-based Python patch script. Each test's
deletion is a separate `(old, new)` tuple where `new=""` (deletion).
SHA-locked precondition.

**Verification gates (sub-step 6b):**
- Legacy test count reduced by exact RETIRE count
- L-9 check: commit body documents each RETIRE'd ID with rationale
  ("poisoned-state-machine forensic artifact, see TEST_SUITE_STATUS.md
  per L-9 fixture provenance")
- `bash tests/test_integration.sh 2>&1 | tail -3` runs to completion
  (tests fewer in count; ❌/✅ count must match expectation that drops
  the previously-failing RETIRE'd tests)
- L-2/L-3/L-5 gates: clean

---

### Sub-step 7 — Two safety tests as standalone commits (Session 3)

**7.1 Double-fire safety test.** New test in
`tests/test_planner_integration.py`. Sequence: arm → fire_pickup →
heartbeat at same coords → assert NO re-fire. Per Gemini Step 6 design
ratification: "the load-bearing bridge of Phase F."

If this test fails, Phase F's heartbeat-loop integration must clear
temporal state post-fire. The Step 5.6 unit suite admitted this gap:
`TestStateStore::test_state_store_drivers_isolated` ends with the
comment that consume() does NOT clear temporal state on a fire decision,
leaving driver_A at count=3 after fire_pickup. The integration-layer
test forces the question.

**7.2 T79 single-cluster safety test.** Already in sub-step 3 but
re-emphasized as a standalone commit for forensic clarity. The two
safety tests together fence the architecture from both the post-fire
and pre-revisit failure modes.

**Verification gates (sub-step 7):**
- Both tests pass on the assembled pipeline
- Both tests verifiably fail on adversarial control builds (no-clear and
  cluster_revisit-forced-True)
- L-2/L-3/L-5 gates: clean

---

### Sub-step 8 — Closeout doc refresh + Step 7 prep (Session 3)

Final commit of Step 6. Per L-11 session-bookend protocol.

- PHASE_E_PROGRESS.md refreshed with Step 6 closure
- Step 7 scope documented (live-PG smoke + B-12 reconcile dispatch)
- Next-session-starter block written for Step 7 resume
- Backlog updated (B-19 closes; B-22 closes if shipped in sub-step 1)
- New Phase F backlog items folded in if any surfaced during Step 6

**Verification gates (sub-step 8):**
- PHASE_E_PROGRESS.md "Current state of the world" matches HEAD
- All Step 6 sub-steps reflected in commit lineage table
- Backlog status accurate
- L-11 doc-currency refresh: clean

---

## Test count projection

| Sub-step | Tests added                              | Cumulative floor |
|----------|------------------------------------------|------------------|
| Pre-Step-6 (UTC patch) | 0                          | 250              |
| Sub-step 0             | 0 (baseline + inventory)   | 250              |
| Sub-step 1a (conditional, if WAI stateless) | 3-5 (cluster-history primitive) | 253-255 |
| Sub-step 1b            | 1-3 (WAI cluster_revisit field + tests) | 254-258 |
| Sub-step 2 (T70-T74)   | 5                          | 259-263          |
| Sub-step 3 (T75-T79)   | 5                          | 264-268          |
| Sub-step 4 (T80-T89)   | 7 (T87-T89 reserved)       | 271-275          |
| Sub-step 5 (T90-T99)   | 0 (reservation)            | 271-275          |
| Sub-step 6a (triage)   | 0 (classification only)    | 271-275          |
| Sub-step 6b (RETIRE cleanup) | 0 (deletes legacy bash tests, no pytest change) | 271-275 |
| Sub-step 7 (safety)    | 1 (double-fire; T79 already counted) | 272-276 |
| Sub-step 8 (doc)       | 0                          | 272-276          |

**Step 6 final pytest floor: ~272-276**, depending on (a) whether sub-step
1a triggers and (b) WAI test additions in 1b. Integration test count
delta: -6 minimum (T44-T49 RETIRE) plus any Group E inventory deletions
in sub-step 6b.

---

## Gemini ratification log (2026-04-26)

**Status: RATIFIED IN FULL with five answered questions.** Implementation
proceeds at Pre-Step-6 (UTC anchor patch) next session.

### Q1 — UTC anchor patch sequencing
**Gemini ruling:** Land on current branch (`patch-00566a-unified-refinement`).
**Rationale:** L-3 anchor-script protocol is the safety net. No need for the
overhead of a separate feature branch on a three-line SQL fix. Keep main-line
momentum.

### Q2 — Sub-step 1 WAI cluster-history scope
**Gemini ruling:** If sub-step 0.3 reveals WAI is stateless, split into
sub-step **1a (Primitive)** and sub-step **1b (Contract)**.
**Rationale:** New architectural primitives are not buried inside contract-
amendment commits. Clean git history is a forensic requirement.
**Implementation:** sub-step 1 becomes conditional. If 0.3 reveals WAI is
already stateful, 1a is a noop and 1b proceeds directly. If 0.3 reveals WAI
is stateless, 1a authors the cluster-history primitive as its own commit and
1b adds the `cluster_revisit` field + 200m constant + v2.6 RFC amendment as
the second commit.

### Q3 — T87-T89 reservation
**Gemini ruling:** Keep all three slots reserved.
**Rationale:** Phase F is most likely to surprise us with STACKED forensic
variations. Reserving a full decade (T80-T89) keeps the ledger organized
and prevents messy re-numbering when production data surfaces unanticipated
classes.

### Q4 — Sub-step 6 RETIRE deletions
**Gemini ruling:** Use a **separate cleanup commit**.
**Rationale:** The triage commit (sub-step 6) is about the **decision**;
the deletion is a **structural action**. Keeping them separate makes the
triage logic easier to audit in `git log`.
**Implementation:** sub-step 6 splits into:
- **6a (triage classification):** TEST_SUITE_STATUS.md updated with the
  full per-ID classification table. No deletions. Single commit.
- **6b (RETIRE structural cleanup):** Legacy tests classified RETIRE
  (T44-T49 and any others surfaced by Group E inventory) deleted from
  `tests/test_integration.sh`. Single commit. Forensic rationale per L-9
  in commit body.

### Q5 — Round-trip first-principles fixtures
**Gemini ruling:** **Deliberately NOT Houston.** Use coordinates near
Null Island.
**Rationale:** Forces any developer reading the test to recognize it as
a Synthetic Specification (L-9) rather than a forensic replay. Prevents
"Planimetric Drift" — the failure mode where a developer assumes the
test is based on a real street that may change in a future map update.
**Implementation:** T75-T79 fixtures use:
- Cluster 1 / cluster 3 (PUDO): `(0.0001, 0.0001)` — slightly off Null
  Island so PostGIS doesn't reject as identity element.
- Cluster 2 (intermediate destination): `(0.005, 0.005)` — approximately
  600m offset from cluster 1 in haversine distance, well past the 200m
  gate.
- L-9 docstring explicit: *"Coordinates intentionally near Null Island.
  These are synthetic specification fixtures per L-9, not forensic
  replays. If any reader recognizes these as a real Houston street, the
  test has been corrupted."*

---

## Lessons cited in Step 6 design

L-3 (anchor-based patch scripts): UTC micro-commit
L-7 (cross-check architectural rulings): sub-step 0.3 WAI source-read
L-9 (fixture provenance): every new fixture in sub-steps 2/3/4
L-10 (gate threshold provenance): 200m constant in sub-step 1
L-11 (doc-currency session-bookend): sub-steps 0 and 8

L-2, L-5, L-6, L-8 are baseline gates active throughout (L-8 inactive
in Step 6, reactivates Step 7).

---

## Step 6 ratification checklist

This document is sent to Gemini for ratification. Gemini's options:

- **Ratify in full.** Step 6 implementation begins next session at
  Pre-Step-6 (UTC anchor patch).
- **Ratify with amendments.** Specific sub-steps modified per Gemini
  feedback. Re-ratify amended design before implementation.
- **Push back.** Specific design rulings re-litigated. New design
  conversation, new design document.

Andrew is the final authority on ratification; Gemini provides the
review pass that has worked through 18 Phase E commits.

---

## Concrete next-session starter (after Gemini ratification)

> Phase E Step 6 design ratified at [commit SHA of this document].
> Implementing the Pre-Step-6 UTC anchor patch as the first commit of
> the next session. Sub-step 0 (baseline + Group E + WAI source-read)
> follows in the same session if patch verification passes.
>
> Per L-11 session-open: doc-currency check on PHASE_E_PROGRESS.md
> against HEAD before any work. Proceed to UTC patch only after
> doc-currency confirmed clean.

---

## AMENDMENT 1 (2026-04-27) — Offer-Anchor Lookback (full spec)

**Status:** Ratified by Gemini 2026-04-27 during the sub-step 0.3
closeout session. Implementation lands in sub-step 1a.

**Origin:** During the sub-step 1a design ratification round following
the sub-step 0.3 WAI source-read finding (commit `7863b11`), Claude
proposed `CLUSTER_HISTORY_LOOKBACK_SEC = 1800` (30 min) as the default
lookback for `get_recent_clusters()`. Gemini's verdict refined the
design: a fixed lookback is paranoia-class per L-10 (no production data
grounds the 30-minute number), and it introduces a Python/Postgres
clock-drift risk on the lookback boundary. The offer-anchor refinement
eliminates both concerns by pinning the window to a database-recorded
event timestamp.

### What changed

**Lookback window source.** Was: fixed `CLUSTER_HISTORY_LOOKBACK_SEC = 1800`
constant in `cluster_detection.py`. Now: event-relative window
`[offer_history.accepted_at - preroll_sec, NOW()]` with `preroll_sec`
defaulting to 60. The window is computed at WAI evaluation time from
the database-recorded acceptance event.

**Constant supersession.** `CLUSTER_HISTORY_LOOKBACK_SEC` is removed
from the spec entirely. Replaced by `CLUSTER_HISTORY_PREROLL_SEC = 60`
with L-10 provenance:

```python
# Provenance per L-10: theoretical-with-shadow-mode-instrumentation.
# Sized to comfortably exceed typical offer-card-to-acceptance latency
# (no published metric; reasoning from product behavior — driver views
# the offer card, the cluster they're parked in begins forming during
# review, then accepted_at is recorded). Phase F shadow-mode telemetry
# (B-24) measures whether 60s is correct against real offer-acceptance
# latency distributions; if too tight (cluster-being-formed missed) or
# too loose (prior unrelated cluster pulled in), the constant moves
# before Phase G ships.
CLUSTER_HISTORY_PREROLL_SEC = 60
```

**Timestamp sourcing.** `accepted_at` MUST be sourced from
`app_private.offer_history` (database-side), never from a Python
`datetime.now()` or system-clock-derived value. Eliminates clock-drift
mismatches between application and database. Phase F integration must
ensure the `Offer` dataclass carries `accepted_at` end-to-end, or that
WAI's `evaluate()` path accepts it as an argument; resolution deferred
to 1b authoring per L-6 source-read at that time (B-15 already covers
the Phase F adjacency for `target_address`; analogous extension for
`accepted_at`).

### Updated sub-step 1a signature

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
    """Find all stopped clusters in the window
    [accepted_at_anchor - preroll_sec, NOW()].

    Window is offer-relative — search starts before the driver accepted
    the offer (capturing the pickup-cluster-being-formed during the
    offer-card review window) and runs through the present moment.
    Returns clusters ordered oldest-first.

    Empty list means no qualifying clusters in the window.
    """
```

The `lookback_sec` parameter from the original ratified spec is
removed. SQL approach (gaps-and-islands) is unchanged; only the window
expression in the `recent` CTE differs.

### NEW: Same-address PLAN-side latch (R5 corollary)

`pudo_planner.py` adds a defensive latch independent of WAI confidence.
The latch lifts T79's test-time assertion to a runtime gate:

```
IF pickup_address == dropoff_address (or coords within 30m geocoder noise)
   AND cluster_revisit IS NOT True:
       REFUSE to emit fire_dropoff
       Hold state, await structural confirmation
```

Without this latch, a same-address ride could fire dropoff at the
pickup cluster on the very first heartbeat post-pickup-fire — the
exact BMOAR Path B failure class this rewrite retires. With the latch,
`fire_dropoff` is structurally gated until `cluster_revisit` genuinely
confirms the round trip via the 3-cluster topology.

**Backlog item B-26 (NEW):** Implement same-address latch in
`pudo_planner.py`. Sub-step assignment: between 1b and 2. Likely a
dedicated sub-step 1c or folded into early sub-step 2. Runtime
behavior must be in place before T75-T79 integration tests in sub-step
3 (T79 tests this latch end-to-end).

### R5 (rev) — Structural revisit, offer-anchored

R5 from the original document (line of the "Architectural ruling locked
at Step 6 design" section) is amended:

> **R5 (rev):** Round-trip detection uses topological evidence (PUDO
> cluster → ≥200m intermediate cluster → PUDO cluster) within a window
> pinned to the current offer's database-recorded `accepted_at`
> timestamp + 60s pre-roll buffer. No fixed time lookback. No odometer
> dependency. The intermediate cluster IS the proof, anchored by the
> database-recorded offer-acceptance event.
>
> The `CLUSTER_REVISIT_MIN_GAP_M = 200` constant is unchanged in value;
> per Gemini's framing, it is a **Structural Noise Floor** (Houston GPS
> multipath wobble), not a policy threshold. Adjusting it would require
> a physics-of-the-environment justification, not a behavioral
> preference.

Refined provenance comment for `CLUSTER_REVISIT_MIN_GAP_M`:

```python
# Provenance per L-10: production-data-grounded structural noise floor.
# Houston GPS multipath wobble in urban canyons can hit 100-150m
# (Forum Park 7623 case + operational observation 2026-04-26). 200m
# ensures "Elsewhere" means the car physically left the block, not GPS
# jitter. This is a noise floor, not a policy — adjusting it requires
# a physics-of-the-environment change, not a behavioral preference.
CLUSTER_REVISIT_MIN_GAP_M = 200
```

### Implementation sequence (sub-step ordering preserved)

1. **Sub-step 1a:** `get_recent_clusters()` per amended signature above.
   Lives in `cluster_detection.py` (per sub-step 0.3 design ratification —
   not `where_am_i.py` as the original document said). `Cluster`
   dataclass extension: add `latest: datetime`.
2. **Sub-step 1b:** `WhereAmIResult.cluster_revisit: bool` field, the
   200m constant with refined provenance, v2.6 RFC amendment to
   `WHERE_AM_I_PROPOSAL_v2.md`. Resolves Phase F `accepted_at`
   plumbing question per L-6 source-read at authoring time.
3. **Sub-step 1c (NEW) or merged into early sub-step 2:** Same-address
   latch per B-26.
4. **Sub-step 2 onward:** Unchanged. T70-T74 Five Pillars, T75-T79
   Round-trip with offer-anchor window, T80-T89 STACKED, T90-T99
   reservation, sub-steps 6a/6b legacy triage, sub-step 7 safety, 8
   closeout.

### What Amendment 1 does NOT change

- Sub-step ordering (1 → 8)
- The 200m gate value
- Q5 Null Island synthetic-fixture convention
- T70-T89 test specs (gate logic same; only window source for cluster
  history changed)
- Legacy triage rubric and pre-classifications
- The two-cluster topology of `cluster_revisit` (PUDO → intermediate
  → PUDO)
- Phase F backlog items B-15, B-16, B-17 scope (B-15 grows slightly
  to also carry `accepted_at` if not already in `Offer` — verified at
  sub-step 1b authoring per L-6)
- Phase G BMOAR deprecation gating

### Ratification log

Gemini, 2026-04-27, sub-step 0.3 closeout session:

> "We are moving away from the 30-minute lookback. We will instead
> anchor the cluster history search to the database-side `accepted_at`
> timestamp with a 60-second pre-roll. Use the 200m spatial gap as the
> structural gate for Cluster 2. This removes all arbitrary
> time/distance policies and replaces them with a ride-relative
> structural latch."

Q1-Q5 from the original sub-step 1a design brief are subsumed by this
ratification. Q6 (forensic round-trip case for L-9 fixture provenance)
remains open and is gated by a re-verification query at sub-step 1a
authoring time per L-6 corollary.
