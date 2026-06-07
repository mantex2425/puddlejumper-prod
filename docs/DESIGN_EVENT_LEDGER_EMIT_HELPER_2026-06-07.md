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
- **READ** at LOAD via **one dedicated cheap PK SELECT** on `driver_trip_state` (chosen over
  extending `compute_effective_last_move` — that helper is queue/staleness, ledger_state is
  observability; keep them uncoupled per §VIII; the read is a single PK lookup, negligible). `:1978`
  does NOT touch `ledger_state`, so the prior value is readable any time before the batch.
- **WRITTEN** only inside the batch (`_advance_ledger_seed`), only on emitting ticks.
- **Cold-start contract (`ledger_state IS NULL`, first post-migration tick):** treat `prior` as
  **empty** but do NOT run the diff (an empty prior would otherwise emit "all offers entered"). Emit
  a single **drive-start keyframe** (full current snapshot), set `seed := current`, emit no
  `offer_entered` / no reaps. `_gather_ledger_events` states this in its contract.

## 3. Queue-diff (C3: emergent reaps, the gold event) — probe attributes ALL six

`prior = ledger_state.last_queue_snapshot` (ids+status); `current = queue_offer_ids` (the live set
computed at `:1948`).
- `entered = current − prior` → `offer_entered_queue`.
- `left = prior − current` → the **Option-B scoped probe** for ALL left ids: ONE query for just the
  dropped ids fetching the six attribution columns (`expected_pickup_distance`,
  `expected_dropoff_distance`, `expected_odometer_status`, `last_odometer_move_at`,
  `actual_pickup_at`, `actual_dropoff_at`) — then a six-clause check in Python maps the drop to a
  reason ∈ {`terminated`, `causality`, `ceiling`, `staleness`, `band_overshot`(+leg), `abandoned`}.
  Projections do NOT carry these columns (C3-verified); the probe fires only on the rare reap tick.

**No handler-attribution special-case (revised — drop C2 as mechanism).** The LOAD/EXECUTE skew
forbids it: `current` is captured at `:1948`, but `FireDropoff` runs at `:2344` — so a dropped
offer stays in `current` *this* tick and only leaves the live set at the *next* tick's LOAD, by
which point the fire is no longer in `executed_actions`. Handler-signal attribution would read the
wrong tick. The probe reads the now-set `actual_dropoff_at` / `expected_odometer_status` directly,
so `terminated` (clause 1) and `abandoned` (clause 6) map with **zero cross-tick state**. Keep the
emergent/handler witness-split as *analysis*; handler⇄diff reconciliation is a Phase-2 enrichment,
not built now. All six reasons mirror the predicate, incl. live-impossible `causality` (C1).

**Empty-probe fallback (poison-pill defense):** if the probe returns no row for a left id, emit
`offer_left_queue(reason='unprobeable')` AND advance the seed past it. This is NOT the async-write
race Gemini posited (a reap's `offer_history` row is never deleted — C3 — rows are append-mostly);
it's defense against a stuck seed: an unhandled empty result under batch-swallow would re-revert the
seed every tick → the seed gets **permanently stuck** re-diffing the same id forever. `unprobeable`
+ seed-advance breaks that. (So: six real reasons + one defensive `unprobeable`.)

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
`now − last_keyframe_at ≥ 15 min` (both load-bearing).
- **Reset to 0 on keyframe — NOT mod-50 (reject Gemini).** A keyframe is an end-of-tick FULL
  snapshot; it anchors the burst's overflow events via its own timestamp, so the post-keyframe delta
  chain is genuinely zero. Reset `keyframe_count := 0` and `last_keyframe_at := now`. The overflow
  test asserts `keyframe_count == 0` (not `3`).
- **Keyframe is ordered LAST in the batch** and built from **end-of-tick** state — so its snapshot
  reflects everything that happened this tick and the next chain truly starts from zero.
- **Cost named (eyes open):** on emitting ticks this is a SECOND `driver_trip_state` write (the
  `:1978` authoritative write + the batch `ledger_state` write). That second write is the
  C4-correctness price already paid for the seed-advance (it MUST be in the batch, not `:1978`, to
  avoid silent-reap-loss) — so the counter is genuinely free, riding a write that must happen anyway.
  Non-emitting ticks do only `:1978`. **Both writes are HOT-eligible** (verified: `driver_trip_state`
  has only the PK index on `driver_id`; neither write touches it). **Forward-looking ops note (not a
  Phase-1 blocker):** `ALTER TABLE app_private.driver_trip_state SET (fillfactor=80)` is clean upside
  whenever applied (leaves page room for HOT chains). If a HOT test is written, assert
  `n_tup_hot_upd` RISES — not `n_dead_tup` low (vacuous at single-driver load).
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
5. **Empty-probe poison-pill (replaces Gemini's write-visibility test, which is dropped):** force a
   left id whose probe returns no row → assert `offer_left_queue(reason='unprobeable')` is emitted
   AND the seed advances past it (no permanently-stuck seed). (Gemini's async-write-visibility
   framing is moot — C3: a reap's row is never deleted.)
6. **HOT-update (optional, forward-looking):** if written, assert `pg_stat_user_tables.n_tup_hot_upd`
   for `driver_trip_state` RISES across the two per-tick writes — NOT `n_dead_tup` low (vacuous at
   single-driver load).

## 9. Open / deferred

- **§XVI.G `suppressed_contexts`** — leave open, but **lean to a first-class `lock_suppressed`
  event**. Gemini's keep-in-payload is defensible (one event, no temporal join), but it cuts against
  the founding rationale: "show me every suppression" should be `WHERE event_type='lock_suppressed'`,
  not a jsonb scan across `matcher_eval`. Additive either way; decide at impl.
- Event `summary` text wording per type (human timeline lines) — cosmetic, settle in impl.

## 10. Scope guards (unchanged)

Does not touch legacy writers (Phase 2). Does not read the ledger from any runtime decision path
(§VIII). Does not store `app_verdict` anywhere (§XV). Does not make the Fix #3 product call.
