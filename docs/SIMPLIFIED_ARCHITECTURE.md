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

**If A1 fails:** consider hybrid model — WAI is primary source of truth, but a lightweight state-machine fallback covers low-confidence WAI results.

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
- First-shift validation — one full shift

Total estimate: 12-15 hours of focused work plus a validation shift. Plausibly 3 sessions.

---

## 12. Document provenance

- 2026-04-30 morning: voice-session brainstorm between Andrew and Claude established the core inversion (WAI as source of truth)
- 2026-04-30 morning: Gemini ratified the core principle, disambiguation rules, and added Queue Sync + Motion Gate refinements
- 2026-04-30 morning: Andrew identified Triangulation Filter as the pricing-context solution; Gemini ratified
- 2026-04-30: Andrew added the "Matcher, not Sensor" reinforcement (§2 corollary 4 + §10 A8) to prevent future fast-path drift
- 2026-04-30: Gemini ratified the formalized document and contributed the Map-Reduce evaluation contract (§3) and the Live Fire test framing (S5.1, S7, S8 covered by §5.1, §7, §8 respectively)
- 2026-04-30: this document formalized as canonical reference and ratified as Product Law for Sprint A

**Modification policy:** changes to §2 (Core Principle), §4 (Match Resolution), §5 (Disambiguation), §7 (Motion Gate), §8 (Triangulation Filter), §10 (Documented Assumptions) require paired ratification. Implementation details in §3, §6, §9, §11 may be updated as production data informs them.

🎩🐸🏁