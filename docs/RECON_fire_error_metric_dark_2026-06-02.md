# Recon + Fix Proposal — `offer_history` error-metric writer dark since 2026-04-30

**To:** Andrew (decision), Gemini (review), then CC (apply)
**From:** Claude
**Date:** 2026-06-02
**Class:** P0-class launch finding — silent accuracy blind spot, 5 weeks
**Discovered:** parallel recon during the 291-hotfix wait, AM-drive 2026-06-02 forensic

---

## 1. Finding

`app_private.offer_history.pickup_error_m` and `dropoff_error_m` — the per-fire
distance between the **intended** geocode and the **fired** (actual) coordinate —
have not been written since **2026-04-30 09:45 UTC**.

Evidence chain (all read-only, this session):

1. **Today's drive:** all 12 fires show `pickup_error_m` / `dropoff_error_m` =
   NULL, while `pickup_classification` (`auto`/`auto_observation`),
   `pickup_data_source` (`nail_it`) populate normally. The fire works; the error
   measurement does not.
2. **Whole-table:** of 2,583 rows, only **167** have `pickup_error_m`, **100**
   have `dropoff_error_m`; `MAX(created_at) WHERE pickup_error_m IS NOT NULL` =
   **2026-04-30**. Nothing since.
3. **Writer search:** no live `.py`/`.sql` assigns the bare `pickup_error_m` /
   `dropoff_error_m`. The only non-`nailed_` references are **reads** in
   `nail_it_core.py:128–130` (`COUNT(*) FILTER (WHERE dropoff_error_m <= 800)`,
   `AVG(dropoff_error_m)`). (The many `nailed_*_error_m` hits are a different
   column on `driver_trip_state`, not this one.)
4. **Fire-site read:** all four `offer_history` fire UPDATEs
   (`driver_heartbeat.py:361` FirePickup, `:662` FPO, `:472` FireDropoff,
   `:771` FDO) confirmed — none sets the error column in any SET clause.

## 2. Root cause (date-correlated, high confidence — not proven)

Last write = 2026-04-30, the last drive **before** the state-machine demolition
(May 1–5: `pudo_planner.py` deleted, `driver_heartbeat.py` rewritten to the
5-stage pipeline, net −1,555 lines). The error computation lived in the cut path
and was not re-wired into the new fire UPDATEs. No test caught it — a NULL column
raises nothing. Stated as a strong lead, not adjudicated fact; the demolition
diff would confirm.

## 3. Impact — why this is launch-class, not cosmetic

- **The system cannot see its own drift.** Today's road-observed misses (8845
  pickup ~1mi at a library; 8855 ~0.5mi at a Holcomb light; a ~2.5mi dropoff)
  are invisible in the data — `pickup_error_m` is NULL for every one. There is no
  signal, no threshold, no self-correction, because the metric is never computed.
- **`nail_it_core.py` reports a frozen-in-April number.** Its accuracy stats
  (`AVG(dropoff_error_m)`, `% <= 800m`) average 167 pre-demolition rows and
  silently present them as current. Anyone reading that dashboard sees a healthy
  lie.
- **Downstream dependents are starved:** §4.6 (GC spatial gate) needs per-fire
  error to know it has a problem; drift detection cannot exist without it; the
  8845-class silent miss (blank `wai_offer_id`, no bind, no error logged) is
  undetectable on every channel precisely because this one is dark.

## 4. Fix — restore the computation in the new pipeline

The fix is clean because both inputs are already in scope at each UPDATE:
- fired point = `(nail_lat, nail_lng)` (already a bound param at all four sites)
- intended point = the row's own `pickup_lat`/`pickup_lng` (pickup sites 361,
  662) or `dropoff_lat`/`dropoff_lng` (dropoff sites 472, 771) — columns on the
  row being updated, usable inline in the SET.

Add one SET term per site:
- **361, 662:** `pickup_error_m = <distance>(pickup_lat, pickup_lng, %s, %s)`
- **472, 771:** `dropoff_error_m = <distance>(dropoff_lat, dropoff_lng, %s, %s)`
  with `(nail_lat, nail_lng)` supplied (guard NULL intended coords → NULL error,
  not a crash).

No new SELECT, no coordinate re-fetch. Per the canonical coordinate rules, the
distance MUST use the project's blessed helper, **not** raw `ST_MakePoint` /
hand-rolled haversine.

## 5. RESOLVED — helper is Python-only; per-site footprint locked

Settled by grep 2026-06-02:
- **No SQL distance function exists** in `migrations/` (empty result).
- The blessed helper is **Python**: `def haversine_meters(lat1, lng1, lat2, lng2)`
  at **`where_am_i.py:307`** (used internally at `:359`). `where_am_i` is already
  in the heartbeat import path, so the helper is in scope at all four fire sites
  with no new dependency. (The `_haversine_meters` hits elsewhere are apply-script
  / doc noise, not the live function.)
- **`arc_band.py` is fully deleted** — `ls` returns nothing, zero live imports.
  Gemini's §5.2 example naming `arc_band.py` pointed at a removed module; the real
  helper is `where_am_i.py:307`.

**Patch shape (canon-compliant): compute in Python, None-guarded, pass as bound
param.** A SQL inline is NOT available (no SQL function) and a hand-rolled SQL
haversine is forbidden by the canonical coordinate rules — so each site computes
`haversine_meters(intended_lat, intended_lng, nail_lat, nail_lng)` in the handler
(or `None` if intended coords are NULL) and adds it as a SET column + bound param.

**Intended coords are NOT currently in handler scope at 3 of 4 sites** (confirmed
by reading each site's pre-UPDATE SELECTs). Per-site footprint:

| Site | Fire | Intended-coord availability | Work |
|---|---|---|---|
| 361 | FirePickup | catch-up SELECT at 277 fetches only `actual_pickup_at` | **widen** that SELECT to `+ pickup_lat, pickup_lng` (free, no new round-trip) |
| 662 | FPO | no pre-UPDATE SELECT | add `SELECT pickup_lat, pickup_lng` |
| 472 | FireDropoff | only an unrelated `SELECT decision_log_id` | add `SELECT dropoff_lat, dropoff_lng` |
| 771 | FDO | only an unrelated `SELECT decision_log_id` | add `SELECT dropoff_lat, dropoff_lng` |

**Design questions — both RESOLVED:**

1. `RETURNING` rejected (Gemini-concur): the haversine runs in Python *before* the
   error can be written, so `RETURNING` would force a second UPDATE to backfill —
   a double round-trip. Use SELECT-then-compute-then-UPDATE.

2. **In-memory read vs pre-fetch — LOCKED to pre-fetch (unconditional).** The
   snapshot SELECT (`driver_queue.py:769`) *does* fetch `pickup_lat/lng`,
   `dropoff_lat/lng`, but feeds them through `self._build_target_spec(...)` —
   which is an **injected callable** (`driver_queue.py:453`,
   `self._build_target_spec = target_spec_builder`), not a fixed method. Whether
   the resulting `Offer` (`pudo_types.py:80`) exposes raw readable coords depends
   on which builder the caller wires in. That injection-dependence makes the
   in-memory read unsafe to *assume* from static reads. Therefore the proposal
   commits to the pre-fetch footprint, which is correct regardless of wiring:

   | Site | Fire | Pre-fetch action |
   |---|---|---|
   | 361 | FirePickup | widen existing catch-up SELECT (277) → `+ pickup_lat, pickup_lng` (free) |
   | 662 | FPO | add `SELECT pickup_lat, pickup_lng FROM offer_history WHERE id=%s` |
   | 472 | FireDropoff | add `SELECT dropoff_lat, dropoff_lng FROM offer_history WHERE id=%s` |
   | 771 | FDO | add `SELECT dropoff_lat, dropoff_lng FROM offer_history WHERE id=%s` |

   The three added SELECTs are single-row primary-key lookups — negligible.

   **Optimization (CC may take at build time, NOT a blocker):** if CC confirms the
   heartbeat's queue snapshot exposes populated `offer.pickup_lat/lng` /
   `offer.dropoff_lat/lng` on the in-scope `Offer` for the specific construction
   used in `_execute_action`, it may read from memory and skip the 662/472/771
   SELECTs. This is verified against the live object at build time, never assumed
   from static reads. Pre-fetch is the default that ships if the check is
   inconclusive.

**Still-open dependency (must confirm at build, gates the optimization only, not
the fix): does `_execute_action` receive the `Offer` object or only
`action.offer_id`? If only the ID, the memory optimization is moot and pre-fetch
is mandatory. The pre-fetch footprint above does not depend on this answer.

**Data-contract ruling (DECIDED — do not reopen at build time):** the `Offer`
dataclass (`pudo_types.py:80`) MUST stay primitive. Do NOT extend it to carry raw
`pickup_lat/lng` / `dropoff_lat/lng` as native fields to serve this fix. Rationale:
(a) `Offer` is a frozen canonical type consumed across matcher/queue/heartbeat/
spec-builder; mutating a wide shared contract to save three microsecond PK lookups
is a disproportionate blast radius; (b) the injected `target_spec_builder` seam
exists to *decouple* offers from raw-coordinate handling — re-attaching raw coords
partially defeats it; (c) the error metric is a fire-time concern, the `Offer` is a
queue-projection concern — keep the lifecycles separate, fetch coords locally at
the fire site. The PK fetch is the correct price. Revisit ONLY if the heartbeat
ever becomes round-trip-bound (it is not — these are PK index scans) or if many
future fire-time computations need raw coords.

## 6. Process / sequencing

- **Independent of the 291 hotfix** — different columns, different concern. Do
  NOT braid into the 291 pass. Sequence AFTER 291 deploys.
- Own branch (e.g. `fix/restore-fire-error-metric-2026-06-02`).
- Regression test MUST run through the real `db_cur` (RealDictCursor) fixture and
  assert each of the four fire paths writes a non-NULL `*_error_m` equal to the
  helper's distance for known coords; and that NULL intended coords yield NULL
  error, not an exception.
- Backfill question (decide separately, do NOT bundle): the 2,416 rows since
  Apr 30 have the coords to compute error retroactively — a one-time UPDATE could
  recover the historical signal. Flag only; not part of the forward fix.

## 7. Verdict

A writer cut in the May-1 demolition has left PuddleJumper blind to its own
pickup/dropoff accuracy for 5 weeks, while a dashboard reads frozen April data as
live. This is the accuracy signal §4.6, drift detection, and silent-miss
diagnosis all depend on. Recommend: land after 291, on its own branch, with the
SQL-vs-Python helper question (§5) resolved first.
