# Scenario Inventory Audit (2026-06-01)

**Branch:** `fix/three-recon-bugs-2026-05-30` @ `0dfc154`
**Pytest baseline:** 683 passed / 1 skipped (with JOB 2 patch staged; pre-patch 680/1)
**Scope:** READ-ONLY inventory and classification. No code, tests, migrations, or deploy.
**Purpose:** identify what re-validation is needed after the §XVIII bind fix lands.

---

## §0 Reading the table

**Era — where the scenario was written:**
- *Post-demolition*: pure-sensor WAI + naked-list dispatch + §XIV.I. References WAI, FirePickupObservation, ClearNarrative, _commits, per_target_outcomes.
- *Hybrid*: TOML files that mix state-machine vocabulary (STACKED, ENROUTE, IN_TRIP, anchor_coord, potential_cancellation) with post-demolition concepts. Most S32-S35.
- *State-machine*: pure state-machine flow tests (state transitions, mode flags). S01-S09, S11, S12.

**Classification vs current architecture:**
- *STILL-VALID*: semantics map directly to current code with no changes.
- *VOCABULARY-STALE*: underlying scenario is real and current-architecture-relevant, but the documented description uses retired terminology. Translation required, not redesign.
- *NEEDS-REDESIGN*: assumptions (e.g., explicit state machine, mode flags) are gone; scenario must be re-articulated against the current model before it can be re-defended.

**Coverage verdict — four categories, not three:**
- *DEFENDED-LIVE*: test exists AND uses `db_cur` (real-PG, conftest.py:164) fixture against the live predicate.
- *DEFENDED-MOCK-ONLY*: test exists but uses MagicMock cursor. False confidence per §XIV.J (B-2 precedent).
- *DEFENDED-PURE*: test exists but exercises **pure-function dispatch logic** without any cursor (mock or real). Validates the case-resolution decision but not the wired ingest→snap→WAI→dispatch→execute pipeline. New category surfaced by this audit.
- *DEFENDED-REPLAY*: test exists and replays captured heartbeats against the WAI evaluation path. Forensic-grade for the path tested; does not exercise dispatch or _execute_action.
- *UNDEFENDED*: no test, or inventory-only structural test.

---

## §1 Dispatch Cases (§4, §5 — Post-Demolition Architecture)

These are the live, post-demolition case taxonomy. All defined in `docs/RIDE_LIFECYCLE.md:99-106` and `docs/SIMPLIFIED_ARCHITECTURE.md §4`.

| # | Scenario | Doc | Test | Era | Validity | Coverage | Dispatch-path? |
|---|---|---|---|---|---|---|---|
| 1 | Case A — No Match (idle / empty cluster) | RIDE_LIFECYCLE.md:99 | test_heartbeat_dispatch.py:94-114 | Post-demolition | STILL-VALID | DEFENDED-PURE | yes |
| 2 | Case B — Single Pickup, No Active Ride | RIDE_LIFECYCLE.md:100 | test_heartbeat_dispatch.py:122-126 | Post-demolition | STILL-VALID | DEFENDED-PURE | **YES (regression risk)** |
| 3 | Case C — Dropoff of Active Ride | RIDE_LIFECYCLE.md:101 | test_heartbeat_dispatch.py:134-141 | Post-demolition | STILL-VALID | DEFENDED-PURE | **YES (regression risk)** |
| 4 | Case D — Implicit Cancellation + New Pickup (Ghost Ride) | RIDE_LIFECYCLE.md:102, SIMPLIFIED_ARCHITECTURE.md §4 D | test_heartbeat_dispatch.py:149-161 | Post-demolition | STILL-VALID | DEFENDED-PURE | **YES (regression risk)** |
| 5 | Case E — Ambiguous (≥3 matches, fail closed) | RIDE_LIFECYCLE.md:103 | test_heartbeat_dispatch.py:169-239 | Post-demolition | STILL-VALID | DEFENDED-PURE | yes |
| 6 | Case F — Dropoff Matched, No Prior Pickup (Missed-Pickup) | RIDE_LIFECYCLE.md:104, SIMPLIFIED_ARCHITECTURE.md §4 F | test_heartbeat_dispatch.py:247-272 | Post-demolition | STILL-VALID | DEFENDED-PURE | **YES (regression risk)** |
| 7 | Case G — Pickup Re-Match While Active (idempotent re-entry) | RIDE_LIFECYCLE.md:105-106 | test_heartbeat_dispatch.py:280-290 | Post-demolition | STILL-VALID | DEFENDED-PURE | **YES (regression risk)** |

**Dispatch-path note (Cases A-G):** all pass through `driver_heartbeat._execute_action`. The §XVIII bind fix (whatever shape it takes) will touch the FPO branch of `_execute_action` and may extend its signature again. Cases producing FirePickup / FireDropoff / FirePickupObservation / FireDropoffObservation are the high-risk set; Cases A and E (which emit Log*-only actions) are lower risk.

---

## §2 §5 Disambiguation Subcases (Multi-Match)

| # | Scenario | Doc | Test | Era | Validity | Coverage | Dispatch-path? |
|---|---|---|---|---|---|---|---|
| 8 | §5.1 Errands (same-offer pickup+dropoff at one cluster) | docs/PHASE_E_STEP_6_DESIGN.md "§5.1 errands"; SIMPLIFIED_ARCHITECTURE.md §5.1 | test_heartbeat_dispatch.py:173-195 | Post-demolition | STILL-VALID | DEFENDED-PURE | yes |
| 9 | §5.2 Hot-swap (primary dropoff == secondary pickup) | SIMPLIFIED_ARCHITECTURE.md §5.2 | test_heartbeat_dispatch.py:197-212 | Post-demolition | STILL-VALID | DEFENDED-PURE | **YES (regression risk)** |
| 10 | §5.3 pickup case (two pickups, same geocode, recency tiebreak) | CANONICAL_RULES.md §XIV.I §5.3 | test_heartbeat_dispatch.py:389-446 | Post-demolition | STILL-VALID | DEFENDED-PURE | **YES (regression risk — emits FPO directly)** |
| 11 | §5.3-mirror dropoff case (two dropoffs same geocode, fail closed) | CANONICAL_RULES.md §XIV.I §5.3-mirror | test_heartbeat_dispatch.py:453-519 | Post-demolition | STILL-VALID | DEFENDED-PURE | yes (ClearNarrative path) |
| 12 | N≥3 matches (unenumerated multi-match) | SIMPLIFIED_ARCHITECTURE.md §5 unenumerated | test_heartbeat_dispatch.py:526-553 | Post-demolition | STILL-VALID | DEFENDED-PURE | yes (LogAmbiguousMatch only) |
| 13 | Houston Playback 7849/7850 (production re-bid regression net) | Captured 2026-05-12 production trace | test_heartbeat_dispatch.py:578-615 | Post-demolition | STILL-VALID | DEFENDED-PURE | **YES (FPO-emitting recency tiebreaker)** |

---

## §3 Round-Trip / Houston Loop

| # | Scenario | Doc | Test | Era | Validity | Coverage | Dispatch-path? |
|---|---|---|---|---|---|---|---|
| 14 | Round-Trip (pickup == dropoff at same geocode, single offer) | PHASE_E_PROGRESS.md "Round-trip block T75-T79"; PHASE_E_STEP_6_DESIGN.md "§5.1 errands, round-trip topology" | NONE (§5.1 errands test covers same-offer same-cluster but not the 3-cluster revisit topology specifically) | Post-demolition (cluster_revisit, WAI) | STILL-VALID | UNDEFENDED (gap — §5.1 errands test asserts the *fail-closed-for-unrelated-active* form, not the round-trip topology proper) | yes (would route to §5.1 errands handling) |

---

## §4 Scenario TOMLs in `scenarios/`

Replay harness lives in `tests/test_scenarios.py`. Only one scenario is replayed; the rest carry inventory/structural test only.

| # | Scenario (TOML id) | TOML path | Test | Era | Validity | Coverage | Dispatch-path? |
|---|---|---|---|---|---|---|---|
| 15 | S01-S09 (single-PUDO state-machine flows) | scenarios/S01.toml .. S09.toml | test_scenarios.py inventory only | State-machine | NEEDS-REDESIGN (state machine is gone) | UNDEFENDED | n/a (no current path) |
| 16 | S11 — System Declined, Driver Manually Accepted | scenarios/S11.toml | test_scenarios.py inventory only | State-machine | VOCABULARY-STALE (maps to §XVIII lost-mode bind territory) | UNDEFENDED | yes (would touch the §XVIII bind path being changed) |
| 17 | S12 — Primary Cancelled Mid-Trip, Secondary Promoted | scenarios/S12.toml | test_scenarios.py inventory only | State-machine | VOCABULARY-STALE (maps to Case D + §5.x) | UNDEFENDED | yes |
| 18 | S31 — Forum Park 7623 (geometric pillar, single-PUDO) | scenarios/S31.toml | test_scenarios.py:155-285 (`_replay_S31`); fixture `tests/fixtures/7623_heartbeats.json` | Post-demolition (WAI confidence model) | STILL-VALID | DEFENDED-REPLAY (WAI evaluation only — no dispatch, no execute) | no (WAI-only replay) |
| 19 | S32 — Implicit STACKED Cancellation | scenarios/S32_implicit_stacked_cancel.toml | test_scenarios.py inventory only (`_REPLAY_HANDLERS_IMPLEMENTED = {"S31"}`) | Hybrid | VOCABULARY-STALE (Case D's implicit-cancellation lane) | UNDEFENDED | **YES (regression risk — implicit-cancel + new pickup is exactly Case D)** |
| 20 | S33 — Calhoun Missed Pickup | scenarios/S33_calhoun_missed_pickup.toml | test_scenarios.py inventory only | Hybrid | VOCABULARY-STALE (Case F implements this exactly) | UNDEFENDED for TOML; the case logic is DEFENDED-PURE under Case F (line 247-272) | yes (Case F path) |
| 21 | S34 — Hot Swap (TOML form) | scenarios/S34_hot_swap.toml | test_scenarios.py inventory only | Hybrid | VOCABULARY-STALE (§5.2 hot-swap implements this) | UNDEFENDED for TOML; logic DEFENDED-PURE under §5.2 (line 197-212) | **YES (§5.2 path emits paired Fire actions)** |
| 22 | S35 — Stacked Uber Re-Award | scenarios/S35_stacked_uber_reaward.toml | test_scenarios.py inventory only | Hybrid | VOCABULARY-STALE (Case D-like, but stacked queue inverse) | UNDEFENDED | yes |
| 23 | S04 — Cancel Mid-ENROUTE | scenarios/S04.toml | test_scenarios.py inventory only | State-machine | VOCABULARY-STALE (semantically Case D) | UNDEFENDED | yes |

---

## §5 Bind / Idempotency / Lost-Mode Forensic Tests

| # | Scenario | Test | Era | Validity | Coverage | Dispatch-path? |
|---|---|---|---|---|---|---|
| 24 | §XVIII cold-start bind (FPO singleton → bind) | tests/test_lost_mode_cold_start_bind.py (5 tests) | Post-demolition | STILL-VALID | DEFENDED-MOCK-ONLY (MagicMock cursor; §XIV.J false-confidence risk) | **YES (this IS the path being changed)** |
| 25 | Rule XV Idempotency (Imperial Valley pickup 5 / FirePickup α-fix) | tests/test_execute_action_id_fix.py (multiple) | Post-demolition | STILL-VALID | DEFENDED-MOCK-ONLY | **YES — touches _execute_action FirePickup branch** |
| 26 | Phase 2c.2 Item 3b.R caller wiring | tests/test_driver_heartbeat_3b_r.py | Post-demolition | STILL-VALID | DEFENDED-MOCK-ONLY | yes |
| 27 | §XVIII Phase 2b lost-mode replay | tests/test_xviii_phase2b_replay.py | Post-demolition | STILL-VALID | **DEFENDED-LIVE** (uses `db_cur` real-PG fixture) | **YES** |
| 28 | Houston playback (lost-mode live) | tests/test_lost_mode_houston_playback_live.py | Post-demolition | STILL-VALID | **DEFENDED-LIVE** | yes |
| 29 | Stale-pointer reconciliation (snapshot L-19 + dead-predicate) | tests/test_driver_queue_reconcile_stale_pointer.py (6 tests) | Post-demolition | STILL-VALID | DEFENDED-MOCK-ONLY | no (queue read path, not dispatch) |

---

## §6 Coverage rollup (all rows above)

| Verdict | Count | What it means |
|---|---|---|
| DEFENDED-LIVE | 2 | Real-PG fixture against live predicate (test_xviii_phase2b_replay, test_lost_mode_houston_playback_live) |
| DEFENDED-MOCK-ONLY | 4 | MagicMock cursor — §XIV.J false-confidence risk per the B-2 precedent |
| DEFENDED-PURE | 13 | Pure-function dispatch unit tests (test_heartbeat_dispatch.py covers Cases A-G + §5 variants + Houston Playback) |
| DEFENDED-REPLAY | 1 | S31 forensic replay against WAI (no dispatch/execute) |
| UNDEFENDED | 9 | S01-S09 (collapsed), S11, S12, S32, S33-TOML, S34-TOML, S35, S04, Round-Trip |

---

## §7 Regression-risk set for the pending §XVIII bind fix

The §XVIII fix (whatever form it takes from the doctrine review) modifies the FirePickupObservation branch of `driver_heartbeat._execute_action` (line 548-700) and likely extends its kwarg surface to receive `per_target_outcomes` and `tad_verdicts` from the caller (per the data-boundary recon).

**Scenarios that hit this exact path and MUST be re-validated post-fix:**

| Row | Scenario | Why at risk |
|---|---|---|
| 10 | §5.3 pickup case | Emits FPO directly via dispatch — any change to the FPO handler hits this |
| 13 | Houston Playback 7849/7850 | Same path; production regression net |
| 24 | §XVIII cold-start bind | The path itself |
| 25 | Rule XV Idempotency | Adjacent FirePickup branch; alpha-fix mirror is in the FPO branch |
| 27 | Phase 2b lost-mode replay | Lost-mode demotion routes through FPO |
| 28 | Houston playback live | Lost-mode coverage |

**Scenarios that hit `_execute_action` more broadly and warrant a second look:**

| Row | Scenario | Path |
|---|---|---|
| 2,3,4,6,7 | Cases B/C/D/F/G | Each emits Fire* actions through `_execute_action` |
| 9 | §5.2 Hot-Swap | Paired Fire actions (FireDropoff + FirePickup at one cluster) |
| 19 | S32 implicit STACKED cancel | Case D semantic equivalent — if reframed and replayed, would touch dispatch+execute |
| 21 | S34 hot-swap TOML | §5.2 semantic equivalent |
| 26 | Item 3b.R caller wiring | Lives at the caller-side of `_execute_action` |

**Scenarios on the dispatch path but lower risk (Log*-only or unbind-only actions):**

| Row | Scenario |
|---|---|
| 1 | Case A (LogNoMatch only) |
| 5 | Case E (LogAmbiguousMatch only) |
| 11 | §5.3-mirror dropoff (ClearNarrative + FireDropoffObservation pair) |
| 12 | N≥3 unenumerated (LogAmbiguousMatch only) |

---

## §8 Audit findings

1. **The dispatch decision logic is well-defended (DEFENDED-PURE).** test_heartbeat_dispatch.py covers every named Case and §5 variant as pure-function tests. The case-resolution algorithm is unlikely to silently regress.

2. **The wired ingest→snap→WAI→dispatch→execute pipeline is thinly defended live.** Only two tests (xviii_phase2b_replay, houston_playback_live) use the real-PG fixture against the full path. Everything else is either MagicMock (false-confidence per §XIV.J) or pure-function (skips the wiring).

3. **The §XVIII cold-start bind path — the path the next fix will modify — is DEFENDED-MOCK-ONLY.** test_lost_mode_cold_start_bind.py covers 5 scenarios with MagicMock cursor only. If the fix changes the FPO handler's kwarg signature or the bind precondition, the mock tests will require manual fixture updates to keep matching mock-shaped expectations; they will NOT independently validate behavior against the real DB.

4. **The TOML scenario library (S32-S35) is INVENTORY ONLY.** `_REPLAY_HANDLERS_IMPLEMENTED = {"S31"}` at test_scenarios.py:116 — every other scenario file has a structural test asserting the file is parseable, but no replay harness exists. S32-S35 are at risk of silent semantic drift; their VOCABULARY-STALE classification is plausible-but-untested-against-current-code.

5. **Round-trip (Houston Loop) topology is UNDEFENDED.** The §5.1 errands test asserts the unrelated-active fail-closed branch, but not the 3-cluster revisit topology that the round-trip design doc specifies. If a real round-trip ride happens, no test ensures the cluster_revisit signal threads correctly into the dispatch case.

6. **Three scenarios that are conceptually covered by Cases F / §5.2 still carry TOML files with state-machine vocabulary.** S33 (Case F equivalent), S34 (§5.2 equivalent), S32 (Case D equivalent). These TOMLs are documentation noise unless harmonized — either replayed against the modern path or archived with a pointer to the post-demolition case.

7. **Two scenarios that look small but warrant audit attention before the §XVIII fix:** S11 (manual accept of declined offer) and S35 (Uber re-award race). Both touch the bind precondition territory the doctrine review will reshape. Neither has a current test; both should be on the post-fix re-validation list even if no test is written.

---

## §9 What this audit does NOT do

- Does not redesign any scenario.
- Does not propose new tests.
- Does not propose changes to existing tests.
- Does not propose archiving any TOML.
- Does not propose a fix or amendment to §XVIII.

Hands the inventory to the doctrine-review loop and the post-fix re-validation planning.

---

## §10 Pointers

- Pure-function dispatch suite: `tests/test_heartbeat_dispatch.py`
- TOML scenario harness: `tests/test_scenarios.py` (replay coverage limited to S31)
- Real-PG fixture: `tests/conftest.py:164` (`db_cur`)
- DEFENDED-LIVE examples: `tests/test_xviii_phase2b_replay.py`, `tests/test_lost_mode_houston_playback_live.py`
- §XVIII bind path: `driver_heartbeat.py:548-700` (FPO branch of `_execute_action`); gate at line 608
- Dispatch cases doc: `docs/RIDE_LIFECYCLE.md:99-106`, `docs/SIMPLIFIED_ARCHITECTURE.md §4`
- §XIV.I §5.3 doctrine: `docs/CANONICAL_RULES.md §XIV.I`
- TOML scenarios: `scenarios/*.toml`
