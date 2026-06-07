# Event-Ledger Design — Correction Delta (post-recon, pre-ratify)

> **Ratification note (Claude, 2026-06-07):** the three load-bearing citations were re-verified
> against source before recording (not taken on the delta's plausibility, per its own instruction):
> - **C1** — `LIVE_OFFER_PREDICATE_SQL` has **six** top-level AND-ed clauses, confirmed verbatim at
>   `driver_queue.py:193–265` (clause 1 `actual_dropoff_at IS NULL` @193; causality @199; ceiling
>   @208; staleness @216–220; band w/ leg-CASE @240–258; abandoned @265). The original design's
>   "5 clauses → 4 buckets" was wrong twice (omitted clause 1, merged causality+ceiling). Causality's
>   live-impossibility is stated in the source comment @194–198. **Delta correct.**
> - **C3** — `offer_ids_only` selects `id::text` only (`driver_queue.py:745`); `_project_offers`
>   selects exactly 14 columns (`:899–907`), **none** of the six attribution columns. Gemini's
>   "data already in memory" premise is false against source. **Delta correct; Option B stands.**
> - **C4** — single terminal `conn.commit()` @`driver_heartbeat.py:2406`; the handler @`2401–2404`
>   swallows (log, no re-raise, no rollback). Single-connection txn shape confirmed. **Delta correct.**
> - **C7/Q3 MVCC** — confirmed by Postgres semantics: any `UPDATE` writes a whole new row tuple, so
>   fewer columns yield no MVCC saving; the delta's correction of Gemini's justification is right.
> - **Carried-open #2 closed:** `peak_confidence`/`decay_samples` have zero writers repo-wide;
>   `phase_reached` is only ever written `None` (write-frozen). The three are confirmed dead globally.

**Status:** DELTA against `docs/DESIGN_EVENT_LEDGER_2026-06-07.md`. Read alongside it; this
supersedes the design's §8 open questions and the consolidated Gemini review where they conflict.
Every correction below is backed by a `file:line` citation from the completed recon — ratify each
against the cited source, not against this document's plausibility.

**Recon basis (all four targets closed against source):**
- Writer inventory: `RECON_EVENT_LEDGER_WRITER_INVENTORY_2026-06-07.md` (1 dead writer, 1 stays, 1 superset target).
- Predicate body: `driver_queue.py:192–265` (verbatim, read this sprint).
- Live-set projections: `driver_queue.py:730` (`offer_ids_only`) and `:876` (`_project_offers`).
- Heartbeat txn shape: `driver_heartbeat.py:1890–2453` (single connection, single terminal commit).

---

## C1 — Reap-reason taxonomy: SIX reasons, not four

**Design §4 says** "5 predicate clauses → 4 reason buckets," merging causality+ceiling into one
"199/208" bucket and omitting the `actual_dropoff_at` clause. **Gemini's Clause Mapping Test**
enumerates four states (staleness, band, ceiling, abandonment) — separates ceiling but drops
causality *and* terminated. **Both are wrong, in opposite directions** — the tell that neither was
checked against the predicate body.

The verbatim predicate (`driver_queue.py:192–265`) has **six** top-level AND-ed clauses:

| # | clause (verbatim source) | reason code | added |
|---|---|---|---|
| 1 | `oh.actual_dropoff_at IS NULL` | `terminated` | — |
| 2 | `oh.created_at <= %s::timestamptz` | `causality` | Causality Guard 2026-05-12 |
| 3 | `oh.created_at > %s::timestamptz - INTERVAL '%s hours'` | `ceiling` | Abandonment Ceiling 2026-05-19 (4h) |
| 4 | `NOT ( %s IS NOT NULL AND %s < %s - INTERVAL '%s minutes' AND oh.created_at <= %s - INTERVAL '%s minutes' )` | `staleness` | Odometer Staleness Gate 2026-05-19 |
| 5 | `( %s::numeric IS NULL OR CASE … upper-edge band … END )` | `band_overshot` (+ `leg`) | Step 6 ERRATUM 2026-06-05 |
| 6 | `oh.expected_odometer_status IS DISTINCT FROM 'abandoned'` | `abandoned` | §9.9.2 2026-06-05 |

**Why the causality/ceiling merge is the costly error specifically:** clauses 2 and 3 are
opposite-direction bounds on the *same column* (`created_at` too-new vs too-old). They mean
maximally different things — *"a future offer leaked into the live set; the replay seed or clock is
broken"* (causality) vs *"offer aged out at the 4h cap; routine expiry"* (ceiling). Collapsing them
into one reason destroys the exact distinction the gold event exists to preserve. The reason code IS
the value-add of capturing an emergent reap; a merged code is a half-captured event.

**`band_overshot` carries a `leg` payload qualifier** (pickup vs dropoff — clause 5's CASE branches
on `actual_pickup_at IS NULL`), not two separate reason codes. Keeps the taxonomy at six while
preserving the leg story.

**Retain `causality` even though it is live-impossible.** Clause 2's own comment states production
wall-clock is always ≥ `created_at`, so this clause never flips false in live operation. Do **not**
drop it from the diagnostic. The diagnostic must mirror all six clauses so a clock-skew or
replay-seed bug is *captured* rather than silently misattributed to the next clause down.
"Can't fire in production" is precisely the assumption class the founding forensic disproved.

---

## C2 — Witness class: 4 emergent / 2 handler-attributable (design drew the line in the wrong place)

Design §4 treats the emergent/non-emergent split as living inside the `created_at` clauses. The real
split is by *who else knows the offer died*:

- **Emergent (the queue-diff is the SOLE witness):** `causality`, `ceiling`, `staleness`,
  `band_overshot`. No upstream handler fires; the offer silently stops satisfying the predicate.
  These are the gold events — the diff is the only record.
- **Handler-attributable (an upstream handler already knows):** `terminated` (the dropoff-fire
  handler set `actual_dropoff_at`) and `abandoned` (the §9.9 dropoff-handler sweep flipped
  `expected_odometer_status`). For these two, attribute from the handler signal *this tick*, not by
  re-checking the clause against a row whose state already changed — and the diff is a corroborating
  cross-check, not the primary record.

Consequence for the emit architecture: `terminated` should be emitted by the dropoff path (it
already has the fact), and the diff's job for that id is to *reconcile* (assert the dropped id
matches a dropoff fired this tick), not to re-derive.

---

## C3 — Q2 resolved → Option B (scoped reap-only probe). Gemini's Flaw-3 premise is false against source.

Gemini's Flaw #3 fix says use an in-Python metadata check because "the variables required … are
already fetched and present in memory at the top of the heartbeat loop." **The actual SELECTs refute
this:**

- `offer_ids_only` (`driver_queue.py:730`) selects `id::text` and nothing else.
- `_project_offers` (`driver_queue.py:876`) selects 14 columns: ids, addresses, coords,
  `created_at`, `pickup_miles`, `trip_miles`, minutes, and the two `leg_start_cumulative_miles_*`.

**Neither projection carries** `expected_pickup_distance`, `expected_dropoff_distance`,
`expected_odometer_status`, `last_odometer_move_at`, `actual_pickup_at`, or `actual_dropoff_at`.
So an in-Python check against the in-memory set can evaluate at most clauses 2/3 (`created_at`) and
the band's `*_miles` width inputs — **three of six clauses (staleness, band center, abandoned) are
unattributable from memory.** `band_overshot` is among the lost ones and is, post-Step-6, one of the
likeliest live reaps.

Also note the diff runs on the *id set*, not on `_project_offers`. The cheapest live-set witness is
`offer_ids_only` (ids only); the prior set on `driver_trip_state.last_queue_snapshot` is ids+status.
At diff time the engine knows an id *left* and holds none of the metadata to say *why*. Attribution
is a distinct second act.

**Resolution — Option B (scoped probe), not projection-widening (Option A):**
- Keep both projections id-lean. When (and only when) the diff finds `left = prior − current`
  non-empty, fire **one** targeted query for just the dropped offer_ids fetching the six attribution
  columns, then run the six-clause check in Python.
- **Cost profile:** Option A pays a wider hot-path SELECT on *every* heartbeat for all live offers,
  of which only the (usually zero) dropped ones ever need the columns. Option B pays one extra query
  *only on the rare tick where an offer actually reaps*. Reaps are orders of magnitude rarer than
  heartbeats (band ERRATUM cites 0/448 NULL odometer in 30d as a reap-rarity proxy). B's inverse
  cost profile wins.
- **Bonus:** B avoids forking the two projections' column lists. `offer_ids_only` and
  `_project_offers` are WHERE-clause-pinned by `test_offer_ids_only_and_project_offers_share_where_clause`;
  widening only `_project_offers` forks them, widening both bloats the monitor path too.
- **Assumption B carries (→ live-PG test, see C6):** the probe reads `offer_history` *after* the id
  left the live set. The offer left the *predicate*, not the *table* — rows are append-mostly and
  the attribution columns (`created_at`, `expected_*`, status) are stable post-reap — so the row is
  still readable. Assert this, don't assume it.

Net: Gemini's *direction* (in-Python over a per-clause SQL storm) is kept; its *premise* (data
already in memory) is corrected; the in-Python check is fed by B's probe, not by the existing projection.

---

## C4 — Flaw #1 resolved → SAVEPOINT on the heartbeat connection. Gemini's "mandatory separate connection" is wrong here.

Gemini's observation is correct: a SAVEPOINT-released insert still lives in the parent txn and dies
on a downstream parent rollback. Its *prescription* (separate autocommit connection, "mandatory") is
wrong against the heartbeat's actual transaction shape (`driver_heartbeat.py:1890–2453`):

- One cursor on one connection (`:1909`, `RealDictCursor`, **no autocommit, no second connection**).
- **Exactly one** `conn.commit()`, terminal, at `:2406` (just before the response at `:2453`).
- All three writes — `_log_decision_context`, the authoritative `driver_trip_state`
  `UPDATE…RETURNING` (`:1979`), the `heartbeat_log` INSERT (`:2070`) — sit **before** that commit.
  No intermediate commit. The whole heartbeat is one atomic transaction.
- The only two exception handlers between the writes and the commit (`:2078`, `:2401`) **catch and
  swallow** (log-and-continue); there is **no bare `raise` and no `conn.rollback()`** on the path
  from the writes to `:2406`.

**Why separate-connection is actively wrong:** the diff-seed advance (the
`driver_trip_state.last_queue_snapshot` write the next tick's diff compares against) rides this same
txn at `:1979`. If reap-emit goes out on a separate autocommit connection while the seed-advance
stays in the heartbeat txn, a rollback after `:1979` *commits the emit but reverts the seed* → next
invocation reads the un-advanced seed, re-diffs, **re-emits the same reap.** Duplicate gold events
describing rolled-back state — direct corruption of the one event the ledger exists to record. Moving
*both* seed-advance and emit onto the side connection is worse: it relocates §VII source-of-truth
state (`driver_trip_state` is the §VIII Authoritative lane) onto an observability connection.

**Why SAVEPOINT is sufficient:** the only thing SAVEPOINT fails to guard is a rollback *after* emit
— and that path **does not exist** on this code (no uncaught raise, no rollback between writes and
commit; the swallow-and-continue handlers are the established §5.5 aborted-txn discipline). SAVEPOINT
protects the parent from a *ledger* failure (the real risk).

**Resolution (sharpened — ratification pass 1 surfaced a precision gap; see below):** the atomic
unit is **not the individual emit** — it is the *whole tick's ledger batch*: all emits for the tick
**plus the seed-advance**, inside **one** savepoint, on the heartbeat connection. No separate
connection.

**Why "same txn" is not enough — the silent-reap-loss path.** The design folds the seed-advance into
the authoritative `:1979` UPDATE (parent txn) and has `emit_event` swallow errors **per emit**. Trace
a ledger-internal failure (bad JSON payload) under that placement: the emit's own savepoint rolls
back, but the seed-advance at `:1979` already ran and commits with the parent at `:2406`. Now the
seed says "X left the queue" while the `offer_left_queue(reason)` explaining X's departure was never
recorded. Next tick diffs against the advanced seed, sees no change, **never re-emits** → the gold
event is *silently lost* (not duplicated — lost). Keyframes do **not** recover this: a keyframe
records current *membership*, not the *reason* X left; the reason is exactly what the delta carries
and the snapshot does not.

**Corrected mechanism (this replaces design §3's "emit swallows its own errors" per-emit model):**
- The seed-advance comes **out** of the `:1979` authoritative UPDATE and into the ledger savepoint,
  writing **only** the ledger-bookkeeping columns (`last_queue_snapshot`, `last_matcher_snapshot`,
  keyframe counter). The authoritative columns (`bound_offer_id`, arrest fields, heartbeat) stay in
  the `:1979` parent write, untouched by the savepoint.
- One savepoint per tick encloses **all emits + the seed-advance**. Batch succeeds → release →
  commits at `:2406`. Any emit fails → roll back to savepoint → **emits *and* seed-advance both
  revert** → parent's authoritative writes still commit (§VIII preserved) → next tick re-diffs
  against the un-advanced seed, re-detects the reap, **retries** (self-healing; no loss, and no
  duplicate because single-connection means nothing partial ever separately committed).
- The swallow therefore happens at the **batch level**, not per-emit. Per-emit swallow is precisely
  what reintroduces silent loss (emit A reverts while the seed advances past A).
- §VIII-safe: `last_queue_snapshot` is ledger bookkeeping — **no decision path reads it** — so
  binding its write to the ledger savepoint never touches live decision state. (Contrast the
  separate-connection failure mode below, which corrupts via the *opposite* mechanism.)

**Why separate-connection is still wrong (the duplicate, as opposed to the loss above):** if reap-
emit went out on a separate autocommit connection while the seed-advance rode the heartbeat txn, a
parent rollback after the seed-advance *commits the emit but reverts the seed* → next tick re-diffs
and **re-emits the same reap** → duplicate gold events describing rolled-back state. Single-connection
batched-savepoint avoids both the duplicate (this paragraph) and the silent loss (above).

**Guard (not architecture):** the "a future edit could add an uncaught raise between emit and commit"
objection is real. Answer with a §XIV.J **structural test** asserting `post_heartbeat` has no
uncaught `raise` and no `conn.rollback()` between the first `emit_event` and `conn.commit()` — pin
the property the way the predicate's single-home is pinned, rather than carrying a parallel
connection forever.

**Caveat stated with eyes open:** under SAVEPOINT, a genuine heartbeat abort (connection-level error,
statement timeout mid-txn — paths that don't exist today but aren't physically impossible) loses that
tick's emit along with everything else. Accepted: the **keyframe is the recovery mechanism** — a lost
delta re-anchors at the next keyframe (the reason deltas-AND-keyframes was locked over pure deltas).
This ledger is §VIII Passive/observability, explicitly *not* the crash-diagnostic of last resort;
buying Gemini's "emit survives parent death" guarantee costs the reap-uniqueness the gold event
depends on. Wrong trade for this table.

---

## C5 — Flaw #2 reframed: time-only is off the table; the real fork is counter-in-seed-write vs COUNT()-read

Gemini's option 1 ("strict time-only trigger, e.g. 12 min") **deletes the count-primary trigger**,
which is a locked decision (drops correlate with event density, not elapsed time; time-only
under-keyframes during exactly the bursts where integrity is at risk). Off the table — do not
re-litigate.

Gemini's option 2 (`COUNT(*)` since last keyframe at load) is workable but mischaracterized, and it
isn't the only non-mutable-counter option:

- **It is not O(1)** as claimed — it's an index *range scan* on `(driver_id, event_time)`.
- **It pulls a synchronous `event_ledger` read into the runtime heartbeat path every tick** — a
  §VIII read-purity gray area. Defensible as self-referential cadence management (the runtime isn't
  reading a *decision* out of the ledger), but name it, don't wave it through.

The option both Gemini and the design missed: **a DB-resident counter is free for queue/matcher
events**, because those events already write `driver_trip_state` to advance the diff-seed (C4). The
counter is just another column in a write that's already happening → write frequency stays
event-density-bound, MVCC guard (C/§VIII) holds. The counter costs an *extra* hot-row write only for
events that touch no seed (e.g. `pickup_detected`).

**C4-feedback (ratification pass 1):** the sharpened C4 moves the seed-advance into the ledger
savepoint and fires it only on **change/reap ticks**. So the "free counter" now covers only
change-tick events; an event on a *non-change* tick (a sensor event when nothing entered or left the
queue) still must bump the 0–49 counter toward the keyframe threshold but has **no seed-write to
ride**. This *weakens* the free-counter argument — it holds for change-tick events, not all events.
The fork's resolution therefore turns on the count of genuinely state-untouching §7 event types (see
the audit below).

**Real fork for ratification:** counter-folded-into-the-seed-write vs `COUNT(*)`-read-at-load,
decided on the sensor/PUDO event mix (how many events don't touch a seed) and on §VIII read-purity
strictness. Time-only is excluded.

**Audit strategy for the fork (answers Gemini's closing question — still carried-open pending the
grep):** classify each of the 17 §7 event types by whether its emit site coincides with a
`driver_trip_state` write. From the recon emit-site map: state-touching = queue-diff events,
`matcher_eval`, `bind`/`unbind`, and the arrest/cluster events sourced from the `:1979
UPDATE…RETURNING`; `pickup_detected`/`dropoff_detected` also coincide (they fire alongside
`bind`/`unbind`, which write `current_offer_id`). Suspected state-*untouching* = `cluster_formed`,
`cluster_dissolved`, `cadence_change`, and `keyframe` itself. Decision rule: if the untouching set is
≤2–3 rare sensor types, fold the counter in (the extra hot-row bump is negligible); if it includes a
hot path, go `COUNT()`-read with the §VIII gray-area caveat. Resolve by grepping each §7 event's emit
site for a same-tick `driver_trip_state` UPDATE — a count, not a judgment call.

---

## C6 — §XIV.J test floor: Gemini's Clause Mapping Test would false-certify; its forensic-survival test encodes the rejected design

Two of Gemini's test items carry its own errors forward and must be corrected, not just accepted:

1. **Clause Mapping Test — must assert all SIX reasons.** As written it enumerates four (staleness,
   band, ceiling, abandonment), dropping `causality` and `terminated`. A green test over four reasons
   would *certify* a diagnostic missing two reason codes — false confidence, worse than no test.
   Assert the queue-diff maps a drop to the exact clause value for **all six** of C1, including the
   live-impossible `causality` (synthesize a future-`created_at` / skewed-`reference_time` row to
   exercise it).
2. **"Seed-Emit Atomicity" Test — Gemini's connection-kill framing is vacuous; replace it.** Gemini's
   ratified version ("kill the connection immediately prior to `:2406`, assert neither seed nor ledger
   is modified") asserts only that Postgres is ACID — it exercises none of our emit/savepoint logic
   and is not deterministically reproducible in a harness. The test that actually proves the C4
   mechanism: force an emit failure *inside the tick's ledger batch* (e.g. an invalid payload to the
   INSERT), then assert (a) the parent heartbeat **commits** with its authoritative writes intact and
   the failed batch absent; (b) `last_queue_snapshot` did **not** advance for the reaped ids (so the
   reap is re-detectable); and (c) a re-run of the tick re-emits the reap **exactly once**. This
   guards against the silent-reap-loss path C4 surfaced — which the connection-kill test would mask.

**Keep as-is:**
- **Privilege-Hardening Test** — `UPDATE`/`DELETE` on `event_ledger` as the app role raises
  `insufficient_privilege`. Directly validates the append-only keystone.

**Add:**
- **No-uncaught-raise structural test** (the C4 guard).
- **Post-reap-readability test** (the C3/Option-B assumption): an offer that has left the predicate
  is still SELECT-able from `offer_history` with its six attribution columns intact.

---

## C7 — Open-questions table corrections (Q3, Q4, Q5 stand; Q3's *justification* is wrong)

- **Q3 (diff-seed location):** Gemini's pick of a single `ledger_state` jsonb is fine, but its stated
  reason — "prevents column pollution on a hot row" / MVCC — is **wrong**. Postgres rewrites the
  entire row tuple on *any* `UPDATE`, whether the change is one jsonb or three columns; there is no
  MVCC saving from fewer columns. If one jsonb is chosen, choose it on schema-flexibility grounds
  (one place to evolve the snapshot shape), not MVCC. **And** it interacts with C5: if the keyframe
  counter is folded into the seed write, it lives in this same jsonb/column set.
- **Q4 (snapshot vs delta), Q5 (partition tooling):** Gemini's picks (separate columns; scheduled
  job over pg_partman) stand. Flaw #4's dead-man's-switch metric on the partition-drop job is kept —
  it concretizes the handoff's own "the lifecycle job needs failure-visibility" note.

---

## Carried open (genuinely unresolved — do NOT let ratification paper these over)

1. **C5 fork** — counter-in-seed-write vs `COUNT()`-read. Needs the Phase-1 event-type mix to decide
   how many events touch no seed.
2. ~~**Recon residual from the inventory**~~ — **CLOSED (Claude, 2026-06-07):** widened grep confirms
   `peak_confidence`/`decay_samples` have **zero writers repo-wide**; `phase_reached` is only ever
   written `None` (write-frozen §XVI.C). All three confirmed dead globally — an empty grep, run, not
   assumed.
3. **§XVI.G `suppressed_contexts`** — confirm it becomes a first-class `lock_suppressed` event vs
   staying inside the `tad_decision_context` blob (inventory flagged, unresolved).

## Unchanged scope guards (from design §9, restated so they survive the edit)

Does not touch legacy writers (Phase 2). Does not make the Fix #3 product call. Does not read the
ledger from any runtime decision path (§VIII). Does not store `app_verdict` as a gating field (§XV) —
and per C1, accept/decline is verdict metadata on the offer, **never** a reap reason or ledger event.
