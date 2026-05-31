# RECON — Lost-Mode Cold-Start Trap (current_offer_id bind paths audit)

**Authored:** 2026-05-31 (post-shift, against revision `puddlejumper-api-00636-4hp`)
**Scope:** Exhaustive audit of every production write path to
`current_offer_id` / `driver_trip_state.current_offer_id`. Confirm or
falsify the claim that the only bind path is the lost-mode-gated
`FirePickup`, making lost-mode a cold-start trap.
**Mode:** INVESTIGATION ONLY. No code changes, no fix proposed.

---

## §1 Findings — every write site

### §1.1 Production write sites (current_offer_id)

| File | Line | Operation | Method | Production caller(s) |
|---|---|---|---|---|
| `driver_queue.py` | 473–477 | `SET current_offer_id = %s` (bind to value) | `DriverQueue.bind(offer_id, cur)` | `driver_heartbeat.py:278`, `driver_heartbeat.py:300` |
| `driver_queue.py` | 506–512 | `SET current_offer_id = NULL, heartbeat = NULL, heartbeat_at = NULL` | `DriverQueue.unbind(cur, clear_heartbeat=True)` | (test reset only — `clear_heartbeat=True` per docstring 491; no production call) |
| `driver_queue.py` | 514–518 | `SET current_offer_id = NULL` (clear) | `DriverQueue.unbind(cur)` | `driver_heartbeat.py:431` (FireDropoff), `driver_heartbeat.py:717` (ClearNarrative) |

### §1.2 Test-only write sites (current_offer_id)

| File | Line | Operation | Method | Caller(s) |
|---|---|---|---|---|
| `driver_queue.py` | 542–547 | `INSERT … ON CONFLICT (driver_id) DO UPDATE SET current_offer_id = …` | `DriverQueue.force_bind(offer_id, cur)` | `tests/test_driver_queue.py:550, 564` — explicitly marked "Production code paths use bind()" (driver_queue.py:533) |
| `test_endpoints.py` | 131–136 | `INSERT … ON CONFLICT (driver_id) DO UPDATE SET current_offer_id = …` | `/api/v1/test/seed_offer` w/ `set_active=true` | Gated by `_require_test_driver` → `TEST_ALLOWLIST` 403 (test_endpoints.py:40–43); cannot fire for non-allowlisted production drivers |

### §1.3 driver_trip_state writes that do NOT touch current_offer_id (verified, not bind paths)

| File | Line | Fields written |
|---|---|---|
| `driver_heartbeat.py` | 1585 | `heartbeat`, `heartbeat_at`, `last_odometer_move_at` |
| `driver_heartbeat.py` | 2008 | `last_voiced_offer_id`, `last_voiced_action_type` |
| `nail_it_core.py` | 166 | `nailed_pickup_lat/lng/error_m` or `nailed_dropoff_*` |

---

## §2 Findings — every caller of `queue.bind()`

**Two production callers exist.** Both are inside the same `if
isinstance(action, FirePickup):` block in
`driver_heartbeat.py::_execute_action`:

| File | Line | Branch | Context |
|---|---|---|---|
| `driver_heartbeat.py` | 278 | Rule XV catch-up branch (added tonight, commit `1d1133f`) | Fires only when `existing_actual_pickup_at IS NOT NULL` — i.e., a prior Observation already populated the cache. queue.bind runs to catch the narrative up. Reachable only if FirePickup itself executes. |
| `driver_heartbeat.py` | 300 | Normal first-fire path | The original bind point. Reachable only if FirePickup itself executes. |

**Both paths require the dispatcher to have emitted a `FirePickup`
action.** If the action is `FirePickupObservation`, neither bind runs.

---

## §3 Finding — the only bind path is `FirePickup`, gated by `lost_mode`

### §3.1 Express lane gate (driver_heartbeat.py:1814)

```python
if leg == 'pickup':
    action_cls = FirePickupObservation if lost_mode else FirePickup
```

When `lost_mode` is True, the single-match express lane emits
`FirePickupObservation` instead of `FirePickup`. The handler for
`FirePickupObservation` (driver_heartbeat.py:449–559) does **NOT** call
`queue.bind` — it writes cache fields, acquires the §XVI.G lock, but
leaves `current_offer_id` untouched. The comment at line 541 says so
explicitly:

> `# queue.bind. The winner's FirePickup already set current_offer_id;`

Implication: an Observation is *defined* as the narrative-blind fire.
It never binds.

### §3.2 §5.3 ambiguity gate (driver_heartbeat.py:1833)

```python
matcher_actions = dispatch(synth, current_offer_id, queue_metadata, lost_mode=lost_mode)
```

`dispatch.py::_demote_to_observation` (dispatch.py:218–243) is the pure
function applied in lost-mode:

```python
if isinstance(action, FirePickup):
    return FirePickupObservation(action.offer_id)
if isinstance(action, FireDropoff):
    return FireDropoffObservation(action.offer_id)
return action
```

When `lost_mode=True` reaches `dispatch()`, every `FirePickup` produced
by §5.3 logic is rewritten to `FirePickupObservation` before return.
Same outcome as the express lane: no `FirePickup` survives demotion,
therefore no bind.

### §3.3 No third gate exists

`queue.bind` is called from exactly two file:line positions (§2). Both
are inside `if isinstance(action, FirePickup):`. There is no other
production code path that constructs a `FirePickup` independent of
these gates, and no production code path that writes
`current_offer_id` to a non-NULL value outside of `bind()` /
`force_bind()`.

---

## §4 Finding — no bind-on-accept path exists in production

### §4.1 Direct search

Grep for `accept_offer`, `on_accept`, `verdict.*ACCEPT`, etc., across
the production tree returns hits in:

- `decisions/triangulation_enricher.py:147–153` — uses `verdict ==
  "ACCEPT"` to gate scorer cache enrichment (Scorer B polyline fetch).
  Does NOT touch `current_offer_id`.
- `drive_review.py:148–218` — reporting/stats. Does NOT touch
  `current_offer_id`.
- `market_intelligence.py:117–255` — analytics. Does NOT touch
  `current_offer_id`.
- `bead_on_wire.py:661` — comment string. Does NOT touch
  `current_offer_id`.
- `tad.py:329`, `pudo_types.py:90`, `where_am_i.py:2052`,
  `driver_queue.py:125` — all reference `accepted_at` as a time
  anchor / dataclass field. Do NOT touch `current_offer_id`.
- `nail_it_core.py:181–197` — `get_next_stacked_offer` joins on
  `current_offer_id` for STACKED-swap lookups, but READS only; no
  writes.

No production HTTP endpoint, no webhook handler, no offer-acceptance
flow writes `current_offer_id`. **Confirmed: there is no
bind-on-accept path.**

### §4.2 Rule XVI consistency

This absence is consistent with Rule XVI (canonical rules dump,
2026-05-30): *"Declined offers and accepted offers are ONLY advice to
the driver, they have zero bearing on the behavior of the PUDO
engine."* A bind-on-accept path would couple PUDO state to driver-
advice state, violating Rule XVI. So the design intent is correct;
the consequence is that no path can perform the *first* bind from a
cold start.

---

## §5 Finding — `lost_mode` is detected from bit 2 alone, NOT bit 1 AND bit 2

`_detect_lost_mode` (driver_heartbeat.py:905–949) docstring says:

> bit 1: current_offer_id IS NULL    (checked by caller)
> bit 2: queue contains an offer with actual_pickup_at IS NULL
>        AND predicate-alive (this function returns this bit)
> Returns True iff bit 2 holds — at least one queued offer is
> predicate-alive AND has no pickup observation recorded.

The caller at driver_heartbeat.py:1700:

```python
lost_mode = _detect_lost_mode(cur, driver_id, queue_ids_int,
                              cumulative_miles, _heartbeat_now,
                              effective_last_move)
```

**Direct assignment from the function's return.** No AND with
`current_offer_id is None`. The local `current_offer_id` is available
(read at line 1553 from `snap.bound_offer_id`) but is not used to
gate `lost_mode`. There is no other lost-mode computation between
line 1700 and the gates at lines 1814/1833.

**Implication:** `lost_mode` is True whenever ANY queued offer has
`actual_pickup_at IS NULL` AND is predicate-alive, regardless of
whether `current_offer_id` is currently bound. The docstring's claim
that bit 1 is "checked by caller" is either stale or aspirational —
the caller does not check it.

Even if bit 1 *were* properly ANDed, the cold-start trap (§7) would
still hold; this finding only documents that the actual gate is
looser than §XVIII.A specifies.

---

## §6 Finding — Observations DO populate `actual_pickup_at`

For the lost-mode loop analysis to be complete, we need to confirm
whether `FirePickupObservation` clears bit 2 for its own offer (i.e.,
whether the queue eventually drains).

`driver_heartbeat.py:479–497` (FirePickupObservation handler):

```sql
UPDATE app_private.offer_history
SET ...
    actual_pickup_at = NOW(),
    ...
WHERE id = %s::bigint
  AND actual_pickup_at IS NULL
```

**Confirmed:** the Observation handler DOES write `actual_pickup_at`,
clearing bit 2 for the offer it fires on. So a single Observation
fires DOES NOT keep that one offer flagging bit 2.

But bit 2 is "ANY queued offer has `actual_pickup_at IS NULL`". A
backlog of unfired offers (offers the driver never physically arrested
on, but which remain predicate-alive within the 4-hour abandonment
window) keeps bit 2 True until each one either fires-as-Observation
(requires the driver to arrest at that geocode) or expires.

In a real shift with offers arriving continuously, the queue
typically contains multiple never-driven-to offers at any moment.
Bit 2 latches True for the duration.

---

## §7 The cold-start trap (logical proof)

Given §1–§6, the trap is:

1. **Initial state (cold start).** Driver opens app. `current_offer_id
   IS NULL`. Queue empty. Bit 2 False. `lost_mode` False. Trivially
   bindable, but nothing to bind.
2. **First offer arrives.** Queue now contains one alive offer with
   `actual_pickup_at IS NULL`. Bit 2 True. `lost_mode` True.
3. **Driver arrests on the offer's pickup.** Matcher fires. Express
   lane (§3.1) demotes to `FirePickupObservation`. Cache writes happen
   correctly. `current_offer_id` stays NULL.
4. **Bit 2 remains True** for any *other* unfired offer in the queue
   (the just-fired offer now has `actual_pickup_at`, but the rest
   don't). Even if there are no other offers, the NEXT arriving offer
   makes bit 2 True again before the driver's next pickup.
5. **Loop indefinitely.** Each pickup is fired as an Observation, no
   bind occurs, `current_offer_id` remains NULL. Lost-mode persists
   forever in production.

**Today's 5/31 shift data is the empirical confirmation of this
proof:** 10/10 fires all `match_signal = lost_mode_*`, 10/10
`current_offer_id_at_eval = NULL`, 10/10 actions Observation variants.
The trap fires deterministically.

The *only* paths out of lost-mode that exist:

- **Queue drains to zero alive unfired offers.** Requires every queued
  offer to either fire-as-Observation or hit the 4-hour abandonment
  ceiling. For a real shift, this happens rarely or never (new offers
  arrive faster than the backlog drains).
- **Manual bind via `force_bind` or test endpoint.** Test-only, not
  reachable in production for non-allowlisted drivers.
- **An undesigned binding path.** Doesn't exist.

There is no production path that exits lost-mode under normal driving
conditions.

---

## §8 Cross-check vs `SPRINT_HANDOFF_LOST_MODE_BIND_REGRESSION_2026-05-20.md`

The brief (commit `465587d`) anticipated this defect and ranked four
hypotheses. Today's findings update the rank:

| Brief hypothesis | Brief's prior | Findings verdict |
|---|---|---|
| **H1** — Backlog is the only blocker; binding resumes when it clears | MEDIUM-HIGH | **Falsified at production scale.** Backlog *can* drain under §6's mechanism, but in a continuously-offer-receiving shift, the drain window doesn't open. Today's 10 fires across a full shift had bit 2 True throughout. |
| **H2** — Lost-mode is intentional permanence for this driver/regime | MEDIUM | **Confirmed as the effective situation** (regardless of design intent). The cold-start trap (§7) means lost-mode is the steady-state operating regime for any driver receiving offers, full stop. Whether this was *designed* permanence (H2 framing) or unintentional permanence (the absence of a designed exit transition) is the same outcome from the driver's perspective. |
| **H3** — A designed lost-mode → narrative transition is missing wiring | MEDIUM-LOW | **Confirmed.** No production code path performs the transition. The brief speculated "may have been deferred" — these findings confirm it was never wired. There is no exit path. |
| **H4** — Detector predicate is overinclusive (filter by ACCEPT?) | LOW | **Falsified under Rule XVI.** Filtering by ACCEPT/DECLINE would couple PUDO state to driver-advice state, which Rule XVI forbids. The H4 fix as the brief proposed it is not principled. The predicate may be overinclusive in some *other* sense, but ACCEPT-filtering is not the right axis. |

**Net update:** H3 (missing wiring) is the correct architectural
diagnosis; H2 (effective permanence) is the current production
behavior. The brief's H1 hope (backlog drain) was reasonable when
authored (2026-05-20, against a then-active 7-offer backlog) but
doesn't survive a continuously-offered shift.

No regression has occurred between the brief's authoring date and
today; the situation has been static. What changed today is:
- The brief was written against an isolated backlog (7 stuck offers).
- Today's shift demonstrated the trap is structural — even a "fresh"
  shift with no leftover backlog falls into permanent lost-mode the
  moment the first offer arrives.

---

## §9 Open questions for Gemini

These are NOT fix proposals — they are scoping questions the next
sprint must answer before any code is written:

1. **Does §XVIII intend permanent lost-mode** for any driver with an
   active offer queue, with the consequence that the monitor signal
   must move off `current_offer_id` entirely? If yes, the fix is on
   the consumer side (rewire monitor alarm to observation-fire events,
   plus any other narrative-state consumer); the binding logic stays
   as-is. If no, §10's design question is live.
2. **If a binding path is wanted, what does it bind on?** Rule XVI
   forbids binding on offer-accept. The remaining candidates: the
   first Observation-fire after lost-mode latches (with rules for
   ambiguity), an explicit driver gesture (button, but §VI forbids
   that), or a heuristic on the queue's narrative coherence (single-
   alive-offer + observation = narrative is unambiguous, bind it).
   Each has trade-offs the user must ratify before code.
3. **Should bit 1 actually be checked?** §XVIII.A spec says bit 1 AND
   bit 2, but the caller checks bit 2 only (§5). Either bring the
   code into compliance with the spec, or update the spec to match
   the code. Either way, document the resolution. (This is
   independent of §7's cold-start trap, which holds either way.)

---

## §10 Verification artifact

Forensic confirmation against revision `00636-4hp` for today's shift,
driver `UjT1hE9eBXh2q95aSZYOkzDJ8lo1`:

```sql
SELECT
  to_char(created_at AT TIME ZONE 'America/Chicago', 'HH24:MI:SS') AS ct,
  matched_offer_id, planner_action,
  match_signal, current_offer_id_at_eval
FROM app_private.pudo_decision_context
WHERE driver_id = 'UjT1hE9eBXh2q95aSZYOkzDJ8lo1'
  AND created_at >= '2026-05-31 00:00:00+00'::timestamptz
  AND matched_offer_id IS NOT NULL
ORDER BY created_at;
```

The user's stated observation is that this returns 10 rows, all with
`current_offer_id_at_eval = NULL`, `match_signal LIKE 'lost_mode_%'`,
`planner_action LIKE '%Observation%'`. This is exactly what §7
predicts and §8's revised hypothesis-ranking expects.

---

**End of findings. No code changes. No fix proposed. Hand to Gemini.**
