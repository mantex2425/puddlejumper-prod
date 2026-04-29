# DTF — Designed-To-Fail Ghost Resolution Framework

**Status:** Design v0.1 — 2026-04-23
**Owner:** Andrew Bruce
**Architecture:** 4-Box Controller (MONITOR → DIAGNOSE → PLAN → EXECUTE)
**Time domain:** UTC canonical. All timestamps bare NOW() on timestamptz columns.
**Deployment philosophy:** Shadow mode first. Live fire only after real-drive validation.

---

## 1. What DTF is

DTF is a pluggable framework for **retroactive event resolution** in the PuddleJumper trip state machine. It handles the class of problems where:

- A real-world event happened (passenger got out, passenger got in, driver completed a stealth ride)
- The primary detection path (BMOAR unified predicate, Watchdog A/B) didn't fire
- Later evidence (a new pickup nail, a stop at an unusual location) confirms retroactively that the earlier event occurred

DTF does NOT replace the unified predicate. It backstops it for cases the unified predicate cannot solve with live data alone.

## 2. What DTF is NOT

- Not a parallel state machine. Every DTF resolution goes through sm_transition().
- Not a write from DIAGNOSE. DTF detectors in DIAGNOSE produce candidate records, not writes.
- Not a bypass of valid_state_transitions. DTF introduces new triggers that must be added to the allowed-transitions table before they can fire.
- Not a speculative system. Every DTF pattern must be justified by observed real-world data.

## 3. The framework shape

A DTF pattern is a 4-tuple declaration:
DTFPattern:
name:                 str                    # DTF_DROPOFF_ON_NEXT_PICKUP etc.
detect:               Callable[...] -> bool  # DIAGNOSE — when to record a ghost
ghost_shape:          dict schema            # what state to capture in ghost row
resolve_trigger:      str                    # event type that resolves this pattern
resolve_conditions:   Callable[...] -> bool  # when triggered, does this ghost qualify
resolve_action:       str                    # sm_transition trigger
ttl_seconds:          int                    # ghost expires after this

Patterns register with a central DTF_REGISTRY. The framework core is pattern-agnostic.

## 4. The ghosts table

```sql
CREATE TABLE app_private.ghosts (
    ghost_id            bigserial PRIMARY KEY,
    driver_id           text NOT NULL,
    pattern_name        text NOT NULL,
    offer_id            text,
    ghost_lat           double precision,
    ghost_lng           double precision,
    ghost_at            timestamptz NOT NULL,
    resolution_payload  jsonb NOT NULL DEFAULT \'{}\'::jsonb,
    expires_at          timestamptz NOT NULL,
    resolved_at         timestamptz,
    resolved_by_trigger text,
    resolution_kind     text
);

CREATE INDEX idx_ghosts_active
    ON app_private.ghosts (driver_id, pattern_name)
    WHERE resolved_at IS NULL;

CREATE INDEX idx_ghosts_expiry
    ON app_private.ghosts (expires_at)
    WHERE resolved_at IS NULL;
```

**Invariant:** At most one active ghost per (driver_id, pattern_name, offer_id) tuple. If a detector fires again for the same tuple, the existing row is UPDATEd (most-recent-cluster wins), not INSERTed. Enforced at the sm_transition level.

## 5. 4-Box layering

### 5.1 MONITOR — unchanged

driver_heartbeat.py receives heartbeats. No DTF logic here. MONITOR is pure input receipt.

### 5.2 DIAGNOSE — dtf_detectors.py

Pure read-only module. Registers detectors. Single entry point:

```python
def evaluate_all_detectors(driver_id, state_row, cur, heartbeat_ctx):
    """
    Run every registered detector against current context.
    Returns 0 or more ghost candidates (detectors can fire concurrently
    — they record, they don\'t act).

    Pure DIAGNOSE: reads state_row, reads cluster via detect_cluster(),
    reads BMOAR context. NEVER writes to DB. NEVER calls sm_transition.
    """
```

Each detector is a class:

```python
class DTFDetector:
    pattern_name: str
    def detect(self, driver_id, state_row, cur, hb_ctx):
        """Returns candidate or None. Must be idempotent and pure."""
```

### 5.3 PLAN — driver_heartbeat.py

After check_convergence returns its primary verdict, PLAN layer iterates DTF candidates:

```python
verdict, new_state, error, *extra = check_convergence(...)

dtf_candidates = dtf_detectors.evaluate_all_detectors(
    driver_id, state_row, cur, hb_ctx
)
for candidate in dtf_candidates:
    if DTF_RESOLVE_ENABLED:
        sm_transition(
            trigger=f"ghost_{candidate.pattern_name.lower()}_recorded",
            ...
        )
    else:
        logging.info(f"[DTF_SHADOW] would record: {candidate}")
```

### 5.4 EXECUTE — sm_transition() + ghost table

Every DTF trigger lands in sm_transition. The function:

1. Validates trigger against valid_state_transitions
2. UPSERTs into app_private.ghosts (one active per driver_id, pattern_name, offer_id)
3. For resolution triggers: atomically drains qualifying ghosts + performs resolution action
4. Logs to driver_trip_state_log for every state-changing resolution

Resolution drain logic — generic across all patterns:

```sql
WITH qualifying_ghosts AS (
    SELECT * FROM app_private.ghosts
    WHERE driver_id = p_driver_id
      AND resolved_at IS NULL
      AND expires_at > NOW()
      AND pattern_name = ANY(p_patterns_this_trigger_resolves)
    ORDER BY ghost_at DESC
    LIMIT 1
)
UPDATE app_private.ghosts
SET resolved_at = NOW(),
    resolved_by_trigger = p_trigger,
    resolution_kind = \'retroactive_nail\'
WHERE ghost_id IN (SELECT ghost_id FROM qualifying_ghosts)
RETURNING *;
```

## 6. Defined patterns (v1)

### 6.1 DTF_DROPOFF_ON_NEXT_PICKUP

**Problem:** UH-class address mismatch. Passenger exits at a location not reachable via BMOAR gates and not within proximity of geocoded pin. Driver then accepts a new offer and nails its pickup. The earlier dropoff is retroactively certain.

**Detect conditions:**
- State: IN_TRIP or REFINE_DROPOFF or STACKED
- Gate 1 odometer (>= 0.9 x trip_miles): PASS
- Gate 3 cluster exists: PASS
- Gate 2 pivot/breadcrumb match: FAIL
- enable_proximity (dist_m < 200): FAIL

**Ghost shape:**
- offer_id = current_offer_id
- ghost_lat, ghost_lng = cluster_median_lat, cluster_median_lng
- resolution_payload: cluster_spread_m, cluster_n, cluster_duration_s
- expires_at: ghost_at + 5 minutes

**Resolve trigger:** INITIAL_NAIL (new pickup nail fires)

**Resolve conditions:**
- New pickup coord within 0.5mi (800m) of ghost coord
- Ghost age <= 5 minutes at resolution time

**Resolve action:** atomic two-transition sm_transition:

1. Ghost\'s offer_id: IN_TRIP -> UNCOMMITTED via dropoff_confirmed_retroactive at (ghost_lat, ghost_lng), actual_dropoff_at=ghost_at
2. New pickup proceeds normally: UNCOMMITTED -> ENROUTE -> IN_TRIP via existing triggers

### 6.2 DTF_UNATTACHED_RIDE (v2 — deferred)

**Problem:** System declined an offer, driver accepted in Uber app anyway. Entire ride happens without system awareness. Only signal: driver moves, eventually accepts a NEW system offer.

**Detect conditions:**
- State: UNCOMMITTED
- Extended stop (>5min) at location not in known home/zone set
- Odometer has incremented since last event (driver actually drove somewhere)

**Ghost shape:**
- offer_id = NULL (no offer in system for this ride)
- ghost_lat, ghost_lng = stop cluster median
- resolution_payload: odometer_delta, stop_duration_s
- expires_at: ghost_at + 10 minutes

**Resolve trigger:** offer_accepted

**Resolve conditions:**
- New pickup within 1.0mi of ghost coord
- Ghost age <= 10 min

**Resolve action:** Synthesize pseudo-ride record in offer_history with app_verdict=INFERRED_UNATTACHED, actual_dropoff_at=ghost_at, actual_dropoff_lat/lng=ghost coord. Then proceed with normal offer_accepted.

**Status:** Deferred to v2. Requires home/zone detection which doesn\'t exist yet.

### 6.3 DTF_ROUND_TRIP_DROPOFF_ON_STACK (v1)

**Problem:** Round-trip ride (pickup==dropoff coord). Driver returns to origin, passenger exits, driver accepts a new stacking offer 60-180s later. Current code: STACKED clobbers current_offer_id, primary dropoff orphaned. This is the Transco 7540 pattern.

**Detect conditions:**
- State: IN_TRIP or REFINE_DROPOFF
- Offer is round-trip: abs(pickup_lat-dropoff_lat) < 0.0005 AND abs(pickup_lng-dropoff_lng) < 0.0005
- cumulative_miles >= 0.9 x trip_miles
- Cluster exists near dropoff_lat/lng (within 300m)

**Ghost shape:**
- offer_id = current_offer_id (the round-trip offer)
- ghost_lat, ghost_lng = cluster median
- resolution_payload: was_round_trip=true
- expires_at: ghost_at + 5 minutes

**Resolve trigger:** offer_accepted (incoming stack offer)

**Resolve conditions:**
- Current state at resolve time: IN_TRIP or REFINE_DROPOFF (not yet STACKED — we catch this before STACKED transition)
- Ghost is for current_offer_id

**Resolve action:** Atomic:

1. Round-trip offer: IN_TRIP/REFINE_DROPOFF -> UNCOMMITTED via dropoff_confirmed_retroactive at ghost coord
2. Incoming offer: UNCOMMITTED -> ENROUTE via offer_accepted
3. current_offer_id now points to the new offer (per live-pointer rule)

**Note:** This ordering means STACKED is never entered when the prior offer was round-trip + ghost-recorded. Semantically correct: there is no primary trip to stack onto; the prior trip ended at the origin stop.

### 6.4 DTF_PICKUP_ON_DEPARTURE (v2 — deferred)

**Problem:** Pickup candidate recorded (driver stopped briefly near pickup) but unified predicate couldn\'t confirm (no gate passed). Driver then departs with passenger and trip is underway but system still in ENROUTE.

**Detect:** Analog of dropoff Watchdog B, pickup-side. Driver stops near pickup for 3-8s, no confirmation fires. Driver departs 300m at trip-consistent speed.

**Resolve trigger:** departure detection. Deferred — needs more analysis on false-positive rate for pickup departures (driver might have stopped briefly before getting to actual pickup).

## 7. State machine trigger additions (HOLY TEXT)

Adding to app_private.valid_state_transitions:

| from_state     | to_state    | trigger                          | purpose                                                      |
|----------------|-------------|----------------------------------|--------------------------------------------------------------|
| IN_TRIP        | IN_TRIP     | ghost_dropoff_recorded           | Side-car ghost write during IN_TRIP, no state change        |
| REFINE_DROPOFF | REFINE_DROP | ghost_dropoff_recorded           | Side-car ghost write during REFINE_DROPOFF                  |
| STACKED        | STACKED     | ghost_dropoff_recorded           | Side-car ghost write during STACKED                         |
| IN_TRIP        | UNCOMMITTED | dropoff_confirmed_retroactive    | Ghost drain on next-pickup nail or stack accept             |
| REFINE_DROPOFF | UNCOMMITTED | dropoff_confirmed_retroactive    | Ghost drain                                                  |
| STACKED        | UNCOMMITTED | dropoff_confirmed_retroactive    | Ghost drain when round-trip stacked                         |
| IN_TRIP        | UNCOMMITTED | ghost_expired                    | Cleanup path when no retroactive resolve happens            |
| REFINE_DROPOFF | UNCOMMITTED | ghost_expired                    | Cleanup                                                      |

**Note:** dropoff_confirmed_retroactive is functionally equivalent to dropoff_confirmed for state purposes. It is a distinct trigger to make audit logs distinguishable — every retroactive resolution is queryable.

**Rule:** These triggers cannot be added to the runtime code without first adding the matrix rows above to valid_state_transitions via migration. Enforcement gate 2 rejects unknown triggers.

## 8. Feature flags (shadow-to-live gradient)

```python
DTF_SHADOW_MODE        = True
DTF_RESOLVE_ENABLED    = False

DTF_ENABLE_DROPOFF_ON_NEXT_PICKUP      = False
DTF_ENABLE_ROUND_TRIP_DROPOFF_ON_STACK = False
DTF_ENABLE_UNATTACHED_RIDE             = False
DTF_ENABLE_PICKUP_ON_DEPARTURE         = False
```

**Deployment sequence:**

1. Ship framework + all detectors in DTF_SHADOW_MODE=True, DTF_RESOLVE_ENABLED=False. Ghosts NOT written; shadow log only.
2. Validate one drive. Review shadow logs — do detectors fire on the right events?
3. Flip DTF_SHADOW_MODE=False, DTF_RESOLVE_ENABLED=True, enable ONE pattern. Ghosts written, resolution fires for that pattern only.
4. Validate drive. Confirm retroactive resolutions are correct.
5. Enable next pattern. Repeat.

**Rollback procedure:**
- Flip DTF_RESOLVE_ENABLED=False → detectors go silent, no new writes
- Existing unresolved ghosts: leave in place (they expire naturally via expires_at)
- Existing resolved ghosts: check resolution_kind to audit, manually correct offer_history rows if a resolution was wrong

## 9. Observability

Every ghost lifecycle event logged:

- [DTF] detector_fired pattern=NAME offer=ID ghost_at=TS
- [DTF] ghost_recorded ghost_id=X
- [DTF] ghost_resolved ghost_id=X by_trigger=T resolution_kind=K
- [DTF] ghost_expired ghost_id=X
- [DTF_SHADOW] would_record pattern=NAME  (shadow mode only)
- [DTF_SHADOW] would_resolve ghost_id=X   (shadow mode, if resolve trigger fires)

Query views:

```sql
CREATE VIEW app_private.view_active_ghosts AS
    SELECT * FROM app_private.ghosts
    WHERE resolved_at IS NULL AND expires_at > NOW();

CREATE VIEW app_private.view_dtf_resolution_rate AS
    SELECT
        pattern_name,
        DATE_TRUNC(\'day\', ghost_at) AS day,
        COUNT(*) AS total_ghosts,
        COUNT(*) FILTER (WHERE resolution_kind = \'retroactive_nail\') AS resolved,
        COUNT(*) FILTER (WHERE resolution_kind = \'expired\') AS expired,
        COUNT(*) FILTER (WHERE resolution_kind = \'superseded\') AS superseded
    FROM app_private.ghosts
    GROUP BY pattern_name, DATE_TRUNC(\'day\', ghost_at);
```

## 10. Testing requirements (production gate)

Before ANY pattern flips to live:

- Unit test for each detector: synthetic (state_row, hb_ctx) -> expected DTFCandidate or None
- Unit test for each resolution: synthetic ghost + triggering event -> expected sm_transition sequence
- Integration test: full flow through test Cloud Run for each pattern
- Replay test: at least ONE historical ride matching the pattern, verified that new code would have correctly resolved

Tests must be in test_dtf.py and test_dtf_integration.sh. Pattern cannot be enabled in production until green on both.

## 11. Migration safety

**Gate order for first DTF deploy:**

1. DB migration: CREATE TABLE app_private.ghosts + indexes + valid_state_transitions rows for all ghost triggers
2. Deploy code with DTF_SHADOW_MODE=True, DTF_RESOLVE_ENABLED=False
3. Verify: no new rows in ghosts (shadow mode), shadow logs appear in Cloud Run
4. Validate on one drive. Review shadow logs.
5. Only then: enable resolution.

**Rollback plan if migration fails:**
- Drop ghosts table (data loss acceptable; no live data until Phase 3)
- Revert valid_state_transitions inserts
- Revert code deploy

## 12. What DTF does NOT do (explicit non-goals)

- DTF does not replace BMOAR. BMOAR is the primary fire path.
- DTF does not replace Watchdog A. Watchdog A is the mercy-kill for when BOTH primary and DTF fail.
- DTF does not introduce writes from DIAGNOSE. Every write is via sm_transition from PLAN.
- DTF does not query live Uber APIs. Resolution is purely from local state.
- DTF does not handle cancellation. Cancellation inference is separate (Monitor feeds).

## 13. Open questions

**OQ-DTF-1.** The resolution_payload field is jsonb. Do we want schema validation per pattern (to catch drift), or is loose schema fine for v1? Lean: loose for v1, add validation if drift bites us.

**OQ-DTF-2.** What happens if two patterns detect simultaneously on the same heartbeat (UH-class AND round-trip both trigger)? Current design: both ghosts recorded separately. Resolution trigger processes each pattern independently. Could result in double-resolution attempts. Mitigation: resolution logic must be idempotent and first-wins.

**OQ-DTF-3.** Clock skew — ghost_at comes from server NOW(), but the actual event (passenger exit) may have been seconds earlier (last cluster heartbeat). Do we backdate ghost_at to last_cluster_heartbeat_ts? Lean: yes, use cluster end time as ghost_at for fidelity.

**OQ-DTF-4.** Ghost cleanup cron — who nightly-expires ghosts past expires_at? New cron or piggyback on existing? Lean: new cron cron_dtf_ghost_cleanup, runs hourly, UPDATEs resolved_at=NOW() resolution_kind=expired for rows past expires_at.

---

## Appendix A: Transco 7540 as DTF_ROUND_TRIP_DROPOFF_ON_STACK case study

**Real timeline:**
- 00:21:06 — offer 7540 accepted (round-trip, pickup==dropoff at Transco)
- 00:29:52 — ENROUTE -> IN_TRIP via gps_convergence (pickup nail)
- 00:33:28 — IN_TRIP -> REFINE_DROPOFF via approaching_dropoff
- 00:53:06 – 00:58:37 — cluster forms near Transco pin (driver returned to origin). 104s total stopped near pin at 249-273m distance. 1st 60s+ cluster visible.
- 00:54:57 — STACKED offer_accepted for 7543, state transitions REFINE_DROPOFF -> STACKED, current_offer_id = 7543. 7540 dropoff orphaned.
- 00:57:24 — manual_reset. 7540 never got actual_dropoff_at written.

**With DTF active:**
- 00:53:06 — cluster forms. DTF_ROUND_TRIP_DROPOFF_ON_STACK detector fires:
  - State: REFINE_DROPOFF ✓
  - Round-trip: ✓
  - Odometer >= 0.9 × 13.9mi ≈ 12.5mi ✓ (driver actually drove the round trip)
  - Cluster near dropoff: ✓
- Ghost recorded: offer_id=7540, ghost_lat/lng = cluster median, expires_at = ghost_at + 5min
- 00:54:57 — offer_accepted arrives for 7543. In sm_transition, BEFORE the normal REFINE_DROPOFF -> STACKED transition:
  - Check for active ghosts where offer_id = current_offer_id (7540), ghost age <= 5min → MATCH
  - Fire atomic: REFINE_DROPOFF -> UNCOMMITTED via dropoff_confirmed_retroactive at ghost coord
  - Then fire: UNCOMMITTED -> ENROUTE via offer_accepted for 7543
- Result: 7540 correctly shows actual_dropoff_at=00:53:06, actual_dropoff_lat/lng=cluster median. 7543 processes normally.

## Appendix B: Calhoun/UH as DTF_DROPOFF_ON_NEXT_PICKUP case study

Hypothetical — exact ride data TBD, but pattern is:
- IN_TRIP on Calhoun Rd offer
- Driver goes to UH Main Campus (3km north on MLK)
- Gates: Gate 1 pass (full trip miles), Gate 2 fail (MLK not Calhoun in breadcrumb), Gate 3 pass (cluster at UH), proximity fail (3km from pin)
- DTF_DROPOFF_ON_NEXT_PICKUP fires detector → ghost recorded at UH cluster
- Next offer accepted after UH, driver nails new pickup within 5min, within 800m of ghost
- Retroactive: IN_TRIP -> UNCOMMITTED at UH coord
- Next ride proceeds normally

---

END DTF_DESIGN.md v0.1