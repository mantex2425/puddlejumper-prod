# TEST_SUITE_STATUS.md — Technical Debt Ledger

**Last verified:** 2026-04-26 (Phase E sub-step 0.1 baseline run, 23:46:19 UTC)
**Owner:** Andrew Bruce
**Status pages this maps to:** Phase D (where_am_i), Phase E (pudo_planner), Phase F (driver_heartbeat integration)

---

## Why this document exists

`tests/test_integration.sh` is a 37KB bash + psql integration test suite covering 61 named tests (T01–T60+).
As of Phase C deploy, **22 tests pass and 39 tests fail.** The 39 failures are NOT regressions — they are
**legacy tests asserting state-machine behaviors that have already been deprecated by ongoing
architectural work.**

The `where_am_i()` build (Phases D–H, in flight) is the dedicated cycle that will replace the underlying
fire-decision logic these tests depend on. As that work ships, the corresponding tests are expected to
either (a) start passing, or (b) be deleted as their use cases are absorbed into new test files.

This document is the **debt ledger** that tracks which failures are expected, why, and which phase is
expected to clear them. It is updated whenever the failure delta moves.

---

## How to use this document

**Before deploy:** run the integration suite. Confirm the failure count matches the count documented here.

```bash
cd ~/puddlejumper-prod
bash tests/test_integration.sh 2>&1 | grep -E "^======" | tail -1
# Expected: ❌  22/61 passed | 39 failed — DO NOT DRIVE
```

If the failure count is not 39 (or whatever number is current at the top of this doc), **do not deploy**
until you understand what changed. The 39-failure floor is verified by sub-step 0.1's live baseline run (2026-04-26 23:46:19 UTC) and is the canonical reference for Phase E Step 6 triage. Either the legacy debt has gotten worse (real regression) or a phase
has cleared some of it (in which case update this doc).

**During Phase D/E/F development:** when a phase ships, re-run the suite. Tests listed under that phase
should flip from FAIL → PASS. If they don't, the phase isn't done. If tests OUTSIDE that phase's scope
flip in either direction, that's a signal worth investigating.

---

## Failure inventory

### Group A — Pickup-nail fire path (deprecated by Phase D `where_am_i()`)

These tests assert that a pickup nail fires under specific conditions, transitioning state
ENROUTE → IN_TRIP. The current BMOAR Path A/B fire decisions have known holes (Forum Park 7623
is the canonical example). Phase D builds the `where_am_i()` continuous awareness primitive that
replaces these fire paths with class-aware matching + ghost cache + retroactive correction.

| Test ID | Description | Current symptom | Phase D expectation |
|---|---|---|---|
| T44 | Nail pickup at circular origin → IN_TRIP | got ENROUTE, expected IN_TRIP | PASS once `pudo_planner` consumes WAI output |
| T47 | Round-trip setup: IN_TRIP after pickup nail | got ENROUTE, expected IN_TRIP | PASS (same fire path) |
| T50a | Stacked+WatchdogB setup: IN_TRIP after pickup nail | got ENROUTE, expected IN_TRIP | PASS (same fire path) |
| T60a | Secondary cancel setup: IN_TRIP after pickup nail | got ENROUTE, expected IN_TRIP | PASS (same fire path) |
| T04 | Heartbeat at pickup stopped → IN_TRIP | got ENROUTE, expected IN_TRIP | PASS once WAI/PLAN wired |
| T05 | driverState response = IN_TRIP | got ENROUTE, expected IN_TRIP | PASS once WAI/PLAN wired |
| T13b | Heartbeat stopped at declined pickup → INITIAL_NAIL → IN_TRIP | got ENROUTE, expected IN_TRIP | PASS once S11 override + WAI/PLAN wired |
| T14 | Setup: reached IN_TRIP | got ENROUTE, expected IN_TRIP | PASS once WAI/PLAN wired |
| T16 | Setup: IN_TRIP with nailed pickup | got ENROUTE, expected IN_TRIP | PASS once WAI/PLAN wired |
| T18 | Stacked setup: IN_TRIP after pickup nail | got ENROUTE, expected IN_TRIP | PASS once WAI/PLAN wired |
| T26 | GPS at pickup stopped → INITIAL_NAIL → IN_TRIP | got ENROUTE, expected IN_TRIP | PASS once WAI/PLAN wired |
| T30 | Watchdog setup: IN_TRIP | got ENROUTE, expected IN_TRIP | PASS once WAI/PLAN wired |
| T32 | No-show setup: IN_TRIP after pickup nail | got ENROUTE, expected IN_TRIP | PASS once WAI/PLAN wired |

### Group B — Dropoff refinement path (deprecated by Phase D `where_am_i()` + Phase E `pudo_planner`)

Tests that assert the REFINE_DROPOFF state arms or holds based on heartbeat positioning relative to the
expected dropoff pin. Same deprecation track as Group A — current logic relies on BMOAR Path A/B
proximity heuristics; new logic uses WAI's class-aware matching with the address taxonomy.

| Test ID | Description | Current symptom | Phase D/E expectation |
|---|---|---|---|
| T45 | Heartbeat near pickup/dropoff pin (1.5mi driven) → REFINE_DROPOFF armed | got ENROUTE, expected REFINE_DROPOFF | PASS once dropoff WAI flow ships |
| T46 | Stopped at circular dropoff → stays REFINE_DROPOFF (Watchdog B pending departure) | got ENROUTE, expected REFINE_DROPOFF | PASS once dropoff WAI flow ships |
| T49 | Round-trip gate clears at 1.1mi → REFINE_DROPOFF armed | got ENROUTE, expected REFINE_DROPOFF | PASS |
| T06b | Entering blast radius at 35mph → REFINE_DROPOFF | got ENROUTE, expected REFINE_DROPOFF | PASS once dropoff WAI flow ships |
| T07 | Heartbeat at dropoff stopped → UNCOMMITTED | got ENROUTE, expected UNCOMMITTED | PASS once dropoff WAI fires |
| T08 | driverState response = UNCOMMITTED | got ENROUTE, expected UNCOMMITTED | PASS once dropoff WAI fires |
| T22 | Heartbeat at secondary dropoff → UNCOMMITTED | got ENROUTE, expected UNCOMMITTED | PASS once STACKED secondary dropoff fires |
| T35 | No-show: complete secondary ride → UNCOMMITTED | got ENROUTE, expected UNCOMMITTED | PASS once dropoff WAI + STACKED swap wired |

### Group C — Watchdog gating (cascade failures from Group A/B)

Tests that depend on a setup step from Group A/B succeeding before they can validate Watchdog gating
behavior. These will start passing the moment Group A/B clear; the actual Watchdog logic itself is
not necessarily wrong.

| Test ID | Description | Current symptom |
|---|---|---|
| T48 | Round-trip gate holds at 0.5mi — stays IN_TRIP (Watchdog blocked) | got ENROUTE — cascades from T47 |
| T17 | Driving at speed after pickup nail → stays IN_TRIP (ABORT blocked) | got ENROUTE — cascades from T16 (Group A) |
| T31 | Watchdog fires after 95min IN_TRIP → UNCOMMITTED | got ENROUTE — cascades from T30 (Group A) |

### Group D — STACKED atomic swap (deprecated by Phase E `pudo_planner` + Phase F integration)

The STACKED state machinery was rebuilt in March 2026 with `current_offer_id` as a live pointer. The
test fixtures still use the older swap semantics. Phase E's PLAN consumer is the natural place to
re-validate STACKED transitions because it's the layer that owns the "fire dropoff for primary,
swap to secondary" decision.

| Test ID | Description | Current symptom | Expected resolution |
|---|---|---|---|
| T50b | Stack accepted while IN_TRIP → STACKED | got ENROUTE, expected STACKED | Cascades from T50a (Group A) |
| T50c | Micro-stop at primary dropoff → candidate set, stays STACKED | got ENROUTE, expected STACKED | Cascades from T50a; also Phase E |
| T50e | Primary offer audit record has dropoff written (not poisoned) | got 0, expected 1 | Phase E (audit write semantics) |
| T60b | Secondary accepted → STACKED | got ENROUTE, expected STACKED | Cascades from T60a (Group A) |
| T60c | New offer while STACKED → secondary cancelled → IN_TRIP on primary | got UNCOMMITTED, expected IN_TRIP | Phase E (cancel semantics) |
| T60d | Primary offer still active after secondary cancel | got 0, expected 1 | Phase E (cancel semantics) |
| T19 | Stack offer accepted → STACKED | got ENROUTE, expected STACKED | Cascades from T18 (Group A) |
| T21 | Heartbeat at secondary pickup → IN_TRIP | got ENROUTE, expected IN_TRIP | Cascades from T18 (Group A) |
| T23 | Exactly 7 state log entries for stacked trip | got 3, expected 7 | Cascades from T18 (Group A) |
| T33 | No-show: new offer accepted → STACKED | got ENROUTE, expected STACKED | Cascades from T32 (Group A) |
| T34b | No-show: GPS at secondary pickup → IN_TRIP | got ENROUTE, expected IN_TRIP | Cascades from T32 (Group A) |

### Group E — Earlier scenarios (T01–T42 area) — CLOSED

**Status:** CLOSED 2026-04-26 by Phase E sub-step 0.1 baseline run (23:46:19–23:47:16 UTC). The full failure inventory was captured and categorized into Groups A, B, C, D, and a new Group F (setup-cascade failures). See Groups A–F above for the per-ID classification.

**Final inventory (2026-04-26):** 39 failures total, matching the long-standing 22/61 figure exactly. Distribution:

| Group | Count | Description |
|---|---|---|
| A | 13 | Pickup-nail fire path (BMOAR Path A deprecation) |
| B | 8 | Dropoff refinement path (BMOAR Path B deprecation) |
| C | 3 | Watchdog gating cascade |
| D | 11 | STACKED atomic swap |
| F | 4 | Setup-cascade from Group A (test fails at a post-setup step that never ran because Group A nail didn't fire) |
| **Total** | **39** | |

### Group F — Setup-cascade failures from Group A (NEW, Phase E sub-step 0.1)

Tests in this group don't fail at their own assertion logic. They fail because an earlier setup step (typically a Group A pickup nail) didn't fire, leaving the test scenario in an unexpected state when the assertion runs. These will start passing the moment Group A clears; the actual logic being tested is not necessarily wrong.

Distinct from Group C: Group C is Watchdog-specific cascades. Group F is generic setup-cascades that don't fit Group C's Watchdog scope.

| Test ID | Description | Current symptom |
|---|---|---|
| T06 | Heartbeat driving to dropoff → stays IN_TRIP | got ENROUTE — cascades from T04 (Group A) |
| T09 | Exactly 4 state log entries for solo trip | got 1, expected 4 — cascades from T04+T07 (Group A+B) |
| T09b | Auto Nail It dropoff written to offer_history | got 0, expected 1 — cascades from T07 (Group B) |
| T13c | Override trip complete → UNCOMMITTED | got ENROUTE — cascades from T13b (Group A) |

---

## Audit script (run before each phase deploy)

This block can be used to confirm the suite hasn't regressed beyond known debt:

```bash
cd ~/puddlejumper-prod && \
bash tests/test_integration.sh 2>&1 | tail -3 | tee /tmp/last_integration_run.txt
```

**Acceptance: failure count must be ≤ the count at the top of this document.**

If failures increase, a real regression has been introduced — investigate before deploying.
If failures decrease, a phase has cleared some debt — update this document accordingly.

---

## Living document

This doc is updated when:
- A phase deploys that is expected to clear some Group A–D entries (move tests from FAIL list to PASS list)
- A new failure is discovered that belongs in an existing group
- The full Group E audit is performed and entries get categorized into A–D

Last update: 2026-04-26 (Phase E sub-step 0.1 — Group E TODO closed, Groups A–D expanded with full inventory, Group F created for setup-cascade failures)
