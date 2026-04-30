# PuddleJumper Simplified Ride Identification Architecture

**Status:** Architectural specification, ratified 2026-04-30 morning by paired-programming (Andrew + Claude + Gemini).
**Replaces:** The 4-box state machine (UNCOMMITTED / ENROUTE / IN_TRIP / STACKED) with atomic-swap logic, B-12 reconciliation, TargetSpec Vacuum recovery.
**Supersedes (when implemented):** The "States" sections of `CANONICAL_RULES.md` (Sections XI, XII), the entire dispatch ladder in `pudo_planner.py` for state-driven logic, and the heartbeat-handler state interlocks in `driver_heartbeat.py`.
**Authority:** This document defines product law for the new architecture. Edits require explicit ratification.

---

## 1. Why this exists

The deterministic state machine that PuddleJumper inherited from BMOAR works by *predicting* what the driver will do next. It says "the driver is ENROUTE to pickup, so we expect a cluster to form near the pickup geocode soon, then we'll transition to IN_TRIP." That prediction is brittle:

- If the cluster forms at a slightly wrong location (corner-lot S32 failures, geocode drift), the prediction breaks
- If the driver bypasses the pickup (Sheraton Ghost Ride), the state machine has no recovery path without explicit reconciliation logic (B-12)
- If two rides stack (atomic swap), the state machine has to orchestrate a complex multi-step transition with primary/secondary tracking
- If a heartbeat handler interlock variable is undefined (`_just_nailed_pickup` bug 2026-04-30), the entire chain crashes

The deeper problem: **the state machine duplicates spatial reasoning that WAI already does.** WAI's Memory pillar tracks cluster history, the Signal pillar runs road topology and adjacency matching, the Latch pillar prevents spurious dropoff fires. All of that is *also* encoded in the state machine's transition rules and threshold ladders. Two places for the same logic to be wrong.

This document proposes inverting the dependency direction: **WAI becomes the single source of truth about which offer is currently being driven, and state follows observation rather than predicting it.**

---

## 2. Core principle

> **WAI is the single source of truth about "which offer am I currently driving."**

Not the state machine. Not the dispatcher. Not the convergence engine. WAI.

Every heartbeat, the system observes reality (cluster + classification) and updates `current_offer_id` to reflect what WAI just observed. State follows reality. There is no prediction, no atomic swap, no STACKED state, no Reconcile dispatch.

Three corollaries:

1. **`current_offer_id` is a derived value**, not a source of truth. It is whatever WAI's most recent match indicated. Other code reads it but does not write to it directly — only the WAI-driven update path writes.

2. **The 4-state machine collapses to a 1-bit memory.** Either an offer is active (`current_offer_id != NULL`) or it isn't (`= NULL`). UNCOMMITTED, ENROUTE, IN_TRIP, STACKED are no longer needed as distinct states.

3. **Recovery from anomalies is structural, not reactive.** Sheraton Ghost Ride, canceled rides, no-shows — these don't require special-case dispatchers. They emerge naturally from "WAI matched a different offer's pickup before matching the current ride's dropoff."

4. **WAI is a Matcher, not a Sensor.** WAI's responsibility is to transform raw spatial clusters into matched PUDO events by applying road topology, adjacency, and historical context. **A GPS cluster is NOT a PUDO.** A GPS cluster is raw input. A PUDO event exists only when WAI has confirmed that a cluster matches a specific offer's pickup or dropoff via the full evaluation pipeline (proximity, topology, adjacency, POI matching, numbered-street normalization, intersection logic). The system shall not perform any independent spatial validation outside of the WAI evaluation. No code path may treat a cluster's existence, its centroid, or its proximity to coordinates as evidence of a PUDO. Only `WAI.evaluate(queue, cluster)` produces PUDO matches.

---

## 3. The data flow

Every heartbeat:

```
1. Detect cluster
   - Existing trilogy: cluster_detection.py.get_recent_clusters()
   - Returns: cluster centroid, member count, age in seconds
   - May return None (driver moving, no cluster)

2. Get queue of active offers
   - Postgres-backed: every offer the driver has accepted but not completed
   - Read with FOR UPDATE SKIP LOCKED to prevent race with dispatcher
     adding new offers (see §6 Queue Synchronization)

3. WAI.evaluate(queue, cluster)
   - New signature: takes a list of offers, returns matched offer + location_type
   - Internally runs the existing classification logic against each offer's
     pickup AND dropoff coords
   - Returns: (matched_offer_id, location_type) or None
   - location_type ∈ {"pickup", "dropoff"}
   - May return multiple matches (see §5 Disambiguation rules)

   Internal evaluation contract (Map-Reduce):
     a. Map: for every offer in the queue, generate two TargetSpecs
        (one for pickup coords, one for dropoff coords)
     b. Filter: discard targets physically impossible (>500m from cluster)
     c. Evaluate: run Signal Economics (proximity, topology, adjacency,
        POI matching, intersection logic, numbered-street normalization)
        against each remaining candidate
     d. Reduce: return the candidate with the highest confidence score
        that clears the WAI confidence threshold (current default: 0.40)
     e. If multiple candidates tie at maximum confidence, return all of
        them (see §5 Disambiguation rules)

4. Apply Motion Gate (see §7)
   - If cluster speed >= 2mph or cluster duration < threshold, do not fire
     a transition. Log the WAI evaluation, return without state change.
   - Prevents drive-by false positives.

5. Update current_offer_id from WAI's match
   - See §4 Match Resolution Rules below

6. Compute pricing context for the next inbound offer (see §8)
   - Triangulation Filter, not just "current ride's dropoff"

7. Dispatch any required actions
   - fire_pickup, fire_dropoff
   - These are the ONLY remaining dispatch actions (see §9)

8. Insert log row into pudo_decision_context
   - Captures queue evaluated, match result, current_offer_id before/after,
     pricing context used, motion gate result
   - **Cluster data MUST be logged independently of WAI outcome.** The
     cluster_lat / cluster_lng / cluster_size / cluster_duration_s columns
     are populated from the cluster object returned by detect_cluster() in
     step 1, NOT from WAI's result. Even if WAI returns no match, even if
     WAI raises an exception, the cluster data must be preserved.
     Rationale: forensic analysis (post-shift) depends on knowing where
     the driver actually clustered. The current heartbeat handler (pre-
     pivot) reads `cluster = wai_result.cluster if wai_result and
     wai_result.cluster else None`, which couples cluster logging to WAI
     success. The 2026-04-30 smoke test surfaced this as a real data-loss
     pattern: yesterday's offers had healthy clustering but zero rows
     logged because WAI was getting current_offer=None and not
     populating wai_result.cluster.
```

That is the entire heartbeat handler. Existing handler is ~600 lines; this one is plausibly ~100.

---

## 4. Match resolution rules

WAI returns one of four answers each heartbeat (after motion gate clears):

### Case A: WAI returns `None` (no match against any offer in queue)

- `current_offer_id` is unchanged
- No fire action
- Log as `no_match` with reason

### Case B: WAI returns `(matched_id, "pickup")` and `current_offer_id == NULL`

- Driver arrived at a pickup; no ride was active
- `current_offer_id = matched_id`
- fire_pickup(matched_id)

### Case C: WAI returns `(matched_id, "dropoff")` and `current_offer_id == matched_id`

- Driver arrived at the active ride's dropoff; ride complete
- fire_dropoff(matched_id)
- `current_offer_id = NULL`

### Case D: WAI returns `(matched_id, "pickup")` and `current_offer_id != NULL` and `matched_id != current_offer_id`

- Driver arrived at a *different* offer's pickup while supposedly active on another ride
- This is the canceled/no-show/Ghost-Ride case
- Implicit conclusion: previous ride never reached dropoff; treat as canceled
- Log: `implicit_cancel` with reason "arrived at offer N pickup while current_offer_id was M"
- fire_dropoff(current_offer_id) with `outcome = canceled` flag (no actual coordinates,
  marks the ride complete in the queue)
- `current_offer_id = matched_id`
- fire_pickup(matched_id)

### Case E: WAI returns multiple matches (e.g., same-location pickup AND dropoff)

- See §5 Disambiguation Rules

---

## 5. Disambiguation rules (multi-match scenarios)

Three multi-match patterns that emerge from real geography:

### 5.1 Same-location pickup AND dropoff for the SAME offer (the "Errands" scenario)

Driver picks up rider, takes them on errands, returns to same location for dropoff.

WAI returns: `[(123, "pickup"), (123, "dropoff")]`

**Rule:** disambiguate using `current_offer_id` history.

- If `current_offer_id == NULL`: this is the pickup → start the ride
- If `current_offer_id == 123`: this is the dropoff → complete the ride

Note: this is a 1-bit memory check, not a state machine. The "history" is just whether the ride has been started yet.

### 5.2 Same-location dropoff and a DIFFERENT offer's pickup (the "Stacked" scenario)

Driver finishes ride 1 at location X, which is also ride 2's pickup.

WAI returns: `[(1, "dropoff"), (2, "pickup")]`

**Rule:** sequential resolution within a single heartbeat.

1. Process the current-ride dropoff first: `current_offer_id == 1` → fire_dropoff(1) → `current_offer_id = NULL`
2. Same heartbeat, re-evaluate the remaining match: `(2, "pickup")` against `current_offer_id = NULL` → fire_pickup(2) → `current_offer_id = 2`

Both transitions complete in one heartbeat. No atomic swap, no STACKED state, no primary/secondary tracking.

### 5.3 Two different offers' pickups (geographically close pickups)

WAI returns: `[(A, "pickup"), (B, "pickup")]`

**Rule:** if WAI's classification logic genuinely cannot distinguish (POI overlap, road segment shared, etc.), return ambiguous match.

- Do not fire any action
- Log as `ambiguous_match` with both candidates
- `current_offer_id` unchanged
- Manual review surface (production) or manual nail (dev builds only)

This is the case where WAI's existing intelligence (Memory + Signal + Latch) actually earns its keep. Distance alone wouldn't disambiguate; road topology and POI logic should. If they can't, the system fails safe by not firing.

---

## 6. Queue synchronization (Postgres as canonical source)

The offer queue must be Postgres-backed and read atomically per heartbeat.

**Pattern:**

```sql
SELECT decision_log_id, pickup_lat, pickup_lng, dropoff_lat, dropoff_lng, ...
FROM app_private.offer_history
WHERE driver_id = $1
  AND actual_pickup_at IS NULL OR actual_dropoff_at IS NULL
  -- (or whatever the canonical "still in flight" predicate is)
ORDER BY decision_log_id
```

Read on every heartbeat. No in-memory caching of the queue. No race window between heartbeat read and dispatcher write because Postgres serializes the reads.

**Why this matters:** if the heartbeat handler caches the queue and the offer dispatcher adds a new offer between heartbeats, the cached queue misses it. WAI evaluates against a stale queue and never matches the new offer's pickup. The system silently drops a ride.

Cost: one extra SELECT per heartbeat. Trivial against Postgres given the existing indexes.

---

## 7. Motion gate (transition prerequisite)

WAI evaluation may run on every heartbeat. **Firing transitions** (fire_pickup, fire_dropoff) requires the cluster to be "Closed":

> A cluster is **Closed** when speed_mph < 2 AND cluster_duration_s >= MOTION_GATE_DURATION_S

Default `MOTION_GATE_DURATION_S = 20` (TBD per first-shift validation).

**Why:** without this, a driver passing within 50m of a destination at 35mph could trigger a false-positive match. The current trilogy already has this logic in `where_am_i.py`'s cluster detection, but the new architecture must elevate it to a hard architectural rule because the dispatch surface no longer has the convergence engine acting as a secondary gate.

**Logging:** every WAI evaluation logs `motion_gate_result` ∈ {"closed", "moving", "transient"}. Transitions only fire on `"closed"`.

---

## 8. Pricing context (Triangulation Filter)

When a new inbound offer arrives, the pricing engine needs an "origin point" — where the driver will be when they start driving toward the new offer's pickup.

The naïve rule (current ride's dropoff if active, else driver GPS) breaks in the canceled-ride scenario: system thinks ride 123 is active, prices new offers from 123's dropoff coords (5 miles away), driver actually at 123's pickup waiting → pricing error.

**Triangulation Filter rule:**

For each inbound offer, compute:
- `D_actual` = distance from driver's current GPS to new offer's pickup
- `D_intent` = distance from current offer's dropoff to new offer's pickup
- `BUFFER_M` = pricing-context jitter buffer (default 500m, TBD per validation)

Decision:
- If `current_offer_id == NULL` → use driver GPS (no current ride to consider)
- If `D_actual < D_intent - BUFFER_M` → use driver GPS (driver is closer to the new pickup right now than they would be after the supposed dropoff)
- If `|D_actual - D_intent| <= BUFFER_M` → use driver GPS (effectively equivalent; prefer current location to avoid jitter)
- Otherwise → use current ride's dropoff coords (driver is genuinely en route to dropoff, which is farther from new pickup)

**Failure mode analysis:**

- **False positive (use GPS when dropoff would be better):** small pricing error, slightly understates deadhead. Acceptable.
- **False negative (use stale dropoff when GPS would be better, e.g., Ghost Ride):** large pricing error, overstates deadhead, bad accept decisions. **Not acceptable.**

Therefore err toward larger BUFFER_M when in doubt. Validate first-shift against real cases.

**Why this is structurally clean:** the system isn't trying to *detect* a canceled ride. It's just computing which origin point makes the pricing math more efficient, and reality wins. The Ghost Ride case self-corrects without any explicit "is this ride still active?" check.

---

## 9. Dispatch surface (what fires)

In the simplified architecture, the dispatch chain has three actions:

1. **fire_pickup(offer_id)** — driver arrived at pickup, ride starts
2. **fire_dropoff(offer_id)** — driver arrived at dropoff, ride completes
3. **fire_dropoff(offer_id, outcome=canceled)** — implicit cancellation per Case D in §4

That is the entire dispatch surface. The 12-action `PlannerDecision` enum from sub-step 1c reduces to these three.

Eliminated dispatch actions:
- `fire_stacked_swap` — no STACKED state to swap
- `fire_stacked_revert` — same
- `fire_retroactive` — Memory pillar handles this implicitly via cluster matching
- `noop_*` variants — replaced by simple "no action this heartbeat"
- `cache_ghost` — no ghost cache; WAI evaluates against live queue every heartbeat

---

## 10. Assumptions documented for future review

This architecture rests on several assumptions that are reasonable given current data but are NOT empirically validated. Future sessions should re-examine these as production data accumulates.

### A1: WAI matching reliability ≥ 90% in production

The architecture trusts WAI's match output as ground truth. If WAI accuracy is materially below 90% for production data (corner lots, geocode drift, multi-entrance buildings), the simplified architecture has weak ground truth and may need fallback layers.

**Validation path:** truth table from first 50-100 production rides. Specifically the `wai_status` distribution and whether `at_current_pudo` matches correlate with actual fare completions.

**Empirical state at 2026-04-30:** smoke test against 24 hours of historical clusters (867 sampled) produced 1 CONFIDENT match across ~80 driving-time evaluations. The single confident match was offer 8303's pickup approach at confidence 0.409 — barely above the 0.40 threshold, single decisive winner over 17 queue offers. This tells us:
  - WAI's matchers CAN return correct, decisive matches for real production geometry (not fundamentally broken)
  - The match landed at the edge of the threshold (0.409 vs 0.400 cutoff) — threshold may be poorly calibrated for real-world geocode offsets
  - Most production heartbeats during driving did NOT produce confident matches; A1's 90% target is not currently met at the matcher layer
  - Production WAI was returning at_unknown_pudo with confidence 0 for the same cluster (4696) because `_assemble_offer` was building offers from `dts.*` (NULL) instead of `decision_log.*` (populated). The wiring layer was masking matcher capability.

**Empirical state at 2026-04-30 evening (high-res 8303 audit, post-Bug-2 commit `f27b2cf`):** After fixing the breadcrumb segment-dict contract violation across `where_am_i._compute_road_topology` and two test-fixture sites (3-file commit `f27b2cf`), a high-resolution smoke test on offer 8303's window (every cluster, no sampling, full 18-offer queue evaluation) produced findings that refine A1's empirical picture:
  - **The 8303 dwell is 13 heartbeats**, not 1. Yesterday's `SAMPLE_EVERY_N=10` view collapsed a 13-cluster sustained-dwell into a single sampled point. The driver stopped at GPS coordinates `29.808911, -95.402587` from 01:09:14 to 01:10:23 (69 seconds of identical-coords clustering). Every heartbeat in that window scored 8303 as `CONFIDENT` with confidence 0.404-0.409. This is exactly the trajectory shape the simplified architecture's Motion Gate (§7) needs: cluster_detection is firing reliably during a real PUDO event, producing the sustained Closed-cluster signal that fire_pickup() will hang off in the new heartbeat handler. A9 (cluster reliability) is empirically supported for this case.
  - **Decisiveness gap held cleanly.** Across all 13 dwell heartbeats, 8303's confidence (0.409) vs the runner-up (0.000) was the maximum possible separation. No other offer in the 18-offer queue scored above zero. The Map-Reduce contract in §3 produces a single decisive winner here; the disambiguation rules in §5 do not need to fire for this scenario.
  - **Breadcrumb_match did not contribute to 8303's score** despite the fix. Pre-fix theory was that breadcrumb_match was zeroing out due to the AttributeError crash and would rise post-fix. Actual result: 8303's confidence stayed at 0.404-0.409 verbatim. Diagnosis: the driver's actual approach path likely did not traverse the named roads of 8303's pickup (`Rutland St`, `W 25th St`) — the driver may have approached perpendicularly or from a side street and parked just before the named-road intersection. Breadcrumb_match returns 0.0 correctly in this case; the signal is now *available* but not *guaranteed positive* for any given geometry. The Bug 2 fix was structurally correct (no more silent zero-contribution) but did not move 8303's score because breadcrumb_match was already returning 0.0 for 8303's specific approach geometry.
  - **The threshold edge is real.** All 13 confident matches scored within 0.005 of the 0.40 threshold (0.404-0.409). A small downward calibration on any signal weight would zero out the entire match cascade for this PUDO; a small upward calibration would do the same. Threshold sensitivity for intersection-class matchers is the dominant tuning concern revealed by this audit.
  - **The dropoff phase is unresolved.** Six clusters from 01:16:37 to 01:18:39 (post-pickup, presumably en route to or arriving at the dropoff at `Cortlandt St & White Oak Dr`) produced no match against any of the 18 offers, including 8303's own dropoff. Four possible causes warrant investigation during Sprint A's first-shift validation: (a) clusters logged are from in-transit driving, not the dropoff dwell; (b) cluster_detection thresholds didn't fire during the dropoff stop; (c) dropoff geometry produces sub-threshold confidence for the same matcher-recall reason the pickup barely cleared; or (d) the current single-offer `evaluate()` signature contains internal state-aware filters (e.g., "only look for pickups if state is ENROUTE") that short-circuited dropoff evaluation in the smoke harness — exactly the kind of internal guard the §3 Map-Reduce signature refactor will eliminate.

**Refinement to A1's failure mode:** the assumption "WAI matching reliability ≥ 90% in production" is bounded by *threshold-edge calibration*, not by signal-availability bugs. Sprint A's matcher-tuning cycle (per §11) needs to address the concentration of confident matches at 0.404-0.409 — possibly by recalibrating signal weights for the intersection class, possibly by lowering the threshold, possibly both. The "1-confident-match-in-80-evaluations" framing from earlier 2026-04-30 was misleading (it was 1 *sampled* match representing 13 actual heartbeats). The corrected framing: 1 PUDO event observed end-to-end, with 13 supporting heartbeats clearing threshold by ≤0.01.

**If A1 fails:** consider hybrid model — WAI is primary source of truth, but a lightweight state-machine fallback covers low-confidence WAI results.

**Sprint A implication:** matcher work (threshold tuning, signal weight calibration) is real scope, not just wiring. The smoke test framework (`tmp/smoke_test_wai_queue_v2.py`) is reusable for empirical iteration.

### A2: Implicit cancellation via "different pickup match" is reliable

The architecture assumes Case D (Ghost Ride detection) is correct: arriving at offer N's pickup while current_offer_id is M means M was canceled.

**Edge case where this could be wrong:** driver in middle of ride M, takes a detour through area near offer N's geocoded pickup (e.g., passing through it on the way to M's actual dropoff). WAI fires `(N, "pickup")` falsely. System concludes M was canceled and fires `fire_dropoff(M, canceled)`.

**Mitigations baked in:**
- Motion gate (§7) requires Closed cluster, so drive-bys don't fire matches
- WAI's existing topology and adjacency matching reduces false-positive rate
- Memory pillar (cluster_revisit) detects when current location was visited recently

**If A2 fails:** add a temporal guard — implicit cancellation only fires if `current_offer_id` was set more than X minutes ago (preventing rapid mid-trip false fires).

### A3: Same-heartbeat sequential resolution (Stacked case) is acceptable

The architecture processes (dropoff, pickup) in a single heartbeat for back-to-back rides at the same location.

**Concern:** does the database support two state changes inside one heartbeat handler call without race conditions? Are there transaction boundaries that need explicit handling?

**Validation path:** Bruno test simulating same-location stacked transition. Verify both fire_dropoff and fire_pickup execute, both update Postgres atomically, no race with concurrent dispatcher writes.

**If A3 fails:** allow the second match to fire on the next heartbeat. Cost: one heartbeat (~5 seconds) of latency between rides. Probably acceptable.

### A4: Triangulation buffer of 500m is correctly calibrated

The pricing-context Triangulation Filter (§8) uses a 500m buffer. This is an educated guess. Real production data may show:
- Buffer too small → pricing jitters between GPS and dropoff coords
- Buffer too large → false positives (uses GPS when dropoff would be more accurate)

**Validation path:** log pricing-context decisions per heartbeat with both D_actual and D_intent. After 100+ offer evaluations, analyze whether the chosen context matched the actual driving outcome.

**If A4 fails:** tune the buffer empirically. Probably ends up in the 200m-1500m range depending on city geometry.

### A5: Motion gate of 2mph / 20 seconds correctly identifies "stopped at PUDO"

The motion gate threshold (§7) is also an educated guess. Real shifts may have:
- Slow-moving traffic crawls at 2mph for >20 seconds → false positive
- Quick pickups where rider is waiting curbside, driver stops for 5 seconds → false negative

**Validation path:** log motion_gate_result per heartbeat. Compare against ground-truth pickup/dropoff timestamps from offer_history. Tune threshold per failure mode.

**If A5 fails:** consider a two-tier gate (e.g., 2mph for 20s OR 0mph for 8s) that handles fast pickups while filtering crawls.

### A6: Postgres queue read on every heartbeat is performant enough

The architecture re-reads the offer queue from Postgres on every heartbeat (~every 5s per driver). Across 100+ drivers, this is 20+ queries/second to Postgres just for queue reads.

**Validation path:** measure actual query latency in production. The existing `offer_history` table has appropriate indexes; expected latency is <10ms per read.

**If A6 fails:** consider a short TTL cache (e.g., 2 seconds) with explicit invalidation on dispatcher writes. Slightly less consistent but much cheaper.

### A7: The trilogy's Memory pillar is sufficient for Sheraton Ghost Ride scenarios

The architecture relies on Case D (different-pickup match) to detect implicit cancellations. The Memory pillar (cluster_revisit) is what prevents the system from firing false dropoff for a *previously visited* location while waiting for the actual dropoff.

**Validation path:** specifically validate Sheraton-class scenarios in production — a canceled ride followed by a new accepted offer with pickup geographically close to the canceled-ride dropoff.

**If A7 fails:** explicit timeout on `current_offer_id` (e.g., if set for > 60 minutes without progress, mark as anomaly and clear).

### A9: Cluster detection fires reliably during real PUDOs

The architecture assumes `detect_cluster()` produces a Cluster object during the brief window when a driver actually stops at a pickup or dropoff. If clusters only form for sustained stops (5+ minutes) and not for typical 30-60 second pickups, the architecture has nothing to evaluate against during real PUDO events.

**Empirical state at 2026-04-30:** unknown. The 2026-04-30 smoke test surfaced that the existing heartbeat handler couples cluster logging to WAI success (`cluster = wai_result.cluster if wai_result.cluster else None`). For 15 of 18 offers in the past 24 hours, zero cluster rows were logged — but Andrew specifically remembers stopping for those PUDOs (and stopping deliberately longer than usual). This strongly suggests the cluster detector was firing but the data was being discarded by the wiring, not that clusters failed to form.

**Validation path:** in Sprint A's heartbeat handler rewrite, log cluster data unconditionally (per §3 step 8). Run a clean shift. Then query: of all completed PUDOs (`offer_history.actual_pickup_at`, `offer_history.actual_dropoff_at`), what percentage had at least one heartbeat with non-NULL cluster_lat within ±60 seconds of the timestamp?

If ≥90%, A9 holds. If lower, investigate cluster_detection thresholds (window_sec, min_samples, max_speed_mph) for tuning.

**If A9 fails:** the issue is upstream of WAI. Tune cluster_detection (likely lower min_samples or extend window_sec) before WAI matching can produce reliable A1 results. May require dual-tier cluster detection (one tight cluster for "definitely stopped at curb," one looser cluster for "approaching destination, slowing").

### A8: WAI is the only legitimate path from cluster to PUDO

The "Matcher, not Sensor" corollary in §2 is an architectural commitment, not just a coding convention. Future code (including features added in Phase F or B-27 work) may be tempted to add fast-path heuristics like "if cluster within 30m of dropoff geocode, fire dropoff" as performance optimizations or fallback paths.

**Why this is dangerous:** any such fast path duplicates spatial logic that WAI already does, recreates the brittleness this architecture was designed to eliminate, and creates two places where the math can be wrong (the same failure mode that motivated the pivot from the state machine).

**Validation path:** code review discipline. Any PR that introduces cluster-based or coordinate-based PUDO inference outside `WAI.evaluate()` must justify why it cannot be expressed within WAI's evaluation pipeline.

**If A8 fails:** the architecture itself is undermined. The "two sources of truth" failure mode reasserts. The fix is not a code change — it's a discipline failure that requires removing the offending fast path and routing the logic through WAI.

---

## 11. Implementation scope

### What survives from existing code

- `where_am_i.py` evaluation logic — extended to accept queue, return matched offer + location_type
- `cluster_detection.py` — unchanged
- `pivot_context.py` — unchanged (pending Bug 2 fix)
- `pudo_planner.py` — collapses to three actions (fire_pickup, fire_dropoff, implicit_cancel_then_pickup)
- `offer_history` schema — unchanged
- `pudo_decision_context` schema — extended with queue evaluation columns

### What is replaced

- `driver_heartbeat.py` heartbeat handler — rewritten to ~100 lines per §3
- State machine code (`DriverStateMachine`, `sm_transition`, the state-transition matrix) — vestigial; left in place but unused by the heartbeat path
- B-12 reconciliation logic — deleted
- Atomic swap / STACKED state handling — deleted
- Convergence engine threshold ladder for state-driven transitions — deleted

### What does NOT need to change

- Trilogy unit tests — most still apply (WAI evaluation, cluster detection)
- Integration tests — significant rework (any test referencing SM_ENROUTE/SM_IN_TRIP/SM_STACKED states needs replacement)
- Database schema for state machine columns — leave in place, just unused

### Effort estimate

- Bug 2 fix in `pivot_context.py` — 30 minutes
- WAI signature change to accept queue — 2-3 hours including unit tests
- Heartbeat handler rewrite — 3-4 hours
- Schema extension for `pudo_decision_context` — 30 minutes
- Smoke testing and Bruno fixtures — 1 hour
- Test suite realignment — **5-8 hours** (per Gemini's flag, this is the hidden iceberg)
- Matcher tuning iterations — **scope dependent on first-shift data** (per 2026-04-30 smoke test, threshold/signal weights for intersection class need empirical calibration; reserve 2-4 hours per round)
- First-shift validation — one full shift

Total estimate: 12-15 hours of focused work plus a validation shift, with matcher tuning as a likely follow-up cycle. Plausibly 3 sessions plus iteration.

---

## 12. Document provenance

- 2026-04-30 morning: voice-session brainstorm between Andrew and Claude established the core inversion (WAI as source of truth)
- 2026-04-30 morning: Gemini ratified the core principle, disambiguation rules, and added Queue Sync + Motion Gate refinements
- 2026-04-30 morning: Andrew identified Triangulation Filter as the pricing-context solution; Gemini ratified
- 2026-04-30: Andrew added the "Matcher, not Sensor" reinforcement (§2 corollary 4 + §10 A8) to prevent future fast-path drift
- 2026-04-30: Gemini ratified the formalized document and contributed the Map-Reduce evaluation contract (§3) and the Live Fire test framing (S5.1, S7, S8 covered by §5.1, §7, §8 respectively)
- 2026-04-30 afternoon: Smoke test against 24h of historical heartbeats surfaced cluster-data-loss pattern (15 of 18 offers had zero cluster rows logged due to wiring coupling cluster logging to WAI success). Added §3 step 8 clarification ("cluster data MUST be logged independently of WAI outcome") and §10 A9 (cluster detection reliability assumption).
- 2026-04-30 evening: Smoke test 2026-04-30: empirical findings — queue-evaluation harness (`tmp/smoke_test_wai_queue_v2.py`) tested 80 driving-time cluster evaluations across 18 offers. 1 CONFIDENT match (offer 8303 pickup, conf 0.409). Findings folded into A1 (matcher capability + calibration scope) and §11 (matcher tuning as iteration cycle). Production-vs-smoke-test divergence (production at_unknown_pudo=0 conf, smoke test at_current_pudo=0.409) confirmed root cause was wiring layer (`_assemble_offer` reading dts.* NULL coords instead of dl.*), supporting the architectural pivot's premise.
- 2026-04-30 late evening: Bug 2 (breadcrumb segment-dict contract violation) closed in commit `f27b2cf` across three files (`where_am_i._compute_road_topology`, `tests/test_where_am_i._fake_pivot`, `tests/test_scenarios._replay_S31`). Test floor restored to 309/309 from a 22-failure cascade during the diagnosis cycle. High-resolution smoke test on offer 8303's window (every cluster, no sampling) revealed a 13-heartbeat sustained dwell scoring 0.404-0.409 against 8303 with runner-up 0.000 — confirming Motion Gate trajectory shape (§7) and Map-Reduce decisiveness (§3) for this case while exposing threshold-edge calibration as the dominant matcher-tuning concern. §10 A1's empirical-state subsection extended to capture the corrected "13 heartbeats / 1 PUDO event" framing.
- 2026-04-30: this document formalized as canonical reference and ratified as Product Law for Sprint A

**Modification policy:** changes to §2 (Core Principle), §4 (Match Resolution), §5 (Disambiguation), §7 (Motion Gate), §8 (Triangulation Filter), §10 (Documented Assumptions) require paired ratification. Implementation details in §3, §6, §9, §11 may be updated as production data informs them.

🎩🐸🏁