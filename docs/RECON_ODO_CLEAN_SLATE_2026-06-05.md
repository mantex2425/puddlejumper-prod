# RECON — Odometer Clean-Slate Audit (Step 6 piece (i) post-commit)

**Date:** 2026-06-05 (after `aa4eada`, piece (i) liveness-predicate-on-band)
**Purpose:** Verify the odometer band is genuinely single-owner after piece (i) —
i.e. no surviving site computes an offer's distance envelope outside the
pudo_types band primitive — and surface any debt for piece (ii).
**Method:** Categorized grep separating (1) legit anchor writers, (2) retired
band-math, (3) the known piece-(ii) raw_min path, plus an import/execution check.
**Verdict:** Slate is clean. No bug. Band is single-owner. One separate-but-
adjacent quantity classified (logger.py chain horizon). Piece (ii) scope
tightened with executed-SQL evidence.

---

## 1. Band is single-owner — CONFIRMED

The only sites doing odometer-band math after piece (i):
- `driver_queue.py` LIVE_OFFER_PREDICATE_SQL — the new per-leg band clause
  (`GREATEST(0.15 * oh.pickup_miles, %s)` / `... trip_miles ...`), hand-written
  to match the primitive and pinned by `test_band_clause_matches_primitive`.
- `pudo_types.py` — `odometer_band` / `odometer_in_band` (the primitive itself).

No rogue band found. The `0.25` post-pickup re-anchor padding grep returned
EMPTY (the old re-anchor buffer is gone). No `GREATEST/LEAST` distance clamp
outside the new clause. No stray `0.15` band literal outside pudo_types/tad/
comments.

**Legit anchor writers (expected, NOT fingers):** tad.py
`compute_offer_expectations` (writes `expected_pickup_distance` /
`expected_dropoff_distance`); the pickup-fire re-anchor UPDATE. driver_heartbeat
reads (not writes) `expected_pickup_distance` at the OfferTadState build.

---

## 2. NO BUG — `GC_BUFFER_MULT` deletion was correct

During the audit a `GC_BUFFER_MULT` reference at `driver_queue.py:763` was
briefly flagged as a possible orphaned-reference NameError (piece (i) deleted the
definition). **This was a MISREAD and is retracted.** Verified:
- Line 763 is INSIDE the `_project_offers` docstring (def at 755, `"""` opens
  756, line 763 in the prose "Per-offer GC window" block, `"""` closes 775). It
  is documentation, not executable code, not an f-string expression.
- The executed SQL (`cur.execute(f"""` at 785+) does NOT reference
  `GC_BUFFER_MULT`. It selects `raw_min` as a passive column and filters via
  `AND {LIVE_OFFER_PREDICATE_SQL}` (the band).
- `import driver_queue` succeeds; `hasattr(driver_queue, 'GC_BUFFER_MULT')` is
  False. Only two occurrences exist: line 79 (RETIRED comment), line 763
  (docstring). Zero live Python references.

Piece (i) correctly deleted the definition. No fix needed. (Lesson: trust the
executed-code / import check over a raw grep line; a grep hit in a docstring is
not a live reader.)

---

## 3. `decisions/logger.py:218-219` chain horizon — SEPARATE quantity, NOT a finger

```
horizon_min = (rem_min + new_pickup_min) * 1.25
horizon_mi  = (rem_mi  + new_pickup_mi)  * 1.25
time_exceeded     = elapsed_minutes > horizon_min
distance_exceeded = distance_axis_available and elapsed_miles > horizon_mi
```

This is **stacked-offer chain-feasibility planning** — "given what's left of the
current offer (`rem_*` from prev_row) plus a new offer's pickup, has the driver
exceeded a planning horizon?" It is a DIFFERENT question from the liveness band
("has this offer's odometer overshot its leg"). It legitimately shares the `1.25`
multiplier as a coincidence of buffer choice, not because it is the same
calculation. **Not a finger to kill.**

NOTE: it is the LAST surviving `1.25` distance buffer after the band retired
its own. Future (non-band-scope) item: decide whether chain-planning should
align with the band's philosophy (per-leg %-of-distance) or keep its own flat
buffer. Recorded, not actioned.

---

## 4. Piece (ii) scope — TIGHTENED with executed-SQL evidence

The executed `_project_offers` SQL (785+) proves `raw_min` is a **passive SELECT
column**: `COALESCE(pickup_minutes, %s) + COALESCE(trip_minutes, %s) AS raw_min`
— computed and returned, but **never filtered on** (the WHERE uses the band).
The `window_min`/`GC_BUFFER_MULT` time-window the docstring describes is already
dead in the executed query. This CONFIRMS the §5.3 "vestigial" ruling with the
real SQL in hand.

Piece (ii) work, now precisely:
1. Evict the passive `raw_min` SELECT column (driver_queue `_project_offers`
   SELECT + the `GC_NULL_PICKUP_MIN, GC_NULL_TRIP_MIN` binds feeding it;
   logger.py:786 region binds the same).
2. Rewrite the `_project_offers` docstring (756-775) — it describes the retired
   `raw_min * GC_BUFFER_MULT` / `window_min` formula the method no longer runs.
   Documentation drift (RIDE_LIFECYCLE.md §3 class).
3. Delete `GC_NULL_PICKUP_MIN` / `GC_NULL_TRIP_MIN` definitions (only readers are
   raw_min at 744-745/786 + test imports at test_driver_queue.py:35-36 — remove
   those imports same-commit).
4. **OPEN GREP before authoring piece (ii):**
   `grep -rn "GC_MIN_MINUTES\|GC_MAX_MINUTES" --include="*.py" .` — confirm the
   MINUTES family (test-imported at test_driver_queue.py:33-34) has no surviving
   production reader before deleting.

---

## 5. Minor doc-hygiene (any-time sweep, not blocking)

- `driver_heartbeat.py:182` — comment references "driver_queue.GC_BUFFER_MULT"
  as "its sole reader." Stale (the constant is retired).
- `superpower_geo.py:62` — comment "(Trip * 1.25) + 2 miles padding." Different
  subsystem; verify it's not describing live band-adjacent math before any sweep.

---

## 6. What this audit changes about the work map

Nothing structural. It CONFIRMS the band is single-owner (the clean-slate
question), CLASSIFIES the logger.py horizon as separate, and adds the stale
`_project_offers` docstring to piece (ii)'s checklist. The remaining work is
unchanged: piece (ii) (now tighter), TAD `_evaluate_distance_gate` unification
onto the primitive (the last intended consumer to read the one owner), Step 5
orphan-trigger implementation, then merge-with-DSI deploy + Step 7 9132 replay.
