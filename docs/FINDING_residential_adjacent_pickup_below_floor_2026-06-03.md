# FINDING — Residential-Adjacent Pickup Scores Below WAI Floor

**Drive:** 2026-06-03 AM (03:45–11:00 CT)
**Prod revision:** `puddlejumper-api-00641-rqf` (100%, us-central1)
**Status:** P0 launch-blocker. Mechanism fully localized. Fix direction RATIFIED
(Path B — adjacency re-weight); weight value NOT set — pending one-time offline backtest (no shadow mode).
**Driver:** `UjT1hE9eBXh2q95aSZYOkzDJ8lo1`
**Author:** Claude (proposes) → Gemini (review) → Andrew (decides) loop. Proposal-stage artifact.

---

## 0. SESSION OUTCOME IN ONE LINE

The three §2 confirmations all PASS. The drive surfaced one new, cleanly-localized
fault: **a correct residential-intersection pickup, arrested 3.5 minutes with the offer
live in queue, was rejected on WAI confidence (0.286 < 0.40 floor) because the driver
stopped on the adjacent street — the normal real-world execution for an intersection
geocode.** Frequency analysis (§3) shows this is NOT a rare edge case: ~14% of all
arrested legs over the available window, 8 of 9 in suburban residential intersections.

---

## 1. THE THREE §2 CONFIRMATIONS — ALL PASS

| Confirmation | Verdict | Evidence |
|---|---|---|
| §2.1 §XVIII bind holds | **PASS** | 7/7 single-offer pickups bound (`t`). The 2 `f` rows (9015, 9017) fired at the identical second 09:38:47 — §XIV.I §5.3 two-pickups-same-geocode, correct-by-design observation-only (no bind). |
| §2.2 zero KeyError (291 fix) | **PASS** | Zero KeyError rows on revision 641 across the drive. |
| §2.3 error-metric writer populates | **PASS** | Populating for the first time since 2026-04-30. Verified accurate to the meter (9011 pu 45.5m geom 45.5m; 9013 pu 116.8m geom 116.7m). |

---

## 2. THE HEADLINE FINDING — OFFER 8997

### 2.1 What happened (ground truth)
8997 = reserved ride (long wait). ACCEPT logged 04:03:21 while staged ~3.3 mi from the
pickup geocode. Driver drove to pickup and **arrested 3.5 minutes** (04:15:56→04:19:29,
GPS dead-still at 29.55598/−95.50250, `cluster_size`→11) **with 8997 live in queue the
whole time**. Pickup never observed. System fired only a `lost_mode_observation` at the
dropoff (04:47:03).

### 2.2 The arrest WAS at the pickup (detection layer is exonerated)
Stop trace: 04:07 South Post Oak traffic light (3.34 mi from geocode, inbound); 04:13:43
one tick at 0.06 mi; **04:15:56–04:19:29 dead-still at 0.16 mi (260 m) — THE PICKUP.**
The §XVI ladder arrested, built an 11-point cluster, ran the matcher every heartbeat for
3.5 min. The failure is purely WAI **scoring** of a correct arrest.

### 2.3 The arithmetic (from `wai_per_offer_scores` at 04:16:07)
Driver on **Elassona Lane** (`wai_current_road`, residential), adjacent to target
"Ardani Ln & Grand Olympia Dr, Fresno." Intersection-class weights × signals:

| signal | value | weight | contribution |
|---|---|---|---|
| cluster_tightness | 1.00 | 0.15 | 0.150 |
| cluster_duration | 0.36 | 0.10 | 0.036 |
| adjacent_road_match | 1.00 | 0.10 | 0.100 |
| proximity | 0.00 | 0.10 | 0.000 |
| breadcrumb_match | 0.00 | 0.30 | 0.000 |
| on_target_road | 0.00 | 0.20 | 0.000 |
| off_wire_pivot | 0.00 | 0.05 | 0.000 |
| **composite** | | | **0.286** |

Matches recorded `0.28555468` exactly. Floor `WAI_CONFIDENCE_THRESHOLD = 0.40`.

### 2.4 The structural wall (root cause)
1. **50% of the budget requires being ON the named target road.** `breadcrumb_match`
   (0.30) + `on_target_road` (0.20). Both zero when the driver stops on the adjacent
   street — the standard execution, because a geocoded intersection is not the curb.
2. **The rescue signal is under-weighted into irrelevance.** `adjacent_road_match` scored
   a perfect 1.00; weight 0.10 → contributes 0.100. Its own docstring (~line 475) says it
   exists for exactly the "on_target_road missed because the snap landed one street over"
   case — but at 0.10 it cannot lift the composite over 0.40.
3. **Proximity zeroed on a 10 m technicality.** `INTERSECTION_RADIUS_M = 250.0`,
   linear-decay to 0. Stop was 260 m → exactly 0.0.

**Realistic ceiling for an adjacent-street residential pickup ≈ 0.286; 0.40 floor
mathematically unreachable.**

### 2.5 Reserved-ride context — dwell/Horny logic UNCHANGED
8997 was reserved (long dwell). Dwell/Horny/arrest performed correctly. **Decision
(Andrew + Gemini): leave dwell and Horny unchanged.** The §XVI ladder is exonerated.

---

## 3. FREQUENCY ANALYSIS — THIS IS A COMMON COHORT, NOT AN EDGE CASE

Query: distinct (offer, leg) where `arrest_duration_s >= 5.0` AND
`adjacent_road_match = 1.0` AND composite confidence in the 0.25–0.40 dead zone.

**Window:** 2026-05-30 → 2026-06-03 (data limited to where `wai_per_offer_scores`
populates). 3 days had drives with this signature.

**Result: 9 distinct missed legs out of 57 matched legs total (~14% of all arrested
legs), across 3 active days (~3/day).** 8 of the 9 are suburban-subdivision intersections:

| offer | leg | target | best_conf | first_seen |
|---|---|---|---|---|
| 8740 | pickup | Gaelic Green St & Halcyon Time Trl | 0.380 | 06-01 |
| 8741 | pickup | Park Douglas Dr & Park Lorne Dr | 0.330 | 06-01 |
| 8842 | dropoff | Robin Knoll Ct & Youpon Glen Way | 0.332 | 06-02 |
| 8845 | pickup | Fairwood Knoll Ln & Iris Ridge W | 0.385 | 06-02 |
| 8848 | dropoff | W Grand Pkwy S, Sugar Land | 0.313 | 06-02 (freeway — §5, not this cohort) |
| 8858 | pickup | Chester Gables Dr & Jubilee Ct | 0.367 | 06-02 |
| 8997 | pickup | Ardani Ln & Grand Olympia Dr | 0.350 | 06-03 |
| 8999 | pickup | Bywood St & Woodwick St | 0.372 | 06-03 |
| 9017 | dropoff | Bayland Ave & Julian St | 0.378 | 09-54 |

All cluster at 0.31–0.385 — the dead zone just under 0.40.

**Caveat on the number:** window is 5 days, n=66 legs. Enough to refute "rare" decisively;
not enough to pin the exact rate (10–20% plausible). Direction is certain.

**§0.D.3 verdict:** initial instinct was to accept the loss (bounded miss beats unbounded
false-positive risk per §0.D.4). That default holds for *rare* cohorts. This cohort is
**not** rare — ~14%, concentrated in suburban residential (plausibly the dominant Houston
pickup geography). Accepting it systematically blinds the pricing model to a large,
non-random market slice. §0.D.3 ("recover the common cohort") flips the decision to FIX.

---

## 4. RATIFIED FIX DIRECTION — PATH B: ADJACENCY RE-WEIGHT (floor preserved)

### 4.1 What was rejected and why
- **Drop the floor 0.40→0.25 (Gemini's Temporal Adjacency Gate): REJECTED (Andrew).**
  Dropping the floor manufactures a pass for a low-confidence score. Missed beats wrong
  (§0.D.4). The 0.40 floor is a hard, honest bar and stays.
- **A new temporal constant N (60/90/120s): REJECTED (Andrew).** The system already has
  one arrest-time concept, `ARREST_DURATION_THRESHOLD_S = 5.0`. A second "how long is a
  real stop" number is arbitrary-threshold proliferation (Rule VII). Not introduced.
- **Widen `INTERSECTION_RADIUS_M` 250→350: REJECTED.** ~3 blocks in a dense grid; floods
  proximity contributions from unrelated parallel streets. §0.D.4 cache-corruption risk.
- **Accept the loss (Path A): REJECTED after §3.** Was the right default under a "rare"
  prior; the 14% frequency refuted the prior.

### 4.2 The fix (direction ratified, value NOT set)
Leave the floor at **0.40, unchanged.** Re-weight `adjacent_road_match` in
`_CONFIDENCE_WEIGHTS["intersection"]` (currently 0.10) so a *genuine* adjacent-street
residential pickup honestly earns its way over 0.40 on real evidence — NOT by lowering
the bar, but by correctly accounting for the fact that "stopped on the street adjacent to
the intersection geocode" is strong evidence of a residential pickup.

The signal already fires correctly (1.0 at the 04:16 pickup). The defect is that a
perfect adjacency detection is weighted into uselessness.

### 4.3 OPEN DESIGN FORK — for Gemini ratification: renormalize or not?
`_CONFIDENCE_WEIGHTS["intersection"]` sums to 1.00. Raising `adjacent_road_match` breaks
the sum unless the difference is taken from elsewhere.
- **Renormalize** (proportionally shave the other weights, keep sum=1.0): keeps confidence
  bounded in [0,1]; slightly dilutes `on_target_road`/`breadcrumb` (the currently-working
  cases, which score ~0.79 with miles of headroom — likely harmless). RECOMMENDED.
- **Don't renormalize** (raise adjacency, let max composite exceed 1.0): doesn't touch
  working cases; but confidence is no longer bounded at 1.0, risking any downstream code
  that treats confidence as a probability. The 0.40 floor comparison still works.

Claude recommends renormalize. Gemini to ratify.

### 4.4 Weight value — UNSET, derived from a ONE-TIME OFFLINE BACKTEST (NOT picked, NO shadow mode)
Do NOT pick a weight ("bump to 0.25 and see") — same trap as the rejected N threshold.
And **NO shadow-mode path** (Andrew, ratified): in-production shadow branches rot into
parallel paths nobody remembers, stagnate, and never get promoted. The weight is
**derived offline from data already on disk**, then a single edit lands on the live path.

Method:
1. **Write a throwaway offline analysis script** `tmp/backtest_adjacency_weight.py`
   (in `~/puddlejumper-prod/tmp/`, an out-of-band script per §XIV.H — NEVER ships).
   It reads historical `wai_per_offer_scores` from `pudo_decision_context` over the
   available window and **recomputes the composite arithmetically** at a sweep of
   candidate `adjacent_road_match` weights. The composite is pure
   `Σ(signal × weight)` — every input is already recorded; no production code runs.
2. The script reports, per candidate weight: **recovery** (how many of the 8 known
   residential legs in §3, and any siblings, now clear 0.40) vs. **false positives**
   (adjacency-1.0 arrests that were NOT pickups — incidental stops near an intersection
   that would newly cross 0.40).
3. **Read the curve, pick the weight, renormalize per §4.3, make ONE edit to
   `_CONFIDENCE_WEIGHTS["intersection"]` in `where_am_i.py`.** Direct to the live path.
4. **Delete the backtest script.** It was analysis, not infrastructure. Its output
   (the recovery-vs-false-positive curve + the chosen weight + the justification) is
   recorded in this doc as the record of *why this weight*; the script does not linger.

This delivers what shadow mode was for — derived weight, false-positive check, evidence
trail — with ZERO parallel production paths and ZERO permanent scaffolding. It is an
offline recompute against historical data (the §XIV.H out-of-band pattern, same as the
§XVII backtest scripts), not a live shadow branch.

**Honest limitation:** a backtest over the 05-30→06-03 window validates against the data
we have; it cannot see false positives in geography not yet driven. This is strictly
better than guessing the weight blind, and strictly better than shadow mode (same blind
spot, plus the spaghetti). If a novel geography later surfaces a false positive, the
flight recorder (`pudo_decision_context`) catches it and the single weight number is
adjusted — one edit, no scaffolding to unwind.

### 4.5 Backtest read order (within the single offline run)
- **First slice:** last-48h residential drives — fast confirmation the re-weight recovers
  the 8 known legs (recovery check).
- **Then:** full 2026-05-30→06-03 window — false-positive safety across varied geography
  (safety check).
Narrow slice proves recovery; broad slice proves safety. Both read from the same one-time
offline run before the single live edit.

---

## 5. SECONDARY FINDING — Road-Name / Freeway Geocode Drift
Error-metric writer (newly live) exposed: geocode error scales with the physical extent
of the named feature. Surface intersection/short street → tight (46–240 m); long surface
road → 1.5–2.8 km; freeway/farm-road/freeway-interchange → 1.4–20 km. (8848 "W Grand Pkwy
S" in §3 is this, not the adjacency cohort.) §XVI made visible — geocode is an
unbounded-error hypothesis, arrest is truth. Loses no observations (arrest coords cache
correctly). It is the input signal the **GC spatial gate (§4.6)** has been starving for.

---

## 6. RED-SCREEN SENTINEL (§2.4) — RESOLVED AND TABLED
NOT the 291 crash. Client-side render on the **iPhone web monitor**
(`app.puddlejumper.io`), kept open to leave the Android device clear for Uber +
PuddleJumper. Data layer 100% healthy at fire windows: 400/400 heartbeats 200, dropoff
fired + persisted, contest tap landed. Display-only, zero observation cost. **Tabled**
(mechanism proven, trigger unproven; revisit only if it ever coincides with an actual
observation miss). If pursued: **web dashboard repo, NOT `Puddle_Jumper` Android** — the
handoff's "grep the Android repo" pivot was built on the wrong assumption.

---

## 7. WHAT IT IS NOT (six theories died to data — do not re-chase)
Red-screen=291 crash (killed: zero 500s, 400/400 200); GPS quality (4–5 m, sub-second);
arrest jitter (cluster reached 11); cold-start §XVIII binding (bind never reached);
§4.7 ingestion lag (server makes accept; Android sensor-only); OCR capture miss (accept
was logged, offer live in queue); predicate gating (odometer inside window, causality
guard passed). The mechanism is WAI **scoring**, full stop.

---

## 8. NEXT STEPS
1. Gemini ratifies Path B + the §4.3 renormalize fork.
2. Write `tmp/backtest_adjacency_weight.py` (out-of-band, §XIV.H) — recompute historical
   composites at candidate adjacency weights from `wai_per_offer_scores`. No production
   code runs; nothing written to cache.
3. Read the recovery-vs-false-positive curve (§4.5 order: 48h residential, then full
   window); pick the weight; renormalize per §4.3.
4. Single edit to `_CONFIDENCE_WEIGHTS["intersection"]` in `where_am_i.py` via standard
   L-3 apply path. Record the curve + chosen weight + justification in this doc. Delete
   the backtest script.
5. Secondary: feed §5 geocode-drift data into the GC spatial-gate (§4.6) work.
