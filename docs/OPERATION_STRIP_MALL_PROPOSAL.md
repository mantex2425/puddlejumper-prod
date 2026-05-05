# Proposal: Operation Strip Mall — un-stub POI matching + transit-class adjacency gate

**Author:** Andrew + Claude (drafted for Gemini review)
**Date:** 2026-05-04
**Status:** **RATIFIED 2026-05-04 evening** (Gemini three-round review). Phase 1A DEPLOYED at revision `puddlejumper-api-00590-dtj`. Phase 1B + Phase 2 are open scope.
**Baseline:** `demolition-2026-05-04` HEAD `574b53e` (Phase 1A commit). Pre-Phase-1A baseline at `e61054c` (revision `00589-whc`).

---

## REVISIONS (read this first before §2)

This proposal went through three Gemini review rounds. The body of the document below preserves the original first-draft proposal with strikethroughs noted in this section. **Where the body conflicts with this REVISIONS block, this block wins.**

### R1 — Cache key architecture: H3-12 → radius-based PostGIS (CRITICAL)

**Stale (in body §2 Change 2 + §6 Q2):** H3-12 cell as cache key with PRIMARY KEY constraint.

**Ratified:** Cache key is bigserial PRIMARY KEY. Spatial lookup uses GiST-indexed `geography` column with `ST_DWithin` radius search. Hex borders create cliff effects (Andrew's pushback) — same reason Price Radar uses continuous-radius IDW instead of H3 buckets for pricing. The radar architecture extends to POI cache.

**Final schema:**
```sql
CREATE TABLE app_private.poi_cache (
  id              bigserial PRIMARY KEY,
  cached_at       timestamptz NOT NULL DEFAULT NOW(),
  expires_at      timestamptz NOT NULL,
  query_lat       double precision NOT NULL,
  query_lng       double precision NOT NULL,
  query_geog      geography(Point,4326) NOT NULL
                  GENERATED ALWAYS AS (
                    ST_SetSRID(ST_MakePoint(query_lng, query_lat), 4326)::geography
                  ) STORED,
  google_place_ids text[] NOT NULL,
  business_names   text[] NOT NULL,
  business_types   text[] NOT NULL
);
CREATE INDEX idx_poi_cache_geog ON app_private.poi_cache USING GIST (query_geog);
CREATE INDEX idx_poi_cache_expires ON app_private.poi_cache (expires_at);
```

### R2 — Multi-row dedupe: not addressed → Option C (Claude's catch)

**Stale:** original proposal didn't address what happens when multiple cache rows are within range.

**Ratified:** distance-weighted dedupe across rows. For each unique business name, keep its closest occurrence; sort by closeness; pass top K to fuzzy matcher. This applies the radar concept to POI identity (not just to spatial lookup). Code pattern:

```python
def get_pois_near_cluster(cluster, cur) -> list[POI] | None:
    cur.execute("""
        SELECT business_names, business_types,
               ST_Distance(
                 query_geog,
                 ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography
               ) AS dist_m
        FROM app_private.poi_cache
        WHERE expires_at > NOW()
          AND ST_DWithin(
            query_geog,
            ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,
            80
          )
        ORDER BY dist_m ASC
    """, (cluster.median_lng, cluster.median_lat,
          cluster.median_lng, cluster.median_lat))

    rows = cur.fetchall()
    if not rows:
        return None  # cache miss -> caller invokes Google Places API

    # Distance-weighted dedupe across rows
    seen = {}
    for row in rows:
        for name, btype in zip(row['business_names'], row['business_types']):
            if name not in seen or row['dist_m'] < seen[name][0]:
                seen[name] = (row['dist_m'], btype)

    return [POI(name=n, business_type=t, dist_m=d)
            for n, (d, t) in sorted(seen.items(), key=lambda x: x[1][0])]
```

### R3 — Search radius: locked at 80m

**Stale (in body §6 Q2):** open question between H3-11 (~70m) and H3-12 (~14m).

**Ratified:** 80m radius for cache lookups. 50m too tight (border-cliff within a single mall). 100m pulls in adjacent businesses (different parcel). Goldilocks per Gemini.

### R4 — Fuzzy match weighting: 80/20 business_name / street_number

**Stale (in body §6 Q1):** weighting unspecified.

**Ratified:** business_name primary at 0.8, street_number tiebreaker at 0.2. Reasoning from Hollister case study: "5770 Hollister Rd" had multiple address numbers across the same parcel, so street_number alone was a distractor. Business name "Planet Fitness" was the only anchor that mattered. Final formula:
```
poi_match_score = (business_name_levenshtein × 0.8) + (street_number_present × 0.2)
```

### R5 — Library: rapidfuzz==3.10.1

**Stale:** library not specified.

**Ratified:** `rapidfuzz==3.10.1` (C-compiled, MIT licensed). Already added to `requirements.txt` and deployed via Phase 1A so Phase 2 has zero new dependencies. Verified import works in venv post-deploy.

### R6 — Phase 1A scope: ratified as proposed

Transit-class adjacency gate (motorway/primary/secondary/tertiary, OSM tag_id 101-109) blocks `_signal_adjacent_road_match` from firing when driver is on a transit road. Other classes (residential 110/111/114, off-wire 112/113/117, unknown, NULL) preserve pre-gate behavior. **DEPLOYED 2026-05-04 evening at commit `574b53e`, Cloud Run revision `00590-dtj`.**

Also folded into Phase 1A by Gemini's recommendation: forensic restoration sub-tasks (re-thread `wai_*` fields into `_log_decision_context`, add new `wai_current_road_class` column). **PARTIAL — code changes complete via Phase 1A, schema migration + INSERT updates deferred to separate Phase 1B sub-commit.**

### R7 — Confidence floor: 0.40 → 0.30 when `poi_match >= 0.8`

**Ratified.** When fuzzy match score is high (≥ 0.8), the matcher has high-confidence physical evidence ("Business Truth"). The 0.40 geometric threshold can be relaxed to 0.30 because the POI signal substitutes for the missing geometric coherence.

### R8 — Phase 2c (batch warming): clear-to-fly, deferred

Periodic batch job that scans `pudo_decision_context` for clusters where `poi_lookup_source = 'skipped'` (cache miss + no API call made because cluster too small) and warms `poi_cache` from Google Places. Self-learning map. **NOT in Phase 2 first cut.** Deferred to Phase 2c.

### R9 — Brief-pickup problem: defer pending heartbeat_log data

Original proposal §3 noted brief-pickup is unsolved by either Phase 1 or Phase 2. Gemini ratified DEFER. Tonight's drives (post-heartbeat-log restoration) are the first useful data. Next session can run a forensic query to characterize walkup cadence; the fix is likely cluster threshold tuning (3 samples → 2) and/or an Android-side trip-state signal. Separate sprint.

### R10 — Branch strategy: stay on `demolition-2026-05-04`

Per Gemini: momentum > clean naming. Phase 1A landed on `demolition-2026-05-04`. Phase 1B + Phase 2 should continue on the same branch. Rename before merge to main.

---

---

## 1. Why this exists

Two PUDO failures observed in tonight's drives (offer 8336 ride 1, offer 8338 ride 2 — both today):

**Failure A: false positive at McKeever traffic light (ride 1).**
Cluster formed at McKeever Rd traffic light, 32m from Sienna Pkwy, 350m from the actual dropoff geocode. WAI returned `wai_pudo_type='dropoff', wai_confidence=0.41` against offer 7712 (today's ride 1 dropoff at "Sienna Pkwy"). Action was `LogAmbiguousMatch` (just barely below FireDropoff threshold for the no-current_offer_id case), but the match shouldn't have surfaced at all. Driver was on a tertiary transit road, not at a destination.

**Failure B: fragile match at strip-mall dropoff (ride 1).**
Actual dropoff at Bees Passage Road (residential, OSM tag_id=110), 5.4m snap. Sienna Pkwy is 32m away. WAI fired `FireDropoff` at confidence 0.42 — over threshold by 0.02. The match worked but is geometric luck: cluster centroid sits in a strip-mall parking lot, and Sienna Pkwy happens to run within 150m. There's no positive evidence that we recognized this as the strip mall in the offer text. Tomorrow it could fail.

These are exactly the failure modes documented in `docs/FORENSIC_2026_04_27_HOUSTON_SHIFT.md`:

> **Case B — Planet Fitness at 5770 Hollister Rd (canonical regression case).**
> ...Structural Pillar (S32) reported 0% road match. Auto-Nail failed to fire.
> Driver intervention: Manual Nail required.

> **Case C — Target at Northwest Crossing strip mall (same-parcel inversion).**
> ...same connected building structure as Case B...with the Uber-listed named road and the driver's actual approach road **swapped** between them.

The Hollister case captured Apr 27 is the same family of failures as today's. POI-level recognition is the architectural fix, and it's been **explicitly stubbed** in production per `docs/WHERE_AM_I_PROPOSAL_v2.md` §v2.4.7:

> POI class (RFC §5.1, "United Airlines" / airport curb / business POI cases).
> RFC v2 deferred this to v1.1...
> Phase D entry default: stub `_match_current_pudo` for POI to return
> `(matched=False, confidence=0.0, reason="poi class not yet implemented")`...

`docs/SPRINT_A_FINDINGS_2026-05-04.md` §2b confirms the gap:

> **G2a and G2b are not implemented.** userMemories says "PUDO fire rule (locked Apr 25 2026): G1 odometer / G2a POI via Google reverse geocode / G2b residential intersection within 200m." Grep finds no implementation of either G2a or G2b.

This proposal un-stubs POI matching (G2a), implements a transit-class adjacency gate to kill Failure A, and explicitly notes what it does NOT solve.

---

## 2. Proposal summary (ratification surface)

Two complementary changes, shippable independently:

### Change 1: Transit-Class Adjacency Gate

**Surface:** `where_am_i.py:_signal_adjacent_road_match`, `pivot_context.get_pivot_context`, `RoadTopology` dataclass.

**Behavior change:** when GPS-snapped `current_road` resolves to a transit-class road (OSM `tag_id` 101–109: motorway / motorway_link / trunk / trunk_link / primary / primary_link / secondary / tertiary), `_signal_adjacent_road_match` returns `0.0` regardless of overlap with target named roads. Adjacency rescue only applies when on residential (110), off-wire (NULL), parking-lot service class (112/113/117), or unknown.

**Rationale:** adjacency was designed for off-wire cases per the Hollister/Planet Fitness shift (`FORENSIC_2026_04_27_HOUSTON_SHIFT.md` Case B). Driver on a tertiary road in transit, 32m from a parallel target road, is not "near a destination, off-wire" — they're transit. Today's McKeever (tertiary, tag_id=109) traffic light vs. Sienna Pkwy (secondary) is the false-positive surface.

**Gate cost:** transit-class drivers lose adjacency rescue. The `on_target_road` signal still applies (driver on the actual target transit road still scores). Real risk: if Uber's geocode for an offer puts the pickup on a transit road and the driver actually pulls into a residential side street to wait, the transit-gate doesn't apply (they pivoted off transit). Non-issue.

**Files:** 2 (where_am_i.py, pivot_context.py). Test additions: 3-5 cases in `tests/test_where_am_i.py`.

### Change 2: POI Adjacency (G2a) — un-stub Operation Strip Mall

**Surface:** new `poi_service.py` module, new `app_private.poi_cache` table, new `_signal_poi_match` in `where_am_i.py`, weight-table extension, optional confidence-floor adjustment for POI-verified matches.

**Behavior change:** during `DIAGNOSE` phase, when a cluster is detected, the heartbeat handler queries Google Places "Nearby Search" within 50m of the cluster centroid. The result (a list of business names) is matched fuzzily (Levenshtein) against the offer's `pickup_address` and `dropoff_address`. A match contributes to a new `poi_match` signal. Cache results in `poi_cache` ~~keyed by H3-12 cell~~ **[SUPERSEDED by R1 — radius-based GiST geography lookup]** with 30-day TTL.

**Rationale, from existing design:**

`docs/WHERE_AM_I_PROPOSAL_v2.md` §5.1 already specifies POI-class matching:

> **poi** ("Target Store", "Hobby Airport"): Within POI polygon (if available) OR within 100m of POI center. `on_wire` not required (parking lots, airports)

Today's stub treats POI as fallthrough. Un-stubbing closes that gap.

`FORENSIC_2026_04_27_HOUSTON_SHIFT.md` Case C cooperative-cache implication:

> Because Planet Fitness and Target occupy the same connected building structure, their computed Adjacency Sets should converge to the same set...the cache key cannot be the POI alone — it must be keyed on the building footprint or the place_id's parent geometry.

~~This proposal uses **H3-12 cell as cache key**, which clusters semantically-related POIs in the same building footprint without requiring place_id parent geometry. (Open: H3-12 cell ≈ 14m hex; some buildings span multiple cells. Mitigation in §6.)~~

**[SUPERSEDED by R1]** Cache key is bigserial; spatial proximity uses GiST geography column. See REVISIONS R1 for full schema and rationale.

**Per-bucket weight changes — TUNABLE pending shadow-mode data per `SPRINT_A_FINDINGS_2026-05-04.md` §1a.** Initial proposal:

| Bucket | prox | breadcrumb | tight | dur | on_target | off_wire | adj | **poi** | sums |
|---|---|---|---|---|---|---|---|---|---|
| intersection | 0.10 | 0.30 | 0.15 | 0.10 | 0.20 | 0.05 | 0.10 | **0.00** | 1.00 |
| single_road | 0.05 | 0.25 | 0.10 | 0.10 | 0.10 | 0.05 | 0.05 | **0.30** | 1.00 |
| number_on_street | 0.20 | 0.15 | 0.10 | 0.10 | 0.10 | 0.00 | 0.05 | **0.30** | 1.00 |
| **poi (new)** | 0.15 | 0.05 | 0.15 | 0.10 | 0.05 | 0.00 | 0.05 | **0.45** | 1.00 |

POI weight is non-zero for `single_road`, `number_on_street`, and the new `poi` bucket — these are the address classes where POI evidence helps disambiguate strip-mall destinations. POI is zero for `intersection` because intersections are inherently road-name puzzles, not building puzzles.

For Gemini's review: the 0.45 weight for the `poi` bucket is more aggressive than Gemini's first-pass proposal (0.60 with road-match at 0.0). I've kept road-match at 0.05 for the `poi` bucket as a tiebreaker — if two nearby POIs both match the address fuzzily, the road-match signal can break the tie. Gemini's 0.60+0.0 is also defensible; this is a tuning question.

**Confidence floor adjustment:** When `poi_match >= 0.8` (high-confidence fuzzy match), allow firing at `confidence >= 0.30` instead of 0.40. Rationale per `FORENSIC_2026_04_27_HOUSTON_SHIFT.md`:

> Cases B and C occurred in the same shift, in the same connected building structure...with the Uber-listed named road and the driver's actual approach road **swapped** between them...The B-27 architectural inversion...is the only proposed approach that handles both Case B and Case C with a single mechanism.

A POI match of "Planet Fitness" against the offer's "5770 Hollister Rd" is positive evidence that overrides the road-name discrepancy. This is what Gemini called "Business Truth."

**Files:** 5 (where_am_i.py, driver_heartbeat.py, new poi_service.py, new migration for poi_cache, tests). Plus secrets (GOOGLE_PLACES_API_KEY) and Cloud Run env config.

---

## 3. What this proposal does NOT solve

Honest scope. Three problems were identified tonight:

| Problem | Solved by Change 1? | Solved by Change 2? |
|---|---|---|
| McKeever transit-road false positive | **YES** | partial (would still be filtered by transit-gate; POI doesn't help if no POI is at the light) |
| Strip-mall fragile-luck dropoff | NO | **YES** (Pepperoni's / Shell at the Bees Passage strip mall would match offer text) |
| Brief residential pickup (no cluster) | NO | NO |

**The brief-pickup problem is real and unsolved.** It's the failure mode that today killed ride 1's pickup entirely (passenger walkup was too short to form a 3-sample cluster). Per `SPRINT_A_FINDINGS_2026-05-04.md` §3a:

> **What does Android's heartbeat cadence look like during a 5-second curbside walkup?**
> ...With heartbeat_log restored (revision 00587-c6b deployed this session), a fresh ride is needed to observe actual frame cadence at curbside walkups.

Tonight's drives now provide that data — heartbeat_log is healthy and we can characterize the cadence. The brief-pickup problem requires a separate mechanism (likely cluster-threshold tuning AND/OR an Android-side trip-state signal) and is a **separate sprint scope**, not part of this proposal.

---

## 4. Implementation plan (incremental, gated)

### Phase 1: Transit-Class Adjacency Gate (Change 1)

Estimated 1-2 hours. Self-contained, low-risk.

1. `pivot_context.py` — extend the LATERAL snap subquery to also return `road_class` mapped from `tag_id`. Update `get_pivot_context` return dict to include `current_road_class`. Five str_replace edits.
2. `where_am_i.py` — `RoadTopology` dataclass gains `current_road_class: Optional[str] = None`. `_compute_road_topology` plumbs it. `_signal_adjacent_road_match` takes `current_road_class` parameter and applies the transit gate. `_compute_signals` call site passes it in. Four edits.
3. `tests/test_where_am_i.py` — three new cases: transit-class blocks adjacency, residential allows adjacency, off-wire (NULL class) allows adjacency.
4. Gates: pytest 236+ passing, app.py boots, full Bruno collection clean.
5. Deploy. Re-run Bruno. Drive a trip past the McKeever-Sienna intersection with a stale offer in the queue and verify confidence drops below threshold.

### Phase 2: POI Adjacency (Change 2)

Estimated 4-8 hours including testing. Rolls in two sub-phases.

**Phase 2a: poi_cache schema + poi_service module (pure infra, no behavior change).**

> **[REVISED per R1, R2, R3]** Original H3-keyed schema below is superseded. Use the radius-based GiST geography schema in REVISIONS R1, and the distance-weighted dedupe lookup in REVISIONS R2.

1. ~~New migration~~: see REVISIONS R1 for the canonical schema.

   ~~```sql
   CREATE TABLE app_private.poi_cache (
     h3_index            text PRIMARY KEY,
     created_at          timestamptz NOT NULL DEFAULT NOW(),
     expires_at          timestamptz NOT NULL,
     google_place_ids    text[] NOT NULL,
     business_names      text[] NOT NULL,
     types               text[] NOT NULL,
     centroid_lat        double precision,
     centroid_lng        double precision
   );
   CREATE INDEX idx_poi_cache_expires ON app_private.poi_cache(expires_at);
   ```~~
2. New `poi_service.py` module.
   - `get_pois_near_cluster(cluster, cur) -> list[POI] | None`:
     - ~~Compute H3-12 cell from cluster centroid. Lookup `poi_cache` by `h3_index`.~~ **[SUPERSEDED]** Run `ST_DWithin` radius lookup (80m) against `query_geog`. Apply distance-weighted dedupe across rows per REVISIONS R2.
     - On miss (zero rows in radius): call Google Places Nearby Search API (50m radius — slightly tighter than the 80m cache lookup is intentional, prevents over-caching). Store result row with TTL = NOW() + 30 days. Return.
     - On API error or rate limit: return `[]` (fail-closed; matcher just doesn't get POI signal).
3. Secret + env: `GOOGLE_PLACES_API_KEY` in Secret Manager, mounted in Cloud Run.
4. Unit tests: cache hit, cache miss with mocked API, API error path, expired entry path.
5. Deploy with no callers yet. Verify cache table is healthy and the service module imports cleanly.

**Phase 2b: wire poi_match signal into the matcher.**

1. `where_am_i.py` — add `_signal_poi_match(poi_names, target_address)` function. Returns max Levenshtein-similarity score in `[0.0, 1.0]` across the POI names against the target address text.
2. Update `_CONFIDENCE_WEIGHTS` per the table in §2 above.
3. Add `poi` bucket to the matcher dispatch in `_evaluate`. POI bucket already exists in `bead_on_wire.py:classify_address` per `WHERE_AM_I_PROPOSAL_v2.md:1176-1185` but is currently stubbed.
4. `driver_heartbeat.py` — call `poi_service.get_pois_near_cluster` after cluster detection, thread the POI list into `WhereAmI.evaluate`. Single addition, ~3 lines.
5. Optional confidence-floor adjustment: in `dispatch.py`, when the top match has `poi_match >= 0.8`, allow firing at `confidence >= 0.30` instead of 0.40. Three line change.
6. Tests: unit tests for `_signal_poi_match` (string variants, fuzzy boundaries, empty inputs). Integration test for the strip-mall scenario (mock POI returning ["Shell", "Pepperoni's"] for offer with address "Sienna Pkwy" — verify `poi_match` fires).
7. Bruno scenario: add a "strip mall dropoff" test with seeded POI cache entry.
8. Deploy. Drive 3 rides. Verify confidence-floor fires for known POI dropoffs.

### Phase 3: Forensic re-enable

Per `SPRINT_A_FINDINGS_2026-05-04.md` and the forensic regression in `_log_decision_context`:

> dropped columns (primary_offer_id, wai_status, wai_target_address, wai_reason, wai_on_target_road, wai_current_road, wai_off_wire_duration_s...) become vestigial NULL in the table

Re-thread these into the INSERT so we have observability for next debugging session. Five-minute change. Could fold into Phase 1 or Phase 2.

---

## 5. Why now (separate from "next sprint")

`docs/INDEX.md` is the doc-of-truth and lists Sprint A as architectural pivot in flight. The state-machine demolition completed today; this proposal is the matcher-tuning sprint that the May 2-4 handoff explicitly named as "real scope, not just wiring":

> **Sprint A implication:** matcher work (threshold tuning, signal weight calibration) is real scope, not just wiring. The smoke test framework (`tmp/smoke_test_wai_queue_v2.py`) is reusable for empirical iteration.

The transit-gate fix (Phase 1) can ship tonight — it's a 2-file structural change with clean tests. The POI fix (Phase 2) is bigger and should be properly designed before code lands.

---

## 6. ~~Open questions for Gemini~~ → RESOLVED

All six open questions were resolved in three rounds of Gemini review on 2026-05-04 evening. Resolutions captured in REVISIONS at top of doc:

1. ~~POI bucket weights~~ → R7 (POI 0.45, road_match 0.05 tiebreaker, ratified)
2. ~~H3 resolution for cache key~~ → R1, R3 (radius-based, 80m, NOT H3 — Andrew's pushback ratified)
3. ~~Spatial join supplement~~ → R8 (Phase 2c batch warming, deferred)
4. ~~Confidence-floor adjustment~~ → R7 (0.40 → 0.30 when poi_match >= 0.8, ratified)
5. ~~Brief-pickup problem~~ → R9 (defer pending heartbeat_log data)
6. ~~Forensic restoration scope~~ → R6 (folded into Phase 1A code; schema migration remains as Phase 1B)

The original questions text is preserved below for historical reference. **Do not act on §6 directly — use REVISIONS R1-R10.**

<details>
<summary>Original open questions (historical, all resolved)</summary>

1. **POI bucket weights.** Initial table in §2. Is `poi: 0.45` too aggressive? Too conservative? Should I keep `road_match` at 0.05 as tiebreaker or zero it out per your earlier proposal?

2. **H3 resolution for cache key.** I've proposed H3-12 (~14m hex). Northwest Crossing Hollister/Target case (`FORENSIC_2026_04_27_HOUSTON_SHIFT.md` Case C: 350m diagonal across the same connected structure) suggests larger buildings span dozens of H3-12 cells. Should we use H3-11 (~70m hex) to better cluster building footprints, accepting that adjacent strip malls will share a cell? Or stay at H3-12 and rely on cooperative population (each driver hitting the structure populates their own H3-12 cell)?

3. **Spatial join supplement.** Your closing question: "should we use a 'Spatial Join' to find nearby POIs in our DB first, or should we strictly stick to the H3-index lookup for maximum speed?" My answer: **stick to H3-index lookup for the live path** (microseconds vs 100ms+ for spatial join), but **add a backfill batch job** that periodically queries Google Places for stops detected in `pudo_decision_context` rows where POI cache had a miss, and writes the result to `poi_cache`. This way a strip mall I haven't been to gets warmed by another driver hitting it, without slowing down the hot path. Want me to add that as Phase 2c?

4. **Confidence-floor adjustment.** Lowering threshold from 0.40 to 0.30 when `poi_match >= 0.8` is what enables a "Shell + Pepperoni's at Bees Passage" match to fire confidently against a "Sienna Pkwy" offer. But it adds a second threshold to the system. Is the win worth the complexity? Alternative: keep threshold at 0.40 but raise `poi` bucket's POI-match weight to 0.55 so any POI match alone clears 0.40.

5. **Brief-pickup problem.** This proposal explicitly does NOT solve it. Should I draft a separate proposal for that (cluster threshold tuning + Android trip-state signal)? Or wait until we have post-deployment data to characterize the gap precisely?

6. **Demolition follow-up: `_log_decision_context` regression.** Re-threading the `wai_*` road fields into the INSERT (Phase 3) is independent of this proposal but blocks all future forensic investigation. Should it fold into Phase 1 (5-line addition) or land as a standalone "forensic restoration" commit? I lean toward folding into Phase 1 since we're already touching adjacent code.

</details>

---

## 7. Rollback plan

**Phase 1A status:** SHIPPED at commit `574b53e`, Cloud Run revision `puddlejumper-api-00590-dtj`. The deployed transit gate is the rollback target for Phase 2 work, not the rollback target itself.

**Phase 1A rollback** (if needed): `git revert 574b53e`. The `_signal_adjacent_road_match` signature change is backward-compatible (`current_road_class` defaults to None), so reverting only the gate logic is viable too. Pytest invariants restored. Cloud Run rollback target: revision `00589-whc`.

**Phase 1B rollback** (when 1B ships): revert the schema migration (DROP COLUMN on the new fields) and `git revert` the INSERT-update commit. Cloud Run rollback target: revision after Phase 1A but before Phase 1B.

**Phase 2 rollback:** the `poi_match` signal weight is `0.00` for the `intersection` bucket and the system already treats POI as fallthrough (per WHERE_AM_I_PROPOSAL_v2.md §v2.4.7 stub). Setting all `poi_match` weights to `0.00` restores pre-Phase-2 behavior without touching the API integration. The `poi_cache` table can stay (no harm in keeping the data even unused). Cloud Run env can keep the `GOOGLE_PLACES_API_KEY`.

**Worst-case rollback** (everything since demolition completed): revert to `puddlejumper-api-00589-whc`. Branch `demolition-2026-05-04` HEAD `e61054c` is the rollback baseline (state-machine demolition complete, transit gate not yet deployed).

---

## 8. Acceptance criteria

**Phase 1A — DEPLOYED 2026-05-04 evening:**
- ✅ pytest 241 passing (236 pre-existing + 5 new transit-gate cases)
- ✅ Bruno PUDO Simulation: 17/17 pass (unchanged behavior on simulated path)
- ⏳ Live query against `pudo_decision_context` post-deploy: at least one confirmed instance where a heartbeat at a transit-road traffic light near a target road has `wai_confidence < 0.30` (vs pre-fix 0.41) — **pending live drive validation**

**Phase 1B — pending:**
- pytest 244+ passing (241 + 3 new forensic-population integration tests)
- Schema migration applies cleanly on a copy of prod
- Bruno PUDO Simulation: 17/17 pass (no behavior change expected)
- `pudo_decision_context` rows written post-deploy populate `wai_current_road`, `wai_current_road_class`, `wai_target_address`, `wai_reason`, `wai_on_target_road`, `wai_off_wire_duration_s` (POI columns NULL until Phase 2)

**Phase 2 — pending:**
- pytest 250+ passing (Phase 1B baseline + 6 new POI cases)
- Bruno PUDO Simulation: 19+/19+ (Phase 1B baseline + new strip-mall scenario)
- Live drive with poi_cache hit: a known strip-mall dropoff fires `FireDropoff` with `wai_confidence >= 0.50` (vs today's 0.42)
- Live drive with poi_cache miss: cluster forms, Google Places API call succeeds, cache row written, FireDropoff fires
- p99 heartbeat latency increase < 250ms (POI lookup adds latency on cache miss)

---

## 9. References (load these into the next session if Gemini greenlights)

- `docs/SIMPLIFIED_ARCHITECTURE.md` (Sprint A architectural law)
- `docs/WHERE_AM_I_PROPOSAL_v2.md` §5.1 (address class matching), §v2.4.7 (POI stub origin)
- `docs/FORENSIC_2026_04_27_HOUSTON_SHIFT.md` (Hollister, Planet Fitness, Target case studies)
- `docs/FORENSIC_2026_04_28_HOUSTON_WAYS_AUDIT.md` (Northwest Crossing localized data quality)
- `docs/SPRINT_A_FINDINGS_2026-05-04.md` (G2a confirmed missing, weights tunable)
- `docs/CANONICAL_RULES.md` (coordinate functions, time, 4-box controller)
- `docs/SESSION_PROTOCOL.md` (paired-programming protocol, paste-safety rules)

---

**Proposal ends. ~~Awaiting Gemini consensus before any code authored.~~ → RATIFIED 2026-05-04 evening (three-round Gemini review). Phase 1A deployed at commit `574b53e`, Cloud Run revision `00590-dtj`. Phase 1B + Phase 2 are open scope per REVISIONS at top of doc.**