# FINDING — Odometer / GC Spec Reconciliation

**Date:** 2026-06-05
**Status:** RATIFIED 2026-06-05 (Andrew + Gemini + Claude). Canonical odometer/GC law.
Open threads §6.2/§6.3 closed by ratification; §6.1 resolved by recon (see §6.1).
One NEW open thread opened during recon: §6.5 (tad.py time-classification vs §5.5).
No code until the §7 sequence reaches the implementation steps.
**Origin:** Review of the 2026-06-04 drive (rev `00642-fg7`). Began as a haircut/adjacency
validation (HANDOFF_2026-06-04_PM_drive_review.md); the data redirected it into a
candidate-set eviction investigation, then into an odometer-subsystem spec reconciliation.
**Ratification protocol:** Andrew + Gemini + Claude. This document is the artifact under review.
**Companion canon:** §0 (Prime Directive), §XIV.H (live offer predicate), §XVI (arrest-defined
truth — TAD-as-score not gate), §XVIII (lost mode), RIDE_LIFECYCLE.md, SIMPLIFIED_ARCHITECTURE.md.

---

## 0. One-paragraph summary

The 2026-06-04 drive caught 19 of ~30 scoped PUDO events (~63%), far under the 95% launch gate.
All true misses share one signature: at the arrest, every WAI spatial signal read exactly 0.00,
because the offer the driver was physically servicing was **absent from the candidate set WAI
scored** — not mis-scored, never presented. Tracing the candidate-set builder
(`_get_alive_unpicked_offer_ids`, which correctly composes `LIVE_OFFER_PREDICATE_SQL`) led to the
GC distance gate. That gate reaps offers on a formula whose constants (a `×1.25` buffer on trip
distance, a `[2.0, 50.0]`-mile clamp) **match no ratified spec** — they are not the ±15%
expected-odometer band the system's author intended. The intended rule exists in Andrew's head
and partially in an *unratified* design doc (`PHASE_2C_2_DYNAMIC_ODOMETER_SNAPSHOT.md`, marked
"not yet ratified"), while the doc that calls itself product law (`RIDE_LIFECYCLE.md`) describes a
**time-based** GC that was replaced by the distance gate on 2026-05-19 and never updated.
INDEX.md points at none of these. The root condition is: **the odometer/GC subsystem has no
current canonical spec, which is exactly why it fragmented across five call sites and why no one
can say whether any given eviction was correct.** This document reconciles the spec, then
prescribes encapsulation to match it.

---

## 1. The triggering evidence (the 9132 case)

Ground truth: 38 manual `contest_labels` taps on 2026-06-04 (Andrew marking real PUDO locations;
no state impact, pure ground truth). Cross-referenced against system fires:

- 38 taps → 19 caught, 8 out-of-scope (`queue_actually_empty` — parked, no live offer), 11 true
  misses (offer present, no fire).
- Every true miss: WAI `best_conf` 0.16–0.30, all spatial signals (`proximity`,
  `on_target_road`, `adjacent_road_match`, `breadcrumb_match`) = 0.00. Confidence was pure
  stop-physics floor (cluster_tightness + cluster_duration only).
- **Not the haircut** (no miss in the [0.40, 0.45] band; `adjacent_road_match` never even fired).
- **Not the matcher** (`adj`/`prox`/etc. = 0.00 means the offer was never compared, not
  mis-scored).

Canonical case — tap 455 (dropoff, 05:23 CT):
- Driver's actual position was **70 m** from offer **9132**'s dropoff geocode.
- WAI's scored candidate set at that arrest contained only **9131** (2.5 mi away, DECLINE) and
  **9133** (received later, ACCEPT). **9132 was absent.**
- An offer whose geocode is 70 m away cannot score 0.00 proximity unless it was never in the
  candidate set. Confirmed: the candidate-set builder excluded a live, accepted, unpicked offer
  the driver was physically on top of.

The drive ran almost entirely in **lost-mode** (133 lost-mode fires vs 4 `wai_above_floor`;
`current_offer_id` essentially never bound). This is central — see §4.

---

## 2. TAD history (reconciled — Andrew's memory confirmed against canon)

- **Was:** TAD (Time And Distance) ran as a hard **gate** — pass/fail, blocked the matcher.
- **Became:** an **input to WAI's confidence score**, not a separate gate (§XVI.C amendment,
  2026-05-22: "TAD is input to WAI, not a separate gate").
- **Time half dropped:** the per-trip time horizon was retired in favor of a pure **distance**
  staleness gate (predicate comment: "Odometer Staleness Gate (2026-05-19) replaces per-trip
  time horizon"). Time was flaky; distance is the truer signal. Andrew's recollection is correct.

The surviving distance half now lives in **two** places that should be one rule:
1. A **scoring signal** — the "Dynamic Odometer" head (designed, **never built**).
2. A **GC reaping gate** — the `LIVE_OFFER_PREDICATE_SQL` distance clause (built, with unsourced
   constants).

---

## 3. The three-way drift (doc vs. doc vs. code)

### 3.1 What `RIDE_LIFECYCLE.md` says (the "product law" doc, 2026-05-04)
Queue projection uses a **time** window:
`LEAST(GREATEST((pu_min + trip_min) * 1.5, 15), 240)` **minutes** from offer creation.
This gate was replaced by the distance gate 15 days later and the doc was never updated.

### 3.2 What `PHASE_2C_2_DYNAMIC_ODOMETER_SNAPSHOT.md` says (status: NOT YET RATIFIED)
A per-offer scoring signal: `delta_miles` vs `pickup_miles`, Gaussian decay, "~0.6 when delta is
15% off." This is the ±15% model Andrew carries — but as a **matcher signal**, never as the GC
gate, and **never implemented**.

### 3.3 What the code actually runs (`LIVE_OFFER_PREDICATE_SQL`, pre-pickup branch)
```
live  iff  current_odo < miles_at_offer_receipt
                         + LEAST( GREATEST( (pickup_mi + trip_mi) * 1.25, 2.0 ), 50.0 )
```
Constants: `GC_BUFFER_MULT = 1.25`, `GC_MIN_DIST_MI = 2.0`, `GC_MAX_DIST_MI = 50.0`.

This is **none of the above**. It is a one-sided ceiling (+25% of *trip distance*, not a ±15%
band on *expected odometer*), with a clamp that has no basis in the intended model. Andrew did
not recognize the `1.25` because it is not his rule.

### 3.4 The consequence
Three different rules wear the same job. Encapsulation alone cannot fix this — the rule must
*exist* in one ratified place before code can converge on it.

---

## 4. Why this lost the 11 PUDOs (the missing bridge term)

The intended `expected_odometer` for an offer is **where the odometer should read when the driver
reaches that offer**. The code's pre-pickup formula assumes the driver heads to the new pickup
*immediately from the receipt position*. That is wrong whenever an offer arrives **while a trip
is already in progress**: the odometer keeps climbing through the *remainder of the current trip*
before the driver ever starts toward the new offer's pickup.

The **GC gate** has no bridge term. So an offer received mid-trip gets a gate ceiling that is too
low, and ordinary driving blows past it before the driver circles back — the offer is reaped while
still live. This is the 9132 mechanism, and in lost-mode (driver effectively always mid-segment)
it **compounds**: each reaped offer fails to fire its pickup, `current_offer_id` stays NULL.

**CRITICAL CORRECTION (from §6.1 recon):** the *system* does NOT lack the bridge term — `tad.py`
**already computes it correctly at receipt** and persists it to `offer_history` as
`expected_pickup_distance` / `expected_dropoff_distance` (absolute odometer targets, not deltas).
Its idle/stacked logic is exactly §5.1: idle → `current_odometer + pickup_miles`; stacked →
`prev.expected_dropoff_distance + pickup_miles`. The defect is narrower and worse than "missing
math": **the GC gate consumes the WRONG receipt-time estimate.** Two fingers computed receipt-time
distance — `miles_at_offer_receipt` (a dumb odometer snapshot) and `tad.py`'s
`expected_pickup_distance` (the correct anchor with the bridge term) — and the gate's pre-pickup
branch reads the dumb one. The fix is to point the band at the anchor that was already there. This
is the encapsulation problem (§5.7) in its purest form: the cost of two unreconciled fingers on
one quantity was 11 lost PUDOs.

---

## 5. THE RATIFIED SPEC (the rule to adopt)

### 5.1 Expected odometer
For each offer, per leg:
```
expected_odometer = odometer_at_offer_receipt
                  + remaining_distance_of_current_trip_at_receipt
                  + pickup_miles                       (pickup leg)
                  + trip_miles                         (add for the dropoff leg)
```

**IMPLEMENTATION NOTE (ratified, from §6.1 recon):** Do NOT compute a new formula. This exact
quantity already exists in `offer_history`, written by `tad.py:compute_offer_expectations` at
receipt:
- pickup leg `expected_odometer` = `offer_history.expected_pickup_distance`
- dropoff leg `expected_odometer` = `offer_history.expected_dropoff_distance`

`tad.py` (lines ~255–307) implements the idle/stacked split verbatim: idle case
(`pickup_distance_anchor = current_odometer`) → `current_odometer + pickup_miles`; stacked case
(`pickup_distance_anchor = prev.expected_dropoff_distance`) → bridge term included. These are
absolute odometer targets ("NOT a delta — a target absolute reading", `tad.py:131`). The GC band
(§5.2) must **consume these columns** and the predicate's naive pre-pickup ceiling
(`miles_at_offer_receipt + (pickup+trip)*1.25` clamped) must be **deleted**. The fix is
consolidation, not new computation — see §5.7.
- **Idle receipt** (no ride in progress at receipt): the bridge term is 0; the formula reduces to
  `receipt_odo + pickup_miles`. No special case — it falls out of the general form.

### 5.2 Liveness — a two-sided band
An offer is live iff:
```
| actual_odometer - expected_odometer |  <=  max( 0.15 * expected_odometer, NOISE_FLOOR_MI )
```
- **Upper edge** (`actual > expected * 1.15`): reap. The driver has travelled too far for this to
  be the live trip. This is the *only* reaping mechanism for normal offers.
- **Lower edge** (`actual < expected * 0.85`): exclude from the candidate set (not yet reached)
  but **do not destroy** — the driver may still get there.
- **Noise floor:** the band is never narrower than `NOISE_FLOOR_MI` (carried as 2.0 from the old
  `GC_MIN_DIST_MI`, **value to be ratified** against real odometer jitter — short trips like a
  1.6-mile fare have a ±15% band of ±0.24 mi, tighter than sensor noise).

### 5.3 Reaping is emergent — no time ceiling, no distance cap
- **No per-trip time ceiling.** Removed. Distance is the true "driver has moved on" signal; time
  was a weak proxy. (Ratified this session.)
- **No upper distance cap.** The 50-mile `GC_MAX_DIST_MI` is removed — a real 235-mile offer is
  valid and must stay live for its whole length. (Ratified this session.)
- **One absolute backstop retained:** `GC_ABANDONMENT_CEILING_HOURS = 4`. No offer survives 4
  hours regardless. This is *not* the flaky per-trip time horizon; it is a coarse safety net,
  and it is the sole reaper for deferred offers (§5.5).

### 5.4 Update scope — the keystone invariant
`expected_odometer` is **recomputed at pickup/dropoff transitions, ONLY for offers received
during the current ride.** Never for offers received before the current ride began. Never for the
whole queue. (Andrew: "the importance of this cannot be overstated.") An offer received *before*
the current trip has a clean receipt anchor with no bridge term to revise; an offer received
*during* it is the only kind whose remaining-trip term resolves at the transition.

### 5.5 The lost-mode case — deferred sentinel
When an offer is received while in **lost-mode** (`current_offer_id IS NULL` AND unpicked offers
exist — the §XVIII condition), the bridge term is *unknowable* (the system does not know it is on
a trip). Do not fabricate it. Instead:
- Set `expected_odometer = NULL` and `expected_odometer_status = 'deferred'`.
  (NULL + status flag, **not** a magic `-1` — a sentinel number would silently corrupt band
  arithmetic in any consumer that forgot to guard it; NULL forces the branch and fails loud.)
- A deferred offer is **alive**, **inert on the odometer axis** (no band → cannot be reaped by
  the band, cannot be matched by the band), awaiting resolution.

**Resolution — the dropoff is the disambiguating event.** On any detected dropoff for offer X:
1. The just-ended trip is identified as X. Its window is `[X.pickup_time, X.dropoff_time]`, or
   `[X.receipt_time, X.dropoff_time]` if X's pickup was also missed (lost-mode — **OPEN, see
   §6.3**).
2. **Recompute** every `deferred` offer whose `created_at` falls in X's window — they were
   received during the now-identified trip; the bridge term is finally known.
3. **Reap** every *other* `deferred` offer in the queue — they belong to an earlier, unresolved
   segment and are abandoned-in-fact. The dropoff is the proof of abandonment. (Andrew: the
   dropoff is "a natural disinfectant to stale offers.")
4. Deferred offers that never see a dropoff (driver still mid-open-segment) are swept only by the
   4-hour abandonment ceiling (§5.3).

This closes both failure modes: no immortal candidates (the dropoff sweep + 4-hour backstop clear
them), and no wrong recomputes (the window scoping confines recompute to the identified trip).

### 5.6 Verdict-blindness (restated, load-bearing)
The GC gate **never** consults `app_verdict`. Every offer — ACCEPT or DECLINE — is a PUDO
candidate (§XVI / Rule XVI: accept/decline is advice to the driver, zero relevance to PUDO
logic). The current predicate is already verdict-blind; the reason a DECLINE offer (9131)
survived while an ACCEPT offer (9132) was reaped on 2026-06-04 was purely the distance ceilings —
correct advice-blind behavior. Do not "fix" this by adding a verdict filter.

### 5.7 Encapsulation (Andrew's structural call, ratified by Gemini)
`actual_odometer` and `expected_odometer` must each have a **single source of truth**:
- **Source:** the Android sensor payload at the heartbeat boundary (RIDE_LIFECYCLE.md §3 step 1;
  §7 "GPS is truth"). Hardware-bound.
- **Persistence:** captured once per heartbeat and **written to `pudo_decision_context`** (the
  §VII Postgres-owns-truth ledger point). Today `odometer_gate_result` is NULL on every row — the
  audit trail is being burned every 5 seconds, which is why 9132's eviction cannot be confirmed
  or refuted from forensics.
- **Single accessor:** all consumers (the 5+ call sites that compose `LIVE_OFFER_PREDICATE_SQL`)
  read the odometer from one accessor, eliminating the "five fingers, five independent values"
  split-brain risk.

---

## 6. Threads (resolution status marked per thread)

### 6.1 The receipt-time writer of `expected_dropoff_distance` — RESOLVED
**Not a rogue writer.** `tad.py:compute_offer_expectations` writes the four `expected_*` anchors
to `offer_history` **at receipt** (`tad.py:114` docstring: "persisted to offer_history at
receipt"). That is why never-picked 9132 had `expected_dropoff_distance` populated (stamped at
04:54 receipt, not at a pickup). The two `driver_heartbeat.py` writers (lines 388 `FirePickup`,
725 `FirePickupObservation`) are both correctly scoped `WHERE id = offer AND actual_pickup_at IS
NULL` — they *re-anchor* at pickup-fire. So the column has two legitimate write occasions
(receipt via tad.py, re-anchor at pickup-fire), not a rogue path.
**The real finding (folded into §4 and §5.1):** tad.py already computes the correct bridge-term
anchor (`expected_pickup_distance` / `expected_dropoff_distance`); the GC gate ignores it and uses
the naive `miles_at_offer_receipt` ceiling instead. The fix is to consume the existing anchor.

### 6.2 The noise-floor value — RESOLVED (Gemini, 2026-06-05)
`NOISE_FLOOR_MI` locked at **2.0 miles** for the first build. Rationale: low-speed residential
GPS lane-snap and fractional-mile triangulation anomalies need ~2mi insulation; tight enough to
still evict genuinely stale targets. Re-calibrate against real jitter data post-launch if needed.

### 6.3 Lost-mode window lower bound — RESOLVED (Gemini, 2026-06-05)
**Use `X.receipt_time`** as the recompute-window lower bound when X's own pickup was missed.
`X.receipt_time` is an immutable server-logged timestamp; any deferred offer created after it
entered the pipeline while the wheels were turning on that unobserved segment, making it safe to
re-evaluate. The "dump all deferred, recompute none" alternative was rejected as too lossy for a
95% target.

### 6.4 §0.D.4 framing (the acceptable-loss boundary) — STANDING PRINCIPLE
We can live with: lost observations (bounded cost per §0.D.4) and deferred offers the 4-hour
ceiling eventually clears. We cannot live with: immortal live candidates or wrong recomputes
(unbounded corruption). §5.5's refinements convert both unbounded risks into bounded ones — this
is what makes the design safe behind a 95% PUDO target.

### 6.5 tad.py time-classification vs §5.5 deferred sentinel — NEW, OPEN
Opened during §6.1 recon. `tad.py`'s idle/stacked/orphan classification (lines ~258–272) keys on
a **TIME comparison**: `if prev_expected_dropoff_arrival_time < now: treat prev as orphaned →
compute as idle case`. We ratified removing time as a *reaping* signal (§5.3), but tad.py uses
time here to *classify which trip context an offer arrived in* at receipt — a different use.
However, the "orphaned → idle" fallback (line 261) produces a too-low anchor for an offer received
during an untracked trip — which is the **receipt-time analog of the §5.5 lost-mode case.** So
§5.5's deferred-sentinel logic and tad.py's existing orphan handling **overlap and must be
reconciled, not duplicated** (the exact "two fingers" sin this whole effort exists to kill).
**OPEN QUESTION for next session:** does the §5.5 deferred sentinel *replace* tad.py's orphan→idle
fallback (i.e., orphaned prev → defer, not idle-compute), or layer on top of it? Recommend the
former — a NULL+`deferred` sentinel is more honest than an idle-case anchor we know is wrong — but
it touches tad.py's receipt path and needs its own recon + ratification before implementation.
This is a recon thread, not a blocker for steps 2–4 of §7.

---

## 7. Ordered fix sequence (no step starts until the prior is done)

1. **Ratify this spec** — ✅ DONE 2026-06-05 (Andrew + Gemini + Claude). Doc reconciliation
   still pending: retire the stale `RIDE_LIFECYCLE.md` §3 time-window text; add the INDEX.md entry
   (see `INDEX_entry_odometer_gc_2026-06-05.md`; INDEX.md is currently silent on the entire
   odometer/GC subsystem — handoff §5).
2. **Locate the receipt-time writer** — ✅ DONE 2026-06-05 (§6.1). It is `tad.py` at receipt; not
   rogue; the gate consumes the wrong column. No "third writer" defect — the defect is the gate's
   input choice.
3. **L-6 blast-radius grep:** inventory all sites composing `LIVE_OFFER_PREDICATE_SQL`, all
   readers/writers of the odometer, and all readers of `expected_pickup_distance` /
   `expected_dropoff_distance`, before any signature change. (Gemini is preparing tracking
   metrics.) ← **NEXT ACTION.** This is the first step where Claude Code may assist (read-only
   inventory; CC reports, does not edit).
4. **Encapsulate + persist + log** (§5.7): single odometer accessor; write `actual_odometer` and
   `expected_odometer` (+ status) to `pudo_decision_context` every heartbeat. Smallest change that
   makes the gate **auditable** — justified on its own terms (an unauditable multi-sourced gate
   input is a §V flight-recorder violation). **Includes Gemini's NULL-not-zero guard test:** the
   accessor MUST coerce a missing/invalid hardware odometer payload to `NULL` (which the band
   guards), never to `0` (which fabricates a garbage expected odometer). Write the schema-
   validation test for this **against the encapsulated accessor here** — NOT earlier, where it
   would test the five-fingered path we are deleting. Recon (step 3) must first report whether the
   *current* path injects zero or NULL on a missing odometer; if it injects zero, that is an
   additional finding to record.
5. **Reconcile §6.5** (tad.py orphan→idle vs §5.5 deferred sentinel) — recon + ratification —
   BEFORE implementing §5.5's sentinel, so the two do not become duplicate logic.
6. **Implement the ratified band** (§5): point liveness at tad.py's `expected_pickup_distance` /
   `expected_dropoff_distance` anchors; **delete** the naive pre-pickup ceiling, the `1.25` buffer,
   the `50`-mile cap, and the per-trip time ceiling; add the two-sided ±15% band with the 2.0mi
   noise floor; wire the deferred-sentinel + dropoff-disambiguation recompute/reap.
7. **Re-diagnose 9132 with real logged numbers.** Only after steps 4–6 can we *confirm* (rather
   than infer) the eviction. The spec is justified independent of this confirmation; the
   confirmation closes the forensic loop.

---

## 8. What is proven vs. inferred (honesty ledger)

**Proven from data / source:**
- 9132 (live, ACCEPT, unpicked, 70 m from the driver) was absent from the candidate set WAI
  scored at the 455 arrest.
- All 11 true misses had every spatial signal at 0.00 (never-compared signature).
- The running distance-gate constants (1.25, 2.0, 50.0) match no ratified spec.
- `odometer_gate_result` is NULL across the ledger — the gate input is unlogged.
- `RIDE_LIFECYCLE.md` describes a time gate the code replaced on 2026-05-19; INDEX.md references
  none of the odometer docs.
- **`tad.py` computes the correct bridge-term anchor at receipt** (`expected_pickup_distance` =
  idle: `current_odometer + pickup_miles`; stacked: `prev.expected_dropoff_distance +
  pickup_miles`) and persists it to `offer_history`. **The GC gate does not consume it** — it uses
  the naive `miles_at_offer_receipt` ceiling instead. (Source-confirmed, `tad.py:255–307`,
  `driver_queue.py` predicate body, 2026-06-05.) The bridge term is present in the system and
  absent from the gate's input — that duplication is the defect.

**Inferred, NOT yet proven (requires steps 4, 6–7):**
- That this specific input-choice defect is *the* cause of 9132's specific eviction. The mechanism
  is concrete and consistent with all evidence (9132's `expected_pickup_distance` anchor vs. its
  naive ceiling would straddle the driver's position differently), but the odometer value the gate
  used at 05:23 is unlogged, so the eviction cannot yet be replayed. The encapsulation/logging work
  (step 4) is what converts this inference into proof (step 7).

The spec reconciliation (§5) and encapsulation (§5.7) are justified **regardless** of §8's
inferred item, because an unauditable, multi-sourced, spec-less gate is a defect on its own terms.
