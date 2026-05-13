# PuddleJumper Canonical Rules

**Status:** eternal product law. These rarely change. Edits require explicit ratification.
**Load:** at the start of every chat session, before any work.

---

## ⚠️ DEPRECATION NOTICE (2026-04-30 — Sprint A)

The simplified architecture pivot ratified 2026-04-30 morning is in active
implementation. Several sections of this document reference state-machine
constructs that are being demolished in Cuts B1–B3 of the WAI signature
refactor:

- **Section III "Logic Rules"** — `check_convergence` state machine is dying
- **Section VIII "4-Box Controller"** — file assignments under PLAN/EXECUTE
  are stale (PudoPlanner being deleted; `sm_transition()` going away)
- **Section IX "Enforcement Layers"** — `valid_state_transitions` and
  `enforce_state_transition_trigger` die with the state machine
- **Section X "Implicit Cancellation"** — S04/S11/S12 vocabulary is legacy;
  the principle ("GPS is always the truth") is preserved by §4 Case D in
  SIMPLIFIED_ARCHITECTURE.md
- **Section XI "State Levels"** — UNCOMMITTED/ENROUTE/IN_TRIP/STACKED are
  collapsing to a 1-bit `current_offer_id` memory
- **Section XII "ABORT Guard"** — ABORT verdict is gone; Case D + §8
  Triangulation Filter handle the equivalent scenarios

While the demolition is in progress, **`docs/SIMPLIFIED_ARCHITECTURE.md` is
the authoritative source on conflict.** These sections will be rewritten
cleanly after Cut B3 lands. Section VIII's separation-of-concerns *frame*
(Monitor/Diagnose/Plan/Execute) survives; only the file assignments need
revision.

The 4-box discipline question to ask before any code (Section VIII end)
remains operative regardless of file-assignment drift.

Sections I, II, IV, V, VI (post-trim), VII, XIII (post-amendment), XIV, and
the new Section XV are fully current.

---

## I. COORDINATE RULES (STRICT)

### Mandatory Functions

- **H3:** `app_private.coords_to_h3(lat, lng)`, `app_private.h3_to_lat(h3)`, `app_private.h3_to_lng(h3)`
- **Geometry:** `app_private.coords_to_point(lat, lng)`, `app_private.coords_to_geography(lat, lng)`
- **Math:** `app_private.distance_miles(lat1, lng1, lat2, lng2)`

### The Blacklist

**NEVER** write `ST_MakePoint`, `h3_latlng_to_cell`, or `h3_cell_to_latlng` directly.

### The Ordering Rule

Arguments are **always** `(lat, lng)`. If you find yourself manually swapping them to fit a raw PostGIS function, you're doing it wrong.

### Developer Sanity Rule

> "If you find yourself thinking about coordinate order, local time offsets, or manual geometry creation, STOP. Use the `app_private` canonical functions. Trust the abstraction."

---

## II. TEMPORAL RULES (UTC-MANDATORY)

- **Canonical Time:** Always use **UTC**.
- **Postgres:** `(NOW() AT TIME ZONE 'UTC')`, or `NOW()` directly on `timestamptz` columns.
- **The Rule:** The engine is timezone-agnostic. All "Texas Time" localization occurs at the edge (the UI), never in the logic.

---

## III. LOGIC RULES

- **State Control:** Use the `check_convergence` state machine. No discrete "4-box" hardware controllers in production.
- **Distance over Geocode:** In the event of a conflict, the physical "YOLO" distance is the primary constraint; the geocode is the starting suggestion.

---

## IV. NAIL IT BUTTONS

There will be **NO MANUAL NAIL IT** buttons in the production system for 8th-grade drivers. Auto Nail It for pickup and dropoff **MUST** work, even if slightly wrong — better than no input at all.

Manual Nail It exists only in dev/validation builds for Andrew's own testing.

---

## V. UBER DATA REALITY

Uber provides **NO lat/lng coordinates**. The offer card is text addresses + trip_miles + trip_minutes + fare.

All coordinates come from:
- Google geocoding (offer pickup/dropoff addresses)
- Driver GPS (current position)

**No design may assume Uber-provided coordinates.**

---

## VI. CURRENT_OFFER_ID — 1-BIT MEMORY

`current_offer_id` is the system's **only memory** of which offer the driver is currently driving.

- Either NULL (no active ride) or set (ride in progress)
- Per the simplified architecture, the four legacy states (UNCOMMITTED, ENROUTE, IN_TRIP, STACKED) collapse to this single field
- Set on `fire_pickup` (a successful pickup observation transitions NULL → matched_id)
- Cleared on `fire_dropoff` (a successful dropoff observation transitions matched_id → NULL)
- Read by the heartbeat handler at the start of every heartbeat to interpret WAI's match list per §4 Cases A-G

The atomic swap concept is dead. There is no "next offer" lookup. There is no STACKED state. When two offers match in the same heartbeat (the §5.2 hot-swap case), the heartbeat handler processes them sequentially within that single heartbeat: dropoff first (clears `current_offer_id` to NULL), then pickup (sets `current_offer_id` to the new offer's ID).

---

## VII. POSTGRES OWNS THE TRUTH. PYTHON OWNS THE STRATEGY.

State, history, geometric truth → Postgres.
Decisions, dispatch, business logic → Python.

---

## VIII. THE 4-BOX CONTROLLER (MANDATORY ARCHITECTURE)

Every file belongs to exactly one box:

```
MONITOR:  Receive sensor inputs only. No logic, no writes.
          → driver_heartbeat.py (input receipt only)
          → Android accessibility service

DIAGNOSE: Interpret sensor data. Pure reads only. No writes.
          → nail_it_core.check_convergence()
          → decisions/state_enricher.py (read state only)
          → where_am_i.evaluate()

PLAN:     Business logic and strategy. No DB writes.
          → decisions/router.py
          → decisions/engine.py
          → decisions/triangulation_enricher.py
          → pudo_planner.consume()

EXECUTE:  The ONLY write path. Period.
          → DriverStateMachine.transition()
          → sm_transition() in Postgres
```

### Violation Pattern

If you see a DB write outside EXECUTE, or a `transition()` call outside the PLAN→EXECUTE handoff, **stop and redesign before proceeding.**

### Before Adding Any Code, Ask:

1. Which state level does this belong to?
2. Which Monitor feed triggers it?
3. Is it Diagnose (read), Plan (logic), or Execute (write)?
4. If Execute — does it go through `sm_transition()`?
   If not — it does not belong here.

---

## IX. ENFORCEMENT LAYERS (Three Gates, Every Transition)

1. **Python wrapper** rejects unknown triggers early
2. **sm_transition()** validates against `valid_state_transitions`
3. **DB trigger** `enforce_state_transition_trigger`, final gate

Nothing reaches the database without passing all three.

`SET LOCAL app.state_trigger` is handled by `sm_transition()` internally — **never call it directly from Python.**

---

## X. IMPLICIT CANCELLATION

GPS is always the truth. If the driver's physical position contradicts the state machine's expectation, the state machine is wrong, not the driver.

Uber never sends an explicit cancellation signal. The system detects cancellations through two mechanisms:

- **Offer card (Monitor 1):** new offer while ENROUTE → primary cancelled (S04). New offer while IN_TRIP → possible stack or no-show.
- **GPS truth (Monitor 2):** driver arrives at different pickup → S11/S12 promote. Driver arrives at dropoff → ride completed. Driver diverges without nailing pickup → ABORT.

**No-show resolution:** primary no-show → new offer accepted → STACKED. Driver heads to secondary pickup → S12 promotes → IN_TRIP. Ghost primary forgotten — GPS truth wins.

---

## XI. STATE LEVELS

The state machine is a hierarchical 4-box controller. Each driver state is a controller level. Each level has two independent Monitor feeds that trigger the same Diagnose → Plan → Execute loop.

**Monitor Feed 1:** Offer card (Android accessibility service)
**Monitor Feed 2:** GPS heartbeat (every 5 seconds)

### UNCOMMITTED — idle, waiting for work

- Monitor 1 (offer): Score offer → accept → ENROUTE
- Monitor 2 (GPS): S11 scan → driver at declined pickup → IN_TRIP

### ENROUTE — navigating to pickup

- Monitor 1 (offer): New offer proves primary cancelled (S04) → UNCOMMITTED → re-evaluate new offer
- Monitor 2 (GPS):
  - At pickup → INITIAL_NAIL → IN_TRIP
  - Diverging at speed + pickup NOT nailed → ABORT → UNCOMMITTED (driver left)
  - Diverging at speed + pickup WAS nailed → HOLD (driver picked up passenger, normal driving — **NEVER abort**, see Section XII)

### IN_TRIP — ride in progress

- Monitor 1 (offer): Stack opportunity → score → STACKED
- Monitor 2 (GPS):
  - At dropoff → DROPOFF_NAIL → UNCOMMITTED
  - S12: at unexpected pickup → IN_TRIP (primary cancelled, secondary promoted)

### STACKED — managing two rides

- Monitor 1 (offer): 3rd offer → one ride died → flag potential_cancellation → stay STACKED
- Monitor 2 (GPS):
  - At primary dropoff → buffer swap → ENROUTE
  - S12 at secondary pickup → IN_TRIP
  - S07 pickup_confirmed → IN_TRIP

---

## XII. ABORT GUARD (Critical Rule)

ABORT only fires when **ALL** of these are true:

- `state = ENROUTE`
- `nailed_pickup_lat IS NULL` ← pickup never confirmed
- `dist_m > 1200m` from pickup
- `speed_mph > 25`
- `state_seconds > 60` (grace period for U-turns)

If `nailed_pickup_lat IS NOT NULL`:

- Driver physically confirmed pickup location
- Diverging at speed = normal driving to dropoff
- ABORT is **NEVER** fired — verdict = HOLD
- State machine waits for DROPOFF_NAIL

**This guard is what allows ENROUTE → IN_TRIP to work. Without it, picking up a passenger triggers ABORT.**

---

## XIII. WHAT STAYS IN PYTHON (Never Becomes a Stored Proc)

These components remain in Python and never migrate to Postgres:

- **Decision engine threshold math** — changes frequently
- **Triangulation / arc-banding** — requires Google Maps API
- **YOLO/OCR pipeline** — Android-side
- **Discord notifications** — Postgres has no HTTP
- **`check_convergence()`** — pure Python math, no DB writes
- **Firebase auth** — external service
- **WAI evaluation, PudoPlanner dispatch** — pure logic, no DB writes

---

## XIV. SIMPLIFIED ARCHITECTURE (Sprint A — 2026-04-30)

These rules emerged from the 2026-04-30 architecture pivot and the WAI signature refactor (Cuts B1-B3 in progress). They are canonical going forward.

### A. The Naked-List Contract (Encapsulation Firewall)

`WhereAmI.evaluate()` is a Pure Sensor. It returns `list[WAIMatch]` and nothing else.

- A `WAIMatch` has exactly three fields: `offer_id`, `location_type` ("pickup" | "dropoff"), `confidence`
- **No coordinates** in the return type — the heartbeat handler reads coords from the cluster object and offer queue directly
- **No topology** in the return type — internal to confidence computation; not exposed
- **No status labels** in the return type — the handler derives meaning from the match list plus `current_offer_id`

The naked list is the contract. Code that bypasses WAI to infer PUDOs from raw cluster proximity violates the architecture (per SIMPLIFIED_ARCHITECTURE.md §10 A8). No fast-path heuristics like "if cluster within 30m of dropoff geocode, fire dropoff" outside `evaluate()`.

### B. Dispatch Purity

`dispatch.py` is a pure function: `dispatch(matches, current_offer_id, queue_offer_ids) -> list[Action]`.

- No DB reads. No DB writes. No GPS access. No file I/O. No side effects of any kind.
- Implements §4 Cases A-G and §5 disambiguation rules directly. The doc and the function stay synchronized.
- The heartbeat handler executes the action list. The dispatcher decides what to do; the handler does it.

### C. WAI Confidence Threshold

`WAI_CONFIDENCE_THRESHOLD = 0.40` is canonical (defined in `pudo_types.py`). 

- WAI returns only matches whose confidence clears this floor
- All test scenarios assert `confidence_min`, never exact `confidence ==` (real-world matches sit at threshold-edge per §10 A1's evening empirical state)
- Tuning this value is matcher-calibration scope, not architecture-amendment scope

### D. Production-Ready Standard

PuddleJumper targets public release. Every proposal must be production-ready code, not MVP/skeleton/proof-of-concept.

- No phased minimal slices ("ship a small piece first")
- No "we can fill this in later"
- Solo developer constraint: respect time by proposing complete solutions
- Auto Nail It must work for every PUDO without human intervention; manual Nail It exists in dev builds only

### E. Terminal-Paste Safety

Multi-line markdown content is unsafe to paste directly to bash. Two failure modes:

1. Lines starting with `> ` (markdown blockquotes) are interpreted as bash redirect operators and can truncate files
2. Bare lines like `1.` or `Floor` become empty filenames when pasted

**Mandatory pattern:** scp from `/mnt/user-data/outputs/` to the VM. Never raw paste multi-line content. For small inline content, route through `> /tmp/file.txt && cat /tmp/file.txt`.

### F. Patch Script Discipline

Code edits land via Python `str.replace` scripts, never `sed`-based patches.

- Every patch script is **idempotent** (re-running it on an already-patched file is a no-op, exit 0)
- Pre-modification check: every anchor must appear exactly once (exit 3 on missing or duplicated)
- Exit codes are disciplined: 0 = success or no-op, 2 = target missing, 3 = anchor problem, 6 = write failure
- Adding a required parameter to a method signature requires inventorying ALL direct invocation sites in `tests/` BEFORE patching (the L-6 corollary, ratified 2026-04-27)

### G. Coordinate and Time Canonicalization

(Cross-reference Sections I and II — these remain canonical.)

For new Sprint A code: when in doubt, route through `app_private.coords_to_*` (lat-first ordering) and `(NOW() AT TIME ZONE 'UTC')`. Any direct `ST_MakePoint` or local-time SQL is a code-review blocker.

### H. Wall-Clock GC Predicate (canonical)

`LIVE_OFFER_PREDICATE_SQL` (defined in `driver_queue.py`) is the single source of truth for "is this offer still live." Every production hot-path query against `app_private.offer_history` MUST compose this predicate; no bespoke freshness rules at call sites.

**Alias contract.** The predicate is alias-qualified to `oh`. Every consumer must alias the table as `oh`:

```sql
FROM app_private.offer_history oh
```

Bare-table FROM clauses (no alias) will fail at runtime with `AmbiguousColumn` whenever the query also JOINs `decision_log` (which shares column names like `created_at`). This was bug-4, diagnosed 2026-05-10 via the first live-DB test in the suite.

**Bind tuple.** Use `live_offer_predicate_params(current_cumulative_miles)` from `driver_queue.py`. The helper returns the 12-element tuple in the exact order the predicate expects. Production hot-path callers should pass a real odometer reading; legacy/test paths may pass `None` (graceful degradation to time-only).

**Drift gate.** `tests/test_driver_queue.py::test_offer_ids_only_and_project_offers_share_where_clause` enforces source-textual identity between the two driver_queue call sites. Adding a third call site in driver_queue.py requires extending this test or factoring out a similar guard.

**Exceptions.** Out-of-band scripts (replay, backtest, harvest, drive_review) may query historical data without the predicate. Each such call site must be documented in `docs/out_of_band_offer_history_queries.md` with the reason for exception. (This file does not yet exist; the first out-of-band script to claim an exception creates it.)

### I. Asymmetric Ambiguity Handling (Dispatch §5.3 + §5.3-mirror)

Implements Rule XV (Observation Before Narrative) at the dispatch layer.

When `WhereAmI.evaluate()` emits multiple matches at the same `location_type` (i.e., two pickups or two dropoffs at the same geocode — the §5.3 cases), dispatch handles the two cases **asymmetrically** because the recoverability profiles differ.

**Pickup ambiguity is recoverable; dropoff ambiguity is not.**

#### Two pickups, same geocode (§5.3 pickup case)

The common cause is an Uber re-bid: same passenger re-thrown as a new offer ID after a decline timeout. Less commonly: two independent passengers requesting from the same building.

**Resolution:**

1. Sort the tied pickup matches by `OfferMeta.created_at` descending. The most recently received offer is the **narrative winner**.
2. Emit `FirePickup(winner)` to set `current_offer_id = winner.offer_id`.
3. Emit `FirePickupObservation(loser)` for every other tied pickup. Each such action stamps `offer_history.actual_pickup_at` for that offer but does NOT touch `current_offer_id`.

**Why recency is the tiebreaker:** in the re-bid case, the later offer supersedes the earlier per Uber's dispatch semantics. In the rare independent-passenger case, recency is the best available default and the error is bounded — GPS truth at the dropoff phase will trigger §X Implicit Cancellation (Case D in dispatch) and correct the narrative within a single ride cycle. Cache observations are correct in both cases.

**Why not `app_verdict`:** acceptance state is a downstream concern. The PUDO matching layer treats all queued offers as equally valid observation candidates; branching on accept/decline at this layer would conflate observation with narrative and violate Rule XV. Recency (`created_at`) is a queue-physics fact available to dispatch through `OfferMeta` and sufficient on its own.

#### Two dropoffs, same geocode (§5.3-mirror dropoff case)

Possible causes: shared-ride (pool) drop, two stacked rides ending at the same building, geocode noise. The dispatcher cannot distinguish among these from within its boundary.

**Resolution:**

1. Emit `FireDropoffObservation(o)` for every tied dropoff. Each stamps `offer_history.actual_dropoff_at` for that offer.
2. Emit `ClearNarrative()`. This sets `current_offer_id = NULL`, placing the system in observe-only mode awaiting the next anchoring event.
3. Do not emit any `FireDropoff` (narrative dropoff). The narrative is explicitly unknown after this point.

**Why no narrative commit:** dropoff ambiguity has no GPS-recoverable downstream — the next event is a new ride, so a wrong dropoff commit would corrupt the narrative going forward with no self-correcting signal. Better to honestly clear the narrative than to commit wrong.

#### Three-or-more matches, any combination

Treat as unenumerated. Emit `LogAmbiguousMatch` and do not modify state. This is genuine architectural ambiguity (e.g., three concurrent matches at the same cluster) and should not be silently auto-resolved. If N≥3 ever appears in production, that data motivates a separate amendment.

#### Dispatcher signature

To support these decisions, `dispatch()` accepts a `queue_metadata` parameter:

```python
def dispatch(
    matches: list[WAIMatch],
    current_offer_id: Optional[str],
    queue_metadata: dict[str, OfferMeta],
) -> list[Action]
```

Where `OfferMeta` is defined narrowly:

```python
@dataclass(frozen=True)
class OfferMeta:
    created_at: datetime  # UTC-aware; timestamp from offer_history.created_at
```

**Strict scope:** `OfferMeta` carries exactly the data dispatch needs for the recency tiebreaker, and nothing else. It does NOT carry `app_verdict`, `fare`, or any other field. Future tiebreaker scenarios that require additional context are separate amendments with their own justification.

**Temporal invariant.** `OfferMeta.created_at` must be timezone-aware UTC per Rule III. The dataclass enforces this via `__post_init__`; any caller that constructs `OfferMeta` from a naive datetime fails at construction, not at the downstream comparison. This prevents silent ordering bugs from tzinfo stripping anywhere in the snapshot path.

The previous `queue_offer_ids: set[str]` parameter is removed — `queue_metadata.keys()` provides the equivalent border filter.

#### Forensic record

When the recency tiebreaker fires (the §5.3 pickup case), the JSONB `tad_decision_context` grows a `narrative_tiebreaker` field:

```json
"narrative_tiebreaker": {
    "winner": "7850",
    "losers": ["7849"],
    "signal": "created_at",
    "winner_created_at": "2026-05-12T18:35:15.501361+00:00"
}
```

When the `ClearNarrative` path fires (the §5.3-mirror dropoff case), the JSONB records the same shape with `signal: "ambiguous_clear"` and no winner. This makes the "I'd rather be lost and right than certain and wrong" decision visible to forensic queries (§V Flight Recorder).

---

## XV. OBSERVATION BEFORE NARRATIVE

The PUDO system exists primarily to populate two fundamental caches:

1. **The Pricing Cache:** Every identified Pickup records fare and price signals. This builds the per-location value model that drives our offer-scoring intelligence.

2. **The Geographic Cache:** Every identified Pickup *and* Dropoff records coordinates. This "pins" the location in our private map, displacing future Google API calls (the "Google Tax").

**The Operational Mandate:** Observation is the primary output; Narrative is the secondary output. Cache writes must fire whenever a PUDO event is identified, regardless of narrative certainty. While the trip narrative (`current_offer_id`) requires absolute disambiguation to avoid corruption, the caches thrive on volume.

**In Practice:** If the system sees an ambiguity (e.g., two pickups at the same spot), it must fire observations for both to capture the data, even if it defers the narrative commit to avoid a state-machine error.

> "We would rather have a perfectly populated map and a 'Lost' narrative than a 'Found' narrative and a blank map."

**Architectural implication.** The dispatcher emits two distinct families of actions: **narrative actions** (`FirePickup`, `FireDropoff`) which set or clear `current_offer_id`, and **observation actions** (`FirePickupObservation`, `FireDropoffObservation`, `ClearNarrative`) which write to the location/pricing caches without touching narrative state. See §XIV.I for the case-resolution semantics.

---


---

## XVI. ARREST-DEFINED TRUTH

**Ratified:** 2026-05-13
**Companions:** §VII (Postgres Owns Truth), §XV (Observation Before Narrative), §XIV.I (§5.3 Asymmetric Handling)

PuddleJumper does not know where a PUDO happens until it observes one.

Geocoded coordinates are **hypotheses** about what Uber's text addresses mean. They are produced by a third-party geocoder applied to vague human-readable inputs. They have an error budget that no part of the system can shrink. They are useful as inputs to confidence scoring. **They are never a gate.**

The car coming to physical rest is a **fact**. It is observable directly via the velocity stream. It has no error budget beyond GPS sampling noise (which on production hardware is ≤ 0.01 mph at zero velocity). When the car stops, a transaction event has occurred — for some reason, at some location, regardless of whether any geocoded point agrees.

> The pin is the stop. The stop is the pin.

### A. The state machine watches the car, not the offers.

PUDO detection has no per-offer state. There is no "armed for offer X." The state machine tracks one thing per driver: how long has the car been at zero velocity. When that counter crosses the arrest threshold, a PUDO event has been detected. The detection layer does not consult the offer queue for arming.

The mistake this rule prevents: "armed for offer X" leaks geocode-trust into the state machine. Arming criteria become "we're near offer X's pin AND offer X's signals look good." That makes the geocoded pin the *organizing principle* of detection. The system then misses every PUDO whose pin is wrong — which, given geocoder error budgets, is many of them.

**Exception, named explicitly:** Phase 1 of the Forensic Ladder uses TAD odometer position across the whole queue as a cheap efficiency filter — "no offer is even close to a destination, so don't burn cycles on Phase 2-5 logic." This is not arming-for-offer-X; it's an early exit when the queue can't plausibly explain *any* stop. The state machine is still watching the car; it's just running its cheapest test first.

### B. Offer matching happens after detection, not before.

Once a PUDO event is detected, the system asks the queue: which offer in the live set best explains this stop?

The matcher uses the existing trustworthy signals — TAD odometer position, WAI confidence — applied as **filters**, not as gates that the state machine waits for. If multiple offers match, §5.3 / dispatch resolves the ambiguity. If zero offers match, the stop is logged but no PUDO fires.

The mistake this rule prevents: requiring TAD or WAI to "pass" before the state machine begins watching for stops. Brief or imprecise stops at moments when TAD/WAI haven't yet converged are then invisible. By detecting the stop first and consulting TAD/WAI as filters after, the detection layer remains responsive to physics while the matching layer remains protective against false positives.

### C. Both TAD and WAI gates are required for offer matching.

A detected stop only fires a PUDO for offer X if:

- **TAD verdict for offer X**: `distance_gate.passed = True` — the driver's odometer position is consistent with arriving at offer X's destination leg (within the 85%-115% window for trips ≥2 miles, ±0.5 mi for shorter trips).
- **WAI confidence for offer X**: outcome confidence ≥ 0.40 — the geometric and POI signals plausibly match offer X.

If only one gate passes, the stop is logged but no fire occurs. The system prefers a missed PUDO (recoverable) to a wrong-narrative PUDO (corrupts forensic record).

The mistake this rule prevents: trusting one signal alone. TAD alone fires on coffee-shop stops in destination neighborhoods. WAI alone fires on geometric coincidences before the driver has actually traveled the leg distance. Both together provide the triple-lock: physics + odometer + geometry.

### D. Distance-to-geocode is never a gate.

No code path uses "distance from car's current position to offer.pickup_lat/lng" as a threshold gate that can prevent a PUDO from firing. Distance enters WAI's confidence calculation as a signal, weighted alongside cluster mass and POI proximity, but the resulting confidence value is the gate — not the raw distance.

The mistake this rule prevents: hard-coded distance thresholds (50m, 100m, etc.) that fail on imprecise geocodes. Apartment-complex dropoffs, residential intersections, mixed-use buildings all routinely have geocodes that resolve to a different point than where the driver actually stops. A distance-threshold gate guarantees these PUDOs miss.

### E. The arrest threshold is symmetric across pickup and dropoff.

Pickups and dropoffs use identical arrest detection parameters. The transaction physics is the same — car stops, rider transitions, car leaves. Any historical sense that "dropoffs are different" reflected geocode-quality variance, not transaction-physics variance.

Asymmetries between pickup and dropoff that genuinely exist:
- **Pickup records fare** (FirePickup writes `pickup_market_signals` + `community_offers`; FireDropoff does not). This is downstream of detection, in the execute layer.
- **Pickup binds `current_offer_id`**; **Dropoff clears it.** Narrative state mechanics, also downstream of detection.

These do not require asymmetric detection parameters.

### F. The Forensic Ladder

Rule XVI is implemented as a five-phase ladder. Each phase has a defined heartbeat cadence, a defined check, and a defined transition criterion. The ladder is **economical** — expensive operations (1Hz cadence, Google Places API call) only occur when cheaper checks have already passed. It is **forensic** — the canonical PUDO record is written exactly once, with back-dated coordinates from the peak-confidence sample during the arrest window.

#### Phase 1 — The Perimeter

- **State:** Passive observation. Default state when driving.
- **Heartbeat cadence:** 5 seconds.
- **Check:** TAD odometer position across the whole live offer queue.
- **Gate:** Is the odometer within `distance_gate.passed = True` window for *any* live offer?
- **Transition:** If yes → Phase 2. If no → stay in Phase 1.

This is the cheapest check the system runs. It gates entry to all higher-cost phases. If no offer in the queue is plausibly close to a destination, the system does nothing further this heartbeat.

#### Phase 2 — The Engagement

- **State:** Active neighborhood vigilance.
- **Heartbeat cadence:** 3 seconds.
- **Check:** TAD continues passing for at least one offer + observe velocity.
- **Gate:** Is the car stopped (`speed_mph = 0`) for 2 consecutive heartbeats (6s total)?
- **Transition:** If yes AND a stop is observed → Phase 2b. If TAD drops for all offers → return to Phase 1.

Phase 2 is the watching window. The driver is in the destination zone; the system is paying closer attention but not yet committed.

#### Phase 2b — The Arrest

- **State:** Preliminary commitment.
- **Trigger:** 6s of contiguous zero velocity reached in Phase 2.
- **Check:** WAI confidence on the best-matching offer.
- **Gate:** Is WAI confidence > 0.40 for any offer that also has TAD passed?
- **Transition:** If yes → mark PUDO event as "happened" (commit intent), proceed to Phase 3. If no → return to Phase 2 (this is a stoplight, traffic, etc., not a transaction).

Phase 2b is where the PUDO is *marked* — the system commits that an event happened — but the coordinates are not yet finalized. Coordinates remain refinable through Phase 4.

#### Phase 3 — The Flashbulb

- **State:** Contextual snapshot.
- **Heartbeat cadence:** 1 second.
- **Logic:**
  1. Check local `poi_cache` for entries near the current coordinates.
  2. **If cache hit:** use cached POI data, no external call. Proceed to Phase 4.
  3. **If cache miss:** make exactly one Google Places API call. Store result in `poi_cache` for this location. Proceed to Phase 4.

The cache-first discipline is non-negotiable. Google API calls cost money; cache hits cost nothing. As the cache populates over time, cache-miss rate drops asymptotically to zero. The "Google Tax" exists only on first-encounter locations.

**Transition:** Always proceeds to Phase 4 after POI data is loaded (whether from cache or live call).

#### Phase 4 — The Hill-Climb

- **State:** Continuous peak sampling.
- **Heartbeat cadence:** 1 second.
- **Logic:**
  1. Sample GPS every second while car remains stopped.
  2. For each sample, compute the WAI confidence for the matched offer using the Phase 3 POI data.
  3. Track the **peak sample** — the (timestamp, lat, lng, confidence) tuple with the highest confidence observed during this stop.
  4. If a new sample exceeds the previous peak, replace the peak.
  5. If a new sample falls below the peak, log it as a "decay" sample but do not replace the peak.
- **Transition:** When `speed_mph > 0` (car moves) → Phase 5.

The hill-climb captures the moment of maximum confidence — typically the moment the car is closest to the actual transaction point — rather than the moment the car finally moves away. This back-dates the canonical record to the physical truth.

#### Phase 5 — The Notarization

- **State:** Canonical record write.
- **Trigger:** Car moves after Phase 4 (`speed_mph > 0`).
- **Logic:**
  1. Identify the peak sample from Phase 4's window.
  2. Perform exactly one atomic database write to the canonical PUDO row using the peak sample's timestamp and coordinates.
  3. Apply the Transaction Lock (see below).
  4. Reset state machine to Phase 1 (or Phase 2 if another offer in the queue is still active).

The Phase 5 write is the only mid-stop database commit. Phases 2b through 4 hold the PUDO record in memory; only Phase 5 persists it. This avoids jittered writes and keeps the canonical record clean.

### G. The Transaction Lock

After a PUDO fires for a specific offer's specific leg (`FirePickup` for offer X, or `FireDropoff` for offer X), the matcher will not fire that same offer's same leg again until:

- The car has moved at least 500 feet from the fire location, **OR**
- The car has maintained `speed_mph > 5` for 10 contiguous seconds.

**Primary defense remains the SQL idempotency guards** (`WHERE actual_pickup_at IS NULL`). The Transaction Lock is belt-and-suspenders, preventing wasted compute and ensuring that long stops with brief speed flickers don't produce machine-gun firing.

Other offers' legs are not locked. A pickup fire for offer X does not block a pickup fire for offer Y at a different location, nor a dropoff fire for offer X later in the leg.

### H. Forensic Record

When a PUDO fires via the Forensic Ladder, the `pudo_decision_context` row records:

- `arrest_started_at`: when the 0.0 mph counter began
- `arrest_duration_s`: total contiguous zero-velocity time at fire
- `matched_offer_id`: which offer the matcher selected
- `match_signal`: which gate combination produced the match (`tad_and_wai`, `tad_and_wai_ambiguous`, `dispatch_resolved`)
- `matcher_candidates`: full list of offers that passed both gates, for ambiguity forensics
- `phase_reached`: which Forensic Ladder phase the heartbeat reached (1, 2, 3, 4, 5)
- `poi_source`: where Phase 3's POI data came from (`local_cache`, `google_live`)
- `peak_confidence`: the Phase 4 peak confidence value used for notarization
- `decay_samples`: count of Phase 4 samples below peak (for future GPS-quality analysis)

When a stop is detected but no offer matches, the row records:

- `arrest_started_at` and `arrest_duration_s` as above
- `matched_offer_id`: NULL
- `match_signal`: `no_match`
- `matcher_candidates`: empty array
- `unmatched_reason`: which gate failed for the closest candidate (`tad_failed`, `wai_below_floor`, `both_failed`, `empty_queue`)
- `phase_reached`: highest phase reached before failure

These forensics make false negatives investigable. A miss is not silent — every detected stop has a row, regardless of whether it produced a fire.

### I. Relationship to Existing Doctrine

**§VII (Postgres Owns Truth)**: §XVI applies the same epistemic discipline to PUDO detection that §VII applies to query results. Just as we don't trust in-memory caches over Postgres, we don't trust geocoded hypotheses over physical observations.

**§XV (Observation Before Narrative)**: §XVI extends §XV's separation of observation from narrative into the detection layer. §XV says cache writes (observation) are durable while `current_offer_id` (narrative) is provisional. §XVI says stop detection (observation) is the primitive while offer matching (narrative) is the secondary step. Same shape, applied earlier in the pipeline.

**§XIV.I (§5.3 Asymmetric Handling)**: The §5.3 dispatcher continues to operate at the dispatch layer. The Forensic Ladder's Phase 2b/3/4/5 produces match candidates; dispatch resolves any ambiguity among them per §XIV.I. §XVI and §XIV.I compose cleanly.

### J. What This Rule Replaces

§XVI deprecates implicit assumptions that were never written as canonical rules but governed implementation decisions:

- **"The cluster gate is the primary PUDO detector."** Cluster mass remains an input to WAI confidence; it is no longer the *trigger* for PUDO fires. The Forensic Ladder triggers; cluster informs WAI scoring.
- **"Distance to the pin matters for detection."** It doesn't. It matters for WAI confidence calculation only, as one input among several.
- **"PUDOs need confirmation via cluster maturation (~30s)."** No. PUDOs are confirmed via 6s of zero velocity in TAD's destination zone, plus WAI confidence > 0.40. The 30s maturation window was a function of waiting for cluster mass to overcome geocoder noise; §XVI removes the need to wait by inverting the detection-vs-matching order.

The existing cluster-detection code paths remain in place because cluster mass is still useful as a WAI signal. The change is in how the *fire decision* is reached, not in how WAI's inputs are computed.

### K. The Discipline

- When a future change wants to use distance-to-geocode as a gate, refer to this rule and refuse.
- When a future change wants to add per-offer state to the detection layer, refer to this rule and refuse.
- When a future change wants to add asymmetric pickup/dropoff arrest parameters, require evidence that the asymmetry is in transaction physics (not geocoder quality, not address class, not narrative state) before accepting.
- When a future change wants to skip the cache-first check before a Google Places API call, refer to this rule and refuse.

> The map is not the territory. The arrest is the pin.


---

## Notes

- These rules are derived from production lessons across Phase D and Phase E.
- Changes require explicit ratification (not silent edit).
- When a new architectural decision is locked, it gets added here only after it has shipped and stabilized — not before.