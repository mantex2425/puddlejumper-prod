# Dispatch-Silence Sprint Handoff (P0 Launch-Blocker)

**Date authored:** 2026-05-11 (mid-morning, post-drive)
**Authoring chat:** Atomic GC Predicate sprint + first verification drive
**Status:** Failure pattern captured forensically; root cause NOT identified;
diagnosis is the entire scope of this sprint.

---

## TL;DR

The Atomic GC Predicate fix shipped to production on rev `puddlejumper-api-00602-99x`
(deployed 2026-05-10 19:04 UTC, commit `9d2d730`). The 5-hour Houston verification
drive (2026-05-11 04:05–10:30 Texas Time / 09:00–15:30 UTC) confirmed the predicate
fix works as designed — bug-signature log filters stay silent, `driver_trip_state`
ends the drive clean — but uncovered an **independent, deeper failure mode**:

**Auto Nail It fired zero times across 10 ACCEPT'd offers in 5 hours of driving.**
The dispatch path produces 4224 `pudo_decision_context` rows (one every ~5s,
healthy heartbeat cadence), but every single row has:

- `current_offer_id_at_eval` = NULL
- `planner_action` = NULL
- `tad_decision_context` (the forensic flight-recorder JSONB) = NULL

This is **architecturally identical** to the pre-sprint forensic state the
original Atomic GC Predicate brief diagnosed:

> Last 30+ hours: 1898 PDC rows, all `current_offer_id = NULL`,
> all `planner_action = NULL`, zero dispatches

The GC predicate fix has shipped and is correct, but the failure it was
intended to resolve is also present in a different form. **The original
brief's root-cause hypothesis was incomplete.** This sprint's job is to
characterize what's actually happening in the dispatch path and fix it.

---

## What we know works (do NOT re-recon these)

- **Heartbeats arrive at healthy cadence.** 4224 PDC rows over 6.5 hours ≈ one
  every 5 seconds. The Android app, the network, and the heartbeat endpoint
  are all functioning.
- **GC predicate fix is correct for its scope.** The bug-signature log filters
  (`prev_offer fetch failed`, `[3b.W]`, `AmbiguousColumn`, `INVARIANT_VIOLATION`,
  `cumulative_miles_value`) return empty over the entire drive window. The
  four bugs hunted in the prior sprint are fixed.
- **`driver_trip_state` returns to NULL cleanly.** Post-drive snapshot shows
  `current_offer_id = NULL`, `pickup_h3 = NULL`, all `nailed_*` coords NULL.
  No stuck pointer.
- **`miles_at_offer_receipt` populates.** Goes 0.00 → 146.56 across the drive,
  confirming odometer signal threads through `log_decision()`.
- **Sprint 1 (Identity Genesis) UUID consumer-side persistence works.**
  `decision_log.trace_data->>'offer_id'` carries a valid UUIDv7 on every row
  (e.g., `019e1774-7399-7f46-86ad-3c6a31065d75` on oh_id 7835).
- **`offer_history` rows write correctly.** 20 rows across the drive, all with
  `created_at`, `app_verdict`, `miles_at_offer_receipt` populated. The
  per-offer logging path is healthy.

---

## What we DON'T know (this sprint's scope)

The four-question forensic axis:

1. **Why is `current_offer_id_at_eval` NULL on every PDC row?** WAI evaluation
   sees no bound offer. Either the offer-accept path doesn't write
   `driver_trip_state.current_offer_id`, or the heartbeat handler doesn't read
   it correctly, or both.

2. **Why is `planner_action` NULL on every PDC row?** The planner either never
   ran, or ran and returned no action, or ran and returned an action that
   wasn't persisted. The brief's architecture says `dispatch()` returns a list
   of actions — that list is empty on every heartbeat across the drive.

3. **Why is `tad_decision_context` JSONB NULL on every PDC row?** This blob is
   the forensic flight recorder — it's supposed to capture WHY a gate verdict
   was returned. NULL means either no gate was evaluated, or evaluation
   completed but the persistence layer dropped the data.

4. **What changed between rev `00598-hvx` (pre-sprint state of memory record)
   and rev `00602-99x` (current)?** This sprint's commits are part of the
   delta. Sprint 1 (Identity Genesis) is also part of the delta. The
   pre-existing `00598` already had the dispatch-silence symptom per the
   original brief, so the failure predates this sprint — but it may also
   have been compounded.

---

## Hypothesis space (ranked rough priors, NOT verified)

These are conjectures to guide first-pass recon, not conclusions:

### H1 — The offer-accept path never writes `current_offer_id` (HIGH)
The Android app accepts an offer, `offer_history` gets a row with
`app_verdict='ACCEPT'`, but the corresponding `UPDATE driver_trip_state SET
current_offer_id = ...` either never fires or writes NULL. Check:
- `pickup_confirm.py` / heartbeat handler write paths
- Per CANONICAL_RULES Section VI: `current_offer_id` "Set on `fire_pickup`"
  — what fires fire_pickup if Auto Nail It never completes?

### H2 — WAI evaluates but returns an empty match list (MEDIUM-HIGH)
The dispatch path's purity contract is: `dispatch(matches, current_offer_id,
queue_offer_ids) -> list[Action]`. If `matches = []` always, `dispatch()`
returns `[]` always, and `planner_action` stays NULL. Check:
- `where_am_i.evaluate()` invocation site
- Whether `WAI_CONFIDENCE_THRESHOLD` is filtering everything

### H3 — Feature flag gates the entire dispatch (MEDIUM)
`WAI_PLANNER_ENABLED_DRIVERS` is the brief's stated sole gate post-Apr 25.
It was not visible in `gcloud run services describe` for rev `00602-99x`,
which could mean: removed, renamed, runtime-config-only, or env-var-with-
empty-value. If driver is gated out, dispatch short-circuits before any
evaluation. Check:
- `gcloud run revisions describe puddlejumper-api-00602-99x --region=us-central1`
- Codebase grep for `WAI_PLANNER_ENABLED_DRIVERS`
- Whether the gate check is `if driver_id in ENABLED_DRIVERS:` (where empty
  list = nobody enabled) or `if driver_id not in DISABLED_DRIVERS:` (where
  empty list = everyone enabled)

### H4 — The forensic write path itself is broken (LOW-MEDIUM)
Maybe dispatch *is* working but the write of `tad_decision_context` /
`planner_action` /  `current_offer_id_at_eval` into the `pudo_decision_context`
row is broken — perhaps a NULL-binding regression introduced in a prior
sprint that nobody caught because evaluation was failing for OTHER reasons.
Check:
- `_log_decision_context()` in `driver_heartbeat.py`
- The columns are explicitly mentioned in CANONICAL_RULES V (Forensic Rules):
  "If a column needed for debugging is NULL, restore the binding in
  `_log_decision_context` before attempting to tune logic."

### H5 — A new bug from the GC sprint (LOW)
Unlikely but cannot be ruled out. Our 3-commit chain modified `driver_queue.py`,
`driver_heartbeat.py`, `decisions/logger.py`. If any of those introduced a
regression that prevents `pudo_decision_context` from being populated,
it would manifest as exactly this symptom. **Pre-sprint had the same symptom
shape** (per the original brief's forensic), so this hypothesis is weak —
but the new sprint's recon should verify by comparing PDC fill rate on a
revision before our changes vs. after.

---

## Recon path (first-session moves)

When opening this sprint in a fresh chat, the recon sequence should be:

1. **Verify the failure is current.** Re-run the Step 28 + 29 queries from the
   prior chat to confirm 0 bound evaluations / 0 dispatched / 0 forensic-blobs
   in the most recent window (NOT just the drive window).

2. **Test H3 first** — feature flag is the cheapest to verify and would explain
   everything if it's the cause. Three commands:
   - `gcloud run revisions describe puddlejumper-api-00602-99x --region=us-central1 | grep -i "wai\|planner\|enabled"`
   - `grep -rn "WAI_PLANNER_ENABLED_DRIVERS\|wai_planner_enabled\|WAI_ENABLED" --include="*.py" .`
   - If found in code: identify the gate logic and check the env var value
     vs. the driver_id

3. **Test H1 second** — recon the `current_offer_id` write path:
   - `grep -rn "SET current_offer_id\|UPDATE.*driver_trip_state\|current_offer_id\s*=" --include="*.py" .`
   - Find which function persists `current_offer_id` to driver_trip_state on
     ACCEPT, trace back from there

4. **Then test H4** — the forensic write path:
   - `grep -rn "_log_decision_context\|pudo_decision_context" --include="*.py" .`
   - Verify the function exists, is called per heartbeat, and writes the
     expected columns; check if the binding fields are explicitly NULL-passed
     in the current code

5. **H2 last** — WAI evaluation results are downstream of H1 and H3; only
   diagnose this if H1/H3/H4 don't explain the silence

---

## Forensic data to load fresh

When opening the next chat:

- **This brief:** `docs/SPRINT_HANDOFF_DISPATCH_SILENCE_2026-05-11.md`
- **Standard load:** `docs/CANONICAL_RULES.md`, `docs/SESSION_PROTOCOL.md`,
  `docs/INDEX.md`
- **Predecessor brief (for context on what changed):**
  `docs/SPRINT_HANDOFF_ATOMIC_GC_PREDICATE_2026-05-10.md`

Live recon queries (copy-paste ready):

```sql
-- Drive window PDC summary (from this chat's Step 29)
SELECT
  COUNT(*) AS total_pdc_rows,
  COUNT(*) FILTER (WHERE current_offer_id_at_eval IS NOT NULL) AS bound_evaluations,
  COUNT(*) FILTER (WHERE planner_action IS NOT NULL) AS dispatched,
  COUNT(*) FILTER (WHERE tad_decision_context IS NOT NULL) AS forensic_blobs_written
FROM app_private.pudo_decision_context
WHERE driver_id = 'UjT1hE9eBXh2q95aSZYOkzDJ8lo1'
  AND created_at >= NOW() - INTERVAL '24 hours';

-- Verdict-to-confirmation funnel
SELECT
  COUNT(*) FILTER (WHERE app_verdict = 'ACCEPT')                          AS accepts,
  COUNT(*) FILTER (WHERE actual_pickup_at IS NOT NULL)                    AS pickups_confirmed,
  COUNT(*) FILTER (WHERE actual_dropoff_at IS NOT NULL)                   AS dropoffs_confirmed,
  COUNT(*) FILTER (WHERE app_verdict = 'ACCEPT' AND actual_pickup_at IS NULL)
    AS accepts_without_pickup
FROM app_private.offer_history oh
JOIN app_private.decision_log dl ON dl.id = oh.decision_log_id
WHERE dl.driver_id = 'UjT1hE9eBXh2q95aSZYOkzDJ8lo1'
  AND oh.created_at >= NOW() - INTERVAL '24 hours';
```

---

## What this sprint is NOT

- **Not re-opening the GC predicate sprint.** That work shipped, is correct,
  and tests prove it. Don't unwind it.
- **Not adding more live-DB tests for the predicate.** Sprint 3 hygiene.
- **Not Sprint 2 (Identity Genesis UUID migration).** Independent. Don't
  conflate.
- **Not a refactor sprint.** Goal is "Auto Nail It fires on a real offer."
  Architectural cleanups deferred.

---

## Verification gate (what closes this sprint)

A single real Uber ride, accepted in production, where:

1. `current_offer_id` populates in `driver_trip_state` after offer accept
2. `pudo_decision_context` shows non-NULL `current_offer_id_at_eval`,
   non-NULL `planner_action`, populated `tad_decision_context` JSONB
3. `offer_history.actual_pickup_at` populates when driver arrives at pickup
4. `offer_history.actual_dropoff_at` populates when driver arrives at dropoff
5. `driver_trip_state.current_offer_id` returns to NULL after dropoff

Same gate as the GC predicate sprint's. The gate didn't close last time —
let's close it this time.

---

## Note on pace

Andrew drove 5 hours from 4am Texas time. The forensic data is captured;
the diagnosis can wait until Andrew's ready to engage again. The branch is
pushed, production is on `00602-99x`, the GC fix is live. Nothing is
actively burning.

---

## Kickoff prompt for next chat

> Loaded: SPRINT_HANDOFF_DISPATCH_SILENCE_2026-05-11.md.
> Standard load: CANONICAL_RULES, SESSION_PROTOCOL, INDEX.
> Predecessor: SPRINT_HANDOFF_ATOMIC_GC_PREDICATE_2026-05-10.md (closed).
>
> Mission: diagnose why Auto Nail It fired zero times across a 5-hour drive
> with 10 ACCEPTs. Forensic shape is captured in the brief: 4224 PDC rows
> all with NULL `current_offer_id_at_eval` / NULL `planner_action` /
> NULL `tad_decision_context`. GC predicate fix is shipped (rev 00602-99x)
> and confirmed working — this is a separate failure.
>
> Per session protocol: recon first. Start with H3 (feature flag) — cheapest
> to verify. Then H1 (current_offer_id write path) and H4 (forensic write
> path). Propose the recon sequence for ratification before running.
