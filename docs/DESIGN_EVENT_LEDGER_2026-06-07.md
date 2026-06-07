# Event-Ledger — Design (greenfield Phase 1) for Gemini review

**Status:** DESIGN proposal. Per the build loop (recon → **design** → Gemini-ratify → migrate);
the L-6 writer inventory (`RECON_EVENT_LEDGER_WRITER_INVENTORY_2026-06-07.md`) is done. **No code
until this design is ratified.** Builds on the now-live canon: §I/§II Foundation (Space/Time),
§VIII EXECUTE two-lane (the ledger IS the Passive lane), §XIV.H predicate, §VII, §0/§XV.

Invariants are frozen (see `BRAINSTORM_EVENT_LEDGER_2026-06-07.md`): change/decision-only +
keyframes; emergent-reap diffing with causing clause; append-only never-backfill, schema-enforced.

## 0. Phasing (decided: greenfield-first + shadow-validate, then cut over)

- **Phase 1 (this design):** an **additive** `event_ledger` + a thin emit helper + the diff-seed
  columns on `driver_trip_state`. Emit the **missing** events (queue deltas/reaps, keyframes, offer
  lifecycle, sensor, matcher-inflection, PUDO). Legacy logs (`pudo_decision_context`, `heartbeat_log`)
  keep writing **untouched**. Run the next `00649` drive → shadow-validate the change-only stream
  reconstructs what pdc captured.
- **Phase 2 (deferred):** consolidation migration (pdc → `matcher_eval` events; `driver_trip_state_log`
  is dead; back-compat view), gated on the shadow assertion. Runs in the shadow window.

## 1. Schema — `app_private.event_ledger`

```sql
CREATE TABLE app_private.event_ledger (
    id               bigint GENERATED ALWAYS AS IDENTITY,
    driver_id        text        NOT NULL,
    event_time       timestamptz NOT NULL DEFAULT now(),   -- §II: UTC, never naive
    event_type       text        NOT NULL,
    offer_id         text,                                  -- identity bridge; verdict-blind
    cluster_id       text,
    arrest_id        text,
    lat              double precision,                      -- §I: raw (lat,lng); no raw geometry
    lng              double precision,
    gps_accuracy_m   double precision,
    cumulative_miles double precision,
    queue_snapshot   jsonb,   -- KEYFRAME: full [{offer_id,status,in_band,picked_up}] + bound id
    queue_delta      jsonb,   -- CHANGE:   {entered:[id...], left:[{offer_id,reason}]}
    matcher_snapshot jsonb,   -- {top_candidate_offer_id, confidence_tier}
    payload          jsonb,   -- heterogeneous per type (signals/scores/reason; raw+snapped pos)
    summary          text     -- human-readable timeline line
) PARTITION BY RANGE (event_time);
```
- **Partitioning:** by **day**; retain **14 days**; drop the oldest partition (O(1), no vacuum churn).
  Lifecycle via `pg_partman` or a tiny scheduled job (decide — §6). No rollup crons.
- **Indexes (per partition):** `(driver_id, event_time DESC)` [the lookback], `(offer_id)`, `(event_type)`.
- **Append-only, schema-enforced (the keystone):** `REVOKE UPDATE, DELETE ON app_private.event_ledger
  FROM <app_role>;` — a confirmation is a NEW event, never a mutation. Not convention; grants.
- **§I/§II compliance:** `event_time timestamptz DEFAULT now()`; `lat/lng` raw `(lat,lng)`; any derived
  H3 via `coords_to_h3(lat,lng)` at read time, never stored as raw geometry.

## 2. `driver_trip_state` additions (diff-seed; Option 1, the Authoritative lane)

Add `last_queue_snapshot jsonb`, `last_matcher_snapshot jsonb` (or fold into the existing `heartbeat`
jsonb — §6 open). **READ** at LOAD (top of `post_heartbeat`, already reads driver_trip_state).
**WRITTEN** in the existing per-tick `UPDATE … RETURNING` (`driver_heartbeat.py:1979`) — but **only
when the diff detects a change/reap** (the §VIII MVCC guard: write frequency = event density, not
heartbeat density). The ledger is never read by the runtime; the diff-seed lives here.

## 3. Emit architecture

`emit_event(cur, driver_id, event_type, *, offer_id=None, cluster_id=None, lat=None, lng=None,
queue_delta=None, queue_snapshot=None, matcher_snapshot=None, payload=None, summary=None)` — the
**Passive lane** helper: best-effort, append-only, **swallows its own errors** (a ledger failure
must never break a live decision — §VIII).

- **Transaction safety (IMPORTANT):** a ledger INSERT that errors inside the heartbeat's txn would
  poison it (the aborted-transaction trap we already hit in §5.5). So emit on a **SAVEPOINT**
  (release on success, rollback-to-savepoint on error) so a ledger failure can't abort the
  heartbeat's authoritative writes. (Alternative: a separate connection — heavier; SAVEPOINT
  preferred.) This is a hard design requirement, not an optimization.

**Emit sites** (from L-6):
- `decisions/logger.py` (log_decision): `offer_received`.
- `post_heartbeat`: the queue-diff (`offer_entered_queue` / `offer_left_queue`) + keyframe; cluster
  `formed/dissolved`, `arrest_start/end`, `cadence_change`; matcher inflections; PUDO dispatch
  (`pickup_detected`/`dropoff_detected`/observation, `bind`/`unbind`).

## 4. Queue-diff engine (§3b — emergent reaps, the gold event)

Each heartbeat: `prior` = `driver_trip_state.last_queue_snapshot` (read at LOAD); `current` =
`LIVE_OFFER_PREDICATE_SQL` output (the queue projection already computed this tick).
- `entered = current − prior` → `offer_entered_queue`.
- `left = prior − current` → for each, **clause-diagnostic** to attribute the reap, then
  `offer_left_queue(reason=<clause>)`. Reasons map to the 5 predicate clauses (driver_queue.py):
  **causality/ceiling** (`created_at` window, 199/208), **staleness** (216–219), **band** (240),
  **abandoned** (§9.9, 265). The reap is emergent (no decision-point) — this diff is the ONLY way
  to capture it.
- **Clause-diagnostic mechanism (open — §6):** re-evaluate the dropped offer against each clause
  individually. Either a single-row per-clause SQL probe, or an in-Python check against the offer's
  metadata (the band/staleness inputs are already in hand). Perf vs fidelity tradeoff for Gemini.

## 5. Keyframe scheduler (§4 — 50 events OR 15 min, both load-bearing)

Per driver, keyframe (full `queue_snapshot`) when **events-since-keyframe ≥ 50** OR
**minutes-since-keyframe ≥ 15**, whichever first. Drive-start is the first keyframe. Counter/last-
keyframe-time home: `driver_trip_state` columns vs derived-from-ledger (§6 open).

## 6. Matcher-inflection events (§5 / §6.2)

`prior` = `driver_trip_state.last_matcher_snapshot {top_candidate_offer_id, confidence_tier}`;
`current` from WAI diagnostics. Emit `matcher_eval` when: top candidate changes, OR confidence_tier
crosses (**tier boundaries MUST include the 0.40 floor** so floor-cross = tier-change), OR
fire/abstain. Payload carries the per-offer signal breakdown (re-home of `wai_per_offer_scores`).

## 7. Event taxonomy (Phase 1)

`offer_received`, `offer_entered_queue`, `offer_left_queue`(reason), `offer_deferred` (§5.5),
`offer_abandoned` (§9.9), `cluster_formed`, `cluster_dissolved`, `arrest_start`, `arrest_end`,
`cadence_change`, `matcher_eval` (inflection), `pickup_detected`, `dropoff_detected`, `bind`,
`unbind`, `keyframe`. (Accept/decline are NOT lifecycle states — §XV verdict-blindness; verdict is
advice metadata on the offer, never a ledger event.)

## 8. Open design questions for Gemini

1. **emit txn safety** — confirm SAVEPOINT-per-emit (vs separate connection). Load-bearing.
2. **Clause-diagnostic** for `offer_left_queue(reason)` — per-clause SQL probe vs in-Python metadata
   check. Which gives faithful attribution without a per-heartbeat query storm?
3. **diff-seed + keyframe counters** — new `driver_trip_state` columns, or fold into the existing
   `heartbeat` jsonb? (MVCC: more columns on the hottest row vs one jsonb rewrite.)
4. **queue_snapshot vs queue_delta** — two columns (as drafted) or one jsonb with a `kind`
   discriminator?
5. **Partition tooling** — `pg_partman` vs scheduled job for daily create/drop.
6. **§XIV.J live-PG test floor** — the emit helper + queue-diff touch the cursor; enumerate the
   test obligations now.

## 9. What this does NOT do (scope guard)

Does not touch the legacy writers (Phase 2). Does not make the Fix #3 product call. Does not read
the ledger from any runtime decision path (§VIII Passive lane). Does not store `app_verdict` as a
gating field anywhere (§XV).
