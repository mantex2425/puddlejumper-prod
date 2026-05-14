# HANDOFF — 2026-05-14 LATE PM — §XVII Patches 4–5 + Drift Fix + IAH Replay

**Predecessor session:** the IAH §XVII implementation session (this afternoon
and evening). Built on the morning handoff (`HANDOFF_2026-05-14_PM_xvii_production_patch.md`).
**Branch:** `phase-2c-2-tad-exit-4tools`
**HEAD:** `35e2e44` — §XVII Patch 3 (Full)/5 — Semantic Anchor Engine, no more stubs
**Cohort context:** Andrew has been working continuously since early AM.
No wellness redirects. The session ended at a clean architectural milestone
because the work that comes next benefits from a fresh window and clear
instructions, NOT because of fatigue.

---

## The Mandate for the Next Claude

**Read this first. The temptation to ship fast will be strong. Resist it.**

What's ahead is Patch 4 (PDC forensic logging) and Patch 5 (regression
test suite) — plus a **drift fix** that was discovered during Patch 3
recon and explicitly deferred for clean thinking.

The previous Claude (me, in this session) recommended Option I — carry
the drift forward — and was correctly overruled. Andrew said:

> "option 3, but with the emphasis on instructions to the next chat to
> do that right thing, not the easy thing while trying to ramp a
> suboptimal solution into production"

This handoff exists because Patch 3 was the clean architectural milestone
to stop on. Don't undo that discipline by patching the drift sloppily.

### The Drift in concrete terms

`driver_heartbeat.py` writes to `pudo_decision_context` like this
(around line 1001):

```python
INSERT INTO app_private.pudo_decision_context (
    ...
    poi_lookup_source, poi_match_score, poi_top_names,
    ...
) VALUES (
    ...
    top_outcome.poi_witness if top_outcome else None,    # WRONG: witness → source
    top_outcome.poi_match if top_outcome else None,
    diagnostics.cluster_poi_names if ... else None,
    ...
)
```

**`poi_lookup_source` is meant to hold strings like `cache_hit`,
`api_call`, `api_error`, and (post-§XVII) `semantic_cache_hit`,
`semantic_api_call`, `semantic_api_error` per canonical §XVII §I.**

What's actually being bound is `top_outcome.poi_witness` — a witness
string like `fuzzy:Pappadeaux Seafood Kitchen` when Head 1 fires.

Empirically verified during the session:

```sql
SELECT count(*), count(poi_lookup_source), count(poi_match_score),
       count(poi_top_names)
FROM app_private.pudo_decision_context
WHERE created_at > NOW() - INTERVAL '7 days';
-- 75710 rows, 0 source_set, 65 score_set, 2238 names_set
```

The column has **NEVER** had a row populated in 7 days. The drift bug
is latent — the matcher Heads 1-4 are so dormant in production that
the wrong binding never actually wrote anything. Patch 4 is the first
moment Head 5 will start producing non-None data, which is when the
drift becomes a real bug.

### What "the right thing" looks like for Patch 4

Per the previous session's strategic decision (Option II):

1. **Add a `semantic_lookup_source` field to MatchOutcome** (this would
   be the 15th field; current count is 14 post-Patch 2). Optional[str],
   default None.

2. **Thread the source through `_fetch_cluster_anchors`** in
   `where_am_i.py` (around line 1668-area in evaluate()):
   - `get_anchors_for_text` already returns `POILookupResult(pois, source)`
   - The current helper discards the source
   - Patch 4 captures and returns it so the matcher can populate
     MatchOutcome.semantic_lookup_source

3. **Update the 5 matchers and `_build_outcome`** to accept and pass
   through `semantic_lookup_source` per the same pattern Patch 3 used
   for `semantic_anchor_score/witness`.

4. **Fix the `driver_heartbeat.py` INSERT bindings:**

   | Column | Old binding (wrong) | New binding (right) |
   |---|---|---|
   | `poi_lookup_source` | `top_outcome.poi_witness` | `top_outcome.semantic_lookup_source` |
   | `poi_match_score` | `top_outcome.poi_match` | `max(poi_match, semantic_anchor_score)` — the winning score |
   | `poi_top_names` | `cluster_poi_names` | `[sem_witness] + cluster_poi_names` when Head 5 fires; just `cluster_poi_names` otherwise |
   | `match_signal` (TBD recon) | currently bound to something | also write the witness winner there if architecturally clean |

5. **`top_outcome.poi_witness` needs to land somewhere.** It's the Head 1
   witness (fuzzy/branded/airport_type). Most natural home: a new column,
   OR `match_signal` if it makes architectural sense. Recon first.
   If `match_signal` is already bound to something we shouldn't disturb,
   land Head 1 witness loss in Patch 4 with a TODO comment for a follow-up
   patch that adds a `poi_witness` column.

### What "the easy thing" looks like (DON'T do this)

```python
# DON'T: Bind semantic_anchor data into the drifted column
poi_lookup_source = top_outcome.semantic_anchor_witness or top_outcome.poi_witness
```

This would establish wrong precedent at the moment §XVII starts producing
forensic data. Andrew explicitly rejected this path.

---

## Where Things Stand (Verified at session-end)

### Patches shipped to origin

```
35e2e44 phase-2c-2: §XVII Patch 3 (Full)/5 — Semantic Anchor Engine, no more stubs
fb5ac91 phase-2c-2: §XVII Patch 3a/5 — TargetSpec.address plumbed, Head 5 dispatch wired
53814b0 phase-2c-2: §XVII Patch 2/5 — where_am_i Head 5 semantic anchor signal
3e5b4dd phase-2c-2: §XVII Patch 1/5 — poi_service searchText helpers + defensive read-cache fix
75c0dba doctrine: §XIV.J Live-PG Test Floor
```

### What's live in the trunk

**Patch 1 (`3e5b4dd`):** `poi_service.py`
- `PLACES_V1_TEXT_URL`, `PLACES_V1_TEXT_MAX_RESULTS`, `SEMANTIC_CACHE_TTL_DAYS`, `SEMANTIC_DEFAULT_BIAS_RADIUS_M` constants
- `_call_google_places_text_v1` — sibling of `_call_google_places_v1`, uses `locationBias.circle` (soft) not `locationRestriction.circle` (hard)
- `_read_text_cache`, `_write_text_cache` — sibling helpers for searchText cache, partial UNIQUE-keyed
- `_parse_v1_anchors` — wraps `_parse_v1_to_pois` and zeros dist_m per Gemini Directive 1
- `get_anchors_for_text(text_query, cur, *, bias_lat, bias_lng, bias_radius_m=50000.0) -> POILookupResult`
- `POILookupResult.source` extended to `{semantic_cache_hit, semantic_api_call, semantic_api_error}`
- **Defensive fix:** `_read_cache` SQL gained `AND pc.text_query IS NULL` to isolate legacy searchNearby from §XVII searchText rows
- **L-5 hygiene:** trailing newline added to a file that was committed without one in `3bc2cf6`

**Patch 2 (`53814b0`):** `where_am_i.py`
- `_SEMANTIC_TYPE_HORIZON_MAP` (12 entries: airport=800→1000 in Patch 3) + `_SEMANTIC_DEFAULT_HORIZON_M=500.0`
- `_signal_semantic_anchor(anchors, horizon_map=None, default_horizon=None) -> tuple[float, Optional[str]]` — pure-Python signal function, linear decay
- `MatchOutcome` extended with `semantic_anchor_score: Optional[float] = None`, `semantic_anchor_witness: Optional[str] = None`
- Test `test_matchoutcome_has_12_fields` renamed to `has_14_fields` with positive membership assertions for both new fields

**Patch 3a (`fb5ac91`):** plumbing
- `pudo_types.py`: `TargetSpec.address: Optional[str] = None` (5th field on a frozen dataclass)
- `driver_heartbeat.py`: `_bucket_to_target_spec` populates `address=address_text` in all 4 TargetSpec construction sites
- `where_am_i.py`: `_fetch_cluster_anchors` helper added to evaluate() (initially passed cluster centroid as bias)
- `where_am_i.py`: dispatch line passes `anchors=cluster_anchors` alongside `pois=cluster_pois`
- `where_am_i.py`: all 5 matchers (`_match_intersection`, `_match_single_road`, `_match_number_on_street`, `_match_apartment_complex`, `_match_poi_stub`) accept `anchors: Optional[list] = None` kwarg
- `tests/test_where_am_i.py`: 5 lambda fakes + 1 named tracking_matcher gain `**kwargs`. TestClassDispatchContract renamed to test_every_dispatch_matcher_accepts_pois_and_anchors_kwargs and asserts both kwargs.

**Patch 3 Full (`35e2e44`):** the Semantic Anchor Engine
- Airport horizon 800m → 1000m per Gemini ratification (IAH-centroid case clears 0.40 floor at 0.50)
- `_HOUSTON_BIAS_LAT=29.7604`, `_HOUSTON_BIAS_LNG=-95.3698` constants
- Populator switched to **fixed market center bias** (NOT cluster centroid) — eliminates cache fragmentation, ~7x cache hit rate for high-volume venues
- `_build_outcome` signature gained `semantic_anchor_score` + `semantic_anchor_witness` kwargs, threaded to MatchOutcome
- 4 production matchers gain `sem_score, sem_witness = _signal_semantic_anchor(anchors or [])` before `_build_outcome` call
- **`_match_poi_stub` REPLACED by `_match_poi_class`** — first-class matcher for `address_class='poi'`, composes `max(Head 5, Head 4, Head 1)` per Gemini ratification, no more WARN log, no more matched=False short-circuit
- `_CLASS_DISPATCH['poi']` → `_match_poi_class`
- `TestMatchPoiStub` class DELETED entirely per Gemini Decision A ("no zombies in the cellar")
- Pytest: 609 passed, 5 skipped, 0 failed

### What's NOT yet in the trunk (Patch 4 and 5 scope)

- **Patch 4:** PDC forensic logging in `driver_heartbeat.py`. The §XVII data is computed and lives on MatchOutcome but doesn't reach the `pudo_decision_context` row. The drift bug above must be fixed as part of this work.

- **Patch 5:** Regression test suite at `tests/test_semantic_anchor.py`. Mandatory cases per the morning handoff:
  1. The IAH regression — score ≥0.40 for "United, Houston, Texas" anchors at IAH coords
  2. HOU edge case (xfail or skip — the William P. Hobby Airport ~4428m beyond 1000m horizon; tracked but not blocking)
  3. Cache round-trip — write text_query row, read back, verify TTL 365d and last_hit_at touch
  4. Per-type horizon selection — hospital=150m, airport=1000m, default=500m
  5. Negative score below floor — anchor at 600m with 500m horizon returns 0.0
  6. **Dentist preservation** — Andrew's catch from this session: cluster POI wins when no semantic anchor exists for address class != 'poi'. Test should verify Heads 1+4 fire correctly for office/dentist destinations.
  7. **Iron Curtain** — spatial cluster reads do NOT return semantic anchor rows. Synthetic searchText row at (29.76, -95.37), `get_pois_near_cluster` at same coords returns `[]`. Proves the `AND pc.text_query IS NULL` defensive fix.

---

## The IAH Replay Validation Plan

**This is the gold-standard validation case for §XVII.** Andrew explicitly
wants it done. The case:

- **Ride 7883**, accepted 2026-05-14 04:03 CDT
- Pickup: Camille Park Dr & Legacy Oaks Dr, Missouri City TX — fired 04:12:31
- **Dropoff: "United, Houston, Texas"** — NEVER fired, `wai_below_floor`
  at IAH arrest `(29.9869, -95.3350)` at 05:06 CDT
- Trip: 47.90 miles, $60.77
- Tier A backtest showed §XVII would produce score 0.824 against "United"
  anchor at 88m

**Replay sequence (after Patch 4 lands and is deployed):**

1. `scripts/replay_pudo.py` exists per session recon. Reconstruct
   offer 7883's dropoff leg evaluation.
2. The replay should produce a `pudo_decision_context` row with:
   - `matched_offer_id='7883'`
   - `poi_lookup_source='semantic_cache_hit'` (the Tier A backtest cached
     this anchor row earlier today — verify the row still exists)
   - `poi_match_score=~0.824`
   - `poi_top_names` containing `semantic_anchor:United/transportation_service (88m)` (or similar)
3. Save the PDC row as forensic evidence. Reference it in:
   - The Patch 4 commit message (validation case)
   - `CANONICAL_RULES_SECTION_XVII.md` (the documented validation case)
   - Patch 5's regression test fixture (synthetic anchor list mirroring
     what Google returned)

**If the cache row was evicted or the replay produces different anchors**
than the morning's Tier A run, the replay still validates §XVII works
end-to-end — just with a fresh API call (`semantic_api_call`) instead of
a hit. Either is a clean signal.

---

## Patches 4 and 5 — Detailed Plan

### Patch 4 — PDC Forensic Logging + Drift Fix (right thing)

**Scope:** 3 files. ~30 lines code. Tight but real.

**File 1: `where_am_i.py`**

(a) `MatchOutcome` gains `semantic_lookup_source: Optional[str] = None`
    (the 15th field). Update `test_matchoutcome_has_14_fields` → `_15_fields`
    with positive membership assertion.

(b) `_fetch_cluster_anchors` returns the `POILookupResult.source` along
    with the anchors. Current shape:

```python
def _fetch_cluster_anchors(target_addr):
    ...
    anchor_result = get_anchors_for_text(target_addr, ...)
    return cluster_anchors  # currently just the list
```

   New shape:

```python
def _fetch_cluster_anchors(target_addr):
    ...
    anchor_result = get_anchors_for_text(target_addr, ...)
    raw_source = anchor_result.source if anchor_result else None
    return cluster_anchors, raw_source
```

(c) Dispatch site updates:

```python
cluster_anchors, semantic_source = _fetch_cluster_anchors(
    getattr(target, "address", None),
)
outcome = matcher(
    cluster, topo, target,
    pois=cluster_pois, anchors=cluster_anchors,
    semantic_lookup_source=semantic_source,
)
```

(d) 5 matchers accept `semantic_lookup_source: Optional[str] = None` kwarg,
    pass it through to `_build_outcome`.

(e) `_build_outcome` accepts the kwarg, passes to MatchOutcome.

(f) **TestClassDispatchContract** updated to also accept the new kwarg.
    Test name should evolve again: `test_every_dispatch_matcher_accepts_pois_anchors_and_source_kwargs`.

**File 2: `driver_heartbeat.py`**

Single INSERT update (around line 1044-1056). The drift fix:

```python
# §XVII Patch 4: forensic logging for Head 5 semantic anchor.
# When Head 5 wins, surface its data in the existing PDC columns.
# Drift fix: poi_lookup_source now holds the actual source string,
# NOT a witness (which was the pre-Patch-4 bug — column had been
# 100% NULL in production for 7 days, so no migration needed).

if top_outcome and top_outcome.semantic_anchor_score is not None and \
   top_outcome.semantic_anchor_score >= (top_outcome.poi_match or 0.0):
    # Head 5 won
    pdc_poi_lookup_source = top_outcome.semantic_lookup_source
    pdc_poi_match_score = top_outcome.semantic_anchor_score
    pdc_poi_top_names = (
        [top_outcome.semantic_anchor_witness]
        + (diagnostics.cluster_poi_names or [])
    )
else:
    # Head 1 won or no signal — preserve cluster_poi_names diagnostic
    pdc_poi_lookup_source = None  # TBD: Head 1 doesn't surface its source
    pdc_poi_match_score = top_outcome.poi_match if top_outcome else None
    pdc_poi_top_names = diagnostics.cluster_poi_names or None
```

**Important:** `top_outcome.poi_witness` (the Head 1 witness string)
currently goes nowhere usable after this fix. It used to drift into
`poi_lookup_source` where it was a category mistake. After Patch 4,
it lands in... ?

Options for the Head 1 witness:
- **Leave it unbound for now.** Add TODO comment. A follow-up patch
  adds a dedicated `poi_witness` column or writes it to `match_signal`.
  This is OK because production data for Head 1 witness is 65 rows
  in 7 days — negligible, and they were misfiled anyway.
- **Bind to `match_signal`.** But `match_signal` is currently bound to
  something else (recon needed — see line ~1075 of `driver_heartbeat.py`).
  Don't disturb without understanding what.

**My recommendation: leave Head 1 witness unbound with TODO comment.**
Don't expand Patch 4 scope further. A follow-up "PDC witness cleanup"
patch handles it cleanly.

**File 3: `tests/test_where_am_i.py`**

- Update `test_matchoutcome_has_14_fields` → `_15_fields`, add
  `semantic_lookup_source` to membership assertions
- Update TestClassDispatchContract test name and probe to include new kwarg
- Update 5 lambda fakes and 1 tracking_matcher to swallow new kwarg via
  the existing `**kwargs` (already in place from Patch 3a — verify)

**Pytest baseline after Patch 4: 609 passed, 5 skipped, 0 failed.**

### Patch 5 — Regression Test Suite

**Scope:** 1 new file. `tests/test_semantic_anchor.py`. Per §XIV.J live-PG
`db_cur` fixture pattern (SAVEPOINT-isolated real-PG).

The 7 mandatory test cases listed above in the "What's NOT yet in the
trunk" section. The IAH case becomes the canonical fixture (use the
replay output as the data source — what Google actually returned for
"United, Houston, Texas" gets pickled into the test as a synthetic
anchor list).

**Pytest baseline after Patch 5: 617+ passed (609 + 7 new + xfail).**

---

## Auxiliary Findings From This Session

### 1. The `atjb` user auth issue

Pytest occasionally shows:

```
FATAL: password authentication failed for user "atjb"
```

This is unrelated to §XVII. The `atjb` Postgres user's password lives
in Google Secrets per Andrew. Tests that need it should fetch the
secret; the connection-pool warning fires when tests bypass that path.
Not blocking — the `-x` runs that hit it eventually proceed when the
failing matcher's lambda fakes are fixed (tested in this session).

If Patch 4 surfaces persistent auth failures, fetch the secret from
Google Secrets Manager before pytest. Andrew has the credential path.

### 2. The L-5 hygiene defect in `poi_service.py`

`poi_service.py` was committed in `3bc2cf6` without a trailing newline.
Patch 1's apply script normalized it (added the newline) as part of
its work, with the rationale captured in the commit message. Future
patches to `poi_service.py` don't need to worry about this.

### 3. Two test classes had their names evolved this session

- `TestWitnessWiring::test_matchoutcome_has_12_fields` → `has_14_fields`
  (Patch 2)
- `TestClassDispatchContract::test_every_dispatch_matcher_accepts_pois_kwarg`
  → `_pois_and_anchors_kwargs` (Patch 3a)

If Patch 4 adds another field/kwarg, evolve the names again. Per Gemini's
"no zombies in the cellar," explicit invariants are preferable to
forward-compat magic. The method name IS the invariant.

### 4. `market_slug` column for `poi_cache` — REJECTED twice

Gemini proposed it twice in this session. I rejected both times. Andrew
sided with rejection. Don't revisit unless multi-market expansion is
actively imminent code (not speculative).

### 5. Bias center decision — fixed market, NOT cluster centroid

This was switched in Patch 3 Full. Don't accidentally revert to
cluster-centroid bias in any populator refactor — it would re-fragment
the cache. The constants `_HOUSTON_BIAS_LAT` and `_HOUSTON_BIAS_LNG`
in `where_am_i.py` are the canonical bias.

### 6. Bug 1 (silent NULL writer) — still parked

Per the morning handoff, Bug 1 (the silent NULL writer to
`current_offer_id`) was explicitly parked behind §XVII. After Patch 5
lands and the IAH replay validates, the original sequence resumes:

1. Re-enable statement logging (`ALTER DATABASE puddlejumper SET log_statement = 'mod'`)
2. Bump Cloud Run env-var to refresh connection pool
3. Drive (any duration, any route) to capture
4. **REVERT statement logging IMMEDIATELY after capture** — the morning
   handoff said this and was ignored, journal filled, broke unrelated
   queries
5. Diagnose and fix Bug 1

---

## What To Do FIRST in the New Session

1. **Read this handoff in full.** Don't skim. The "right thing not easy
   thing" mandate is the entire reason for stopping at Patch 3.

2. **Read `docs/CANONICAL_RULES_SECTION_XVII.md`** — the canonical doc
   was drafted but never appended to `docs/CANONICAL_RULES.md`. After
   Patch 5 lands, send it to Gemini for final ratification and append.
   Note the canonical doc needs updates to reflect Patch 3 decisions:
   - §C: airport/international_airport horizon 1000m (was 800m)
   - §G: `_match_poi_class` is the 5th matcher head
   - §K: add "No more stubs" discipline rule
   - Bias center: fixed market center, not cluster centroid

3. **Run `git log --oneline -5` and verify HEAD is `35e2e44`.** If
   anything is different, recon what changed before proceeding.

4. **Run pytest as baseline:** expect 609 passed, 5 skipped, 0 failed.
   If anything is broken, fix that before adding more changes.

5. **Authoring order:**
   - Patch 4 (drift fix included) — the right thing, ~3 files, ~30 lines
   - Push Patch 4
   - **Replay ride 7883 dropoff** — produces gold PDC row, save as forensic
   - Patch 5 (regression test suite) using the replay output as fixture
   - Push Patch 5
   - Canonical doc updates → Gemini ratification → append to CANONICAL_RULES.md
   - Then return to Bug 1

6. **The Gemini loop applies.** Patches 4 and 5 propose → Gemini reviews →
   Andrew ratifies → execute. Especially important for Patch 4's drift
   fix because the "right thing" path is opinionated and Andrew explicitly
   instructed not to take shortcuts.

---

## What Went Well This Session

- 4 patches landed cleanly with disciplined L-3 envelope (Phase 1 verify,
  Phase 2 in-memory transform, Phase 3 atomic write + read-back)
- Recon-first discipline prevented multiple potential bugs (the matcher
  signature mismatch, the missing `_match_poi_stub` in `_CLASS_DISPATCH`
  contract test, the boundary detection in `_replace_poi_stub`)
- The L-22 paste-back issues were managed (the second §XVII patch run
  showed up as a SKIP because of idempotency — saved by the apply
  script's defensive design)
- The dentist/office preservation concern Andrew raised mid-Patch-3 was
  immediately incorporated into the test plan for Patch 5
- The architectural pushback on Gemini (`market_slug`, `cluster-centroid
  vs fixed bias`) led to better decisions
- `TestMatchPoiStub` deletion was decisive and correct — no zombies

## What To Watch For

- **The drift fix in Patch 4 is opinionated.** Don't shortcut it.
- **Patch 4 adds a 15th MatchOutcome field.** The TestMatchOutcomeHas
  invariant test must update. Don't forget.
- **Don't re-litigate decisions from this session.** Bias center, horizon
  numbers, composition strategy, `market_slug` rejection — all settled.
  If genuinely new information arrives, surface it, but don't second-guess.
- **Context budget for the new session: Patches 4 + 5 + replay + doc
  updates is realistic if disciplined.** Tight if anything blows up.
  Plan accordingly.

---

End of handoff.
