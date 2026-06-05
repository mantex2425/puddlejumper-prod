# HANDOFF — Odometer/GC reconciliation (2026-06-05 session close)

**Read first:** `docs/FINDING_ODOMETER_GC_SPEC_RECONCILIATION_2026-06-05.md` (RATIFIED). This
handoff is the orientation; that doc is the law.

---

## What happened this session

Started as the 2026-06-04 drive review (validate the adjacency haircut). The data redirected it:
the haircut and adjacency fix were NOT implicated. The real finding is a GC/odometer defect.

**The drive:** 38 ground-truth taps → 19 caught, 8 out-of-scope (parked), **11 true misses**.
Every true miss: offer present and live, but ABSENT from the candidate set WAI scored → all
spatial signals 0.00. Canonical case offer **9132**: live, ACCEPT, unpicked, dropoff geocode 70 m
from the driver, never presented to the matcher.

**Root cause (ratified):** the GC distance gate's **pre-pickup branch** reaps offers using a naive
ceiling (`miles_at_offer_receipt + (pickup+trip)*1.25`, clamped [2,50]) that has **no bridge term**
for the remaining distance of an in-progress trip. So an offer received mid-trip gets a too-low
ceiling and is reaped by ordinary driving before the driver reaches it. In all-day lost-mode (this
drive: 133 lost-mode fires vs 4 normal) it compounds.

**The kicker:** `tad.py` ALREADY computes the correct bridge-term anchor at receipt
(`expected_pickup_distance` / `expected_dropoff_distance`) and persists it. The gate ignores it and
reads the dumb column instead. Two fingers on one quantity; the gate consumes the wrong one. The
post-pickup branch (`driver_queue.py:224`) already reads the right column — only the pre-pickup
branch is wrong.

---

## The ratified spec (one-line version)

Liveness = **two-sided ±15% band** around `expected_odometer` (= tad.py's anchor), never narrower
than a **2.0-mi noise floor**, **no upper distance cap** (235-mi offers are valid), **no per-trip
time ceiling** (reaping is emergent from the band), **4-hour abandonment ceiling** retained as the
only backstop. Updated **only for offers received during the current ride**. Lost-mode receipt →
**NULL + `deferred` status**, resolved/reaped at the next dropoff (the disambiguating event).
**Verdict-blind. Encapsulated. Persisted to `pudo_decision_context`.**

---

## Where we are in the §7 fix sequence

- ✅ Step 1 — spec ratified (Andrew + Gemini + Claude).
- ✅ Step 2 — receipt-time writer located (tad.py; not rogue).
- ✅ Step 3 — L-6 blast-radius inventory done (read-only, via Claude Code).
- ⬜ **Step 4 — NEXT: encapsulate + persist + log the odometer.** Single accessor; write
  `actual_odometer` + `expected_odometer` (+ status) to `pudo_decision_context` every heartbeat.
  This is the first CODE step — paired-programming loop applies (Claude proposes → Gemini reviews →
  CC implements → Andrew deploys). Do it in a fresh session with focused context for the L-3
  apply-script discipline.
- ⬜ Step 5 — reconcile the two lost-mode recon threads (§6.5, §6.6) before building the sentinel.
- ⬜ Step 6 — implement the band (delete naive pre-pickup ceiling, 1.25, 50-cap, time ceiling; add
  ±15% band + noise floor + deferred sentinel).
- ⬜ Step 7 — re-diagnose 9132 against the DEPLOYED revision (not branch tip — see §6.7).

---

## The 6 LIVE_OFFER_PREDICATE_SQL compose sites (must move in lockstep on encapsulation)

1. `driver_queue.py:409` — temporal-only re-count (`_log_distance_cull_if_any`)
2. `driver_queue.py:617` — `offer_ids_only`
3. `driver_queue.py:783` — `_project_offers`
4. `driver_heartbeat.py:1063` — `_get_last_known_anchor_id`
5. `driver_heartbeat.py:1111` — `_get_alive_unpicked_offer_ids`
6. `decisions/logger.py:106` — prev-offer TAD-anchor read

Odometer origins: heartbeat `driver_heartbeat.py:1711` (`body.get('cumulative_miles')`),
driver-status `driver_status.py:199`. Both `.get()`→None, never 0 (confirmed clean).

---

## Open recon threads (resolve before the steps that depend on them)

- **§6.5** — tad.py's orphan→idle fallback (time-keyed) overlaps the §5.5 deferred sentinel. Must
  reconcile, not duplicate. Recommend: orphaned prev → defer (NULL+status), not idle-compute.
- **§6.6** — `_detect_lost_mode` has ZERO production callers; production uses
  `_get_alive_unpicked_offer_ids` at `driver_heartbeat.py:1927` directly. The pinned delegation
  test guards a path production bypasses (§XIV.J hazard). Decide: wire it in, or delete + repoint
  the test. Resolve WITH §6.5 (same real path).
- **§6.7** — the inventory was branch-tip (`9eebb45`), not the deployed rev (`00642-fg7`). Step 7
  replay must run against the deployed revision.

---

## Doc-hygiene actions still pending (from step 1)

- Land `FINDING_ODOMETER_GC_SPEC_RECONCILIATION_2026-06-05.md` in `docs/` (scp).
- Paste the `INDEX_entry_odometer_gc_2026-06-05.md` content into `docs/INDEX.md`. INDEX.md is
  currently silent on the entire odometer/GC subsystem — this is the first entry.
- Retire the stale `RIDE_LIFECYCLE.md` §3 time-window text (describes the demolished time gate).
- Commit all together.

---

## The lesson (Andrew's, stated this session)

"We MUST encapsulate the odometer." Every wall hit this session was a value with no single owner —
at the data layer (odometer sourced 5 ways, persisted nowhere), the code layer (two fingers on
`expected_dropoff_distance`), and the spec layer (three drifted definitions, INDEX.md pointing at
none). Encapsulation is the thesis the whole fix sequence serves, not one step in it.
