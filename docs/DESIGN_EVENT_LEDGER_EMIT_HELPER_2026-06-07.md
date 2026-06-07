# Event-Ledger — emit-helper + queue-diff Design (Phase 1) for Gemini review

**Status:** DESIGN proposal (recon → **design** → ratify → implement). The Phase-1 migration is
APPLIED (`event_ledger` live, keystone verified runtime-true). This is the hot-path artifact — where
the C4 batched-savepoint actually lands — so it gets the loop, not just an apply. **No code until
ratified.** Builds on `DESIGN_EVENT_LEDGER_DELTA_2026-06-07.md` (C1–C7) + the now-live §VIII.

**Recon basis (verbatim, this sprint):** `post_heartbeat` (`driver_heartbeat.py:1888–2406`):
- LOAD `:1932–1957`: `snap = queue.snapshot(...)` → `current_offer_id = snap.bound_offer_id`,
  `queue_offer_ids = set(snap.offer_ids)` (the **current live set**), `queue_metadata`.
  `compute_effective_last_move` (`:1942`) already SELECTs `driver_trip_state` (seed-read piggyback).
- Authoritative write `:1978` (`UPDATE driver_trip_state … RETURNING arrest_*`) — heartbeat/arrest.
- EXECUTE `:2344–2361`: `for action in actions: _execute_action(...)` → `executed_actions`,
  `dispatch_error_msg`, `dispatch_executed`. Post-dispatch offer id = `_derive_post_offer_id`.
- LOG `:2382–2404`: `_log_decision_context(...)` in a swallow-handler; `cadence_target_hz` computed
  at `:2380` (horny = WAI≥0.40 ∧ speed<5).
- `conn.commit()` `:2406` (single terminal commit; the C4 anchor).

## 0. Scope (greenfield Phase 1)

Emit the new events ALONGSIDE the untouched legacy writers (`_log_decision_context`, `heartbeat_log`
keep writing). No consolidation (Phase 2). The ledger is the §VIII **Passive lane**: append-only,
never read by a runtime decision, never alters state.

## 1. The batch insertion point + structure (C4: batched SAVEPOINT, single connection)

Insert **after** the LOG try/except (`:2404`), **before** `conn.commit()` (`:2406`). By then every
tick fact is in hand. The atomic unit is the **whole tick's ledger batch (all emits + the
seed-advance)** in ONE savepoint on the heartbeat connection:

```python
# ── EMIT (event-ledger; §VIII Passive lane; C4 batched savepoint) ──
try:
    cur.execute("SAVEPOINT ledger_batch")
    events = _gather_ledger_events(...)          # pure: diff + inflection + sensor + PUDO + keyframe
    if events:
        for ev in events:
            _emit_event(cur, driver_id, ev)       # INSERT INTO event_ledger (no per-emit swallow)
        _advance_ledger_seed(cur, driver_id, new_seed)   # UPDATE driver_trip_state.ledger_state
    cur.execute("RELEASE SAVEPOINT ledger_batch")
except Exception as e:
    cur.execute("ROLLBACK TO SAVEPOINT ledger_batch")    # emits AND seed revert together
    log.exception("[heartbeat] ledger batch failed (swallowed): %s", e)
conn.commit()
```

- **Swallow at the BATCH level, not per-emit** (per-emit swallow is the silent-reap-loss bug C4
  surfaced: an emit reverts while the seed advances past it). On batch failure, emits *and* seed
  revert → next tick re-diffs the un-advanced seed and **retries** (self-heals; single connection ⇒
  nothing partial separately committed ⇒ no duplicate).
- `_emit_event` itself does NOT swallow — it just INSERTs; the batch wrapper owns the swallow.
- Parent's authoritative writes (`:1978`, `_log_decision_context`) commit regardless (§VIII).
- **Structural guard (C4):** a §XIV.J test asserts no uncaught `raise` and no `conn.rollback()`
  between the first emit and `:2406` — pin the property, don't carry a parallel connection.

## 2. The seed (diff-seed; lives in `driver_trip_state.ledger_state` jsonb — already migrated)

`ledger_state = {last_queue_snapshot, last_matcher_snapshot, keyframe_count, last_keyframe_at}`.
- **READ** at LOAD (one PK read; piggyback `compute_effective_last_move`'s existing
  `driver_trip_state` SELECT, or a tiny adjacent SELECT). `:1978` does NOT touch `ledger_state`, so
  the prior value is readable any time before the batch.
- **WRITTEN** only inside the batch (`_advance_ledger_seed`), only on emitting ticks.

## 3. Queue-diff (C2/C3: emergent reaps, the gold event)

`prior = ledger_state.last_queue_snapshot` (ids+status); `current = queue_offer_ids` (the live set
already computed at `:1948`).
- `entered = current − prior` → `offer_entered_queue`.
- `left = prior − current`:
  - **Handler-attributable (C2):** if a dropoff/abandon fired this tick (in `executed_actions` / the
    §9.9 sweep), attribute `terminated` / `abandoned` from the handler signal and treat the diff as a
    *reconcile* cross-check, not the primary record.
  - **Emergent (the gold):** otherwise run the **Option-B scoped probe** (C3) — ONE query for just
    the dropped ids fetching the six attribution columns (`expected_pickup_distance`,
    `expected_dropoff_distance`, `expected_odometer_status`, `last_odometer_move_at`,
    `actual_pickup_at`, `actual_dropoff_at`) — then a six-clause check in Python maps the drop to a
    reason ∈ {`causality`, `ceiling`, `staleness`, `band_overshot`(+leg)}. The projections do NOT
    carry these columns (C3-verified), so the probe is required; it fires only on the rare reap tick.
  - Emit `offer_left_queue(reason=…)`. All six reasons mirrored, incl. live-impossible `causality`
    (C1 — capture clock/replay-seed bugs, never silently misattribute).

## 4. Matcher-inflection (C-§6.2)

`prior = ledger_state.last_matcher_snapshot {top_candidate_offer_id, confidence_tier}`; `current`
from `diagnostics`/`matches` (top match + `_max_wai_confidence` already computed at `:2371`). Emit
`matcher_eval` when: top candidate changes, OR confidence_tier crosses (**tier edges include the
0.40 floor** so floor-cross = tier-change), OR fire/abstain. Payload re-homes `wai_per_offer_scores`.

## 5. Sensor / PUDO / keyframe events

- **arrest_start/end** ← the `:1978` RETURNING (`arrest_started_at_post`, `arrest_counter_s_post`);
  **cluster_formed/dissolved** ← `diagnostics.cluster` transitions; **cadence_change** ←
  `cadence_target_hz` vs prior. All end-of-tick, in the batch.
- **pickup_detected / dropoff_detected / bind / unbind** ← `executed_actions` (FirePickup,
  FireDropoff, FirePickup/DropoffObservation, ClearNarrative) — classified from the action types.
- **keyframe** ← scheduler (§6).

## 6. C5 RESOLVED by recon → counter-in-seed-write

Every `post_heartbeat` event emits in the batch, which writes `ledger_state` once per **emitting**
tick. So the keyframe counter (`keyframe_count`) rides that write **for free** — no `COUNT(*)`
range-scan, no §VIII read-purity gray area. Keyframe fires when `keyframe_count ≥ 50` OR
`now − last_keyframe_at ≥ 15 min` (both load-bearing; reset both on keyframe).
- **Cost named (eyes open):** on emitting ticks this is a SECOND `driver_trip_state` write (the
  `:1978` authoritative write + the batch `ledger_state` write). That second write is the
  C4-correctness price already paid for the seed-advance (it MUST be in the batch, not `:1978`, to
  avoid silent-reap-loss) — so the counter is genuinely free, riding a write that must happen anyway.
  Non-emitting ticks do only `:1978` (one write). MVCC-tolerable at 0.2–1 Hz.
- **Exception:** `offer_received` emits in `decisions/logger.py` (the `/decisions` endpoint), a
  different txn — it does NOT ride the heartbeat seed and does NOT touch `keyframe_count` (the
  keyframe counter is heartbeat-scoped). It carries no queue-diff.

## 7. `_gather_ledger_events` is PURE; `_emit_event` is the only writer

`_gather_ledger_events(...)` takes the in-hand tick state (snap/diff inputs, diagnostics, matches,
executed_actions, arrest/cluster/cadence, prior seed) and RETURNS `(events, new_seed)` with no DB
writes — testable without a cursor. `_emit_event(cur, driver_id, ev)` does the single INSERT
(`event_time` defaults to `now()` §II; `lat/lng` raw §I). Keeps the hot path's only ledger write
behind one helper and the decision logic unit-testable.

## 8. §XIV.J test floor (the loop's real payoff here — hot-path, not inert DDL)

1. **Batch atomicity / silent-loss (C6):** force an emit failure inside the batch → assert (a)
   parent commits with authoritative writes intact, (b) `ledger_state` seed did NOT advance for the
   reaped ids, (c) re-run re-emits the reap **exactly once**. (Replaces Gemini's vacuous
   connection-kill test.)
2. **Six-reason clause mapping (C6):** a drop maps to the exact clause for all SIX reasons, incl.
   synthesizing a future-`created_at`/skewed-`reference_time` row to exercise `causality`.
3. **Runtime-true keystone on a POPULATED CHILD partition (C6):** `atjb` UPDATE/DELETE denied where
   rows physically live (not just the parent — catalog-true ≠ runtime-true).
4. **No-uncaught-raise structural test (C4 guard).**
5. **Post-reap readability (C3 assumption):** a reaped offer is still SELECT-able from
   `offer_history` with its six attribution columns.

## 9. Open / deferred

- **§XVI.G `suppressed_contexts`** — promote to a first-class `lock_suppressed` event, or keep inside
  the `tad_decision_context`-style payload? (Carried from the delta; decide during impl.)
- Event `summary` text wording per type (human timeline lines) — cosmetic, settle in impl.

## 10. Scope guards (unchanged)

Does not touch legacy writers (Phase 2). Does not read the ledger from any runtime decision path
(§VIII). Does not store `app_verdict` anywhere (§XV). Does not make the Fix #3 product call.
