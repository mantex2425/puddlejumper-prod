# Phase 2c.2 Sprint — State Handoff (2026-05-09 Second Session, Post-Hotfix Validation)

**Branch:** `phase-2c-2-tad-exit-4tools` @ commit `2e6a974` (push to origin: confirmed)
**Production deploy:** `puddlejumper-api-00598-hvx`, 100% traffic, deployed 2026-05-09 ~17:30 UTC
**Pytest floor:** 558/558
**Session arc:** Drive review → cluster-formation defect surfaced → root cause traced to `_match_poi_stub` missing `pois` kwarg → contract regression test authored → deploy → validation drive → end-to-end success

---

## Read order at session start

1. **This document** — operative state and full diagnostic chain
2. `docs/SPRINT_STATE_HANDOFF_2026-05-09.md` — first session of 2026-05-09 (resurrection patch). Tonight's session investigated the post-resurrection drive that handoff produced.
3. `docs/PHASE_2C_2_SPRINT_BIBLE.md` — original spec
4. `docs/SESSION_PROTOCOL.md` — paired-programming workflow, paste hazards, lessons
5. `docs/CANONICAL_RULES.md` — eternal architectural rules

---

## Recon at session start

```bash
cd ~/puddlejumper-prod && git log --oneline f3f5dc9..HEAD | head -10
source venv/bin/activate && python3 -m pytest --tb=short -q 2>&1 | tail -5
```

Expected: top commit `2e6a974` (matcher contract hotfix), preceded by `fd76356` (prior handoff doc) and `fb56175` (resurrection patch). Pytest **558/558 passing**.

---

## What landed this session

### Hotfix — `_match_poi_stub` missing `pois` kwarg (commit `2e6a974`)

**Production impact pre-fix:**
- 50+ `TypeError: _match_poi_stub() got an unexpected keyword argument 'pois'` errors in 75-minute drive window (2026-05-09 16:00-17:15 UTC)
- Each TypeError → Flask 500 → Android exponential backoff → heartbeat cadence collapsed from healthy ~5s to bimodal ~5s/~60-120s
- Sparse heartbeats starved `cluster_detection.detect_cluster`'s `min_samples=3` in `window_sec=60` gate
- `motion_gate_result` returned `no_cluster` for 51 consecutive minutes including 20+ minutes of stationary heartbeats at home (29.50628/-95.50231)
- Zero PUDO commits despite ACCEPTed offers in queue
- **Single bug, three production failure modes**

**Root cause:** `where_am_i.py:1700` dispatches every matcher with `pois=cluster_pois`. `_CLASS_DISPATCH` has 5 entries; 4 accepted `pois`, the 5th (`_match_poi_stub`) didn't. Pre-resurrection, this was latent (POI service swallowed exceptions, cluster_pois was always [], but actually the bug WOULD have fired if dispatch had been reached — it stayed latent because cluster=None early-exits in `_evaluate` upstream of dispatch were happening on every heartbeat). The resurrection patch (commit `fb56175`) fixed the upstream POI service exception, populated cluster_pois with real values, dispatch became reachable, TypeError fired.

**Fix:**
- `where_am_i.py:1085` — `_match_poi_stub` signature gains `pois=None` kwarg. Body unchanged. Docstring updated with contract reason and L-6 corollary framing.
- `tests/test_where_am_i.py` — new `TestClassDispatchContract` class with one parametrized test that asserts every `_CLASS_DISPATCH` entry accepts `(cluster, topo, target, pois=...)`. Real `Cluster`/`RoadTopology`/`TargetSpec` fixtures via existing `_cluster()`/`_topo()`/`_target()` factories — explicitly NOT MagicMock (would silently accept any signature). Strong inline commentary documenting the bug story so future-Claude doesn't strip the "redundant" test.

Tests +1 (557 → 558). L-3 envelope: byte deltas matched expected exactly (`+580` on `where_am_i.py`, `+3370` on test file). Read-back verified, sentinels confirmed, push clean.

### Diagnostic chain (the work that produced the fix)

This session's value was less in the LOC shipped and more in the diagnosis. The chain that surfaced the bug:

1. **Drive review** — accepted offer 7780/7781 (Davenport → County Road 79). Ride completed in real life, no `actual_pickup_at` populated. Offer 7787 had `expected_pickup_distance=283.94` vs actual cumulative ~11.5mi.
2. **Math accumulation** — every offer 7779-7787 satisfied `this.expected_pickup_distance = prev.expected_dropoff_distance + this.pickup_miles` to the cent. **Confirmed Gemini's first hypothesis (Python state leak) was wrong**; carry was via DB SELECT in `decisions/logger.py:99-100` picking any prev_offer regardless of disposition. This is the TAD anchor GC defect from prior handoff.
3. **Cluster formation defect** — pdc rows showed `motion_gate_result=no_cluster` for 51 minutes including 20 minutes stationary at home where a cluster had formed cleanly 30 minutes earlier. Localized failure deeper than the GC defect.
4. **Heartbeat cadence bimodal** — 172/203 healthy <10s, 31/203 in 60-120s gap range. Cadence independent of motion state (median 5.7s stopped, 5.4s moving) — ruled out Android power-save throttling.
5. **Server error correlation** — every gap_start timestamp correlated within seconds with `_match_poi_stub` TypeError in Cloud Run logs. 50 errors in 75 minutes ≈ 1 per 90s, exactly matching gap cadence.
6. **Single-bug explanation** — the TypeError caused 500 responses, Android backoff produced bimodal gaps, gaps starved cluster detection, no clusters meant no dispatch, no commits.

Two false leads were rejected along the way:
- **"Python state leak in `decisions/logger.py`"** (Gemini's initial hypothesis) — eliminated by SQL inspection showing cross-offer DB chaining.
- **"Move cluster detection to the device"** (tempting after seeing cadence collapse) — premise was that cadence couldn't be fixed server-side; the data showed cadence was being collapsed BY a server bug. Fix the server bug, cadence recovers, server-side detection works. Device-side detection deferred indefinitely (creates two definitions of "where the driver is" — exactly the smell `cluster_detection.py`'s docstring exists to prevent).

### Validation drive (post-deploy) — full success

Drove a short loop: home → red light at (29.51109, -95.52579) → dentist at (29.51036, -95.52725) → home. ~10 minutes total.

| Validation goal | Result |
|---|---|
| Heartbeat cadence recovery | ✓ 96% healthy <10s gaps (110/116). 5 bad gaps all in stationary-at-home idle period before drive started. |
| Cluster formation at non-home location (red light) | ✓ Cluster size 7 / duration 34s / `closed` verdict at (29.51109, -95.52579). |
| Cluster formation at non-home location (dentist) | ✓ Cluster size 7 / duration 33s / `closed` verdict at (29.51036, -95.52725). |
| POI lookup at non-home location | ✓ `poi_cache` row 17 written for red light (poi_count=0, expected — intersection has no businesses), row 18 written for dentist (poi_count=16, real Google return). |
| Cluster reformation at home post-drive | ✓ Cluster formed within 22s of arrival. Sequence size 3→11 with motion_gate transitioning `transient`→`closed`→`moving`→`closed` as expected. |
| Cloud Run errors on new revision | ✓ Zero. 20+ minutes serving production with no errors of any class. |
| Matcher dispatch fix exercised | ⚠ Not validated — no Uber offers came through during the test loop. Contract test in pytest validates it; production absence of TypeErrors is operational confirmation; next real driving session with offers will be the natural validation. |

---

## Issues observed but not blocking

### 1. Red light produces `closed` cluster verdict

The 17:42:55-17:43:18 cluster at the red light reached `motion_gate_result=closed` after 22 seconds of stationary heartbeats. Motion gate's `MOTION_MIN_DURATION_S=20.0` doesn't distinguish "stopped at light" from "stopped to drop off passenger." This is the false-positive failure mode the **dual-leg odometer gate** is designed to catch (pickup leg requires cumulative_miles ≥ 0.9 × pickup_miles before pickup commit fires).

With no offer in queue tonight, the odometer gate had nothing to enforce against. With an offer in queue, the odometer gate would have rejected the red-light cluster because the driver hadn't traveled far enough toward the pickup yet.

**Architectural verdict:** correct. The multi-gate design is necessary, and tonight's data confirmed why motion gate alone is insufficient. No fix needed.

### 2. Stationary-at-home heartbeat cadence drops

Pre-drive (17:31:15 → 17:39:30, idle at home before validation loop), heartbeats arrived every 105-180s. Once driving started, cadence recovered to ~5s immediately. This is real Android power-save behavior, distinct from the TypeError-induced backoff we eliminated.

**Production impact:** When arriving at a destination after a long stationary period elsewhere, cluster formation could be delayed. Tonight's data showed it doesn't prevent formation — the home-arrival cluster formed within 22 seconds because the drive-home heartbeats were dense (5s cadence) and immediately produced the 3 samples needed.

**No fix needed for launch.** Worth tracking. If post-launch we see "first arrival after long idle" failures, the candidate fix is Android-side: `setInterval(5000)` and `setFastestInterval(5000)` on the `LocationRequest`, or a foreground service holding regular GPS pings while the driver is on-shift.

### 3. Forensic wiring gaps remain

Per prior handoff, `pudo_decision_context` columns `wai_status`, `wai_pudo_type`, `wai_offer_id`, `wai_target_address`, `wai_confidence`, `wai_reason`, `poi_lookup_source`, `poi_match_score`, `poi_top_names` are still hardcoded NULL in production (no writer). Tonight's PDC rows confirm: every cluster row has cluster_lat/lng/size/duration/motion_gate_result populated, but the wai_* and poi_* family is uniformly NULL.

**This blocks intelligent prosecution of the TAD anchor GC fix** — if the GC fix lands and a commit fails to fire, we won't know whether TAD held it back, the matcher returned low confidence, POI matching failed, etc. Per Gemini's "Never perform surgery while the monitors are showing None" framing, this is the next-session priority.

---

## What's NOT working in production

Same as prior handoff plus an update:

### Auto-nailer still has not fired on a real ride

`offer_history.actual_pickup_at` and `actual_dropoff_at` are NULL for every recent row. **However, the cause has shifted:**

- Prior handoff cause: TAD anchor GC defect (broken SELECT picking accumulated prev_offer)
- Tonight's discovery: TypeError preventing dispatch from running at all

Tonight's hotfix removed the TypeError. The TAD GC defect remains. Until we drive with offers in queue post-hotfix, we don't know whether the GC defect alone is sufficient to prevent commits, or whether there are further layered defects.

**Prediction:** with the TypeError gone, a real ride will produce more interesting data than "all NULL." Either commits will fire on the first ride (TAD GC was a non-issue once the TypeError was removed), or commits will still not fire and we'll have populated forensic columns showing exactly what TAD said. Either outcome moves the sprint forward.

### TAD anchor GC defect (unchanged from prior handoff)

Still real, still in `decisions/logger.py:88-105`:

```sql
WHERE dl.driver_id = %s
  AND oh.expected_dropoff_arrival_time IS NOT NULL
ORDER BY oh.created_at DESC
LIMIT 1
```

Picks most-recent prior offer regardless of disposition. Andrew's three-rule fix design (declined offers don't anchor / expired ACCEPTs don't anchor / completed rides don't anchor) is ratified pending Gemini final review.

Proposed fix shape:
```sql
WHERE dl.driver_id = %s
  AND dl.decision_result->>'verdict' = 'ACCEPT'
  AND oh.actual_dropoff_at IS NULL
  AND (
    (oh.actual_pickup_at IS NULL AND oh.expected_pickup_arrival_time > NOW())
    OR (oh.actual_pickup_at IS NOT NULL AND oh.expected_dropoff_arrival_time > NOW())
  )
ORDER BY oh.created_at DESC
LIMIT 1
```

---

## Architecture work shelved (unchanged from prior handoff)

`poi_cache.query_radius_m` schema column, API failure cooldown, `API_SEARCH_RADIUS_M` 50→150 bump. Re-trigger conditions still apply.

**One update from tonight's drive:** `poi_cache` row 18 returned 16 POIs from the dentist at the current 50m radius. Suggests 50m may be adequate for genuinely commercial locations even though the 2026-05-06 audit data argued for 150m. Defer the radius bump until we observe a missed POI at production drive locations post-launch.

---

## Validated artifacts from this session

**Cloud Run deploy:** revision `puddlejumper-api-00598-hvx`, deployed 2026-05-09 17:30:33 UTC

**poi_cache rows seeded:**
- ID 17: red light at (29.51109, -95.52579), poi_count=0 (correct — intersection)
- ID 18: dentist at (29.51036, -95.52725), poi_count=16 (real Google return — first non-home, non-empty POI lookup)

**pytest:** 558/558 passing (was 557 + 1 new contract test)

**Memory updates:** none required tonight. Prior session removed the stale `gpsAgeSec` TODO already.

---

## Next session priority order (updated)

Reordered per Gemini's "observability before surgery" framing:

1. **Forensic wiring fix** — populate `wai_status`, `wai_pudo_type`, `wai_offer_id`, `wai_target_address`, `wai_confidence`, `wai_reason`, `poi_lookup_source`, `poi_match_score`, `poi_top_names` from `WhereAmI.evaluate()` outputs and `cluster_pois` results. Read existing PDC writer in `driver_heartbeat.py:632-731` to find current None-bindings; wire each from the appropriate `diagnostics` field. This is plumbing, not algorithm work — should be a single focused sprint.

2. **Validation drive with full forensics** — drive normal driving session with Uber offer flow. Three goals satisfied at once: validates tonight's hotfix exercises matcher dispatch successfully (currently unvalidated since no offers came through tonight), populates forensic columns with real production telemetry, and gives us positive control data we never had before.

3. **TAD anchor GC fix** — with full forensic visibility from #2, prosecute the SQL filter fix in `decisions/logger.py:88-105` against real prev_offer chains. Andrew's three-rule design is ratified pending Gemini final review. Tests +3-5 (idle, stacked-pre-pickup, stacked-mid-ride, expired-accept cases).

4. **Contract audit on `tad.py` and `bead_on_wire`** (per Gemini recommendation) — replicate the `TestClassDispatchContract` pattern for any other dispatch surfaces. Candidates: `tad.evaluate_tad_gate` call site (6 kwargs, brittle), `bead_on_wire.compute_target` dispatch, `decisions.dispatch` action handler routing. Tests-only sprint, no behavior change.

5. **Re-evaluate shelved architecture work** — with real drive data in hand post-validation, decide on cache-coordination, cooldown, and radius-bump triggers.

6. **Sprint exit criterion review** — original Bible criterion + updated post-launch criteria from prior handoff. Verify current state against full criterion set.

---

## Lessons captured

### L-22 update: layered defect unmasking

Tonight's bug stayed latent for >7 days because an upstream defect was hiding it. When the resurrection patch (`fb56175`) fixed the upstream POI service exception, the previously-unreachable matcher dispatch path became reachable, surfacing the TypeError.

**Proposed canonical rule (request Gemini ratification):**

> **L-23: Layered defect unmasking.** When a fix lands on a system with multiple latent bugs, expect previously-unreachable code paths to surface their own bugs immediately. Post-deploy validation should specifically exercise paths that were dormant pre-fix, not just verify the fix's local correctness.

Practical application: after every "fix that touches a guard or early-exit," construct the validation specifically to traverse the path that was previously short-circuited. Tonight's contract test does this for the `_match_poi_stub` path; future fixes of similar shape should add similar tests.

### L-6 corollary reinforced

Bug entered via the same failure mode as the resurrection patch's bug: a contract change at one site without grepping all dispatch entries' signatures. Tonight's `TestClassDispatchContract` locks the matcher dispatch contract. Per Gemini's recommendation, this pattern should replicate to other dispatch surfaces.

### Gemini's "monitors showing None" framing

> "Never perform surgery while the monitors are showing None."

Adopted as the rationale for inverting next-session priority (forensic wiring before TAD GC). When debugging a multi-gate system, the cost of being blind to which gate held a decision back is far higher than the cost of an extra sprint to wire the diagnostic columns.

---

## Operational reminders (unchanged)

- **Test floor: 558/558.** Every change must verify against full pytest run.
- **L-8:** `\d <table>` before any SQL referencing columns. Pre-modification discipline applies to schema work, not just code edits.
- **L-3 envelope discipline:** every apply script in `~/puddlejumper-prod/tmp/`, never `/tmp/`.
- **Paste hazards (L-22):** chat-display linkification of `[name.py](http://name.py)`. File transfer via `create_file` → `present_files` → Andrew's local-then-scp pattern.
- **Paired programming:** Claude proposes → Gemini reviews → consensus → Andrew runs. Tonight's session caught one Gemini hypothesis miss (Python state leak) early via SQL inspection — pattern healthy.

---

## Useful queries for next session

After forensic wiring fix lands, validate population:

```sql
SELECT
  COUNT(*) AS pdc_rows,
  COUNT(wai_status) AS wai_status_populated,
  COUNT(wai_confidence) AS wai_confidence_populated,
  COUNT(wai_target_address) AS wai_target_populated,
  COUNT(poi_lookup_source) AS poi_source_populated,
  COUNT(poi_match_score) AS poi_score_populated
FROM app_private.pudo_decision_context
WHERE driver_id = 'UjT1hE9eBXh2q95aSZYOkzDJ8lo1'
  AND created_at > '<post-wiring-deploy-timestamp>';
```

Expected post-wiring: every column equals pdc_rows count (or close — some columns legitimately null when conditions aren't met, e.g. wai_target_address NULL when no offers in queue).

After TAD GC fix, validate sane expected_* anchors:

```sql
SELECT id, miles_at_offer_receipt, pickup_miles, expected_pickup_distance,
       (expected_pickup_distance - miles_at_offer_receipt - pickup_miles) AS anchor_drift
FROM app_private.offer_history
WHERE created_at > '<post-gc-deploy-timestamp>'
ORDER BY created_at DESC
LIMIT 20;
```

Expected: `anchor_drift` ≈ 0 for idle-case offers, non-zero only for genuinely stacked offers.

---

End of handoff.
