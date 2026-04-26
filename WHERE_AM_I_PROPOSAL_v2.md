<!--
================================================================================
v2.5 AMENDMENT — Phase D shipped
Date: 2026-04-26
Branch: patch-00566a-unified-refinement
Final commit: 5eed55c (Step 5.7.2 — wai_smoke.py)

Phase D is complete. The continuous awareness primitive `where_am_i.py` is
shipped, tested, and verified against the production database with the
production cursor convention.

The contents below are the v2.4 RFC as proposed and ratified BEFORE Phase D
implementation began. They remain accurate as historical context for the
v1.0 design and should not be re-litigated. For the implementation record —
what actually shipped, the verification trail, lessons learned, and the
backlog for Phase E — see PHASE_D_RETRO.md at the repo root.

Two specific corrections to v2.4 that surfaced during implementation:

  1. The Step 4 Q1 ruling about cursor types ("tuple unpacking for perf")
     was made without auditing production conventions. Production uses
     psycopg2.extras.RealDictCursor everywhere. WAI's _match_ghost_cache
     was corrected to dict access in Step 5.7.1 (commit 01211f8). The
     reasoning below for Q1 is preserved as historical record but is
     superseded by the audit-driven ruling in PHASE_D_RETRO.md L-7.

  2. The TargetSpec `address` field that v2.4 implies exists is not
     currently in the dataclass. WAI's _build_outcome uses
     getattr(target, "address", None) which always returns None until
     Phase F adds the field. Logged as a Phase F TODO.

Phase E (PLAN consumer / pudo_planner.py) is the next major construction.
Its scope is unchanged from v2.4's deferred section, with the addition of
B-11 (implicit STACKED cancellation detection) as a primary concern. See
PHASE_D_RETRO.md for the full backlog.
================================================================================
-->


# RFC: `where_am_i()` — Continuous Location Awareness Primitive

**Author:** Andrew Bruce (PuddleJumper), Claude (Architect) **Date:** 2026-04-24 **Reviewers requested:** Gemini, Grok **Status:** Draft for peer review **Context:** Prior RFC `STOPS_DB_PROPOSAL.md` (Stop Atlas, traffic signal + RR crossing DB) remains valid as a supporting component. This RFC extends it into a broader primitive.

---

## TL;DR

After forensic analysis of last night's auto-nail regressions (20% rate, down from 36%), we identified that our current fire-decision architecture is fundamentally **event-driven** — individual detectors (BMOAR Path A, Path B, Watchdog A/B) race to fire or hold based on local signals. They share no common notion of ground truth and produce contradictory, brittle outcomes in edge cases.

We propose replacing this with `where_am_i()` — a **continuous awareness primitive** that runs on every heartbeat, always produces an answer about the driver's physical situation, never commits on its own, and feeds a clean PLAN → EXECUTE pipeline. This is a structural shift from "detectors that fire" to "a system that knows where it is."

This primitive solves a whole class of problems we've been whack-a-mole fixing individually:

- **Forum Park 7623** (intersection-class pickup at 205m fell through all fire paths)
- **Ghost riders** (missed PUDOs that fire later or never)
- **The Calhoun problem** (Uber provided wrong street for a dropoff)
- **STACKED dropoff swap corner cases**
- **Traffic light false positives** (already partially solved by Stop Atlas — this generalizes it)

---

## 1. Background

### 1.1 Current architecture (event-driven, detector-based)

Today, on every heartbeat, `check_convergence()` runs a cascade of detectors:

1. **BMOAR Path A (blind_man)** — breadcrumb-based pickup inference
2. **BMOAR Path B (proximity)** — geometric proximity + cluster
3. **Watchdog A** — 5-minute stopped fallback
4. **Watchdog B** — micro-stop + departure retroactive nail

Each detector:

- Has its own gates and thresholds
- Fires or holds independently
- Does not share intermediate reasoning
- Commits to writes at decision time via `sm_transition`

**Failure modes this produces:**

- **Forum Park 7623:** BMOAR returned `osm_intersection` source (coord improver, not fire path). Path B proximity was 205m (threshold 200m). No detector could fire. Manual nail.
- **Ghost riders:** A PUDO event happens, but no detector recognizes it at the time. By the time we realize (next heartbeat, next cluster), state machine has moved on. No recovery path.
- **Calhoun problem:** Uber gave us the wrong street for a dropoff. Every detector chased the wrong target coord. System was "correctly" wrong because it trusted the input.
- **Overlapping detectors:** Sometimes Path A and Path B both evaluate the same moment. Race conditions, duplicate nail attempts, inconsistent audit trail.

### 1.2 The architectural smell

Whack-a-mole. Every session we fix one failure mode, and the next session reveals another edge case the new fix didn't consider. That's the signature of a detector-proliferation problem: we don't have ONE system that knows where the driver is; we have a dozen local heuristics, each partially right, none authoritative.

---

## 2. The proposed primitive

### 2.1 Philosophy

**Detectors → Awareness.** Instead of many detectors racing to fire, one primitive continuously reports the driver's physical situation. Consumers (PLAN layer) decide what to do with that information.

This maps cleanly onto PuddleJumper's existing 4-box controller architecture (Monitor → Diagnose → Plan → Execute):

|Box|Before|After|
|---|---|---|
|Monitor|Heartbeat arrives|Heartbeat arrives (unchanged)|
|**Diagnose**|Multiple detectors compute fire signals|**`where_am_i()` produces structured awareness**|
|Plan|Each detector either fires or holds|Consumer evaluates `where_am_i()` output + history, decides|
|Execute|`sm_transition()` writes|`sm_transition()` writes (unchanged)|

**Critical constraint:** `where_am_i()` has NO write side effects (except ghost cache inserts). It is a pure diagnostic. This makes it safe to run continuously, safe to shadow-mode, safe to query from outside the main flow (monitoring, replay, debugging).

### 2.2 Ground truth inversion

Currently, the system treats Uber's text address → Google geocode → target coord as authoritative. Every detector chases that coord. When Uber is wrong (typo, outdated address, passenger error, Google hallucination), the whole pipeline amplifies the error.

`where_am_i()` inverts the authority:

|Signal|Role|
|---|---|
|Driver's actual GPS trail|**Authoritative** (physical reality)|
|Driver's actual speed profile|**Authoritative**|
|Driver's breadcrumb (roads traveled)|**Authoritative**|
|Stop Atlas (traffic signals, crossings)|**Context**|
|Uber's claimed pickup/dropoff address|**Input, fallible**|
|Google's geocode|**Input, fallible**|
|Historical nail events in this area|**Prior evidence** (future extension)|

When reality disagrees with Uber's claim, `where_am_i()` reports reality. PLAN decides what to do with the disagreement (fire at actual location, flag for review, etc.).

---

## 3. The return object

```python
from dataclasses import dataclass
from typing import Literal

@dataclass
class WhereAmIResult:
    # ─── Primary status ─────────────────────────────────────────────
    status: Literal[
        "at_current_pudo",      # We're at a PUDO for the current ride
        "at_previous_pudo",     # We're at a PUDO from a recently cached ghost
        "at_unknown_pudo",      # Strong stop cluster, but no offer explains it
        "not_at_pudo",          # Moving or stopped without pudo signals
    ]
    pudo_type: Literal["pickup", "dropoff"] | None
    offer_id: str | None                # Which offer this PUDO belongs to (None if unknown)

    # ─── Corrected coordinates ──────────────────────────────────────
    # Where we ACTUALLY are (cluster median if stopped, else current GPS)
    # These override Uber's claimed coords for downstream writes
    corrected_lat: float | None
    corrected_lng: float | None

    # ─── Road topology context ──────────────────────────────────────
    on_wire: bool                       # Currently on a named road?
    current_road: str | None            # "Settemont Road" or None if off-wire
    on_target_road: bool                # Is current_road one of the target's named roads?
    off_wire_duration_s: int            # How long off a named road (0 if on-wire)

    # ─── Stop context (what is the environment?) ────────────────────
    stop_context: Literal[
        "at_traffic_signal",    # Within N meters of known signal
        "at_rr_crossing",       # Within N meters of known RR crossing
        "at_stop_sign",         # Future: within range of known stop sign
        "in_parking_lot",       # Off-wire after pivoting off a named road
        "at_curb",              # On-wire, stopped, no known feature explains it
        "in_traffic",           # Moving slow but not stopped
        "unknown_stop",         # Stopped but cannot classify
        "not_stopped",          # Moving normally
    ] | None

    # ─── Confidence ─────────────────────────────────────────────────
    confidence: float                   # Overall 0.0 to 1.0
    confidence_breakdown: dict          # Components: {"proximity": 0.8, "breadcrumb": 0.9, ...}
    reason: str                         # Human-readable for logs

    # ─── Ghost recovery ─────────────────────────────────────────────
    ghost_id: int | None                # suspected_pudos.id if matched a cached ghost
```

### 3.1 Confidence breakdown components

`confidence` is a weighted aggregate of:

|Component|What it measures|
|---|---|
|`proximity`|Distance from cluster median to target, normalized by class-specific threshold|
|`breadcrumb_match`|Did the driver actually drive through the target's named roads?|
|`cluster_tightness`|Cluster spread in meters (tighter = higher confidence)|
|`cluster_duration`|Seconds stopped (longer within reason = higher confidence, until light-cycle threshold)|
|`on_target_road`|Is the driver currently on one of the target's named roads?|
|`off_wire_pivot`|Did the driver recently pivot off a target road (parking lot arrival)?|

Each component is 0.0 to 1.0. Overall confidence is a weighted blend, with weights class-dependent (e.g. breadcrumb weighs more for `single_road` addresses, proximity weighs more for `number_on_street`).

---

## 4. The matching algorithm

`where_am_i()` evaluates in strict hierarchical order, first match wins:

```
1. Is there a current stop cluster?
   - Compute cluster from last N heartbeats (speed < 10mph, spread < radius)
   - If no cluster → return not_at_pudo immediately
   
2. Compute road topology context (on_wire, current_road, on_target_road, off_wire_duration_s)

3. Compute stop_context (query Stop Atlas for signals/crossings within radius)

4. Match against CURRENT RIDE's expected PUDOs:
   a. If state in {ENROUTE, REFINE_PICKUP}: test pickup target
   b. If state in {IN_TRIP, REFINE_DROPOFF}: test dropoff target
   c. If state == STACKED: test both primary and secondary
   Match criteria are CLASS-AWARE (see §5)
   If match passes confidence threshold → return at_current_pudo

5. Match against GHOST CACHE (app_private.suspected_pudos):
   Query suspects for this driver, active (not resolved, not expired, within 50m)
   If match → return at_previous_pudo (retroactive correction opportunity)

6. Cluster exists but no offer explains it:
   Insert row into app_private.suspected_pudos (with offer_id=NULL)
   Return at_unknown_pudo

7. Fallback: cluster exists but failed all matches:
   Return not_at_pudo (shouldn't reach here if cluster detected — safety net)
```

---

## 5. Class-aware matching

Different address classes require different matching logic. BMOAR already classifies addresses into buckets (`intersection`, `single_road`, `number_on_street`, `poi`, etc.). `where_am_i()` extends this classification into fire decisions.

### 5.1 Address class matching rules

|Class|Match criteria|
|---|---|
|**single_road** ("fondren rd")|`on_wire=True` AND `current_road` matches pickup road AND breadcrumb contains pickup road AND cluster is tight|
|**intersection** ("Joan St & Settemont Rd")|`on_wire=True` AND `current_road` matches EITHER named road AND within 250m of intersection point AND cluster is tight|
|**number_on_street** ("1234 Main St")|Within 50m of geocoded point AND `on_wire=True` AND `current_road` matches street name|
|**poi** ("Target Store", "Hobby Airport")|Within POI polygon (if available) OR within 100m of POI center. `on_wire` not required (parking lots, airports)|
|**apartment_complex**|Off-wire AND off_wire_duration_s > 10s AND previously on target road within last 60s (pivot from target road into complex)|

### 5.2 Confidence thresholds

A match is considered "strong enough" for `at_current_pudo` status when `confidence >= 0.7`.

A weaker match (`0.4 <= confidence < 0.7`) returns `at_current_pudo` but PLAN layer may decide to wait for more evidence before firing.

---

## 6. Ghost cache (suspected_pudos)

### 6.1 Schema

```sql
CREATE TABLE app_private.suspected_pudos (
    id                  bigserial PRIMARY KEY,
    driver_id           text NOT NULL,
    detected_at         timestamptz NOT NULL DEFAULT NOW(),
    
    -- Location
    lat                 double precision NOT NULL,
    lng                 double precision NOT NULL,
    geog                geography(Point, 4326) NOT NULL,
    
    -- Cluster characteristics
    cluster_spread_m    real NOT NULL,
    cluster_duration_s  int NOT NULL,
    cluster_median_speed_mph real,
    on_wire             boolean NOT NULL,
    current_road        text,
    stop_context        text,
    
    -- Context at time of detection
    offer_id_at_time    text,           -- May be NULL if off-ride
    state_at_time       text,
    
    -- Confidence snapshot
    confidence          real NOT NULL,
    confidence_breakdown jsonb,
    
    -- Resolution
    resolved_as         text,           -- 'current_pudo', 'previous_pudo_retroactive', 
                                        -- 'false_positive', NULL if unresolved
    resolved_at         timestamptz,
    resolved_by_offer   text,           -- Which offer resolved this ghost
    
    -- Lifecycle
    expires_at          timestamptz NOT NULL DEFAULT (NOW() + INTERVAL '15 minutes'),
    created_at          timestamptz NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_suspected_pudos_active 
    ON app_private.suspected_pudos (driver_id, expires_at) 
    WHERE resolved_at IS NULL;
CREATE INDEX idx_suspected_pudos_geog 
    ON app_private.suspected_pudos USING GIST (geog);
```

### 6.2 Lifecycle

- **Insertion:** When `where_am_i()` detects a cluster that doesn't match the current ride's expected PUDOs, insert row with `offer_id_at_time=current_offer_id`, `resolved_as=NULL`, `expires_at=NOW()+15min`
- **Retroactive match:** When a later heartbeat matches an existing unresolved suspect (same driver, within 50m, still active), it becomes a retroactive-nail candidate. `resolved_as='previous_pudo_retroactive'`, `resolved_by_offer` set to the offer that was missed.
- **Expiration:** After 15 minutes, unresolved suspects are considered false positives. A cleanup cron can mark them `resolved_as='false_positive'` and/or delete old rows.
- **Analysis value:** Resolved/unresolved ratio becomes a product-health metric. High unresolved rate = our matching is missing real PUDOs.

### 6.3 Why Postgres, not in-memory

- Survives Cloud Run process restarts (critical for commercial product)
- Survives deploys
- Cross-pod visible
- Queryable for post-hoc analysis and product metrics
- Low write volume (only on cluster detection, maybe 2-5 per ride)
- GIST index on geography makes spatial lookups microsecond-fast

In-memory caches are a shortcut that will bite at production scale. Rejected.

---

## 7. Integration with Stop Atlas

The `STOPS_DB_PROPOSAL.md` work (traffic signals + RR crossings DB) is NOT abandoned. It becomes the `stop_context` enrichment source:

```python
def _stop_context(lat, lng, speed_mph, stopped_seconds) -> str:
    """Classify the stop environment using Stop Atlas and topology."""
    if speed_mph > 10:
        return "not_stopped"
    
    # Query Stop Atlas
    stop_match = cur.execute("""
        SELECT stop_type FROM routing.known_stops
        WHERE ST_DWithin(geog, ST_MakePoint(%s, %s)::geography, 40.0)
        LIMIT 1
    """, (lng, lat)).fetchone()
    
    if stop_match:
        if stopped_seconds > 60:  # Duration override: longer than any light cycle
            return "at_curb"  # Pickup at a light, probably
        return f"at_{stop_match['stop_type']}"
    
    # No known stop feature nearby
    if speed_mph < 2 and stopped_seconds > 5:
        return "at_curb"  # On-wire, stopped, nothing explains it
    
    return "in_traffic"
```

Stop Atlas shifts from "suppress BMOAR fires here" (its original role in the prior RFC) to "provide context for `where_am_i()` decisions." PLAN layer uses `stop_context` to modulate firing:

- `at_current_pudo` + `stop_context=at_traffic_signal`: hold, wait for driver to exit the light
- `at_current_pudo` + `stop_context=at_curb`: fire
- `at_current_pudo` + `stop_context=in_parking_lot`: fire (apartment/POI arrival)

---

## 8. PLAN layer consumer

`where_am_i()` produces diagnostic output. The PLAN layer consumes that output and decides actions.

### 8.1 Temporal pattern detection

Single-heartbeat `where_am_i()` output is useful but not sufficient. PLAN maintains a per-driver state machine OVER the diagnostic stream:

```
not_at_pudo → at_current_pudo (single heartbeat):
    Arm candidate, wait for confirmation

at_current_pudo stable for N heartbeats (N=3-6 configurable):
    Fire (execute sm_transition)

at_current_pudo → not_at_pudo (brief, < 15s):
    Was a traffic light or brief slowdown, cancel candidate

at_current_pudo → not_at_pudo (after long stop ≥ 15s then departure):
    PUDO happened, fire retroactive if not already fired
    
not_at_pudo → at_previous_pudo (ghost match):
    Retroactive correction opportunity, fire previous offer's PUDO
    
at_unknown_pudo:
    Log for analysis, optionally create "ghost offer" for driver review
```

### 8.2 Why this two-layer design matters

- **`where_am_i()` stays pure.** No decision-making. Just reports reality.
- **PLAN owns firing logic.** All the "fire now vs wait vs retroactive" complexity lives in one place.
- **Testable independently.** Unit tests for `where_am_i()` use fixed GPS inputs, check the diagnostic output. PLAN tests use mocked `where_am_i()` streams.

---

## 9. Shadow mode deployment

### 9.1 First-drive strategy

`where_am_i()` runs on every heartbeat from day one. Its output is **logged only**. No firing behavior changes. Existing detectors (BMOAR Path A/B, Watchdogs) continue to operate.

Log format:

```
[WAI] driver=Uj... status=at_current_pudo pudo_type=pickup offer_id=7623
      corrected=(29.6246, -95.5102) on_wire=T road='Settemont Road' on_target=T
      off_wire_s=0 stop_ctx=at_curb confidence=0.82 reason='intersection match, breadcrumb 
      contains Settemont, cluster tight 15m, stopped 30s'
```

### 9.2 Validation criteria

After one drive session:

1. **For every successful manual or auto nail**, check: did `where_am_i()` report `at_current_pudo` at least N heartbeats before the nail? (Expected: yes)
2. **For every traffic light stop**, check: did `where_am_i()` correctly report `not_at_pudo` or `stop_context=at_traffic_signal`? (Expected: yes)
3. **For every ghost rider incident**, check: did `where_am_i()` cache a suspected_pudo and later report `at_previous_pudo` when the pattern repeated? (Expected: yes, if it happens)

Only after shadow mode validates on 2-3 drives do we wire PLAN to fire from `where_am_i()`.

---

## 10. Migration path

### 10.1 Build sequence (3 sessions)

**Session 1 — Foundation + Shadow Mode** (~6-8 hours production build)

- Schema migrations: `app_private.suspected_pudos`, `routing.known_stops` (Stop Atlas)
- Stop Atlas ingestion scripts (5 sources, per prior RFC)
- `where_am_i.py` module with all helpers
- Shadow-mode integration in `driver_heartbeat.py` — logs only, no firing changes
- Unit tests for the primitive (class-aware matching, ghost cache, topology)
- Deploy and validate on test drive

**Session 2 — PLAN Layer + Wire to Fire** (~4 hours)

- PLAN consumer module with temporal pattern state machine
- Wire to `sm_transition` via existing write paths
- Retroactive correction path for ghost matches
- Feature flag: `PLAN_LAYER_ENABLED` (default false, enables per-driver)
- Test drive with flag enabled for Andrew's driver_id only

**Session 3 — Deprecate Legacy Detectors** (~3 hours)

- Remove BMOAR Path A blind_man from `check_convergence`
- Remove Path B proximity
- Keep Watchdog A/B as safety nets only
- Update all integration tests
- Deploy to production for all drivers

Total: ~13-15 hours over 3 sessions. Fits within the 1-month commercial launch window.

### 10.2 Rollback strategy

- Feature flag for PLAN layer means we can disable it per-driver without redeploy
- Legacy detectors are removed in Session 3 AFTER Session 2 proves the new path works
- Shadow mode in Session 1 changes nothing about current behavior

---

## 11. Problems this solves

### 11.1 Forum Park 7623

- Driver stopped on Settemont Road, 205m from Joan St intersection
- `where_am_i()` returns `at_current_pudo` with confidence ≥ 0.7:
    - `on_wire=True, current_road='Settemont Road', on_target_road=True` (intersection class, Settemont is one of the two named roads)
    - Proximity to intersection 205m (within class-specific 250m threshold)
    - `stop_context='at_curb'` (no signal within radius)
    - Cluster tight, duration 30s, confidence breakdown all strong
- PLAN fires pickup at corrected coords (cluster median)

### 11.2 Ghost riders

- Driver arrives at actual passenger location, cluster forms, no detector fires (system didn't recognize it)
- `where_am_i()` detects cluster, doesn't match current ride's expected pickup, logs as suspected_pudo
- Driver drives off, passenger is in car, state still ENROUTE
- 3 minutes later, state machine times out or Uber advances ride
- New cluster heartbeat triggers `where_am_i()` which sees ghost match
- PLAN fires retroactive pickup at ghost coords, corrects offer_history

### 11.3 The Calhoun problem

- Uber gave wrong street name in dropoff address
- Driver navigates to actual destination (correct physical location)
- Stop cluster forms at actual destination — a road that isn't in the Uber address string
- Current detectors: fail all target matches, fall through to Watchdog or orphan
- `where_am_i()` reports `at_unknown_pudo` (cluster but no target match)
- PLAN can: (a) fire dropoff at cluster location anyway with `uber_address_mismatch=true` flag, (b) use breadcrumb + on-wire to infer the real destination street, (c) contribute to market intelligence (X% of rides to "Calhoun" actually drop at "Prairie" — Uber geocode is wrong for this address)

### 11.4 Traffic light false positives

- Driver stops at a red light 300m before pickup destination
- `where_am_i()`: cluster exists, but `stop_context='at_traffic_signal'`, `on_target_road=True`, proximity too far
- Matches: `at_current_pudo` probably FALSE (cluster too far + at signal), returns `not_at_pudo`
- No fire. Driver continues past light, reaches actual destination, new cluster → at_current_pudo fires.

### 11.5 STACKED dropoff swap

- In STACKED state, both primary and secondary dropoffs are active targets
- `where_am_i()` tests both in matching step, returns whichever matches better
- PLAN handles the atomic swap per existing state machine contract

---

## 12. Failure modes and limitations

### 12.1 What this doesn't solve

**Extremely poor GPS quality.** If GPS drifts 100m randomly, cluster detection itself is broken. `where_am_i()` depends on usable GPS. No amount of cleverness at the diagnostic layer fixes noisy sensors.

**Adversarial behavior.** Driver deliberately parks somewhere false to trick the system. Not in scope.

**Rural areas with no OSM road coverage.** `on_wire` detection depends on `routing.houston_ways`. Rural county edges will have off-wire periods that aren't parking lots but just unmapped gravel roads. Accept as a coverage limitation.

### 12.2 Computational cost

Each heartbeat now does:

- Cluster detection (already done — no new cost)
- Road topology lookup (2-3 queries on houston_ways, GIST-indexed, ~5-10ms total)
- Stop Atlas lookup (1 query on known_stops, GIST-indexed, <1ms)
- Target matching (in-memory, microseconds)
- Ghost cache check (1 query on suspected_pudos, GIST-indexed, <1ms)

Total estimated overhead per heartbeat: ~15-25ms. Heartbeats fire every 5s. Current heartbeat latency budget is ~500ms. Acceptable.

### 12.3 Cache growth

`suspected_pudos` grows with ghost events. At 50 drivers × 5 clusters/ride × 8 rides/day × 365 days = ~730,000 rows/year. Not trivial, but:

- Weekly cleanup cron deletes resolved + expired rows older than 30 days
- Partition by month if needed at scale
- GIST index stays lean if table is regularly pruned

Not a concern for v1. Add partitioning when row count exceeds 1M.

---

## 13. Questions for Gemini and Grok

### For Gemini:

1. **Architecture critique.** Is the DIAGNOSE/PLAN separation clean, or am I over-engineering the two-layer design? Could this be one function with internal phases and still be testable/debuggable?
    
2. **Cluster detection primitive.** We already have cluster detection in `bead_on_wire.py`. Should `where_am_i()` reuse that function directly, or should cluster detection be extracted into a shared primitive that both old and new paths use during migration?
    
3. **Retroactive correction correctness.** When we fire a previous pickup retroactively (via ghost match), we need to rewrite offer_history AND update any dependent records (decisions_trace, community_offers, etc.). Is there a clean pattern for retroactive writes in an event-sourced state machine, or do we need to design a compensating-transaction pattern?
    
4. **Performance ceiling.** At the estimated 15-25ms per heartbeat, we have headroom. But if PuddleJumper scales to 10,000 concurrent drivers on Cloud Run, are there bottlenecks (DB connection pool, GIST contention, hot rows) we should design around now?
    
5. **Houston_ways topology queries.** We're now doing 2-3 on-wire lookups per heartbeat across 1M+ edges. Current index strategy is sufficient for 1 driver, but at scale? Should we pre-compute a "driver is within N meters of road X" cache?
    

### For Grok:

1. **Counter-arguments.** Is there a structural reason this is wrong that I'm not seeing? We've been burned before by "elegant architectures" that don't survive contact with real driver behavior. What's the most likely way this fails in production that I'm missing?
    
2. **Class-aware matching complexity.** Each address class gets its own matching rules. In practice, how many classes actually matter? Are we building a taxonomy for edge cases that are rare enough to ignore, while missing something fundamental in the common cases?
    
3. **Temporal pattern detection in PLAN.** The "stable for N heartbeats" logic adds latency (N×5s) to fires. Is there a smarter way to detect "this is really a stop" without waiting 15-30 seconds? Or is the latency acceptable for the correctness gain?
    
4. **Ghost cache semantics.** When a retroactive nail fires 3 minutes late, the state machine has been in a wrong state for 3 minutes. Any downstream writes during that window are suspect. How should we handle: (a) heartbeats during the window that assumed wrong state, (b) any Discord notifications sent, (c) state_log entries showing the "wrong" transitions?
    
5. **Simplest version that ships.** If you had to strip this proposal to its minimum viable form while keeping the architectural win, what would you keep and what would you defer?
    

### For both:

1. **Have you seen this pattern in other geospatial / mobility systems?** Continuous awareness primitives are common in robotics (SLAM, localization). Is there prior art in rideshare or logistics we should study?
    
2. **What's the riskiest assumption?** In the whole proposal, what's the single thing that, if wrong, would make this proposal not work?
    
3. **Simplest next step.** Session 1 is 6-8 hours. What's the FIRST thing to build within that session that proves the architecture works?
    

---

## 14. Current state of the codebase

For reviewers examining the code:

- `nail_it_core.py` — `check_convergence()` with current Path A/B/Watchdog logic
- `bead_on_wire.py` — `compute_target()`, cluster detection, classify_address()
- `driver_heartbeat.py` — main endpoint, calls check_convergence per heartbeat
- `state_machine.py` — wrapper over `app_private.sm_transition()`
- `routing.houston_ways` — OSM Houston road network, 1M+ edges, GIST-indexed
- `routing.known_stops` — to be created (Stop Atlas per prior RFC)
- `app_private.suspected_pudos` — to be created (this RFC)

Current production revision: `puddlejumper-api-00572-74l` (deployed 2026-04-23 evening). All tests passing with known-failure exceptions.

---

---
## v2.1 Implementation Notes (post-Phase-A, post-Phase-C)

The body above reflects the v2 design as approved on 2026-04-24.
The following decisions were made during implementation and supersede
the corresponding sections of v2:

### §6.1 schema (v2.1)
- PK is `(id, driver_id)` (hash partitioning requires partition key in PK)
- 16-way hash partition on `driver_id`
- Two-timer lifecycle: `match_expires_at` (15 min) + `retention_expires_at` (48 h)
- Autovacuum tuned per partition: threshold=25, scale_factor=0.05
- `pickup_h3` column added (canonical H3 anchor)
- Janitor: VM crontab hourly (no pg_cron)
- Canonical: `~/puddlejumper-prod/migrations/2026_04_25_phase_a_where_am_i_foundation.sql`

### §10 build sequence (v2.1)
Replaced 3-session plan with 7-phase plan: A → C → D → E → F → G → H.
- Phase A (foundation schemas): SHIPPED 2026-04-25 morning
- Phase C (cluster_detection relocation): SHIPPED 2026-04-25 afternoon (revision 00573-g5m)
- Phase D (where_am_i.py, shadow mode): in flight
- Phase E (pudo_planner.py PLAN consumer): queued
- Phase F (driver_heartbeat.py integration): queued
- Phase G (tests): queued
- Phase H (deploy + 7623 forensic replay): queued

### §3 WhereAmIResult dataclass (v2.1)
- `confidence_breakdown: dict` REMOVED from dataclass; rendered into `reason` text instead.
  Full breakdown stored in `suspected_pudos.confidence_breakdown jsonb` for analytics.
- Decision pending Phase D: whether to add `cluster: Cluster` field to expose the
  underlying cluster snapshot to PLAN consumers.

### §7 Stop Atlas (v2.1)
DEFERRED to v1.1. `_stop_context()` returns `'unknown_stop'` always for v1.
Code sample in v2 §7 uses `ST_MakePoint` which violates canonical coordinate rules
(see PuddleJumper canonical rules: use `coords_to_geography(lat,lng)`).

### §14 codebase state (v2.1)
Current production revision: puddlejumper-api-00573-g5m (deployed 2026-04-25).
`detect_cluster()` now lives in `cluster_detection.py` (top-level), not bead_on_wire.py.
Both BMOAR and WAI consume from cluster_detection.

### Forensic motivating case (v2.1)
The Forum Park 7623 analysis in §11.1 is the canonical motivating case.
Reframed per Apr 25 forensics: offer_history.id=6999, current_offer_id="7623",
pickup_error_m=680.5. The error reflects driver waiting per protocol then
manually nailing while moving away — NOT a system misfire. The 30-second
stop at (29.6245833, -95.5102295) is the actual pickup that should have
auto-detected. Phase D is built specifically to detect this case.
Fixture preserved at tests/fixtures/7623_heartbeats.json.
---
## v2.2 Backlog — Dynamic Street-Length Gating + Multi-Gate Intersection Convergence

Captured 2026-04-25 during Phase D planning. Not in Phase D scope.
Tentatively v1.0 launch refinements (post-Phase-D, pre-public-launch),
or v1.1 polish if scope pressure forces deferral.

### B-1. Dynamic street-length radius (replaces hardcoded 200m gate)

PROBLEM: The Apr-25-locked PUDO fire rule G2b uses a hardcoded 200m radius
for residential intersection matching. 200m is a guess. A 25m residential
side-street produces 175m of false-positive territory; a 2km arterial
produces missed stops near the far end.

PROPOSAL: After snapping to a named road, query the road's actual length
from routing.houston_ways (SUM(ST_Length(geog::geography)) GROUP BY name,
or pre-aggregated). Use that length as the dynamic radius.

CACHING: Use the existing street cache (location TBD — confirm during
implementation). Cache key: street name (post-canonicalization). Cache
value: length_m. Miss path: query houston_ways. Second-miss path
(houston_ways doesn't have it — newly paved street, private drive, edge
of coverage): query Google Directions API for the segment, cache result.
Each Google miss reduces future Google calls for the same street to zero.

INVARIANTS:
  - Cache is read-mostly. Writes only on miss.
  - Cache eviction policy TBD (probably none — Houston named roads are a
    bounded set, ~50K entries).
  - Cache is in-memory per pod (Cloud Run) UNLESS the existing street cache
    is already PG-backed, in which case adopt that.

OPEN QUESTIONS:
  - Where IS the existing street cache? (Andrew referenced it during the
    drive — locate before implementing.)
  - Pre-aggregate street lengths into a materialized view, or compute on
    the fly with caching? Materialized view is more honest data; on-the-fly
    is simpler.
  - Streets with multiple disconnected segments of the same name (common
    in grid-pattern Houston) — sum the longest contiguous run, or all
    segments? Affects radius semantics.

### B-2. Multi-gate convergence for intersection class

PROBLEM: Current proposal scores intersection match with a single weighted
confidence. Easier to reason about as a set of independent boolean gates,
all of which must pass.

PROPOSAL: For address_class == "intersection", _match_current_pudo applies
four gates. ALL must pass to return at_current_pudo.

  Gate 1: Geocode confirms — Google returned a valid intersection point
  Gate 2: On named road — current_road snaps to road_a OR road_b
  Gate 3: GPS convergence — cluster detected (cluster_detection.py)
  Gate 4: Within bounds — cluster median within dynamic length of BOTH roads
                          (depends on B-1 dynamic radius)

If any gate fails, the result is not_at_pudo (fall through to ghost match
per RFC §4 step 5). Per-gate boolean shows up in `reason` for forensics.

RATIONALE: Independent signals fail differently — GPS noise alone, name
mismatch alone, geocode error alone. Requiring all four collapses the
false-positive rate sharply. Single-confidence-score lets a strong signal
on one axis mask a weak signal on another; gates don't.

### B-3. Stop classification status (as of 2026-04-25 drive)

  intersection      — solved (four-gate convergence per B-2, dependent on B-1)
  apartment_complex — solved (existing pivot logic in get_pivot_context)
  single_road       — solved (Google entrance + breadcrumb + convergence;
                       see compute_target single_road branch in bead_on_wire.py)
  number_on_street  — tentative: cluster within ~10-15m of geocoded coords.
                       Tighter than intersection because Google's house-
                       number geocode is precise.
  poi               — open. Airport "United Airlines" / "Southwest Airlines"
                       cases require polygon/footprint matching, not point
                       proximity. RFC v2 §5.1 deferred to v1.1; airports
                       being real production cases may force into v1.0.

### Promotion path

These items live in v2.2 backlog until Phase D ships and we have shadow-mode
data showing the magnitude of the false-positive / missed-stop problem the
hardcoded gates produce. At that point, prioritize B-1 first (it unblocks
B-2 gate 4), then B-2, then resolve number_on_street and poi as separate
RFCs if their complexity warrants.


---
## v2.3 Phase D.0 Retrospective — Lessons for Phases D, E, F, G

Phase D.0 (relocate get_pivot_context, _build_breadcrumb, _road_names_match,
canonicalize_address, SUFFIX_CANONICAL out of bead_on_wire.py) closed
2026-04-26 at commit 15b7a2f. Pre-D.0 baseline ref was 6a3ef5e.

The work itself was straightforward. Most of the wall time went into
hygiene and verification rigor that paid for itself. Notes below for
future-self entering Phase E or G.

### L-1. "Documented baselines" need re-verification before being trusted

The "21/21 pytest pass" baseline carried over from Phase C was real
but fragile -- it only held if pytest was invoked with PYTHONPATH=. Without
that env var, pytest collection failed with ModuleNotFoundError on
'cluster_detection' and 'utils'. Discovered Sunday morning when re-running
baseline as Step 4.0.2.

Fix: added `pyproject.toml` with `[tool.pytest.ini_options]
pythonpath = ["."]`. Both `pytest tests/` and `PYTHONPATH=. pytest tests/`
now produce identical 21/21.

Lesson for E/F/G: when a phase opens by re-running a documented baseline,
treat any deviation (collection errors, different counts, env-var rituals)
as a STOP condition. Resolve the fragility before any code edits. Half an
hour up front saves you from chasing phantom regressions later.

### L-2. `git add -u` is a footgun for scope-claimed commits

A "remove phantom files" commit (4b93ccb, since amended) silently bundled
6 unrelated root-level test-file deletions because `git add -u` stages
ALL pending modifications, not just the ones the commit message describes.
Caught immediately by reading the commit's `delete mode` lines.

Fix: amended commit message to be honest. New rule: never use `git add -u`
for a commit with a precise scope claim. Always `git add <explicit list>`.

Lesson for E/F/G: each phase commit should stage files by name. The
discipline saves you from history that lies about what changed.

### L-3. Anchor-based edit scripts beat sed for surgical relocations

Phase D.0's two edit scripts (phase_d0_strip_bead.py and
phase_d0_update_imports.py, since deleted -- their record is in commit
15b7a2f) used multi-line string anchors plus sha-locked pre-conditions
plus py_compile post-conditions. Pattern:

  1. Refuse to run unless source sha matches expected.
  2. Verify each anchor exists exactly once (no ambiguity).
  3. Apply via str.replace().
  4. Verify obsolete symbols removed AND required symbols present.
  5. Compile-check the in-memory edited content.
  6. Predict post-edit sha during dry run; verify against actual on apply.
  7. --dry-run is default; --apply is explicit opt-in.

Both scripts produced predicted shas that matched actual shas exactly.
Zero surprises during apply.

Lesson for E/F/G: when relocating or refactoring across files, write a
script with this shape. Don't trust hand-edits or sed line-ranges for
multi-region changes. The script is faster than reviewing a diff.

### L-4. Read existing primitives before designing new ones

Twice during Phase D planning the proposal nearly duplicated working
production code:

  - Phase C extracted detect_cluster() from BMOAR; the original RFC §13
    review flagged this risk explicitly ("Should where_am_i() reuse that
    function directly, or should cluster detection be extracted into a
    shared primitive...").
  - Phase D's _compute_road_topology helper was sketched as a from-scratch
    backward-trail walker before discovering get_pivot_context() at
    bead_on_wire.py:598 already does exactly that, with the breadcrumb
    fan-out (max_segments=4) Andrew already remembered.

Lesson for E/F/G: before proposing a new helper, grep the codebase for
the function name AND its likely synonyms ("breadcrumb", "pivot",
"trail", "context"). The five-minute grep prevents three-hour
duplicate implementations.

### L-5. Trailing-newline gotcha is real

Three artifacts (pudo_types.py, address_utils.py, pivot_context.py,
phase_d0_strip_bead.py, phase_d0_update_imports.py) all hit the
"editor strips trailing newline on save" issue. Sha mismatch on first
verification, fixed by `[ -n "$(tail -c 1 file)" ] && echo >> file`.

Lesson for E/F/G: bake the trailing-newline auto-fix into every
"transfer file from chat to VM" verification block. It's idempotent
and zero-cost.

### L-6. Gemini's "while we're here..." follow-ups consistently expand scope

Two examples in this phase:

  - "Should we include a cache warm-up script for common R10 hexes?"
    (Step 4 -- declined, deferred to observability data)
  - "While we're creating address_utils.py, should we sweep
    bead_on_wire.py for other string-cleaning logic to move?"
    (Q26 follow-up -- declined, sweep deferred to v2.2 backlog item B-4)

Both follow-ups were architecturally reasonable but would have expanded
a tightly-scoped relocation into a multi-day refactor.

Lesson for E/F/G: Gemini's primary architectural calls have been
consistently strong (Q12 box-discipline ruling, Q18 relocate-now,
Q22-Q24 dynamic radius framing). Its follow-up questions have a
consistent "scope creep" failure mode. Hold the line; defer extras to
backlog. The signal-to-noise on Gemini's verdicts is high; on its
follow-ups, mixed.

### L-7. Phase G's "delete bead_on_wire.py" surgery is now mechanical

Going into Phase D.0, bead_on_wire.py was 962 lines with 4 distinct
external consumers (nail_it_core, apply_step_2c, plus its own internal
re-exports). Going out, it's 693 lines and the only external consumer
of bead_on_wire-specific symbols is nail_it_core's compute_target import
at line 1145. When Phase G arrives, deleting bead_on_wire.py will
require:
  1. Relocate compute_target to its own module (bead_on_wire is the
     last home that still owns it).
  2. Update nail_it_core's L1145 import.
  3. Delete bead_on_wire.py.
That's it. Phase D.0 made Phase G a one-day job instead of a multi-day
slog.

### Phase D.0 final stats

  Commits in this hygiene + relocation pass:
    f8c4aad - Phase C: relocate detect_cluster (was uncommitted Sunday
              morning; committed first as a clean baseline)
    7d462fa - chore: phantoms + test relocations (amended)
    6a3ef5e - chore: additional phantoms (amended)
    15b7a2f - Phase D.0: relocate pivot_context + address_utils

  Files changed: 7 (4 new, 3 modified)
  Net line delta in bead_on_wire.py: -269 (962 -> 693)
  pytest:        21/21 preserved
  integration:   22/61 preserved
  Phantoms exorcised from working tree: 8

  Phase D proper (where_am_i.py) is unblocked.


---
## v2.4 Phase D Entry Brief — Handoff for Next Session

This section seeds Phase D proper (`where_am_i.py` build). Designed to
let a fresh chat start without re-litigating the 28 architectural
decisions already locked. Read top to bottom; everything below is
authoritative.

### v2.4.1 Where we are

Phase D.0 closed at commit 15b7a2f (2026-04-26). Phase A (foundation
schemas) closed earlier. Phase C (cluster_detection relocation) closed
at f8c4aad. Phase D proper -- the where_am_i.py build itself -- is
the next concrete work.

Current production revision: puddlejumper-api-00573-g5m (deployed
2026-04-25, serving 100% of traffic).

### v2.4.2 Phase D scope

  IN scope:
    - Build where_am_i.py at the project root.
    - Public surface: class WhereAmI with evaluate(...) -> WhereAmIResult.
    - Pure DIAGNOSE primitive: no writes, no sm_transition calls, no
      Discord notifications, no offer_history mutations.
    - Class-aware matching: intersection, single_road, number_on_street,
      apartment_complex. POI is open (see v2.4.7).
    - Shadow mode by default: WAI runs every heartbeat, logs the result,
      makes no fire decisions.
    - Phase D ships unit tests + the 7623 integration replay test.

  OUT of scope (separate phases):
    - PLAN consumer pudo_planner.py (Phase E).
    - sm_transition wiring (Phase E -> EXECUTE).
    - Temporal pattern detection ("stable for N heartbeats" -- Phase E).
    - suspected_pudos INSERT writes (Phase E owns these per Q12 ruling).
    - driver_heartbeat.py integration (Phase F).
    - Deprecation of BMOAR Path A/B (Phase G).

### v2.4.3 Foundation already in place (do not rebuild)

Files that exist at project root and are importable:

  cluster_detection.py
    Cluster                 -- frozen dataclass: n, median_lat, median_lng,
                                spread_m, duration_s
    detect_cluster(driver_id, cur, ...) -> Optional[Cluster]
    is_stable(cluster, threshold_s) -> bool

  pivot_context.py
    get_pivot_context(driver_id, cur, anchor_time=None) -> dict with keys
      on_wire, current_road, last_named_road, pivot_time, breadcrumb
    _build_breadcrumb(rows, ...)        -- walks heartbeat trail
    _road_names_match(snapped, address) -- fuzzy compare; uses canonical
    Constants used internally: 60-heartbeat lookback, 40m snap distance,
      4-segment breadcrumb cap, 10s min dwell, 300s max age

  address_utils.py
    canonicalize_address(addr) -- lowercase + abbreviation-normalize
    SUFFIX_CANONICAL           -- 17 (regex, replacement) pairs

  pudo_types.py
    WhereAmIResult            -- frozen dataclass, see RFC §3 + v2.1
    TargetSpec                -- a single PUDO target's metadata
    Offer                     -- pickup + dropoff + secondary_dropoff

  Schemas (Phase A, in production):
    app_private.suspected_pudos   -- 16-way hash partitioned, two-timer
                                     lifecycle, hourly VM cron janitor
    routing.known_stops_config    -- thresholds table
    app_private.feature_flags     -- runtime gates incl.
                                     WAI_PLANNER_ENABLED_DRIVERS

### v2.4.4 Locked decisions Q1-Q28 (one-line summaries; do not re-open)

  Q1  cluster: Cluster | None field IS on WhereAmIResult.
  Q2  Snap cache DROPPED entirely (Q19 supersedes -- pivot_context
      already does the topology lookup; no per-call cache needed).
  Q3  Same -- snap cache dropped.
  Q4  MIN_REPORT_THRESHOLD = 0.4. Below threshold falls through to
      ghost match / unknown_pudo path.
  Q5  WAI uses 250m for intersection class (permissive DIAGNOSE).
      PLAN's G2b uses 200m (strict EXECUTE precondition). Layered.
  Q6  Shared types live in pudo_types.py at top level (already exists).
  Q7  Single backward heartbeat_log scan for topology fields. Done by
      pivot_context.get_pivot_context already.
  Q8  Ghost match radius 50m (when Phase E builds ghost-cache reads).
  Q9  cluster_median_speed_mph: extra query at insert time (Phase E
      concern, not D).
  Q10 WAI_SHADOW_ENABLED defensive flag deferred to Phase E (D has no
      writes, so no flag needed in D).
  Q11 7623 replay is integration test (real PG seed required).
  Q12 EXECUTE-only-write rule wins. WAI is pure-read DIAGNOSE.
      suspected_pudos INSERTS move to PLAN/EXECUTE in Phase E.
  Q13 where_am_i.py at project root, alongside cluster_detection.py
      and pivot_context.py. Not in decisions/.
  Q14 Time form: bare NOW() against timestamptz (matches
      cluster_detection style). Note: canonical rules doc prefers
      (NOW() AT TIME ZONE 'UTC'); WAI subsystem holds consistency
      with cluster_detection rather than diverging.
  Q15 -- (Q15 was reframed -- B-1 dynamic street-length is v2.2 backlog,
       not Phase D. Phase D uses the existing 250m hardcode.)
  Q16 No LRU on snap cache (cache dropped per Q19 anyway).
  Q17 -- (resolved in flow)
  Q18 Relocate get_pivot_context NOW (Phase D.0). Done.
  Q19 Drop snap cache. Pivot context already does it.
  Q20 Adopt production constants verbatim (40m, 60-heartbeat,
      4-segment, 10s/300s).
  Q21 _compute_road_topology is private helper inside where_am_i.py.
  Q22-Q24 Dynamic street-length gating + multi-gate intersection
      convergence + cache-and-learn -> v2.2 backlog (see §B-1, B-2).
      NOT Phase D scope.
  Q25 _canonicalize_address -> address_utils.py (separate from
      pivot_context).
  Q26 Patch scripts updated, no archaeology.
  Q27 Drop underscore prefixes: canonicalize_address (was
      _canonicalize_address), SUFFIX_CANONICAL (was _SUFFIX_CANONICAL).
      Public symbols.
  Q28 Phase D.0 also redirected detect_cluster imports in
      nail_it_core/apply_step_2c to cluster_detection canonical home.

### v2.4.5 Build sequence for Phase D proper

  Step 1: Propose where_am_i.py module structure to Gemini.
          Lean: single class, evaluate() method, internal _RoadTopology
          dataclass private to where_am_i.py, reuse pivot_context for
          all topology lookups.
  Step 2: Once Gemini consensus, write _compute_road_topology adapter
          (uses get_pivot_context, computes on_target_road via
          _road_names_match against breadcrumb).
  Step 3: Write _match_current_pudo for each address class
          (intersection, single_road, number_on_street, apartment_complex).
          Test each class independently.
  Step 4: Write WhereAmI.evaluate() top-level orchestrator implementing
          RFC §4 matching algorithm.
  Step 5: Unit tests in tests/test_where_am_i.py per RFC v2 §11
          (no_cluster, intersection_205m_passes, intersection_300m_fails,
          apartment_pivot, etc).
  Step 6: 7623 fixture replay integration test (per Q11).
  Step 7: Functional smoke against live PG (parallel to Phase D.0
          Step 4.0.8 pattern).
  Step 8: Commit Phase D.

  No deploy in Phase D. Deploy is Phase F (driver_heartbeat
  integration).

### v2.4.6 Verification protocol (paired-programming, mirrors D.0)

  Each step:
    1. Claude proposes.
    2. Gemini reviews. (Sometimes Grok.)
    3. Consensus required before implementation.
    4. Andrew applies; Claude provides instructions with explicit
       verification commands.
    5. Tests must show 21+/21+ pytest and 22/61 integration at every
       gate. Net new tests added in Phase D bring those numbers up.

  File patches: Python anchor-based scripts with sha-locked
  pre-conditions and py_compile post-conditions. Pattern is in
  WHERE_AM_I_PROPOSAL_v2.md v2.3 §L-3 if needed.

  Trailing-newline auto-fix on every artifact transfer:
    [ -n "$(tail -c 1 file)" ] && echo >> file

  Never use git add -u for a scope-claimed commit.

### v2.4.7 Open question carried into Phase D

  POI class (RFC §5.1, "United Airlines" / airport curb / business
  POI cases). RFC v2 deferred this to v1.1. Andrew flagged Saturday
  during the driving brainstorm that airport rides are real production
  cases that may force POI into v1.0 scope.

  Phase D entry default: stub _match_current_pudo for POI to return
  (matched=False, confidence=0.0, reason="poi class not yet implemented")
  and let the cluster fall through to ghost / unknown_pudo. Decision
  to escalate POI into v1.0 vs hold for v1.1 is to be revisited after
  Phase D ships and shadow-mode data shows POI miss rate.

### v2.4.8 Files to seed the new chat with

  1. The brief above (this v2.4 section, plus optionally v2.1, v2.2,
     v2.3 amendments for context).
  2. The full WHERE_AM_I_PROPOSAL_v2.md document.
  3. The PuddleJumper canonical rules document (the one with COORDINATE
     RULES, TEMPORAL RULES, 4-BOX CONTROLLER, EXECUTE-ONLY-WRITE rule).
  4. Andrew's user preferences directive (technical foundation, one-step-
     at-a-time, verification per step).

  Optional but useful:
  5. Recent git log: `git log --oneline -10` to ground the new chat
     in current ref.

