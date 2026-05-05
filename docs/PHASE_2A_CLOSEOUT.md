# Phase 2a Closeout — POI Cache Foundation

**Date:** 2026-05-05
**Commits:**
  - `bab1e13` — phase 2a: poi_cache schema + cache-only read service
  - `53ed23d` — docs: add Operation Strip Mall proposal (ratified 2026-05-04)
**Predecessor:** `e9aaded` (Phase 1B closeout doc)
**Branch:** `demolition-2026-05-04` (pushed to origin)
**Deployed:** **no Cloud Run deploy required.** Schema applied to prod
DB (`puddlejumper-api-00591-vj7` still in traffic). Service module has
zero callers in 2a; first deploy that includes it will be Phase 2b or
Phase 2d depending on slicing.
**Test floor:** 250 → 257 (7 new, 0 regressions)
**Bruno:** 17/17 baseline preserved (no production code paths touched)
**Reviewer:** Gemini (three rounds — schema, module, tests)
**Status:** ✅ Shipped. Schema in prod, code on branch, no behavior change yet.

---

## What shipped

### Schema (`migrations/2026-05-05_phase2a_poi_cache.sql`)

`CREATE TABLE app_private.poi_cache`. Atomic, idempotent, additive.

- `id bigserial PRIMARY KEY`
- `cached_at timestamptz NOT NULL DEFAULT (NOW() AT TIME ZONE 'UTC')`
- `expires_at timestamptz NOT NULL` (30-day TTL convention; Phase 2b enforces)
- `last_hit_at timestamptz` — NULL until first cache read; touched by
  `poi_service.get_pois_near_cluster` on every match, throttled 1h/row.
  Standard §V binding integrity for the analytics column. Distinct from
  `cached_at` (write time) and `expires_at` (TTL boundary).
- `query_lat`, `query_lng double precision NOT NULL`
- `query_geog geography(Point,4326)` GENERATED ALWAYS AS
  `app_private.coords_to_geography(query_lat, query_lng)` STORED.
  Wrapper IMMUTABLE per `pg_proc` inspection 2026-05-05.
- Parallel arrays: `google_place_ids text[]`, `business_names text[]`,
  `business_types text[]` — all NOT NULL.
- CHECK constraints:
  - `poi_cache_expires_after_cached`: TTL ordering
  - `poi_cache_lat_range`: lat in [-90.0, 90.0]
  - `poi_cache_lng_range`: lng in [-180.0, 180.0]
  - `poi_cache_arrays_aligned`: `cardinality(...)` matches across the
    three arrays. Uses `cardinality()` not `array_length()` because the
    latter returns NULL for empty arrays (3-valued logic permits the
    insert). Gemini's catch — load-bearing for negative-cache semantics.
- Indexes: GIST on `query_geog`, B-tree on `expires_at`. No `pkey` index
  noted because that's implicit from PRIMARY KEY.

Applied to prod with pre-apply schema snapshot at
`/tmp/poi_cache_pre_apply_schema.sql` (71284 bytes, full `app_private`
schema). Snapshot is volatile — copy to a durable location if you want
it for rollback insurance beyond this VM session. The simpler rollback
for THIS migration is `DROP TABLE app_private.poi_cache CASCADE` since
the table is brand-new and nothing references it yet.

### Code

**`poi_service.py`** — new module. +199 lines.

- `POI` frozen dataclass: `place_id`, `name`, `business_type`, `dist_m`.
  Forward-compatible with Phase 2c's `_signal_poi_match` and Phase 1B's
  `poi_top_names` forensic column.
- `get_pois_near_cluster(cluster, cur, *, radius_m=80) -> list[POI]`:
  - `ST_DWithin` radius lookup against `query_geog` using
    `app_private.coords_to_geography(median_lat, median_lng)` (Standard
    §II compliance — no raw `ST_MakePoint` anywhere).
  - TTL guard `expires_at > (NOW() AT TIME ZONE 'UTC')` (Standard §III).
  - Option C distance-weighted dedupe across in-range cache rows. Keyed
    on `place_id` (not `name`) so two genuinely-distinct businesses with
    identical names — e.g. two Shell stations on opposite sides of a
    freeway, both within 80m of an overpass cluster centroid — survive
    as separate POIs. Deviation from R2's reference SQL, ratified by
    Gemini.
  - Returns `[]` on cache miss. Phase 2b will distinguish "cache miss →
    API call" from "cache hit, zero POIs" via `poi_lookup_source` enum
    (Phase 1B opened that column).
  - Side effect: matched rows' `last_hit_at` updated to
    `NOW() AT TIME ZONE 'UTC'`, throttled to 1h/row via
    `last_hit_at IS NULL OR last_hit_at < NOW() - INTERVAL '1 hour'`.
    Throttle interval is a module constant (`TOUCH_THROTTLE_INTERVAL`)
    so future tuning has one site.
- `TYPE_CHECKING` import guards on `Cluster` and `cursor` to prevent
  circular dependencies.
- Module docstring cites Standard §I (recon-first), §II (canonical
  coords), §III (UTC), §V (binding integrity for `last_hit_at`).

### Tests (`tests/test_poi_service.py`)

7 tests, +247 lines, +7 to test floor → **257**.

- `test_empty_cache_returns_empty_list` — miss path, no UPDATE
- `test_single_fresh_row_returns_single_poi` — happy path, UPDATE fires
- `test_expired_row_filtered_by_sql_returns_empty` — validates TTL guard
  is in the SELECT (asserts SQL string content)
- `test_dedupe_keeps_closer_occurrence_of_shared_place_id` — Option C
  with two cache rows referencing the same place_id at different distances
- `test_negative_cache_row_returns_empty_but_touches_last_hit_at` —
  the contested semantic: empty arrays produce no POIs, but the row
  is still touched. Gemini ratified: a negative cache row that prevents
  API calls is a utility win and should stay warm in the Living Heatmap.
- `test_radius_parameter_bound_to_st_dwithin` — parameter binding order
  contract (lat, lng twice, then radius)
- `test_default_radius_is_80m_per_proposal_R3` — regression guard on the
  R3-locked constant

Mocking pattern: `MagicMock` cursor with `fetchall.return_value` set to
RealDictCursor-shaped dict rows. Cluster is duck-typed via `MagicMock`
to keep the test file isolated from `cluster_detection` imports.

---

## End-to-end validation evidence

### Pytest

- 257 passed, 0 failed (was 250, +7 new). 0.86s.
- New file alone: 7 passed in 0.06s.

### Migration dry-run

Applied to `poi_scratch` DB (cloned via `TEMPLATE puddlejumper`) twice
to validate idempotency. Five smoke tests confirmed:

1. Valid insert → `geog_generated=t, hit_is_null=t`
2. Misaligned arrays (2/1/1) → constraint violation (correct)
3. **Populated vs empty arrays (the cardinality fix scenario)** →
   constraint violation (correct — would have silently passed under
   `array_length()` due to NULL-permits-insert semantics)
4. All-empty arrays (negative cache) → permitted (correct)
5. ST_DWithin lookup using `app_private.coords_to_geography` →
   returned seeded row at 14.72m (geometry round-trip works)

### Production migration apply

- Pre-existence check: `f` (clean install)
- Apply: `BEGIN/CREATE TABLE/CREATE INDEX/CREATE INDEX/COMMENT/COMMENT/COMMENT/COMMIT`
- Post-apply: 10 columns, 3 indexes (`pkey` btree, `idx_poi_cache_geog`
  gist, `idx_poi_cache_expires` btree), 5 constraints (1 pkey, 4 CHECKs)
- Row count: 0

### Bruno

17/17 against `00591-vj7`. Phase 2a doesn't touch any production code
path the bouncer exercises, so this is a baseline-preservation
confirmation, not a behavior validation.

---

## Things deferred (post-2a follow-ups)

### Phase 2b: Google Places integration

The natural next sub-commit. On cache miss in `get_pois_near_cluster`,
call Google Places "Nearby Search" with 50m radius (slightly tighter
than the 80m cache lookup is intentional — prevents over-caching).
Write the response as a new `poi_cache` row with `expires_at = NOW() +
INTERVAL '30 days'`. Fail-closed: if the API errors or rate-limits,
return `[]` so the matcher just doesn't get the POI signal.

Surface: extends `poi_service.py` with the API call path. New env var
`GOOGLE_PLACES_API_KEY` in Secret Manager + Cloud Run env mount. New
unit tests for: cache-miss-then-API-success, cache-miss-then-API-error,
write path produces correctly-aligned arrays. ~2-3 hours.

### Phase 2c: matcher signal

`_signal_poi_match(pois, target_address)` in `where_am_i.py` with 80/20
business-name/street-number rapidfuzz weighting (R4). POI bucket added
to `_evaluate` matcher dispatch (currently stubbed per RFC v2 §4.7).
`_CONFIDENCE_WEIGHTS` updated per the table in proposal §2 Change 2.
~2-3 hours.

### Phase 2d: heartbeat wiring + forensic binding

`driver_heartbeat.py` calls `poi_service.get_pois_near_cluster` after
cluster detection, threads POI list into `WhereAmI.evaluate`. Bindings
into `pudo_decision_context.poi_lookup_source` /
`poi_match_score` / `poi_top_names` (columns opened in Phase 1B,
NULL until this lands). First sub-commit where production behavior
changes. ~2 hours.

### Phase 2e: dispatch confidence floor

`dispatch.py` allows `confidence >= 0.30` when `poi_match >= 0.8` (R7).
3-line change. ~30 minutes including the test.

### Phase 2f: live validation + deploy

Drive Sienna Pkwy / Bees Passage with offer in queue. Verify
`pudo_decision_context` row shows `poi_lookup_source = 'cache_hit'`
(or `'api_call'` first time) and `poi_match_score` populated. ~1 hour.

### Other deferred

- **State-machine schema cleanup** still pending from Phase 1B. The 6
  vestigial columns in `pudo_decision_context` (`primary_offer_id`,
  `wai_status`, `planner_*`) need DROP COLUMN. Not blocking 2b.
- **`current_offer_id` rename.** Andrew's "committed_offer_id" /
  "bound_offer_id" / "in_flight_offer_id" naming question. Touches
  multiple tables. Its own commit, post-2f.
- **Per-target outcome fan-out logging** (Phase 1B leftover). Currently
  only top match's outcome lands in `_log_decision_context`; the full
  `per_target_outcomes` is on `DiagnosticContext`. Future column or
  sidecar table.
- **`stop_context` logging.** Stop Atlas v1.1 stub on
  `DiagnosticContext` not yet logged anywhere.
- **Phase 2c batch warming** (R8): periodic job that scans
  `pudo_decision_context` for `poi_lookup_source = 'skipped'` clusters
  and pre-warms `poi_cache` from Google Places. Self-learning map.
  Out of Phase 2 scope; defer post-2f.
- **Branch rename.** `demolition-2026-05-04` no longer reflects
  content. Rename before merge to main.
- **Brief-pickup forensic query** (R9). Not solved by Phase 2 at all;
  awaits its own sprint.

---

## Lessons logged this sprint

### L-16: `array_length()` returns NULL on empty arrays — silent CHECK bypass

Postgres' `array_length(arr, 1)` returns `NULL` for `ARRAY[]::text[]`,
not `0`. CHECK constraints treat `NULL` as "permit." A constraint like
`array_length(a, 1) = array_length(b, 1)` with one populated and one
empty array evaluates `3 = NULL` → `NULL` → permitted. Gemini caught
this in the Phase 2a schema review.

**Mitigation rule:** for array-cardinality assertions in CHECK
constraints, always use `cardinality()` not `array_length()`.
`cardinality(ARRAY[]::text[])` returns `0`, which forces real equality
semantics.

### L-17: scp can corrupt commit message strings via markdown linkification

When pasting a long commit message that contained `tests/test_poi_service.py`
through a terminal/clipboard chain that auto-linkifies, the rendered
display showed `tests/test_poi_[service.py](http://service.py)` —
markdown link syntax bleeding into visible output. The git object
itself stored the message correctly; only the display layer was
affected. Initial scare during validation when the visual mismatch
suggested the commit was corrupted.

**Mitigation rule:** for long commit messages, after `git commit`, run
`git log -1 <SHA> --format='%B'` to see the actual stored body. Display
artifacts in terminal output are not authoritative — only `git log`
or `git cat-file` is.

### L-18: Apply scripts go in `tmp/`, durable migrations go in `migrations/`

Initial Phase 2a SQL was authored at `tmp/2026-05-05_phase2a_poi_cache.sql`
and `git add` errored on the gitignore. The convention (per repo
history): `tmp/` is for ephemeral apply scripts and patches; durable
migrations go in `migrations/` for git tracking and reproducibility.
Phase 1B's migration was at `migrations/`, which is the precedent.

**Mitigation rule:** when authoring a migration intended to commit, put
it directly in `migrations/`. Use `tmp/` only for the apply-script
wrapper if separation matters for terminal-paste safety. Phase 2a
recovered cleanly via `cp tmp/... migrations/...` before staging, but
the cleaner path is to author at the durable location from the start.

---

## Branch state

```
HEAD -> demolition-2026-05-04, origin/demolition-2026-05-04
bab1e13  phase 2a: poi_cache schema + cache-only read service     ← THIS SPRINT
53ed23d  docs: add Operation Strip Mall proposal (ratified 2026-05-04)
e9aaded  docs: phase 1b closeout
7fe4391  phase 1b: forensic restoration of pudo_decision_context
574b53e  phase 1a: transit-class adjacency gate (Operation Strip Mall)
6c702a8  (tag: pre-demolition-2026-05-04) state machine demolition plan
```

Branch pushed to origin through `bab1e13`.

---

## Next sprint: Phase 2b (Google Places integration)

Per `OPERATION_STRIP_MALL_PROPOSAL.md` §4.2 Phase 2a sub-step 2 (the
on-miss API call portion that Phase 2a deferred). Fresh-sprint scope,
~2-3 hours. Recommended in a new chat.

Locked design decisions Phase 2b inherits:
- 50m API search radius (not 80m — slightly tighter than cache lookup
  to prevent over-caching at cache-write time)
- 30-day TTL on `expires_at`
- Fail-closed on API error: return `[]`, no exception propagation
- `poi_lookup_source` enum values: `'cache_hit'`, `'api_call'`,
  `'api_error'`, `'skipped'` (per proposal §3.1 column comment)
- Negative-cache writes: when API returns zero results, write a row
  with all-empty arrays. Schema's `poi_cache_arrays_aligned`
  constraint permits cardinality 0 = 0 = 0; smoke test 4 confirmed.

Phase 2b lands the Google Places call surface. Phase 2c lands the
matcher signal. Phase 2d wires both into `driver_heartbeat.py` and
populates the Phase 1B forensic columns. Phase 2e relaxes the dispatch
floor. Phase 2f does the live drive validation and Cloud Run deploy.

The cache-only path Phase 2a built is dormant until 2b at minimum and
unobserved in production until 2d. That's the trade-off of slicing the
work this finely — clean review surface, slow time-to-live-traffic.