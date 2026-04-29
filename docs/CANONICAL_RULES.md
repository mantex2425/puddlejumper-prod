# PuddleJumper Canonical Rules

**Status:** eternal product law. These rarely change. Edits require explicit ratification.
**Load:** at the start of every chat session, before any work.

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

## VI. CURRENT_OFFER_ID — LIVE POINTER

`current_offer_id` is a **live pointer**, not a history field.

- Always points to the **currently active offer** — the one the driver is working RIGHT NOW
- When a stack is accepted, `current_offer_id` immediately advances to the secondary offer
- In STACKED state: `current_offer_id` = secondary offer, `dropoff_lat/lng` = primary dropoff (the one being completed)
- The atomic swap does NOT need to find the secondary offer — it **IS** `current_offer_id`
- The swap's only job: load coords for `current_offer_id` and transition state
- **NEVER** query for "the next offer" — read `current_offer_id` directly

### Corollary

To find the PRIMARY offer during a STACKED ride (for audit purposes), query `offer_history` for `actual_pickup_at IS NOT NULL AND actual_dropoff_at IS NULL` — **NOT** `current_offer_id`.

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

## Notes

- These rules are derived from production lessons across Phase D and Phase E.
- Changes require explicit ratification (not silent edit).
- When a new architectural decision is locked, it gets added here only after it has shipped and stabilized — not before.