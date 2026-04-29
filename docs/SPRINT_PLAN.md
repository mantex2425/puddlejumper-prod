# PuddleJumper Launch Sprint — 30 Days to Live

**Authored:** 2026-04-28 evening
**Updated:** 2026-04-29 morning — pivot per voice session
**Floor at start:** pytest 290/290, integration 22/61. HEAD `48297bf`.
**Mantra:** if it isn't code or a test to prove the code, we don't write it.

---

## Why this exists

BMOAR is broken. Houston shift 2026-04-27 proved it across 5 cases (corner-lot S32 failures, 584m geocode drift, TargetSpec Vacuum). The trilogy (Memory–Signal–Latch) ships in code at `72cf951` but is dormant — `driver_heartbeat.py` does not call it.

**The single critical question:** can the system identify a PUDO reliably?

Everything else — state machine, reconciliation, stacked rides, edge cases — is downstream of that one capability. We're cutting all synthetic-fixture work and validating PUDO identification against real fares with logging.

---

## Forensic artifacts we keep (already shipped, do not re-author)

- `PHASE_E_PROGRESS.md` (commit `773aa49`) — trilogy SHIPPED, lessons-learned, B-27 backlog
- `FORENSIC_2026_04_27_HOUSTON_SHIFT.md` (commit `773aa49`) — 5 cases, L-9 fixture provenance for future work
- `FORENSIC_2026_04_28_HOUSTON_WAYS_AUDIT.md` (commit `48297bf`) — `routing.houston_ways` audit, B-27 layer 4 validation

These are done. We cite them, we don't extend them.

---

## What changed in the 2026-04-29 pivot

The previous sprint plan had three sprints starting with T70-T74 (parachute check). Voice session this morning recognized that:

1. **Synthetic fixtures (T70-T74) don't validate against real PUDOs.** Real driving with logging does the same job better.
2. **WAI classification is the first filter** that determines which PUDO identification rule fires. Validating that classification against real fares is the actual test.
3. **A truth table built from production data** lets us find logic contradictions empirically, not theoretically.
4. **Adjacency logic must use dynamic street-segment lookup**, not hardcoded 200m radius.

T70-T74 sprint dropped. New sprint structure focuses on getting WAI live with logging today.

---

## The two sprints

### Sprint 1 — Wire WAI live with logging (TODAY)

**Goal:** WAI is the active decision engine. Every heartbeat is logged with full decision context. Drive real fares. Build the truth table empirically.

**Scope:**

1. **INDEX.md** at repo root — portable manifest of all decision documents. Pasteable at start of any chat session.

2. **Log table `pudo_decision_context`** — one row per heartbeat. Captures: timestamp, trip_id, driver GPS, WAI classification (straightroad/intersection/POI/not_at_pudo), rule_fired, segment data, identified_as_pudo, ground_truth fields filled post-ride.

3. **Wire `driver_heartbeat.py`:**
   - Replace BMOAR Path A/B detector calls with `WhereAmI.evaluate()` + `PudoPlanner.consume()`
   - Map all 12 `PlannerDecision` actions to existing dispatch handlers
   - **Reconciliation path:** when WAI returns `at_unknown_pudo` and state is IDLE, synthesize TargetSpec from the cluster WAI just produced. Uses cluster detection we already have — no 60-second timer.
   - Wrap WAI call in try/except: any exception falls back to safe inaction (log + no-fire). One bug must not brick the heartbeat handler.
   - Insert log row into `pudo_decision_context` on every evaluation.

4. **Drive single-trip rides.** Deliberately accept some declined offers (test bypass case). Turn system on/off mid-trip if curious.

5. **End-of-night query** the log table. See agreement rate, see contradictions.

**Done when:** `driver_heartbeat.py` calls WAI on every heartbeat, logs every decision, isolates errors. Existing pytest stays green. At least one shift driven with logging on.

**Estimated time:** 4-5 hours of focused work before driving. Then a shift.

---

### Sprint 2 — Iterate based on production data

**Goal:** turn the truth table into rule improvements.

**Scope:** depends entirely on what the log table shows after a few shifts. Likely candidates:

- **Adjacency logic (dynamic street-segment lookup)** if straightroad classification fires false-negatives on parking lots
- **WAI classification refinement** if `at_unknown_pudo` fires too often or too rarely
- **Reconciliation tuning** if the bypass-after-decline case shows logging gaps
- **3 mislabeled `routing.houston_ways` rows** cleanup (Terramont/Player Bend) if adjacency logic queries the table

**No pre-planning past this point.** The data tells us what to do.

---

## After Sprint 2

Iterate. Drive. Log. Query. Fix. Drive again.

If the truth table shows 95%+ agreement with reality, **launch.**

If it shows logic contradictions, fix those, drive again, requery.

The data is the gate, not a sub-step count.

---

## Discipline rules (kept)

- **L-2:** predict-then-verify on every gate
- **L-3:** anchor-based apply scripts when modifying files >200 lines
- **L-6 + corollary + SECOND STRIKE:** read production artifacts before authoring; `grep -rn` on any rename
- **L-9 + corollary refinement:** fixture provenance in docstrings; 80-iteration boundary probes
- **L-10:** gate threshold provenance categorized; paranoia not allowed
- **Memory note #2:** canonical `app_private.coords_to_*` functions; never raw `ST_MakePoint` in production
- **Memory note #9:** Auto Nail It only; no manual Nail It buttons in commercial product
- **Memory notes #17, #18:** never paste multi-line markdown to bash; scp from `/mnt/user-data/outputs/` only

---

## Discipline rules (NEW per 2026-04-29)

- **INDEX.md is pasted at start of every chat session.** Portable manifest of all decision documents. Without it, Claude operates blind.
- **System prompt directs Claude to consult INDEX before proposing changes.** Never guess at architecture; ask for the source document.
- **Read the code before modifying it.** Documents capture decisions; code holds the actual logic. Both required for safe changes.
- **Build outcomes, document outcomes.** No design documents before code ships. Markdown captures what was built and why, not what's planned.

---

## Discipline rules (DROPPED)

- **L-11 doc-currency gate** is now lightweight. Run pytest + git status at session-open, not the full doc-currency check.
- **PHASE_E_PROGRESS.md mid-sprint refreshes are dropped.** One closing refresh after launch, if at all.
- **No more multi-hundred-line forensic docs.** Bugs get a commit message. Findings get a one-paragraph note if they affect future work.
- **No synthetic-fixture sprints.** Real production data validates better than synthetic tests for PUDO identification.

---

## Status

**Sprint 1 (Wire WAI + logging):** SHIPPED 2026-04-29 morning, deployed Cloud Run revision `puddlejumper-api-00574-gw9`. Truth table `pudo_decision_context` filling.
**Sprint 2 (Adjacency lifeboat):** SHIPPED 2026-04-29 evening, deployed Cloud Run revision `puddlejumper-api-00575-mch`. 5 commits pushed (`09b68d6` through `140b45f`). pytest 309/309. See "Sprint 2 closeout" section below.
**Sprint 3 (POI + shadow-replay):** queued. See updated backlog at end of doc.

LFG. 🎩🐸🏁---

## PUDO-FIRST DIRECTIVE (2026-04-29 evening — ratified)

**Mantra:** if the system cannot accurately identify a PUDO, it is worthless. Architecture is secondary; ground truth is everything.

### I. Singular objective

Until further notice, the only purpose of any sprint, test, or line of code is to prove 100% accuracy in PUDO identification. We are building a perception engine, not a state manager.

### II. Operational constraints

1. **Single rides only.** Andrew accepts single-trip offers only.
2. **No stacks (operationally).** Andrew does not accept stacked offers.
3. **No parachutes.** BMOAR is dead. `check_convergence`, `SET_CANDIDATE`, and the legacy `ABORT` guard are all removed. If perception fails, the truth table records the failure and we fix the logic.

#### II.2 amendment — "no stacks" means operational, not architectural

"No stacks" means Andrew does not accept stacked offers. It does NOT mean STACKED-state code is dead.

Implicit stacks still occur: Uber sends a new offer mid-IN_TRIP because the primary rider silently cancelled (CANONICAL § X — Implicit Cancellation). The system must perceive these and recover. The Trilogy's STACKED disambiguation path remains live because GPS truth at the next pickup is the ONLY signal that resolves the implicit cancel — exactly the case that makes WAI valuable.

**Concrete consequence:** all 12 `PlannerDecision` actions are wired into `driver_heartbeat.py` dispatch in Sprint 1, including `fire_stacked_swap`, `fire_stacked_revert`, and the two `reconcile_missed_*` actions. Stubbing handlers to `noop` would break the canonical recovery path the moment Uber teleports a driver from a failing primary into a new pickup.

**Field event 2026-04-28 (Andrew):** arrived at pickup → IN_TRIP fired correctly → passenger silently cancelled → new offer arrived → state went STACKED → Andrew was actually ENROUTE to the new offer. This is the canonical implicit-cancellation pattern. Under wired Trilogy, arrival at the new pickup will resolve via `fire_stacked_swap`. The truth table will capture the resolution path as the first labeled case.

### III. Simplified state surface (driver-perspective)

Four logical states matter to the perception engine:

- **UNCOMMITTED** — idle, looking for work.
- **ENROUTE** — navigating to a known pickup.
- **IN_TRIP** — navigating to a known dropoff.
- **UNKNOWN** — a stop that the system cannot explain (the Sheraton case). This is where the product's intelligence is born.

(STACKED remains alive in the state machine as a holding pattern for implicit-cancel recovery; it is not a driver-perspective primary state.)

### IV. Practice phase ("happy trail")

Execute a series of easy single-trip rides to surface the variety of PUDO geometries (target: 10–30 variants).

**Goal:** build a robust library of successful identifications across geometries — intersections, single roads, POIs, apartment gates.

### V. Anger phase (recovery testing)

Once happy-trail rides show clean identification, deliberately drive:

1. **Declined rides** — prove the system can re-parent a ride it didn't formally accept.
2. **Unseen locations** — prove the system can synthesize a `TargetSpec` from an "unknown" stop without breaking the state machine.

### VI. Success gate

Valuation starts at the curb. If the system cannot accurately assign value and coordinates to a pickup location, it cannot scale. No commercialization until identification is robust, repeatable, and unbreakable.

---

## Sprint 1 — B-strict configuration (2026-04-29 evening)

### Architectural decision: Option B — Full Replace (ratified)

Earlier in this sprint plan, Sprint 1 was framed as "wire WAI live with logging" without specifying how WAI relates to the legacy `check_convergence` ladder. The 2026-04-29 evening session ratified the answer:

- `check_convergence` is dead. Skipped entirely for the heartbeat handler.
- Legacy `ABORT` guard (CANONICAL § XII) is removed. PUDO-FIRST § II.2 operational constraint (no stacks accepted) is what makes this safe — implicit cancels still recover via `fire_stacked_swap`.
- Watchdog module-globals (`_stopped_since`, `_candidate_lat/lng/at`, `_reset_watchdog_state`) are deleted. Dead code under B-strict.
- All 12 `PlannerDecision` actions are wired (see § II.2 amendment).
- No feature gate (`WAI_PLANNER_ENABLED_DRIVERS` is not added). Rollback is `git revert`.
- The Trilogy is the sole nervous system. The truth table (`pudo_decision_context`) is the only safety net.

### Divergence handling (deferred to Sprint 2)

WAI has no "divergence" classification — the four `WhereAmIResult.status` literals are stop-detection only (`at_current_pudo`, `at_previous_pudo`, `at_unknown_pudo`, `not_at_pudo`). When a driver is moving and not near a PUDO, WAI returns `not_at_pudo` and Planner emits `noop`. Under PUDO-FIRST, this is correct: stale state on shift-end (driver quits mid-ENROUTE without a resolving offer) is acceptable for the practice/anger phases. Truth table will surface the signature (many consecutive `not_at_pudo` rows then nothing) for the Sprint 2 stale-state cleanup script.

---

## Sprint 2 closeout — adjacency shipped (2026-04-29 evening)

Sprint 2 ratified earlier in the day as B-strict-pragmatic Trilogy wiring + adjacency lifeboat. Both halves shipped. Deployment is live.

### What shipped

Five atomic commits on `patch-00566a-unified-refinement`, each individually paired-programming reviewed (Claude proposes → Gemini reviews → execution):

```
140b45f  feat(where_am_i): add adjacent_road_match signal (Step E)
1790d2a  feat(where_am_i): wire adjacency into _RoadTopology (Step D, Option β)
22f64ef  feat(adjacency): DIAGNOSE primitive for parking-lot road whitelist
d0d0087  feat(heartbeat): wire Trilogy (WAI + PudoPlanner) per PUDO-FIRST B-strict-pragmatic
09b68d6  docs: append PUDO-FIRST directive to SPRINT_PLAN, update INDEX
```

Capability map:

- **Step 6c (Trilogy wiring)** — `driver_heartbeat.py` calls `WhereAmI` + `PudoPlanner` per PUDO-FIRST B-strict. All 12 `PlannerDecision` actions wired (including stacked-swap recovery for implicit cancels). `pudo_decision_context` row written every heartbeat. `check_convergence` ladder dead. ABORT GUARD removed.
- **Step C (adjacency primitive)** — `adjacency.py` at repo root. `get_adjacent_roads(cur, lat, lng, buffer_m=150)` + cluster wrapper. Hits `routing.houston_ways` GIST index. Defensive guards on `cur=None`, `lat/lng=None`. 15 unit tests.
- **Step D (topology integration)** — `_RoadTopology` gains `adjacent_roads: tuple[str, ...] = ()`. `WhereAmI._compute_road_topology(driver_id, cluster)` populates it via injection seam. 11 test call sites updated.
- **Step E (signal + weights)** — `_signal_adjacent_road_match` reuses `pivot_context._road_names_match` (Rd↔Road normalization). `_compute_signals` returns 7th key. `_CONFIDENCE_WEIGHTS` redistributed for 3 live classes (intersection, single_road, number_on_street); apartment_complex weights row gains `adjacent_road_match: 0.00` as dormant pass-through; poi unchanged (stub). Each row sums to 1.00. 4 new signal tests.

Test floor: pytest 290 → 309. No regressions.

### What we learned forensically (case studies attempted)

Two empirical case studies attempted; both surfaced infrastructure gaps rather than matcher answers:

**April 17 Lexington Blvd case (offer 6007).** "Lexington Blvd, Sugar Land, Texas" classifies as `single_road`. Adjacency at the geocoded centroid `(29.5912811, -95.6200022)` returns 6 roads including `Lexington Boulevard`, plus `East Mall Access Road`/`Mall Ring Road` confirming commercial-lot character. `_road_names_match("Lexington Boulevard", "Lexington Blvd")` returns True. Therefore: **if a cluster had formed at the centroid, `adjacent_road_match=1.0` would fire**. Could not empirically validate confidence math because **zero heartbeats logged for driver on April 17** — `heartbeat_log` empty for that date, predates current logging discipline.

**April 27-28 American Airlines case (offer 7579 / decision_log 8203).** Offer was ACCEPTED (`ACCEPT, Rates met`) and STACKED on top of an in-progress trip. Heartbeats exist for `current_offer_id='8203'` but only 5 rows over 22 seconds, all in `STACKED` state on the freeway 18-19 km from the geocoded dropoff. Trace ends mid-trip — state machine never advanced 8203 to ENROUTE/IN_TRIP. **Failure mode is upstream of the matcher**, not POI-stub. Empirical "would Step E have fired" answer is therefore unavailable from this trace.

**Critical schema finding from the AA forensic:** `heartbeat_log.current_offer_id` is keyed on `decision_log.id`, NOT `offer_history.id`. Forensic queries that join the two must use `offer_history.decision_log_id` as the bridge. **Logged for shadow-replay harness work (B-NEW-12).**

### Lessons logged

- **L-6 SECOND STRIKE corollary refined:** for any contract change to test fixtures, grep ALL forms of fixture construction including multi-line function-call values, not just literal-value assignments. Three test-fixture cascades hit and recovered today — Step D signature change (11 call sites missed initially), Step E `off_wire_pivot` regex (matched 0 of 3 actual sites), test_proximity_only_low_confidence assertion drift (test docstring already documented post-Step-E intent; assertion was contradicting docstring).
- **Apply script discipline held strong.** L-3 envelope (md5+lines+anchor uniqueness Phase 1; in-memory transform Phase 2; atomic os.replace + read-back + sentinel sweep Phase 3) caught every transform. Idempotency checks prevented double-apply. Three recovery patches (Step D fix, Step E fix, surgical str_replace for proximity_only test) all clean.
- **Transfer-pipeline newline-strip recovery is reliable.** Every download → upload cycle strips trailing newline. `printf "\n" >> file` recovers. Validate with md5+lines+syntax check before running.
- **"Bench is built; calibration ahead":** Sprint 1 + Sprint 2 shipped the perception engine plumbing. POI matcher, ground-truth labeling pipeline, shadow-replay harness, and weight calibration are different rhythm of work — Sprint 3 territory.

---

### Sprint 2 backlog (deferred from Sprint 1)

- **B-NEW-1: Implicit cancellation forensic case.** First labeled row in `pudo_decision_context` where `fire_stacked_swap` fires for a primary the driver never confirmed as cancelled — i.e. the 2026-04-28 field event reproduced. Validates the recovery path empirically.
- **B-NEW-2: Stale-state cleanup.** Driver quits mid-ENROUTE → row sits forever. Build post-shift script that reads `pudo_decision_context` and resets stale states.
- **B-NEW-3: Adjacency logic.** **DONE** as Steps C/D/E above (commits `22f64ef`, `1790d2a`, `140b45f`). Covers parking-lot back-entrance / strip-mall / commercial-lot cases for `intersection`, `single_road`, `number_on_street` classes.
- **B-NEW-4: WAI classification refinement** based on truth-table data once shifts are recorded (carry-forward).
- **B-NEW-5: 3 mislabeled `routing.houston_ways` rows** (Terramont/Player Bend) cleanup (carry-forward).
- **B-NEW-6: Replace naive address parser.** Deferred — `classify_address` is sufficient for Sprint 2 scope. Revisit if truth-table data shows misclassification rate >5%.
- **B-NEW-7: Validate or delete `apartment_complex` matcher.** Currently unreachable — `classify_address` never produces this bucket. Either wire a path that produces it (apartment-name keywords) or delete the dead matcher and weights row.
- **B-NEW-8: Schema migration permission audit.** Bake `GRANT USAGE ON SEQUENCE` into DDL by default. Discovered when `pudo_decision_context_id_seq` USAGE grant was missing post-create and broke first deploy.
- **B-NEW-9: Polygon-aware POI matching (Approach 1).** Replace `_match_poi_stub`. Load OSM `building`/`amenity` polygon data into a new schema. Per-cluster geometric-inside / near-perimeter check against polygon footprint. Covers hospitals, malls, named businesses with defined footprints. Does NOT cover small businesses without OSM polygons. Estimated 2-3 focused days.
- **B-NEW-10: Google Places Address Descriptors (Approach 2).** Per-heartbeat reverse-geocode call. `spatial_relationship in (BESIDE, WITHIN)` and `travel_distance < 50m`. Requires H3-keyed cache to control API costs (~$5/1000 calls). Adds 100-300ms latency per call; may impact 5-second heartbeat cadence. Last-resort fallback when polygon matching fails. Estimated ~1 week.
- **B-NEW-11: Driver-PUDO-history (Approach 3).** New table `driver_pudo_history` keyed on `(driver_id, cluster_h3)`. Records every successful PUDO. On future visits to the same H3, treat the historical cluster centroid as a strong fire signal. Solves repeat-route case. Cold-start problem unresolved. Wires the EXECUTE side of the planner's `cache_ghost` action. Estimated 2-3 weeks.
- **B-NEW-12: Shadow-replay harness.** Given an offer_id (or decision_log_id), reconstruct heartbeat sequence from `heartbeat_log`, run each heartbeat through current `WhereAmI`/`PudoPlanner` pipeline offline, compare predicted outcome to historical `actual_*` fields. Useful for validating Step E weight tuning, future POI matcher, and any matcher refactor before deploying. Bridge between the offer_history/heartbeat_log schema split (current_offer_id keyed on decision_log.id) is required. Estimated 1-2 days.
