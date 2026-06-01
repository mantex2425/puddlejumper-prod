# RECON — Queue Shows 6 Live Offers Hours After Last Ride (2026-06-01)

**Status:** root cause confirmed (read-only recon, no code changes)
**Branch:** `fix/three-recon-bugs-2026-05-30` @ `0dfc154`
**Prod rev:** `puddlejumper-api-00638-wpg`
**Driver:** `UjT1hE9eBXh2q95aSZYOkzDJ8lo1`
**Pytest baseline:** 680 passed / 1 skipped (confirmed before recon)

---

## TL;DR

The "6 live offers" the monitor surfaces ~7h after the last ride are **not**
stale-cached and they are **not** the result of a too-loose predicate. The
read path executes `LIVE_OFFER_PREDICATE_SQL` correctly — but the call site
at `driver_status.py:188` passes `current_cumulative_miles=None` and
`last_odometer_move_at=None`, which silently degrades the distance gate to
"always pass" and renders the odometer-staleness gate permissive. Only the
causality clause and the 4h abandonment ceiling do any work.

H1 ("predicate too loose") — **FALSE**. The predicate, given the heartbeat
handler's real per-tick state, reaps **all 13** offers (0 alive). The 4h
ceiling and distance envelope are both biting correctly.

H2 ("stale read, not actually composing the predicate") — **PARTIALLY TRUE,
in spirit but not in letter**. The read path IS the predicate. The bug is
upstream: the caller doesn't supply the per-tick inputs that make the
predicate's distance / staleness gates discriminative.

**Single-sentence verdict:** `driver_status.py:188` invokes
`DriverQueue(driver_id).offer_ids_only(cur)` with no odometer signals,
producing a "live" set that excludes only offers killed by the 4h
abandonment ceiling — letting recently-created-but-physically-departed
offers ride forever from the monitor's point of view.

---

## §1 Predicate text + reap constants (as found in code)

`driver_queue.py:183-237` defines `LIVE_OFFER_PREDICATE_SQL`. Four clauses:

| # | Clause | Constant(s) bound |
|---|---|---|
| 1 | `oh.actual_dropoff_at IS NULL` | — (terminal state filter) |
| 2 | Causality: `oh.created_at <= ref_time` | — |
| 3 | Abandonment ceiling: `oh.created_at > ref_time - INTERVAL 'N hours'` | `GC_ABANDONMENT_CEILING_HOURS = 4` (`driver_queue.py:100`) |
| 4a | Odometer-staleness: `NOT (lom_at IS NOT NULL AND lom_at < ref - 30min AND oh.created_at <= ref - 30min)` | `GC_ODOMETER_FREEZE_MINUTES = 30` (`driver_queue.py:87`) |
| 4b | Distance envelope: `cum_miles IS NULL OR oh.miles_at_offer_receipt IS NULL OR cum_miles < (envelope)` | `GC_NULL_PICKUP_MI=4.0`, `GC_NULL_TRIP_MI=8.0`, `GC_BUFFER_MULT=1.25`, `GC_MIN_DIST_MI=2.0`, `GC_MAX_DIST_MI=50.0` |

**No spatial term.** Reap is time + odometer-staleness + distance-envelope.

Both gates 4a and 4b are **gracefully degrading** by design — they
short-circuit to "pass" when their input signal is NULL. That's correct
for replay/forensic use cases. It's incorrect for live monitor use cases
where the data is sitting one JOIN away.

---

## §2 Read paths

**Heartbeat path (`driver_heartbeat.py:1659`):**

```python
snap = queue.snapshot(cur,
                      current_cumulative_miles=cumulative_miles,
                      last_odometer_move_at=effective_last_move)
```

Both signals are threaded with real per-tick values. `effective_last_move`
is computed at lines 1631-1657 from the pre-update `driver_trip_state`
row, bumped if the odometer just moved.

**Monitor path (`driver_status.py:187-189`):**

```python
"planner_queue": list(
    DriverQueue(driver_id).offer_ids_only(cur)
),
```

`offer_ids_only(cur, current_cumulative_miles=None, last_odometer_move_at=None)`
— both kwargs default to None and the call site supplies neither. The
data exists on `driver_trip_state.heartbeat->>'cumulative_miles'` and
`driver_trip_state.last_odometer_move_at`; the call site does not read it.

Confirmed by signature at `driver_queue.py:549`:

```python
def offer_ids_only(self, cur, current_cumulative_miles=None, last_odometer_move_at=None) -> tuple[str, ...]:
```

---

## §3 DB evidence (2026-06-01 ~18:14 UTC, ≈7h after last ride)

### Driver state (heartbeat is still firing — no shift end)

| Field | Value |
|---|---|
| `current_offer_id` | NULL |
| `heartbeat_at` | 2026-06-01 18:14:28 UTC |
| `last_odometer_move_at` | 2026-06-01 18:14:28 UTC (lom_age = **0.07 min**) |
| `cumulative_miles` | **362.36** |
| position | (29.5063766, -95.5022146) — south of Hwy 6 / SW Houston |

The driver is still moving. The odometer-staleness gate (4a) cannot bite
at this snapshot regardless of which path runs the predicate.

### Offers since `DRIVE_START_MARKER` (2026-06-01 09:47:42 UTC)

13 offers, ages 2.5h to 4.9h, six unpicked:

| offer_id | age_hr | picked | dropped | miles_at_receipt | envelope cap |
|---|---|---|---|---|---|
| 8748 | 2.55 | f | **t** | 318.24 | 326.6 |
| 8747 | 2.55 | f | f | 317.94 | 333.1 |
| 8746 | 2.56 | t | f | 317.47 | 327.0 |
| 8745 | 2.58 | f | f | 316.35 | 323.5 |
| 8744 | 2.58 | t | f | 315.94 | 329.2 |
| 8743 | 2.59 | f | f | 315.48 | 336.2 |
| 8742 | 2.60 | t | f | 315.17 | 329.2 |
| 8741 | 3.26 | t | **t** | 305.57 | 315.0 |
| 8740 | 4.08 | f | **t** | 267.32 | 305.0 |
| 8739 | 4.71 | t | f | 243.25 | 273.7 |
| 8738 | 4.83 | f | f | 238.15 | 241.3 |
| 8737 | 4.84 | f | f | 238.15 | 248.0 |
| 8736 | 4.91 | f | f | 238.12 | 254.1 |

### Live set — monitor path (NULL odometer params) — 6 offers

```
8742, 8743, 8744, 8745, 8746, 8747
```

These are exactly the under-4h, no-dropoff rows. **Matches the anomaly
report exactly.** 8748 (dropped) excluded by gate 1; 8741 (dropped) by
gate 1; 8736-8740 by gate 3 (4h ceiling).

### Live set — heartbeat path (real odometer + real lom_at) — 0 offers

```
(empty)
```

Distance gate kills every row: `cum_miles=362.36` exceeds every offer's
envelope cap (max envelope = 336.2 for 8743). The heartbeat handler
correctly considers the queue empty. The "still firing" heartbeat at
18:14:28 is a `_get_alive_unpicked_offer_ids` = ∅ case, which means
`lost_mode=False` and the monitor's queue display is the **only** surface
where these phantom 6 are visible.

---

## §4 Per-offer clause breakdown

`g_*` columns show which gates pass under the **heartbeat-path** params
(real odometer, real lom_at, both fresh). The distance gate is the
discriminator — it fails for every offer:

| offer_id | age_hr | g_no_dropoff | g_under_4h | g_odo_pass | g_dist_pass | miles_from_driver |
|---|---|---|---|---|---|---|
| 8748 | 2.55 | f | t | t | **f** | 30.4 |
| 8747 | 2.55 | t | t | t | **f** | 34.6 |
| 8746 | 2.56 | t | t | t | **f** | 29.8 |
| 8745 | 2.58 | t | t | t | **f** | 29.0 |
| 8744 | 2.58 | t | t | t | **f** | 34.5 |
| 8743 | 2.59 | t | t | t | **f** | 34.1 |
| 8742 | 2.60 | t | t | t | **f** | 34.5 |
| 8741 | 3.26 | f | t | t | **f** | 24.3 |
| 8740 | 4.08 | f | f | t | **f** | 9.5 |
| 8739 | 4.71 | t | f | t | **f** | 17.8 |
| 8738 | 4.83 | t | f | t | **f** | 22.4 |
| 8737 | 4.84 | t | f | t | **f** | 21.9 |
| 8736 | 4.91 | t | f | t | **f** | 15.9 |

The 6 the monitor surfaces (8742-8747) are 29-35 miles from the driver
straight-line. The distance gate (scalar odometer envelope) does the
right thing about this when invoked with the data. The fact that the
monitor surfaces them anyway is purely a function of `cum_miles=NULL`
hitting the gate's NULL guard.

A future spatial gate (§5.1) would reap the 9 unpicked offers (8736-8738,
8742-8747) at this snapshot since all are >5 mi from the driver — but
that's a defense-in-depth question, not the proximate fix.

---

## §5 Verdict

The bug is at **`driver_status.py:187-189`**: the call site for the
monitor's `planner_queue` field invokes `offer_ids_only(cur)` without
threading the per-tick odometer state that lives on `driver_trip_state`.
The predicate behaves correctly for the inputs it receives; the inputs
are the problem.

This is not H1 (predicate logic). The predicate at
`driver_queue.py:183-237` is doing exactly what its design intends:
graceful degradation when odometer signals are absent. The heartbeat
handler, which IS the source of truth for liveness during a live shift,
sees 0 alive offers and routes `lost_mode=False` accordingly — that part
of the system is healthy.

This is not pure H2 either. The read is fresh; the predicate composes
on every status request; nothing is cached. It's a third category:
**fresh-read with degraded inputs** — call-site negligence at the seam
between the queue module and the status surface.

Fix proposal lives in a separate brief (do not write here per recon
constraints). The minimal change is one signature update plus a JOIN at
the caller; the harder question is whether `offer_ids_only(cur)` should
accept NULL params at all, given that every production caller has the
data on hand. That's a Gemini-loop question.

---

## §6 Pointers

- Predicate: `driver_queue.py:183-237` (`LIVE_OFFER_PREDICATE_SQL`)
- Ceiling: `driver_queue.py:100` (`GC_ABANDONMENT_CEILING_HOURS = 4`)
- Freeze: `driver_queue.py:87` (`GC_ODOMETER_FREEZE_MINUTES = 30`)
- Heartbeat-path call site: `driver_heartbeat.py:1659`
- Monitor-path call site (the bug): `driver_status.py:187-189`
- Per-tick state read for heartbeat: `driver_heartbeat.py:1639-1657`
- Out-of-band SQL log: `docs/out_of_band_offer_history_queries.md`
  (2026-06-01 entry — H1/H2 disambiguation)
