# Recon: Matcher Empty Candidates — Bind-Parameter Drift

**Date:** 2026-05-30
**Driver:** `UjT1hE9eBXh2q95aSZYOkzDJ8lo1`
**Deployed revision:** `puddlejumper-api-00634-gq8` (branch `phase-2c-2-geocode-signal`)
**Investigator:** Claude (server-side recon, read-only)
**Status:** Read-only investigation complete. No code edited. Output for Gemini review.

---

## VERDICT

**(1) Bind-parameter drift — CONFIRMED at static level + runtime-proven for one offer at the 12:22:38 starved pickup.**

The matcher's queue-projection path (`driver_heartbeat.py:1413` → `DriverQueue.snapshot()` → `_project_offers`) passes `current_cumulative_miles=<real value>` to `LIVE_OFFER_PREDICATE_SQL`, which **activates the distance gate**. The Monitor's queue-projection path (`driver_status.py:188` → `DriverQueue.offer_ids_only()` with no kwargs) passes `current_cumulative_miles=None`, which **short-circuits the distance gate to permissive** (first clause of the OR: `%s::numeric IS NULL`).

At the 12:22:38 starved pickup with `cumulative_miles = 10.66 mi`:
- **Offer 8575** (envelope = 9.885 mi): matcher gate `10.66 < 9.885 = FALSE` → **REJECTED by matcher**, **kept by Monitor** (NULL bind → permissive).
- **Offer 8576** (envelope = 23.325 mi): matcher gate `10.66 < 23.325 = TRUE` → **survives both**.

So the matcher's queue at 12:22:38 contained {8576}; Monitor's queue contained {8575, 8576}. The bug is real and demonstrably eats offers the Monitor still displays.

**One important caveat (Section D below):** the matcher's `matcher_candidates` at 12:22:38 is `{}` — even offer 8576, which survives the distance-gate bind drift, did not emerge as a candidate. That implies a **second** factor (WAI substantive scoring < `WAI_CONFIDENCE_THRESHOLD=0.40`, or a TAD-bouncer skip inside `_evaluate`) is also contributing to the empty matcher_candidates. The bind drift is a real bug but is **not the complete picture** for why every pickup was starved. The user-noted "matcher_candidates='{}'" at five pickups is partially explained by verdict (1) and partially by downstream WAI/TAD behavior that this recon did not investigate further.

**Secondary latent bug discovered:** `DriverQueue.snapshot()` at `driver_queue.py:370` accepts `last_odometer_move_at` as a kwarg but **silently drops it** when calling `_project_offers`. The matcher passes a real value through `snapshot()`, but it never reaches `live_offer_predicate_params`. Result: the odometer-staleness gate is permissive for both the matcher and the Monitor — i.e., the matcher is *less restrictive than intended*, not more. This is NOT the cause of empty matcher_candidates (it makes the matcher accept more, not less), but it's a separate kwarg-drop bug worth flagging.

---

## A. The Matcher's Read Query

**The matcher does not run its own SQL query against `offer_history`.** It consumes the queue snapshot built upstream by `DriverQueue.snapshot()` and iterates `diagnostics.per_target_outcomes` from `WhereAmI.evaluate_with_diagnostics`.

### A.1 Where matcher_candidates is populated

**File:** `driver_heartbeat.py:1612-1663`

```python
        for offer_id, leg, outcome in diagnostics.per_target_outcomes:
            if leg not in ('pickup', 'dropoff'):
                continue

            # WAI confidence floor: the canonical match signal per
            # §XVI.C (amended 2026-05-22). TAD verdict is consulted
            # only for forensic recording in tad_decision_context;
            # it does not gate candidates here.
            if outcome is None or outcome.confidence < WAI_CONFIDENCE_THRESHOLD:
                continue

            candidates.append((offer_id, leg, outcome.confidence))
        ...
        # §XVI.G Transaction Lock: filter candidates ... (lines 1636-1661)
        ...
        matcher_candidates = [c[0] for c in candidates]
```

`WAI_CONFIDENCE_THRESHOLD = 0.40` (per `pudo_types.py` constant, confirmed by `tests/test_where_am_i.py:2254 assert WAI_CONFIDENCE_THRESHOLD == 0.40`).

### A.2 The actual SQL read (via snapshot → _project_offers)

**File:** `driver_heartbeat.py:1413`

```python
snap = queue.snapshot(cur, current_cumulative_miles=cumulative_miles, last_odometer_move_at=effective_last_move)
```

**File:** `driver_queue.py:343, 370` (`snapshot()`):

```python
def snapshot(self, cur, current_cumulative_miles=None, last_odometer_move_at=None) -> QueueSnapshot:
    ...
    offers = self._project_offers(cur, current_cumulative_miles=current_cumulative_miles)
    raw_bound = self._select_bound_offer_id(cur)
    ...
```

**⚠️ `snapshot()` accepts `last_odometer_move_at` but does NOT forward it to `_project_offers` at line 370.** This is the secondary latent bug.

**File:** `driver_queue.py:534, 563-583` (`_project_offers`):

```python
def _project_offers(self, cur, current_cumulative_miles=None, last_odometer_move_at=None) -> tuple[Offer, ...]:
    ...
    cur.execute(f"""
        SELECT
            id, pickup_address, dropoff_address, ...
        FROM app_private.offer_history oh
        WHERE decision_log_id IN (
            SELECT id FROM app_private.decision_log WHERE driver_id = %s
        )
          AND {LIVE_OFFER_PREDICATE_SQL}
        ORDER BY created_at DESC
    """, (
        GC_NULL_PICKUP_MIN, GC_NULL_TRIP_MIN,
        self.driver_id,
    ) + live_offer_predicate_params(current_cumulative_miles, _now(), last_odometer_move_at))
```

### A.3 Source of each bind value (matcher path)

| Bind | Source | Effective Value |
|---|---|---|
| `current_cumulative_miles` | `driver_heartbeat.py:1413` passes `cumulative_miles` (real heartbeat value) | **REAL** (10.66 mi at 12:22:38) |
| `reference_time` | `_now()` inside `live_offer_predicate_params` — fresh `datetime.now(timezone.utc)` | real |
| `last_odometer_move_at` | passed by caller but DROPPED by `snapshot()` at line 370 — `_project_offers` receives `None` | **NONE** (silently lost) |

---

## B. The Queue Projection (Monitor baseline)

**File:** `driver_status.py:187-189`

```python
"planner_queue": list(
    DriverQueue(driver_id).offer_ids_only(cur)
),
```

**File:** `driver_queue.py:401-424` (`offer_ids_only`):

```python
def offer_ids_only(self, cur, current_cumulative_miles=None, last_odometer_move_at=None) -> tuple[str, ...]:
    ...
    cur.execute(f"""
        SELECT id::text AS offer_id
        FROM app_private.offer_history oh
        WHERE decision_log_id IN (
            SELECT id FROM app_private.decision_log WHERE driver_id = %s
        )
          AND {LIVE_OFFER_PREDICATE_SQL}
        ORDER BY created_at DESC
    """, (
        self.driver_id,
    ) + live_offer_predicate_params(current_cumulative_miles, _now(), last_odometer_move_at))
```

`offer_ids_only` correctly forwards `last_odometer_move_at` to `live_offer_predicate_params`. It does NOT have the kwarg-drop bug.

### B.1 Source of each bind value (Monitor path)

| Bind | Source | Effective Value |
|---|---|---|
| `current_cumulative_miles` | `driver_status.py:188` passes NOTHING → default `None` | **NONE** |
| `reference_time` | `_now()` — fresh `datetime.now(timezone.utc)` | real |
| `last_odometer_move_at` | `driver_status.py:188` passes NOTHING → default `None` | **NONE** |

---

## C. Three-Way Side-by-Side

| Component | Matcher (driver_heartbeat) | Monitor (driver_status) | Differ? |
|---|---|---|---|
| Table | `app_private.offer_history` | `app_private.offer_history` | NO |
| WHERE clause | `{LIVE_OFFER_PREDICATE_SQL}` | `{LIVE_OFFER_PREDICATE_SQL}` | NO (textually identical, enforced by `test_offer_ids_only_and_project_offers_share_where_clause`) |
| Bind: `current_cumulative_miles` | **REAL value** (e.g., 10.66 at 12:22:38) | **NULL** (default) | **YES** ⚠ |
| Bind: `reference_time` | `_now()` (per-call) | `_now()` (per-call) | NO (semantically equivalent — both are "now") |
| Bind: `last_odometer_move_at` | **NULL** (dropped by snapshot bug) | **NULL** (default) | NO (both NULL by different paths) |

**The single material divergence is `current_cumulative_miles`.** The matcher activates the distance gate; the Monitor does not.

### C.1 The distance gate predicate (driver_queue.py:218-236)

```sql
AND (
    %s::numeric IS NULL                            -- current_cumulative_miles
    OR oh.miles_at_offer_receipt IS NULL
    OR %s::numeric < (                             -- current_cumulative_miles
        CASE
            WHEN oh.actual_pickup_at IS NOT NULL
                 AND oh.expected_dropoff_distance IS NOT NULL THEN
                oh.expected_dropoff_distance + oh.trip_miles * 0.25
            ELSE
                oh.miles_at_offer_receipt + LEAST(
                    GREATEST(
                        (COALESCE(oh.pickup_miles, %s) + COALESCE(oh.trip_miles, %s)) * %s,  -- GC_BUFFER_MULT
                        %s                                                                     -- GC_MIN_DIST_MI
                    ),
                    %s                                                                         -- GC_MAX_DIST_MI
                )
        END
    )
)
```

When `current_cumulative_miles IS NULL` (Monitor path), the first OR clause is TRUE → entire AND passes → distance gate is permissive.

When `current_cumulative_miles IS NOT NULL` (matcher path), first clause is FALSE; second clause depends on `miles_at_offer_receipt`; third clause compares the real cumulative_miles against the envelope.

### C.2 Constants used (driver_queue.py:66-87)

```python
GC_NULL_PICKUP_MI = 4.0
GC_NULL_TRIP_MI = 8.0
GC_MIN_DIST_MI = 2.0
GC_MAX_DIST_MI = 50.0
GC_BUFFER_MULT = 1.25
```

Envelope formula (pre-pickup-fire): `miles_at_offer_receipt + LEAST(GREATEST((pickup_miles + trip_miles) * 1.25, 2.0), 50.0)`

---

## D. The Runtime Bind Values at 12:22:38 (The Proof Step)

### D.1 Heartbeat at 12:22:38 CT (17:22:38 UTC)

Queried `app_private.heartbeat_log` for the driver at the pickup window:

```
       logged_at (CT)      |  cumulative_miles
---------------------------+--------------------
 2026-05-30 12:22:31.333649 | 10.656775031591442
 2026-05-30 12:22:36.720313 | 10.656775031591442
 2026-05-30 12:22:42.493363 | 10.656775031591442
 2026-05-30 12:22:48.174034 | 10.656775031591442
 2026-05-30 12:22:53.880307 | 10.656775031591442
```

**Matcher's `current_cumulative_miles` bind at 12:22:38 = `10.66 mi`** (stationary — driver at pickup, not moving).

### D.2 Offers in queue at 12:22:38

Queried `app_private.offer_history` with the **Monitor's permissive predicate** (NULL binds) at `reference_time = 17:22:38 UTC`:

```
  id  |   created_ct (CT)         | miles_at_offer_receipt | pickup_miles | trip_miles | app_verdict
------+---------------------------+------------------------+--------------+------------+-------------
 8575 | 2026-05-30 12:05:06.502272|                   0.01 |         4.70 |       3.20 | DECLINE
 8576 | 2026-05-30 12:11:10.58465 |                   4.70 |         5.80 |       9.10 | ACCEPT
```

Only TWO offers at this pickup. Both NULL `actual_pickup_at` and NULL `actual_dropoff_at` (predicate-alive). (The user's "offers 8575-8585" refers to the cumulative state later in the drive — by 13:51 there were 11. At 12:22:38 the queue was just {8575, 8576}.)

### D.3 Computed distance-gate envelopes

**Offer 8575:**
- `miles_at_offer_receipt = 0.01`, `pickup_miles = 4.70`, `trip_miles = 3.20`
- `(4.70 + 3.20) * 1.25 = 9.875`
- `LEAST(GREATEST(9.875, 2.0), 50.0) = 9.875`
- envelope = `0.01 + 9.875 = 9.885 mi`
- matcher gate: `10.66 < 9.885 = FALSE` → **REJECTED by matcher**
- Monitor gate: `NULL` first-clause TRUE → **PASSES Monitor**

**Offer 8576:**
- `miles_at_offer_receipt = 4.70`, `pickup_miles = 5.80`, `trip_miles = 9.10`
- `(5.80 + 9.10) * 1.25 = 18.625`
- `LEAST(GREATEST(18.625, 2.0), 50.0) = 18.625`
- envelope = `4.70 + 18.625 = 23.325 mi`
- matcher gate: `10.66 < 23.325 = TRUE` → **PASSES matcher**
- Monitor gate: NULL first-clause TRUE → **PASSES Monitor**

### D.4 Observed matcher_candidates at 12:22:38

Queried `app_private.pudo_decision_context` for the heartbeats in that window:

```
   id   |          hb_ct           | matcher_candidates |        unmatched_reason
--------+--------------------------+--------------------+-------------------------------
 336150 | 2026-05-30 12:22:31.333  | {}                 | cluster_unavailable
 336151 | 2026-05-30 12:22:36.720  | {}                 | lost_mode_no_candidate
 336152 | 2026-05-30 12:22:42.493  | {}                 | lost_mode_no_candidate
 336153 | 2026-05-30 12:22:48.174  | {}                 | lost_mode_no_candidate
 336154 | 2026-05-30 12:22:53.880  | {}                 | lost_mode_no_candidate
```

**Bind drift proven for offer 8575.** Matcher's snap.offers excludes 8575 because of distance gate; matcher_candidates therefore cannot contain 8575. Monitor's queue includes 8575 because of NULL binds. This is the bind-drift bug, runtime-confirmed.

### D.5 Unexplained residual — why isn't 8576 in matcher_candidates?

Offer 8576 should survive the matcher's distance gate (`10.66 < 23.325`). It should reach WAI. WAI should score it. If confidence ≥ 0.40, it should be in matcher_candidates.

Yet `matcher_candidates = {}` and `unmatched_reason = lost_mode_no_candidate`.

Per the unmatched_reason cascade (`driver_heartbeat.py:1714-1763`), `lost_mode_no_candidate` fires when:
- `arrest_counter_s_post >= ARREST_DURATION_THRESHOLD_S` (entered the candidate-build block)
- `len(candidates) == 0` (no candidate passed WAI floor + lock filter)
- `diagnostics.tad_verdicts` is non-empty (so the snapshot was non-empty — WAI did run)
- `lost_mode = True`

So at 12:22:36+ the matcher's snap.offers was non-empty (consistent with 8576 surviving the distance gate), WAI ran, but no candidate cleared the 0.40 confidence floor. Possible explanations for offer 8576 specifically failing WAI scoring at the 12:22:38 pickup:
1. WAI cluster center was not near offer 8576's pickup coords → low spatial-scoring confidence
2. TAD-bouncer skip at `where_am_i.py:2178-2185` (`verdict.passed is False` → skipped before scoring)
3. Some other matcher-substantive reason

**These are downstream WAI/TAD behaviors, not bind-drift. They are out of scope for this recon (which targeted the queue/matcher-read drift) but the user should be aware that fixing the bind-drift bug alone may not restore matcher_candidates at 12:22:38 — offer 8575 would be added back, but 8576 might still fail WAI scoring.**

### D.6 The four other starved pickups

```
  hb_ct (CT)                |  cumulative_miles
---------------------------+--------------------
 2026-05-30 12:22:03.50733 | 10.644964411323576
 2026-05-30 12:40:04.04274 | 20.285047519371062
 2026-05-30 12:57:01.88035 |   25.2159234410169
 2026-05-30 13:47:01.13334 |  59.55972332378626
 2026-05-30 13:59:04.25167 |  65.19224924714021
```

By 13:47 (cumulative_miles=59.56) and 13:59 (cumulative_miles=65.19), the matcher's distance gate would have rejected far more offers than at 12:22:38. The Monitor's `offer_ids_only` with NULL binds would still display them as long as they're within the 4-hour abandonment ceiling. The user's observation of "queue 8575-8585 stacked while matcher empty" is consistent with bind-drift growing more severe over the course of the drive as `cumulative_miles` accumulates.

---

## E. Post-Fetch Filter Check

Two post-snapshot filters exist on the matcher path, both verified to NOT be the cause at 12:22:38:

### E.1 §XVI.G Transaction Lock filter (driver_heartbeat.py:1636-1661)

```python
for offer_id, leg, conf in candidates:
    lock_ctx = is_locked(...)
    if lock_ctx is not None:
        suppressed_contexts.append(lock_ctx)
    else:
        _unlocked_candidates.append((offer_id, leg, conf))
candidates = _unlocked_candidates
```

When this filter drops candidates, `unmatched_reason` is set to `lock_suppressed` (line 1713). At 12:22:38, unmatched_reason was `lost_mode_no_candidate`, NOT `lock_suppressed` — so the lock filter is not the cause.

### E.2 WAI TAD-bouncer skip (where_am_i.py:2178-2185)

```python
if tad_verdicts:
    verdict = tad_verdicts.get(offer_id)
    if verdict is not None and verdict.passed is False:
        continue
```

This filter exists inside `_evaluate` and drops offers from per_target_outcomes BEFORE candidate scoring. It is a substantive matcher decision (TAD said no), not a post-fetch Python bug. It IS a possible contributor to 8576 not emerging as a candidate at 12:22:38, but it's design behavior, not a defect.

### E.3 WAI confidence floor (driver_heartbeat.py:1620)

```python
if outcome is None or outcome.confidence < WAI_CONFIDENCE_THRESHOLD:
    continue
```

Same character — substantive matcher behavior. WAI_CONFIDENCE_THRESHOLD = 0.40. Any offer scored below 0.40 doesn't make it to matcher_candidates. Not a bug; design.

**Conclusion for E: no post-fetch Python-side filter is silently dropping rows after a "correct" read. The matcher's read itself is filtering offers via bind drift (verdict 1), and substantive WAI/TAD logic accounts for the residual.**

---

## Summary

- **Verdict 1 (bind-parameter drift) is the matcher-vs-Monitor disagreement mechanism.** Static analysis proves it; runtime data at 12:22:38 proves it for offer 8575.
- **The drift is on `current_cumulative_miles`:** matcher passes real value (activating the distance gate); Monitor passes NULL (gate permissive).
- **Secondary latent bug:** `DriverQueue.snapshot()` at `driver_queue.py:370` accepts `last_odometer_move_at` kwarg but doesn't forward it to `_project_offers`. Currently makes the matcher less restrictive than the original design intent (odometer-staleness gate forced to permissive on both paths). Not the cause of starvation, but worth tracking.
- **Bind drift does not fully explain offer 8576 at 12:22:38.** Offer 8576 passes the matcher's distance gate but still doesn't reach matcher_candidates. The `lost_mode_no_candidate` label implies a downstream WAI/TAD substantive rejection (cluster mismatch, TAD verdict.passed=False, or confidence < 0.40). Fixing the bind drift alone may not restore full matcher_candidates; the WAI-scoring side likely needs a separate investigation.
- **No post-fetch Python filter is silently dropping rows.** The two post-snapshot filters that exist (§XVI.G lock + WAI confidence floor) produce distinct unmatched_reason labels (`lock_suppressed`, `wai_below_floor`) that did not appear at 12:22:38.

Output for Gemini review. No fixes proposed.
