# Sprint A Findings Report — 2026-05-04

**Audience:** next-session Claude, Gemini paired-reviewer, future Andrew
**Status:** findings only; no design proposals
**Context:** session 2026-05-02 → 2026-05-04, post-housekeeping closeout

---

## Why this exists

The session began with eight commits of housekeeping (heartbeat_log restore, test harness, deploy fix, status observability, replay tooling, smoke test, voice tests, gitignore). It ended having identified that **G1 (the dropoff odometer gate) is not enforced anywhere in the live PUDO chain**, despite being documented as a locked rule in the canonical doc and userMemories. Several in-flight cleanup tasks (cluster threshold tuning, bead_on_wire deprecation marker) were paused mid-session as a result.

This document captures what's verified true, what's verified missing, what needs field data, and a forensic query to quantify the G1 gap before designing a fix.

---

## 1. What's verified true (the live PUDO chain)

End-to-end flow, derived from grep and code reading (not documentation):

```
offer card text
  → classify_address(text)                [bead_on_wire.py:164]
  → bucket ∈ {intersection, single_road, poi, street_number, garbage}
  → TargetSpec built with bucket as a field
  → _project_queue (driver_heartbeat.py)  → list[Offer]
  → WhereAmI(cur).evaluate_with_diagnostics(driver_id, queue)
        defaults: _recent_clusters_fn=get_recent_clusters
                  _adjacency_fn=get_adjacent_roads_for_cluster
  → per-bucket matcher (_match_intersection / _match_single_road / ...)
                                          [where_am_i.py lines 630, 651, ...]
  → confidence = _weighted_confidence(signals, _CONFIDENCE_WEIGHTS[bucket])
  → confidence ≥ WAI_CONFIDENCE_THRESHOLD (0.40) → WAIMatch
  → dispatch(matches, current_offer_id, queue_offer_ids)
        pure function, no I/O                [dispatch.py]
  → list[Action]
  → _execute_action loop                   [driver_heartbeat.py:540+]
  → side effects (write_nailed_position, current_offer_id update, voice)
```

### 1a. WAI confidence model is multi-signal and topology-aware

`where_am_i.py` lines 95–125 define per-bucket signal weights. Every matcher consumes seven independent signals:

| Signal | What it measures |
|---|---|
| `proximity` | distance from cluster centroid to geocode |
| `breadcrumb_match` | driver's recent road sequence matches the target road |
| `cluster_tightness` | spatial spread of the cluster (tighter = stronger) |
| `cluster_duration` | how long the driver has been stopped |
| `on_target_road` | snapped road equals the target road exactly |
| `off_wire_pivot` | arrival via parking lot (off named-road) |
| `adjacent_road_match` | cluster centroid near a road intersecting the target |

Per-bucket weights (each row sums to 1.0, marked TUNABLE pending shadow-mode data):

| Bucket | prox | breadcrumb | tight | dur | on_target | off_wire | adj |
|---|---|---|---|---|---|---|---|
| intersection | 0.10 | 0.30 | 0.15 | 0.10 | 0.20 | 0.05 | 0.10 |
| single_road | 0.05 | 0.35 | 0.15 | 0.15 | 0.15 | 0.05 | 0.10 |
| number_on_street | 0.30 | 0.20 | 0.15 | 0.10 | 0.15 | 0.00 | 0.10 |

This is a serious multi-signal scorer, not a thin proximity heuristic.

### 1b. Memory Eye / cluster_revisit is wired in production

`WhereAmI.__init__` defaults `_recent_clusters_fn=get_recent_clusters` (line 971) and `_adjacency_fn=get_adjacent_roads_for_cluster` (line 972). Production constructs `WhereAmI(cur)` at `driver_heartbeat.py:533` with no override — both defaults bind. The Houston Loop revisit gate (`_compute_cluster_revisit`, lines 830–1087) and the v2.6 amendment from Phase D are intact.

### 1c. Naked-list contract is intact (§XIV.A verified)

`WAIMatch` (`pudo_types.py:117`) is a frozen dataclass with exactly three fields: `offer_id: str`, `location_type: Literal["pickup", "dropoff"]`, `confidence: float`. No coordinates, no topology, no status labels leak into the return type. The downstream interpreter (heartbeat handler) reads coords from the cluster object and offer queue directly.

### 1d. Dispatch purity is intact (§XIV.B verified)

`dispatch.py` has zero `db`/`psycopg` imports, zero file or network I/O imports, zero `app_private.` table references, zero `heartbeat_log` reads. It's genuinely `(matches, current_offer_id, queue_offer_ids) → list[Action]`.

### 1e. Single execution site is intact (§XIV verified)

Only `driver_heartbeat.py:537` calls `dispatch()` in production. Other matches are gitignored patch scripts (excluded from `git status`).

---

## 2. What's verified missing

### 2a. G1 odometer gate is not enforced

`odometer_floor` is *produced* by `decisions/triangulation_enricher.py:98–114` (three tiers: 0.92, 0.88, 0.75), *logged* by `decisions/logger.py:145`, *displayed* in `driver_status.py` API response. **It is never read as a gate before firing a dropoff.** Grep across the entire codebase for `odometer_floor` use sites confirms producers/loggers/displays only — no consumers.

WAI has zero references to `pickup_miles`, `trip_miles`, `odometer`, or `cumulative_miles`. Dispatch has zero references. The heartbeat handler's `_execute_action` runs immediately after dispatch returns the action list, with no intervening odometer check.

**Production behavior today:** a `FireDropoff` action fires whenever WAI confidence ≥ 0.40 against any queue offer's dropoff geocode, regardless of how far the driver has actually traveled since pickup. Gemini's "ghost dropoff at the red light" scenario is the live behavior, not a hypothetical.

This has not produced observable customer-visible bugs *yet* because the failure mode requires:
- An active `current_offer_id`
- A queue offer whose dropoff geocode the driver happens to drive within ~80m of
- Cluster forms (≥3 samples, ≤10mph, ≤25m spread, ≤60s window)
- WAI confidence ≥ 0.40 — multi-signal, so usually requires breadcrumb / on_target_road support too

The multi-signal confidence model is what's been masking this. A red light 200m from a previous dropoff geocode with no breadcrumb match probably doesn't clear 0.40. But "probably" is not "doesn't," and the confidence threshold is 0.40 not 0.70 — close calls fire.

### 2b. G2a and G2b are not implemented

userMemories says "PUDO fire rule (locked Apr 25 2026): G1 odometer / G2a POI via Google reverse geocode / G2b residential intersection within 200m." Grep finds no implementation of either G2a or G2b. They appear to be design intent that never reached code.

G2a in particular would be relevant to the curbside-walkup-pickup question (POI adjacency could distinguish a curbside pickup at a known POI from a random stop). Currently both questions are answered solely by the multi-signal confidence model, with no second-gate check.

### 2c. bead_on_wire.py is half-live, half-dead

The file is misnamed and the original docstring (lines 1–22) describes a defunct "Blind Man with his hand on a rail" decider model from the BMOAR v2 era.

**Live primitives in this file** (used outside it):
- `classify_address(text)` — used by `driver_heartbeat.py:56`, `nail_it_core.py:1145`, `scripts/replay_pudo.py`, `smoke_test_wai_24h.py`. **This is the most load-bearing function in the file.** Its bucket output drives WAI's per-bucket matcher selection.
- `compute_target(...)` — called from `nail_it_core.py:1203` inside an ENROUTE-state legacy fallback. Dies with the state machine (Sprint A demolition Cuts B1–B3 follow-on).

**Dead today, no live caller** (safe to delete in a future cleanup commit):
- `single_road` and `poi` decider functions (the ones containing `detect_cluster()` calls at ~lines 572, 659)
- `snap_to_intersection` (only called by the dead deciders)
- `snap_to_road` (no callers anywhere)
- `odometer_miles_since` (only called by the dead deciders)

**Recommended cleanup, in order:**
1. Header rewrite documenting live vs. dead (paused this session)
2. Delete dead deciders + their helpers (separate commit)
3. Relocate `classify_address` to `address_utils.py` or a new `address_classification.py` (renaming commit)
4. Delete `compute_target` and the ENROUTE legacy block in `nail_it_core` once state machine collapse lands

---

## 3. Open empirical questions (need driving data)

### 3a. What does Android's heartbeat cadence look like during a 5-second curbside walkup?

The prior session's ride 7694 finding ("5-second curbside walkup, no cluster formed") was inferred from offer geometry, not observed in heartbeat data, because ride 7694 fell during the 14-hour heartbeat_log regression blackout (May 1 09:00 UTC → May 2 ~01:00 UTC). The May 1 hourly frame counts confirm: 9 hours of normal traffic (~640 frames/hour), then 15 hours of zero, then partial recovery.

**With heartbeat_log restored (revision 00587-c6b deployed this session), a fresh ride is needed to observe actual frame cadence at curbside walkups.** Until that data exists, cluster threshold tuning is guesswork.

### 3b. Is `cumulative_miles` reliable as a G1 anchor?

Gemini noted a "Reset Problem" — Android can sometimes reset or jump `cumulative_miles` after a crash or restart. We don't have data on:
- How often this happens in practice
- Whether the Android app populates the field consistently
- Whether the heartbeat body's value is monotonic in normal operation

If `cumulative_miles` is unreliable, G1 needs a fallback (e.g., haversine GPS-distance-since-pickup) or has to tolerate gaps gracefully.

### 3c. How many ghost-dropoff candidates exist in the production audit trail?

See Section 4 for the forensic query. If the count is zero or near-zero across the past 7 days, G1's absence is theoretical and the launch-blocker label may be too strong. If non-zero, those are real ghost dropoffs that fired and need investigation case-by-case.

---

## 4. Forensic query (run before designing G1 fix)

Goal: identify FireDropoff events in the past 7 days where the driver had not yet driven enough distance to plausibly have completed the trip. These are ghost-dropoff candidates.

This is exploratory — `pudo_decision_context` schema and the way `driver_trip_state` records the pickup anchor need to be inspected first to know what columns to join. The query below is a starting sketch; the next session should adapt it once the schema is confirmed.

```sql
-- Sketch: needs schema verification before running
-- Expected columns to confirm:
--   pudo_decision_context.planner_action  (text — 'FireDropoff' literal)
--   pudo_decision_context.primary_offer_id (text)
--   pudo_decision_context.created_at (timestamptz)
--   driver_trip_state_log entries with state='IN_TRIP' for pickup anchor
--   offer_history.trip_miles (numeric)
--   heartbeat_log cumulative_miles snapshot at pickup vs. dropoff
-- Plus: where does cumulative_miles actually get stored? It's in the heartbeat
-- request body but I haven't confirmed it lands in any table.

SELECT
  pdc.created_at AS dropoff_fired_at,
  pdc.driver_id,
  pdc.primary_offer_id,
  oh.trip_miles AS expected_trip_miles,
  pdc.wai_confidence,
  pdc.cluster_lat,
  pdc.cluster_lng
  -- TODO: join to pickup-time anchor, compute driven_miles, flag if
  -- driven_miles < 0.9 * trip_miles
FROM app_private.pudo_decision_context pdc
JOIN app_private.offer_history oh ON oh.id::text = pdc.primary_offer_id
WHERE pdc.planner_action = 'FireDropoff'
  AND pdc.created_at > NOW() - INTERVAL '7 days'
  AND pdc.dispatch_executed = true
ORDER BY pdc.created_at DESC;
```

Sanity guard: in the past few weeks Andrew has been actively development-driving with `current_offer_id` set/cleared via the dev-build manual Nail It buttons, so production audit data may include synthetic test rides that should be filtered out. Andrew's driver_id should be filtered if the goal is "real customer-style rides."

---

## 5. Recommended next-session priority order

1. **Run the forensic query (Section 4)** — quantify the ghost-dropoff incidence in the audit trail. Adjust priority based on what shows up.
2. **Verify `cumulative_miles` reliability** — quick query against `heartbeat_log` for monotonicity and population rate. Determines whether G1 anchor can be odometer-based or needs GPS-haversine fallback.
3. **Drive 1–2 rides** (per Andrew's prior plan) — produces the curbside-walkup empirical data and a fresh ride or two through the now-restored heartbeat_log path. Confirms the matcher chain works end-to-end on real, post-fix data.
4. **Design G1 enforcement (the soft-confidence-penalty model)** — confidence penalty rather than hard veto. Avoids the wedge state where a vetoed dropoff fires repeatedly without ever clearing. Specifically: `confidence_after_g1 = confidence - penalty(driven_miles, expected_miles)`, where `penalty` is 0 when `driven ≥ 0.9 × expected`, scales up to ~0.4 at `driven = 0`, with a smooth curve in between. The phrase Andrew used: "are you at the right place AND you've done most of the work" — that's the design intent.
5. **G2a (POI adjacency) implementation** — once G1 is in, G2a is the next gate. Affects curbside walkups directly.
6. **bead_on_wire cleanup** — only after the architectural work above is stable. Three commits: header rewrite, dead-code deletion, primitive relocation.
7. **Cluster threshold tuning** — only after at least one curbside walkup is observed in clean post-fix heartbeat_log data. Possibly no change is needed; let data decide.

---

## 6. What was committed this session (eight commits, branch `patch-00566a-unified-refinement`)

```
8f9d35c  fix(heartbeat): restore app_private.heartbeat_log INSERT (Patch 00567 flight recorder)
dd17791  feat(test-harness): add allowlist-gated /api/v1/test/* endpoints
698e217  fix(deploy): force traffic to LATEST after every Cloud Run deploy
f60e53c  feat(status): surface last 3 planner dispatch actions in driver status
5ee1560  feat(scripts): add forensic replay harness (replay_pudo, harvest_ride)
46237cc  test(wai): add 24-hour smoke test for matching reliability (A1 validator)
0b9f437  test(heartbeat): add unit tests for _voice_for_actions priority logic
2861008  chore(gitignore): ignore patch-script artifacts, b3 backups, tmp/
```

All eight pushed to `origin/patch-00566a-unified-refinement` at session end.

---

## 7. Outstanding non-code items

- **Bruno test driver password rotation** — userMemories priority #5 partially complete (Firebase Web API key rotated; Bruno password still pending). Low risk because gated by `TEST_ALLOWLIST` at the endpoint, but worth doing before public launch.
- **Bruno relaxed-assertion changes on Andrew's laptop** — separate repo, not on the VM. Need to land before the Bruno regression suite is trustworthy in CI.
- **userMemories TODO** — "Log gpsAgeSec from Android decision request into trace_data in decisions.py." Field arrives as `p.get("gpsAgeSec")` in request body. Unrelated to G1 work but worth noting it's still open.

---

## 8. Notes for Gemini's review

The session followed paired-programming protocol but was Claude-only this time (no Gemini round-trips). The findings in Section 2 — particularly G1's absence — should be the most consequential thing to validate. Specific questions for Gemini:

1. **G1 enforcement design**: is the soft-confidence-penalty model correct? Or does Gemini's recollection of "$Current\_Odo - Pickup\_Odo \ge Trip\_Estimated\_Miles \times 0.9$" intend a hard veto? If the latter, how does the design handle the wedge-state failure mode (vetoed dropoff fires repeatedly without ever clearing)?
2. **Pickup-side G1**: Gemini's prior recollection was that G1 doesn't apply to pickups. Is that still correct? Andrew's Case D scenario (canceled-pickup pair, FireDropoff(canceled) + FirePickup(new)) suggests G1 should be skipped when `outcome != "normal"` — confirm?
3. **G2a / G2b status**: are these design-intent for post-launch, or were they intended for current sprint and got missed? userMemories says "locked Apr 25 2026" but they're not in code.
4. **cumulative_miles reliability**: Gemini's prior caveat about Android resets — is there empirical data on how often this happens in Andrew's actual driving, or is the caveat purely defensive?
5. **bead_on_wire cleanup ordering**: the proposed sequence (header → delete dead → relocate primitives) seems right, but the dead-code commit could happen any time. Should it be sooner (cleanup hygiene) or later (after G1/G2 ship)?

---

**End of report.**
