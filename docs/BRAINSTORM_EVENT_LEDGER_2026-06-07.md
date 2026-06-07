# Brainstorm — Event-Tracker Ledger (the per-driver perception-and-decision ledger)

**Status:** BRAINSTORM / pre-design notes. No code, no schema committed. This is the
shared record of what was decided in the brainstorm and what is still open, so CC can
pick it up without re-deriving the reasoning. Decisions here still go through the
normal recon → design → Gemini-ratify → build loop before anything lands.

**Authors:** Andrew + Gemini + Claude (2026-06-07 brainstorm).

**Origin:** the 2026-06-04 38-tap forensic took ~a dozen queries and four refuted
conclusions before landing on the real root cause ("the offer wasn't in the live
queue") — and that root cause was the one event nobody was logging. The historical
queue was *impossible to reconstruct* because the state it derives from mutates
(offer_history backfills, statuses flip deferred→active→abandoned, band params
change). This table exists so that question is a single timeline read.

**The litmus test for the whole design:** *can you reconstruct a driver's entire PUDO
story for any time window in one query?* If yes, it's built right.

---

## The three non-negotiable invariants (must survive design intact)

1. **Change/decision-only resolution, WITH periodic keyframes** — deltas for
   resolution, sparse full snapshots for integrity. Pure deltas are correct on volume
   but *silently corruptible* under best-effort logging; keyframes are the self-healing
   anchors. Deltas AND keyframes, not deltas XOR snapshots. (§3a, §4)
2. **"Change" includes emergent predicate reaps** — diff the queue state on each
   evaluation and log silent drops with the *causing clause* (band overshoot /
   staleness / causality). The reap is the **gold event** and it has **no
   decision-point** in code (it's emergent from `LIVE_OFFER_PREDICATE_SQL`), so
   "log on decision" alone misses it. (§3b)
3. **Append-only, never backfill — enforced at the schema level** — a later
   confirmation is a NEW event, never a mutation of a past row. Mutating a past event
   recreates `offer_history`'s un-replayability. Enforce by denying UPDATE/DELETE
   grants to the app role, not by convention. This is the keystone property. (§2.5)

---

## 1. What it is (the mental model)

An **immutable perception-and-decision ledger**, per driver. Not a telemetry dump,
not a heartbeat duplicate.

- **State tables** (`offer_history`, `driver_trip_state`) = mutable *current* truth.
  They answer "what is true now" and they rewrite history (the backfill). That
  rewriting is exactly why they can't be replayed.
- **The ledger** = what the system *perceived and decided* at each instant, frozen.
  The live queue is the system's perception of available work; recording it per event
  is recording perception. Decisions (fire / abstain / reap) are recorded against the
  perception that produced them.

It is **observability, never a source of truth** (§VII discipline). The
predicate/state tables stay authoritative. No consumer reads state from the ledger.
The moment something reads a *decision* from the ledger instead of from the
authoritative tables, it has become a parallel state machine and §VII is violated.

§0 trace: the ledger does not directly record pickup observations into the pricing
cache — it is pure observability, so by §0.D.5 it is a *means to the end*. The trace:
it makes the system's misses investigable, which is how we find and fix the misses
that cost pickup observations. It earns its place as the §V flight-recorder culture,
extended — and only as that.

---

## 2. DECIDED (in the brainstorm)

These were settled by Andrew during the brainstorm. Recorded as decisions, still
subject to the ratify loop before build.

1. **Build it**, and build it **before Fix #3's geocode-free redesign.** The ledger
   turns Fix #3's verification from cross-table archaeology into a single timeline
   query, and turns the next real drive into a self-validating regression (the
   queue-per-event snapshot directly answers "was the offer in the queue at the
   PUDO" — the exact thing that was impossible in the 38-tap forensic). The ledger
   replaces the shaky replay path, not just helps future-driver onboarding.

2. **Consolidate the existing logs into it**, don't sit beside them.
   `pudo_decision_context` becomes `matcher_eval` events; `driver_trip_state_log`
   becomes `state_transition` events; the `DRIVE_START_MARKER` becomes just another
   event type (markers stop being special). This is a **migration, not a greenfield
   table** (see §5/§7).

3. **Record the live driver queue on every event.** This is the sharpest decision
   in the thread and the thing that is *currently impossible to reconstruct*. Per
   event, snapshot the queue as a compact structure, not just the id set:
   `[{offer_id, status (active/deferred/abandoned), in_band?, picked_up?}]` plus the
   bound `current_offer_id`. That structure would have answered the entire 38-tap
   forensic in one row.

4. **Resolution = change/decision only, NOT full per-heartbeat snapshots.** Full
   snapshots on every event are wasteful (a 0–10-offer array × ~15k heartbeats/day is
   redundant). Log when something *changed or was decided*. **BUT** see §3 — this
   decision has two sharp edges that must be designed for, or it recreates the
   un-replayability the ledger exists to escape.

5. **Append-only, never backfill — enforced at the schema level.** When a pickup is
   later confirmed, that is a NEW event ("pickup detected at T2"), never an UPDATE to
   the T1 row. The moment you mutate a past event you have recreated `offer_history`'s
   un-replayability. Enforce by denying UPDATE/DELETE grants on the table to the app
   role — not by developer convention (convention erodes). This is the keystone
   property; enforce it hardest.

---

## 3. The two sharp edges of "change/decision only" (MUST design for)

"Log only on change" is a delta scheme, and delta schemes have two failure modes that
must be designed out *now*, in the design, or the ledger silently becomes an engine of
confident misinformation.

### 3a. Keyframes are NOT optional under best-effort logging

The ledger must be best-effort / non-blocking in the hot path (a logging failure must
never break a live decision — same as `heartbeat_log` / `pudo_decision_context`
today). Best-effort + pure-deltas = **silently corruptible history**: if an
`offer_left_queue` delta drops under load, the gap between two logged events silently
contains an unlogged change, and reconstruction reads the stale prior state as truth —
with false confidence, because "the ledger says so." That is the exact false
conclusion the ledger exists to prevent.

**Mitigation: periodic keyframes** — sparse FULL queue snapshots that fire regardless
of change, as self-healing anchors. A dropped delta then corrupts at most one
keyframe-interval instead of the whole timeline forward, and reconstruction has a
known-good anchor to fold deltas from. This is NOT "full snapshots per event" (which
was correctly rejected as wasteful) — it is sparse integrity anchors. The design is
**deltas for resolution, keyframes for integrity**, not deltas XOR snapshots.

### 3b. "Change" must include EMERGENT predicate reaps, not just explicit decisions

The most important event class — an offer *leaving the live set* — is the one we were
blind to in the forensic, and it is usually NOT a "decision." Reaping is emergent from
`LIVE_OFFER_PREDICATE_SQL` re-evaluating to false (band overshoot, staleness gate,
causality guard) — there is no code line that "decides" to reap; the predicate just
stops returning the offer on the next read (§XIV.H: "reaping is emergent, nothing
writes a dead flag"). So "log on decision" *misses the reap* — the gold event.

**Mitigation: log on observed change in derived state, not just explicit decisions.**
On each queue evaluation, diff the current predicate output against the last logged
queue state; when an offer silently drops, run a single-row clause-diagnostic to
determine *which* clause flipped (band overshoot / staleness / causality) and emit
`offer_left_queue(reason=<clause>)`. That turns the invisible database omission into a
positive, categorized tracking event. The `offer_left_queue(reason=...)` event is the
availability root cause we were blind to — it is the gold.

**Cloud Run caveat (important):** the heartbeat is stateless-per-invocation on Cloud
Run. The "last known queue set" for the diff CANNOT be process memory (it resets every
invocation). It must be the **last persisted queue state read from the ledger** at the
top of the heartbeat. So the diff is "current predicate output vs. last *persisted*
queue snapshot," and it costs one indexed ledger read per heartbeat (bounded, fine).
Design it as a persisted-state-read, not an in-process diff.

---

## 4. DECIDED — keyframe trigger: hybrid `50 events OR 15 min`, both load-bearing

**Resolved (Andrew + Gemini, 2026-06-07): keyframe on `50 events` OR `15 minutes of
active drive`, whichever fires first. Both triggers are load-bearing — they defend
against different failure modes:**
- **Count (N = 50):** bounds the corruption window during churn. In a rush-hour surge
  the queue mutates every few heartbeats; a time-only gate would let the delta chain
  grow long, so an early dropped write leaks a big block of history. 50 events caps it.
- **Time (M = 15 min):** catches the dead-zone case. On a long silent highway haul the
  event stream goes quiet; a time keyframe drops an anchor confirming "nothing changed"
  is an *empirical observation*, not an unlogged outage.

`N`/`M` are starting values — tune `N` from real event-density recon (loose enough that
keyframes aren't constant, tight enough a burst can't run long unanchored); `M` just
needs to catch long-quiet stretches.

(Original recommendation reasoning, retained — it's why the count trigger is the
integrity-load-bearing one:) keyframes defend
against delta-drop, and drops correlate with **event density** (peak churn, lock
contention), not elapsed time. The corruption window should be bounded in the unit
that *causes* corruption (events), not in minutes (uncorrelated with drop-risk during
bursts). A time-primary scheme can let many minutes of dense churn — peak drop risk —
run with no keyframe, exactly the window where corruption is most likely and least
bounded. So:

- **Primary:** keyframe every **N events** (integrity anchor; bounds corruption in the
  unit that causes it).
- **Secondary floor:** keyframe every **M minutes of active drive** (catches the
  long-silence case so a quiet stretch still drops a "nothing changed, confirmed"
  anchor).

Tunables: `N` by expected peak event density (loose enough that keyframes aren't
constant, tight enough that a burst can't run long unanchored); `M` just needs to
catch long-quiet stretches.

**Sharper option for later (probably over-engineering for v1, flagged for the
principle):** keyframe-on-burst-boundary — drop a keyframe whenever you EXIT a
high-density window (cluster dissolved, arrest ended, churn settled), putting the
integrity anchor exactly where drop-risk was highest. Event-count is a proxy for this;
burst-boundary is the direct hit. The principle is "anchor the keyframe to where drops
happen." Event-count-primary is the pragmatic v1.

> Note: Gemini ratified the design but framed the keyframe as time-primary ("every 15
> min of active drive... confirm nothing changed"). That optimizes for the wrong risk
> (confirming silence is cheap and low-value; the keyframe you NEED is during churn).
> The correction is event-count-primary. Recorded so CC doesn't inherit the
> time-primary framing.

---

## 5. Event taxonomy (working draft, not final)

Group the event types:

- **Offer lifecycle:** received → accepted/declined → entered-live-queue →
  reaped/excluded (with reason — §3b, the gold) → deferred (§5.5) → abandoned (§9.9) →
  resurrected.
- **Sensor / physical:** cluster formed/dissolved, arrest start/end, cadence
  transition (0.2 ↔ 1 Hz).
- **Matcher:** evaluation per offer (signals + confidence + verdict), match / abstain
  / below-floor, each with the reason. Logged at **meaningful inflections** (floor
  cross, top-candidate change, fire/abstain), not every per-heartbeat eval — the
  inflection IS the change worth capturing.
- **PUDO:** dispatch fired (pickup/dropoff, observation vs bind), bind/unbind.
- **Ground truth (debug):** the manual contest_labels tap, folded in as an event type
  so taps and detections sit on ONE timeline. (contest_labels has no offer_id today —
  the lack of tap→offer linkage is what made attribution hell in the forensic.)

---

## 6. Fields (working draft) — it's all about linkage

The reason attribution was hard is missing linkage (contest_labels has no offer_id).
Every event row carries:

- `driver_id`
- `event_time`
- `event_type`
- `offer_id` (nullable)
- `cluster_id` / `arrest_id` (nullable)
- **`lat` / `lng` (RAW, un-snapped device coords) + `gps_accuracy` + `cumulative_miles`**
  at the event — the un-derived sensor truth (see §6.1).
- `queue_snapshot` (the §2.3 compact structure: per-event for keyframes, delta for
  change events) + bound `current_offer_id`
- `matcher_snapshot` (`{top_candidate_offer_id, confidence_tier}`) — the persisted prior
  matcher state for inflection detection (see §6.2).
- `payload` jsonb (heterogeneous per type — signals/scores/reason; raw+snapped position
  on matcher events where a snap exists).
- `summary` text (human-readable, so support reads a timeline, not raw JSON)

Indexes: **`(driver_id, event_time DESC)`** (the lookback index — §6.2), `offer_id`,
`event_type`.

### 6.1 No `correlation_id` — driver+time is the spine (Gemini 2026-06-07, ratified)

The §6 draft had a `correlation_id` "to group a ride's events." **Dropped** — it's the
same trap as the whole §5.5 saga: a derived identifier sourced from the active offer is
NULL/wrong exactly in lost-mode, split-chains, resurrections, and out-of-order cancels —
precisely the anomalous moments the ledger exists to capture. The tracking spine is
`(driver_id, event_time)` + `offer_id` per event; "a ride's events" is an **analytical
fold** over the offer-lifecycle events at debug time, not a fabricated chain. (For the
traffic-light multi-PUDO case there is no single "ride" anyway — driver+time is the only
honest spine.) Only attach an *explicit* token (`active_contract_id`/`session_token`) if
it's read straight from a state table — never fabricated.

### 6.2 The stateless lookback read (Gemini 2026-06-07, ratified)

Cloud Run is stateless-per-invocation, so the queue-diff (§3b) AND matcher-inflection
detection (§5) both need the *prior* persisted perception. One read at the top of the
heartbeat gets both:
```sql
SELECT queue_snapshot, matcher_snapshot
FROM app_private.event_ledger
WHERE driver_id = %s
ORDER BY event_time DESC
LIMIT 1;
```
The composite index `(driver_id, event_time DESC)` makes finding that row instant; the
two jsonb columns are a single heap fetch (NOT index-only — large jsonb is never in the
index — but trivial for one row). At 1Hz "horny" cadence this is one indexed single-row
read per heartbeat per driver — bounded and fine with the index.
- **queue_snapshot** → diff vs current predicate output → emergent-reap events (§3b).
- **matcher_snapshot** `{top_candidate_offer_id, confidence_tier}` → diff vs current →
  inflection events (§5). Tier boundaries **must include the 0.40 floor**, so "crossed the
  floor" is a tier-change (and you don't log on sub-threshold wiggle).
- *Alternative considered:* piggyback the prior snapshot onto the `driver_trip_state` row
  the heartbeat already reads (the `effective_last_move` prefetch) → zero extra query, at
  the cost of a mutable column on `driver_trip_state`. Keep the clean ledger-read (ledger
  stays self-contained) unless profiling demands otherwise.

### 6.3 Raw GPS on the event — yes; geocode-degradation framing — rejected

Carry **raw, un-snapped device coords + accuracy** on every event (§6 `lat`/`lng`/
`gps_accuracy`). Rationale is NOT "geocode-degradation analysis" — that conclusion was
**withdrawn** (the km-offset finding was scoring-based mis-attribution; the established
root cause is offer *availability*, not geocode). The rationale is general: raw coords are
the un-derived sensor truth, consistent with the perception-ledger philosophy, and let
*any* localization question be answered without re-derivation. Where the matcher computes
a snapped/derived position, store **both raw + snapped** in that event's `payload` so the
snap delta is visible. Do NOT duplicate the full GPS stream into the ledger —
`heartbeat_log` owns continuous telemetry (§3 boundary); the ledger carries position
*at the event* only.

---

## 7. Consolidation is a MIGRATION, not a CREATE TABLE (scope honestly)

Folding `pudo_decision_context` and `driver_trip_state_log` in means:

- Repointing live hot-path writers (`_log_decision_context`, the state-log inserts).
- A **shadow-write period**: both the old logs AND the ledger write simultaneously,
  with a background assertion script verifying the change-only stream *accounts for
  every metric* the verbose old tables captured — BEFORE retiring the old writers.
  Don't cut over until that assertion passes (it's the proof that deltas+keyframes
  actually reconstruct what the per-heartbeat stream captured).
- A **back-compat view** so existing forensic queries don't break mid-cutover.
- §XIV.J live-PG test obligations for everything touching the cursor.

This is a careful cutover, not a side-effect of standing up the ledger.

---

## 8. Honest scope

This is a **real build, bigger than fix #1 or fix #2 were** — schema, a thin
emit-helper at every event site, the queue-diff engine (§3b), the keyframe scheduler
(§4), the migration with shadow-period + assertion-verification + back-compat views
(§7), schema-level grant changes (§2.5), and live-PG tests throughout. Scope it as a
**multi-session phased build** with its own recon → design → ratify → migrate phases,
NOT "stand it up quick before fix #3." The sequencing (ledger before fix #3) is right;
just don't let "before fix #3" compress it into something smaller than it is.

**Phasing — DECIDED (Andrew + Gemini, 2026-06-07): greenfield-first + immediate shadow
validation, then cut over.** A big-bang consolidation on line one risks breaking the
hot-path heartbeat writers while wiring a complex new layout — too high. Instead:
1. Stand up the ledger as an **entirely additive greenfield** structure. Legacy
   `pudo_decision_context` / `driver_trip_state_log` keep writing exactly as today.
2. Wire the new event handlers (the queue-diff engine §3b, the sentinel/keyframe logic
   §4) into the greenfield ledger. **Run the next drive.**
3. Only once the drive logs prove the change-only engine catches every emergent reap
   (the shadow-period assertion in §7 passes) → **cut over**: retire the old writers,
   repoint internal code into the ledger, and deploy a Postgres **VIEW under the old
   table names** so legacy forensic scripts keep working.

So the consolidation (§7) is **phase 2**, gated on the shadow assertion — not line one.

---

## 8.5 DECIDED — retention: 14-day daily-partition drop (NOT `DELETE` of old rows)

**Resolved (Andrew + Gemini, 2026-06-07): partition the ledger by day; retain 14 days;
drop the oldest partition. Interval is 2 weeks (Andrew's call — debug + onboarding never
need older; anything analytical older than that belongs in a cold warehouse, not the hot
transactional ledger).**

**Mechanism rationale — why partition-drop, not `DELETE WHERE created_at < now()-14d`
(record this so it isn't re-litigated):** `DELETE` is the intuitive choice and the wrong
tool for a high-volume append-only table — it's the version that quietly melts the SSD
anyway. In Postgres, `DELETE` does **not** return disk to the OS: it marks rows dead
(MVCC), autovacuum reclaims them only *for reuse within the same table*, so the file
stays at high-water mark. Shrinking it needs `VACUUM FULL` (exclusive lock, full rewrite)
or `pg_repack`. So a daily delete-old-rows cron gives the worst of both: the file never
shrinks **and** you pay constant vacuum + WAL + index-bloat churn competing with the live
heartbeat writes. By contrast, **`DROP` of the oldest partition is O(1)** — a metadata op
that instantly frees the whole chunk to the OS, no vacuum, no bloat, no lock fight.

**"Drop the partition" ≠ "drop the ledger."** The ledger is one logical table backed by
per-day child partitions; you drop only the oldest 14-day-old child while the parent and
all recent partitions keep serving. It's the oldest slice falling off the back.

Daily (not weekly) partitions give an exact 2-week cutoff (keep 14, drop day-15); weekly
would be coarser (14–21 days effective). The one real cost: partition *lifecycle*
automation — pre-create tomorrow's partition, drop the old one — via `pg_partman` or a
tiny scheduled job. No custom Python rollup/aggregation crons (bug surface, wasted
compute) — partitions only.

## 9. Decision status

**RESOLVED (2026-06-07):**
- **Keyframe trigger** — §4: hybrid `50 events OR 15 min`, both load-bearing.
- **Phasing** — §8: greenfield-first + shadow-validate, then cut over (consolidation is phase 2).
- **Retention** — §8.5: 14-day daily-partition drop (not `DELETE`); partition lifecycle via
  pg_partman or a scheduled job; no rollup crons.

**STILL OPEN / owed before build:**
- **Recon owed before design (the next concrete step, L-6):** read the existing log writers
  (`_log_decision_context`, the `driver_trip_state_log` inserts, `heartbeat_log`) and inventory
  exactly what each captures, so the unified schema is a provable superset and the shadow-period
  assertion (§7) has a checklist.
- **Tune `N` (and confirm `M`)** from real event-density recon — §4 gives starting values (50/15).
- **Partition-lifecycle tooling:** `pg_partman` vs a tiny scheduled job — pick at design time.

**Out of scope (named so it isn't conflated):** this does NOT make the Fix #3 product call
(ground-truth tolerance: arrest near-but-not-at a pickup you can't physically stop at — miss /
correct-observation / log-don't-bind). The ledger makes Fix #3 *investigable*; it doesn't make the
tolerance decision.

---

## 10. The trap to keep naming

Be honest that `pudo_decision_context` is already a per-heartbeat matcher-event log and
`driver_trip_state_log` is already a state-transition log. The ledger's value-add is
(a) unifying all event types on ONE timeline, (b) the offer/cluster/queue linkage they
lack, and (c) the missing event types (emergent reaps, cluster lifecycle, queue
snapshot). If it doesn't deliver all three, it's a 6th half-overlapping stream — the
thing that ADDS to the archaeology instead of ending it. The litmus test (intro) is the
guard: one-query reconstruction, or it wasn't worth building.
