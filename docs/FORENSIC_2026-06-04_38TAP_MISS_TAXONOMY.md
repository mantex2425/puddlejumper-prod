# Forensic — 2026-06-04 drive: ground-truth tap analysis of the "11 misses"

**Analysis date:** 2026-06-06 · **Drive:** 2026-06-04 ~04:20–15:25 CT, real driver
`UjT1hE9eBXh2q95aSZYOkzDJ8lo1` (~97% lost-mode: 133 lost-mode fires vs 4 above-floor).
**Why:** the standing "Fix #3 = WAI single-road tuning" framing assumed the misses were a WAI
confidence problem. Cross-referencing the driver's physical button taps against the matcher's
decisions shows **they mostly are not** — and points at a different, testable root cause.

## Framing: the taps are the eval harness, not the product

`contest_labels` button presses are a **debug-only ground-truth instrument**. The shipped product
has **zero human PUDO input** — all pickup/dropoff identification is **fully automated** by the
matcher. So the catch rate here (≈63% vs the ~95% launch gate) is **autonomous-detection accuracy**,
and "fixes" that would rely on a human to correct a bad signal are off the table. This is why a
km-wrong geocode is *disqualifying* (root cause below), not a tunable: at runtime nothing catches
it. See memory `pudo-detection-is-fully-automated`.

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

## ⚠️ Validation update (2026-06-06): horny-mode hypothesis REFUTED

The "arrest-gated horny" hypothesis below was tested against the 16 missed taps and **does not
hold.** Decomposition of the misses:
- **A cluster formed at all 16** (cluster_size 3–19, duration 11–58s) — the misses are **not**
  sample-starved, so 1 Hz cannot "form a cluster that wasn't there."
- **11 of the 13 scored misses had localizing-signal-sum = 0.00** (proximity + on_target_road +
  breadcrumb + adjacent). They were capped by **localization**, not cluster density — and more
  samples add no spatial signal.
- 3 taps weren't scored at all (queue-empty / exclusion); 2 (14:58, 15:03) had strong
  localization (sum 2.0) yet didn't fire — a dispatch/arbitration gap.

So the caught/missed cadence gap (4.87s vs 3.19s) was **real but non-causal**; clusters formed
regardless. **Arrest-gated horny would not recover these misses.** The cadence section below is
retained for the record but is superseded by this validation.

**What the validation re-confirms — the original Fix #3 question.** The dominant failure is a
cluster that forms *right where the driver arrested*, but `proximity=0` and `on_target_road=0` —
i.e. the **actual stop is beyond the proximity radius from the offer's geocode and on a different
road than the geocode's road** (proximity *does* compute when near — 9127's pickup hit 0.48 — so
0.00 means genuinely offset, not a dead signal). That is exactly *"arrested near-but-not-at a
pickup you can't physically stop at."* The real levers are therefore: **(a) the ground-truth
tolerance disposition** (miss / correct-observation / log-don't-bind — the product call), **(b)
geocode/road localization for offset address classes** (intersections, airline-name POIs), **(c)**
the 3 queue-exclusions (cohort replay), **(d)** the 2 dispatch-gap anomalies. **Not cadence, not
cluster-formation, not WAI-weight reweighting.**

## Leading mechanism (SUPERSEDED — see validation update above): the horny-mode bootstrap deadlock

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
- Most misses are upstream of WAI *reweighting* — they have a formed cluster but the localizing
  signals read 0 (the stop is offset from the geocode), or they aren't scored at all (exclusion).

## Implications for the fix backlog (post-validation)

- **Cadence/horny is NOT the lever** (validated-refuted): clusters formed at all 16 misses; 1 Hz
  adds samples, not localization. Do not pursue arrest-gated horny as the miss fix.
- **The dominant lever is the ground-truth-tolerance disposition** (the original Fix #3 product
  question): what should the system do when a clean arrest forms a cluster that is **offset from
  the offer's geocode** (proximity=0, on_target_road=0) — *miss / correct-observation /
  log-don't-bind*? 11 of 13 scored misses are this case.
- **Localization for offset address classes** is the engineering half: intersection ("Road & Road")
  geocode/road snap and airline-name POI resolution — why the offer's geocode lands beyond the
  reachable stop. (Verify offset distance per class before designing.)
- **De-scope "Fix #3 = WAI confidence reweighting."** There is no over-confident signal to
  downweight and (at the misses) no signal to amplify — reweighting `_CONFIDENCE_WEIGHTS` cannot
  recover an offset-cluster miss.
- **Re-measure EXCLUDED (3) via cohort replay** against rev `00649` (sentinel+reaping+band) — may
  already be fixed.
- **Investigate the 2 dispatch-gap anomalies** (14:58, 15:03): strong localization (signal sum
  2.0) yet no fire within 90s — a dispatch/arbitration issue distinct from all the above.

## Confirmed vs inferred

- **Confirmed:** 38 taps; arrest (15–66s) + a formed cluster at all 38; ~22 caught; horny engaged
  ~4/38 (missed taps ~4.9s cadence vs caught ~3.2s); WAI-scored misses have all spatial signals 0
  (confidence = stop-physics only); 9127 success (0.897, fired); the ~5 WAI residual span
  intersection/single_road/POI.
- **Validated-refuted:** arrest-gated horny does NOT recover the misses — a cluster formed at all
  16; 11/13 scored misses had localizing-signal-sum = 0 (localization-capped, not sample-capped).
- **Inferred / owed:** that the offset is physics vs geocode-bug per class (measure stop-vs-geocode
  distance); that EXCLUDED taps recover under the shipped fixes (cohort replay); exact integer
  splits (window-sensitive); per-tap offer attribution (`contest_labels` has no offer id — time
  cross-ref only, so a ±90s fire for a different offer can over-credit CAUGHT).

## ROOT CAUSE (2026-06-06): the geocodes are kilometers-wrong

Measured distance from the driver's clustered stops to the offer geocodes: closest-ever approach
was **1.7 km (intersection), 2.7 km, 4.7 km (single_road), 6.4 km and 13.8 km (airline-name POIs)**
— with `proximity = 0` throughout. These are not "stopped offset from a good pin" distances
(those are 100s of meters); at km scale **the geocodes themselves are garbage** for these address
classes (Uber intersection "Road & Road" and POI/airline-name resolution).

That is the localization failure: `proximity` and `on_target_road` are **derived from the
geocode**, so a km-wrong pin zeros them regardless of where the driver actually stopped — garbage
in, garbage out. 9127 caught because its geocode was usable (on_target_road/breadcrumb/adjacent
fired); the misses had unusable geocodes. **Geocode quality is the catch/miss discriminator.**

Attribution note: tap→offer binding should be **cluster/sequence-based**, not geocode-distance-
based (tap time → heartbeat → cluster → the active offer by sequence/odometer). Do NOT attribute by
nearest geocode — the geocodes are the broken input.

## Implication: retire geocode-derived signals; identify offers by TAD + sequence

The matcher over-relies on geocode-derived signals (`proximity`, `on_target_road`) that are
unreliable for a large address-class population. The geocode-**independent** signals are sound:
`cluster_tightness`/`cluster_duration`, `breadcrumb_match` (driven path), arrest, and especially
**TAD / the odometer band** (distance traveled vs the offer's expected miles — the §5.5/§9.9
machinery). Direction: **de-weight/retire proximity + on_target_road, and re-home offer
*identification* (which offer is this cluster?) onto TAD + offer sequence/timing + breadcrumb** —
geocode-free. (Removing geocodes outright is the strong version; the prerequisite is the TAD+
sequence identifier, since geocodes currently do the identification job, badly.)

## Next steps (ordered)

1. **Cluster/sequence tap→offer attribution** (not geocode): tap time → cluster → active offer by
   received-order + odometer progression. Confirms exclusion vs localization per tap without the
   broken geocode input.
2. **Quantify geocode uselessness:** per offer the driver serviced, did `proximity`/`on_target_road`
   ever exceed 0 (usable geocode) vs never (garbage)? Sizes the geocode-quality problem.
3. **Ground-truth-tolerance disposition** (product call) + the TAD-based-identification redesign.
3. **Cohort replay** of the 06-04 PUDOs against `00649` → how many EXCLUDED recover with no WAI
   change; isolates the true residual.
4. **Multi-PUDO-at-one-stop** design (traffic-light pickup+dropoff-of-two-offers) and the 2
   dispatch-gap anomalies (14:58, 15:03).
5. (No cadence/horny work; no `_CONFIDENCE_WEIGHTS` reweighting — both ruled out.)
