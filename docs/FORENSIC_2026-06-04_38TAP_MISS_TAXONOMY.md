# Forensic — 2026-06-04 drive: ground-truth tap analysis of the "11 misses"

**Analysis date:** 2026-06-06 · **Drive:** 2026-06-04 ~04:20–15:25 CT, real driver
`UjT1hE9eBXh2q95aSZYOkzDJ8lo1` (~97% lost-mode: 133 lost-mode fires vs 4 above-floor).
**Why:** the standing "Fix #3 = WAI single-road tuning" framing assumed the misses were a WAI
confidence problem. Cross-referencing the driver's physical button taps against the matcher's
decisions shows **they mostly are not** — and points at a different, testable root cause.

## Method (ground truth, zero state impact)

`app_private.contest_labels` = the driver's physical PUDO button presses (`label` ∈
pickup/dropoff/traffic/other, `label_time`). It has **no offer id and no coordinates**, so each
tap is cross-referenced **by time** to `app_private.pudo_decision_context` (matcher decision:
confidence, `unmatched_reason`, `dispatch_executed`, cluster fields, `arrest_duration_s`,
`wai_per_offer_scores`) and `app_private.heartbeat_log` (position/cadence). 38 taps on this drive.

Roster query (adjust the date):
```sql
SELECT to_char(label_time AT TIME ZONE 'America/Chicago','HH24:MI:SS') tap, label
FROM app_private.contest_labels
WHERE driver_id='UjT1hE9eBXh2q95aSZYOkzDJ8lo1'
  AND label_time >= '2026-06-04 00:00 America/Chicago'
  AND label_time <  '2026-06-05 00:00 America/Chicago'
ORDER BY label_time;
```

## What's solid (data)

- **Arrest happened at every PUDO.** All 38 taps had a real arrest (≥5s stillness; observed
  15–66s) within an average of **3.5s** of the press. → "PUDOs are decoupled from arrests" is
  **false**; the driver physically stopped at every one.
- **A cluster formed at every PUDO.** Every tap had `cluster_size` 3–31 in its window. → "the
  cluster never forms" is **false** too.
- **~22 of 38 caught** (a dispatch fired within ±90s of the tap; reconciles with the ~19 hand-count).
- **The genuine WAI residual is small (~5).** Taps where an offer *was* scored at the PUDO but
  stayed below the 0.40 floor, all localization-starved (every spatial signal 0; confidence =
  pure stop-physics): offers 9145, 9153, 9158 (intersection/single_road), 9137, 9143
  (airline-name POIs — POI match failed). These are the *only* taps WAI scoring is implicated in.
- **~3 EXCLUDED** (`queue_actually_empty`) — offer not in the live set at the tap.

## What I retract

An earlier pass bucketed misses as **"NO_CLUSTER = 12 (dominant)"** by counting taps whose window
contained a `cluster_unavailable` heartbeat. **That was a measurement artifact.** `cluster_unavailable`
is a **transient per-heartbeat state at the spin-up/decay edges of the detection envelope** — it
appears even on *caught* taps and on taps with size-31 clusters. On a 97%-lost-mode drive these
per-heartbeat reasons flicker, so counting them does **not** attribute a tap to a cause. There is
**no** "no-cluster dominates" conclusion to draw here.

## Leading mechanism: the horny-mode bootstrap deadlock

Intended design: normal cadence is **0.2 Hz (one sample / 5s)**; when WAI confidence rises the
matcher goes **"horny" → 1 Hz** to catch quick car entry/exit (`where_am_i.py`; heartbeat
response carries `cadence_target_hz`, observed 0.2 on this drive). Measured cadence at the taps:

| | taps | avg min inter-sample gap | reached ~1 Hz |
|---|---|---|---|
| missed | 16 | **4.87s** | 1/16 |
| caught | 22 | 3.19s | 3/22 |

**Horny mode barely engaged drive-wide — ~4 of 38 taps reached 1 Hz.** The drive ran at ~0.2 Hz
almost throughout. At 5s cadence a quick 8–12s PUDO yields only 1–2 samples → a thin cluster.

The lost-mode miss pattern is a **positive-feedback deadlock**:

> horny is gated on **rising confidence** → in lost-mode the spatial signals read **0** →
> confidence stays low → **horny never triggers** → cadence stays **0.2 Hz** → the quick PUDO is
> under-sampled → cluster stays thin → confidence stays low. It cannot bootstrap.

This unifies the device model the driver described — *detect early → improve through the arrest →
decay on leaving* — with the failures: the lifecycle **runs** (clusters grow), but it can't
*sharpen* without the 1 Hz samples, and the 1 Hz samples don't come without the confidence they'd
have produced.

**Fix hypothesis (testable; targets the real driver):** gate horny mode on **arrest (stillness)**,
not (only) on rising confidence. On this drive **arrest fired at 38/38 taps; horny at 4/38** — so
an arrest-triggered 1 Hz would densely sample *every* PUDO, breaking the deadlock at the stop,
independent of lost-mode-zeroed confidence. This is a **cadence-trigger** change — neither WAI
confidence tuning nor cluster-detection params. *Caveat:* the caught/missed cadence gap is modest
(most catches also ran ~0.2 Hz on longer dwells), so this is a leading hypothesis to validate, not
a proven sole cause.

## A second, distinct gap: multi-PUDO at one stop

Witnessed (driver, 2026-06-04): at a traffic light, confirmed a **pickup of one offer AND a
dropoff of a different offer at the same light.** Even with a clean cluster, the dispatcher fires
**one narrative per cluster** (§5.3-mirror / two-PUDO arbitration, dispatch.py) — so two events of
two offers cannot both register from one light-cycle cluster. Distinct from cadence and from WAI.

## What this overturns

The HANDOFF open-thread #2 / early Fix #3 framing — *"`on_target_road` is falsely precise (≈1.0)
on multi-mile transit roads; reweight proximity to dominate"* — is refuted:
- On the WAI-scored misses, **`on_target_road` = 0.00, not 1.0** (so are proximity/breadcrumb/
  adjacent). No over-confident signal to downweight; usually no signal to amplify.
- The suite **works** when the driver arrests at the target (offer 9127 dropoff: 0.897 via
  on_target_road/breadcrumb/adjacent = 1.0, fired and completed).
- Most misses are upstream of WAI scoring entirely (cadence/sampling, queue exclusion, multi-PUDO).

## Implications for the fix backlog

- **De-scope "Fix #3 = WAI confidence tuning."** It addresses ~5 of the misses at most, and even
  those are localization (intersection dual-road snap; airline-name POI cache miss), not
  transit-road reweighting. Do **not** reweight `_CONFIDENCE_WEIGHTS` to "recover the 11."
- **Promote the cadence-trigger fix** (arrest-gated horny) as the highest-leverage candidate — it
  attacks the bootstrap deadlock that under-samples quick PUDOs.
- **Multi-PUDO-at-one-cluster** design (the traffic-light case) — owed, distinct.
- **Re-measure EXCLUDED via cohort replay** against rev `00649` (sentinel+reaping+band) before any
  new work — those may already be fixed.

## Confirmed vs inferred

- **Confirmed:** 38 taps; arrest (15–66s) + a formed cluster at all 38; ~22 caught; horny engaged
  ~4/38 (missed taps ~4.9s cadence vs caught ~3.2s); WAI-scored misses have all spatial signals 0
  (confidence = stop-physics only); 9127 success (0.897, fired); the ~5 WAI residual span
  intersection/single_road/POI.
- **Inferred / owed:** that arrest-gated horny recovers the misses (validate — modest caught/missed
  cadence gap); that EXCLUDED taps recover under the shipped fixes (cohort replay); exact integer
  splits (window-sensitive); per-tap offer attribution (`contest_labels` has no offer id — time
  cross-ref only, so a ±90s fire for a different offer can over-credit CAUGHT).

## Next steps (ordered)

1. **Validate the cadence-trigger hypothesis:** would arrest-gated horny (1 Hz on stillness) have
   densified the 16 missed taps' clusters? Replay/simulate, or A/B on a drive.
2. **Cohort replay** of the 06-04 PUDOs against `00649` → recovery with no WAI change; isolates the
   true post-fix residual.
3. **Multi-PUDO-at-one-stop** design (traffic-light pickup+dropoff-of-two-offers).
4. Only then the WAI localization residual (intersection / airline-POI), if any survives.
