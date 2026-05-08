# Phase 2c.2 Sprint — Complete State Handoff (2026-05-08, ~22:00 UTC)

**Branch:** `phase-2c-2-tad-exit-4tools` @ commit `88c04e8`, 18 commits ahead of `f3f5dc9`
**Production deploy:** `puddlejumper-api-00596-z7x` (100% traffic, healthy startup)
**Pytest floor:** 552/552 passing
**Status:** Sprint ~80% complete. Item 3d unbuilt. Path-1 session odometer work is blocking the test-drive validation but is independent of finishing the sprint code.

---

## Quick recon at session start

```bash
cd ~/puddlejumper-prod && git log --oneline f3f5dc9..HEAD
source venv/bin/activate && python3 -m pytest --tb=short -q 2>&1 | tail -5
```

Expected: 18 commits ahead of `f3f5dc9`, top commit `88c04e8`. Pytest 552/552.

---

## The 18 commits, in order

```
88c04e8 alpha-patch: realign _execute_action SQL to offer_history.id semantics
95f037f phase 2c.2 item 3b.R: heartbeat reader activates dual commit rule
6b55357 docs: phase 2c.2 item 3b.W complete + resume brief for 3b.R + 3d
1fab701 phase 2c.2 item 3b.W tests: TestExpectedAnchorBindings (8 tests)
673ff12 phase 2c.2 item 3b.W: writer-side expected_* anchors at offer-receipt
d208073 deploy: cut Cloud Run cost (1 CPU, 1Gi mem, scale-to-zero, throttle)
733502d docs: phase 2c.2 item 3b + 3d handoff for next session
9cff775 phase 2c.2 brain tests: item 3 dual commit rule + classify_commit_rule
07a003e phase 2c.2 brain: item 3a + 3c + 3e
949fff4 docs: phase 2c.2 item 3 handoff for next session
9c326fc phase 2c.2 brain: head 4 + patch 2c (poi witness signals)
1d84248 docs: phase 2c.2 sprint bible amendment for lost mode + inferred dropoff
a77c211 docs: phase 2c.2 sprint handoff for next session
de2ba5d phase 2c.2 brain: tad.py Part 2 + Lost Mode (evaluate_tad_gate)
61094d0 phase 2c.2 brain: tad.py Part 1 (compute_offer_expectations)
386ba3f phase 2c.2 prereq: extend Offer with pickup_minutes/trip_minutes
7a236a3 schema: phase 2c.2 migration applied to prod
5be275e docs: phase 2c.2 sprint bible (operative spec, supersedes kickoff)
```

There may also be a 19th commit (this very handoff doc, if Andrew committed it before opening the new chat).

---

## What's live in production

The full WAI brain pipeline is wired and running. Every heartbeat that reaches the new revision exercises:

1. Cluster detection → topology → stop context → memory eye
2. Map step (candidate generation from queue)
3. **Step 4.5: TAD bouncer** (Phase 2c.2 Items 3a/3c/3e) — filters candidates by Bible Rule 7's tristate verdict
4. Step 5 dispatch (per-class matcher, skipped on TAD `passed=False`)
5. Step 6 dual commit rule (Bible Rule 3a Normal Mode / 3b Lost Mode)
6. Forensic blob serialization to `pudo_decision_context.tad_decision_context`

Real-world test trip on 2026-05-08 (driver `UjT1hE9eBXh2q95aSZYOkzDJ8lo1`) confirmed:
- 900 heartbeats during the trip
- 146 wrote `tad_decision_context` JSONB blobs
- All 146 recorded `passed: false, mode: "missing_state"` — **defensive design firing exactly as designed**
- Zero FirePickup, zero FireDropoff fires

The system is operating *correctly given broken inputs.* The architecture works. The blocker is upstream: `miles_at_offer_receipt` is NULL on every offer_history row, which cascades to NULL `expected_*` columns, which causes 3b.R to exclude every offer from `per_offer_state`, which produces `missing_state` TAD verdicts.

---

## What's deferred (architectural decisions made; work not done)

### A. Path-1 session odometer (BLOCKING test-drive validation)

**Decision ratified 2026-05-08** (Andrew + Gemini):

The Android `cumulativeMiles` field in `AutoNailItManager` is a per-leg odometer (resets at pickup fire, dropoff fire, state transitions, coord changes). It satisfies the motion gate's per-leg ratio check but **cannot** satisfy Bible Rule 6's session-cumulative odometer contract that TAD distance gates and chain math depend on.

Path 1 = add a separate `sessionCumulativeMiles` accumulator on Android that resets ONLY at app launch. Both odometers coexist; both flow to the backend.

**Android-side status:** Claude Code applied 6 edits (AutoNailItManager.kt:115/329/414 + DTOs + ScreenshotMonitorService.kt). Andrew installed via Claude Code's adb pipeline and drove a test trip. Backend confirms the new field DOES NOT appear in any heartbeat JSONB. A Claude Code debug brief was authored to diagnose serialization vs. cached-process vs. second-heartbeat-path. **Resolution is on Android side.**

**Backend-side status:** Waiting. ~5-line change spec already documented in `docs/PHASE_2C_2_ITEM_3B_R_AND_ALPHA_COMPLETE_2026-05-08.md` section "Backend wiring (waiting on Android — exact spec)". Will land as one focused apply script (`tmp/apply_path_1_backend.sh`) the moment Andrew confirms the field arrives in heartbeat JSONB.

### B. Item 3d (inferred-dropoff event table) — NOT YET BUILT

The Bible Rule 8 mechanism for stacked-ride narrative recovery. **Design is fully ratified** (Gemini, 2026-05-08). Schema:

```sql
CREATE TABLE app_private.inferred_dropoffs (
    offer_id                  bigint PRIMARY KEY REFERENCES app_private.offer_history(id),
    inferred_at               timestamptz NOT NULL DEFAULT NOW(),
    lat                       double precision NOT NULL,
    lng                       double precision NOT NULL,
    source                    text NOT NULL
                              CHECK (source IN ('cluster_centroid', 'gps_fallback')),
    source_pdc_id             bigint REFERENCES app_private.pudo_decision_context(id),
    trigger_decision_log_id   integer NOT NULL REFERENCES app_private.decision_log(id),
    created_at                timestamptz NOT NULL DEFAULT NOW()
);
```

Writer: `_check_inferred_dropoff(cur, driver_id, fired_pickup_offer_id)` helper, called from `post_heartbeat()` after a successful FirePickup execution. Reads `pudo_decision_context.cluster_lat/cluster_lng` for primary location source (Option a, ratified — re-clustering raw heartbeats was rejected). Falls back to `heartbeat_log` for `gps_fallback` source.

Implementation breakdown:
- Migration file: `migrations/2026-05-08_phase_2c_2_inferred_dropoffs.sql`
- Writer + helper SQL queries (3 of them — find ride A, find primary cluster, find fallback GPS) in `driver_heartbeat.py`
- Tests: ~5-8 tests for trigger conditions, primary vs. fallback, no-fire cases
- Estimated work: one design ratification round (already done) + one apply script + one short feedback loop on test failures.

**Why not done yet:** the alpha-patch discovery in mid-3d-recon stopped the line. After alpha-patch shipped, we wanted to validate the deploy via a test drive before extending the fix surface further. The test drive then surfaced the session-odometer issue, which required Path-1 ratification, which surfaced an Android serialization issue. None of that touches 3d's correctness — it's just queue-position behind the odometer chain.

### C. Two minor cleanup commits identified during forensics

1. **`_get_last_known_anchor_id` needs a recency filter.** Currently returns the oldest historical anchor in the DB if no recent ones exist. The 2026-05-08 trip showed it returning offer `7136` from before any 3b code shipped — a stale forensic artifact. Add `AND oh.created_at > NOW() - interval '24 hours'` to the SQL. One-line fix in `driver_heartbeat.py`.

2. **Lost Mode firing on stale data.** The 2026-05-08 trip showed `lost_mode: true` on every blob because `_detect_lost_mode` found an in-window orphan from before deploy. Likely resolves itself once `expected_*` columns start populating in real time. Worth a focused observation pass after the Android fix lands; if it persists, separate diagnostic.

### D. Option β (full identity-crisis architectural realignment) — DEFERRED FUTURE SPRINT

See `docs/IDENTITY_CRISIS_TECHDEBT_2026-05-08.md` for the audit. 6 prerequisite recon questions must be answered before β can safely proceed. Not in scope for finishing this sprint.

---

## Exact work remaining to call this sprint complete

In strict dependency order:

### Step 1 — Path-1 Android resolution (Claude Code's territory)

**Status:** in flight. Claude Code has the debug brief asking it to verify:
1. Whether the new APK is what's actually running (`adb shell dumpsys package` + `am force-stop` if needed)
2. Whether there's a second heartbeat code path that wasn't found in original recon
3. Whether kotlinx serialization is silently omitting the field due to `encodeDefaults = false` config

**Andrew's job here:** ping Claude Code, get the answer, fix on Android side, rebuild, sideload, drive briefly.

**Definition of done:** the following query shows a non-null `session` column:

```bash
psql -h 10.128.0.2 -U postgres -d puddlejumper -c "
  SELECT heartbeat->>'cumulative_miles' AS per_leg,
         heartbeat->>'session_cumulative_miles' AS session,
         heartbeat_at
  FROM app_private.driver_trip_state
  WHERE driver_id = 'UjT1hE9eBXh2q95aSZYOkzDJ8lo1'
  ORDER BY heartbeat_at DESC LIMIT 1;"
```

### Step 2 — Backend wiring of session_cumulative_miles

**Status:** ready to author. Spec lives in `docs/PHASE_2C_2_ITEM_3B_R_AND_ALPHA_COMPLETE_2026-05-08.md`.

Work:
1. Author `tmp/apply_path_1_backend.sh` (L-3 envelope)
2. Touches `decisions/router.py` (~3 lines: read new field, rename local, update params dict) and `driver_heartbeat.py` (~3 lines: read body field, change current_odometer source, update JSONB blob serialization)
3. Add 2-3 unit tests confirming router reads new field and post_heartbeat passes correct value into WAI brain
4. Pytest target: 552 → ~555
5. Apply, commit, push, deploy

**Definition of done:** new offer_history rows show `miles_at_offer_receipt` populated, and tad_decision_context blobs start showing real `passed: true` / `passed: false` (not `missing_state`) verdicts.

### Step 3 — Item 3d (inferred-dropoff event table)

**Status:** design ratified, not built.

Work:
1. Recon: confirm `_execute_action`'s FirePickup branch return path and exact line for the helper call insertion
2. Design ratification round (recap with Gemini before coding — schema is locked but the writer's SQL queries should get a fresh review)
3. Author migration: `migrations/2026-05-08_phase_2c_2_inferred_dropoffs.sql`
4. Author writer changes to `driver_heartbeat.py`: new `_check_inferred_dropoff()` helper + call site after FirePickup execution
5. Author tests: `tests/test_inferred_dropoffs.py` (~5-8 tests)
6. L-3 apply script
7. Apply, commit, push, deploy
8. Pytest target: ~555 → ~563

**Definition of done:** schema migration applied, writer code in tree, full test pass, deployed. Forensic verification is multi-shift (need to actually observe a stacked-ride scenario where ride A's dropoff doesn't confirm spatially).

### Step 4 — Two cleanup commits (one combined commit, optional)

**Status:** small, well-scoped.

Work:
1. Add recency filter to `_get_last_known_anchor_id` SQL (one line)
2. Observation pass on Lost Mode behavior (might be self-resolving; might need a separate `_detect_lost_mode` adjustment)
3. Single combined commit if both small; separate if Lost Mode needs more investigation

### Step 5 — Sprint completion report

Per `docs/PHASE_2C_2_SPRINT_BIBLE.md` "Sprint completion report" section, author `docs/PHASE_2C_2_SPRINT_REPORT.md` covering:

1. TL;DR
2. What landed (file-by-file commits)
3. Phase 1 / Phase 2 split
4. Test results (final pytest count vs. baseline)
5. Deviations from the Bible (alpha-patch was a major one — surface and explain)
6. Pre-existing oddities flagged but not addressed (link to `IDENTITY_CRISIS_TECHDEBT_2026-05-08.md`)
7. Operational state (final commit SHA, deploy revision)
8. Open decisions (Item 3d's persistence mechanism if not yet done; Option β for future sprint)
9. What's left

---

## Operational reminders for the new chat

- **Test floor: 552/552.** Every change verified by full pytest run.
- **Apply scripts: L-3 envelope, dynamic delta computation.** This session's L-2 arithmetic gates caught 4 delta-prediction errors (3b.R Patch 3, alpha-patch Patches 1/5/6) before any disk writes — working as designed. Be careful counting comment lines around SQL replacements; delta math is finicky around heredocs.
- **Paste hazards: L-22.** Chat-display linkification of `[name.py](http://name.py)` is real. Read past it. File transfer via `create_file` → `present_files` → Andrew's local-then-scp pattern is the only safe path for substantive code.
- **psql output: file-and-cat.** psql tabular formatting doesn't survive chat copy-paste. Always `> /tmp/recon.txt 2>&1` then `cat`.
- **Pre-modification discipline:** read the current code with `sed -n` before authoring any patch. Don't trust prior-session recon without verifying SHA-256.
- **Paired programming:** Claude proposes → Gemini reviews → consensus → Andrew runs. Don't author code until ratified.

---

## Where the test drive stands

Andrew has driven one real Uber trip on the deployed code. The TAD blobs from that trip prove 3b.R works end-to-end. The trip did not produce any FirePickup/FireDropoff fires because `expected_*` columns were NULL on offer_history (no `miles_at_offer_receipt` source).

Once Steps 1+2 land, a follow-up test drive should produce real PUDO commits via the dual commit rule. That's the moment the sprint's value becomes operationally visible.

---

End of comprehensive handoff.
