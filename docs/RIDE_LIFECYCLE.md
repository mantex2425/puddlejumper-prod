# Ride Lifecycle — Architectural Reference

**Status:** Product law as of demolition 2026-05-04.
**Replaces:** `CANONICAL_RULES.md` sections III (Logic Rules), VIII (4-Box Controller — file assignments), IX (Enforcement Layers), X (Implicit Cancellation), XI (State Levels), XII (ABORT Guard).
**Companion:** `SIMPLIFIED_ARCHITECTURE.md` (the architectural pivot that motivated this rewrite). Where the two documents disagree, this one wins.
**Modification policy:** changes require paired ratification (Andrew + Gemini + Claude).

---

## 1. The 1-bit memory model

`current_offer_id` on `app_private.driver_trip_state` is the system's only memory of which ride is being driven. It is `NULL` (no active ride) or set to an offer's identifier (ride in progress). There are no other states.

The legacy 4-state model (UNCOMMITTED, ENROUTE, IN_TRIP, STACKED) and its hierarchical controllers were demolished on 2026-05-04. References to those states in older documentation are historical artifacts.

```
NULL                     : driver is not currently committed to any ride
<offer_id>               : driver is committed to that offer; pickup observed,
                           dropoff not yet observed
```

The transition rules:

```
NULL  →  <offer_id>      : observed by FirePickup action (a pickup cluster
                           matched an offer in the queue)
<X>   →  NULL            : observed by FireDropoff action for offer X
                           (a dropoff cluster matched offer X)
<X>   →  <Y>             : observed by Case D (a pickup cluster matched
                           offer Y while X was active — implicit cancellation
                           of X, sequenced as FireDropoff(X, canceled) +
                           FirePickup(Y) within a single heartbeat)
```

There are no other transitions. Nothing else writes `current_offer_id`.

---

## 2. The 4-box controller (architecture, not file mapping)

The system separates concerns into four layers. This is the architectural discipline; the file-by-file mapping is in §6.

```
MONITOR    Receive sensor input only. No logic, no writes.
DIAGNOSE   Interpret sensor data into structured observations.
           Pure reads only. No writes.
PLAN       Apply business logic to decide what should happen.
           No DB writes; pure function input → action list.
EXECUTE    The only write path. Period.
```

The discipline question to ask before any code change:

1. Which 4-box layer does this belong to?
2. Which Monitor feed triggers it?
3. Is it Diagnose (read), Plan (logic), or Execute (write)?
4. If Execute — does it go through the heartbeat handler's `_execute_action` path?

If the answer to (4) is "no" and the code writes `current_offer_id`, it's wrong.

---

## 3. The data flow

Every heartbeat:

```
1. MONITOR        driver_heartbeat_bp receives a POST with GPS + odometer
2. DIAGNOSE       cluster_detection.detect_cluster() examines the last 60s of
                  heartbeat_log and returns a Cluster (or None)
3. DIAGNOSE       _project_queue() reads offer_history for offers within the
                  GC window (LEAST(GREATEST((pu_min + trip_min) * 1.5, 15), 240)
                  minutes from offer creation, regardless of accept/decline
                  verdict) and returns list[Offer]
4. DIAGNOSE       WhereAmI(cur).evaluate_with_diagnostics(driver_id, queue)
                  per-bucket matchers select the highest-confidence target
                  per offer; returns list[WAIMatch] where each match has
                  exactly three fields (offer_id, location_type, confidence)
5. PLAN           dispatch(matches, current_offer_id, queue_offer_ids)
                  pure function applies §4 case logic; returns list[Action]
6. EXECUTE        _execute_action loop runs each action; each action's
                  side effects are atomic (transaction-bounded)
7. EXECUTE        _log_decision_context writes pudo_decision_context audit
                  row with current_offer_id_at_eval (the read at step 3, not
                  the write at step 6)
8. RESPOND        heartbeat returns 200 OK with optional voice payload
```

That is the entire heartbeat handler. Nothing else.

---

## 4. The seven dispatch cases

Dispatch is a pure function. It receives `(matches, current_offer_id, queue_offer_ids)` and returns `list[Action]`. The seven cases are exhaustive — every input pattern routes to exactly one case. See `dispatch.py` for the implementation; this section is the contract.

| Case | matches | current_offer_id | Action(s) | Meaning |
|------|---------|------------------|-----------|---------|
| A | empty | NULL | `LogNoMatch` | idle, nothing matched |
| B | `[(N, pickup)]` | NULL | `FirePickup(N)` | start ride N |
| C | `[(M, dropoff)]` | M | `FireDropoff(M)` | complete ride M |
| D | `[(N, pickup)]` | M (M ≠ N) | `FireDropoff(M, canceled)`, `FirePickup(N)` | implicit cancel of M, start N |
| E | ambiguous (multiple matches) | any | `LogAmbiguousMatch` | refuse to fire on uncertainty |
| F | `[(M, dropoff)]` | NULL | `FireDropoff(M, pickup_missed)` | dropoff observed, pickup never was |
| G | `[(M, pickup)]` | M | `LogPickupRematch` | pickup re-observed for active ride; idempotent no-op |

### Case D: implicit cancellation

Cases C and D handle the only two ways an active ride ends. Case C is "the driver completed the ride normally." Case D is "the driver started a different ride, which is only possible if the prior ride was canceled or no-show."

The fired actions in Case D are sequenced within a single heartbeat:
1. `FireDropoff(M, outcome="canceled")` — clears `current_offer_id` to NULL
2. `FirePickup(N)` — sets `current_offer_id` to N

Both transitions complete in one heartbeat. There is no atomic-swap bookkeeping, no STACKED intermediate state, no primary/secondary tracking.

### Case E: ambiguous matches

If multiple matches tie at confidence ≥ threshold and `dispatch` cannot reduce them to a single winner, the system fails closed: log the ambiguity and fire nothing. Recovery on the next heartbeat as the cluster evolves.

### Case F: missed pickup recognition

If the driver completes a ride whose pickup was never auto-confirmed, the dropoff cluster matches an offer in the queue while `current_offer_id` is NULL (or set to a different offer). The system records the dropoff honestly with `outcome="pickup_missed"` rather than retroactively synthesizing a pickup at an arbitrary cluster point.

### Case G: idempotency safety net

Pickup re-match while the active ride is the matched offer is a no-op. The pickup has already fired. Re-firing it would update `current_offer_id` redundantly and produce a spurious second voice notification. This case exists specifically to prevent that.

---

## 5. The naked-list contract

`WhereAmI.evaluate()` returns `list[WAIMatch]`. A `WAIMatch` has exactly three fields:

```python
@dataclass(frozen=True)
class WAIMatch:
    offer_id: str
    location_type: Literal["pickup", "dropoff"]
    confidence: float
```

No coordinates. No topology metadata. No status labels. The downstream interpreter (the heartbeat handler) reads coordinates from the cluster object and offer queue directly.

This is the encapsulation firewall. WAI is a pure sensor. Code that bypasses WAI to infer PUDOs from raw cluster proximity violates the architecture and is a code-review blocker.

The matchers that compute confidence (`_match_intersection`, `_match_single_road`, `_match_apartment_complex`, `_match_number_on_street`, etc.) operate inside WAI and consume seven signals per target: proximity, breadcrumb_match, cluster_tightness, cluster_duration, on_target_road, off_wire_pivot, adjacent_road_match. The per-bucket signal weights live in `where_am_i.py` and are tunable per `SIMPLIFIED_ARCHITECTURE.md` §10 A1's calibration scope.

---

## 6. File assignments (post-demolition)

| 4-box layer | Files |
|-------------|-------|
| MONITOR | `driver_heartbeat.py` (input receipt only), Android accessibility service |
| DIAGNOSE | `cluster_detection.py`, `where_am_i.py`, `bead_on_wire.classify_address`, `_project_queue` in `driver_heartbeat.py` |
| PLAN | `dispatch.py`, `decisions/router.py`, `decisions/triangulation_enricher.py` |
| EXECUTE | `_execute_action` and `_log_decision_context` in `driver_heartbeat.py` |

Notably absent: `state_machine.py`, `decisions/state_enricher.py`, `monitor.py`, `pickup_confirm.py`. All deleted in the demolition.

---

## 7. The truth ordering

The system has multiple potentially-conflicting sources of information about reality. The ordering is:

1. **GPS is truth.** Where the driver actually is, right now, observed via the heartbeat stream.
2. **Cluster is observation.** What the GPS stream looks like over the last 60 seconds.
3. **WAI is interpretation.** What the cluster appears to mean given the offer queue.
4. **`current_offer_id` is memory.** What WAI most recently concluded about which offer is being driven.

When these conflict, the lower number wins. Specifically: if `current_offer_id` says ride M is in progress but WAI sees a pickup cluster for offer N, the answer is "ride M was canceled, ride N is starting" (Case D). The state machine's old way of resolving this — refuse the transition because it doesn't match a predicted state — was wrong. Reality wins.

---

## 8. Time discipline

UTC is mandatory at every layer except the UI edge.

- Postgres: `(NOW() AT TIME ZONE 'UTC')`, or `NOW()` directly on `timestamptz` columns
- Python: `datetime.now(timezone.utc)`, never `datetime.now()` (which is naive local time)
- UI: localizes to driver-local time (Texas time for Andrew's Houston operation) at presentation

Engine code is timezone-agnostic. Texas-time conversion happens only in the React UI layer or in `psql -c "SET TIMEZONE TO 'America/Chicago'"` for forensic CLI sessions.

---

## 9. Coordinate discipline

Use `app_private` canonical functions for all coordinate work:

```
app_private.coords_to_h3(lat, lng)            -- lat first, lng second
app_private.h3_to_lat(h3) / h3_to_lng(h3)
app_private.coords_to_point(lat, lng)
app_private.coords_to_geography(lat, lng)
app_private.distance_miles(lat1, lng1, lat2, lng2)
```

Direct PostGIS calls are forbidden:

- `ST_MakePoint` — use `coords_to_point`
- `h3_latlng_to_cell` / `h3_cell_to_latlng` — use the canonical helpers

If you find yourself writing `ST_MakePoint(lng, lat)` and reasoning about argument order, stop. Use the abstraction. Lat first, always.

---

## 10. What the heartbeat handler does NOT do

This is the inverse of §3, and it is short by design.

- It does not write `current_offer_id` outside the `_execute_action` path.
- It does not consult a state machine.
- It does not call `sm_transition()`.
- It does not enforce state transitions via Postgres triggers.
- It does not auto-reset stuck states (because there are no states).
- It does not predict what the driver will do next.

The architecture is reactive. The driver acts; the system observes; the matcher matches; the dispatcher decides; the executor writes. There is no prediction layer to be wrong.

---

## 11. Auto Nail It is the contract

Production drivers do not see manual nail buttons. Every pickup and every dropoff confirmation arrives via the auto-detection chain described in §3. If the chain fails to fire for a real PUDO, the bug is in the matcher (cluster threshold, signal weights, geocode quality) — not in the absence of a manual fallback. Adding a manual button is band-aid work that masks matcher bugs and trains drivers on a workflow that will be removed.

The single exception: dev builds for Andrew's own testing may carry a manual nail surface for diagnostic purposes. This surface is dev-only and contributes nothing to production.

---

## 12. Provenance

This document was created on 2026-05-04 as part of the state-machine demolition. It replaces sections of `CANONICAL_RULES.md` that had been marked deprecated since the Sprint A architectural pivot ratified 2026-04-30 morning. The pivot itself is documented in `SIMPLIFIED_ARCHITECTURE.md`. The execution that retired the deprecated sections is documented in `DEMOLITION_PLAN_2026-05-04.md`.

🎩🐸🏁