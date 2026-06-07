# Forensic — 2026-06-04 drive: why automated PUDO detection missed

**Analysis:** 2026-06-06 · **Drive:** 2026-06-04 ~04:20–15:25 CT, real driver
`UjT1hE9eBXh2q95aSZYOkzDJ8lo1`, ~97% lost-mode (133 lost-mode fires vs 4 above-floor).
**Bottom line:** the misses on this drive are dominated by **offer availability** — the driver's
actual offer was **not in the matcher's scored queue at the PUDO** (15 of 21 pickups). That is
upstream of geocode/confidence/cadence/cluster, and it is the class the shipped §5.5/§9.9 fixes
target. The decisive test is a live `00649` drive (validation marker set); the event-ledger, once built, makes it a one-query read. Several other hypotheses were
investigated and **refuted** (recorded below so they aren't re-walked).

## Framing: the taps are the eval harness, not the product

`app_private.contest_labels` button presses are a **debug-only ground-truth instrument**. The
shipped product has **zero human PUDO input** — all pickup/dropoff identification is **fully
automated** by the matcher. So the catch rate (≈63% on this drive vs the ~95% launch gate) is
**autonomous-detection accuracy**, and any "fix" that relies on a human to correct a bad signal is
off the table. See memory `pudo-detection-is-fully-automated`.

## Method (ground truth; zero state impact)

38 taps on this drive. `contest_labels` has **no offer id / no coords**, so taps are cross-
referenced by time to `pudo_decision_context` (decision: `dispatch_executed`, `unmatched_reason`,
cluster fields, `arrest_duration_s`, `wai_per_offer_scores`) and `heartbeat_log`.

**Tap→offer attribution = receipt sequence** (NOT geocode, NOT scored-set membership — both proved
unreliable on a lost-mode drive). 21 ACCEPT offers ↔ 21 pickup taps, 1:1; the attribution is
validated because the FIFO-paired offer ids **increase monotonically with tap time**
(9127→9128→9129→9132→…→9166).

## ROOT CAUSE: the offer wasn't in front of the matcher

Pairing each pickup tap to its offer by receipt sequence, then checking whether that offer was in
the scored queue (`wai_per_offer_scores`) at the cluster:

- **15 of 21 pickup taps: the driver's actual offer was NOT in the scored queue at the PUDO.**
- When the offer **was** in the queue, it was caught **5 of 6** times (only 9166 in-queue-but-missed).

Availability is the discriminator. The offer was reaped/excluded from the live set (or never scored)
before the arrest — so there was nothing to localize or score. On a ~97%-lost-mode drive this is
exactly the pre-fix failure mode: fabricated idle anchors + no reaping discipline dropped valid
offers from the queue. **§5.5 (defer-don't-fabricate) + §9.9 + the band fix were built to stop it**,
which is why the next step is empirical: a fresh measured drive on `00649` (the live pipeline; the
validation marker is already set) directly shows how many of the 15 stay in the queue through the
PUDO and catch — with no matcher change. The event-ledger, once built, makes this a one-query read.

## What else is solid

- **Arrest + a formed cluster at all 38 taps** (arrest 15–66s within ~3.5s of the tap; cluster_size
  3–31). Neither arrest nor cluster-formation is a systemic failure.
- The **in-queue residual** is small (6 of 21 pickups in queue; 5 caught). Only here could geocode/
  localization plausibly matter — and it's **unverified** (see retractions).

## Hypotheses investigated and REFUTED (do not re-walk)

1. **HANDOFF #2: "`on_target_road` falsely ≈1.0 on transit roads → reweight proximity."** Refuted —
   on the scored misses `on_target_road = 0.00`, not 1.0; nothing over-confident to downweight.
2. **Arrest-gated horny / cadence bootstrap.** Refuted — a cluster formed at all 16 missed taps
   (size 3–19); they are not sample-starved, so 1 Hz adds nothing. The caught/missed cadence gap
   (4.87s vs 3.19s) was real but non-causal.
3. **"NO_CLUSTER dominates."** Retracted — `cluster_unavailable` is a transient per-heartbeat
   edge-of-envelope state, present even on caught taps; counting it per-tap was an artifact.
4. **"Geocodes are km-wrong = root cause."** Withdrawn — the km offsets (1.7–13.8 km) came from
   **scoring-based mis-attribution** (wrong offers), not bad pins. Geocode quality is unverified.
5. **WAI `_CONFIDENCE_WEIGHTS` reweighting.** Not the lever — there's no over-confident signal to
   downweight, and for an offer not in the queue, no weight matters.

## Distinct, still-open gaps

- **Multi-PUDO at one stop** (witnessed: pickup of one offer + dropoff of another at the same
  traffic light). The dispatcher fires one narrative per cluster (§5.3-mirror / two-PUDO,
  dispatch.py) — two events of two offers can't both register from one light-cycle cluster.
- **2 dispatch-gap anomalies** (14:58, 15:03): strong localization (signal sum 2.0) yet no fire
  within 90s — a dispatch/arbitration issue.
- **In-queue residual localization** (the 6, esp. 9166): only if it survives the live-drive validation.

## Implications for the fix backlog

- **The dominant lever is offer availability/liveness — already addressed by the shipped fixes.**
  Validate by a live `00649` drive before anything else; the miss count may collapse without new code.
- **De-scope "Fix #3 = WAI confidence tuning"** and the geocode/cadence theories (all refuted).
- The **product-correct direction** (memory `pudo-detection-is-fully-automated`): autonomous
  detection must rest on geocode-independent signals — TAD/odometer + sequence + breadcrumb +
  cluster — but that work is gated on what the live-drive validation leaves unrecovered.

## Next steps (ordered)

1. **Live measured drive on `00649`** (sentinel+reaping+band; validation marker already set) → tap
   the PUDOs and score how many stay in the queue and catch, with no matcher change. **Decisive.**
   (The event-ledger, once built, turns this into a one-query post-drive read.)
2. **Dropoff attribution** (17 dropoff taps) by the same sequence method, for completeness.
3. Whatever the live drive leaves missed → the in-queue residual: multi-PUDO arbitration, the 2
   dispatch anomalies, and (only if implicated) geocode-independent identification (TAD+sequence).

## Method caveats

- ~97% lost-mode flickers per-heartbeat states (`unmatched_reason`, `cluster_unavailable`) — do not
  bucket misses by them per-tap. Use the sequence attribution.
- "Caught within ±90s" over-credits when a *different* offer fires near a tap; the clean signal is
  in-queue→caught (5/6), not the not-in-queue "caught" flags.
- `contest_labels` taps are debug instrumentation only — not a runtime signal.
