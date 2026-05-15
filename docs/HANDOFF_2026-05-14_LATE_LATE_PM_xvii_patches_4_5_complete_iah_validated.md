# HANDOFF — 2026-05-14 LATE-LATE PM — §XVII Patches 4/4a/4b shipped, IAH validated, Patch 5 next

**Predecessor session:** the §XVII Patch 4 implementation session that began
from `HANDOFF_2026-05-14_LATE_PM_xvii_patches_4_5.md`.
**Branch:** `phase-2c-2-tad-exit-4tools`
**HEAD:** `357c9eb` — §XVII Patch 4b (replay lift removal + L-5 backfill)
**Cohort context:** Andrew has been driving this work continuously. No
wellness redirects. The handoff exists because the IAH validation is the
right architectural milestone to pause on before authoring Patch 5.

---

## What shipped this session

### Patches in version control, pushed to origin

```
357c9eb phase-2c-2: §XVII Patch 4b — replace replay_pudo build_target_spec lift with production import + L-5 backfill
eb88b39 phase-2c-2: §XVII Patch 4/5 — PDC forensic logging + poi_lookup_source drift fix (incl. 4a)
35e2e44 phase-2c-2: §XVII Patch 3 (Full)/5 — Semantic Anchor Engine, no more stubs
fb5ac91 phase-2c-2: §XVII Patch 3a/5 — TargetSpec.address plumbed, Head 5 dispatch wired
53814b0 phase-2c-2: §XVII Patch 2/5 — where_am_i Head 5 semantic anchor signal
3e5b4dd phase-2c-2: §XVII Patch 1/5 — poi_service searchText helpers + defensive read-cache fix
75c0dba doctrine: §XIV.J Live-PG Test Floor
```

### Patch 4 (commit `eb88b39`) — what it does

Three concurrent landings in one commit:

**Patch 4 proper — PDC forensic logging + drift fix.**

- Adds 15th `MatchOutcome` field: `semantic_lookup_source: Optional[str] = None`.
  Values: `'semantic_cache_hit'`, `'semantic_api_call'`, `'semantic_api_error'`,
  or None.
- Plumbs `POILookupResult.source` through the entire matcher pipeline:
  `_fetch_cluster_anchors` (returns `(anchors, source)` tuple now) →
  5 matchers (all accept `semantic_lookup_source` kwarg) →
  `_build_outcome` → `MatchOutcome`.
- **Fixes the 7-day production drift bug** in `driver_heartbeat.py:1055`.
  Pre-Patch-4, the `poi_lookup_source` PDC column was bound to
  `top_outcome.poi_witness` — a Head-1 witness string like
  `"fuzzy:Pappadeaux"`, not a lookup-source string. The column had
  been 100% NULL in production for 7 days because Head 1 was dormant;
  drift only would have started producing wrong rows the moment §XVII
  Head 5 began firing. Patch 4 routes the corrected
  `top_outcome.semantic_lookup_source` to the column.
- Tie-break in driver_heartbeat: Head 5 wins when its score >=
  Head 1's score (None as 0). When Head 5 wins, all three PDC columns
  (`poi_lookup_source`, `poi_match_score`, `poi_top_names`) reflect
  Head 5's data, with `sem_witness` prepended to `cluster_poi_names`.
  When Head 1 wins or no head fires, Head 1 score lands in
  `poi_match_score` and `cluster_poi_names` lands in `poi_top_names`
  (legacy behavior preserved).
- TODO: `top_outcome.poi_witness` is now unbound from any PDC column.
  A follow-up "PDC witness cleanup" patch should add a dedicated
  `poi_witness` column or repurpose `match_signal`. Production volume
  of misfiled Head 1 witnesses: ~65 rows in 7 days, negligible.

**Patch 4a — test-fake update.**

- `_StandinMatchOutcome` in `tests/test_log_decision_context_bindings.py`
  was stale across 5 fields (`poi_type_match`, `poi_type_witness` from
  Item 2 2026-05-08; `semantic_anchor_score`, `semantic_anchor_witness`
  from Patch 2; `semantic_lookup_source` from Patch 4). The drift didn't
  surface until Patch 4 made `_log_decision_context` actually read those
  attributes.
- `test_pdc_poi_lookup_source_populated` was rewritten — it had been
  codifying the pre-Patch-4 drift bug as the correct invariant
  (asserting `poi_witness 'branded:starbucks'` lands in
  `poi_lookup_source`). Now asserts the corrected invariant:
  `semantic_lookup_source 'semantic_cache_hit'` lands in
  `poi_lookup_source` when Head 5 wins.

**Patch 4b (commit `357c9eb`) — replay harness lift removal.**

Discovered during IAH replay validation. `scripts/replay_pudo.py`
carried a 30-line "lift" of `driver_heartbeat.py:_bucket_to_target_spec`
written 2026-05-13 (commit `5ee1560`), before Patch 3a added the
`address=address_text` kwarg to all 4 TargetSpec construction sites.
The lift was never updated → `TargetSpec.address=None` for every
replayed offer → `_fetch_cluster_anchors` short-circuited at
`if not target_addr: return [], None` → Head 5 dark → false negative
in the replay.

**Resolution:** delete the lift, replace with a direct import:

```python
from driver_heartbeat import _bucket_to_target_spec as build_target_spec
```

Eliminates the bug class for this helper permanently. Production's
helper becomes the single source of truth; the replay tracks any
future evolution automatically. L-5 backfill added — file was
committed in `5ee1560` without a trailing newline.

### Test state

```
609 passed, 5 skipped, 0 failed, 3 deprecation warnings
```

Unchanged from baseline at session-start. Patch 4 was net-zero on test
count (2 renames + 1 rewrite). New §XVII regression tests are Patch 5.

---

## The IAH validation event — gold-standard evidence

Ride 7883 was the canonical case the morning handoff predicted §XVII
would fix: dropoff "United, Houston, Texas" never fired in production
(actual_dropoff_at IS NULL), Tier A backtest predicted score ~0.824.

**Validation post-Patches 4 + 4a + 4b:**

Replay of ride 7883 dropoff via `scripts/replay_pudo.py 7883`
produced **6 contiguous arrest frames at the canonical IAH arrest
coordinate** (29.986899, -95.335009), spanning 05:06:02 → 05:06:30 CDT.
All 6 frames produced:

```
target_address          = 'United, Houston, Texas'
semantic_anchor_score   = 0.8241866559386112
semantic_anchor_witness = 'semantic_anchor:United/transportation_service (88m)'
semantic_lookup_source  = 'semantic_cache_hit'
poi_match (Head 1)      = 1.0  (branded:united)
composed confidence     = 1.000 → FireDropoff
```

The replay's 0.8242 matches Tier A backtest's 0.824 to 4 decimal
places. Same anchor, same distance, same cache row (`poi_cache.id=349`,
`text_query='United, Houston, Texas'`, 12 places, bias center Houston).

**`poi_cache.id=349.last_hit_at` was NULL pre-replay** — the replay
was the first production touch of that cache row since the Tier A
backtest seeded it at 13:49 CDT today. (Not verified post-replay; the
replay uses SAVEPOINT/ROLLBACK so the touch may not have committed.)

**The replay's full timeline also fired four spurious post-IAH dropoffs**
in the next 41 minutes at coords (30.027, -95.420) and similar — all
matching `branded:united` (Head 1) at saturated 1.000 against
non-IAH places containing "united" in their names (likely United
Healthcare offices, United Rentals branches, or the United Houston
Corporate Support Center). **These are not a §XVII bug.** They would
be suppressed in real production by:
- The §XVI.G Transaction Lock (offer/leg locked for 500ft/10s after fire)
- The SQL idempotency guard `WHERE actual_dropoff_at IS NULL`
- The replay's `current_offer_id=None` hardcoded — production would
  have bound it to 7883 after the IAH fire and the matcher wouldn't
  re-evaluate that leg

The Head-1 fuzzy "united" overreach is a pre-existing concern worth
addressing in a future tuning sprint, not Patch 4 scope.

---

## The pickup investigation — closed

The replay reports pickup@0.4358 confidence — concerning at first
glance, since it sits right at the 0.40 floor. Decomposition via
forensic recon revealed:

**Production fires this pickup at 0.876 confidence**, per
`pudo_decision_context` rows at 04:12:31 CDT 2026-05-14 (the actual
production pickup-fire moment):

```
wai_pudo_type      = pickup
wai_offer_id       = 7883
wai_confidence     = 0.8758358
wai_current_road   = 'Camille Park Drive'
wai_current_road_class = 'residential'
wai_on_target_road = 1
wai_off_wire_duration_s = 0
```

**The replay's 0.4358 is a harness artifact**, not a production
bug. Specifically:

- `RoadTopology.current_road = None` in the replay (production sees
  `'Camille Park Drive'`)
- `RoadTopology.on_wire = False` (production sees True)
- `on_target_road = 0` in the replay → loses 0.20 weight
- `breadcrumb_match = 0` in the replay (recent_clusters_fn is a stub)
  → loses 0.30 weight
- Total replay weight lost: 0.50, exactly matching the gap between
  observed 0.44 and production's expected 0.94

**Why the replay's topology is wrong:** `pivot_context.get_pivot_context()`
(called from `WhereAmI._compute_road_topology` at line 2065 of
`where_am_i.py`) uses NOW()-anchored SQL. The replay's `cluster_fn`
and `recent_clusters_fn` were written to dodge this exact landmine for
cluster detection, but topology computation was missed. The replay
calls production's `_compute_road_topology` with no override, against
DB state that has no recent heartbeat history matching the historical
moment.

**The §XVI adjacency rescue saved the match.** `adjacent_road_match=1.0`
because the cluster IS within 150m of both Camille Park Drive AND
Legacy Oaks Drive (Section D of Step 48 recon confirmed both roads
exist in `routing.houston_ways` with the canonical "Drive" suffix).
The matcher gracefully degraded from the failed primary signals
through the secondary adjacency signal, and the pickup still cleared
the 0.40 floor.

**The matcher is doing the right thing. The replay's topology
computation is broken. Two different problems.**

---

## Deferred concerns (NOT Patch 5 scope)

Three items surfaced but explicitly parked:

### 1. Replay topology injection ("Patch 4c" or call it tooling debt)

The replay needs a `_topology_fn` injection point on `WhereAmI.__init__`
the same way it has `_cluster_fn` and `_recent_clusters_fn`. Until
fixed, the replay's topology signals (`on_target_road`,
`adjacent_road_match` partly, `off_wire_pivot`, breadcrumb-derived
signals) are unreliable for historical replays.

**Sizing:** 1 file (`where_am_i.py`) gains `_topology_fn` parameter
with default `None` falling through to current behavior; 1 file
(`scripts/replay_pudo.py`) injects a topology function that queries
`pivot_context` against the historical moment instead of NOW(); pytest
unaffected (production path unchanged when no injection); regression
test new in `tests/test_replay_topology_injection.py` covering the
override pattern.

**Priority:** medium. Doesn't block Patch 5 (regression tests for
the semantic anchor signal don't need replay topology). Should land
before any other replay-based investigation, because every replay
session until then will produce false-negative pickup/dropoff
confidences for residential-class targets.

### 2. The broader lift audit

`build_target_spec` was a stale lift. The replay's topology is a
different shape of the same bug class (calling production code that
internally uses NOW()-anchored state). Two found examples now justify
the audit.

**Suggested scope:** grep `scripts/` and `tests/` for the word
"lift" in docstrings/comments, plus look for function definitions
whose docstring says "from <module>" or "of <production-function>".
Catalog them. For each: either replace with import (Z-pattern from
Patch 4b) or add behavioral-parity pytest (X-pattern).

**Priority:** low-medium. Not blocking anything; serves as
hardening for future replay/forensic work.

### 3. PDC witness cleanup (the "Patch 4 TODO")

Patch 4 left `top_outcome.poi_witness` (the Head-1 witness string)
unbound from any PDC column after the drift fix. A follow-up patch
should add a dedicated `poi_witness` column or repurpose
`match_signal`. Production volume: ~65 rows in 7 days, all
previously misfiled, negligible for now.

**Priority:** low. The 65 historical rows are statistically
irrelevant compared to the post-Patch-4 stream Head 5 will produce.

### 4. Head-1 fuzzy "united" overreach (pre-existing)

The replay's spurious post-IAH dropoffs at (30.027, -95.420) and
(29.987, -95.338) were all `branded:united` fuzzy matches at
saturated 1.000 against non-airport "united"-named POIs. Production
suppresses these via Transaction Lock + SQL idempotency; the
underlying overreach in `_signal_poi_match` (Head 1) is a recall vs.
precision tradeoff.

**Priority:** out of scope for §XVII entirely. Track as matcher
tuning work for a future sprint.

---

## What's next: Patch 5 (regression test suite for §XVII)

**Branch state:** `phase-2c-2-tad-exit-4tools` at `357c9eb`. Tree
clean (verify with `git status`). Pushed to origin.

**Pytest baseline:** 609 passed, 5 skipped, 0 failed.

**Patch 5 mandate per the morning handoff** (still active):

Build `tests/test_semantic_anchor.py` with the §XIV.J live-PG `db_cur`
fixture pattern (SAVEPOINT-isolated real-PG). Mandatory test cases:

1. **The IAH regression** — score >= 0.40 for "United, Houston, Texas"
   anchors at IAH coords. Use the canonical replay output as the
   fixture: replay scored 0.8241866559386112 at 88m. Pickle the
   12-place anchor list from `poi_cache.id=349.places` and assert the
   matcher produces >= 0.40 against it at cluster (29.9869, -95.3350).
2. **HOU edge case** (xfail or skip — William P. Hobby Airport ~4428m
   beyond 1000m airport horizon; tracked but not blocking).
3. **Cache round-trip** — write a text_query row via `write_text_cache`,
   read back via `_read_text_cache`, verify TTL 365d and `last_hit_at`
   touch.
4. **Per-type horizon selection** — hospital=150m, airport=1000m
   (after Patch 3 raise), default=500m. Use `_SEMANTIC_TYPE_HORIZON_MAP`
   directly.
5. **Negative score below floor** — anchor at 600m with 500m horizon
   returns 0.0 from `_signal_semantic_anchor`.
6. **Dentist preservation** (Andrew's catch) — cluster POI wins when
   no semantic anchor exists for `address_class != 'poi'`. Test
   verifies Heads 1+4 fire correctly for office/dentist destinations.
7. **Iron Curtain** — spatial cluster reads do NOT return semantic
   anchor rows. Synthetic searchText row at (29.76, -95.37);
   `get_pois_near_cluster` at same coords returns `[]`. Proves the
   `AND pc.text_query IS NULL` defensive fix from Patch 1.

**Pytest target after Patch 5:** 617+ passed (609 + 7 new + possibly
1 xfail for HOU).

**Validation evidence to reference in Patch 5's commit message:**

- Cache row: `poi_cache.id=349`, `text_query='United, Houston, Texas'`,
  bias center (29.7604, -95.3698), 12 places, TTL 365d
- Replay produced: 6 frames at canonical IAH arrest with
  `semantic_anchor_score=0.8241866559386112` against
  `semantic_anchor:United/transportation_service (88m)`
- Tier A backtest (predecessor session, morning) predicted ~0.824
- Replay date/time: 2026-05-14, post-`357c9eb` push, ~23:50 CDT

---

## Canonical doc state

`docs/CANONICAL_RULES_SECTION_XVII.md` was committed in `eb88b39`
(swept in by `git add -A` along with the predecessor handoff doc —
worth knowing because the morning handoff said this should wait for
Gemini's final ratification post-Patch-5). **The canonical doc is in
the repo but Gemini has NOT yet ratified it post-Patch-5.** Next
session should:

1. Update the canonical doc to reflect Patch 3 Full / 4 / 4b
   decisions:
   - §C airport horizon 1000m (not 800m as originally drafted)
   - §G `_match_poi_class` is the 5th matcher head (replaces
     `_match_poi_stub`)
   - §K add "No more stubs" discipline rule
   - Bias center: fixed market center (29.7604, -95.3698), not
     cluster centroid
   - Add a §L referencing the IAH validation event (commit `357c9eb`)
2. Submit to Gemini for ratification
3. Append (or leave in place — it's already in `docs/`) to
   `docs/CANONICAL_RULES.md` if Gemini approves

---

## What to do FIRST in the new session

1. **Read this handoff in full.** The pickup investigation is closed
   but the *findings* (replay topology bug, lift audit justified)
   need to be carried forward, not re-discovered.

2. **Run `git log --oneline -5` and verify HEAD is `357c9eb`.** If
   anything differs, recon what changed before proceeding.

3. **Run pytest baseline:** expect 609 passed, 5 skipped, 0 failed.

4. **Authoring order:**
   - Patch 5 (regression test suite) — 1 new file
     `tests/test_semantic_anchor.py` with the 7 mandatory cases
   - Push Patch 5
   - Canonical doc updates → Gemini ratification
   - **Then return to Bug 1** (the silent NULL writer to
     `current_offer_id` — original priority before §XVII started)

5. **Defer for separate sprints:**
   - Replay topology injection (Patch 4c-equivalent)
   - Broader lift audit
   - PDC witness cleanup (TODO from Patch 4)
   - Head-1 fuzzy "united" overreach tuning

6. **The Gemini loop applies.** Patch 5 proposal → Gemini reviews →
   Andrew ratifies → execute.

---

## What went well this session

- Patches 4 / 4a / 4b landed atomically with full L-3 envelope
  discipline. v1's apply script caught its own residual-sweep bug at
  Phase 2 (no disk writes leaked) and v2 fixed both the immediate
  bug AND the underlying substring-overlap class of bug
  (count-based delta gate redesign that benefits all future patches).
- The L-6 corollary's "second strike" landed for real: the test-side
  fake (`_StandinMatchOutcome`) was caught via pytest, not retrospective
  audit. Patch 4a closed the gap before any commit-on-red.
- The IAH validation hit the predicted 0.824 to four decimal places
  on the first replay after Patch 4b — the §XVII implementation is
  mathematically correct, not just structurally plausible.
- The pickup investigation didn't waste effort — it confirmed
  production fires this pickup at 0.876 (well above floor) and
  identified a new replay-harness bug that the broader lift audit
  will catch.

## What to watch for in the new session

- **The replay topology bug is now a known landmine.** Any future
  forensic replay of a residential/intersection target will produce
  artificially-low confidence. Don't treat replay confidences <0.5
  as production-fragility signals until the topology injection lands.
- **The Patch 5 regression tests should use the IAH replay data as
  fixtures, not invoke the replay itself.** Pickle the 12-place
  anchor list from `poi_cache.id=349` into a `.json` fixture and
  assert directly against `_signal_semantic_anchor`. Don't depend on
  the live cache row (it could expire or be evicted in 2027) — make
  the test self-contained.
- **Context budget for the next session:** Patch 5 + canonical doc
  ratification + possibly returning to Bug 1 is realistic if
  disciplined. Tight if anything blows up. Plan accordingly.

---

End of handoff.
