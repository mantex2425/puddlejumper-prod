# Phase 2c.2 Design Snapshot — Dynamic Odometer Signal Addition

**Date:** 2026-05-06 (end of full-day Phase 2c.2 architectural session)
**Status:** Design notes for Phase 2c.2 resumption. Not yet ratified for implementation.
**Predecessor:** Original Phase 2c.2 design (Head 4, 150m radius, single_road=0.40, etc.) ratified earlier 2026-05-06.

---

## Summary

A late architectural conversation introduced a NEW signal concept that should be added to Phase 2c.2's scope alongside Head 4: the **Dynamic Odometer** (per-offer trip-progress signal).

The data infrastructure for this signal is being captured in the gate layer sprint (kickoff amendment 1). When Phase 2c.2 resumes, the schema columns will already exist with populated data accumulating across all offers received since gate layer ship.

---

## The architectural concept

**Current odometer gate (in BEAD, ported to gate layer sprint):** binary, dropoff-only, applied to current offer's `pickup_miles`. Gate fails -> matcher held entirely.

**Dynamic Odometer signal (Phase 2c.2 deliverable):** continuous per-offer signal computed during matcher evaluation.

For each offer in the queue at evaluation time:
- Compute `delta_miles = cluster_evaluation_cumulative_miles - offer.miles_at_offer_receipt`
- Compare against `offer.pickup_miles` (Uber-reported estimate from offer card)
- Score how closely `delta_miles` matches `pickup_miles`
- Higher match score = stronger evidence this cluster corresponds to THIS offer

This is a disambiguator. When 3 offers are on the queue and a cluster fires, the offer whose `delta_miles` best matches its `pickup_miles` is the strongest candidate for what the driver actually arrived at.

---

## Why this matters

The original Phase 2c.2 design (Head 4 + 150m radius + class-specific weights) handles the case "is this cluster at a real POI?" reasonably well. It does NOT handle "WHICH offer in the queue does this cluster correspond to?"

Three offers on queue scenarios where this is critical:
1. **Pickup ambiguity:** driver has 1 active matched offer + 2 declined offers still in `offer_history`. All three might have pickups in the same general area. Matcher needs to identify which one fired.
2. **Stacked rides:** simultaneous matched + accepted creates queue contention. The Dynamic Odometer can disambiguate "you just dropped off offer A and arrived at offer B's pickup" from "you're still mid-route on offer A."
3. **Mid-shift offer accumulation:** drivers accumulate 5-10 offers per shift. Without per-offer trip-progress, the matcher uses geographic heuristics alone, which Houston-no-zoning makes unreliable.

---

## Scoring function (proposed, to be ratified at resumption)

Gemini's proposed Gaussian decay:

```
score = exp(-((delta_miles - pickup_miles) / (sigma * pickup_miles))^2)
```

Where:
- `sigma` controls falloff steepness (probably 0.10-0.15 based on real Uber estimate variance)
- Score = 1.0 when `delta_miles == pickup_miles` (perfect match)
- Score ~ 0.6 when delta is 15% off (still meaningful)
- Score -> 0 as delta diverges further

The actual `sigma` should be tuned against real data collected during gate layer sprint and the validation period before Phase 2c.2 resumes. Empirically derive it from the distribution of `(delta_miles - pickup_miles) / pickup_miles` for known-correct PUDOs.

Alternative: linear with cliff edge at 25% off:
```
score = max(0, 1.0 - 4 * |delta_miles - pickup_miles| / pickup_miles)
```

Simpler but less forgiving of routing variance. Gaussian is preferred unless tuning data shows it's overkill.

---

## Pickup vs. dropoff treatment

The signal applies asymmetrically:

**Dropoff PUDO evaluation:** `pickup_miles` is the Uber-estimated trip distance from pickup to dropoff. `delta_miles` since `miles_at_offer_receipt` overshoots by the pickup-acquisition leg. Better metric is `delta_miles_since_pickup_confirm` vs `trip_miles`.

**Pickup PUDO evaluation:** `pickup_miles` is the estimated distance from offer-receipt position to pickup. `delta_miles` since `miles_at_offer_receipt` directly corresponds to this. Cleanest match case.

Phase 2c.2 design needs to:
1. Distinguish pickup-PUDO evaluation from dropoff-PUDO evaluation
2. Use different reference distances (pickup_miles vs trip_miles)
3. Use different anchor points (offer-receipt vs pickup-confirm)

The schema captured at offer receipt only supports the pickup case directly. For dropoffs, Phase 2c.2 may need ADDITIONAL columns (`miles_at_pickup_confirm` etc.) or may need to compute the dropoff anchor by querying the actual_pickup_at timestamp's heartbeat row.

This is a Phase 2c.2 design detail to ratify at resumption.

---

## Integration into _CONFIDENCE_WEIGHTS

A new signal needs weight-table integration. Per-class weight allocation TBD at resumption, but rough hypothesis:

| Class | dynamic_odometer weight | Reasoning |
|---|---|---|
| `intersection` | 0.10 | Backup signal; intersection class has strong other heads |
| `single_road` | **0.20** | Strong; Uber's road-class geocode is unreliable, miles is the rescue |
| `number_on_street` | 0.15 | Moderate; geocode usually accurate, miles confirms |
| `poi` | 0.10 | Backup; Head 4 carries the load |

The 0.20 single_road weight is the architectural insight: when geocoded coords are unreliable (road-class), the trip-progress signal becomes a critical disambiguator. This allocation would shift weight from `breadcrumb_match` or `on_target_road` (which also depend on geocoded reference) toward `dynamic_odometer`.

Final weight table balancing happens at Phase 2c.2 resumption, with the constraint that all rows sum to 1.00.

---

## What gate layer sprint accomplishes for this

When gate layer ships (kickoff amendment 1):

- `offer_history.miles_at_offer_receipt` column exists, populated for new offers
- `offer_history.lat_at_offer_receipt`, `lng_at_offer_receipt` columns exist, populated for new offers
- Real production offers accumulate this data starting day 1 of post-deploy

By Phase 2c.2 resumption (estimated 2-3 days later):
- 50-200 real offers with receipt data captured (depending on driving volume)
- Empirical distribution of `(delta_miles - pickup_miles) / pickup_miles` available for sigma tuning
- The signal can be designed against real data, not theoretical assumptions

---

## Open questions for Phase 2c.2 resumption

1. **Sigma tuning:** what's the right falloff width? Empirical from collected data.
2. **Pickup vs dropoff anchor:** for dropoff evaluation, do we need `miles_at_pickup_confirm` as additional schema? Or compute on-the-fly from heartbeat history?
3. **GPS-at-receipt usage:** does Phase 2c.2 actually use the lat/lng columns we captured, or only the miles? If only miles, document the lat/lng columns as forward-collateral.
4. **NULL handling:** offers without receipt data (legacy or edge cases) — how does the signal degrade? Probably fall back to `score = 0` (no contribution) rather than penalize.
5. **Interaction with existing odometer gate:** the binary 0.9x gate (in gate layer sprint) and the continuous Dynamic Odometer signal could conflict. Probably the gate stays as a hard floor (definitely not a dropoff if not 90% there) and the signal does fine-grained disambiguation above the gate. But ratify this at resumption.

---

## What this means for Phase 2c.2 scope

Original Phase 2c.2 deliverables:
- Replace lexical `_signal_poi_match` with semantic `_signal_poi_type_match` (Head 4)
- Add `CLASS_TO_TYPE_MAP` constant
- Bump `API_SEARCH_RADIUS_M` 50 -> 150
- Revise weight tables (single_road `poi_match` 0.30 -> 0.40, etc.)
- Delete `apartment_complex` weight row and dispatcher entry
- Tests

NEW Phase 2c.2 deliverables (from this snapshot):
- Add `_signal_dynamic_odometer` function reading the receipt columns
- Integrate into `_CONFIDENCE_WEIGHTS` with per-class weights
- Handle pickup vs dropoff asymmetry
- Possibly add `miles_at_pickup_confirm` column if dropoff evaluation needs it
- Tests covering signal scoring, NULL handling, pickup-vs-dropoff distinction

Phase 2c.2 grows from a single-purpose Head 4 commit to a two-signal commit (Head 4 + Dynamic Odometer). Still atomic, but larger scope.

---

## End of snapshot