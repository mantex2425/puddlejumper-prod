# Handoff — PUDO P0 discovered after Causality Guard shipped

**Date:** 2026-05-13 (early morning, post-midnight UTC)
**Branch:** `phase-2c-2-tad-exit-4tools`
**Last commit:** `4ae289b` — feat(predicate): horizon physics + causality guard + Houston Playback
**Pytest floor:** 604 passed, 5 skipped (was 592 at session start)
**Cloud Run:** unchanged — `puddlejumper-api-00604-2rs` still serving 100%
**State:** tonight's commit pushed to origin. NOT YET DEPLOYED.

---

## TL;DR

Tonight shipped the predicate-side fixes for lost_mode reliability (Causality Guard, horizon-physics `_detect_lost_mode`, Houston Playback regression net). Then we did a forensic deep-dive on today's PUDO misses to understand why Auto Nail It failed catastrophically on the 2026-05-12 drive (1 of 5 pickups fired, 2 of 5 dropoffs fired).

**The investigation discovered a P0 architectural gap that is upstream of everything we've been working on:**

> TAD writes recovery commits to `pudo_decision_context.tad_decision_context` JSONB, including a `committed: [{"offer_id": "7850", "commit_rule": "lost_floor_with_poi_lift"}]` list. **NO production code reads this list.** The planner reads `verdicts.X.passed`. In lost_mode, `passed` is always `null`. The planner sees null, does nothing, `planner_action` stays empty, `dispatch_executed` stays false, `current_offer_id` never gets set. The PUDO doesn't fire even when every gate is screaming "fire this."

The recovery path is wired forensically (TAD writes the right verdict). The execution path consuming it does not exist (or got removed in the state-machine demolition).

This is the actual bug behind today's massive failure. Every missed PUDO from 2026-05-12 likely has this same pattern in its `tad_decision_context`.

---

## What shipped tonight (commit `4ae289b`, +1136 / -46, 11 files)

### 1. `_detect_lost_mode` rewrite (driver_heartbeat.py)
Replaces `actual_pickup_at IS NULL` + wall-clock 2-hour window with `LIVE_OFFER_PREDICATE_SQL`. Fixes Houston Miss + Calhoun Zombie failure modes via clauseA (`oh.actual_dropoff_at IS NULL`). Physics terminates the narrative, not fire state.

### 2. Causality Guard (driver_queue.py predicate + helper)
Adds `oh.created_at <= %s::timestamptz` as the second clause of `LIVE_OFFER_PREDICATE_SQL`. Without it, replay against a historical reference_time returns offers from the future. Production NOW() was silently safe; replay was not. The helper now returns a 14-tuple with `reference_time` bound at both the Causality Guard slot (position 1) and the time-horizon slot (position 7) — both from the same `_now()` capture per call.

### 3. Houston Playback (tests/test_lost_mode_houston_playback_live.py)
11-row parametrized replay of the 2026-05-12 afternoon drive against real `offer_history` rows. Anchors at synthetic UTC timestamps, reads real odometer from `pudo_decision_context.odometer_gate_result` blobs, asserts `_detect_lost_mode` behavior at each inflection point. **Row 10 is the KILL SHOT** — 7852 mid-Calhoun-window (pickup-fired 18:55:42, horizon expires 19:41:01) with synthetic empty queue at 19:00:00 UTC. Old rule said False; new rule correctly says True. Regression net is locked in.

### 4. Structural-contract tests (tests/test_live_offer_predicate_imports.py)
- New: `test_predicate_has_causality_guard` ensures the new clause stays in
- Updated: `test_predicate_does_not_use_server_clock` now covers a predicate body that contains no NOW() reference (the comment text was reworded to avoid the literal — comment originally tripped the test)

### 5. Tuple-shape invariant (tests/test_driver_queue.py)
`test_offer_ids_only_query_uses_canonical_gc_constants` updated to 14-tuple shape with two `reference_time` slots, plus a new assertion: `params[1] == params[7]` ensures both slots bind the same `_now()` capture (catches a future regression where someone accidentally calls `_now()` twice instead of using the passed-in reference_time argument).

---

## The P0 discovered (and why we stopped)

### Today's drive — outcomes

| offer | pickup | dropoff | classification |
|---|---|---|---|
| 7848 | ❌ MISSED | ✓ 18:38:48 | Houston Miss (driveway pickup, residential intersection) |
| 7850 | ❌ MISSED | ❌ MISSED | Total miss (apartment-curb pickup AND dropoff) |
| 7852 | ✓ 18:55:41 | ❌ MISSED | Calhoun Zombie |
| 7853 | ❌ MISSED | ✓ 20:14:20 | Houston Miss (strip-club dropoff fired via POI G2a) |
| 7854 | ❌ MISSED | ❌ MISSED | Total miss (in-progress when session started) |

**Pickup fire rate: 1/5 = 20%. Dropoff fire rate: 2/5 = 40%.**

### The 7850 forensic — what we found

7850 pickup at Bonhomme Rd was a clean apartment-curb stop. Rider was waiting, checked the license plate (8-15 second verification), got in. Total pause: **22.7 seconds at 0.0 mph, 254ft from the recorded pin** (consistent with "pin at building entrance, actual pickup at unit's nearest curb").

Heartbeat-by-heartbeat (`recon_7850_pickup_forensic_2026-05-12.sql`):

| time | mph | cluster_n | cluster_s | wai_type | wai_conf | on_target_road | current_road | motion | odo_elig | held_offers | planner | dispatched |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 18:42:26 | 0.0 | — | — | — | — | — | — | no_cluster | true | {} | (empty) | f |
| 18:42:31 | 0.0 | 3 | 10.8 | pickup | **0.85** | 1.00 | Bonhomme Road | transient | true | {} | (empty) | f |
| 18:42:37 | 0.0 | 4 | 16.9 | pickup | **0.88** | 1.00 | Bonhomme Road | transient | true | {} | (empty) | f |
| 18:42:43 | 0.0 | 5 | 22.5 | pickup | **0.90** | 1.00 | Bonhomme Road | closed | true | {} | (empty) | f |
| 18:42:48 | 0.0 | 6 | 28.1 | pickup | **0.93** | 1.00 | Bonhomme Road | closed | true | {} | (empty) | f |
| 18:42:54 | 3.3 | 7 | 33.8 | pickup | **0.94** | 1.00 | Bonhomme Road | moving | true | {7851} | (empty) | f |
| 18:43:00 | 3.3 | 8 | 39.4 | pickup | **0.94** | 1.00 | Bonhomme Road | moving | true | {7851} | (empty) | f |
| 18:43:05 | 1.6 | 9 | 45.0 | pickup | **0.94** | 1.00 | Bonhomme Road | closed | true | {7851} | (empty) | f |

Every signal is gold-plated:
- 25+ seconds at 0.0 mph
- Cluster grew 3 → 9 heartbeats over 45 seconds
- WAI confidence 0.85 → 0.94 (way above 0.40 threshold)
- WAI type = pickup, on-target-road = 1.00, current_road = Bonhomme Road (matches pickup address)
- Odometer gate eligible = true, reason = passed
- 7850 NOT in `gate_held_offer_ids`

But **`planner_action` is empty/null in every row.** `dispatch_executed` = false. `current_offer_id` never got set.

### What TAD did with this — the JSONB

`tad_decision_context` for 18:42:31 (and identical-shape for the rest of the pause):

```json
{
  "verdicts": {
    "7850": {
      "passed": null,
      "time_boost": 0.0,
      "time_signal": null,
      "distance_gate": {
        "mode": "lost_mode",
        "passed": null,
        "fail_reason": null,
        "lost_mode_reason": "narrative_blindness",
        "last_known_anchor_id": null
      },
      "leg_evaluated": "pickup",
      "lost_mode_reason": "narrative_blindness"
    },
    "7849": { ... same shape, declined offer being evaluated in case driver drove it anyway ... }
  },
  "committed": [
    { "offer_id": "7850", "commit_rule": "lost_floor_with_poi_lift" },
    { "offer_id": "7849", "commit_rule": "lost_floor_with_poi_lift" }
  ],
  "lost_mode": true,
  "last_known_anchor_id": null
}
```

**TAD did the right thing.** Lost_mode was active (old `_detect_lost_mode` returning True; refactor not yet deployed). TAD couldn't use anchor-based distance evaluation (`last_known_anchor_id: null`). TAD applied the lost-mode commit rules and listed 7850 in `committed` with `lost_floor_with_poi_lift` — which per the apply-script history in `tmp/apply_fix_b_poi_as_lifter.py` means "high-confidence PUDO recovery signal, POI matched, fire this."

The planner ignored it.

### Architectural intent (locked tonight)

Andrew + Gemini both confirmed:

> Lost mode is the search state. The whole purpose of being in lost_mode is to be hyper-receptive to recovery signals. A gold-standard PUDO observation in lost_mode should SNAP the system back to ground truth by firing the PUDO immediately. The recovery commit is the "I found a heartbeat!" signal — it MUST fire.

Any other interpretation keeps the system in dispatch blackout while collecting perfect evidence and doing nothing with it — which is exactly what happened today.

The answer is unambiguous: **TAD's `committed` entries in lost_mode are strong commits and must fire the PUDO immediately.**

### What we then discovered

Quick grep across production code (excluding `tmp/`, `tests/`, `venv/`):

- `tad_decision_context` — referenced only in `decisions/logger.py` (the WRITER), one stale comment in another file
- `committed` (as a TAD field) — zero production-code references
- `lost_floor_with_poi_lift` — zero production-code references
- `lost_mode` — appears in `_detect_lost_mode` itself (which COMPUTES it) and in tests; no production consumer that DECIDES based on it

**The recovery commit chain TAD produces has no production consumer.** It writes to the forensic column and stops there. The planner reads `verdicts.X.passed`, sees null, makes no decision.

This is not a "tune the planner" bug. This is a missing arch — the lost-mode recovery execution path. Whether it never got built, or got removed in the state-machine demolition (CANONICAL_RULES.md §XIV), needs investigation.

---

## What's needed next session

### Phase 1 — Recon the planner code

Find:
1. **Where the planner consumes TAD output.** It does read SOMETHING from `pudo_decision_context` or the in-memory equivalent — `planner_action`, `planner_target_state`, `planner_reason` get populated in normal-mode rows. Where does that come from?
2. **What the planner does with `verdicts.X.passed = null`.** Probably just skips. Confirm.
3. **What governs `current_offer_id` writes.** This is the "system has committed to an offer" signal. Per CANONICAL_RULES.md §VI, it's the 1-bit memory — set on fire_pickup, cleared on fire_dropoff. Today, it was NEVER set for any missed PUDO.

Entry points identified tonight (from `decisions/`):
- `engine.py::run_decision_engine` (line 8)
- `triangulation_enricher.py::enrich_with_triangulation` (line 39)
- `router.py::make_decision` (line 280)
- `logger.py::log_decision` (line 24) — the WRITER, not a consumer

The brief and CANONICAL_RULES.md §VIII reference a 4-Box Controller pattern (Monitor/Diagnose/Plan/Execute) and mention `pudo_planner.consume()` but the codebase may have evolved. Confirm current architecture.

### Phase 2 — Design the recovery path

Per the locked intent: when `tad_decision_context.committed` contains an entry AND `lost_mode = true`, the planner MUST treat this as a strong fire commit, equivalent to `verdicts.X.passed = true` in normal mode.

Design questions:
- Where does the commit-consumer live? Probably in the same path that reads `verdicts.X.passed` today.
- What's the write path? Setting `current_offer_id` is the canonical "this offer is now committed" signal — does TAD's commit need to go through `current_offer_id` setting, or through `actual_pickup_at`/`actual_dropoff_at` population, or both?
- What about commits during normal mode (`verdicts.X.passed = true`)? Same path? Or different?
- How do dropoffs vs pickups differ in the commit path? Probably the leg_evaluated field tells us.

### Phase 3 — Verify against today's data

Once the recovery path is built, replay 7850 pickup with the new code against the real heartbeats. Confirm:
- Same evidence (cluster 9, wai_conf 0.94, lost_mode true, committed contains 7850)
- New planner decision: `planner_action = fire_pickup` or equivalent
- `actual_pickup_at` would have been set to 18:42:31 (first heartbeat where cluster + WAI + lost_floor_with_poi_lift commit aligned)

A Houston Playback v2 covering the planner-side fire path is the natural regression net.

---

## Deployment status

**Cloud Run still serving `00604-2rs` (pre-tonight code).** Per tonight's plan:
- Review-then-deploy in daylight
- Tonight's predicate changes are committed (`4ae289b`) but not deployed
- The new P0 finding (planner doesn't consume commits) is INDEPENDENT of tonight's commit — that bug exists at `00604-2rs` and will continue to exist after tonight's commit deploys

The deploy of tonight's commit will:
- Make `_detect_lost_mode` use horizon physics (FIXES the over-eager lost_mode trigger that polluted heartbeats for 2 hours after dropoff fired)
- Add the Causality Guard to the predicate (production NOW() makes this a no-op; replay benefits)
- NOT fix the planner gap (the missing recovery commit consumer)

So deploying `4ae289b` is safe and reduces one failure mode (poisoned heartbeats from old lost_mode rule), but doesn't restore Auto Nail It reliability. **The planner fix is the actual P0 for Auto Nail It restoration.**

---

## Quick-start for next session

```
1. Open with this brief + CANONICAL_RULES.md + session_protocol.md
2. Confirm state:
     cd ~/puddlejumper-prod && git log --oneline -3
     # should show 4ae289b at HEAD
     python3 -m pytest tests/ 2>&1 | tail -5
     # should show 604 passed, 5 skipped
3. Phase 1 recon — read the planner:
     - decisions/engine.py::run_decision_engine
     - decisions/triangulation_enricher.py::enrich_with_triangulation
     - decisions/router.py::make_decision
     - The current write-paths that set planner_action / current_offer_id
4. Find the verdict-consumer. Trace from "TAD produces verdict" to "planner_action gets set."
5. The gap in that chain — where lost_mode commits should fire but don't — is the bug to fix.
```

---

## Process notes

### What worked tonight
- Recon-first discipline. Each predicate decision was grounded in a SQL query against real data, not theorizing.
- Idempotent apply scripts via `create_file` → `present_files` → scp. Three apply patches landed cleanly; the one false-positive sentinel (Causality Guard count = 2) was caught and reasoned through without damage.
- Paired-programming cycle held. Claude proposed, Gemini reviewed, Andrew ratified, then execute. The Causality Guard architectural decision and the KILL SHOT timestamp choice both went through this loop with clean outcomes.

### What to flag for future me
- **Sentinel sweeps must extract the specific scope before counting.** The Causality Guard's first sentinel counted occurrences across the whole file and got a false positive because the comment text matched. The fix patch's sentinel correctly used regex to extract just the predicate body before counting. Default to scope-extraction.
- **The handoff timeline was wrong about 7846/7847.** They're dev exercises Andrew ran before driving. Treating them as real offers caused initial test design confusion. Always confirm dev-vs-prod data origin before encoding it into tests.
- **L-22 paste corruption did NOT bite tonight.** Two false alarms (one in patch script execution, one in the dict-cursor diagnosis) turned out to be real findings, not display-layer artifacts. The lesson holds (chat-rendered output is not ground truth) but tonight was clean.

### Open architectural rules that landed but aren't documented
- Causality Guard belongs in CANONICAL_RULES.md §H ("Wall-Clock GC Predicate"). Should be added as an amendment.
- The `params[1] == params[7]` invariant in `live_offer_predicate_params` — same place.
- The lost-mode recovery commit semantics (when TAD's `committed` list MUST fire) — needs CANONICAL_RULES.md entry once the planner fix lands.

### Files / state to be aware of
- Apply scripts from tonight (in `tmp/`, gitignored):
  - `apply_causality_guard_2026-05-12.py`
  - `apply_causality_guard_fixups_2026-05-12.py`
  - `apply_playback_row_fix_2026-05-12.py`
  - `patch_dictrow_access.py`
- Recon SQL from tonight (in `tmp/`):
  - `recon_pudo_pauses_2026-05-12.sql`
  - `recon_pudo_pauses_v2_2026-05-12.sql`
  - `recon_7850_pickup_forensic_2026-05-12.sql`
- The full 7850 forensic output is in `/tmp/7850_forensic.txt` (will not survive VM reboot, copy if needed)
