# ERRATUM — Odometer / GC Spec Reconciliation (corrections to FINDING_ODOMETER_GC_SPEC_RECONCILIATION_2026-06-05.md)

**Date:** 2026-06-05 (same day as the FINDING it corrects)
**Status:** PROPOSED — awaiting ratification (Andrew + Gemini + Claude).
**Corrects:** `docs/FINDING_ODOMETER_GC_SPEC_RECONCILIATION_2026-06-05.md` (RATIFIED earlier
this session). This erratum amends the FINDING's central mechanism claim (§4 "missing bridge
term") and pins the band's exact per-leg shape against the *deployed* TAD gate algebra. The
FINDING's fix *direction* stands; its *explanation of why* was wrong and is corrected here.
**Origin:** Step 5 recon + the Step-6 band-shape design pass. The units algebra against the
deployed `evaluate_tad_gate` (tad.py) forced three corrections and confirmed four points the
FINDING left open or stated imprecisely.
**Companion canon:** §0, §XIV.H, §XVI (esp. §XVI.C as deployed), §XVIII, §5.5 (deferred
sentinel), Step 5 §6.5 resolution (orphan trigger).

---

## 0. Why this erratum exists

The FINDING diagnosed the liveness-predicate distance gate and prescribed the ±15% band. In
doing so it introduced a "missing bridge term" as the *central mechanism* of the 9132 loss
(§4 CRITICAL CORRECTION, §5.1). The Step-6 design pass did the units algebra against the
**deployed** TAD distance gate and proved the bridge term is a phantom: the deployed gate never
used one, does not need one, and the real 9132 mechanism is narrower (a wrong-column read in the
liveness predicate). Striking the bridge term collapses a large branch of complexity that was
causing repeated re-derivation. This erratum records the correction so future sessions inherit
the simpler, correct model and do not re-introduce the phantom.

---

## 1. PROVEN (algebra, not inference) — carry as settled

### 1.1 Pickup-leg band ≡ deployed TAD gate, term-for-term

The deployed `evaluate_tad_gate` pickup branch (tad.py ~526–533) computes:

```
expected_distance  = state.expected_pickup_distance        # tad.py receipt anchor
leg_start_odometer = expected_pickup_distance − pickup_miles
actual_delta       = current_odometer − leg_start_odometer
expected_delta     = pickup_miles
completion_pct     = actual_delta / pickup_miles
gate: 0.85 ≤ completion_pct ≤ 1.15
```

Substituting the lower bound `completion_pct ≥ 0.85`:

```
(current_odometer − (expected_pickup_distance − pickup_miles)) / pickup_miles ≥ 0.85
current_odometer − expected_pickup_distance ≥ −0.15 · pickup_miles
current_odometer ≥ expected_pickup_distance − 0.15 · pickup_miles
```

Upper bound `≤ 1.15` rearranges symmetrically to:

```
current_odometer ≤ expected_pickup_distance + 0.15 · pickup_miles
```

Therefore the deployed TAD pickup gate is **exactly**:

```
| actual_odometer − expected_pickup_distance | ≤ 0.15 · pickup_miles
```

This is the FINDING's ±15% band on the pickup leg, expressed as a ratio rather than an absolute
difference. **They are the same quantity.** The band the FINDING wanted to "build" already exists
on the candidacy axis for the pickup leg.

### 1.2 §XVI.C is deployed and consistent — NOT canon-vs-code drift

An earlier read of `tad.py` `if verdict.passed is False: continue` was misinterpreted as a
candidate-inclusion gate, which §XVI.C forbids. It is not. Per
`docs/PHASE_2C_2_ITEM_3B_3D_HANDOFF.md:63` and `docs/CANONICAL_RULES.md:981`, the `continue` is
the **Google-spend wallet gate**: a too-early / overshot offer is skipped from *expensive reverse-
geocode spend*, not excluded from *matching*. §XVI.C forbids the latter (TAD gating candidate
inclusion at the matcher boundary); it does not forbid the former (TAD controlling when money is
spent geocoding). The amendment is thoroughly shipped — deprecation breadcrumbs at
`CANONICAL_RULES.md:1106` (`tad_and_wai` labels), `:1120` (`tad_failed` DEPRECATED 2026-05-22),
`:1692`/`:1713` (lost-mode "TAD bypass" marked historical/subsumed), and `project_pj_db_schema:73`
(`phase_reached` WRITE-FROZEN per §XVI.C). **No reconciliation needed. No Gemini question.**

### 1.3 Dropoff-leg width = `trip_miles` (ratified this session)

The deployed TAD dropoff branch (tad.py ~629–635) uses `expected_delta = trip_miles` and anchors
`leg_start_odometer = cumulative_miles_at_pickup_fire`. The band width is therefore
`0.15 · trip_miles`. The FINDING-era erratum draft briefly proposed `0.15 · (pickup_miles +
trip_miles)`; that proposal is **withdrawn**. The dropoff width is `0.15 · trip_miles`, matching
the deployed gate.

---

## 2. CORRECTION — strike the bridge term

### 2.1 What the FINDING claimed

FINDING §4 ("Why this lost the 11 PUDOs (the missing bridge term)") and §5.1 frame the fix as
adding a remaining-current-trip bridge term to `expected_odometer`:

```
expected_odometer = odometer_at_offer_receipt
                  + remaining_distance_of_current_trip_at_receipt   ← the "bridge term"
                  + pickup_miles (+ trip_miles)
```

### 2.2 What the deployed algebra proves

The deployed gate measures `actual_delta` against the leg distance directly, anchored at
`expected_*_distance`. There is **no bridge term in the gate**. The pickup-leg gate is
`|actual_odometer − expected_pickup_distance| ≤ 0.15·pickup_miles` (§1.1); the dropoff-leg gate
is `|actual_odometer − expected_dropoff_distance| ≤ 0.15·trip_miles` (§1.3). The
`expected_*_distance` anchors come from tad.py's idle/stacked classification at receipt — and the
"stacked" path's use of `prev.expected_dropoff_distance` is the only thing resembling a bridge
term, which is precisely the orphan-branch logic Step 5 §6.5 already addressed (and which is being
re-triggered on distance-overshoot, not time).

**The bridge term is a phantom.** It was introduced by the FINDING to explain 9132, but the gate
never used one and does not need one.

### 2.3 The actual 9132 mechanism (corrected)

9132 was reaped by the **liveness predicate** (`LIVE_OFFER_PREDICATE_SQL`), whose pre-pickup
branch consumed `miles_at_offer_receipt` (a dumb odometer snapshot) instead of
`expected_pickup_distance` (tad.py's receipt anchor). That is a **wrong-column bug**, not a
missing-bridge-term bug. The fix is to point the liveness band at `expected_pickup_distance` /
`expected_dropoff_distance` and apply the per-leg ±15% band — the same quantity TAD already gates
on the candidacy axis. No new bridge-term arithmetic is computed anywhere.

### 2.4 Disposition

- FINDING §4 title and body: the "missing bridge term" framing is **struck**. Replace with: the
  liveness predicate consumed the wrong receipt-time column (`miles_at_offer_receipt` instead of
  `expected_pickup_distance`); the fix is column-correction + per-leg band, no bridge term.
- FINDING §5.1: the `+ remaining_distance_of_current_trip_at_receipt` line is **struck**.
  `expected_odometer` per leg is simply tad.py's existing `expected_pickup_distance` /
  `expected_dropoff_distance` anchors, consumed as-is.

---

## 3. CLARIFICATION — doc-hardening to FINDING §2

FINDING §2 correctly states TAD "became an input to WAI's confidence score, not a separate gate
(§XVI.C)." Add one clarifying line so the deployed `continue` is not re-misread by a future
session:

> The `passed=False → continue` in `evaluate_tad_gate` is the Google-spend **wallet gate**
> (PHASE_2C_2_ITEM_3B_3D_HANDOFF §63; CANONICAL_RULES §981) — it skips expensive reverse-geocode
> spend for a too-early/overshot offer. It is NOT a candidate-inclusion gate. §XVI.C forbids the
> latter, not the former. Do not read this `continue` as a §XVI.C violation.

---

## 4. THE PINNED BAND (the Step-6 target spec — complete, no open questions)

Per leg, centered on the absolute cumulative anchor, width scaled to that leg's distance:

```
PICKUP LEG (actual_pickup_at IS NULL):
    center    = expected_pickup_distance          (tad.py receipt anchor; NO bridge term)
    tolerance = max( 0.15 · pickup_miles,  NOISE_FLOOR_MI )
    live iff  | actual_odometer − expected_pickup_distance | ≤ tolerance

DROPOFF LEG (actual_pickup_at IS NOT NULL):
    center    = expected_dropoff_distance          (re-anchored at pickup-fire)
    tolerance = max( 0.15 · trip_miles,  NOISE_FLOOR_MI )
    live iff  | actual_odometer − expected_dropoff_distance | ≤ tolerance
```

- **NOISE_FLOOR_MI = 2.0** (§6.2, ratified). Short trips whose ±15% is tighter than sensor noise
  are floored here.
- **Upper edge** = liveness/reaping (offer overshot → reap from projection). This is the only
  reaping mechanism for normal offers (no time ceiling, no distance cap — both struck per §5.3).
- **Lower edge** in the *candidacy* axis (TAD's `continue` wallet gate / WAI exclusion) — NOT in
  the liveness predicate. A not-yet-reached offer must stay live (driver still en route); it is
  withheld from spend/scoring, not reaped. (Resolves the "two edges, two layers" design: liveness
  predicate owns the upper edge; candidacy owns the lower.)
- **NULL anchor (either leg)** → §5.5 deferred sentinel: no band, offer stays alive, resolved at
  dropoff-disambiguation or the 4-hour abandonment ceiling. The dropoff-leg lost-mode case (pickup
  never fired → `cumulative_miles_at_pickup_fire` IS NULL) routes here — confirmed, no new
  mechanism (ratified this session).

---

## 5. STEP-6 IMPLEMENTATION SHAPE (recorded; not authored here)

This erratum closes the *spec*. Step 6 *implementation* is a separate, focused effort (its own
recon of the 6 LIVE_OFFER_PREDICATE_SQL compose sites + bind-tuple propagation + the source-
identity test). The shape, for the next session's runway:

1. **Liveness predicate distance-band clause — wholesale replacement** (the conflated three-era
   CASE → the §4 per-leg band). Other three predicate clauses (causality guard, abandonment
   ceiling, odometer-staleness gate) **untouched**.
2. **Delete:** `miles_at_offer_receipt` from the band, `GC_BUFFER_MULT (1.25)`, `GC_MIN_DIST_MI`
   as a trip-buffer floor, `GC_MAX_DIST_MI (50)` cap, the post-pickup `*0.25` buffer, and the
   per-trip time ceiling. **Add:** `expected_pickup_distance` read, the per-leg ±15% band, the
   2.0mi noise floor.
3. **Dead-code eviction:** line-746 `window_min` time-projection (vestigial — `window_min` is
   docstring-only, `raw_min` is computed-and-discarded; settled by §5.3, NOT a new scope
   question). **L-6 note:** `test_driver_queue.py` imports `GC_BUFFER_MULT`, `GC_MIN_MINUTES`,
   `GC_MAX_MINUTES`, `GC_NULL_*` — the constant deletions must reconcile the test import block
   inline or the suite breaks on import.
4. **Encapsulated band primitive** (Andrew's "one owner"): a single accessor
   `(leg, expected_*_distance, leg_distance) → (center, tolerance)` that the liveness predicate
   reads from. If Gemini ratifies unification, TAD's candidacy gate reads the same primitive —
   making the band a single-source quantity across both axes (the encapsulation thesis). Whether
   to unify TAD's gate onto the primitive in Step 6 or defer that to a follow-up is a Step-6
   scoping call, not a spec question.
5. **L-3 rigor** concentrated on bind-tuple arity propagation across the 6 compose sites + the
   source-identity drift test (§XIV.H). The band's SQL text is the low-risk part; the bind
   threading is where silent positional corruption hides.
6. **Step 5 (§6.5 orphan trigger)** lands in the same tad.py-touching neighborhood — sequence
   Step 5's time→distance-overshoot swap and Step 6's band so they do not collide; both are
   distance-driven and disjoint by condition (orphan = post-cancellation tracked trip; deferred
   sentinel = lost-mode no-pickup), per the Step 5 resolution.

---

## 6. Honesty ledger (proven vs corrected vs confirmed)

**Proven (algebra/source, this session):**
- Pickup-leg TAD gate ≡ ±15% band on `expected_pickup_distance`, term-for-term (§1.1).
- §XVI.C deployed + consistent; the `continue` is the wallet gate (§1.2).
- Dropoff-leg width is `trip_miles` in deployed code (§1.3).

**Corrected (this erratum):**
- The "missing bridge term" is struck — phantom; the gate never used one (§2). The real 9132
  mechanism is the liveness predicate's wrong-column read (§2.3).

**Confirmed (ratified this session):**
- Dropoff width `trip_miles` (not `pickup+trip`).
- NULL anchor (either leg) → §5.5 deferred sentinel; lost-mode dropoff included; no new mechanism.

**Still inferred (carried from the FINDING, unchanged):**
- That the liveness-predicate wrong-column fix *specifically* explains 9132's eviction remains
  inferred until Step 7 replays 9132 against a deployed revision containing the Step-4 logging +
  the Step-6 band. The spec is justified independent of that confirmation; Step 7 closes the loop.
