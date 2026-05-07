# Phase 2c.2 Sprint Bible

**Status:** OPERATIVE SPEC. Supersedes `docs/PHASE_2C_2_SPRINT_KICKOFF.md` for execution purposes.
**Date:** 2026-05-07
**Branch:** `phase-2c-2-tad-exit-4tools` off `f3f5dc9` on `demolition-2026-05-04`
**Audience:** Claude Code session executing Phase 2 of this sprint. You have the repo. You have no conversation context. Read this end-to-end before authoring anything.

---

## Why this doc exists (read first)

`docs/PHASE_2C_2_SPRINT_KICKOFF.md` was ratified by Gemini before architecture-chat performed code recon on `where_am_i.py`. The recon revealed that the kickoff's mental model — "four spatial tools (T-Pivot, adjacency, intersection-adjacency, Head 4) feeding combined confidence" — does not match the production WAI architecture. The actual architecture is a 7-signal weighted sum with per-class weight tables that Gemini math-checked at lock-in.

A reconciliation memo was authored, run through Gemini, and ratified in architecture-chat on 2026-05-07. **This Sprint Bible captures the reconciled rules.** If you find conflicts between this doc and the kickoff doc, **this doc wins.** The kickoff stays in tree as historical input only.

The reconciliation matters because Phase 2c.2's commit threshold (`weighted >= 0.90 OR (weighted >= 0.80 AND poi_type_match)`) only makes sense in the witness-signal architecture. If you implement Head 4 as a weighted signal, you'll rebalance the `_CONFIDENCE_WEIGHTS` table that Gemini already locked, and the elevator rule becomes incoherent.

**Drift signal: if you find yourself adding `poi_type_match` to `_CONFIDENCE_WEIGHTS`, stop. Re-read this doc.**

---

## The mantra (architectural anchor)

**The only reason for this system is to price locations.**

PUDOs identify locations. Pickup confirmation tags the price. Dropoff identification seeds the POI cache to reduce future Google API costs. Skip rather than pollute Price Radar with weak signals.

When in doubt during implementation, ask: "does this serve the mantra?" If yes, proceed. If no, simplify.

---

## What landed in Phase 1 (architecture-chat, this sprint)

By the time you read this, the following have been committed to `phase-2c-2-tad-exit-4tools`:

1. Schema migration applied to production DB (`migrations/2026-05-07_phase_2c_2_tad_exit.sql`)
2. `Offer` dataclass extended with `pickup_minutes` and `trip_minutes` (Optional[int]) in `pudo_types.py`, plus `_project_offers` in `driver_queue.py` updated to populate them from `offer_history`
3. `tad.py` module with unit tests
4. `_signal_poi_type_match` + `CLASS_TO_TYPE_MAP` + `MatchOutcome` extensions in `where_am_i.py` with unit tests
5. `evaluate()` TAD bouncer integration + commit rule update in `where_am_i.py` with integration tests

Run `git log --oneline f3f5dc9..HEAD` on the sprint branch to see exactly what landed. Read those commits before authoring Phase 2.

### Phase 1 prerequisite discoveries

The kickoff and the original Bible drafting did not anticipate that `Offer` lacked `pickup_minutes` and `trip_minutes` fields. Recon during architecture-chat revealed the gap: `offer_history` schema has them as `smallint`, `_project_offers` SQL already touched them inline for GC-window math, but the `Offer` dataclass surfaced only `pickup_miles` / `trip_miles` (gate-layer additions from 2026-05-06).

This sprint extends `Offer` mirroring the gate-layer pattern exactly: two `Optional[int] = None` fields appended to the dataclass, two `int(...) if not None else None` kwargs added to the `Offer(...)` construction in `_project_offers`, two columns added to that function's SELECT clause. Zero existing constructors break (8 production callers + 1 test fixture all use keyword args). Test-only `FakeOffer` callers (~18 in tests/) are unaffected — separate fixture class.

If Phase 2 finds anywhere that constructs `Offer(...)` and needs minutes context, the pattern is established. If you find a place that still reads `pickup_minutes` from `offer_history` rows directly (e.g., `scripts/replay_pudo.py`, `scripts/harvest_ride.py`), leave it alone — those are out-of-scope script-level reads from raw DB rows, not `Offer` instances.

---

## Your scope (Phase 2)

Six files to author/touch. None of them require you to make architectural decisions. Every contract you need is locked in this doc or in the Phase 1 commits.

1. **`escape_detection.py`** — new module, exit velocity primitive
2. **`decisions/router.py`** — wire `tad.compute_offer_expectations()` into offer receipt path
3. **`decisions/logger.py`** — extend `offer_history` INSERT with new `expected_*` columns
4. **`driver_heartbeat.py`** — wire `escape_detection.check_exit_velocity()` into post-pickup flow
5. **`poi_service.py`** — bump `API_SEARCH_RADIUS_M` from 50 to 150
6. **`tmp/validate_phase_2c_2.py`** — validation harness with 2026-05-07 shift scenarios + 2026-05-06 audit cases

Plus tests for each. Plus the deploy.

---

## Architectural reconciliation (the locked rules)

These are the rulings from the 2026-05-07 reconciliation. They are non-negotiable for this sprint.

### Rule 1: TAD is a candidate filter, not a sibling to spatial logic

The kickoff implied TAD runs alongside spatial logic and feeds combined confidence. **It does not.** TAD is a bouncer at the door — it filters the candidate list inside `evaluate()` between Step 4 (Map: candidate generation) and Step 5 (Dispatch: per-class matcher). Candidates that fail TAD never get scored. No API calls, no spatial work, no forensic spam — just a logged TAD-fail entry per dropped candidate.

Phase 1 already integrated this. Phase 2 does not modify `evaluate()`.

### Rule 2: Head 4 is a witness signal, not a weighted signal

The existing `_CONFIDENCE_WEIGHTS` table has 7 signals per class, math-checked by Gemini to sum to 1.0:

```python
_CONFIDENCE_WEIGHTS = {
    "intersection": {
        "proximity": 0.10, "breadcrumb_match": 0.30, "cluster_tightness": 0.15,
        "cluster_duration": 0.10, "on_target_road": 0.20, "off_wire_pivot": 0.05,
        "adjacent_road_match": 0.10,
    },
    "single_road":       { ... },  # 7 signals, sum 1.0
    "number_on_street":  { ... },  # 7 signals, sum 1.0
    "apartment_complex": { ... },  # 7 signals, sum 1.0
}
```

`_signal_poi_match` (existing, from patch 2b) is NOT in this table. It populates `MatchOutcome.poi_match` and `poi_match_witness` as a separate witness signal outside the weighted sum.

`_signal_poi_type_match` (Head 4, Phase 1 work) follows the same pattern: NOT in `_CONFIDENCE_WEIGHTS`. Populates `MatchOutcome.poi_type_match` (boolean) and `MatchOutcome.poi_type_witness` (string description).

**You will not modify `_CONFIDENCE_WEIGHTS` in Phase 2. If you think you need to, you don't.**

### Rule 3: Commit threshold is the elevator rule

```
COMMIT if:
    weighted_confidence >= 0.90
    OR (weighted_confidence >= 0.80 AND poi_type_match is TRUE)
ELSE skip and log forensically.
```

Phase 1 implemented this in `evaluate()`. Phase 2 does not modify it.

### Rule 4: Best-offer-match already exists

The kickoff's "best-offer-match refactor" was based on a stale mental model. WAI's `evaluate()` already does Map-Reduce: candidates from all queue offers are scored, filtered by threshold, and tied at max. Phase 1 left this Reduce step alone (only modified the threshold to use the elevator rule). Phase 2 does not refactor it.

### Rule 5: Forensic logging uses a JSONB blob

`pudo_decision_context` got one new column in Phase 1: `tad_decision_context jsonb`. This mirrors the existing `odometer_gate_result jsonb` pattern. All TAD verdict data, time signals, distance completion percentages, and per-offer pass/fail reasons go in this blob.

**Do not add flat columns for TAD diagnostic data.** If you want to capture a new field, add it to the JSONB structure.

### Rule 6: Unit discipline (miles vs. meters)

This sprint crosses the miles/meters boundary in two modules. Get this wrong and tests may pass while production silently fails.

**Miles (numeric):**
- `offer_history.pickup_miles`, `trip_miles` — fields from Uber offer
- `offer_history.miles_at_offer_receipt` — cumulative odometer at receipt
- `offer_history.cumulative_miles_at_pickup_fire`, `cumulative_miles_at_dropoff_fire` — cumulative odometer at PUDO commits
- `offer_history.expected_pickup_distance`, `expected_dropoff_distance` — Phase 1 TAD outputs (cumulative miles)
- `offer_history.pickup_exit_odometer` — cumulative miles at exit velocity detection
- `Heartbeat.cumulative_miles` — odometer source for runtime calculations

**Meters:**
- `EXIT_VELOCITY_DISTANCE_M = 150` (escape_detection.py)
- `where_am_i.haversine_meters()` returns
- `*_RADIUS_M` constants in where_am_i.py

**Critical pairing in `escape_detection.py`:** the distance check uses meters (`haversine_meters(heartbeat_lat, heartbeat_lng, centroid_lat, centroid_lng) > EXIT_VELOCITY_DISTANCE_M`). The exit_odometer write uses miles (`heartbeat.cumulative_miles`). Both are correct; do not normalize one to the other. The test suite must include at least one case that would fail if these were swapped (e.g., a heartbeat at 0.1 miles cumulative odometer that's 200m from centroid — exit detected, exit_odometer=0.1; a swap would either compare 0.1 to 150 and never fire, or compare 200 to 150 and fire on the first heartbeat regardless of cumulative state).

**No unit conversions in this sprint.** Miles stays miles. Meters stays meters. They never multiply or compare to each other. If you find yourself writing `* 1609.34` or `/ 1609.34` somewhere, stop — the architecture doesn't need that conversion in Phase 2c.2.

---

## Recon evidence (so you don't have to re-derive)

This section inlines what architecture-chat recon found in production code as of `f3f5dc9`. If the code changed between then and now, your recon takes precedence — but flag the divergence in your sprint completion report.

### `where_am_i.py` shape (1391 lines)

**Top-level constants you'll see:**
- `MIN_REPORT_THRESHOLD = 0.4`
- `STRONG_MATCH_CONFIDENCE = 0.7`
- `INTERSECTION_RADIUS_M = 250.0`
- `SINGLE_ROAD_RADIUS_M = 500.0`
- `NUMBER_ON_STREET_RADIUS_M = 50.0`
- `APARTMENT_RADIUS_M = 300.0`
- `POI_RADIUS_M = 100.0` (patch 2a, used by `_signal_poi_match`)
- After Phase 1: `CLASS_TO_TYPE_MAP` constant added by `_signal_poi_type_match` work.

**The 7 weighted signals (`_signal_*` functions):**
1. `_signal_proximity(cluster, target_lat, target_lng, threshold_m) -> float`
2. `_signal_breadcrumb_match(...) -> float`
3. `_signal_cluster_tightness(cluster) -> float`
4. `_signal_cluster_duration(cluster) -> float`
5. `_signal_on_target_road(...) -> float`
6. `_signal_off_wire_pivot(on_wire, last_named_road, off_wire_duration_s, target_road_names) -> float`
7. `_signal_adjacent_road_match(adjacent_roads, target_road_names, current_road_class) -> float` — has the transit-gate from Operation Strip Mall

**Witness signals (NOT in weights table):**
- `_signal_poi_match(pois, target_address) -> tuple[float, Optional[str]]` — existing, 3 heads (fuzzy / branded / airport)
- `_signal_poi_type_match(pois, address_class) -> tuple[bool, Optional[str]]` — Phase 1, Head 4

**`MatchOutcome` dataclass fields (Phase 1 extends it):**
```python
matched: bool
confidence: float
corrected_lat: Optional[float]
corrected_lng: Optional[float]
reason: str
pudo_type: Optional[str]
target_address: Optional[str]
signals: Optional[dict[str, float]]
poi_match: Optional[float] = None         # patch 2a/2b
poi_witness: Optional[str] = None         # patch 2a/2b
poi_type_match: Optional[bool] = None     # Phase 1 (Head 4)
poi_type_witness: Optional[str] = None    # Phase 1 (Head 4)
```

**Class dispatch:**
```python
_CLASS_DISPATCH = {
    "intersection":      _match_intersection,
    "single_road":       _match_single_road,
    "number_on_street":  _match_number_on_street,
    "apartment_complex": _match_apartment_complex,
    "poi":               _match_poi_stub,
}
```

These string keys are what `CLASS_TO_TYPE_MAP` uses.

**`evaluate()` shape (the orchestrator):**
- Step 1: Cluster check
- Step 2: Topology (single `pivot_context` call)
- Step 3: Stop context (Stop Atlas v1.1 stub)
- Step 3.5: Memory Eye (cluster revisit check)
- Step 4 (Map): generate (target, location_type, offer_id) candidates from queue
- **Step 4.5 (TAD bouncer, Phase 1): filter candidates by `tad.evaluate_tad_gate()`**
- Step 5 (Evaluate): per-class matcher dispatch on surviving candidates
- Step 6 (Reduce): elevator rule applied (`>=0.90 OR (>=0.80 AND poi_type_match)`), tie at max

### `offer_history` schema (post-Phase 1)

Pre-existing columns relevant to this sprint:
- `id bigint PK`
- `created_at timestamptz default now()` — offer receipt timestamp
- `pickup_miles numeric`, `pickup_minutes smallint`
- `trip_miles numeric`, `trip_minutes smallint`
- `actual_pickup_at timestamptz`, `actual_dropoff_at timestamptz`
- `cumulative_miles_at_pickup_fire numeric` — distance anchor at pickup commit
- `cumulative_miles_at_dropoff_fire numeric`
- `miles_at_offer_receipt numeric`, `lat_at_offer_receipt double precision`, `lng_at_offer_receipt double precision` (gate-layer sprint, present at sprint start)

Phase 1 added:
- `expected_pickup_arrival_time timestamptz`
- `expected_pickup_distance numeric`
- `expected_dropoff_arrival_time timestamptz`
- `expected_dropoff_distance numeric`
- `pickup_exit_time timestamptz`
- `pickup_exit_odometer numeric`
- `exit_velocity_timeout boolean default false`

### `pudo_decision_context` schema (post-Phase 1)

Phase 1 added:
- `tad_decision_context jsonb` — single forensic blob

---

## Phase 2 spec (your work)

### Module 1: `escape_detection.py` (new)

**Purpose:** Detect when driver has truly *left* the pickup cluster, anchoring the next leg's TAD math correctly.

**Constants:**
```python
EXIT_VELOCITY_DISTANCE_M = 150        # primary signal
EXIT_VELOCITY_SPEED_MPH = 15          # secondary signal
EXIT_VELOCITY_SPEED_DURATION_S = 30   # sustained for this long
EXIT_VELOCITY_TIMEOUT_S = 30 * 60     # 30 minutes
```

**Public function:**
```python
def check_exit_velocity(
    pickup_centroid: tuple[float, float],
    pickup_confirmed_at: datetime,
    recent_heartbeats: list[Heartbeat],
) -> ExitVelocityResult
```

Return type:
```python
@dataclass(frozen=True)
class ExitVelocityResult:
    detected: bool
    exit_time: Optional[datetime]
    exit_odometer: Optional[float]
    timeout: bool
```

**Detection logic:**
```
detected = TRUE if EITHER:
    distance_from_centroid_m > 150  (most recent heartbeat)
  OR
    sustained speed > 15 mph for last 30s of heartbeats
```

When `detected = True`:
- `exit_time` = timestamp of the heartbeat that triggered detection
- `exit_odometer` = `cumulative_miles` of that heartbeat
- `timeout` = False

When time since `pickup_confirmed_at` exceeds `EXIT_VELOCITY_TIMEOUT_S` and detection has NOT fired:
- `detected` = False
- `exit_time` = None
- `exit_odometer` = None
- `timeout` = True

When neither condition met (still within timeout window, no exit detected):
- All None / False / False

**Sticky semantics:** Caller must check before invoking. Once an offer has `pickup_exit_time IS NOT NULL` OR `exit_velocity_timeout = TRUE`, do not re-evaluate.

**Heartbeat schema:** Confirm via recon what `Heartbeat` exposes. Phase 1 noted that `f3f5dc9` plumbed `speed_mph` through to motion gate — that same plumbing should feed exit velocity. If `speed_mph` is missing from `Heartbeat`, that's a recon-level surprise and should fail-stop the sprint until reconciled.

**Speed sustain check:** "Sustained speed > 15mph for 30s" means: across the heartbeats spanning the most recent 30 seconds (relative to the latest heartbeat), every heartbeat shows `speed_mph > 15`. Not average. Not minimum. Every single one. This is conservative — momentary slowdowns reset the sustain window. Avoid the false-positive case where one fast heartbeat triggers exit during a passenger-loading lurch.

**Distance check:** Most recent heartbeat's GPS position vs. `pickup_centroid` via haversine. Use `where_am_i.haversine_meters` (already exists, pure-Python, no PostGIS dependency).

**Tests (~6):**
- Distance trigger fires (heartbeat 200m from centroid)
- Distance trigger does NOT fire (heartbeat 100m from centroid)
- Speed trigger fires (3 heartbeats in last 30s, all >15mph)
- Speed trigger does NOT fire (3 heartbeats in last 30s, one at 10mph) — tests sustain semantics
- Timeout fires (35 minutes since pickup_confirmed_at, no exit detected)
- Parking garage scenario (driver inside structure with no movement for 4 minutes, then exits — detection fires on first heartbeat >150m from centroid, with correct exit_time and exit_odometer)

### Module 2: `decisions/router.py` (existing — wire offer receipt)

**At offer receipt (after offer is parsed and validated):**

```python
from tad import compute_offer_expectations

# After validating new offer N:
prev_offer = _get_most_recent_offer_in_queue(driver_id)  # may be None
expectations = compute_offer_expectations(
    new_offer=N,
    prev_offer=prev_offer,
    current_odometer=N.miles_at_offer_receipt,
    now=N.created_at,
)
# Pass expectations through to logger when persisting offer_history row
```

`compute_offer_expectations()` is in `tad.py` from Phase 1. Read its docstring and signature before integrating. Do not re-derive its math.

**Tests (~3):**
- Idle case (no prev_offer) — expectations populated from now + new_offer fields only
- Stacked case (prev_offer present, prev trip not yet expected to complete) — expectations chain correctly
- Orphan case (prev_offer present but its expected_dropoff_arrival_time < now) — expectations treat as idle, log orphan flag

### Module 3: `decisions/logger.py` (existing — extend INSERT)

The existing `offer_history` INSERT statement needs the 7 new columns added:

```sql
expected_pickup_arrival_time,
expected_pickup_distance,
expected_dropoff_arrival_time,
expected_dropoff_distance,
pickup_exit_time,
pickup_exit_odometer,
exit_velocity_timeout
```

At INSERT time (offer receipt), only the four `expected_*` columns are populated. The three exit velocity columns default to NULL/FALSE and get UPDATEd later by `driver_heartbeat.py`.

**Tests (~1-2):**
- INSERT with expectations populates the 4 expected_* columns
- INSERT without expectations (defensive path) leaves them NULL

### Module 4: `driver_heartbeat.py` (existing — wire exit velocity)

**In the post-pickup heartbeat handler:**

```python
from escape_detection import check_exit_velocity

# Only run if there's a pickup-confirmed offer and exit velocity hasn't been resolved:
current = _get_current_offer(driver_id)  # via current_offer_id in driver state
if (current
    and current.actual_pickup_at is not None
    and current.pickup_exit_time is None
    and not current.exit_velocity_timeout):
    
    result = check_exit_velocity(
        pickup_centroid=(current.actual_pickup_lat, current.actual_pickup_lng),
        pickup_confirmed_at=current.actual_pickup_at,
        recent_heartbeats=_get_recent_heartbeats(driver_id, lookback_s=60),
    )
    
    if result.detected:
        _update_offer_exit_velocity(
            offer_id=current.id,
            exit_time=result.exit_time,
            exit_odometer=result.exit_odometer,
        )
    elif result.timeout:
        _set_offer_exit_velocity_timeout(offer_id=current.id)
```

The `_get_recent_heartbeats` lookback should be 60 seconds — enough to evaluate the 30s sustain window with margin. Adjust if recon shows the heartbeat schema makes 60s impractical.

**Tests (~2-3):**
- Heartbeat triggers exit detection, persists pickup_exit_time and pickup_exit_odometer
- Heartbeat does not trigger detection within first few minutes, leaves columns NULL
- Heartbeat after 30+ minutes of no detection sets exit_velocity_timeout=TRUE

### Module 5: `poi_service.py` (existing — constant bump)

One-line change:

```python
# Before:
API_SEARCH_RADIUS_M = 50

# After:
API_SEARCH_RADIUS_M = 150
```

**Justification:** Audit-validated from 2026-05-06 audit data. 50m was missing legitimate POI matches at strip mall plazas where the cluster centroid sits in a parking lot offset from the storefronts. 150m captures the realistic offset envelope without bleeding into adjacent properties.

**Tests:** None required — constant only. Existing tests should pass; if any depend on the 50m value, update them and document why.

### Module 6: `tmp/validate_phase_2c_2.py` (new — validation harness)

**Purpose:** End-to-end validation against real-world scenarios. Modeled on `validate_gate_layer.py` from the gate-layer sprint.

**Scenarios required:**

**A. 2026-05-07 shift data (8 rides).** For each ride, the harness loads:
- The offer's parsed fields (pickup_miles, pickup_minutes, trip_miles, trip_minutes, addresses, address_class)
- The heartbeat sequence from offer receipt through dropoff
- The expected outcome: which cluster should commit as pickup PUDO, which as dropoff PUDO, with what confidence

The harness then runs the full pipeline (TAD bouncer + signal scoring + elevator rule) and asserts the expected commit/skip outcome.

**Critical scenarios to include:**
- 7 of 8 rides where pickup completion was ≥85% at actual pickup (positive cases)
- Ride #4 (train delay, +483% time error) — TAD distance gate should pass, time signal should be 0 (outside ±50%), spatial logic should still commit if confidence ≥0.90 or elevator condition met
- Ride #8 (driver took shortcut, -12.5% trip distance) — dropoff distance gate would fail at 85%; should test whether spatial+Head-4 elevator can rescue it. If it can't, the test asserts the skip and the harness logs it as "expected miss, candidate for Phase 2g threshold tuning"

**B. 2026-05-06 audit cases:**
- Excel Dental cluster — POI cache contains `dental_clinic` type, address_class is `single_road` or `street_number` (verify which) — Head 4 should fire TRUE
- Pappasito's at 10005 FM 1960 — address_class likely `street_number` (residential-looking number) — POI is `mexican_restaurant` — Head 4 should fire TRUE if `street_number` includes `restaurant` types
- US-90 strip mall — adjacency tool fires, weighted confidence ~0.82, POI cache has restaurant/retail types — elevator rule should commit
- Houston no-zoning false-positive — residential cluster with weak/distant commercial POIs — Head 4 should fire FALSE, weighted confidence below 0.90 alone — should skip

**Pass condition:** 100% of scenarios produce expected outcome. Any failure halts deploy until reconciled.

---

## Schema migration (already applied — verification only)

Phase 1 applied `tmp/phase_2c_2_schema.sql`. Verify with:

```bash
psql -h 10.128.0.2 -U postgres -d puddlejumper -c "
SELECT column_name FROM information_schema.columns
WHERE table_schema = 'app_private'
  AND table_name = 'offer_history'
  AND column_name IN (
    'expected_pickup_arrival_time', 'expected_pickup_distance',
    'expected_dropoff_arrival_time', 'expected_dropoff_distance',
    'pickup_exit_time', 'pickup_exit_odometer', 'exit_velocity_timeout'
  );

SELECT column_name FROM information_schema.columns
WHERE table_schema = 'app_private'
  AND table_name = 'pudo_decision_context'
  AND column_name = 'tad_decision_context';
"
```

Expected: 7 + 1 = 8 rows. If any are missing, Phase 1 didn't complete — STOP, report.

---

## Acceptance criteria (sprint completion)

The sprint is complete when ALL of these hold:

### Tests
1. `pytest` green. Phase 1 added ~15-20 tests. Phase 2 adds ~10-15 more. Target ~445-465/445-465.
2. Phase 2 unit tests cover all scenarios listed in module specs above.
3. Validation harness `tmp/validate_phase_2c_2.py` passes 100%.

### Deploy
4. Single squash-merge commit on `demolition-2026-05-04`.
5. Cloud Run deploy succeeds. New revision deploys, traffic routes.
6. Smoke check via Bruno `driver_status` request returns no errors.

### Forensic verification (1 hour post-deploy)
7. New offers populate `expected_*` columns in `offer_history`.
8. Cluster evaluations populate `tad_decision_context` JSONB blob in `pudo_decision_context`.
9. No null-pointer errors in Cloud Run logs.

---

## Sprint completion report (your final deliverable)

Author `docs/PHASE_2C_2_SPRINT_REPORT.md` covering:

1. **TL;DR** (one paragraph)
2. **What landed** (file-by-file commits)
3. **Phase 1 / Phase 2 split** (which commits came from architecture-chat, which from Claude Code)
4. **Test results** (pytest counts, harness pass/fail breakdown)
5. **Deviations from this Bible** (if any) with rationale and Gemini ratification trail
6. **Pre-existing oddities flagged but NOT addressed** (don't fix outside scope)
7. **Operational state** (branch, commit SHA, deploy revision)
8. **Open decisions** (anything deferred to Phase 2g)
9. **What's left** (validation drives, forensic verification window)

The report enables the next architecture-chat session to resume cleanly. Mirror the format of the gate-layer sprint's report (location: `docs/` post-completion).

---

## Operational rules for Phase 2

These are session-protocol rules that apply throughout. From `docs/SESSION_PROTOCOL.md`:

- **Push back when direction is wrong.** Don't just comply. If you find a contract in this Bible that you believe is incorrect after recon, stop and surface it. The reconciliation was thorough but not infallible.
- **Pre-modification discipline.** Before changing any existing file, read its current state. Don't trust this Bible's recon evidence — re-verify against current `git show HEAD:where_am_i.py` (etc.).
- **L-6 corollary:** signature changes require grepping ALL callers including tests. `grep -rn "<symbol>" --include="*.py"` against the entire repo.
- **L-8:** live-PG smoke for DB-coupled code before committing. Schema drift is real even though Phase 1 just applied the migration.
- **Terminal safety:** don't paste markdown blockquote content (lines starting with `> `) to bash. Don't paste multi-line content to bash via heredoc. Use `create_file` + scp pattern for anything substantive.
- **Apply scripts** go in `~/puddlejumper-prod/tmp/` (not `/tmp/`).
- **Paired programming:** code-chat proposes → Andrew runs through Gemini → consensus → execute. This applies in Phase 2 too. The reconciliation locked architecture, not implementation details — Gemini still reviews each module before it lands.

---

## What this sprint does NOT include

- ❌ Manual nail-it endpoint changes (production constraint: Auto Nail It only)
- ❌ State machine logic (`driverState`, IN_TRIP, etc.) — fully replaced by 1-bit memory + offer_history arithmetic
- ❌ Removing existing pickup logic (T-Pivot, adjacency, intersection-class matcher) — preserved
- ❌ Tuning thresholds beyond initial defaults (Phase 2g concern)
- ❌ Building Phase 2c.3 spatial enhancements (defer to data-driven analysis post-2c.2)
- ❌ Removing the existing motion gate or odometer gate from `motion_gate.py` — both stay
- ❌ INDEX.md update (stale per architecture-chat finding 2026-05-07; separate maintenance task)
- ❌ Modifying `_CONFIDENCE_WEIGHTS` (do not, will not, must not)

---

## Cross-session resumption

If Phase 2 pauses and resumes:

1. Read this Sprint Bible first
2. Read Phase 1 commits via `git log --oneline f3f5dc9..HEAD`
3. Check Phase 1 test results: `pytest -x` should be green
4. Check schema state via the SQL in the "Schema migration" section above
5. Resume at the next unfinished module in the Phase 2 spec

---

End of Sprint Bible.