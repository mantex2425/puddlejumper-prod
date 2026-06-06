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

**SCOPE REFINEMENT (L-6 inventory, 2026-06-05):** the predicate's **post-pickup** branch
(`driver_queue.py:224–225`) ALREADY consumes the anchor (`oh.expected_dropoff_distance +
oh.trip_miles*0.25`). Only the **pre-pickup** branch still runs the naive ceiling. So the column-
source change is narrower than "point the band at the anchor wholesale": switch the **pre-pickup
branch** to read `expected_pickup_distance`; do NOT touch the post-pickup branch's column choice —
it is already correct. (The ±15% / noise-floor / no-cap band reshaping of §5.2–5.3 still applies
to both branches; only the post-pickup branch's *column* is already right.)

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

### 6.6 `_detect_lost_mode` has NO production caller — NEW, OPEN (L-6 inventory, 2026-06-05)
The L-6 inventory found that `_detect_lost_mode` (def `driver_heartbeat.py:1119`) is called **only
by tests** (`test_driver_heartbeat_3b_r.py`, `test_lost_mode_houston_playback_live.py`,
`test_live_offer_predicate_imports.py`). **Production derives lost-mode directly from
`_get_alive_unpicked_offer_ids` at `driver_heartbeat.py:1927`** — it does not call
`_detect_lost_mode` at all. The §XVIII single-source contract test
(`test_detect_lost_mode_delegates_to_helper`) therefore pins a **delegation path that production
bypasses**: the test guards that `_detect_lost_mode` delegates to the helper, but production never
runs `_detect_lost_mode`. This is a §XIV.J-class hazard (a test exercising an imaginary path) and
means our model of "how production detects lost-mode" was one indirection off — the real path is
the direct `:1927` call. **Implication for §6.5:** the tad.py-vs-sentinel reconciliation must be
written against the REAL production lost-mode path (`_get_alive_unpicked_offer_ids` at :1927), not
against `_detect_lost_mode`. **OPEN:** decide whether `_detect_lost_mode` should be (a) wired into
production as the single entry point (making the pinned test meaningful), or (b) deleted as dead
code with the contract test repointed at the :1927 path. Not a blocker for step 4 (encapsulation),
but must be resolved before §6.5 / §5.5 implementation.

### 6.7 Inventory was branch-tip, not deployed revision — STANDING CAVEAT
The L-6 inventory ran against the working tree (`fix/restore-fire-error-metric-2026-06-02`, HEAD
`9eebb45`), NOT the deployed Cloud Run revision (`00642-fg7`) that produced the 2026-06-04 data.
For forward encapsulation work this is correct (we fix the tree forward). But §7 step 7 (re-
diagnose 9132) MUST replay against the **deployed** revision, not the branch tip, or it will
measure different code than the one that evicted 9132.

---

## 7. Ordered fix sequence (no step starts until the prior is done)

1. **Ratify this spec** — ✅ DONE 2026-06-05 (Andrew + Gemini + Claude). Doc reconciliation
   still pending: retire the stale `RIDE_LIFECYCLE.md` §3 time-window text; add the INDEX.md entry
   (see `INDEX_entry_odometer_gc_2026-06-05.md`; INDEX.md is currently silent on the entire
   odometer/GC subsystem — handoff §5).
2. **Locate the receipt-time writer** — ✅ DONE 2026-06-05 (§6.1). It is `tad.py` at receipt; not
   rogue; the gate consumes the wrong column. No "third writer" defect — the defect is the gate's
   input choice.
3. **L-6 blast-radius grep** — ✅ DONE 2026-06-05 (read-only, via Claude Code). `LIVE_OFFER_PREDICATE_SQL`
   has **6 compose sites**: `driver_queue.py:409` (temporal-only re-count), `:617`
   (`offer_ids_only`), `:783` (`_project_offers`); `driver_heartbeat.py:1063`
   (`_get_last_known_anchor_id`), `:1111` (`_get_alive_unpicked_offer_ids`); `decisions/logger.py:106`
   (prev-offer TAD-anchor read). All 6 must be touched in lockstep on encapsulation. Two odometer
   ORIGINS confirmed (heartbeat path `driver_heartbeat.py:1711`; driver-status path
   `driver_status.py:199`) — both `.get()` → None, never 0. Findings §6.6 and §6.7 opened from this
   inventory.
4. **Encapsulate + persist + log** (§5.7): single odometer accessor; write `actual_odometer` and
   `expected_odometer` (+ status) to `pudo_decision_context` every heartbeat. Smallest change that
   makes the gate **auditable** — justified on its own terms (an unauditable multi-sourced gate
   input is a §V flight-recorder violation). **NULL-not-zero guard test:** the inventory CONFIRMED
   every current odometer path already fails closed to NULL (never injects 0) — so this test locks
   in existing-correct behavior, it does not fix a defect. Write it against the encapsulated
   accessor. (One adjacent nuance to capture: the pickup-fire UPDATEs use `COALESCE(trip_miles, 0)`,
   so a NULL-`trip_miles` offer collapses `expected_dropoff_distance` to a zero-length horizon —
   a `trip_miles` default, not an odometer-0, but a latent edge worth a guard.)
5. **Reconcile the lost-mode recon threads** — recon + ratification — BEFORE implementing §5.5's
   sentinel: (a) §6.5 tad.py orphan→idle vs the deferred sentinel; (b) §6.6 `_detect_lost_mode`
   dead-vs-wire decision. Both touch the REAL production lost-mode path
   (`_get_alive_unpicked_offer_ids` at `driver_heartbeat.py:1927`), so they reconcile together or
   the sentinel gets built against the wrong model.
6. **Implement the ratified band** (§5): point liveness at tad.py's `expected_pickup_distance` /
   `expected_dropoff_distance` anchors; **delete** the naive pre-pickup ceiling, the `1.25` buffer,
   the `50`-mile cap, and the per-trip time ceiling; add the two-sided ±15% band with the 2.0mi
   noise floor; wire the deferred-sentinel + dropoff-disambiguation recompute/reap.
7. **Replay ALL 11 true-misses with real logged numbers, cohorted by mechanism.** (Widened
   2026-06-05 from "re-diagnose 9132 only" — 9132 is one clean liveness-predicate case, but the
   2026-06-04 drive was almost entirely lost-mode [133 lost-mode fires vs 4 normal], so confirming
   9132 alone verifies the band fix on one normal-mode case and says NOTHING about the lost-mode
   majority.) Only after steps 4–6 (Step 4's logging supplies the odometer value the gate used,
   which is NULL on every row today — without it there is no real number to replay against). The
   replay is a parametrized test, one case per true-miss, **split into two cohorts so each miss is
   asserted against the fix that is supposed to catch it**:
   - **Normal-mode cohort** (offer reaped by the liveness predicate's wrong-column ceiling, e.g.
     9132): assert the Step-6 band keeps the offer LIVE at the arrest (e.g. "9132 in the candidate
     set at tap 455" — the thing that was false on 2026-06-04).
   - **Lost-mode cohort** (offer with no computable anchor, the drive's majority): assert the §5.5
     deferred-sentinel logic keeps the offer alive-and-deferred rather than 0.00-signal-excluded.
   A miss the band fix does not catch is NOT a failure — it must be visibly attributed to the
   lost-mode/sentinel cohort, not logged as "still broken." The replay is designed to assign each
   miss to its mechanism. The spec is justified independent of this confirmation; the cohorted
   replay closes the forensic loop and is the test that answers "did today's fixes catch yesterday's
   misses." **Fixture prep (do now, while 2026-06-04 data + forensic context are fresh):** freeze
   the 11 true-miss offers — their `expected_pickup_distance`/`expected_dropoff_distance` anchors,
   arrest coords/timestamps, and `contest_labels` taps — into a test fixture, so the replay has its
   ground-truth cases captured rather than needing re-excavation when Step 6 lands.

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
- That the input-choice defect (wrong column) is *the* cause of the **normal-mode** true-misses'
  evictions (9132 the canonical case). The mechanism is concrete and consistent with all evidence
  (9132's `expected_pickup_distance` anchor vs. its naive ceiling would straddle the driver's
  position differently), but the odometer value the gate used at 05:23 is unlogged, so the eviction
  cannot yet be replayed.
- That the **lost-mode** true-misses (the 2026-06-04 majority) are caused by the no-computable-
  anchor condition the §5.5 deferred sentinel addresses — separate mechanism, separate fix,
  separate cohort in the step-7 replay.
- Step 4's logging converts both inferences into proof; the **cohorted step-7 replay** (normal-mode
  → band fix; lost-mode → sentinel) is what confirms each cohort against its own fix. Confirming
  9132 alone would verify only the normal-mode cohort.

The spec reconciliation (§5) and encapsulation (§5.7) are justified **regardless** of §8's
inferred item, because an unauditable, multi-sourced, spec-less gate is a defect on its own terms.

---

## 9. ADDENDUM — §5.5 sentinel implementation design (2026-06-05 PM)

**Status:** design refinement of §5.5 / §6.5 / §6.6, captured before implementation.
Append to the FINDING above. References existing section numbers; renumbers nothing.
Origin: the 2026-06-05 PM session that merged DSI (rev 00645→00647), recovered the
band fix from a parallel-deploy regression (00646 dropped it; 00647 restored it), and
specified the §5.5 sentinel for implementation.

---

### 9.1 The bridge term has TWO senses — one struck, one real

The erratum (`2064ea6`) struck "the bridge term." This caused real confusion in the
PM session (two reviewers reached opposite conclusions). Resolution: there are two
distinct quantities both called "bridge term," and the erratum struck only one.

- **Normal-mode bridge (STRUCK — phantom).** The notion that the receipt-time anchor
  needed an explicit additive "remaining current trip" term. It was phantom: the
  deployed gate never used one, and the real 9132 mechanism was the liveness
  predicate's wrong-column bug (consumed `miles_at_offer_receipt` instead of
  `expected_pickup_distance`). `compute_offer_expectations` already handles the
  mid-trip case correctly by CHAINING — the stacked-case anchor is
  `prev.expected_dropoff_distance + pickup_miles` (tad.py ~150/168/174). The bridge
  is folded into the chain; a separate additive term would double-count. Struck,
  correctly, and stays struck.

- **Lost-mode bridge (REAL — never struck).** When the driver is in lost-mode
  (`current_offer_id IS NULL` AND unpicked offers — §XVIII), the system cannot see
  the trip it is on, so the chaining anchor (`prev.expected_dropoff_distance`) does
  not exist. The offset from "where the unobserved current trip will end" to "this
  new offer's pickup" is genuinely UNKNOWABLE at receipt. This is the lost-mode
  bridge. It is NOT computed and NOT fabricated — it is DEFERRED (NULL sentinel)
  until a dropoff supplies it.

**The discipline:** never fabricate the lost-mode bridge. NULL, never a magic number
(`-1` in a miles column silently corrupts: `actual - (-1) = actual + 1` reads as a
valid comparison; NULL forces every consumer to branch or fail loud). No number means
no band; no band means the offer cannot be reaped or matched on the odometer axis —
it is parked, alive, inert, until ground truth arrives.

### 9.2 The dropoff supplies the lost-mode bridge — WINDOW-SCOPED recompute

When a dropoff fires for offer X, the just-ended trip is identified. Its window is
`[X.pickup_time, X.dropoff_time]`. That window is the key. It is the lost-mode bridge,
finally known.

The recompute is **window-scoped — this is the load-bearing guardrail, not a detail:**

- For each deferred offer D where `D.created_at ∈ [X.pickup_time, X.dropoff_time]`:
  D was received DURING the now-identified trip, so X is D's true prior anchor.
  Re-invoke the EXISTING `compute_offer_expectations(D, prev_offer=X,
  current_odometer=<X's dropoff-fire odometer>, ...)`. Its stacked-case chaining
  produces the bridge-correct anchor — the same number it would have produced at
  receipt had the segment been known then. Write result to `expected_odometer`,
  flip `expected_odometer_status` to `'active'`. NO new formula, NO additive bridge
  term — the existing chaining IS the bridge.

- For each deferred offer D OUTSIDE X's window: D belongs to an earlier, unresolved
  segment and is abandoned-in-fact. Flag for the upstream Horizon Budget GC (§6.5).
  Do NOT recompute against X — recomputing an offer against a trip it did not belong
  to is the "confident-and-wrong" failure the sentinel exists to prevent (a new
  9132-class bug by a different door).

- Backstop: deferred offers that never see a dropoff are swept by the 4-hour
  abandonment ceiling (`GC_ABANDONMENT_CEILING_HOURS`, never hardcoded).

**Why the window matters:** without it, a deferred offer would be recomputed against
whatever dropoff fired next — possibly the wrong trip — producing a confident garbage
anchor. The window confines recompute to the trip the offer actually belonged to.
This is the single most important correctness property of the Class-B path.

### 9.3 §6.5 / §6.6 resolutions (from PM recon)

- **§6.6 (RESOLVED):** `_detect_lost_mode` is NOT dead code. It delegates to
  `_get_alive_unpicked_offer_ids` (driver_heartbeat.py), which has a live production
  caller in the heartbeat path, and the delegation is pinned by
  `test_live_offer_predicate_imports.py`. The original "no production caller" note
  was stale (pre-2026-05-31-refactor). Build the sentinel's lost-mode detection
  against `_get_alive_unpicked_offer_ids` — the single canonical bit-2 predicate.

- **§6.5 (RESOLVED):** the sentinel's reap defers to the upstream Horizon Budget GC
  (decisions/logger.py), NOT a second reaper in the dropoff handler. The dropoff
  handler declares the window closed (flags out-of-window deferred offers); the
  upstream GC sweeps them on its own authority + 4h ceiling. tad.py's orphan→idle
  was already subordinated upstream by Step 5 Option C (`2418401`). No competing
  garbage collectors in one thread space.

### 9.4 Deferred taxonomy (two classes, two resolution points)

- **Class A — dropoff-leg deferred:** pickup fired late or in lost-mode;
  `cumulative_miles_at_pickup_fire` was NULL at receipt, so the dropoff leg could not
  be banded. Resolves at the PICKUP-fire handler (FirePickup/FirePickupObservation),
  which already sets `cumulative_miles_at_pickup_fire` and writes
  `expected_dropoff_distance`. Extend that EXISTING UPDATE to also set
  `expected_odometer` and flip status `'active'`. No new writer (encapsulation
  discipline). The anchor here is the REAL post-pickup odometer — no bridge needed,
  because the pickup actually fired.

- **Class B — receipt-deferred (pure lost-mode arrival):** no prior segment context
  at receipt. Resolves at the DROPOFF-fire handler via §9.2's window-scoped recompute.

### 9.5 Build state (verified PM 2026-06-05)

- Band primitive (`odometer_band`, `odometer_in_band`) and the None-routing
  ("no band → never reap on absence") are LIVE in rev 00647-ckx.
- The columns `expected_odometer` / `expected_odometer_status` are NOT in the DB.
- The status constants are NOT in pudo_types.py.
- Nothing persists the deferred state or runs the dropoff-disambiguation sweep.

Therefore the PROTECTIVE behavior (don't reap a lost-mode offer) is already live; the
PERSISTENCE (auditable deferred state) and the LIFECYCLE (recompute-or-reap at
dropoff) are the increment the implementation package builds. Implementation package
pieces: (1) migration; (2) status constants; (3) persist-on-None at log_decision's
offer_history INSERT — re-touches the INSERT, requires positional re-verify +
`EXPECTED_PICKUP_DIST_IDX` shift; (4) Class A pickup-handler extension + Class B
window-scoped dropoff recompute/flag; (5) tests incl. a §XIV.J live-PG test for the
cross-heartbeat liveness mutation.

### 9.6 Verdict-blindness invariant (restated)

The deferred/recompute/reap logic MUST NOT consult `app_verdict` anywhere. Per §0.B
and §XV, the car's physical position is the sole sensor of driver intent; deferral and
recompute key on receipt-time, dropoff-window membership, and the live-offer predicate
only. This is the rule most likely to be "helpfully" violated during implementation —
stated here so it is not.

### 9.7 What this design does NOT prove

It does not prove 9132 is caught. The persist/recompute tests cover the sentinel's own
behavior; none replays 9132 through the deployed predicate. The premise is sound
(9132's bridge-correct dropoff anchor was 867.86; arrest odometer was ~868.08 — the
right column lands within ~0.2 mi of the actual stop), but whether the BAND keeps 9132
live at that arrest is the predicate-level replay still owed (FINDING step 7,
cohorted). That replay is separate from this package and gated on replay-against-the-
deployed-revision (FINDING §7 step 7 / the "MUST replay against deployed, not branch
tip" constraint).

### 9.8 Class A / Class B resolution-location ratification (Gemini 00648, 2026-06-06)

§9.4 named the deferred taxonomy (Class A resolves at pickup, Class B at dropoff).
This section records the *location* decision — WHERE each resolution executes and
WHY — and the Gemini ratification that cleared rev 00648 to deploy. Finalized late
in the 2026-06-05 PM session; recorded here so it lives in the spec, not only in
chat + the Gemini exchange.

**Class A — dropoff-leg deferred → resolves at the pickup-fire UPDATEs.**
The two existing pickup-fire UPDATEs (driver_heartbeat.py FirePickup ~:388,
FirePickupObservation ~:725) already compute `expected_dropoff_distance = <post-
pickup odometer> + COALESCE(trip_miles,0)`. Class A extends *those same UPDATEs*
to also set `expected_odometer = <same expression>` and flip
`expected_odometer_status='active'`. No new writer, no new authority — the anchor
is the real post-pickup odometer, written on a path that already fires. Clean
first increment.

**Class B — receipt-deferred → recompute AT the dropoff handler; reap STAYS at
the GC (the B2 split).** When a dropoff fires for offer X (driver_heartbeat.py
FireDropoff ~:652, FireDropoffObservation ~:1001), the deployed handler calls
`_resolve_deferred_at_dropoff(cur, driver_id, X_id, dropoff_fire_odometer)`. That
helper recomputes (window-scoped per §9.2) every deferred offer received inside
X's window, flipping it active with a chained anchor. Out-of-window deferred
offers are LEFT untouched — the upstream Horizon Budget GC reaps them on its
existing authority + 4h ceiling. The recompute lives at the handler; the reap
lives at the GC. Two responsibilities, two owners.

**Why the split (B2), not GC-owns-everything (B1) — the structural argument.**
B1 (let the GC own both recompute and reap for Class B) was REJECTED. The GC's
prev_offer SELECT (decisions/logger.py ~:106) requires `actual_pickup_at IS NOT
NULL` and is shaped LIMIT-1-one-picked-up-prev. A pure receipt-deferred offer has
NO pickup, so the GC's SELECT *structurally cannot fetch it*. To make the GC own
Class B recompute, its SELECT contract would have to widen to also pull
unpicked-deferred offers — distorting the single authority into something it is
not, on the same grounds Step 5 Option B was rejected. The recompute therefore
must live where the disambiguating event (the dropoff) and its window bounds and
fire-odometer are in scope: the dropoff handler. The reap stays with the GC
because the GC is the single killing authority (no second reaper, §6.5). The
split is not a compromise — it is the only placement consistent with both the
GC's structural shape and the single-owner-for-evictions discipline.

**Recompute mechanism (the chaining IS the bridge, §9.1).** The helper re-invokes
the EXISTING `compute_offer_expectations` with X's dropoff anchors supplied via the
`prev_expected_dropoff_*` kwargs. No new formula, no additive bridge term — the
stacked-chaining branch produces the bridge-correct anchor. The deferred offer's
`expected_odometer` is set to the recomputed `expected_pickup_distance`, and a
FULL backfill writes all four `expected_*` anchors so a recomputed offer is
indistinguishable from one that was active at receipt (no half-resolved state).

**Gemini 00648 ratification (recorded).** Two judgment calls were put to Gemini
explicitly (not rubber-stamped):
- **Call A — the `prev_offer=d_offer` self-proxy** used to force entry into the
  chaining branch (the branch reads only the kwargs, never `prev_offer.<attr>`).
  ACCEPTED. Protection chosen: a GUARD-TEST
  (`tests/test_chaining_ignores_prev_offer_attrs.py`) that pins the invariant
  executably — a decoy prev_offer with wrong attributes must still yield the
  kwarg-derived anchor — stronger than a comment, and the Option-C guard in tad.py
  was NOT re-touched (it is today's shipped, ratified, floor-green code).
- **Call B — full backfill** (write all four anchors, not just expected_odometer).
  ACCEPTED. Consumer audit cleared it: the time-signal path is asymmetric-soft and
  never penalizes a past `expected_pickup_arrival_time`; no consumer gates liveness
  on future-ness; the hard liveness decisions are odometer-only (§5.3 retired the
  time ceiling). A historical pickup-ETA on an active offer yields at worst a zero
  time-boost — never a break, never a reap.

Floor at ratification: 770 passed / 1 skipped / 0 failed (index guard at 38).
Deployed: rev `puddlejumper-api-00648-lwx`, serving, from branch
`fix/restore-fire-error-metric-2026-06-02` @ `f88b8c7`.

**Production-witness status (honest, as of 2026-06-06 AM).** The deferred path is
unit-proven (live-PG 3/3) and deployed, but has NOT fired in production
(`deferred_count=0` since deploy; no `[deferred-sentinel]` log lines). Witnessing
it requires driving a genuine lost-mode receipt through the live API — pursued via
a Bruno scenario through the real endpoints (interpretation B: reproduce the real
receipt path, no test-only shortcut, no faked status). Until that witness is
green, the correct confidence statement is: "unit-tested, deployed, not yet
production-witnessed." This is the §5.5 analogue of §9.7's scope honesty.
