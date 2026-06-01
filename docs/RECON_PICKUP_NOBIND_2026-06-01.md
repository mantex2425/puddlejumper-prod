# RECON — Pickups didn't bind narrative all shift (2026-06-01)

**Status:** read-only forensic recon; no code changes.
**Verdict:** **H-A confirmed.** The §XVIII cold-start bind gate worked
exactly as designed. No code defect in the bind/persist path. The product
failure is doctrinal: the gate's "exactly one alive-unpicked offer"
precondition is virtually unsatisfiable during stacked-morning driving.
**Pytest baseline:** 680 passed / 1 skipped (confirmed earlier this session).
**Branch:** `fix/three-recon-bugs-2026-05-30` @ `0dfc154`. Prod rev
`puddlejumper-api-00638-wpg`.
**Driver:** `UjT1hE9eBXh2q95aSZYOkzDJ8lo1`.
**Shift marker:** `DRIVE_START_MARKER` @ 2026-06-01 09:47:42 UTC.

---

## §1 Bind-gate single-source confirmation

`driver_heartbeat.py:608` evaluates:

```python
if alive_unpicked_offer_ids == frozenset({str(action.offer_id)}):
    queue.bind(action.offer_id, cur)
```

The trace:
- Set is computed once at `driver_heartbeat.py:1812` via
  `_get_alive_unpicked_offer_ids(cur, driver_id, cumulative_miles,
  _heartbeat_now, effective_last_move)` — the canonical helper.
- Threaded into `_execute_action(...)` at `driver_heartbeat.py:2040` as
  the kwarg `alive_unpicked_offer_ids=alive_unpicked_offer_ids`.
- The FPO branch (`driver_heartbeat.py:548-700`) reads the kwarg
  unchanged; no parallel re-query, no hand-rolled set.
- `_get_alive_unpicked_offer_ids` (`driver_heartbeat.py:958-1001`) runs
  `LIVE_OFFER_PREDICATE_SQL` (verbatim from `driver_queue.py:183-237`)
  + `AND oh.actual_pickup_at IS NULL`. Pinned by
  `tests/test_live_offer_predicate_imports.py` (
  `test_get_alive_unpicked_offer_ids_uses_horizon_predicate`).

Single-source contract intact.

---

## §2 Per-fire alive_unpicked reconstruction

Out-of-band replay per §XIV.H (logged in
`docs/out_of_band_offer_history_queries.md`). Method: mirror
`_get_alive_unpicked_offer_ids` SQL with two historical overrides
(`actual_pickup_at IS NULL OR ≥ reference_time`,
`actual_dropoff_at IS NULL OR > reference_time`) where reference_time =
the PDC fire's `created_at`. Cum_miles taken from closest preceding
`heartbeat_log`. Odometer-staleness gate permissive at active-drive
pace (lom_at within seconds of arrest; NOT-block FALSE → gate passes).

The reconstructed set INCLUDES the firing offer when its
`actual_pickup_at` was still NULL going into the heartbeat — this
matches production's sample order (line 1812 runs BEFORE the FPO's
offer_history UPDATE at line 620-638).

### Pickup-class fires on 2026-06-01

| # | fire_ct (CT) | fire_offer | cum_miles | alive_unpicked_set | size | gate fires (set == {fire})? | bound on next tick? |
|---|---|---|---|---|---|---|---|
| 1 | 08:36:40.156 | 8739 | 246.03 | {8736, 8737, 8739} | 3 | ❌ no | no |
| 2 | 08:41:40.582 | 8739 | 247.37 | {8736, 8737} | 2 | ❌ no | no |
| 3 | 08:44:43.692 | 8739 | 248.17 | {8736, 8737} | 2 | ❌ no | no |
| 4 | 08:46:52.067 | 8739 | 248.97 | {8736, 8737} | 2 | ❌ no | no |
| 5 | 10:17:09.313 | 8741 | 307.90 | {8741} | 1 | ✅ **yes** | **yes** |
| 6 | 10:45:17.827 | 8746 | 321.32 | {8742, 8743, 8744, 8745, 8746, 8747, 8748} | 7 | ❌ no | no |
| 7 | 10:45:24.462 | 8742+8744 (§5.3 pair) | 321.32 | {8742, 8743, 8744, 8745, 8747, 8748} | 6 | ❌ no (not singleton match) | no |

Fires 2-4 on offer 8739 sample size = 2 because 8739's `actual_pickup_at`
was written by Fire 1 (08:36:40), so subsequent samples exclude it from
alive-unpicked. The pair-fire at #7 happens after #6 wrote 8746's
pickup_at, narrowing the set by one.

### Decisive observation

Across all 7 pickup-class fires, exactly **one** event satisfied the
gate's precondition (`set == frozenset({fire_offer})`): Fire 5 (8741).
That event DID bind on the next heartbeat (PDC `bound_post = 8741` at
~10:17:11 CT per the brief's context). Every other event had
set_size ≥ 2 and the bind was correctly withheld.

**No fire exists where set_size == 1 AND did_bind == false.** H-B is
ruled out. The bind/persist path has no defect.

---

## §3 H-A vs H-B verdict

**H-A (doctrinal gap) — confirmed.** The bind gate's "exactly one
alive-unpicked offer" precondition is virtually unsatisfiable during
normal stacked morning driving in this market:

- 8739 fire was caught with 8736 and 8737 already alive-unpicked
  (offered 4-12 min prior, sitting at age 4-16 min, well inside the 4h
  ceiling and within distance envelope).
- The 10:45 fires were caught with the entire 8742-8748 cohort live
  (offered 1-7 min prior, sitting on the same Cypress cluster).
- The only time the gate fired was 8741 at 10:17, when 8736/8737/8738
  had all aged past 4h (created 08:20-08:24) and 8740 had dropoff'd —
  leaving 8741 as the lone alive-unpicked offer.

**Practical impact**: on a typical Houston morning the driver receives
2-7 offers per arrest cluster (cluster_size = 3-4 here). A solo
alive-unpicked offer only occurs at the tail of a stacked sprint or in
the rare "single offer, no stacking" minute. Across the 8h shift the
gate had its precondition for 1/7 = 14% of pickup arrests — every other
pickup ran in lost-mode with no narrative bind, and downstream
narrative-optimization (TAD pre-filter, wai_above_floor commit rule)
never engaged.

**H-B (code defect) — ruled out.** The bind precondition was checked
honestly and reflected the actual queue state at each fire. The single
case where it fired (8741) succeeded end-to-end. There is no try/except
swallow, no rollback, no branch-specific failure.

### What the §XVIII amendment discussion needs to weigh

This is a doctrine question for the Claude<->Gemini loop, NOT a code
patch. Some directions the doctrine could move (NOT proposals — just
framing for the discussion):

- **Loosen the singleton gate** (e.g., bind to the firing offer
  whenever it is *uniquely* the candidate the matcher chose, even if
  other alive-unpicked offers exist in the queue). This re-introduces
  the L-19 risk class — the bound pointer would no longer be jointly
  justified by the queue.
- **Bind on first-of-stacked-cohort** (bind when the firing offer is the
  oldest unpicked offer in the alive set). Cheaper L-19 risk but still
  drift from the "unambiguous" framing.
- **Two-tier doctrine** (singleton binds narrative; ambiguous fires get
  a softer "lean" pointer that biases WAI but doesn't claim narrative).
  Larger surgery.
- **Accept the status quo** and harden lost-mode operation instead so
  that running in lost-mode is no longer a degraded path — i.e., make
  lost-mode the first-class steady state and demote the bound state to
  a confirmation overlay.

This recon does NOT recommend any of these. It surfaces the gap and
hands the framing to the doctrine review.

---

## §4 §XVI.G Transaction Lock finding (8739 4x FPO refire)

### The data

| fire_ct (CT) | cluster_lat, cluster_lng | speed_mph | arrest_s | meters_from_prior_cluster | seconds_since_prior_fire |
|---|---|---|---|---|---|
| 08:36:40 | 29.5817827, -95.2075893 | 0 | 10.67 | (first) | (first) |
| 08:41:40 | 29.5907162, -95.2216032 | 0 | 10.67 | **1680.5** | 300 |
| 08:44:43 | 29.5847549, -95.2140429 | 0 | 10.73 | **986.5** | 183 |
| 08:46:52 | 29.5925570, -95.2045246 | 0 | 10.71 | **1264.2** | 128 |

`LOCK_RELEASE_DISTANCE_M = 152.4` (`decisions/transaction_lock.py:31`).

### Mechanism trace

After each FPO, `acquire_lock(...)` runs at
`driver_heartbeat.py:687-696` with `pudo_type='pickup'`, anchored at
that fire's `(nail_lat, nail_lng)`. The matcher's gate at
`driver_heartbeat.py:1900-1914` calls `is_locked(...)` for each
candidate before dispatch. `is_locked`
(`decisions/transaction_lock.py:95-162`) returns None (unlocked) when
distance from anchor ≥ 152.4 m OR speed_streak ≥ 10 s.

Each gap between 8739 fires was **6× to 11× the 152.4 m release
threshold**. The lock was acquired correctly on each fire, then released
cleanly by the **spatial axis** at every subsequent arrest. `is_locked`
then deletes the prior lock row and returns None, which is why §5 of
the diagnostic shows only the most-recent 8739 lock (the others were
deleted on release).

The lock engaged. The lock released per spec. The lock layer is doing
exactly what §XVI.G specifies.

### What this surfaces (independent of bind-gate doctrine)

A real product question — separate from H-A and from the lock layer —
is **why the matcher kept selecting offer 8739 as the pickup candidate
after its `actual_pickup_at` was already written by Fire 1**. Once
`actual_pickup_at` is set, 8739 is alive-but-picked; its WAI leg should
shift from pickup to dropoff. Yet Fires 2-4 emitted `FirePickupObservation`
on 8739 again. Possibilities (not investigated here):

- WAI scores both legs every tick and the matcher picks the leg with
  the higher arrest-time confidence regardless of `actual_pickup_at`.
- `per_offer_state` doesn't expose pickup_already_fired to WAI.
- The driver was geographically still at the pickup zone, so the
  pickup leg legitimately out-scored the dropoff leg on spatial
  criteria.

Whatever the cause, the data-layer side held up: `offer_history.actual_pickup_at`
was written exactly once (by Fire 1) thanks to the `WHERE
actual_pickup_at IS NULL` guard at `driver_heartbeat.py:633`. The
narrative remains a Fire-1 record; Fires 2-4 produced redundant cache
writes (pms got overwritten 3 more times — pms's actual_pickup_lat is
the LAST fire's anchor, 29.5925570 / -95.2045246) but no logical
corruption.

This is logged here as a **separate doctrinal/architectural item** to
add to the §XVIII amendment discussion: post-pickup pickup refires are
data-noise even when bind doctrine is fully sorted.

---

## §5 What this recon did NOT touch

- No proposal of a §XVIII amendment (handed to the doctrine review).
- No analysis of the matcher's leg-selection logic (separate file).
- No fix proposal for the JOB-2-style monitor-queue surface (handled by
  `docs/FIX_PROPOSAL_QUEUE_NO_REAP_2026-06-01.md`).
- No deploy, no code edits, no test changes.

---

## §6 Pointers

- Bind gate: `driver_heartbeat.py:608` (singleton equality).
- Set source: `driver_heartbeat.py:1812` → `_get_alive_unpicked_offer_ids`
  (`driver_heartbeat.py:958-1001`).
- Set threading: `driver_heartbeat.py:2040` (kwarg into `_execute_action`).
- Lock acquire (FPO): `driver_heartbeat.py:687-696`.
- Lock check (matcher gate): `driver_heartbeat.py:1900-1914`.
- Lock impl: `decisions/transaction_lock.py` (`acquire_lock`,
  `is_locked`, `release_lock`).
- OOB log entry: `docs/out_of_band_offer_history_queries.md` (this recon
  appended).
