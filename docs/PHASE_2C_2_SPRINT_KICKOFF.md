# Phase 2c.2 Sprint Kickoff — TAD Gate + Exit Velocity + Head 4

**Date:** 2026-05-07
**Status:** Ratified by Gemini (v3.2 design), ready for code-chat authoring
**Branch base:** `demolition-2026-05-04` at commit `a840934` (post gate-layer + monitor merges)
**Predecessor docs:** `docs/PHASE_2C_2_DYNAMIC_ODOMETER_SNAPSHOT.md` (superseded), `MOTION_GATE_KICKOFF.md` (analogous pattern)

---

## Mantra (architectural anchor)

**The only reason for this system is to price locations.**

Every implementation decision serves that goal. PUDOs identify locations; price tagging the pickup happens at the same moment as the pickup confirmation; the dropoff identification seeds the POI cache to reduce future Google API costs.

When in doubt during implementation, ask: "does this serve the mantra?" If yes, proceed. If no, simplify.

---

## Sprint scope

Single atomic commit. Three new primitives + integration into existing matcher + schema migrations.

### New modules

1. **`tad.py`** — Time and Distance gate. Pure arithmetic over `offer_history` timestamps and odometer values. No state machine. Computes `expected_*` values at offer receipt; evaluates clusters against those expectations at evaluation time. Returns per-offer pass/fail with optional time confidence boost.

2. **`escape_detection.py`** — Exit velocity primitive. Watches heartbeats post-pickup-confirmation. Fires when driver has moved >150m from cluster centroid OR sustained >15mph for 30s. Sticky once fired (no oscillation). Timeout at 30 minutes (festival graceful degradation).

### Existing module changes

3. **`where_am_i.py`** — Add Head 4 (`_signal_poi_type_match`), `CLASS_TO_TYPE_MAP` constant, and refactor matcher decision logic to: (a) call `tad.evaluate_tad_gate()` first, (b) run spatial logic only on TAD-passing offers, (c) apply best-offer-match.

4. **`decisions/router.py`** — At offer receipt, call `tad.compute_offer_expectations()` to populate the new `expected_*` columns.

5. **`decisions/logger.py`** — Extend offer_history INSERT with the new columns.

6. **`driver_heartbeat.py`** — Wire exit velocity detection into post-pickup heartbeat flow. When fired, write `pickup_exit_time` and `pickup_exit_odometer` to offer_history.

7. **`poi_service.py`** — Bump `API_SEARCH_RADIUS_M` from 50m to 150m (audit-validated).

### Schema migrations

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

Atomic transaction (BEGIN/COMMIT). All idempotent ADD COLUMN IF NOT EXISTS. Apply BEFORE code deploy (per gate layer sprint pattern). Land in `tmp/phase_2c_2_schema.sql`, apply via psql, then deploy code.

### Forensic logging extensions

`pudo_decision_context` rows for cluster evaluations should capture:
- TAD verdict per offer (pass/fail with reason — distance gate, time signal, neither, both)
- Spatial scores per tool (T-Pivot, adjacency, intersection-adjacency, Head 4)
- Combined confidence
- Whether commit happened or was skipped
- For skipped commits: what threshold was missed by how much

This enables Phase 2g tuning analysis. Don't truncate forensic data even when commits are skipped — the misses are how we learn what to tune.

---

## Architectural overview

```
[Heartbeat arrives]
       |
       v
[Motion gate] (already shipped — filters out moving clusters)
       |
       v (cluster passed motion gate)
       |
[TAD gate] ← NEW
       |   - For each offer in queue (DriverQueue.offer_ids_only):
       |     - Distance gate: cluster's odometer >= 0.85 × expected_pickup_distance
       |       OR (for offers <2mi) within ±0.5mi absolute
       |     - Time signal: |cluster_time - expected_arrival| / expected_duration
       |       → +0.15 boost if ≤15%, +0.05 if ≤50%, 0 if >50%
       |       → 0 if exit velocity timeout was set on prior leg
       |
       v (TAD-passing offers, may be 0 or more)
       |
       v (if 0: skip spatial review, no API call, log only)
       v (if 1+: run spatial logic for all TAD-passing offers)
       |
[Spatial confirmation] ← uses existing tools + NEW Head 4
       |   - T-Pivot
       |   - Adjacency (Five Guys case)
       |   - Intersection adjacency (length-bounded)
       |   - Head 4 (POI type match) ← NEW
       |
       v (per-offer spatial confidence)
       |
[Best-offer-match]
       |   - Take TAD-passing offer with highest spatial confidence
       |   - Tiebreaker: created_at (oldest wins)
       |
       v (winning offer + combined confidence)
       |
[Confidence threshold ≥ 0.90?]
       |
       v (yes)                     v (no)
[Commit PUDO]                 [Skip, log forensically]
  - Write actual_pudo_lat/lng
  - Cache POI
  - If pickup: record price to Price Radar
```

---

## Module specifications

### `tad.py`

#### Constants

```python
DISTANCE_GATE_COMPLETION_THRESHOLD = 0.85
SHORT_TRIP_THRESHOLD_MILES = 2.0
SHORT_TRIP_ABSOLUTE_TOLERANCE_MILES = 0.5

TIME_SIGNAL_TIGHT_PCT = 0.15      # ±15% error → +0.15 boost
TIME_SIGNAL_TIGHT_BOOST = 0.15

TIME_SIGNAL_LOOSE_PCT = 0.50      # ±50% error → +0.05 boost
TIME_SIGNAL_LOOSE_BOOST = 0.05

# Outside ±50% → 0 boost (no penalty)
```

#### Public functions

```python
def compute_offer_expectations(
    new_offer: Offer,
    prev_offer: Optional[Offer],   # most recent prior offer in queue, or None
    current_odometer: float,
    now: datetime,
) -> OfferExpectations:
    """
    At offer receipt time, compute the four expected_* values for this new offer.
    Pure arithmetic. Returns a dataclass with the 4 values to write to offer_history.
    
    If prev_offer is None or prev_trip_remaining_time < 0:
        Driver was idle (or prev offer is orphaned).
        expected_pickup_arrival_time = now + (new_offer.pickup_minutes × 60)
        expected_pickup_distance = current_odometer + new_offer.pickup_miles
    Else:
        Compute from prev_offer's expected dropoff anchors.
    
    expected_dropoff_arrival_time = expected_pickup_arrival_time + (trip_minutes × 60)
    expected_dropoff_distance = expected_pickup_distance + trip_miles
    """


def evaluate_tad_gate(
    cluster: Cluster,
    queue_offers: List[Offer],     # already GC'd by DriverQueue
    current_odometer: float,
) -> Dict[int, TadVerdict]:
    """
    For each offer in queue, evaluate distance gate (hard) and time signal (soft).
    Returns map of offer_id → TadVerdict(passed: bool, time_boost: float, reason: str).
    
    Caller (where_am_i) only runs spatial logic on offers where verdict.passed == True.
    """
```

#### Cancellation detection

If `prev_offer.expected_dropoff_arrival_time < now` when computing for new_offer:
- Log forensic event (orphan flag in `pudo_decision_context`)
- Treat as idle case for new_offer expectations
- Don't fail loudly — this is a recoverable signal that previous PUDO was missed/cancelled

### `escape_detection.py`

#### Constants

```python
EXIT_VELOCITY_DISTANCE_M = 150        # primary signal
EXIT_VELOCITY_SPEED_MPH = 15          # secondary signal
EXIT_VELOCITY_SPEED_DURATION_S = 30   # sustained for this long
EXIT_VELOCITY_TIMEOUT_S = 30 * 60     # 30 minutes
```

#### Public functions

```python
def check_exit_velocity(
    pickup_centroid: Tuple[float, float],
    pickup_confirmed_at: datetime,
    recent_heartbeats: List[Heartbeat],
) -> ExitVelocityResult:
    """
    Returns (detected: bool, exit_time: Optional[datetime], 
            exit_odometer: Optional[float], timeout: bool).
    
    Sticky semantics: caller must remember once detected = True, don't re-evaluate.
    Implementation: check the most recent heartbeat against:
      1. Distance from pickup_centroid > 150m → fire
      2. Sustained speed > 15mph for last 30s of heartbeats → fire
      3. Time since pickup_confirmed_at > 30 min AND not yet detected → timeout=True
    """
```

#### Sticky behavior

Once exit velocity has been detected for an offer (i.e., `pickup_exit_time IS NOT NULL`), don't re-evaluate. Caller checks this before invoking. Specifically:

```python
if offer.pickup_exit_time is None and not offer.exit_velocity_timeout:
    result = check_exit_velocity(...)
    if result.detected:
        # Write pickup_exit_time and pickup_exit_odometer
        # Use these as anchors for subsequent expected_dropoff_* calculations
    elif result.timeout:
        # Write exit_velocity_timeout = TRUE
        # Subsequent TAD evaluations: time_signal = 0 (neutral)
```

#### Parking garage handling

The 150m distance trigger handles parking garages reliably (any garage exit covers >150m). The 15mph trigger handles fast surface-street departures. OR semantics ensure both cases are caught.

#### Festival/timeout handling

When `exit_velocity_timeout = TRUE`:
- Distance signal still applies normally (odometer is still valid)
- Time signal forced to 0 in subsequent TAD evaluations for offers in this queue
- Forensic log captures the timeout for tuning analysis

### Head 4 in `where_am_i.py`

#### `CLASS_TO_TYPE_MAP` constant

Maps offer address classes to acceptable Google Places types:

```python
CLASS_TO_TYPE_MAP: Dict[str, FrozenSet[str]] = {
    "single_road": frozenset({
        "restaurant", "gas_station", "convenience_store",
        "parking_garage", "lodging", "shopping_mall",
        # ...the set of POI types that make sense as pickup destinations
        # accessible from a road
    }),
    "intersection": frozenset({
        # subset relevant for intersection-class addresses
    }),
    "poi": frozenset({
        # broad set; almost anything goes for explicit POI addresses
    }),
    "street_number": frozenset({
        # residential is the obvious match, but also commercial street-numbered
        # addresses (10005 FM 1960 = Pappasito's)
    }),
}
```

The exact contents need a careful pass against today's audit data and Google Places' published type taxonomy. Code-chat owns this design with code-level review.

#### `_signal_poi_type_match` function

```python
def _signal_poi_type_match(
    cluster: Cluster,
    offer: Offer,
    poi_results: List[PoiResult],   # from poi_service cache or API
) -> Tuple[float, Optional[str]]:
    """
    Returns (confidence_score, matched_poi_name).
    
    Algorithm:
      1. Look up acceptable types for offer.address_class via CLASS_TO_TYPE_MAP
      2. For each POI in poi_results, check intersection with acceptable types
      3. Score by: (matching POI count, distance from cluster centroid)
      4. Return best match with confidence
    
    If no POIs match acceptable types: return (0.0, None)
    """
```

#### Best-offer-match refactor

Where the existing matcher loops "for each offer, evaluate signals," refactor to:

1. Filter `queue_offers` by TAD verdict (only TAD-passing offers proceed)
2. For each TAD-passing offer, compute spatial confidence (existing tools + Head 4)
3. Add TAD time_boost to spatial confidence
4. Take the offer with the highest combined score
5. If combined score ≥ 0.90, commit PUDO

---

## Acceptance criteria

The sprint is complete when ALL of the following hold:

### Tests passing

1. `pytest` green (currently 435/435 from gate layer sprint; this sprint adds ~25-30 tests, target ~460/460)
2. New tests cover:
   - `tad.compute_offer_expectations` for: idle case, stacked case, orphan case, short trip case
   - `tad.evaluate_tad_gate` for: distance pass/fail, time signal tight/loose/zero, mixed scenarios
   - `escape_detection.check_exit_velocity` for: distance trigger, speed trigger, timeout case, parking garage scenario
   - Head 4 `_signal_poi_type_match` for: residential class with apartment POI, single_road class with restaurant POI, false positive (gas station for residential class)
   - Best-offer-match for: single TAD-pass, multiple TAD-pass with clear winner, multiple TAD-pass with tiebreaker

3. Validation harness (the equivalent of `validate_gate_layer.py` for this sprint) constructed at `tmp/validate_phase_2c_2.py`:
   - Includes scenarios from 2026-05-07 shift data (8 rides, including Ride #4 train delay outlier)
   - Includes 2026-05-06 audit data (Excel Dental, Pappasito's, US-90 strip mall, Houston-no-zoning false-positive cases)
   - Each scenario has expected commit/skip outcome
   - 100% scenarios pass before deploy

### Deployment artifacts

4. Single atomic commit on a focused branch (e.g., `phase-2c-2-sprint`)
5. Schema migration applied to production database BEFORE code deploy
6. Cloud Run deploy succeeds and traffic routes to new revision
7. Smoke check via Bruno `driver_status` request:
   - `expected_pickup_arrival_time` and `expected_pickup_distance` populated for any new offers
   - No regressions in existing fields
   - No null pointer errors in cluster evaluation

### Forensic logging verification

8. After 1 hour of production traffic post-deploy:
   - `pudo_decision_context` rows include TAD verdicts and spatial scores
   - `offer_history` rows for new offers populate the new `expected_*` columns

---

## Empirical thresholds (from 2026-05-07 shift)

These are the initial defaults. Tune from data in Phase 2g.

| Constant | Value | Justification |
|---|---|---|
| `DISTANCE_GATE_COMPLETION_THRESHOLD` | 0.85 | 7/8 of 2026-05-07 rides reached ≥85% completion at actual pickup |
| `SHORT_TRIP_THRESHOLD_MILES` | 2.0 | Below this, percentage math is too noisy |
| `SHORT_TRIP_ABSOLUTE_TOLERANCE_MILES` | 0.5 | Catches Ride #2 case (0.8mi pickup, +0.2mi navigation deviation) |
| `TIME_SIGNAL_TIGHT_PCT` | 0.15 | Most non-outlier rides today within ±15% |
| `TIME_SIGNAL_LOOSE_PCT` | 0.50 | Catches plausible-but-delayed cases without penalizing |
| `EXIT_VELOCITY_DISTANCE_M` | 150 | Conservative; covers parking garage egress |
| `EXIT_VELOCITY_SPEED_MPH` | 15 | Surface-street departure speed |
| `EXIT_VELOCITY_SPEED_DURATION_S` | 30 | Avoids momentary speed spikes |
| `EXIT_VELOCITY_TIMEOUT_S` | 1800 | 30 min covers most festival/rodeo gridlock cases |
| Confidence threshold | 0.90 | Strict initial setting; tune from data |

---

## What this sprint does NOT include

- ❌ Manual nail-it endpoint changes (production constraint: Auto Nail It only)
- ❌ State machine logic (`driverState`, IN_TRIP, etc.) — fully replaced by arithmetic
- ❌ Removing existing pickup logic (T-Pivot, adjacency, intersection) — preserved
- ❌ Tuning thresholds beyond the initial defaults (Phase 2g concern)
- ❌ Building Phase 2c.3 spatial enhancements (defer to data-driven analysis post-2c.2)
- ❌ Removing the existing motion gate or odometer gate from `motion_gate.py` — both stay

---

## Operational state at sprint start

**Branch base:** `demolition-2026-05-04` at `a840934`
- Includes: motion gate (Sprint A), monitor queue display, Mock Mirage bugfix
- Verified working: gate layer producing closed/moving verdicts correctly
- Current production: `puddlejumper-api-00594-ph5`

**Cherry-pick consideration:** `origin/phase-2c2-wip-2026-05-06-stash` at `4e48916` contains abandoned Phase 2c.2 patch 2b deeper changes (single_road weight reshuffle, `_match_poi` rename). Code-chat should evaluate whether any of that work is recoverable for this sprint. If yes, cherry-pick before authoring. If no, abandon.

**Schema state:** Gate layer sprint already added these columns:
- `pudo_decision_context.motion_gate_result text`
- `pudo_decision_context.odometer_gate_result jsonb`
- `pudo_decision_context.gate_held_offer_ids text[]`
- `pudo_decision_context.gate_held_legs text[]`
- `offer_history.miles_at_offer_receipt numeric`
- `offer_history.lat_at_offer_receipt double precision`
- `offer_history.lng_at_offer_receipt double precision`

This sprint adds 7 more columns (per migration above).

---

## Implementation guidance

### Recommended sequence

1. **Recon first.** Read `where_am_i.py` end-to-end. Read `decisions/router.py` offer-receipt path. Read `driver_heartbeat.py` motion gate integration point. Confirm the existing four spatial tools (T-Pivot, adjacency, intersection-adjacency) are still in `where_am_i.py` and understand their signatures.

2. **Schema first.** Land migration in `tmp/phase_2c_2_schema.sql`. Apply to production database via psql. Confirm with `\d offer_history`. This makes subsequent code work easier (the columns exist when code references them).

3. **Author `tad.py` standalone.** No integration yet. Get unit tests green first. ~200 lines + ~10 tests.

4. **Author `escape_detection.py` standalone.** Same approach. ~80 lines + ~6 tests.

5. **Author Head 4 in `where_am_i.py`.** Add `CLASS_TO_TYPE_MAP` and `_signal_poi_type_match`. Unit tests against synthetic POI results. ~50 lines + ~5 tests.

6. **Refactor matcher decision logic in `where_am_i.py`.** Wire TAD gate before spatial review. Wire best-offer-match. Wire combined confidence threshold. ~30 lines + integration tests.

7. **Wire offer receipt path** in `decisions/router.py` to call `tad.compute_offer_expectations()` and persist results. ~20 lines + 2-3 tests.

8. **Wire offer logging** in `decisions/logger.py` to include new columns. ~10 lines + 1 test.

9. **Wire heartbeat path** in `driver_heartbeat.py` to call `escape_detection.check_exit_velocity()` and persist results. ~25 lines + 2-3 tests.

10. **Validation harness** at `tmp/validate_phase_2c_2.py`. Pull 2026-05-07 shift scenarios + 2026-05-06 audit cases. 100% pass before deploy.

11. **Single atomic commit, squash-merge to demolition-2026-05-04, deploy.**

### Audit-first discipline

Per protocol lessons from this project:
- **L-6:** Inspect actual production artifacts before authoring assertions
- **L-7:** Cross-check architectural rulings against production conventions
- **L-8:** Live-PG smoke non-negotiable for DB-coupled code
- **L-6 corollary:** Signature-change patches must grep ALL direct callers including in `tests/`

Don't trust this kickoff doc's signatures — read the actual code. Don't trust this kickoff's column descriptions — query `information_schema.columns`. Validate everything against ground truth.

### Terminal safety reminders

- NO markdown blockquote content (`> `) to bash — truncates files
- NO multi-line markdown paste to bash — use `create_file` then `scp`
- Apply scripts in `~/puddlejumper-prod/tmp/`
- NO heredocs with code-like syntax

---

## Code standard

Production-ready complete solutions only. No MVP/skeleton/phased-slice framing. No "ship a small piece first." Full implementation on every component listed above. Design gaps go into the current solution package — not parked for later.

---

## Paired-programming protocol

Code-chat proposes implementation → Andrew runs through Gemini for review → consensus before commit. For DB-coupled code, smoke against live PG before commit. CLI-first execution: SQL wrapped for psql, long output to `/tmp/*.txt`.

---

## After sprint completion

Code-chat authors a sprint completion report mirroring the format of the gate layer sprint's report. The report covers:

- TL;DR (one paragraph)
- What landed (file-by-file)
- Architecture summary
- Validation results (unit + behavioral + harness)
- Deviations from this kickoff (if any) with rationale
- Pre-existing oddities flagged but NOT addressed (don't fix outside scope)
- Operational state (branch, commit, deploy status)
- Open decisions (if any)
- What's left (validation drives, forensic verification)

The report enables architecture-chat or future code-chat to resume cleanly without re-reading every commit.

---

## Cross-session resumption guidance

If this sprint pauses and resumes in a future session:

1. Read this kickoff doc first
2. Read `docs/PHASE_2C_2_FINAL_DESIGN.md` (v3.2, the design summary Gemini ratified)
3. Check current branch state via `git log --oneline -10`
4. Check schema state via `\d app_private.offer_history` and `\d app_private.pudo_decision_context`
5. Resume at the next unfinished item in "Recommended sequence" above

---

## Awaiting code-chat handoff

This kickoff is self-contained. A fresh code-chat can pick it up and execute without needing the design conversation context. The four-tool architecture, the TAD math, the exit velocity primitive, the schema, the acceptance criteria — all locked here.

When code-chat completes, it produces:
1. The sprint commit on `demolition-2026-05-04`
2. A sprint completion report (markdown doc)
3. Production deploy at a new revision number

Architecture-chat (or wherever Andrew picks up next) reads the completion report to understand what landed and decides next steps (Phase 2g tuning sprint, validation drives, etc.).

---

End of kickoff.
