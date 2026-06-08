# Forensic — 2026-06-07 drive: the first event-ledger drive (tap taxonomy + band/anchor decomposition)

**Drive:** 2026-06-07 ~13:50–17:19 CT, real driver `UjT1hE9eBXh2q95aSZYOkzDJ8lo1`, on rev
`puddlejumper-api-00651-2fs` (event-ledger Phase 1 + `ground_truth_tap` live). Drive-start marker
in `event_ledger` at **2026-06-07 18:50:40.732577 UTC**. ~97% lost-mode (`bound_offer` NULL on all
8 taps). **Method:** two `ORDER BY event_time` reads of `app_private.event_ledger` + one
`offer_history` join — *no archaeology*. The 06-04 forensic took a dozen cross-referenced queries
and four refuted conclusions to reach a fuzzier version of this. **This is the ledger's payoff.**

## Drive shape (since the marker)

51 events: 22 `keyframe`, 9 `matcher_eval`, 8 `ground_truth_tap`, 4 `offer_entered_queue`,
4 `offer_left_queue`, 2 `pickup_detected`, 1 `dropoff_detected`, 1 `drive_start_marker`.

## Tap-by-tap taxonomy — 3 caught / 5 missed

Each tap's **live queue at the instant of the tap** is on the `ground_truth_tap` row (`queue_snapshot`)
— the attribution that took a dozen queries on 06-04 is now a column.

| # | tap (CT) | label | live queue at tap | system | verdict |
|---|---|---|---|---|---|
| 1 | 14:15:26 | pickup | [10468] | `pickup_detected 10468` @14:15:38 | ✅ caught |
| 2 | 14:43:55 | dropoff | [] | `dropoff_detected 10468` @14:43:50 (terminated 14:43:52) | ✅ caught |
| 3 | 14:59:48 | pickup | [10469] | `pickup_detected 10469` @15:00:03 | ✅ caught |
| 4 | 15:46:50 | dropoff | [10469,10470] | — | ❌ in-queue, not detected |
| 5 | 15:47:50 | pickup | [10469,10470] | — | ❌ in-queue, not detected |
| 6 | 16:18:21 | dropoff | [] | — | ❌ availability: 10469 `band_overshot:dropoff`-reaped @16:00 (18 min early) |
| 7 | 16:34:33 | pickup | [] | — | ❌ availability: 10471 `band_overshot:pickup`-reaped @16:31 (3 min early) |
| 8 | 17:19:22 | dropoff | [] | — | ❌ availability: nothing in queue |

**Two miss classes:**
- **Availability (taps 6,7,8)** — the offer was reaped/absent before the physical PUDO; the dominant
  driver is **`band_overshot`** (reap reason + minutes-early are on the `offer_left_queue` row).
- **In-queue, not detected (taps 4,5)** — the **multi-PUDO pair** (dropoff + pickup one minute apart,
  both offers live, neither fired): the traffic-light/back-to-back arbitration gap, witnessed cleanly.

## Band-overshoot decomposition (the headline)

The band MATH fired correctly — in all three reaps `reap_odo` lands right at the upper edge. The
failure is *what it reaps against*. Constants: `tolerance = max(0.15·leg_miles, 2.0)`, upper-edge
liveness (reap when `actual_odo > center + tolerance`).

| offer | leg | anchor (exp dist) | upper (+tol) | reaped @odo | **actual PUDO @odo** | past anchor | past band | cause |
|---|---|---|---|---|---|---|---|---|
| **10469** | dropoff | 118.53 | 123.66 *(tol 5.13)* | 123.73 | **140.16** | **+21.6** | **+16.5** | **anchor / Uber data** |
| **10471** | pickup | 142.37 | 144.37 *(tol 2.0 floor)* | 144.37 | **145.20** | +2.83 | **+0.83** | **noise-floor too tight** |
| 10470 | pickup | 118.53 | 120.53 *(tol 2.0 floor)* | 120.58 | (not a tap miss) | — | — | floor (incidental) |

**10469 = §V "Uber Data Reality" garbage-in, NOT a band bug.** The *pickup* anchored perfectly
(actual pickup odometer **84.33** vs anchor **84.41**, off 0.08 mi). The dropoff anchor (118.53 =
84.4 + Uber's `trip_miles` 34.2) was wrong because **Uber's trip estimate was 63% low**:
- Uber `trip_miles` = **34.2** · actual driven = 140.16 − 84.33 = **55.83 mi** (+21.6, **+63%**).
- Addresses: pickup `Dickson St & Patterson, Houston` → dropoff `N Autumnwood Way & Plumero Pl,
  Spring` (part of an airport chain: 10470 Woodlands→`United, IAH`; 10471 `Terminal D/E, IAH`→Houston).
- No band-widening or anchor-logic change saves a 63%-wrong *input*.

**10471 = a marginal noise-floor case.** `pickup_miles`=2.2 → tolerance falls to the **2.0-mi floor**;
the actual pickup (145.20) was only **0.83 mi past** the band's upper edge. A modestly wider floor
(~3 mi) would have caught it — at the cost of keeping stale short-leg offers alive longer.

## The lead (evidence-backed; NOT a fix — disposition still a product call)

The dropoff-leg upper-edge reap keys on `trip_miles`, a number Uber can be **63% wrong** about. The
§5.5 sentinel philosophy already says *don't reap on an untrustworthy anchor*. So the strongest lead
is: **the dropoff-leg liveness reap should NOT fire on `trip_miles` overshoot once a trip is
underway** — defer (keep the offer live) and let the **physical** signal (cluster/arrest at the real
dropoff) end the leg, exactly as the auto-detector is meant to. This is the opposite of "tune the
band weights" — which is where blind tuning would have sent us, and the ledger showed it would have
been wrong.

Secondary: the **2.0-mi noise floor** is marginally tight on short legs (10471) — a cheap widen with
a staleness trade-off. And the **multi-PUDO arbitration** (taps 4/5) is a distinct, still-open gap.

## Reusable reads

```sql
-- the whole drive, one timeline (taps + detections + reaps interleaved)
SELECT event_time AT TIME ZONE 'UTC' t, event_type, offer_id, queue_delta, matcher_snapshot, payload, summary
FROM app_private.event_ledger
WHERE driver_id = '<driver>' AND event_time >= '<marker>' ORDER BY event_time;

-- reap reasons (the availability story)
SELECT offer_id, cumulative_miles AS reap_odo, queue_delta->'left'->0->>'reason' AS reason, event_time
FROM app_private.event_ledger WHERE event_type='offer_left_queue' AND driver_id='<driver>' ORDER BY event_time;

-- band drill: anchor (offer_history.expected_*_distance, *_miles) vs actual pickup/dropoff odometer
--   (pickup_detected.cumulative_miles for the pickup odo; the dropoff tap / heartbeat_log for the dropoff odo)
```

## What this drive proves about the ledger

The miss taxonomy, the availability-vs-detection split, the per-reap reason + minutes-early, the
band-vs-anchor decomposition, and the Uber-estimate-vs-odometer-actual — all from a handful of
one-line reads. The ledger turned the multi-hour, four-wrong-turns forensic into a transparent
timeline. *The next drive's misses are a query, not an investigation.*
