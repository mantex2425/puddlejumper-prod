# Phase 2b Closeout — Operation Strip Mall: Google Places v1 + JSONB Cache

**Date:** 2026-05-05
**Commits:**
  - `3bc2cf6` — phase 2b: operation strip mall — google places v1 + jsonb cache refactor
**Predecessor:** `bf25a9e` (sub-commit 1c.2 — heartbeat hot-path migration to DriverQueue API)
**Branch:** `demolition-2026-05-04` (pushed to origin)
**Deployed:** **no Cloud Run deploy required.** Schema applied to prod DB
(`puddlejumper-api-00592-5bq` still in traffic, L-19 vaccine live). Service
module has zero callers in 2b; first deploy that includes it will be Phase 2d
(heartbeat wiring) at the earliest.
**Test floor:** 291 → 302 (+11 net, 0 regressions). Phase 2a's 7 tests in
`test_poi_service.py` were rewritten to the new schema + new return shape; 11
new tests added covering v1 API path and community-cache invariants.
**Bruno:** 17/17 baseline preserved (no production code paths touched).
**Reviewer:** Gemini (iron-fist mandate ratified 2026-05-05 — V1 endpoint not
legacy, JSONB Architectural Reset, field-mask precision, negative-cache
writes, fail-closed error handling).
**Status:** ✅ Shipped. Schema in prod, code on branch, no behavior change yet.

---

## What shipped

### Schema (`migrations/2026-05-05_phase2b_poi_cache_jsonb.sql`)

**Architectural Reset** of the 2a parallel-array storage model. Atomic,
idempotent, applied to prod with zero data loss (table was empty per 2a's
no-deploy slicing convention).

DROP:
- `google_place_ids text[]`
- `business_names text[]`
- `business_types text[]`
- CHECK constraint `poi_cache_arrays_aligned` (the cardinality alignment
  constraint that depended on the parallel-array shape)

ADD:
- `places jsonb NOT NULL DEFAULT '[]'::jsonb` — single column holding the
  full Google Places v1 payload per place: `{id, displayName.text, types[],
  location}`. Empty array `[]` is the negative-cache sentinel.
- CHECK constraint `poi_cache_places_is_array`:
  `CHECK (jsonb_typeof(places) = 'array')`. Postgres lacks
  `IF NOT EXISTS` on `ADD CONSTRAINT`, so guarded with a `DO $migration$`
  block for idempotency.

Idempotency validated by re-running the migration against prod (the second
apply emitted NOTICEs, no errors).

Rollback for THIS migration is functionally cost-free: the table had zero
rows pre-migration. Re-creating the dropped columns + constraint and
back-filling from `places` jsonb is feasible but unnecessary unless we
revert the whole sprint.

### Code (`poi_service.py`)

Full rewrite, 198 → 531 lines (additional length is mostly docstrings;
logic is ~250 lines). The v1 API integration is the substantive new
surface; the cache-read path was structurally rebuilt to consume JSONB.

**Public API:**

- `POI` frozen dataclass — `place_id`, `name`, `types: list[str]` (full
  Google types array per iron-fist mandate), `lat`, `lng`, `dist_m`. The
  per-place `lat`/`lng` are new in 2b; 2a had only the cache-row level.
- `POILookupResult` frozen dataclass — `pois: list[POI]`, `source: str`.
  source ∈ {`'cache_hit'`, `'api_call'`, `'api_error'`, `'skipped'`}.
  Phase 1B opened `pudo_decision_context.poi_lookup_source`; Phase 2d
  binds this field on every code path.
- `get_pois_near_cluster(cluster, cur, *, radius_m=80) -> POILookupResult`
  — same signature as 2a but the return type changed from `list[POI]` to
  `POILookupResult`.

**v1 API integration:**

- Endpoint: `https://places.googleapis.com/v1/places:searchNearby` (POST,
  JSON body, headers carry field mask + API key). Legacy endpoint NOT
  used; iron-fist mandate explicitly rejected it.
- API search radius: 50.0m (tighter than 80m cache lookup so cache-write
  coverage stays well-bounded).
- Field mask: `places.id,places.displayName,places.types,places.location`
  via `X-Goog-FieldMask` header. v1 pricing is per-field; mask reduces
  billable size and scopes to exactly what the matcher consumes.
- API key: `GOOGLE_MAPS_API_KEY` reused from existing infrastructure
  (`puddles_brain.py`, `superpower_geo.py`). Pre-flight curl 2026-05-05
  confirmed v1 endpoint access on this key — Excel Dental round-trip
  validated against the live Google API before any code shipped.
- Timeout: 5s (matches `tools/places_tools.py` convention).
- Max results: 20 (v1 hard cap).
- Fail-closed: `requests.RequestException`, non-200 status, malformed
  JSON, or missing API key all return `POILookupResult(pois=[],
  source='api_error')` with no cache write. The system reverts to
  road-only matching for that cluster.

**Cache write:**

- Negative-cache write on zero-result API responses. Schema's
  `poi_cache_places_is_array` CHECK passes for `[]`; the write goes
  through unconditionally.
- 30-day TTL on `expires_at` per OPERATION_STRIP_MALL_PROPOSAL R5.
- Community-cache invariant: no `driver_id` column on `poi_cache`, no
  `driver_id` filter in any SQL. Cache is fleet-shared by coordinate
  proximity. One driver pays the Google tax; every driver within 80m for
  the next 30 days reads it free.

**Cache read:**

- `LEFT JOIN LATERAL jsonb_array_elements(pc.places) AS p ON TRUE`
  unnests the JSONB array per place. Cache rows with empty places yield
  one row with NULL place fields (negative-cache sentinel) — preserved
  by the LEFT JOIN so we can distinguish complete miss (zero rows) from
  negative-cache hit (rows with NULL place_id).
- Distance computed in Postgres via `app_private.distance_miles(...) *
  1609.344` per Standard §II ("trust the abstraction"). The API-fresh
  parse path uses a flat-earth approximation in Python because there's
  no DB cursor available there and accuracy at 50m radius is sub-meter,
  which is sort-order-correct (the matcher in 2c re-computes against
  driver-actual GPS anyway).
- Option C dedupe: for each unique `place_id` across all matched cache
  rows, keep the closest occurrence by `dist_m`.
- `last_hit_at` touched on matched cache rows, throttled to once per
  hour per row to suppress heartbeat write storms (Standard §V).

### Tests (`tests/test_poi_service.py`)

Full rewrite, 246 → 666 lines. **18 tests** total in this file (was 7).
Suite floor: 291 → 302 (+11 net).

**Cache-hit path (8):**
- `test_cache_hit_single_place_returns_one_poi_source_cache_hit`
- `test_cache_hit_negative_cache_only_returns_empty_pois_source_cache_hit`
  — the Negative Cache Dividend: `cache_hit` source, empty pois, UPDATE
  fires (cache row stays warm), no API call
- `test_cache_hit_touches_last_hit_at_with_throttle_interval`
- `test_cache_hit_dedupe_keeps_closer_occurrence_of_shared_place_id`
- `test_cache_lookup_default_radius_is_80m_per_proposal_R3`
- `test_cache_lookup_custom_radius_passed_to_st_dwithin`
- `test_cache_lookup_uses_canonical_geometry_functions` — SQL contains
  `app_private.distance_miles` and `app_private.coords_to_geography`,
  does NOT contain `ST_MakePoint`
- `test_cache_hit_results_sorted_ascending_by_per_place_dist_m`

**Cache-miss → API path (6):**
- `test_cache_miss_api_success_writes_row_returns_api_call`
- `test_cache_miss_api_zero_results_writes_negative_cache_row`
- `test_cache_miss_api_request_exception_returns_api_error_no_write`
- `test_cache_miss_api_500_returns_api_error_no_write`
- `test_cache_miss_api_malformed_json_returns_api_error`
- `test_cache_miss_api_key_missing_returns_api_error_no_post_call`

**API request shape (1):**
- `test_api_call_uses_50m_radius_and_field_mask_and_api_key` — body has
  50m radius at cluster centroid, headers carry `X-Goog-FieldMask` and
  `X-Goog-Api-Key`

**Community-cache invariants (2 — regression guards):**
- `test_cache_lookup_sql_has_no_driver_id_filter`
- `test_cache_write_sql_has_no_driver_id_column`

**Bonus (1):**
- `test_cache_write_uses_30_day_ttl_per_proposal_R5`

Mocking pattern: `MagicMock` cursor with `fetchall.return_value` set to
flat per-place dict rows mirroring the `LEFT JOIN LATERAL` SELECT output.
The v1 API path uses `monkeypatch.setattr("poi_service.requests.post",
fake_post)` — no real network calls. `with_api_key` fixture sets the env
var via `monkeypatch.setenv` for tests that need the API path; the missing-
key test deliberately omits the fixture to exercise the no-key short-circuit.

---

## End-to-end validation evidence

### Pytest

- 302 passed, 0 failed in 0.75s (was 291 + 11 new = 302 expected; clean).
- `test_poi_service.py` alone: 18/18 passed in 0.07s.

### Migration apply + idempotency + behavioral probe

Pre-flight:
- `poi_cache` row count = 0 ✅
- 3 columns to drop confirmed present ✅
- `poi_cache_arrays_aligned` constraint confirmed present ✅
- `places` column confirmed absent ✅

Apply: clean `BEGIN/ALTER TABLE×3/DO/COMMENT×2/COMMIT` with no errors.

Idempotency: second apply produced NOTICEs (constraint already absent,
columns already absent, places column already exists) but no errors.
Standard idempotent-migration behavior.

Behavioral probes (all wrapped in BEGIN/ROLLBACK, no permanent rows):
1. Positive insert (Excel Dental v1-shaped JSON) → succeeded, `len=1`,
   `jsonb_typeof=array` ✅
2. Negative-cache insert (`'[]'::jsonb`) → succeeded, `len=0` ✅
3. Constraint-violation insert (`'{}'::jsonb`) → ERROR with constraint
   violation message — confirms constraint actively enforces ✅

### Live curl pre-flight (the smoking gun)

Before any code shipped, the iron-fist mandate required a live
`curl -X POST` to `places.googleapis.com/v1/places:searchNearby` from the
VM, using the existing `GOOGLE_MAPS_API_KEY`, with the locked field mask.
The Sienna Pkwy / Bees Passage coordinate (29.510316, -95.527151) returned
three places:

- Shell (gas_station)
- Stomp's Burger Joint (restaurant)
- **Missouri City Dentist - Excel Dental (dentist)** ← the cluster anchor

This proved (a) the API key was hot for the v1 endpoint, (b) the field
mask returned the exact data structure the parser expected, and (c) the
"Bees Passage ghost" failure mode — where the GPS reads a side-street name
but the actual stop is at a strip-mall business — was solvable by POI
matching against this exact response shape.

### Smoke test against live response shape

Andrew ran a manual smoke test (`tmp/smoke_test_v1.py`) feeding the live
Google response into `_parse_v1_to_pois` directly:

```
[MATCH] Missouri City Dentist - Excel Dental
  ID:    ChIJUSRiYjnvQIYRp7GjiZW9MHg
  Types: dentist, point_of_interest, health, establishment
  Dist:  0.00m
```

Two notable observations from this smoke test:

1. The Shell record in the test fixture was missing a `location` field.
   The defensive parser correctly skipped it (per `_v1_place_to_poi`'s
   None-on-missing-required-fields contract) rather than crashing or
   producing a junk POI with NULL coordinates. This is the iron-fist
   "fail-closed at the field level" working as designed.
2. Distance precision at zero coordinates is exactly 0.00m, confirming
   the flat-earth distance approximation is correct in the trivial case.

### Bruno

17/17 against `puddlejumper-api-00592-5bq`. Phase 2b touches no
production code path the bouncer exercises (poi_service has zero callers
until Phase 2d), so this is a baseline-preservation confirmation.

---

## Things deferred (post-2b follow-ups)

### Phase 2c: matcher signal

Per `OPERATION_STRIP_MALL_PROPOSAL.md` §2 Change 2 and the iron-fist
mandate. Lands `_signal_poi_match(pois, target_address)` in
`where_am_i.py` with rapidfuzz 80/20 business-name/street-number
weighting (R4). POI bucket added to `_evaluate` matcher dispatch.
`_CONFIDENCE_WEIGHTS` updated. ~2-3 hours. Recommended in a fresh chat.

Locked design decisions Phase 2c inherits:
- 80% business-name fuzzy weight (rapidfuzz `partial_ratio` or `WRatio`)
- 20% street-number numeric weight
- 0.30 confidence floor when `poi_match >= 0.8` (R7) — relaxes from 0.40
- POI types array used for type-aware weighting (gas_station vs dentist
  vs restaurant signal differently in different contexts)

Phase 2c calls `get_pois_near_cluster` directly. Phase 2d wires the
heartbeat call site.

### Phase 2d: heartbeat wiring + forensic binding

`driver_heartbeat.py` calls `poi_service.get_pois_near_cluster` after
cluster detection, threads the POI list into `WhereAmI.evaluate`.
Bindings into `pudo_decision_context.poi_lookup_source` /
`poi_match_score` / `poi_top_names` (columns opened in Phase 1B, NULL
until this lands). First sub-commit where production behavior changes.
~2 hours. **No deploy until 2d.**

### Phase 2e: dispatch confidence floor

`dispatch.py` allows `confidence >= 0.30` when `poi_match >= 0.8` (R7).
3-line change. ~30 minutes including the test.

### Phase 2f: live validation + deploy

Drive Sienna Pkwy / Bees Passage with offer in queue. Verify
`pudo_decision_context` row shows `poi_lookup_source = 'cache_hit'` (or
`'api_call'` first time) and `poi_match_score` populated with Excel
Dental as the witness. ~1 hour.

### GIN index on `places` jsonb

Deferred until Phase 2c (or later) introduces a query that filters JSONB
content structurally inside Postgres (e.g., `WHERE places @>
'[{"types":["gas_station"]}]'::jsonb`). Until then, GIN is write-cost
without read-benefit. `CREATE INDEX CONCURRENTLY ... USING GIN (places
jsonb_path_ops)` works on populated tables, so deferring is operationally
free.

### `tmp/smoke_test_v1.py` cleanup

Andrew's manual smoke test against the live Google response is on disk
at `tmp/smoke_test_v1.py`. The `tmp/` directory is gitignored so it's
not in the commit. Recommendation: delete after the closeout commits;
hardcoded API smoke tests bitrot quickly. The unit tests cover parser
correctness and the curl pre-flight covered "key works against v1."

### Sub-commit 1c.3 — INDEX.md cleanup

Was queued in this session before pivoting to Phase 2. Still needed:
stale "22/61 integration" line, stale `patch-00566a-unified-refinement`
branch reference, test-floor update from 291 to 302. Trivial work,
pickup whenever convenient.

### Other deferred (carried from 1B / 2a closeouts)

- **State-machine schema cleanup** — 6 vestigial `pudo_decision_context`
  columns need DROP COLUMN. Not blocking 2c.
- **`current_offer_id` rename** to `committed_offer_id` /
  `bound_offer_id` / `in_flight_offer_id`. Multi-table touch, own commit.
- **Per-target outcome fan-out logging** — only top match's outcome lands
  in `_log_decision_context`; full `per_target_outcomes` available on
  `DiagnosticContext`. Future column or sidecar table.
- **`stop_context` logging** — Stop Atlas v1.1 stub on `DiagnosticContext`
  not yet logged anywhere.
- **Phase 2c batch warming (R8)** — periodic job that scans
  `pudo_decision_context` for `poi_lookup_source = 'skipped'` clusters
  and pre-warms `poi_cache` from Google Places. Self-learning map.
  Defer post-2f.
- **Branch rename** — `demolition-2026-05-04` no longer reflects content.
  Rename before merge to main.
- **Brief-pickup forensic query (R9)** — own sprint.

---

## Lessons logged this sprint

### L-23: Bash history-expansion eats `!` in f-strings

Inline bash command containing a Python f-string with `!r}` formatting
(e.g., `print(f"throttle={TOUCH_THROTTLE_INTERVAL!r}")`) triggers
interactive bash's history-expansion before any command runs. The shell
parses `!r}` as a history event lookup, fails with `event not found`,
and aborts the command — no Python ever executes. The error message
("event not found") is opaque about the actual cause if you don't know
the failure mode.

**Mitigation rule:** for verification commands more than 2-3 lines OR
containing any `!` followed by an identifier character, write to a
file via heredoc with single-quoted delimiter (`<<'EOF'`) then `bash
/path/to/script.sh`. The single-quoted heredoc disables variable
interpolation, command substitution, AND history expansion in the body
text, so the `!` becomes a literal character. Non-interactive bash
(`bash script.sh`) doesn't run history expansion at all.

Alternative mitigations: `set +H` before the command (disables history
expansion for the rest of the session — sticky, easy to forget); escape
`!` as `\!` (works but clutters the f-string and is easy to miss).
The heredoc-to-file pattern is most robust.

### L-24: Defensive field-level parser skip preserves heartbeat resilience

The Google Places v1 response can have records missing fields the
field-mask requested — Google's API doesn't guarantee every field is
present in every result. The parser `_v1_place_to_poi` returns None on
any missing required field (`place_id`, `name`, `lat`, `lng`); the
caller (`_parse_v1_to_pois`) filters Nones out of the POI list. This
trades "Google sometimes returns a partial record" for "the heartbeat
path never crashes on a partial record" — net win.

Validated by Andrew's smoke test against the live response: a Shell
record was missing its `location` field; the parser correctly skipped
it rather than producing a malformed POI with NULL coordinates that
would crash downstream distance math.

**Mitigation rule:** for any external-API parser (Google, Uber, third-
party), structure as "extract required fields, return None if any
missing, filter Nones at the caller." Don't crash on partial responses;
the caller decides whether the empty-result case is worth logging or
silently fail-closing on. For poi_service we silently skip — the
matcher tolerates an undersized POI list cleanly.

### L-25: Architectural Reset on zero-row tables is functionally cost-free

Phase 2a shipped a `poi_cache` schema with three parallel `text[]`
columns. Three days later, the iron-fist Operation Strip Mall mandate
required a single `places jsonb` column instead. The 2a closeout
explicitly noted "row count: 0" and "zero production callers" — both
preconditions for safely refactoring. The Phase 2b migration dropped
the three parallel columns + alignment constraint and added the JSONB
column; recovery from a botched migration would have been re-creating
the dropped columns (zero data to back-fill).

**Implication for future sprints:** the "ship something fast, refactor
when better information arrives" pattern is safer when the artifact
has zero rows + zero callers. Phase 2a's no-deploy slicing (poi_cache
table created but unused until 2b) preserved this property
intentionally. Same pattern available for future infrastructure that
benefits from staged build-out.

**Counter:** this only works if the table genuinely stays at zero rows
between sprints. Once production traffic populates a table, schema
refactors get expensive (data migration + backfill + code dual-path).
The window for cost-free reset closes when the first deploy lands that
exercises the table.

---

## Branch state

```
HEAD -> demolition-2026-05-04, origin/demolition-2026-05-04
3bc2cf6  phase 2b: operation strip mall — google places v1 + jsonb cache refactor   ← THIS SPRINT
bf25a9e  sub-commit 1c.2: heartbeat hot-path migration to DriverQueue API
69c39d1  sub-commit 1c.1.1: reconcile driver_queue GC constants to Houston Tax
582ba1a  sub-commit 1c.1: caller migration to DriverQueue API (peripherals)
afb9ff0  sub-commit 1b: drift detector for offer_ids_only / _project_offers
dfe9c36  sub-commit 1a: introduce DriverQueue module + unit tests
bab1e13  phase 2a: poi_cache schema + cache-only read service
53ed23d  docs: add Operation Strip Mall proposal (ratified 2026-05-04)
e9aaded  docs: phase 1b closeout
7fe4391  phase 1b: forensic restoration of pudo_decision_context
574b53e  phase 1a: transit-class adjacency gate (Operation Strip Mall)
6c702a8  (tag: pre-demolition-2026-05-04) state machine demolition plan
```

Branch pushed to origin through `3bc2cf6`.

---

## Next sprint: Phase 2c (the matcher signal)

Per `OPERATION_STRIP_MALL_PROPOSAL.md` §2 Change 2 and the iron-fist
mandate. Fresh-sprint scope, ~2-3 hours. **Recommended in a new chat.**

The Phase 2c session should load:
- This closeout (`docs/PHASE_2B_CLOSEOUT.md`)
- `OPERATION_STRIP_MALL_PROPOSAL.md` (the ratified design)
- `where_am_i.py` (matcher dispatch home)
- `poi_service.py` (the data source — already shipped here)
- `INDEX.md` updated for 1c.3 if that's done first

Locked design decisions Phase 2c inherits:
- POI bucket plugged into `_evaluate` dispatch (currently stubbed per
  RFC v2 §4.7)
- rapidfuzz 80/20 business-name/street-number weighting
- 0.30 confidence floor when `poi_match >= 0.8`
- Type-aware weighting using full `POI.types` list
- Returns `MatchOutcome` shape per Phase 1B §s10 A8 (DiagnosticContext
  encapsulation)

Phase 2c calls `get_pois_near_cluster` directly (returns POILookupResult
now, not list[POI] — Phase 2c's matcher uses `result.pois` and forwards
`result.source` to the heartbeat caller for forensic logging in 2d).

The cache + API surface 2b built is dormant until 2d. That's the trade-
off of slicing the work this finely — clean review surface, slow
time-to-live-traffic. The compounding payoff is in 2f when Excel Dental
shows up in `pudo_decision_context.poi_top_names` for the first real
heartbeat.