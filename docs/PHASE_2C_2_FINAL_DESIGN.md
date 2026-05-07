# Phase 2c.2 Design Summary v3.2 — For Gemini Ratification

**Date:** 2026-05-07
**Status:** Awaiting Gemini ratification before authoring
**Predecessors:** v1, v2, v3 superseded; v3.2 is grounded in empirical 2026-05-07 shift data + architectural insights from session

---

## The mantra

**The only reason for this system is to price locations.**

Every architectural decision serves that goal. Identifying PUDOs is the mechanism — geographic price intelligence is the deliverable.

---

## Architectural overview

For each stationary cluster the motion gate lets through, the matcher answers one question:

**"Is this cluster confidently a PUDO for one of the offers in the queue?"**

The answer requires two layers:

1. **TAD (Time And Distance) gate** — uses arithmetic on offer timestamps and odometer values to filter clusters to plausible candidates
2. **Spatial logic** — uses physical-location reasoning (T-Pivot, adjacency, intersection-adjacency, Head 4) to confirm the match

TAD gate runs first. Spatial logic runs only when TAD passes. Combined confidence ≥ threshold → commit PUDO.

---

## Why TAD gate exists — dual purpose

### 1. Cost gatekeeper
Without TAD: spatial logic runs on every cluster → Google Places API call on every cluster → cost unsustainable at scale.

With TAD: spatial logic runs only on TAD-passing clusters → API costs proportional to actual PUDO candidates.

### 2. False positive prevention
**Without TAD, spatial logic alone produces false positives.** Example: driver's actual pickup is at Five Guys inside HEB plaza accessible from US-288. While driving toward it, driver stops at a red light on US-288, 5 miles before Five Guys. There's an Exxon at the intersection.

Spatial logic alone:
- Head 4: "gas_station POI here" → matches some offer's address class
- Adjacency: "near US-288" → true
- Spatial confidence: 0.85+
- **False positive: writes actual_pickup at the Exxon**

TAD gate prevents this:
- At red light: distance traveled is 0.3mi of 5mi estimated = 6% completion
- Distance gate: FAIL (need ≥85% completion)
- Skip spatial review entirely. No API call. No false positive.

TAD anchors spatial logic to the temporal/odometer reality of where the driver actually IS in the trip lifecycle.

---

## TAD gate design

### Core arithmetic

When a new offer N arrives, snapshot remainders from previous offer P:

```
prev_trip_remaining_time     = P.trip_minutes - elapsed_time_since_P_pickup_anchor
prev_trip_remaining_distance = P.trip_miles - odometer_delta_since_P_pickup_anchor
```

Then compute N's expectations:

```
N.expected_pickup_arrival_time = now + prev_trip_remaining_time + (N.pickup_minutes × 60)
N.expected_pickup_distance     = current_odometer + prev_trip_remaining_distance + N.pickup_miles

N.expected_dropoff_arrival_time = N.expected_pickup_arrival_time + (N.trip_minutes × 60)
N.expected_dropoff_distance     = N.expected_pickup_distance + N.trip_miles
```

If P doesn't exist (first offer or driver was idle), the calculation simplifies:

```
N.expected_pickup_arrival_time = now + (N.pickup_minutes × 60)
N.expected_pickup_distance     = current_odometer + N.pickup_miles
```

These four `expected_*` values are stored in `offer_history` at offer receipt time. Snapshot in time.

### No state machine dependency

Phase 2c.2 design does NOT use any `driverState` field or 4-state machine logic. The 1-bit memory (current_offer_id NULL or set) per the May 4 demolition plan is sufficient. TAD math is pure arithmetic over offer timestamps and odometer values from offer_history — no state machine needed.

### Cancellation detection (forensic side-effect)

If `prev_trip_remaining_time < 0` when computing for new offer N, it means the previous trip's expected dropoff already passed. Implications:

- Previous offer was likely cancelled, OR
- Previous offer's dropoff happened but TAD missed confirming it

Either way: treat new offer N as if driver were idle. Log the orphan event for forensic analysis.

### Distance gate (hard, primary)

**Pickup distance gate:**
- For pickups ≥ 2.0 miles: driver must have traveled ≥ 85% of `expected_pickup_distance` since offer received
- For pickups < 2.0 miles: cluster forms within ±0.5 miles (absolute threshold) of expected pickup distance
- Empirically validated from 2026-05-07 shift: 7/8 rides reached ≥85% completion at actual pickup

**Dropoff distance gate:**
- For trips ≥ 2.0 miles: driver must have traveled ≥ 85% of `expected_dropoff_distance` since pickup anchor
- For trips < 2.0 miles: cluster forms within ±0.5 miles of expected dropoff distance

Distance is geometric and reliable. This is the primary gate.

### Time signal (soft, secondary)

Time is volatile (traffic, accidents, festival delays, train crossings). Use as a confidence boost, not a hard gate:

```
time_error_pct = |cluster_time - expected_arrival_time| / expected_trip_duration

if time_error_pct <= 15%:    confidence_boost = +0.15  (very close)
elif time_error_pct <= 50%:  confidence_boost = +0.05  (plausible)
else:                         confidence_boost = 0      (no boost, no penalty)
```

**Never penalize for being late.** External delays are not the driver's fault.

### Time signal applies only when valid

Time signal is meaningful only when the prior leg's anchors are reliable. When exit velocity from previous PUDO was NOT detected (festival case, see below), time signal is set to 0 (neutral).

Distance gate is always applied. Time signal is conditional bonus.

---

## Exit velocity primitive

### Why it matters

Without exit velocity detection, the next leg's TAD calculations are anchored to the wrong moment.

Example without exit velocity:
- Pickup confirmed at 14:00 (cluster centroid, driver stopped)
- Driver waits in parking spot 4 minutes for passenger to load luggage
- Pulls out of lot, navigates exit, enters traffic at 14:04
- Trip really started at 14:04, not 14:00
- If we use 14:00 as pickup anchor, expected_dropoff_arrival_time is 4 minutes too early
- For stacked offers, this error compounds

Exit velocity detects when the driver has truly *left* the cluster and started moving consistently in a direction, anchoring the next leg's TAD calculation correctly.

### Detection logic

```
exit_velocity_detected = TRUE when EITHER:
  - speed > 15 mph sustained for ≥30 seconds, OR
  - distance from cluster centroid > 150m
```

Conservative initial defaults. Tune from data after Phase 2c.2 ships.

### Captured at exit velocity event

When exit velocity fires:
```
pickup_exit_time     = current heartbeat timestamp
pickup_exit_odometer = current cumulative_miles
```

These become the anchors for the NEXT leg's TAD math:

```
expected_dropoff_arrival_time = pickup_exit_time + (trip_minutes × 60)
expected_dropoff_distance     = pickup_exit_odometer + trip_miles
```

### Timeout / festival case

If exit velocity does not fire within a timeout window (e.g., 30 minutes after pickup confirmation), the driver is stuck:

- Festival/rodeo gridlock
- Long passenger loading
- Construction delay

In this case:
- Use pickup_confirmation_time as the time anchor (best available)
- Use odometer_at_pickup_confirmation as the distance anchor (still valid — the odometer hasn't moved much, so distance signal remains accurate)
- **Set a flag** indicating exit velocity timeout occurred
- **Time signal in subsequent TAD evaluation:** set to 0 (neutral)
- **Distance gate:** still applies normally

The festival case is **gracefully degraded**, not catastrophic. Distance + spatial logic can still confirm dropoffs without time confirmation.

---

## Spatial logic (the four tools)

Spatial logic runs ONLY when TAD distance gate passes. Three tools already exist; one is new.

### 1. T-Pivot (existing in `where_am_i.py`)
Driver was on a named road, then pivoted off into a parking lot or complex. Pivot is the arrival signal.

### 2. Adjacency / "Five Guys" logic (existing in `where_am_i.py`)
Cluster is in a plaza or complex adjacent to the target road. Solves: offer says US-288, driver actually stops at Five Guys inside HEB strip mall.

### 3. Intersection adjacency, length-bounded (existing in `where_am_i.py`)
For intersection-class targets: proximity tolerance scales with cross-street length. Self-disqualifies when cross-streets are long (a 20-mile road tells you nothing about positional accuracy).

### 4. Head 4: POI Type Match (NEW in Phase 2c.2)
Semantic check: cluster's Google Places types match the offer's address class. Catches the trap-address case (10005 FM 1960 looks residential but Google says `mexican_restaurant` — Pappasito's Cantina).

### Best-offer-match across queue

When multiple offers pass TAD, spatial logic scores cluster against ALL of them. Highest spatial confidence wins. Tiebreaker: created_at (oldest offer first).

---

## Confidence threshold

**Initial threshold: 0.90 combined confidence (TAD distance gate × spatial confidence + time bonus).**

Tight initial setting per the mantra: skip rather than pollute.

**Tuning plan (Phase 2g):**
1. Run 10-20 shifts at 0.90 threshold
2. Query `pudo_decision_context` for all evaluations including below-threshold
3. Analyze: missed PUDOs vs. false positives
4. Adjust threshold or per-tool weights based on observed distribution

If we write nothing because 0.90 is too strict, Price Radar gets no signal — the system is pointless. If we write false PUDOs at 0.50, Price Radar gets polluted. Tune empirically toward whatever produces the best price signal.

---

## Same logic for pickups and dropoffs

Identical flow:

1. TAD evaluation (uses pickup expectations for pickup PUDOs, dropoff expectations for dropoff PUDOs)
2. Spatial confirmation (same four tools)
3. Confidence threshold (0.90)

**Differences in commit action:**
- Pickup commit: write `actual_pickup_lat/lng` + cache POI + **record price to Price Radar**
- Dropoff commit: write `actual_dropoff_lat/lng` + cache POI

Pickup is the operational priority. Dropoff is secondary (cache warming for future API cost reduction).

---

## Schema additions

```sql
ALTER TABLE app_private.offer_history
  ADD COLUMN IF NOT EXISTS expected_pickup_arrival_time timestamptz,
  ADD COLUMN IF NOT EXISTS expected_pickup_distance numeric,
  ADD COLUMN IF NOT EXISTS expected_dropoff_arrival_time timestamptz,
  ADD COLUMN IF NOT EXISTS expected_dropoff_distance numeric,
  ADD COLUMN IF NOT EXISTS pickup_exit_time timestamptz,
  ADD COLUMN IF NOT EXISTS pickup_exit_odometer numeric,
  ADD COLUMN IF NOT EXISTS exit_velocity_timeout boolean DEFAULT FALSE;
```

All idempotent ADD COLUMN IF NOT EXISTS. Migrations applied separately before code deploy (per gate layer sprint pattern).

---

## Code changes

### New module: `tad.py`
- `compute_offer_expectations(new_offer, prev_offer, current_odometer)` → returns the four `expected_*` values
- `evaluate_tad_gate(cluster, queue, current_odometer)` → returns per-offer TAD verdict (pass/fail) with confidence boost from time signal
- Cancellation detection (negative remainders → orphan flag)

### New module: `escape_detection.py`
- `detect_exit_velocity(heartbeats_since_pickup, pickup_centroid)` → returns (detected: bool, exit_time, exit_odometer)
- Timeout handling (after 30 min, return timeout flag)

### Existing module changes

**`decisions/router.py`** (offer receipt path):
- After offer parse, call `compute_offer_expectations(...)` and write expected_* values to offer_history

**`decisions/logger.py`** (offer logging):
- Insert new expected_* columns into offer_history INSERT

**`where_am_i.py`** (the matcher):
- Add `_signal_poi_type_match` function (Head 4)
- Add `CLASS_TO_TYPE_MAP` constant
- Refactor matcher decision logic:
  - Run TAD gate first via `evaluate_tad_gate(...)`
  - For TAD-passing offers, run spatial logic (existing tools + Head 4)
  - Apply best-offer-match
  - Commit if combined confidence ≥ 0.90

**`poi_service.py`**:
- Bump `API_SEARCH_RADIUS_M` from 50m to 150m (audit-validated)

**`driver_heartbeat.py`** (existing motion gate code):
- Wire exit velocity detection into the post-pickup heartbeat flow
- When exit velocity fires, write `pickup_exit_time` and `pickup_exit_odometer` to offer_history

### Forensic logging

`pudo_decision_context` already has columns for gate verdicts. Add:
- TAD verdict per offer (pass/fail with reason)
- Spatial scores per tool
- Combined confidence
- Whether commit happened or was skipped

This enables Phase 2g tuning analysis.

---

## What this design does NOT include

- ❌ Time as primary gate (volatile; secondary asymmetric bonus only)
- ❌ Houston_ways pre-filter as gate (existing spatial tools already use road-network context)
- ❌ Per-leg odometer math from BEAD (replaced by simpler arithmetic)
- ❌ Dynamic Odometer signal (architectural overengineering)
- ❌ State machine semantics (`driverState`, IN_TRIP, etc.) — all replaced by 1-bit memory + offer_history arithmetic
- ❌ Pickup vs dropoff path divergence (same flow; pickup adds price write)
- ❌ Weight table redesign (existing weight infrastructure preserved)

---

## Implementation scope estimate

- New `tad.py` module: ~200 lines + tests
- New `escape_detection.py` module: ~80 lines + tests
- New `_signal_poi_type_match` in `where_am_i.py`: ~50 lines + tests
- New `CLASS_TO_TYPE_MAP` constant: ~30 lines
- Best-offer-match refactor in `where_am_i.py`: ~30 lines
- `decisions/router.py` and `decisions/logger.py` wiring: ~30 lines
- `driver_heartbeat.py` exit velocity wiring: ~25 lines
- `poi_service.py` constant bump: 1 line
- Tests: ~25-30 unit tests covering TAD + Head 4 + exit velocity + best-match + integration
- Schema migration script: ~10 lines

Single atomic commit. Estimated 8-12 hours of focused work after ratification (larger than v3 estimate due to exit velocity primitive).

---

## Empirical validation grounding

### 2026-05-07 shift (8 rides)

| Gate Configuration | Capture Rate |
|---|---|
| Pickup distance ±10% percentage | 1/8 (validates need for absolute thresholds on short trips) |
| Pickup distance 85% completion | 7/8 (87.5%) |
| Time-only ±15% | 3/8 (validates time as soft signal, not gate) |
| Trip distance ±10% | 5/8 (62.5%) |

Outliers:
- Ride #4 (train delay, +483% time error): TAD distance gate would correctly fail, time gate would have rejected it anyway
- Ride #8 (driver took shortcut, -12.5% trip distance): would fail dropoff distance gate but spatial logic could still confirm via Head 4 + adjacency

### 2026-05-06 audit data

- Real GPS clusters at Excel Dental, Pappasito's, US-90 strip mall: Head 4 finds correct POI types at <10m
- Houston no-zoning false-positive vector: residential clusters returned 0-2 weak commercial POIs at 80-150m, all of which would fail combined confidence threshold

---

## Two-stage philosophy

**Stage 1 (Phase 2c.2 — this sprint):** Ship TAD + spatial integration with conservative thresholds. Capture data on accuracy.

**Stage 2 (Phase 2c.3 — later, only if needed):** Tune thresholds, refine TAD logic, or add additional spatial tools based on observed production behavior. Triggered only if Phase 2c.2 data shows specific failure modes that need targeted solutions.

Audit-first discipline: ship the simplest viable system, observe production, evolve based on data.

---

## Awaiting ratification

Gemini, please review and ratify or push back on:

1. **TAD gate as cost gatekeeper AND false positive preventer** — dual purpose
2. **TAD math: arithmetic on offer_history timestamps + odometer**, no state machine
3. **Distance gate (hard) + time signal (soft, asymmetric)** — distance always applies, time only when valid
4. **85% distance completion** with absolute fallback for short trips (<2mi)
5. **Exit velocity primitive** with conservative defaults (15 mph for 30s OR 150m from centroid)
6. **Festival/timeout graceful degradation** — distance still works, time signal set to 0
7. **Best-offer-match across TAD-passing offers**
8. **0.90 combined confidence threshold for commit** with explicit tune-from-data plan
9. **Same flow for pickups and dropoffs**, pickup adds Price Radar write
10. **Cancellation detection** as forensic side-effect of negative remainders
11. **Schema additions** (7 new columns on offer_history)
12. **Two new modules** (`tad.py`, `escape_detection.py`) plus existing module changes

If ratified, Claude authors a Phase 2c.2 sprint kickoff doc, then a focused code-chat executes implementation.

---

End of summary v3.2.
