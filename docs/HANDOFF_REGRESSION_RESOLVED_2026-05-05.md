# Resolution — Matcher Dispatch Regression (False Alarm)

**Date:** 2026-05-05
**Status:** Closed. No code change. Operational artifact, not a code regression.
**Predecessor:** `docs/HANDOFF_REGRESSION_2026-05-05.md`
**Deploy unchanged:** `puddlejumper-api-00591-vj7` remains in production.
**Bruno:** 17/17 against `00591-vj7` confirms matcher dispatch is healthy.

---

## TL;DR

The "matcher hasn't fired today" symptom was not a Phase 1A or Phase 1B
regression. It was an operational state inconsistency: the regression-hunt
priming set `driver_trip_state.current_offer_id = '7712'` without
inserting a corresponding fresh `offer_history` row. Offer 7712 had been
created 14+ hours earlier with a 45-minute GC window. By drive time,
`_project_queue` was correctly returning an empty queue. The matcher
correctly returned no matches. The dispatcher correctly fired
`LogNoMatch` on every cluster. Every observed fact follows from this
single cause.

---

## Root cause

`_project_queue` (driver_heartbeat.py:141) builds the workload queue from
`app_private.offer_history` filtered by `actual_dropoff_at IS NULL` AND a
per-offer GC window. It does NOT consult `driver_trip_state.current_offer_id`
to populate the queue. The 1-bit memory and the queue are separate
sources of truth. Setting `current_offer_id` without a live offer_history
row produces a state the production system would never naturally
produce: memory pointing at an offer that is no longer queue-eligible.

## Why every observed fact follows

- `0/9701 wai_confidence`: empty queue → zero candidates in `_evaluate`'s
  Map step → `per_target_outcomes=[]` → `matches=[]` → `top_match=None`.
- `7689/9920 has_action`: dispatch with empty matches returns
  `LogNoMatch`, which `_execute_action` fires as a side-effect log. This
  populates `action_str` while `wai_confidence` stays NULL — the
  asymmetry that originally looked anomalous is actually correct
  behavior.
- `wai_current_road_class` populating in 21 rows: pivot_context runs
  independently of queue state. Topology probes succeed when GPS lands
  near a named road regardless of whether any offers exist.
- `cluster_size` populating: cluster detection is upstream of queue
  evaluation and is unaffected by queue emptiness.

## Decisive evidence

Bruno PUDO Simulation, run end-to-end against the live `00591-vj7`
deploy after the diagnosis: 17/17 passing, including step 08's
`FirePickup with dispatch_executed=true in last_3_dispatch_actions`.
The exact code path that was producing zero matches today fires
correctly when given a fresh in-window offer.

## Hypotheses tested and eliminated

1. Phase 1B parameter rename broke orchestrator — eliminated by diff
   inspection (cleanly updated at definition + call site).
2. `_project_queue` returns empty due to code change — eliminated;
   function unchanged in bisect window.
3. Phase 1A transit gate fired pervasively — eliminated empirically
   (only 19/9920 rows had transit class).
4. pivot_context LATERAL rewrite made topology darker — eliminated
   empirically (yesterday `has_road=0/5424` with old query, today
   `has_road=21/9920` with new query — slightly less dark, not more).
5. `_evaluate` itself changed — eliminated by diff inspection.
6. `adjacent_roads` carrier signal went dark — never confirmed because
   the simpler explanation (empty queue) accounted for all evidence.

## What the test drive earned

The drive was not wasted despite the diagnosis taking longer than
optimal. It established:

1. Cluster detection healthy: 40 cluster rows in 10 minutes including
   an 11-frame stationary cluster at the dentist.
2. Heartbeat write path healthy: 104 heartbeats with GPS, accuracy,
   speed all flowing into `heartbeat_log`.
3. Topology probe healthy: `wai_current_road='McKeever Road'` and
   `wai_current_road_class='transit'` populated correctly on transit
   sections.
4. Phase 1B's forensic restoration earned its keep: every diagnostic
   step relied on a column 1B added or restored. Without 1B this
   would have been opaque.
5. Phase 1A's transit gate fired correctly when it fired (19 rows on
   tertiary roads, suppressed adjacency rescue as designed).

## What we'd do differently

**Before bisecting code, validate that the system's inputs are
well-formed.** A 5-second `_project_queue`-equivalent query at the
start of the diagnosis:

```sql
SELECT COUNT(*) FROM app_private.offer_history oh
JOIN app_private.decision_log dl ON dl.id = oh.decision_log_id
WHERE dl.driver_id = '<driver>'
  AND actual_dropoff_at IS NULL
  AND created_at + window_calc > NOW();
```

would have returned 0 and pointed at the priming bug immediately.
Bisecting answers "what changed in the code?" — but a more powerful
first question is "are the inputs to the code well-formed?"

**Run Bruno before assuming code regression.** 60 seconds of
`bru run "PUDO Simulation"` against the live deploy will distinguish
"matcher is broken" from "matcher has nothing to match" decisively.
Bruno seeds a fresh in-window offer via `/api/v1/test/seed_offer`,
drives 5 heartbeats at pickup coords, and asserts FirePickup. If it
passes, matcher dispatch is fine — the issue is upstream of the matcher.

## Lessons logged

### L-19: Priming `current_offer_id` alone produces inconsistent state

`UPDATE driver_trip_state SET current_offer_id=...` without inserting a
corresponding fresh `offer_history` row creates a state where the 1-bit
memory points at an offer the queue can no longer see. The matcher
behaves correctly (empty queue → no matches) but the system as a whole
is in a state it would never naturally enter.

**Mitigation rule:** prime regression replays via the
`/api/v1/test/seed_offer` endpoint, which writes both `decision_log` and
`offer_history` with `created_at = NOW()`. Bruno's
`PUDO Simulation/02 Seed Offer` is the canonical example. Direct
UPDATE on `current_offer_id` is appropriate only when there is already
a live queue-eligible offer to point at.

### L-20: Validate inputs before bisecting code

When a recently-deployed change correlates in time with an observed
regression, the temptation to bisect is strong. But correlation is not
causation: an unrelated operational change may have happened around
the same window. Before committing to a bisect, run the existing
integration harness (Bruno) against the new deploy. A 60-second pass
eliminates a 5-hour bisect.

**Mitigation rule:** the first response to "X is broken since deploy Y"
is to run the existing test harness against deploy Y. Only if the
harness reproduces the failure does code-level bisecting make sense.

### L-21: Forensic columns are diagnostic legend

Phase 1B's restored bindings turned what would have been an opaque
"matcher is dark" into an inspectable pattern: `cluster_size` populated
but `wai_confidence` NULL while `planner_action` populated localized
the failure to the matcher dispatch path within minutes of querying
`pudo_decision_context`. Forensic columns earn their keep on the days
something is wrong.

## Branch and deploy state

- Branch: `demolition-2026-05-04` HEAD `bab1e13` (Phase 2a closeout)
- Deploy: `puddlejumper-api-00591-vj7` (unchanged)
- Bruno: 17/17 against `00591-vj7` post-diagnosis
- Pytest: 257/257 (unchanged)
- `driver_trip_state.current_offer_id` for `UjT1hE9eBXh2q95aSZYOkzDJ8lo1`:
  cleared to NULL post-diagnosis.

## Next sprint unblocked

Phase 2b (Google Places integration) per `PHASE_2A_CLOSEOUT.md` is
unblocked. ~2-3 hours scope. No prerequisite work introduced by this
diagnosis.
