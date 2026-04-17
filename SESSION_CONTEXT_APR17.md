# PuddleJumper Session Context — April 17, 2026
# (Start every new session by reading this file)

## Current Revision
puddlejumper-api-00552-prm (deployed April 16, 2026)
Android: heading field added (Claude Code) — APK build pending

## Test Status
- State machine: 54/54 ✅
- Integration: 49/50 ✅ (T29 known pre-existing)

## System Status: READY TO DRIVE 🐸

---

## Canonical Rules (NON-NEGOTIABLE)

### Coordinates
ALL coordinate operations must use app_private wrapper functions:
- app_private.coords_to_h3(lat, lng)
- app_private.h3_to_lat(h3), app_private.h3_to_lng(h3)
- app_private.coords_to_point(lat, lng)
- app_private.coords_to_geography(lat, lng)
- app_private.distance_miles(lat1, lng1, lat2, lng2)
- Arguments ALWAYS (lat, lng) — NEVER reversed
- NEVER: ST_MakePoint, h3_latlng_to_cell, h3_cell_to_latlng

### Time
- ALL DB time operations: NOW() (bare UTC)
- Chicago timezone ONLY at display/analytics layer
- NEVER: NOW() AT TIME ZONE 'America/Chicago' in engine logic

### 4-Box Architecture (MANDATORY)
- MONITOR: Sensor inputs only — Android + GPS heartbeat. No writes.
- DIAGNOSE: Pure reads — nail_it_core.check_convergence(), sm_read()
- PLAN: Business logic — decisions/engine.py, router.py. No DB writes.
- EXECUTE: ONLY write path — DriverStateMachine.transition() → sm_transition()
- Android is the sensor array — outside the 4-box framework entirely

### State Machine
- Three enforcement gates: Python wrapper → sm_transition() → DB trigger
- NEVER call SET LOCAL app.state_trigger directly from Python
- NEVER write state outside DriverStateMachine.transition()

---

## What Was Shipped April 15-16 (Revisions 00543–00552)

| Revision | Change |
|---|---|
| 00545 | Radar keys persisted to decision_log via patch_decision_log |
| 00546 | DB-backed candidate coords — horizontally scalable Watchdog B |
| 00547 | Watchdog B STACKED atomic swap — full fix, T50 green |
| 00548 | Pillar II — null address pickup confirm radius → 1000m |
| 00549 | High Score candidate promotion + shadow logging |
| 00550 | Vague address geocode cache bypass |
| 00551 | Phase 0 — Unified Stop Buffer + debug endpoint |
| 00552 | Buffer logging + detect_passenger_stops() deployed |

---

## Architecture Breakthrough (April 16 Evening)

### 4-Box as Event-Driven OS Kernel
The 4-box architecture is not just a code organization pattern —
it is a full event-driven operating system kernel:

- MONITOR  = Interrupt handlers    — raw hardware events
- DIAGNOSE = Kernel scheduler      — evaluates interrupt priority  
- PLAN     = Process manager       — decides what runs next
- EXECUTE  = System calls          — the only way to write to disk

DriverStateMachine.transition() IS the system call interface.
Nothing writes state without going through it.

### Eight Architectural Pillars (Grok + Claude synthesis)
1. Expect failure as core design principle (recovery = equal partner)
2. Event-driven monitoring & interrupts (no time-based polling)
3. Adaptive/live route context for stop confidence
4. Provisional + reversible states
5. High-confidence triggers for silent recovery
6. Intelligent tie-breakers (same-spot scenarios)
7. Observability / rich logging of all decisions and recoveries
8. Graceful degradation when recovery can't resolve

### Modern OS Lessons Applied
- Microkernel: keep nail_it_core minimal, extract modules
- Async everything: no blocking in decision path
- Designed fallbacks: every path has explicit degradation mode
- Optimistic concurrency: DB as conflict resolver, not locks
- Actor isolation: per-driver state, no shared memory
- Structured observability: every decision emits full context
- CAP awareness: explicit choices consistency vs availability
- Event sourcing: buffer IS the truth, state is derived

---

## Phase 0 Complete — Stop Buffer Live

### What's in the buffer (nail_it_core.py)
