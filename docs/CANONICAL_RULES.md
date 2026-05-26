# PuddleJumper Canonical Rules

**Status:** eternal product law. These rarely change. Edits require explicit ratification.
**Load:** at the start of every chat session, before any work.

---

## §0. THE PRIME DIRECTIVE

**Status:** ratified 2026-05-16
**Position:** §0 — ontologically prior to all other canonical rules.
PuddleJumper does not exist without this section. Every other rule
derives from it.

### The product

**PuddleJumper exists to answer one question:**

> *"For where I am right now, at this time, on this day of the year,
> on this day of the week, is this Uber offer good or not, when
> compared to the market?"*

**The market** is defined as the crowd-sourced observed price for this
place, at this time, in this calendar context, on this day of the week.

The answer is computed by comparing a new offer's quoted $/hr and
$/mile against the distribution of past observed quotes for the
matching (place, time-of-day, calendar-context, day-of-week) cohort.

Everything else in the codebase exists to produce, refine, defend, or
deliver that answer.

### A. The objective

Accurate $/hr and $/mile valuation for every (place, time-of-day,
calendar-context, day-of-week) cohort the driver might encounter,
derived from crowd-sourced observed offers, used to evaluate new
offers at the moment they arrive.

The four dimensions are non-negotiable. A pricing model that ignores
any of them fails the directive:

- **Place** — Houston traffic at the Galleria is not Houston traffic
  at IAH. Rates per location are distinct populations.

- **Time of day** — a 7am rush-hour pickup in Sugar Land is not a
  2am surge run from downtown. Rates per hour-of-day are distinct.

- **Calendar context** — Thanksgiving Thursday is not an ordinary
  Thursday. Christmas Eve is not December 23. Rodeo weekends in
  Houston are not ordinary weekends. The dimension is not the
  integer day-of-year (which is a numeric position in the calendar
  that bears no semantic relationship to ride economics); the
  dimension is the **kind of day** for ride demand. This includes
  named holidays (fixed and floating), event days (rodeo, major
  games, concerts), school sessions, seasonal position (summer
  vs. winter for non-special days), and whatever other classifiers
  prove operationally meaningful as the cache populates.

  Implementation note: the specifics of how calendar context is
  classified — flag tables, holiday calendars, event feeds — is
  downstream of §0. The directive only requires that the dimension
  is named correctly and not silently collapsed to integer day-of-year.

- **Day of week** — Friday-night ride volume is not Tuesday-morning
  ride volume. Weekly rhythms persist across all other dimensions.

The cohort an offer belongs to is the intersection of all four.

### B. The mechanism

**Confirmed Pickups** populate the pricing cache. Each pickup
observation records the **quoted economics from the offer card** —
the fare, $/hr, $/mile, effective hourly rate, trip miles, trip
minutes, and pickup miles as Uber communicated them at offer-receipt
time. The pricing cache is a cache of **offers, not of realized
rides.** Realized economics — actual trip duration after traffic,
actual distance after route changes, actual fare after tolls — are
informational color but not directive scope. We compare offers
apples-to-apples on the terms Uber presented them.

**Confirmed Pickups and Dropoffs** populate the geographic cache.
Each observation records canonical coordinates that displace future
Google Geocoding and Places API calls (the "Google Tax").

Both caches are append-only sensors of physical reality. Neither
makes claims about the future. The pricing model — the engine that
answers the Prime Directive's question — reads from the caches at
offer-evaluation time, computes the cohort distribution, and
produces a value verdict.

### C. The implication

**Every pickup observation is the entire product.** A pickup that
observes is product working. A pickup that misses observation is
product missing.

The PUDO logic, the WAI matcher, the §XVI Forensic Ladder, the §XVII
semantic anchor, the §XVIII lost-mode rule, and every future
matcher refinement exists for one reason: **each pickup observation
is one data point in the pricing model that decides whether the
next offer is worth taking.**

Dropoffs are secondary but not optional. They populate the
geographic cache, which reduces the marginal cost of every future
evaluation by avoiding paid Google API calls.

### D. The corollaries

These corollaries are not optional. Every existing canonical rule
derives from them; every future rule must too.

#### D.1 Volume beats precision — within high-confidence limits

The pricing model improves with more observations. **When the
matcher produces high-confidence matches, record them all.** This
includes the legitimate-ambiguity cases where multiple matches
genuinely apply at the same cluster:

- **Single high-confidence match** → record it. The normal path.
- **Multiple high-confidence matches at the same location_type**
  (§XIV.I §5.3 cases — two pickups at one geocode, two dropoffs at
  one geocode) → record observations for all matched offers. Each
  observation is independently correct. The cache writes are
  symmetric to the physical reality (two contracts, one curb).
- **No high-confidence match** → record none. Per D.4, the right
  action is `LogAmbiguousMatch` / `unmatched_reason` with no cache
  write.

Volume beats precision *within the regime where every observation
clears the confidence floor.* It does NOT mean writing low-confidence
guesses to the cache. The §XV doctrine ("we'd rather have a
populated map and a Lost narrative than a Found narrative and a
blank map") presumes the observations being populated were
identifiable as observations in the first place. A guess is not an
observation.

This corollary does NOT apply to narrative state (`current_offer_id`).
Narrative requires precision; observation rewards volume within
confidence bounds. The two are different products of the PUDO
system and obey different rules.

#### D.2 Narrative is an optimization for observation, not its master.

current_offer_id exists because narrative-bound matches are cheaper and more disambiguated than full-queue searches. When the system has high enough confidence to bind narrative, it does — and subsequent matching benefits from the constraint. When narrative breaks, observation continues unimpeded; the caches keep filling; §XIV.I §5.3 handles the additional ambiguity correctly.
The product is the caches. Narrative is the optimization that makes cache writes cheaper. The system strives for narrative because it improves write efficiency, but the system does not depend on narrative for correctness.

#### D.3 Engineering effort allocates by observation impact

When prioritizing work, the question is not "is this technically
elegant" or "does this fix a bug." The question is: **how many
pickup observations per week does this recover, and how common is
the cohort it serves?**

A feature that recovers 1% more pickups in a common (place, time,
calendar-context, day-of-week) cohort beats a feature that recovers
50% more pickups in a rare cohort. A residential side-stub recovery
(decision-not-to-implement, 2026-05-16) is an example of choosing
not to build for a rare cohort. A lost-mode recovery (§XVIII) is
an example of building for a common one (anyone with stacked
offers, which is most accepting drivers).

When two proposals compete, the one with broader cohort impact
wins. This is not "easy beats right" — it is "right is defined
relative to the directive."

#### D.4 Cost of a missed observation is bounded; cost of a wrong observation is unbounded

A missed pickup loses one data point. The pricing cache misses one
fare signal for that cohort. The model gets marginally less
accurate for that (place, time, calendar-context, day-of-week)
intersection until the next observation arrives.

A wrong pickup — firing for offer X when the driver was actually at
offer Y — corrupts the pricing cache for the *wrong* place. The
model gets actively worse at evaluating future offers in either
place because both cohorts now carry false signal.

**When the matcher is genuinely uncertain** (e.g., three or more
overlapping match candidates, or zero candidates above the WAI 0.40
floor), the right action is to log the miss in
`pudo_decision_context` with the appropriate `unmatched_reason`
and move on. Skip beats pollute (§VI canonical). Forensic
visibility of misses is operationally cheap; price corruption is
operationally expensive.

The distinction from D.1 is the regime: D.1 covers the cases where
the matcher has high confidence in multiple candidates (write all of
them — they're each correct). D.4 covers the cases where the matcher
has low confidence in any single candidate (write none of them —
none are supportable). The regimes do not overlap. Both serve the
directive.

#### D.5 Every other metric is a means to this end

Match rate, fire latency, false-positive rate, WAI confidence floor,
TAD gate thresholds, §XVI Phase 2b candidate counts — these are
diagnostic instruments for whether the system is fulfilling the
Prime Directive. None of them is the goal.

A 100% match rate built on 30% false positives serves the directive
worse than an 80% match rate with 0% false positives, because the
former corrupts the pricing model and the latter merely under-fills
it.

When a metric becomes the goal, return to §0. The directive is the
goal. The metrics describe how well we are achieving it.

### E. The discipline — the §0 Test

When any future architectural decision becomes contested, return
to the Prime Directive and apply the §0 Test:

| Question | If yes |
|---|---|
| Does this change increase the rate at which we record correct pickup observations? | priority work |
| Does this change increase the rate at which we record correct dropoff observations? | priority work |
| Does this change reduce the Google Tax via cache hits? | priority work |
| Does this change improve the pricing model's cohort-level accuracy? | priority work |
| Does this change improve narrative accuracy without improving observation? | de-prioritize until observation work is complete |
| Does this change improve a metric that doesn't trace back to pricing-cache accuracy? | refuse, or demand the trace |
| Does this change make the system more elegant without serving the directive? | refuse |

The Test does not say "every change must serve the directive
directly." Infrastructure, tooling, observability, and developer
ergonomics work all serve the directive *indirectly* by enabling
the direct work. The Test asks whether the indirect path traces
back to the directive. If it does, proceed. If it doesn't —
return the work to the backlog and explain.

### F. What this replaces

§0 does not retroactively change any existing canonical section.
Every section from §I through §XVIII is consistent with §0 because
§0 captures what those sections were already serving. The
directive was implicit; §0 makes it explicit.

§0 changes how *future* sections are written:

- New canonical rules must trace their motivation to §0
- New architectural proposals must pass the §0 Test
- New features must justify themselves in observation-impact terms
- Disagreements between contributors resolve by §0 first, then by
  subsequent sections

§0 also changes how existing rules are *read*:

- §VI (1-bit memory) is short because narrative serves observation;
  the minimum viable narrative bit is the right design
- §XV (Observation Before Narrative) is a direct quotation of D.2
- §XVI (Arrest-Defined Truth) is a direct expression of D.4 (the
  physical sensor is bounded; the geocode hypothesis is unbounded)
- §XVII (Semantic Anchor) is a direct expression of B (Google's
  text search resolves cohort identity for venue-class destinations)
- §XVIII (Driver-State Lost Mode) is a direct expression of D.2
  (narrative serves observation; when narrative breaks, observation
  continues)

### G. What this does not do

- **Does not eliminate ambiguity in edge cases.** The directive
  tells us what to optimize for; it does not tell us how to resolve
  every contested design call. The subsequent sections (§I-§XVIII
  and beyond) handle the specifics.

- **Does not justify shortcuts.** Rule VII (do the right thing, not
  the easy thing) operates inside the §0 frame. A "right" solution
  is right *relative to the directive*. A solution that violates
  the directive cannot be the right thing, regardless of how
  technically elegant it is.

- **Does not track realized economics.** Quoted economics from the
  offer card are the canonical record. The cache compares offers
  to offers, all on Uber's quoted terms. If the driver accepts an
  offer and reality diverges (traffic, tolls, route changes), that
  divergence is not a directive concern.

### H. The discipline (future-Claude refusals)

- When a future change wants to optimize a metric that doesn't
  trace to pricing-cache accuracy, refer to §0.D.5 and refuse, or
  demand the trace.

- When a future change wants to prioritize narrative correctness
  over observation volume, refer to §0.D.2 and require evidence
  that observation volume is unaffected.

- When a future change wants to build for a rare cohort while
  common-cohort observation gaps remain unaddressed, refer to
  §0.D.3 and request the cohort-impact comparison.

- When a future change wants to ship a feature whose justification
  cannot be traced to §0 in a sentence, refer to §0 and require
  the trace before reviewing the technical proposal.

- When a future change wants to write low-confidence guesses to
  the pricing cache "because volume beats precision," refer to
  §0.D.1 and §0.D.4 and refuse. Volume beats precision within the
  high-confidence regime only. Outside that regime, skip beats
  pollute.

- When a future change wants to collapse "calendar context" to
  integer day-of-year for simplicity, refer to §0.A and refuse.
  The dimension is the kind of day, not the date arithmetic.

- When a future change wants to record realized trip economics
  (post-traffic, post-toll, post-route-change) into the pricing
  cache as if they were offer data, refer to §0.B and refuse.
  The cache is a cache of offers, not of realized rides.

### The single sentence

> **PuddleJumper is a pricing engine. The PUDO machinery is its
> sensor array. The narrative is the indexing scheme. The product
> is the prices.**

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

Sections §0, I, II, IV, V, VI (post-trim), VII, XIII (post-amendment),
XIV, XV, XVI, XVII, and XVIII are fully current.

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

## §XIV.J — Live-PG Test Floor

**Status:** Canonical (ratified 2026-05-13)
**Date:** 2026-05-13 (revised)
**Origin:** Rule XVI B-2 RealDictRow positional-unpack bug, which shipped to
production despite 611 passing tests because every test mocked
`cur.fetchone()` to return tuples rather than using real RealDictRow.
**Precedent:** Test infrastructure for real-PG already exists in
`tests/conftest.py` — `db_cur` fixture (SAVEPOINT/ROLLBACK per test),
`pg_conn` (session-scoped connection), `_janitor` (autouse session
teardown), sentinel discipline (`TEST_GC_<datestamp>_<uuid>`). The
infrastructure is exemplary; the rule formalizes WHEN to use it.

---

## The Rule

Tests that exercise database-touching code paths MUST use the
`db_cur` fixture (or equivalent SAVEPOINT-isolated real-PG cursor),
not MagicMock cursors.

## The Three Rationales (priority order)

### 1. Row-shape correctness

Cursor row classes (`RealDictRow`, `NamedTupleCursor.Record`, default tuple)
have different iteration, indexing, and unpacking semantics. Mocks model
only one of these and pass when production code uses another. A mock
configured with `cur.fetchone.return_value = ("ts", 5.0)` will pass for any
test that uses positional unpack, regardless of whether production code
runs against a RealDictCursor where the same unpack yields keys.

The B-2 hotfix bug demonstrates this directly. The line
`arrest_started_at_post, arrest_counter_s_post = cur.fetchone()`
worked in 611 tests because every mock returned a tuple. In production
it returned a RealDictRow, which iterates as `('arrest_started_at',
'arrest_counter_s')` — the column NAMES, not values. Production threw
TypeError on every heartbeat. No test would have caught this without
running against a real cursor.

### 2. Type fidelity

psycopg2's type adapters convert Postgres types to Python types at fetch
time. `real → float`, `timestamptz → datetime`, `numeric → Decimal`,
`text[] → list`, `jsonb → dict`. Mocks bypass these adapters entirely.

A test that asserts `result.value == 5.0` will pass whether the production
code receives `5.0` (correct), `Decimal('5.0')` (silent corruption when
later used in JSON serialization), or `"5.0"` (silent corruption on
arithmetic comparison). Real cursors expose type mismatches at the
fetch boundary where they belong.

### 3. SQL correctness

A mock cursor accepts any query string — including syntactically invalid
SQL, references to nonexistent columns, ambiguous joins, broken alias
chains. The production cursor parses every query. Real-PG tests catch
SQL errors at test time; mock tests defer them to production heartbeats.

The Sprint A predicate-alias bug (`bug-4`, diagnosed 2026-05-10 via the
first live-DB test in the suite) is the canonical example: a bare-table
FROM clause that worked in unit tests because the mock never JOINed,
but failed in production with `AmbiguousColumn` whenever the predicate
was composed with `decision_log`.

## The Fixture and the Isolation Pattern

The test infrastructure already exists. The `db_cur` fixture in
`tests/conftest.py` provides:

- **A real psycopg2 cursor** against the production database (RealDictCursor
  by default).
- **SAVEPOINT isolation** per test: `cur.execute("SAVEPOINT test_savepoint")`
  at fixture setup, `ROLLBACK TO SAVEPOINT test_savepoint` at teardown.
  Each test can INSERT/UPDATE/DELETE freely; nothing persists.
- **Sentinel-tagged test data**: tests should use the `test_driver_id`
  fixture (which produces a `TEST_GC_<datestamp>_<uuid>` value) for any
  driver_id they create, so the session-end janitor can sweep any rows
  that escaped rollback (segfault, kill -9, etc.).

A canonical real-PG test:

```python
from psycopg2.extras import RealDictCursor

def test_arrest_counter_realdictrow_unpack(db_cur, test_driver_id, seed_decision_log):
    """Regression: positional-unpacking RealDictRow.fetchone() yields KEYS,
    not values. The B-2 hotfix bug demonstrated this. The matcher must
    access fields by key.
    """
    # Set up: insert a driver_trip_state row for this test driver.
    db_cur.execute("""
        INSERT INTO app_private.driver_trip_state
            (driver_id, arrest_counter_s, arrest_started_at)
        VALUES (%s, 7.5, NOW())
    """, (test_driver_id,))

    # Exercise: the UPDATE...RETURNING pattern from driver_heartbeat.py.
    db_cur.execute("""
        UPDATE app_private.driver_trip_state
        SET arrest_counter_s = 8.0
        WHERE driver_id = %s
        RETURNING arrest_started_at, arrest_counter_s
    """, (test_driver_id,))
    row = db_cur.fetchone()

    # Assertions (the doctrine):
    # Key access yields the value, correctly typed.
    assert isinstance(row['arrest_counter_s'], float)
    assert row['arrest_counter_s'] == 8.0

    # Positional unpack yields KEYS, not values. The negative assertion
    # makes the bug pattern permanent regression: any future code that
    # unpacks positionally will fail this test.
    a, b = row
    assert a == 'arrest_started_at'
    assert b == 'arrest_counter_s'
```

The savepoint rolls back at teardown; the inserted row vanishes; the next
test starts from the same clean state.

## Migration Path

Existing tests that use MagicMock cursors are **grandfathered**. They
are not retroactively migrated.

**New tests** for code that calls `cur.execute()` directly, or that
depends on specific Postgres types being returned, MUST use `db_cur`.

**Bug-fix tests** that demonstrate "the existing tests should have caught
this" MUST migrate as part of the fix. The B-2 hotfix is the precedent.

**Logic-only tests** (math, dispatch case resolution, pure functions) can
remain MagicMock-based. Real-PG tests are not a hammer for nails that
don't exist.

## Known Limitation (until Sub-step B lands)

`db_cur`'s SAVEPOINT chain is destroyed if the code under test calls
`conn.commit()`. The heartbeat handler commits at the end of every
heartbeat. So **end-to-end tests of `post_heartbeat()` cannot use
`db_cur` today** — they would need the commit-suppression infrastructure
documented in conftest.py's docstring ("Sub-step B").

Workaround: test the subcomponents in isolation. The B-2 unpack bug can
be tested without invoking `post_heartbeat()` — exercise the UPDATE +
fetchone + unpack pattern as its own unit (as in the canonical example
above). When commit-suppression lands, integration tests of `post_heartbeat()`
can use the same infrastructure.

## Convention Summary

- Use `db_cur` for the cursor.
- Use `test_driver_id` for any driver_id.
- Use `seed_decision_log` / `seed_offer_history` fixtures (already in
  conftest.py) when you need a row to test against.
- Default to `RealDictCursor` cursor_factory unless the production code
  under test uses a different one.
- Assert both the positive case (key access works) and the negative case
  (unpack yields keys not values) when the test is regression-shaped.

## What This Replaces

Implicit assumption: "Mock cursors with hand-computed SQL return values
are sufficient because tests exercise function behavior, not SQL
correctness."

That assumption was wrong. Tests that exercise function behavior at the
cursor boundary inherit the cursor's behavior; mocking the cursor means
the tests pass against an imaginary cursor and silently diverge from
production.

## The Discipline

- When writing a new test for code that touches the cursor, default to
  `db_cur`. Reach for MagicMock only when the test is pure function logic
  with no cursor interaction.
- When fixing a bug that "the tests should have caught," add a `db_cur`
  test as part of the fix. Do not rely on retrospective audit; bug-driven
  migration is the migration path.
- When a test file becomes a candidate for full real-PG migration (e.g.,
  every test in it touches the cursor), file an issue and migrate the
  whole file rather than letting the conventions diverge within one file.
- When the test under design genuinely needs end-to-end orchestration
  through `post_heartbeat()` or other commit-calling code, **flag it as
  Sub-step B work** — don't try to mock the cursor as a workaround. The
  workaround is the rot this rule exists to prevent.

> The mock matches itself. The real cursor matches Postgres.

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

### C. WAI confidence ≥ 0.40 is the canonical match signal. TAD is input to WAI, not a separate gate.

**Amended 2026-05-22** (ratified via Andrew + Claude + Gemini paired-programming protocol). Previous text required both TAD and WAI gates to pass for offer matching. That doctrine treated TAD as a separate gate when in practice TAD's verdict is already passed into WAI's evaluation via `per_offer_state` and contributes to WAI's confidence score. The dual-gate framing double-counted TAD, and produced a real false-negative class: an offer driven out-of-order from what TAD predicted (the stacked-offer case) had its WAI confidence cleared but its TAD gate blocked, causing the observation to be missed.

A detected arrest fires a PUDO for offer X if:

- **WAI confidence for offer X**: outcome confidence ≥ 0.40 — the matcher's composite spatial verdict, with TAD verdict as one input among many.
- **Arrest counter**: ≥ 5.0 seconds of contiguous zero velocity (§XVI.F Phase 2; see also `ARREST_DURATION_THRESHOLD_S` in `driver_heartbeat.py`).

Both gates together provide the two-key lock: spatial (WAI) + temporal (arrest counter). TAD's verdict is consulted by WAI internally and is preserved in `tad_decision_context` JSONB for forensic analysis, but it does not gate candidate inclusion at the matcher boundary.

**The mistake this rule prevents:** trusting TAD as a load-bearing gate. TAD encodes a hypothesis about which destination the driver is heading to, derived from offer geometry and odometer position. WAI's spatial signals (Heads 1-5 including the §XVII semantic anchor) are ground-truth observations of where the car actually is. When the two disagree, ground truth wins. The previous gate-era doctrine treated TAD's hypothesis as load-bearing; the amended doctrine treats it as advisory.

**§XVIII relationship.** Lost-mode no longer "bypasses" TAD — there is no gate to bypass. Lost-mode's behavior simplifies: WAI runs against all live ACCEPTed offers in the queue, the matcher commits when WAI ≥ 0.40 and arrest fires. The historical "TAD bypass" framing in §XVIII.C is preserved as documentation of the prior doctrine; the §XVIII.C.1 section now notes the subsumption.

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

**§XVIII relationship (post-§XVI.C amendment, 2026-05-22).** Phase 1's TAD pre-filter no longer gates Phase 2 advancement — TAD is input to WAI, not a separate gate. The "lost-mode bypass" framing in §XVIII.C.1 is historical; in the amended doctrine, all heartbeats with live offers advance to candidate evaluation regardless of TAD verdict or lost-mode state.

#### Phase 2 — The Engagement

- **State:** Active neighborhood vigilance.
- **Heartbeat cadence:** 3 seconds.
- **Check:** TAD continues passing for at least one offer + observe velocity.
- **Gate:** Is the car stopped (`speed_mph = 0`) for 2 consecutive heartbeats (6s total)?
- **Transition:** If yes AND a stop is observed → Phase 2b. If TAD drops for all offers → return to Phase 1.

Phase 2 is the watching window. The driver is in the destination zone; the system is paying closer attention but not yet committed.

#### Phase 2b — The Arrest

- **State:** Preliminary commitment.
- **Trigger:** 5s of contiguous zero velocity reached in Phase 2 (threshold lowered 6.0→5.0 per §XVI.C amendment, 2026-05-22; paired with 1Hz Horny cadence delivering 5 confirming samples).
- **Check:** WAI confidence on the best-matching offer.
- **Gate:** Is WAI confidence ≥ 0.40 for any offer? (TAD verdict is internal to WAI's confidence calculation, not a separate gate — see §XVI.C amended 2026-05-22.)
- **Transition:** If yes → mark PUDO event as "happened" (commit intent), proceed to Phase 3. If no → return to Phase 2 (this is a stoplight, traffic, etc., not a transaction).

Phase 2b is where the PUDO is *marked* — the system commits that an event happened — but the coordinates are not yet finalized. Coordinates remain refinable through Phase 4.

**§XVIII relationship (post-§XVI.C amendment).** Lost-mode's candidate set is the same as cold-mode's: every offer in the live queue. The previous "expands to every ACCEPTed live-queue offer regardless of TAD verdict" framing is now redundant — all candidates are considered regardless of TAD verdict in both modes. Lost-mode's distinct behavior is now limited to (a) demoting narrative fires to observation fires per §XVIII.C.4, and (b) firing on offers without an existing narrative anchor.

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

**The Transaction Lock is the primary defense against same-leg refire.** SQL idempotency guards (`WHERE actual_pickup_at IS NULL`) operate at the storage boundary and prevent duplicate column writes, but the underlying matcher continues to evaluate and emit lock-eligible actions on every heartbeat — producing redundant `pudo_decision_context` rows and consuming heartbeat cycles even when the writes are no-ops. The Transaction Lock prevents the match-and-emit cycle from running at all when a recent fire's release conditions haven't been met. The SQL idempotency guards remain in place as belt-and-suspenders below the Lock.

**Status:** implemented 2026-05-26 as Sprint A. Artifacts:
- `migrations/2026-05-26_sprintA_driver_trip_locks.sql` — lock state table
- `migrations/2026-05-26_sprintA_speed_streak_columns.sql` — temporal release substrate on `driver_trip_state`
- `decisions/transaction_lock.py` — application API (acquire_lock, is_locked, release_lock, LockContext)
- `tests/test_transaction_lock.py` — 13-test verification matrix
- `driver_heartbeat.py` matcher integration (12 surgical edits, commit 3a49d70)
- Forensic context: `docs/sprint_notes/xvi_c_validation_amendment_brief_2026-05-26.md`

Other offers' legs are not locked. A pickup fire for offer X does not block a pickup fire for offer Y at a different location, nor a dropoff fire for offer X later in the leg.

### H. Forensic Record

When a PUDO fires via the Forensic Ladder, the `pudo_decision_context` row records:

- `arrest_started_at`: when the 0.0 mph counter began
- `arrest_duration_s`: total contiguous zero-velocity time at fire
- `matched_offer_id`: which offer the matcher selected
- `match_signal`: which path produced the match (`wai_above_floor` for single-match commit, `dispatch_resolved` for §5.3 ambiguity resolution, or one of the §XVIII lost-mode signals per §XVIII.D.1). The legacy values `tad_and_wai` and `tad_and_wai_ambiguous` are deprecated per §XVI.C amendment (2026-05-22) — pre-amendment rows retain the historical labels and remain forensically valid.
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
- `unmatched_reason`: which precondition or gate failed. Values:
    - `tad_failed`: **DEPRECATED 2026-05-22** per §XVI.C amendment — TAD is no longer a separate gate, so it can no longer "fail" as one. Pre-amendment rows retain this label and remain forensically valid. New rows that would have emitted this label now emit `wai_below_floor` (the collapsed reason: no offer's WAI confidence cleared the canonical 0.40 floor).
    - `wai_below_floor`: TAD passed but WAI confidence < 0.40 floor
    - `both_failed`: TAD and WAI both rejected (legacy, retained for back-compat)
    - `queue_actually_empty`: `snap.offers` was empty — no live offers in queue (Bug B'-1 surface; time-horizon scrubbing)
    - `cluster_unavailable`: `diagnostics.cluster is None` — cluster detector returned no cluster despite arrest
    - `odometer_unavailable`: `cumulative_miles is None` — heartbeat body lacked odometer (structurally impossible from current Android client; firing this label is itself an alert)
    - `tad_skipped_unknown`: queue non-empty, cluster present, odometer present, TAD still didn't run — **Bug B'-2 recurrence sentinel**. When this label fires, a Cloud Run WARNING tagged `[Bug B'-2]` is also emitted carrying driver_id, snap.offers length, and cumulative_miles for investigation.
    - `lock_suppressed`: one or more candidates cleared the WAI floor but were caught by the §XVI.G Transaction Lock from a recent fire on the same `(offer_id, pudo_type)`. The forensic payload (locked offer IDs, lock_age_s, current vs. target release metrics for both spatial and temporal axes) is threaded into `tad_decision_context.lock_suppressions` as a JSONB array. Distinct from `wai_below_floor` because candidates *did* satisfy the matcher; the lock suppressed emission. Sprint A (2026-05-26).
    - one of the §XVIII lost-mode reasons per §XVIII.D.2
    - **Deprecated:** `empty_queue` was the historical conflated label; replaced 2026-05-18 by the four-way split above. Pre-2026-05-18 PDC rows retain `empty_queue` and should be interpreted as "one of {queue_actually_empty, cluster_unavailable, odometer_unavailable, tad_skipped_unknown} but unknowable which without replay."
- `phase_reached`: highest phase reached before failure

These forensics make false negatives investigable. A miss is not silent — every detected stop has a row, regardless of whether it produced a fire.

### I. Relationship to Existing Doctrine

**§VII (Postgres Owns Truth)**: §XVI applies the same epistemic discipline to PUDO detection that §VII applies to query results. Just as we don't trust in-memory caches over Postgres, we don't trust geocoded hypotheses over physical observations.

**§XV (Observation Before Narrative)**: §XVI extends §XV's separation of observation from narrative into the detection layer. §XV says cache writes (observation) are durable while `current_offer_id` (narrative) is provisional. §XVI says stop detection (observation) is the primitive while offer matching (narrative) is the secondary step. Same shape, applied earlier in the pipeline.

**§XIV.I (§5.3 Asymmetric Handling)**: The §5.3 dispatcher continues to operate at the dispatch layer. The Forensic Ladder's Phase 2b/3/4/5 produces match candidates; dispatch resolves any ambiguity among them per §XIV.I. §XVI and §XIV.I compose cleanly.

**§XVI.G (Transaction Lock)**: the lock is consulted at Phase 2b *after* candidate identification but *before* action emission. Candidates that satisfy the WAI confidence floor but are locked from a recent fire on the same `(offer_id, pudo_type)` produce a `lock_suppressed` PDC row (per §XVI.H taxonomy) instead of being emitted. The §XVI Forensic Ladder's Phase 1/2/2b/3/4/5 physics-based detection runs unchanged; the lock filters *which* of the detected candidates fire. Stacked offers maintain independent locks per Sprint A composite-key design (driver_id, offer_id, pudo_type), so locking offer A's pickup does not block offer B's pickup.

**§XVIII (Driver-State Lost Mode)**: §XVIII conditionally overrides the TAD gates at Phase 1 (pre-filter) and Phase 2b (candidate set) when the driver is in lost-mode. The physics-based arrest detection (Phase 2 velocity counter, Phase 3 POI lookup, Phase 4 hill-climb, Phase 5 notarization) runs unchanged in both modes. See §XVIII for the full specification.

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

## XVII. SEMANTIC ANCHOR — THE OFFER IS THE QUERY

**Ratified:** 2026-05-14
**Companions:** §V (Uber Data Reality), §XV (Observation Before Narrative),
§XVI (Arrest-Defined Truth)
**Replaces in practice:** the token-driven POI matching strategy that
required maintaining `_EXTENDED_POI_TOKENS` / `_AIRLINE_AIRPORT_TOKENS` /
`CLASS_TO_TYPE_MAP` as Houston-specific data structures. Those structures
remain useful for §XVI's noise-gate and witness signals, but they no longer
gate destination matching.

### The principle

The offer's destination text is a Google Places query. Resolve it via
Places Text Search to a set of **semantic anchors** — real-world POIs
that the destination string refers to. The PUDO fires when the driver
arrests within the venue horizon of any anchor.

This inverts the previous design. The previous matcher asked: *"What
POIs are near the car? Does any of their name/type match the offer?"*
That question fails for airport-class destinations because Phase 3's
50m searchNearby at the arrest coordinates returns POIs like "Female
Bathroom" — accurate for what's nearest the car, useless for confirming
identity against the offer.

The new matcher asks: *"Where does the offer say we're going? Is the
car there?"* For "United, Houston, Texas" Google's Text Search returns
the United terminals, the United Club, the United Bag Drop, the IAH
polygon centroid, and the United Houston Corporate Support Center —
the actual venue footprint. The car at `(29.9869, -95.3350)` is 88m
from one of those anchors. Match.

### A. The text query is the offer's destination text, unmodified

`places:searchText` is called with the offer's `dropoff_address` (or
`pickup_address` when in pickup leg) **as-is, no preprocessing**, no
tokenization, no lexicon lookup, no canonicalization. Google's text
search is robust to messy address strings.

The only structured parameter is `locationBias.circle`:

- center: fixed Houston market center `(29.7604, -95.3698)` per
  §XVII Patch 3 ratification 2026-05-14 (Andrew + Gemini). Not the
  cluster centroid. Fixed bias eliminates cache fragmentation —
  IAH's 7+ sub-clusters share one cache row per unique offer text,
  trading 1% relevance for ~7x cache hit rate. When PuddleJumper
  expands beyond Houston, this becomes a per-market lookup keyed
  on driver location.
- radius: 50,000 meters. Wide enough to catch all metro destinations
  even when the offer text matches a national chain ("Hilton, Houston,
  Texas" finds Houston Hiltons, not the New York Hilton).

`maxResultCount: 20` — captures enough anchors for high-density venues
like airports without inflating cost.

### B. The arrest gate runs first; §XVII is the matcher inside Phase 2b

§XVII does NOT change the §XVI Forensic Ladder. It plugs in as the
matcher consulted at Phase 2b:

1. **Phase 1** (passive): TAD odometer flags destination zone.
2. **Phase 2** (active): 6s of `speed_mph = 0` accumulates.
3. **Phase 2b** (the new entry point): §XVII fires `places:searchText`
   for each TAD-passing offer's current-leg address. Computes anchor
   distances. Matcher score = `max(0, 1.0 − dist_m / horizon_m)` per
   anchor. The score that crosses WAI's 0.40 floor (and the §XVI
   commit logic above it) triggers Phase 3.
4. **Phase 3-5** (notarize): unchanged.

This means highway-speed drive-bys never reach §XVII. The arrest gate
already filters them. §XVII's "false positive" envelope is bounded by
"places where someone could have arrested for 6s+", which excludes the
vast majority of false positives by construction.

### C. Per-anchor-type horizons (the matcher's only classification step)

The horizon for each returned anchor is derived from the anchor's own
`types` array — NOT from the offer text, NOT from any lexicon we
maintain.

| Anchor type contains            | Horizon |
|---------------------------------|---------|
| `airport`, `international_airport` | 1000m |
| `stadium`, `tourist_attraction` | 600m |
| `university`, `shopping_mall`   | 500m |
| `hospital`, `medical_clinic`    | 150m |
| `lodging`                       | 150m |
| (anything else)                 | 500m default |

Rationale: airports and stadiums are huge polygons with sprawling
adjacent infrastructure; passenger-drop happens anywhere in the
complex. Hospitals are tight footprints; the driver must reach the
specific building. The horizons reflect real-world venue geometry.

**This is the only category step in §XVII**, and it operates on
Google's classifications, not ours. Adding a new market doesn't require
extending any lexicon — Google's `airport` taxonomy already covers JFK,
LAX, Heathrow, etc.

When multiple anchors return, each uses its own type-based horizon.
The winning anchor is the one with the highest score (linear-decay
weighted by its own horizon), not the closest in raw meters.

### D. Linear decay confidence weighting

For each returned anchor `a` with horizon `h_a`:

```
score_a = max(0, 1.0 − dist(cluster, a) / h_a)
```

The §XVII signal score for the offer = `max(score_a for a in anchors)`.
The winning anchor's name and type are recorded as witness.

At 0m → score 1.0. At horizon → score 0.0. Outside horizon → no
contribution. This produces graceful degradation when the driver is
near but not at the venue, and lets the §XVI commit logic (weighted
confidence ≥ 0.40 floor, or higher with `poi_type_match`) use the
score in its existing arithmetic without special-casing.

### E. The cache — extension of existing `poi_cache`, not a parallel table

§XVII reuses `app_private.poi_cache`. The table is extended with two
nullable columns:

- `text_query text` — the searchText query string. NULL for legacy
  searchNearby rows; non-NULL for §XVII searchText rows.
- `bias_radius_m double precision` — the locationBias circle radius
  used in the API call.

Dual access patterns share one table:

| `text_query` | Access pattern | Cache key | TTL |
|---|---|---|---|
| `NULL` | searchNearby (legacy) | `(query_lat, query_lng)` via GIST | 30 days |
| `NOT NULL` | searchText (§XVII) | `(text_query, query_lat, query_lng, bias_radius_m)` via partial UNIQUE | 365 days |

**TTL rationale.** SearchNearby cache entries reflect transient
business presence near a point — Starbucks opens, the strip mall
tenants rotate. 30 days is the existing setting and remains correct.
SearchText cache entries reflect venue identity — "United Airlines"
terminals at IAH do not relocate quarterly. 365 days is conservative
even for venue cache; could go longer.

**`last_hit_at` semantics.** Cache hits on either mode touch
`last_hit_at`, throttled to once per hour per row (matches existing
`_read_cache` behavior in `poi_service.py`). Forensic value: lets us
detect cold cache entries for pruning, and confirms which text queries
are most active in production.

**Idempotent dedupe.** New searchText writes use
`INSERT ... ON CONFLICT (text_query, query_lat, query_lng, bias_radius_m)
WHERE text_query IS NOT NULL DO NOTHING`. The partial UNIQUE index
guarantees one row per `(query, bias)` tuple. Legacy searchNearby rows
are unaffected by the partial index.

**Migration.** Idempotent DDL script `tmp/migrate_poi_cache_xvii.sql`
adds the columns + indexes. Safe to run any number of times. Required
prerequisite before §XVII production code lands or before the Tier A
backtest runs.

### F. Witness format

The `pudo_decision_context.poi_top_names` column extends to carry
semantic-anchor witnesses. Format:

```
semantic_anchor:{name}/{primary_type} ({dist_m}m)
```

Example: `semantic_anchor:United/transportation_service (88m)`.

The primary_type is the first element of the anchor's `types` array
(Google sorts these by relevance). For forensic queries, the full
`places` blob is in `poi_cache.places` and joinable via
`text_query = dropoff_address`.

### G. Composition with existing matcher signals

§XVII becomes Head 5 of `_signal_*` in `where_am_i.py` as
`_signal_semantic_anchor`. §XVII Patch 3 Full also replaced the
legacy `_match_poi_stub` matcher with `_match_poi_class` (a
first-class implementation, no longer a stub). The existing
heads (Head 1 fuzzy, Head 2 branded, Head 3 airport-type, Head 4
poi_type_match) remain in place. The final signal score is:

```
final_score = max(head1, head2, head3, head4, head5_semantic_anchor)
```

When Head 5 wins, the witness reflects it. When an existing head
wins (e.g. an apartment-complex match via Head 1 fuzzy at a residential
address with no anchor structure), that head's witness wins instead.

Existing token-driven heads continue to work for cases where Google
Text Search returns nothing useful (residential intersections,
single-road dropoffs in suburbs with no nearby commercial venues).
§XVII covers the venue-class gap they were never designed for.

### H. What this does NOT do

- **Does not deprecate `_EXTENDED_POI_TOKENS`.** That set remains in
  use for Head 2 (branded co-reference) and `_signal_poi_type_match`.
  Its scope tightens: it's a *secondary* signal at addresses that
  already passed Head 5 or that Head 5 missed. The two-tier system
  is intentional — anchor primary, tokens secondary.
- **Does not require offer-text-to-category derivation.** No
  `CATEGORY_LEXICON` in the §XVII path. The lexicon-keyed approach
  was discussed and rejected on 2026-05-14 because it reintroduces
  the maintenance trap §XVII is built to eliminate.
- **Does not change the §XVI Phase economy.** Phase 1 still runs TAD
  cheap; Phase 2 still requires 6s arrest; the API call is still
  cache-first and per-arrest-event, not per-heartbeat.
- **Does not create a parallel cache.** §XVII extends `poi_cache`,
  the table already in production. One table to operate.

### I. Forensic record

`pudo_decision_context` rows during §XVII evaluations gain:

- `poi_lookup_source` extends from `{cache_hit, api_call, api_error}`
  to also include `{semantic_cache_hit, semantic_api_call,
  semantic_api_error}` to disambiguate which endpoint sourced the data.
- `poi_top_names` carries anchor witnesses prefixed `semantic_anchor:`.
- `poi_match_score` carries the linear-decay score of the winning
  anchor when Head 5 wins.
- The `poi_cache.places` blob preserves the full anchor list (name,
  type, lat/lng) for offline analysis. JOIN by
  `text_query = offer.dropoff_address`.

### J. Out-of-band scripts

Backtest scripts (`tmp/backtest_xvii_*.py`) MAY write production cache
rows from historical data. This is a deliberate exception, not a
violation: the cache rows are correct and useful regardless of which
process wrote them. Per §XIV.H Out-of-band Exceptions, each such
script documents its exception in
`docs/out_of_band_offer_history_queries.md`.

### K. The discipline

- When a future change wants to add an "airport-specific arming"
  branch in the matcher, refer to this rule and refuse. The arrest
  gate fires for airports the same way it fires for hospitals.
- When a future change wants to extend a per-market token lexicon
  with new airline names or hospital chains, refer to this rule and
  refuse. The anchors come from Google Text Search; no maintenance
  burden.
- When a future change wants to call `places:searchNearby` again for
  primary destination matching, refer to this rule and refuse. The
  inversion is the point.
- When a future change wants to create a parallel cache table for
  "semantic anchors specifically," refer to this rule and refuse.
  Extension of `poi_cache` is the canonical pattern.
- When a future change wants to ship a placeholder/stub
  implementation in production hot-path code (matchers, dispatchers,
  signal heads), refer to this rule and refuse. Stubs accumulate
  into invisible failure modes — the `_match_poi_stub` 2026-05-09
  production bug (50+ TypeErrors in 75 minutes; Android backed off
  to exponential, heartbeat cadence collapsed) is the canonical
  example. Ship first-class implementations, even when they're
  simple. "No more stubs" is the §XVII Patch 3 Full ratification.

### L. Validation evidence

The §XVII implementation was validated end-to-end via the IAH
ride-7883 replay on 2026-05-14. Ride 7883's dropoff
("United, Houston, Texas") never fired in production
(`actual_dropoff_at IS NULL`). After §XVII Patches 1-4b landed,
replaying the ride against the production codebase produced 6
contiguous arrest frames at the canonical IAH coordinate
(29.986899, -95.335009), all with:

- `target_address = 'United, Houston, Texas'`
- `semantic_anchor_score = 0.8241866559386112`
- `semantic_anchor_witness = 'semantic_anchor:United/transportation_service (88m)'`
- `semantic_lookup_source = 'semantic_cache_hit'`
- composed confidence → `FireDropoff`

The replay's 0.8242 score matches the Tier A backtest's prediction
to four decimal places. The 12-place anchor set is preserved
canonically in `tests/fixtures/iah_united_anchors.json`. The runtime
cache row is keyed `(text_query='United, Houston, Texas',
query_lat=29.7604, query_lng=-95.3698, bias_radius_m=50000)` against
`app_private.poi_cache` — discoverable by query, not by `id`. (At
time of ratification: `id=349`, expires 2027-05-14, but the row may
rotate; the fixture file is the durable artifact.)

§XVII Patch 5 (commit `37a4c65`) codifies this validation as a
regression-test fixture (`tests/fixtures/iah_united_anchors.json`)
with seven test cases covering: the IAH peak-score regression, the
365-day cache TTL contract, the per-type horizon constants, the
linear-decay clamp at horizon, the empty-input contract, and the
"Iron Curtain" defensive predicate that prevents searchText rows
from leaking into spatial cluster reads.

Future changes to §XVII Head 5 (`_signal_semantic_anchor`),
`_SEMANTIC_TYPE_HORIZON_MAP`, or `_flat_earth_m` must keep
`tests/test_semantic_anchor.py` green. If the IAH regression test
fails, the change is wrong before it's reviewed.

> The map is not the territory. The arrest is the pin. But to know
> which arrest matters — ask the offer where it was going.

---

## XVIII. DRIVER-STATE LOST MODE

**Ratified:** 2026-05-16
**Companions:** §0 (Prime Directive), §VI (1-bit memory), §XIV.H (live offer predicate), §XIV.I (§5.3 asymmetric handling), §XV (observation before narrative), §XVI (arrest-defined truth)
**Replaces in practice:** the per-offer `lost_mode_reason: narrative_violation` formulation inside `tad_decision_context.verdicts.X`; the top-level `tad_decision_context.lost_mode` boolean as a stored field

### The principle

Lost-mode is the natural operating state of a PUDO system whose narrative isn't bound. It is not an error condition. It is observation-mode without the narrative optimization.
The system is always observing. The system binds narrative when a pickup observation crosses confidence against a single offer in the queue — that's the optimization that lets subsequent matches consult a smaller search space. When narrative is unbound (because no pickup has fired yet, or because a dropoff just cleared narrative, or because §5.3-mirror ambiguity refused to commit), the system continues observing — just without the optimization.
Lost-mode rules are not recovery rules. They are the un-optimized fallback path that the system always falls back to when the optimization isn't available.

The previous per-offer formulation produces four pathologies, all
observed in production on the 2026-05-15 drive:

1. **Asymmetric recovery.** Only an offer whose `completion_pct > 1.15`
   could enter `lost_mode_reason: narrative_violation`. Offers whose
   completion was deeply negative (driver hadn't traveled to their
   anchor yet) stayed `passed: false` with no recovery affordance,
   even when the physical reality matched a lost driver.
2. **Unreachable for stacked offers.** Offers whose pickups never
   fired never had narrative engaged, so `narrative_violation` was
   structurally unreachable. Every offer accepted after the first
   missed pickup was invisible to lost-mode recovery.
3. **TAD remains authoritative when it shouldn't.** Without a
   driver-level lost flag, every per-offer TAD verdict ran its math
   against poisoned anchors and honestly returned `passed: false`,
   blocking recovery on inputs the system itself didn't trust.
4. **Incoherent forensic record.** `tad_decision_context.lost_mode:
   false` at the top level while a per-offer verdict reported
   `lost_mode_reason: narrative_violation` made historical queries
   for "drives where the driver was lost" either over- or under-count
   depending on which signal was consulted.

Driver-state lost-mode fixes all four in one stroke.

### A. The trigger — two bits, derived not stored

A driver is in lost-mode when **both** of the following hold:

1. `driver_trip_state.current_offer_id IS NULL` for that driver.
2. The driver's live offer queue contains at least one offer with
   `actual_pickup_at IS NULL`, where "live" is determined exclusively
   by `LIVE_OFFER_PREDICATE_SQL` per §XIV.H. The trigger does NOT
   consult `app_verdict` — the PUDO system has no prior knowledge of
   the driver's accept/decline decision; the car's physical position
   is the sole sensor of driver intent (§0.B, §XV).

Both bits are derived from existing canonical sources. Lost-mode is
**not** a stored column. No `driver_trip_state.is_lost` field will
be added.

The derivation is mathematically incapable of drifting from reality —
the state evaluates ground truth dynamically on every consultation.
Stored state introduces a synchronization matrix: every container
restart, transaction rollback, and offer expiry would need a handler
to keep `is_lost` aligned with the underlying bits. The derived form
has no such matrix because the underlying bits ARE the state.

#### Exit from lost-mode

Lost-mode exits automatically when either bit flips:

- `current_offer_id` binds (next clean ACCEPT cycle engages narrative), or
- The queue empties of unmatched ACCEPTED offers (existing
  `LIVE_OFFER_PREDICATE_SQL` GC sweeps them per §XIV.H — no parallel
  cleanup process)

There is no `ClearLostMode` action. There is no stored flag to clear.

### B. The Physical Sensor Axiom

> *"The driver's physical presence IS our sensor."*

§XVIII rests on this axiom. Per §XV, observation is the primary
output. The driver's GPS arrest at a specific location is a physical
fact, independent of whether the system's narrative state is intact.

If an offer is alive in the queue (passes `LIVE_OFFER_PREDICATE_SQL`,
not yet GC'd) and the driver physically arrests at that offer's
destination geocode, the engine MUST record the observation. The
arrest is the sensor reading. Whether the driver consciously attempted
that offer is unknowable from outside the driver's head, and §XV
explicitly rejects inferring intent from physical observation.

This axiom resolves an objection that may arise: "what if the driver
mentally skipped offer X but physically stopped at its curb anyway?"
The answer: cache the observation. The pricing cache gets a fare
signal. The geographic cache gets a coordinate pin. Both are correct
regardless of the driver's intent. The narrative does not engage
(observation-only per §C below), so no corruption risk to
`current_offer_id`.

### C. Behavioral rules during lost-mode

When the driver is in lost-mode, the following apply at every
heartbeat where §XVI Phase 2b is consulted:

#### C.1 TAD bypass (subsumed by §XVI.C amendment 2026-05-22)

**Status: historical.** The §XVI.C amendment (2026-05-22) removed TAD as a separate gate. There is no longer a TAD gate to bypass in lost-mode — all heartbeats with live offers evaluate WAI regardless of TAD verdict or lost-mode state. The text below is preserved as documentation of the prior doctrine and explains the rationale that drove the §XVI.C amendment.

**Historical text follows:**

#### C.1 TAD bypass [historical]

The TAD distance gate is **not consulted** for any queued offer's
evaluation. TAD anchors during lost-mode are mathematically poisoned
(the previous failed pickup left expected_dropoff_* predictions
uncorrected per §3b24239's design constraint — re-anchor fires only
at successful pickup fire). Consulting TAD in this state gates
legitimate recoveries on inputs the system itself does not trust.

The §XVI Forensic Ladder Phase 1's TAD pre-filter is similarly
bypassed in lost-mode. Phase 1 normally exits early when no offer
has a plausible TAD position; in lost-mode, no offer has a plausible
TAD position by definition, but that fact does not justify exiting
Phase 1. The driver may be at any queued offer's destination.

#### C.2 Expanded Phase 2b candidate set (subsumed by §XVI.C amendment 2026-05-22)

**Status: historical.** The §XVI.C amendment (2026-05-22) removed TAD
as a separate gate. Phase 2b now consults every offer in the
`LIVE_OFFER_PREDICATE_SQL`-filtered queue regardless of lost-mode
state — the "expansion" this section describes is no longer
lost-mode-specific; it is the canonical Phase 2b behavior in all
modes. The text below is preserved as documentation of the prior
doctrine.

**Post-amendment behavior:** Phase 2b consults every offer in the
live queue with `actual_pickup_at IS NULL`. WAI confidence is
computed against each. The 0.40 floor (§XIV.C) is the canonical
match signal; TAD verdict is one input to WAI's confidence
calculation, not a separate gate. The candidate set is NOT filtered
by `app_verdict` (per §0.B and §XV) — physical arrest at an offer's
geocode reveals driver intent after the fact.

**Historical text follows:**

§XVI Phase 2b normally consults TAD-passing offers. In lost-mode,
Phase 2b consults **every offer** in the `LIVE_OFFER_PREDICATE_SQL`
-filtered queue with `actual_pickup_at IS NULL`. WAI confidence is
computed against each. The 0.40 floor still applies per §XIV.C.

The candidate set is NOT filtered by `app_verdict`. Per §0.B and §XV,
the system has no prior knowledge of which offers the driver chose to
drive; the car's physical arrest at an offer's geocode is the sensor
that reveals driver intent, after the fact.

#### C.3 §XIV.I §5.3 dispatcher rules apply normally

When Phase 2b produces multiple matches at the same `location_type`
during lost-mode, §XIV.I §5.3 fires unchanged:

- **§5.3 pickup case** (two pickups same geocode): recency tiebreaker
  on `OfferMeta.created_at`. Most recent → `FirePickupObservation`
  (not `FirePickup` per §C.4 below). Others → `FirePickupObservation`.
- **§5.3-mirror dropoff case** (two dropoffs same geocode): emit
  `FireDropoffObservation` for every tied offer; emit
  `ClearNarrative` (no-op in lost-mode since narrative is already
  clear); do not emit `FireDropoff`.
- **Three-or-more matches**: emit `LogAmbiguousMatch`; do not modify
  state.

#### C.4 Observation-only fires

All fires during lost-mode are observation fires, not narrative fires:

- `FirePickup` → `FirePickupObservation`
- `FireDropoff` → `FireDropoffObservation`

The caches populate per §XV (pricing cache from
`FirePickupObservation`, geographic cache from both). `current_offer_id`
does NOT bind. The narrative remains explicitly unknown until the
next clean ACCEPT cycle re-engages it.

This is the cost of being lost: we cache the observation, we do not
commit the narrative. A narrative commit during lost-mode would
require the system to claim certainty it does not have, and §XV
forbids that ("we would rather have a populated map and a 'Lost'
narrative than a 'Found' narrative and a blank map").

### D. Forensic record

`pudo_decision_context` rows during lost-mode evaluations gain new
canonical values:

#### D.1 New `match_signal` values

- `lost_mode_observation` — single match in lost-mode produced an
  observation fire
- `lost_mode_ambiguous_observation` — multiple matches in lost-mode
  produced observation fires per §XIV.I §5.3

#### D.2 New `unmatched_reason` values

- `lost_mode_no_candidate` — driver in lost-mode, Phase 2b consulted
  full queue, no offer's WAI confidence cleared the 0.40 floor
- `lost_mode_three_plus_matches` — driver in lost-mode, Phase 2b
  produced N≥3 matches; per §XIV.I.3, state was not modified

#### D.3 Derived `tad_decision_context.lost_mode`

The top-level `lost_mode` field in the JSONB blob becomes a derived
boolean reflecting whether the driver-state lost-mode condition
holds at evaluation time. Per the trigger in §A, this is computed
from `current_offer_id` and the queue, not stored.

The per-offer `verdicts.X.lost_mode_reason` field is **deprecated**.
Existing code that writes `lost_mode_reason: narrative_violation`
into this field continues to write it for backward compatibility
with historical forensic queries, but new code MUST NOT depend on
it. The driver-state flag is the canonical source going forward.

#### D.4 Forensic discoverability

The dashboard or any operational query needing to surface "drivers
currently in lost-mode" derives the answer from the same two bits:

```sql
SELECT dts.driver_id, dts.heartbeat_at
FROM app_private.driver_trip_state dts
WHERE dts.current_offer_id IS NULL
  AND EXISTS (
    SELECT 1 FROM app_private.offer_history oh
    WHERE oh.driver_id = dts.driver_id
      AND oh.actual_pickup_at IS NULL
      AND <LIVE_OFFER_PREDICATE_SQL with oh alias>
  );
```

Historical queries against `pudo_decision_context` find lost-mode
fires by `match_signal IN ('lost_mode_observation',
'lost_mode_ambiguous_observation')` and lost-mode misses by
`unmatched_reason IN ('lost_mode_no_candidate',
'lost_mode_three_plus_matches')`.

No new tables. No precomputed `driver_lost_mode_log`. No
materialized view of lost-mode duration. Per Rule VII (do the right
thing, not the easy thing) and §VII (Postgres owns the truth):
queries against the canonical sources answer all forensic questions
without parallel state.

### E. Validation case (2026-05-15 drive)

Seven offers (7918-7924) demonstrate every failure mode this rule
fixes:

| Offer | App verdict | Pickup fire | Dropoff fire | TAD verdict at 21:57:53 |
|---|---|---|---|---|
| 7918 | ACCEPT | none | 21:57:53 via lost_floor side-channel | `passed: null, lost_mode_reason: narrative_violation` (completion 1.626) |
| 7919 | DECLINE | n/a | n/a | `passed: false` (completion -0.032) |
| 7920 | DECLINE | n/a | n/a | `passed: false` (completion -1.980) |
| 7921 | ACCEPT | none | none | `passed: false` (completion -3.294) |
| 7922 | DECLINE | n/a | n/a | `passed: false` (completion -9.257) |
| 7923 | ACCEPT | none | none | `passed: false` (delta -57.819mi outside ±0.5mi) |
| 7924 | DECLINE | n/a | n/a | not yet in queue |

At 21:57:53, two ACCEPTed offers (7918 and 7923) shared the dropoff
address "Highway 6, Missouri City, Texas." The driver was physically
at 7918's dropoff coordinates. Under the previous per-offer rule:

- Only 7918 entered lost-mode (its completion overshot the 1.15 ceiling)
- 7923's TAD honestly reported `passed: false` because the driver
  had only had 18 seconds since accepting 7923
- 7921's TAD honestly reported `passed: false` because its anchors
  had compounded off 7918's stale receipt-time predictions
- The §XVI Phase 2b matcher consequently reported `match_signal:
  no_match` and `matched_offer_id: NULL`
- A side-channel (WAI confidence 0.86 + TAD `committed:
  [{"offer_id": "7918", "commit_rule": "lost_floor"}]`) fired
  `FireDropoff` for 7918
- §XIV.I §5.3-mirror was **silently violated**: two ACCEPTed offers
  shared the dropoff geocode but the system fired narrative for one
  unilaterally rather than firing observations for both and clearing
  narrative

Under §XVIII:

- Driver was in lost-mode the entire drive (current_offer_id was
  NULL from 21:28:35 onward; live ACCEPTed queue had unfired pickups)
- Phase 2b at 21:57:53 evaluates 7918, 7921, 7923 against WAI (the
  ACCEPTed offers in queue)
- WAI returns matches for both 7918 and 7923 at the Highway 6 geocode
- §XIV.I §5.3-mirror fires: `FireDropoffObservation(7918)`,
  `FireDropoffObservation(7923)`, `ClearNarrative` (no-op)
- Both `offer_history` rows get `actual_dropoff_at` populated and
  coordinates recorded
- `match_signal: lost_mode_ambiguous_observation`
- `current_offer_id` remains NULL (was already NULL)
- The geographic cache gains two dropoff coordinate pins

The system honestly records "two offers' dropoffs were observed at
this location; we cannot disambiguate the narrative." This is more
correct than "we committed 7918's narrative via a side-channel and
hoped." If 7923 was actually a re-bid of 7918 (same passenger), the
double observation is harmless — both rows record the same arrest
coordinates. If 7923 was a stacked second ride, the system correctly
records that we cannot disambiguate.

### F. What this does not do

- **Does not eliminate the missed pickup that started the cascade.**
  §XVIII handles the symptom (TAD gates blocking recovery when
  anchors are poisoned) not the cause (the WAI sub-floor failure at
  7918's pickup). The cause is the broader problem space of
  residential side-stub geometries documented in the 2026-05-16
  decision-not-to-implement record.
- **Does not introduce time gates.** No "lost-mode for >N seconds
  triggers X." Either the two bits are true or they're not.
- **Does not commit narrative on recovery.** Observation-only fires
  honor §XV. If a driver wants to resume narrative cleanly, the next
  ACCEPT cycle does it; the system does not retroactively guess.
- **Does not add new tables or precomputed state.** All forensic
  queries derive from `pudo_decision_context`, `driver_trip_state`,
  and `offer_history`.
- **Does not deprecate the existing `tad_decision_context.committed`
  list.** TAD's lost-floor commit logic continues to write to this
  list. Under §XVIII, the planner no longer needs to consult it for
  recovery fires (lost-mode handles that path), but the field
  remains forensically useful as TAD's independent attempt to
  identify which offer the driver may be engaged with.

### G. Companions and relationship

- **§0 (Prime Directive):** §XVIII is a direct expression of §0.D.2
  (narrative serves observation). When narrative is broken, the
  product (observations into the pricing and geographic caches)
  continues uninterrupted.
- **§VI (1-bit memory):** lost-mode is derived from the absence of
  the 1-bit (`current_offer_id IS NULL`) combined with queue
  contents. §VI's single source of truth is preserved; §XVIII does
  not add a second memory bit.
- **§XIV.H (live offer predicate):** §XVIII's trigger and Phase 2b
  candidate set both consume `LIVE_OFFER_PREDICATE_SQL`. Single
  freshness model preserved.
- **§XIV.I (§5.3 asymmetric handling):** dispatcher rules apply
  unchanged during lost-mode; only the fire actions transform from
  narrative to observation per §C.4.
- **§XV (observation before narrative):** §XVIII is the matcher-layer
  expression of §XV. When narrative is broken, observation continues
  uninterrupted. The map gets populated; the ledger stays clean.
- **§XVI (arrest-defined truth):** §XVI Phase 1's TAD pre-filter and
  Phase 2b's TAD gate are both bypassed in lost-mode. The Forensic
  Ladder phases themselves (the velocity-based arrest detection,
  Phase 3 POI lookup, Phase 4 hill-climb, Phase 5 notarization) run
  unchanged. §XVIII changes only the TAD-dependent gates, not the
  physics-based arrest detection.

### H. The discipline

- When a future change wants to re-introduce per-offer lost-mode
  (e.g., "this offer's `lost_mode_reason` is X"), refer to this
  rule and refuse. Lost-mode is a property of the driver. Offers
  have anchors that may be poisoned, but that is a property of
  their anchors, not a state.
- When a future change wants to add a stored `driver_trip_state.is_lost`
  column, refer to this rule and refuse. The two-bit derivation is
  the canonical source; storing introduces synchronization
  obligations with no operational benefit.
- When a future change wants to add time-based thresholds to
  lost-mode entry or exit, refer to this rule and refuse. Either
  narrative is engaged or it isn't. Time gates are arbitrary
  numbers per Andrew Rule VII.
- When a future change wants to commit narrative on lost-mode
  recovery, refer to this rule and refuse. The high-confidence
  single-match case still produces observation, not narrative.
  Narrative re-engages on the next clean ACCEPT cycle, not on a
  recovered observation. §XV is explicit on this and §XVIII
  inherits its discipline.
- When a future change wants to precompute lost-mode duration into a
  materialized view or new table for dashboard performance, refer
  to this rule and refuse. The derived query is sub-millisecond on
  indexed columns; precomputation introduces drift; the forensic
  record in `pudo_decision_context` is already sufficient for
  historical queries.

> The driver is lost, not the ride.

---

## Notes

- These rules are derived from production lessons across Phase D and Phase E.
- Changes require explicit ratification (not silent edit).
- When a new architectural decision is locked, it gets added here only after it has shipped and stabilized — not before.
