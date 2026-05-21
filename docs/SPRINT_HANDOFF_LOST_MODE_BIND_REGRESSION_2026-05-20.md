# Lost-Mode Bind Regression Sprint Handoff

**Date authored:** 2026-05-20 (afternoon, post-deploy investigation)
**Authoring chat:** Driver-side monitor alarm went silent on real
pickups/dropoffs; backend trace from `puddle-jumper` VM
**Status:** Forensic shape captured; regression bracketed to §XVIII Lost
Mode commits; diagnosis is this sprint's scope.

---

## TL;DR

The web-app Monitor alarm (`94a81a4 MonitorScreen: Bug 1 trap — alarm on
current_offer_id non-null→null`, shipped 2026-05-15) fires when
`/api/v1/driver/status.offer_id` transitions from a non-null UUID to
null. That field is `driver_trip_state.current_offer_id` (per
`driver_status.py:132`).

**The alarm worked on 5/14 and 5/15** because the backend was binding
`current_offer_id` on `FirePickup` and clearing it on `FireDropoff`:

| day | total PDC rows | bound evals (`current_offer_id_at_eval` non-null) |
|---|---|---|
| 2026-05-14 | 8,630 | **905** |
| 2026-05-15 | 5,857 | **225** |
| 2026-05-16 | 10,086 | **0** |
| 2026-05-17 | 4,207 | **0** |
| 2026-05-18 | 12,712 | **0** |
| 2026-05-19 | 9,597 | **0** |
| 2026-05-20 | 13,461 | **0** |

(`app_private.pudo_decision_context`, driver
`UjT1hE9eBXh2q95aSZYOkzDJ8lo1`.)

**The cliff lines up with §XVIII Driver-State Lost Mode landing:**
- `70a22b9 feat(§XVIII): driver-state lost mode — fix detection,
  retire per-offer narrative_violation behavior` (2026-05-17 13:37 UTC)
- `3cd1e01 feat(§XVIII): dispatcher emits observation-only actions in
  lost-mode` (2026-05-17 13:56 UTC)
- Canonical docs: `c8f86e3` (Prime Directive §0), `ab59a05` (§XVIII
  advice-blind lost mode, 2026-05-16)

In lost-mode, `dispatch.py` (Phase-2c-2 branch, `_demote_to_observation`
at line ~218 with the call site at lines 212-213) rewrites every
`FirePickup`→`FirePickupObservation` and `FireDropoff`→
`FireDropoffObservation`. The Observation variants intentionally skip
the `UPDATE driver_trip_state SET current_offer_id = …` write
(`driver_heartbeat.py:158-162` action wiring; FPO handler at line ~379,
FDO at line ~477). Result: `current_offer_id` stays NULL forever, no
edge transition ever reaches the monitor, no chime.

The detector at `driver_heartbeat.py:717 _detect_lost_mode` returns
True whenever *any* queued offer has `actual_pickup_at IS NULL` and is
still predicate-alive per `LIVE_OFFER_PREDICATE_SQL`
(`driver_queue.py:183`). The active driver currently has a backlog of
such offers (oh_ids 8094, 8092, 8091, 8090, 8089, 8088, 8086 — all
ACCEPT, all `actual_pickup_at IS NULL` from 2026-05-20 mornings). So
the detector latches True on every heartbeat and the demotion path
runs unconditionally.

**This is architecturally distinct from the original Dispatch-Silence
sprint** (`docs/SPRINT_HANDOFF_DISPATCH_SILENCE_2026-05-11.md`). That
brief's failure was "the planner never runs / forensic blobs are
NULL." Today, the planner runs, forensic blobs populate, and
observation fires are emitted on real pickup/dropoff events — but the
narrative-state bind is suppressed by design. The §XVIII work was a
deliberate semantic change; this sprint's job is to reconcile that
change with the driver-facing monitor signal.

---

## What is working (do NOT re-recon)

- **Planner runs and dispatches actions.** Today's distribution from
  `pudo_decision_context.planner_action` over the last 24h:
  ```
   planner_action                                               | count
  --------------------------------------------------------------+-------
   FirePickupObservation                                        |    26
   FirePickupObservation+FirePickupObservation                  |    11
   FireDropoffObservation                                       |     5
   FireDropoffObservation+FireDropoffObservation+ClearNarrative |     2
   (null — heartbeats with no action)                           | 13421
  ```
- **Forensic blobs populate.** 985 non-NULL `tad_decision_context` rows
  in the last 24h. The forensic write path is healthy (the failure the
  Dispatch-Silence brief hypothesized for H4 is not present here).
- **`actual_pickup_at` / `actual_dropoff_at` populate on real events.**
  Today's verdict→confirmation funnel for the driver:
  ```
   accepts | pickups_confirmed | dropoffs_confirmed
  ---------+-------------------+--------------------
        13 |                 5 |                  9
  ```
  Per-offer confirmations land — they just don't flip the narrative
  bit. (oh_id 8095 today is a clean roundtrip: ACCEPT 12:30 → pickup
  12:42 → dropoff 13:02.)
- **Frontend wiring is correct.** `MonitorScreen.jsx:115-124` reads
  `data.offer_id`, tracks `prevOfferIdRef`, and calls `fireAlarm` on
  the non-null → null edge with no dedup. Test-alarm button proves the
  audio path works on the device. **Do not propose frontend changes
  in this sprint** — the user has separately scoped a frontend rewire
  as fallback option 2 and explicitly chose this path instead.
- **Heartbeat cadence is healthy.** 13,461 PDC rows in 24h ≈ one
  every 6.4s, consistent with the production heartbeat interval.

---

## What we DON'T know (this sprint's scope)

The design question is upstream of the implementation:

**Q1 — What is the intended semantic for `current_offer_id` in
lost-mode?** §XVIII.A bit 1 says "structurally implies
`current_offer_id=None` at the caller" — i.e., lost-mode is *defined*
as the state where there is no bound narrative. So setting
`current_offer_id` from a lost-mode action would violate the §XVIII
invariant. The original §XVIII commits chose to demote rather than
bind, presumably for this reason. **Is binding from lost-mode actually
intended to remain forbidden, in which case the driver-facing pickup/
dropoff signal needs a different surface?** Or is there a designed
escape hatch (e.g., "lost-mode is reset once a pickup observation
lands on a queue with exactly one alive offer") that hasn't been
wired yet?

**Q2 — Why does the lost-mode detector latch?** The detector returns
True if any queued offer has `actual_pickup_at IS NULL` and is
predicate-alive. For the active driver right now there are 7+ such
offers — accepted in the morning, never fired. The
`LIVE_OFFER_PREDICATE_SQL` includes a 4-hour abandonment ceiling and
an odometer-staleness gate, but the backlog still slipped through.
What's keeping these offers predicate-alive 8+ hours later — distance
gate not closing, or staleness gate misconfigured? If the backlog
were correctly reaped, the detector would flip False on the next
heartbeat after the last live offer expired, and binding would
resume.

**Q3 — What is the right user-facing semantic?** The user's working
mental model is "the alarm fires when a pickup or dropoff is
confirmed for the ride I'm currently on." If lost-mode is the
permanent operating regime (per §0/§XVIII philosophy), then "the
ride I'm currently on" isn't a state the system models — and the
monitor needs to surface the per-offer confirmation event directly
instead. If lost-mode is meant to be transient, then the alarm
signal will return once the binding does. **The user should ratify
the framing before code changes.**

---

## Hypothesis space (ranked priors, NOT verified)

### H1 — The backlog is the only blocker; binding resumes when it clears (MEDIUM-HIGH)
Investigation prediction: if the 7 unfired offers are manually
expired (or expire naturally), `_detect_lost_mode` returns False on
the next heartbeat and the regular `FirePickup`/`FireDropoff` path
takes over for the next real ride. **Test by checking what each
oh_id 8086/8088-92/8094 looks like through `LIVE_OFFER_PREDICATE_SQL`
right now.** If they all evaluate alive, find the clause keeping them
alive (distance gate vs. abandonment ceiling vs. odometer gate).

### H2 — Lost-mode is meant to be permanent for this driver/regime (MEDIUM)
Per §0 Prime Directive and §XVIII "advice-blind" framing, lost-mode
may be the intentional steady state for the production driver after
the post-Apr-25 dispatcher refactor. If so, no amount of GC tuning
restores the alarm — the monitor signal needs to move off
`current_offer_id` and onto observation-fire events. **Resolve by
reading §XVIII.A bit 1 + §0.B carefully and asking the user to
clarify intent before writing code.**

### H3 — There is a designed lost-mode → narrative transition that's missing wiring (MEDIUM-LOW)
A pickup observation on a queue with exactly one alive offer is
functionally identical to a normal `FirePickup` — there's no
ambiguity, no narrative violation, just a confirmation. §XVIII might
intend for this case to exit lost-mode and bind `current_offer_id`,
but the wiring may have been deferred. Check: does any commit on the
`phase-2c-2-tad-exit-4tools` branch since 5/17 mention "lost-mode
exit" or "narrative recovery"?

### H4 — Detector predicate is overinclusive (LOW)
Maybe `actual_pickup_at IS NULL` is too coarse — should it be
`actual_pickup_at IS NULL AND app_verdict = 'ACCEPT'`? A `DECLINE`
that's still predicate-alive shouldn't be holding the driver in
lost-mode. Quick to verify by checking whether any of the backlog
offers are DECLINE.

---

## Recon path (first-session moves)

When opening this sprint in a fresh chat:

1. **Verify the failure is current.** Re-run the 7-day PDC fill-rate
   query (below). Confirm bound_evals is still 0 and lost-mode is
   still latched on the active driver.

2. **Test H4 first** (cheapest): inspect each oh_id in the current
   backlog. Any DECLINE rows holding lost-mode latched is a one-line
   detector fix.

3. **Test H1 second**: run `LIVE_OFFER_PREDICATE_SQL` interactively
   against each backlog offer to identify which clause keeps them
   alive. If the offers should be reaped but aren't, the fix is in
   the predicate, not in §XVIII.

4. **Test H3 third**: grep the branch since 2026-05-17 for any
   commits mentioning lost-mode exit, narrative recovery, or single-
   alive-offer handling. If none, propose H3 as a design conversation
   with the user.

5. **Test H2 last**: if H1/H3/H4 are all negative, the §XVIII
   semantic *is* intentional permanence and this sprint's verdict is
   "no backend fix; revise the monitor signal." Loop back to the
   user and surface fallback option 2 (frontend rewire to
   observation-fire events) from the originating chat.

---

## Forensic data to load fresh

When opening the next chat:

- **This brief:** `docs/SPRINT_HANDOFF_LOST_MODE_BIND_REGRESSION_2026-05-20.md`
- **Standard load:** `docs/CANONICAL_RULES.md` (esp. §0 Prime
  Directive, §XIV.I, §XVIII Driver-State Lost Mode, §XV advice-blind),
  `docs/SESSION_PROTOCOL.md`, `docs/INDEX.md`
- **Predecessor brief (related but distinct):**
  `docs/SPRINT_HANDOFF_DISPATCH_SILENCE_2026-05-11.md`. Read its
  "What we know works" / "What we DON'T know" sections for the prior
  forensic baseline. **Do not unwind its scope.** That sprint
  diagnosed a different failure mode (planner never ran); this one
  diagnoses a binding suppression introduced by a later fix attempt.
- **Authoring commits to inspect:**
  - `70a22b9` and `3cd1e01` (the §XVIII feature commits)
  - `ab59a05` (canonical §XVIII text — the design intent)
  - `c8f86e3` (§0 Prime Directive — read this before second-guessing
    §XVIII's intent)
- **Deployed revision:** `puddlejumper-api-00622-562` (commit
  `ae74f51` on `phase-2c-2-tad-exit-4tools`, deployed 2026-05-20
  18:47 UTC). Branch is 43 commits ahead of `main`.

Live recon queries (copy-paste ready):

```sql
-- PDC fill-rate by day for the active driver (the cliff to confirm)
SELECT
  date_trunc('day', created_at) AT TIME ZONE 'UTC' AS day,
  COUNT(*) AS pdc_rows,
  COUNT(*) FILTER (WHERE current_offer_id_at_eval IS NOT NULL) AS bound_evals,
  COUNT(*) FILTER (WHERE planner_action IS NOT NULL) AS dispatched,
  COUNT(*) FILTER (WHERE tad_decision_context IS NOT NULL) AS forensic_blobs
FROM app_private.pudo_decision_context
WHERE driver_id = 'UjT1hE9eBXh2q95aSZYOkzDJ8lo1'
  AND created_at >= NOW() - INTERVAL '7 days'
GROUP BY 1 ORDER BY 1 DESC;

-- Current backlog keeping lost-mode latched
SELECT oh.id, oh.created_at AT TIME ZONE 'UTC' AS created_utc,
       oh.app_verdict, oh.actual_pickup_at, oh.actual_dropoff_at,
       oh.miles_at_offer_receipt, oh.pickup_miles, oh.trip_miles,
       oh.expected_dropoff_distance
FROM app_private.offer_history oh
JOIN app_private.decision_log dl ON dl.id = oh.decision_log_id
WHERE dl.driver_id = 'UjT1hE9eBXh2q95aSZYOkzDJ8lo1'
  AND oh.actual_pickup_at IS NULL
ORDER BY oh.created_at DESC
LIMIT 20;

-- Current driver_trip_state snapshot
SELECT driver_id, current_offer_id, heartbeat_at AT TIME ZONE 'UTC' AS hb_utc,
       last_odometer_move_at AT TIME ZONE 'UTC' AS last_move_utc,
       leg_start_cumulative_miles, arrest_counter_s, phase_current
FROM app_private.driver_trip_state
WHERE driver_id = 'UjT1hE9eBXh2q95aSZYOkzDJ8lo1';

-- Recent dispatched actions (proves planner is alive but only emitting observations)
SELECT id, created_at AT TIME ZONE 'UTC' AS at_utc, planner_action,
       primary_offer_id, current_offer_id_at_eval
FROM app_private.pudo_decision_context
WHERE driver_id = 'UjT1hE9eBXh2q95aSZYOkzDJ8lo1'
  AND planner_action IS NOT NULL
ORDER BY created_at DESC
LIMIT 20;
```

DB access (from authoring chat):
- `gcloud compute ssh puddle-jumper --tunnel-through-iap
  --zone=us-central1-f` (peer auth as `postgres`)
- Then `sudo -u postgres psql -d puddlejumper -P pager=off`

---

## What this sprint is NOT

- **Not a frontend rewire.** The originating chat offered the user a
  fallback "rewire `MonitorScreen.jsx` to fire the chime on a new
  `FirePickupObservation`/`FireDropoffObservation` in
  `last_3_dispatch_actions`." The user picked the backend path
  instead. **Do not loop back to the frontend without surfacing why
  the backend path is unreachable first** (i.e., only switch to the
  frontend if H2 is the diagnosis and the user re-ratifies).
- **Not re-opening §XVIII.** Driver-State Lost Mode is canonical and
  intentional. The job is reconciling its semantic with the driver-
  facing signal, not unwinding it. Read §0 + §XVIII before suggesting
  any narrative-bind changes.
- **Not re-opening Dispatch-Silence (5/11).** That sprint diagnosed a
  pre-§XVIII failure mode. This sprint is a regression introduced by
  the §XVIII fix attempt. Independent scope.
- **Not a refactor sprint.** Goal is "the audible alarm fires on a
  real confirmed pickup or dropoff in production." Architectural
  cleanups deferred.

---

## Verification gate (what closes this sprint)

A single real Uber ride, accepted and driven in production, where:

1. The driver's web Monitor (`app.puddlejumper.io`) plays the 880 Hz
   chime and flashes red within ~3s of the actual pickup happening
   (per `/api/v1/driver/status` poll cadence)
2. The same alarm fires within ~3s of the actual dropoff happening
3. `driver_trip_state.current_offer_id` is observed non-null between
   pickup and dropoff in a live DB snapshot (or, if H2 is the
   diagnosis and the frontend was rewired instead, the equivalent
   observation-fire signal carries through `last_3_dispatch_actions`
   and the monitor reacts to it)
4. No regression in §XVIII semantics: the lost-mode detector still
   correctly latches when there's an unconfirmed-pickup backlog;
   observation fires still emit correctly when the detector is True
5. `pudo_decision_context` from the drive shows a non-zero count of
   `current_offer_id_at_eval IS NOT NULL` rows (or, under H2, a clear
   audit trail showing why binding stays NULL by design)

---

## Note on pace

The user's driving day is over (it's late afternoon Texas time on
2026-05-20). The forensic data is captured; this is not actively
burning. The web app's "Test alarm" button still works for the
user's own validation; the production-event alarm is what's
silent. Per the user's stated preference (see authoring chat
memory: pragmatic stopping points), ship the minimum fix that
restores the audible signal — do not turn this into a full §XVIII
re-litigation.

---

## Kickoff prompt for next chat

> Loaded: SPRINT_HANDOFF_LOST_MODE_BIND_REGRESSION_2026-05-20.md.
> Standard load: CANONICAL_RULES (§0, §XIV.I, §XV, §XVIII),
> SESSION_PROTOCOL, INDEX.
> Predecessor: SPRINT_HANDOFF_DISPATCH_SILENCE_2026-05-11.md
> (related but distinct; do not unwind).
>
> Mission: the web-app Monitor alarm (commit `94a81a4`, shipped
> 2026-05-15) went silent on real pickups/dropoffs starting
> 2026-05-16, coincident with §XVIII Lost Mode landing (commits
> `70a22b9` + `3cd1e01`). The alarm watches
> `/api/v1/driver/status.offer_id` for non-null → null edges;
> `driver_trip_state.current_offer_id` has been NULL on every PDC
> row for 4+ days because `dispatch.py` demotes every
> `FirePickup`/`FireDropoff` to Observation when `_detect_lost_mode`
> returns True, and the detector is latched True by an unconfirmed-
> pickup backlog (oh_ids 8086, 8088-92, 8094 as of authoring time).
>
> Per session protocol: recon first. Start with H4 (DECLINE rows
> in backlog — cheapest detector fix) then H1 (LIVE_OFFER_PREDICATE
> per-clause inspection) then H3 (lost-mode exit wiring already
> designed but unwired) then H2 (lost-mode is intentional
> permanence — surface fallback to user). Propose the recon
> sequence for ratification before running.
