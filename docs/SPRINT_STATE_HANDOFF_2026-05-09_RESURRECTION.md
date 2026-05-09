# Phase 2c.2 Sprint — State Handoff (2026-05-09 Resurrection Session)

**Branch:** `phase-2c-2-tad-exit-4tools` @ commit `fb56175` (push to origin: confirmed)
**Production deploy:** `puddlejumper-api-00597-hcd`, 100% traffic, deployed 2026-05-09 ~15:38 UTC
**Pytest floor:** 557/557
**Session arc:** Item 8 shipped → Item 4 architecture cascade discovery → POI call-contract bug found and fixed → deploy → privilege grant cascade → forensic wiring gaps surfaced → architecture work shelved → real-drive validation → TAD anchor GC defect discovered

---

## Read order at session start

1. **This document** — operative state
2. `docs/PHASE_2C_2_SPRINT_BIBLE.md` — original spec. Module 5 (radius bump) is now superseded by this handoff's "shelved" decision; Modules 1, 6 not yet shipped.
3. `docs/SESSION_PROTOCOL.md` — paired-programming workflow, paste hazards, L-3
4. `docs/SPRINT_STATE_HANDOFF_2026-05-09.md` — prior handoff (Item 3b validation, post-Bruno cumulativeMiles wiring). Tonight superseded its expected next-step (Item 4 radius bump) by surfacing two cascading defects.

---

## Recon at session start

```bash
cd ~/puddlejumper-prod && git log --oneline f3f5dc9..HEAD | head -10
source venv/bin/activate && python3 -m pytest --tb=short -q 2>&1 | tail -5
```

Expected: top commit `fb56175` (resurrection patch). Pytest **557/557 passing**.

---

## What landed this session

### Item 8 — `cumulative_miles` trace_data alias (commit `9db7d0d`)

Single-line forensic alias in `decisions/logger.py:31`, mirroring `gps_age_sec` pattern. Recon revealed the `gpsAgeSec` TODO from prior memory was already complete (wiring landed in earlier work; only the memory note was stale, now removed). Tests +3 (552 → 555 floor). **Validated end-to-end in production tonight** — `decision_log.trace_data->>'cumulative_miles'` populated with real values from real Android payloads (e.g. offer 7787, `cumulative_miles_logged = 11.5318`).

### Resurrection patch — POI call-contract argument swap fix (commit `fb56175`)

`where_am_i.py:1630` was calling `get_pois_near_cluster(self.cur, cluster)` against signature `(cluster, cur, *, radius_m=...)` — arguments swapped. The frozen `Cluster` dataclass has no `.execute` attribute, so `_read_cache`'s first `cur.execute(...)` call (which was actually `cluster.execute(...)`) raised `AttributeError`. The `try/except Exception` at `where_am_i.py:1632` swallowed the error silently.

**Production impact:** 69,944 PUDO contexts in 7 days written with empty POI witnesses. `_signal_poi_match` and `_signal_poi_type_match` (Patch 2c + Phase 1 Head 4) received empty lists for 7+ days. The Bible's elevator commit rule (`weighted >= 0.80 AND poi_type_match TRUE`) **never fired in production**.

**Provenance:** `tmp/patch_item_3_evaluate_dual_commit.py:296` and `:436` contain the same wrong argument order — the bug entered via that patch script not grepping `poi_service.py`'s actual signature before authoring. Exact L-6 corollary failure.

**Fix shipped:**
- `where_am_i.py:1630` — argument order corrected
- `where_am_i.py:38` — `_first_poi_success_logged: bool = False` module flag
- `where_am_i.py:1632` — one-shot `[RESURRECTION]` info log on first non-empty POI return per process
- `tests/test_poi_service.py` +3 contract regression tests using REAL `Cluster` import (not the file's MagicMock fixture, which would silently pass under both argument orders since MagicMock returns Mock for any attribute access)

Tests +2 (555 → 557 floor).

### Sequence grant fix (DDL, applied directly via psql)

```sql
GRANT SELECT, USAGE ON SEQUENCE app_private.poi_cache_id_seq TO atjb;
```

When deploy went live, Cloud Run logs immediately surfaced cascading errors:
```
psycopg2.errors.InsufficientPrivilege: permission denied for sequence poi_cache_id_seq
psycopg2.errors.InFailedSqlTransaction: current transaction is aborted, commands ignored until end of transaction block
```

The cache table itself had grants (`atjb=arwd/postgres`); the underlying SERIAL sequence was bare (`(none)` in pg_class.relacl). Pattern check confirmed one-off miss: every other sequence in `app_private` has `atjb=rU/postgres`. Whoever added `poi_cache` granted on the table but missed the sequence. Because the call-contract bug meant `_write_cache` was unreachable, the missing grant was invisible until tonight's resurrection.

**Verified post-fix:** `poi_cache` row 4 wrote successfully at 15:39:38 UTC. Privilege errors ceased within 60 seconds (containers caught up after grant).

**Note:** This DDL was applied directly to production. It is not in the repo. Future schema migration scripts should grant on sequence creation.

---

## What's validated end-to-end in production

| Component | Status | Evidence |
|---|---|---|
| Item 8 `cumulative_miles` → trace_data | ✅ Working | `decision_log.trace_data->>'cumulative_miles'` populated on real offers |
| Item 8 → `offer_history.miles_at_offer_receipt` | ✅ Working | 7787 etc. show 11.53 matching trace_data |
| Item 3b.W `expected_*` anchor writes | ✅ Working | Every offer has expected_pickup_distance and expected_dropoff_distance |
| Resurrection POI lookup call | ✅ Alive | `poi_cache` rows 4 and 5 written from real cluster centroids |
| Cache cost-control | ✅ Working | Multiple heartbeats at same cluster don't write duplicate rows |
| Privilege chain | ✅ Resolved | Zero `InsufficientPrivilege` errors post-grant |

## What is NOT working in production

### Auto-nailer has not fired on a real ride in ≥7 days

`offer_history.actual_pickup_at` and `actual_dropoff_at` are NULL on every recent row. **No PUDO commits, no Price Radar seeds, no validation surface for WAI confidence in production.** This is a much bigger sprint-impact finding than the resurrection itself.

Evidence from tonight's drive (1 trip, accepted offer 7780/7781 Davenport Pkwy → County Road 79):
- All 10 most recent `offer_history` rows: `actual_pickup_at = NULL, actual_dropoff_at = NULL`
- 2 ACCEPT verdicts (8404, 8405, 8408) followed by no commits
- Trip completed in real life (driver returned home) but no commit fires recorded

The resurrection patch unblocks one failure mode (POI signal). The PUDO commit failure is a *separate* defect: see "TAD anchor GC defect" below.

### TAD anchor GC defect — root cause of no commits

**Discovery from tonight's drive data.** The `expected_pickup_distance` values on consecutive offers accumulated unboundedly:

```
Offer 7780 (Davenport, ACCEPT): expected_pickup_distance = 19.57   (correct: 11.32 + 8.25)
Offer 7781 (Davenport, ACCEPT): expected_pickup_distance = 157.4   (wrong)
Offer 7782 (Poets Corner, DECLINE): expected_pickup_distance = 164.97
Offer 7783 (Poets Corner, DECLINE): expected_pickup_distance = 183.97
Offer 7784 (Olive Stone, DECLINE): expected_pickup_distance = 205.28
Offer 7785 (Dovetail Canyon, DECLINE): expected_pickup_distance = 254.37
Offer 7786 (Floral Bloom, DECLINE): expected_pickup_distance = 279.25
Offer 7787 (Floral Bloom, DECLINE): expected_pickup_distance = 283.93
```

Driver's actual `cumulative_miles ≈ 11.5`. TAD bouncer compares against `expected_pickup_distance` in 85-115% window. **The window for offer 7787 is 241-326 miles. Driver's actual position is 11.5 miles. Window cannot possibly hit. No commit fires.**

**The broken filter** at `decisions/logger.py:99-100`:

```sql
WHERE dl.driver_id = %s
  AND oh.expected_dropoff_arrival_time IS NOT NULL
ORDER BY oh.created_at DESC
LIMIT 1
```

This picks the most-recent offer regardless of disposition. Every new offer becomes "stacked" onto the previous one's dropoff anchor — including DECLINED offers, including ACCEPTED offers whose ETAs have long passed, including completed rides from yesterday. The accumulation visible in tonight's data is the symptom; the missing GC filter is the cause.

**Andrew's three-rule clarification (the design):**

1. **Declined offers stay in queue.** A declined offer is data — the driver may drive toward that pickup anyway, the dropoff is an implicit target signal, Price Radar uses it. **But declined offers should NOT anchor TAD math for future offers.**
2. **Accepted offers whose ETA has passed without commit are GC'd.** If `expected_pickup_arrival_time < NOW() AND actual_pickup_at IS NULL`, the accept is considered abandoned. Same logic for dropoff. Such offers should NOT anchor future TAD math.
3. **Long-completed offers don't anchor new offers.** `actual_dropoff_at IS NOT NULL` excludes the row from prev_offer consideration.

**Proposed fix shape** (needs Gemini ratification before authoring):

```sql
WHERE dl.driver_id = %s
  AND dl.decision_result->>'verdict' = 'ACCEPT'
  AND oh.actual_dropoff_at IS NULL
  AND (
    (oh.actual_pickup_at IS NULL AND oh.expected_pickup_arrival_time > NOW())
    OR (oh.actual_pickup_at IS NOT NULL AND oh.expected_dropoff_arrival_time > NOW())
  )
ORDER BY oh.created_at DESC
LIMIT 1
```

Produces correct behavior for all four cases: idle, stacked-pre-pickup, stacked-mid-ride, expired-accept.

**Self-unblocking property:** Once this fix lands, idle-case `expected_*` distances become sane. TAD windows match real cumulative_miles. Auto-nailer fires when driver actually arrives at pickup. `actual_pickup_at` populates. Future offers correctly chain when stacked.

### Forensic wiring gaps (Phase 2 placeholders)

Several `pudo_decision_context` columns are hardcoded `None` in production code:

- `driver_heartbeat.py:731` — `None,  # poi_lookup_source (Phase 2)` — this is the column we'd most want for resurrection validation
- `wai_status` and the rest of the `wai_*` family (`wai_pudo_type`, `wai_offer_id`, `wai_target_address`, `wai_confidence`, `wai_reason`) — read in `driver_status.py:111` but **no production writer exists**

The columns were created (motion gate sprint, schema migration applied) but the populating code was deferred. Without these populated, we can't observe `poi_type_match` going TRUE on real production clusters — the elevator branch validation criterion is currently unobservable.

**Logged for next-session work** as forensic wiring scope.

---

## Architecture work shelved (per ratified plan)

Designs ratified by Gemini but deferred until production data justifies the schema work:

### Cache-coordination defect (`poi_cache.query_radius_m` schema column)

`_read_cache` filters on `ST_DWithin(query_geog, current_centroid, radius_m=80)` regardless of what radius the original API call covered. Cross-driver reuse is gated by 80m centroid proximity, not by API discovery overlap. Cache row written by a 150m API call doesn't carry geographic-coverage metadata.

**Ratified design:** Add `poi_cache.query_radius_m`, backfill historical at 50m (the API radius they were written under), update `_write_cache` to record `API_SEARCH_RADIUS_M`, update `_read_cache` to filter by `pc.query_radius_m`. Remove vestigial `radius_m` parameter from `get_pois_near_cluster`.

### API failure cooldown (`poi_cache.status_code` + dual-TTL)

Without cooldown, transient Google failures cause per-heartbeat retry storms. Pre-resurrection production was never affected (function failed before reaching API). Post-resurrection, the storm risk is real if Google has an outage.

**Ratified design:** Add `poi_cache.status_code` smallint. Backfill 200 (assume historical successes). On API failure (5xx/timeout), write a cache row with `status_code != 200` and `expires_at = NOW() + 60 seconds`. `_read_cache` returns new `POILookupResult(source='api_cooldown')` for cooldown rows; calling code sees the cooldown signal and skips the API.

### Constant bump (`API_SEARCH_RADIUS_M`: 50 → 150)

Bible Module 5 spec. Justified by 2026-05-06 audit data (Excel Dental, Pappasito's, US-90 strip mall — POIs missed at 50m). **Now contingent on production-drive observation post-resurrection.** Cannot validate radius adequacy until POI lookup runs against real drive clusters.

### Re-trigger conditions for unshelving

The shelved designs come off the shelf if any of these surface:

1. **Cache hit rate observed degraded** — multiple drivers in same area paying Google repeatedly because cluster centroids are within API discovery overlap but outside cache-lookup window. Diagnostic: query `poi_cache` post-multiple-drives, see if drivers in the same neighborhood produce duplicate rows. If yes → cache-coordination fix.
2. **Google API outage observed** producing per-heartbeat retry pattern in Cloud Run logs. → cooldown fix.
3. **POI matching observed missing** legitimate audit cases (Excel Dental etc.) at production drive locations. → radius bump.

---

## Validation queries (for next session, after first PUDO commit fires)

After the TAD anchor GC fix lands and a real drive produces `actual_pickup_at IS NOT NULL`:

```bash
psql -h 10.128.0.2 -U postgres -d puddlejumper > /tmp/post_fix_validation.txt 2>&1 <<'EOF'
\echo '=== Did auto-nailer fire? ==='
SELECT id, created_at, pickup_address, dropoff_address,
       actual_pickup_at, actual_dropoff_at,
       cumulative_miles_at_pickup_fire, cumulative_miles_at_dropoff_fire
FROM app_private.offer_history
WHERE created_at > '<deploy timestamp>'
  AND (actual_pickup_at IS NOT NULL OR actual_dropoff_at IS NOT NULL)
ORDER BY created_at DESC
LIMIT 5;

\echo ''
\echo '=== Sane expected_* anchors post-GC-fix ==='
SELECT id, miles_at_offer_receipt, pickup_miles, expected_pickup_distance,
       (expected_pickup_distance - miles_at_offer_receipt - pickup_miles) AS expected_anchor_drift
FROM app_private.offer_history
WHERE created_at > '<deploy timestamp>'
ORDER BY created_at DESC
LIMIT 10;

\echo ''
\echo '=== poi_cache growth across drive locations ==='
SELECT id, cached_at, query_lat, query_lng,
       jsonb_array_length(places) AS poi_count
FROM app_private.poi_cache
ORDER BY cached_at DESC LIMIT 10;
EOF
cat /tmp/post_fix_validation.txt
```

```bash
echo "=== RESURRECTION log fires from drive ==="
gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="puddlejumper-api" AND textPayload:"RESURRECTION"' --limit=5 --format="value(timestamp,textPayload)" --freshness=2h 2>&1
```

`expected_anchor_drift` should be ~0 for idle-case offers (true math: anchor = receipt + pickup_miles). Non-zero only for genuinely stacked offers.

---

## Sprint completion criterion (updated)

Original Bible criterion: pytest green + Cloud Run deploy succeeds + `expected_*` columns populate on new offers.

**Updated criterion (this handoff):** All of the above PLUS production observation of:

1. **Auto-nailer fires** on a real drive (`actual_pickup_at IS NOT NULL` on at least one post-deploy `offer_history` row)
2. **`[RESURRECTION]` log emits** at least once on a non-empty POI return
3. **Elevator commit rule observed** firing on real cluster (requires forensic wiring fix to be observable; or in absence of that, indirect proof via successful pickup commit at a strip-mall-class location)

The sprint cannot claim sensors-are-wired without observing #1. Item 3b.W's `expected_*` writes don't count as sensor wiring if the anchors they produce are unusable for TAD evaluation.

---

## Next session priority order

1. **Ratify TAD anchor GC design** — Gemini conversation. Confirm three-rule semantics, edge cases (gap between accept and pickup-fire, what counts as expired, etc.). Land design.
2. **Production data audit** — query last week's `offer_history` and identify how many rows would be different prev_offers under old vs new logic. Sizes the impact and validates the design against real data.
3. **Apply prev_offer SELECT fix** in `decisions/logger.py:88-105`. Tests +3-5 (idle, stacked-pre-pickup, stacked-mid-ride, expired-accept cases). Apply script + L-3 envelope per protocol.
4. **Real-drive validation** — confirm auto-nailer fires post-fix. This is the moment Phase 2c.2 can claim sensor wiring is alive.
5. **Forensic wiring fix** — populate `pudo_decision_context.poi_lookup_source`, `poi_match_score`, `poi_top_names` from `WhereAmI.evaluate()` outputs. Wire `wai_status`, `wai_pudo_type`, `wai_offer_id`, `wai_target_address`, `wai_confidence`, `wai_reason`. This unblocks elevator-branch observation and broader Phase 2c.2 validation.
6. **Decide on shelved architecture work** — with real drive data in hand, evaluate cache-coordination, cooldown, and radius-bump triggers.

---

## Operational reminders

- **Test floor: 557/557.** Every change must verify against full pytest run.
- **L-8 lesson, internalized this session:** `\d <table>` before any SQL referencing columns. Three column-assumption errors tonight (`heartbeat_log.cumulative_miles`, `pudo_decision_context.cluster_n`, `poi_cache.created_at`). Pre-modification discipline applies to schema work, not just code edits.
- **L-3 envelope discipline:** every apply script in `~/puddlejumper-prod/tmp/`, never `/tmp/`.
- **Paste hazards (L-22):** chat-display linkification of `[name.py](http://name.py)`. File transfer via `create_file` → `present_files` → Andrew's local-then-scp pattern.
- **Bruno:** PUDO Simulation collection found tonight, but `01 Reset (before)` returns `403 Forbidden: "test endpoints not enabled for this driver"` for the production driver_id. Test endpoints gated by feature flag. Real driving validation chosen over enabling the flag.
- **Paired programming:** Claude proposes → Gemini reviews → consensus → Andrew runs. Tonight's session caught two design rationalizations on Claude's part (deferring API cooldown, deferring cache coordination) — Andrew's pushback was diagnostic. Pattern continues.

---

## Useful artifacts from this session

**Cloud Run deploy:** revision `puddlejumper-api-00597-hcd`, deployed 2026-05-09 ~15:38 UTC

**poi_cache rows seeded tonight:**
- ID 4: home cluster `(29.5064, -95.5021)`, `poi_count=0` (negative cache, residential)
- ID 5: home cluster `(29.5070, -95.5026)`, `poi_count=0` (negative cache, residential, slightly different centroid)

**Drive offers (selected):**
- 7780/7781: Davenport Pkwy → County Road 79 — ACCEPTED, ride completed, auto-nailer did not fire
- Sequence 7782-7787: declined offers post-acceptance, accumulating `expected_*` values demonstrating the GC defect

**DDL applied directly:**
- `GRANT SELECT, USAGE ON SEQUENCE app_private.poi_cache_id_seq TO atjb;`
- Should be added to a future migration script for repeatability

**Memory updates this session:**
- Removed stale `gpsAgeSec` TODO (line #3 in user memories)

End of handoff.
