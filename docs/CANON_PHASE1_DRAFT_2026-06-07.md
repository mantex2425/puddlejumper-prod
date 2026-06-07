# Canon rewrite — Phase 1 DRAFT (§I, §II, §VIII) for Gemini review

## ✅ APPLIED to `CANONICAL_RULES.md` (2026-06-07) — consensus reached

Claude proposed → Gemini reviewed → both corrected against the grep → consensus → §I/§II/§VIII +
§XIV.G-redirect are now live in `CANONICAL_RULES.md`. **Three grep-verified corrections folded at
apply-time** (the meta-result: on all three "ratified" judgment-calls, the grep moved one lane and
corrected two reasons — every one was mis-laned or mis-justified, and neither plausibility-review
caught it):
1. **`decision_log`** — lane unchanged (Authoritative); REASON corrected: it's the runtime
   offer-identity / driver-scoping bridge (`id`, `driver_id`) the queue projection joins through —
   **verdict-blind** (reads identity, never `app_verdict`). NOT FK-lineage, NOT "insert fails →
   tracking breaks." The Authoritative join must not reach for `app_verdict`.
2. **`intelligence_conversations`** — **lane CORRECTED**: Passive → **Other subsystem**. Grep shows
   `market_intelligence.py:317` reads `role`/`content` back to feed the next LLM turn → read-to-drive-
   behavior, fails the Passive "never read by runtime" test; but it's the LLM-assistant subsystem,
   not the PUDO pipeline → neither PUDO lane.
3. **Geo caches** (`poi_cache`/`geocode_cache`/`road_membership_cache`) — lane unchanged
   (Authoritative); REASON corrected from "not droppable" (false) to **"read at future
   offer-evaluation; stale/corrupt poisons the next eval's input"** (poi_service.py:278/639,
   geo_utils.py:82, road_membership.py:242). `pickup_market_signals` keeps the distinct
   "genuinely not-droppable — lost fare = lost product (§0)" justification.

Plus **Gemini's MVCC guardrail** on `driver_trip_state.last_queue_snapshot`: READ every heartbeat
(rides the existing authoritative read) to detect deltas, WRITTEN only on detected change/reap —
write frequency scales with event density, never heartbeat density.

Housekeeping confirmed: no `tmp/road_membership.py` shadow copy exists (only production
`./road_membership.py`). Phase 2 (§III/IX/X/XI/XII/XIII vocab purge; §XII = dead-machinery → fold
prohibition into §X) defers to the ledger's shadow-validation window per the plan.

---


**Status:** DRAFT proposed replacement text for the Prerequisite Gate sections, per
`docs/CANON_REWRITE_PLAN_2026-06-07.md` (ratified plan). **Not yet applied to
`CANONICAL_RULES.md`** — this is the proposal for Gemini review; consensus → then it replaces the
live sections. Scope: ONLY the event-ledger's direct ancestors (Space/Time foundation + the EXECUTE
write-boundary). The §III/IX/X/XI/XII/XIII vocab purge is Phase 2.

## Drafting adjustments to flag (deviations from the plan, made on the data-driven rule)

1. **§I and §II are NOT physically merged.** The plan said "merge §I+§II+§XIV.G into one Foundation
   section," but the grep shows **§II has 6 code refs** — collapsing it would break them (violates
   the data-driven numbering rule). So §I (Space) and §II (Time) stay as two numbered sections under
   a shared **"Foundation"** framing; only **§XIV.G** (no code refs) folds in as a redirect tombstone.
2. **§VIII retitled** "THE 4-BOX CONTROLLER (MANDATORY ARCHITECTURE)" → **"SEPARATION OF CONCERNS —
   MONITOR / DIAGNOSE / PLAN / EXECUTE"** (drops the "controller" state-machine connotation; number
   §VIII preserved, so any ref resolves; the *frame* survives per the deprecation notice).
3. The **§XII prohibition → §X fold** is **Phase 2** (not drafted here; §X is in the deferred purge).

---

## PROPOSED §I (replaces current §I)

```markdown
## I. COORDINATE RULES — SPACE (Foundation)

These bind every coordinate that enters the system — including every persisted/logged event.
A swapped or raw-geometry coordinate corrupts spatial joins and the event ledger silently, at
the source. Trust the abstraction.

### Mandatory functions (the only sanctioned path)
- **H3:** `app_private.coords_to_h3(lat, lng)`, `app_private.h3_to_lat(h3)`, `app_private.h3_to_lng(h3)`
- **Geometry:** `app_private.coords_to_point(lat, lng)`, `app_private.coords_to_geography(lat, lng)`
- **Distance:** `app_private.distance_miles(lat1, lng1, lat2, lng2)`

### The ordering rule
Arguments are **always `(lat, lng)`**. If you are manually swapping them to fit a raw function,
you are doing it wrong.

### The blacklist (code-review blockers)
**NEVER** write `ST_MakePoint` (it takes `(lng, lat)` — the swap trap), `h3_latlng_to_cell`, or
`h3_cell_to_latlng` directly. Any direct PostGIS/H3 primitive or raw geometry construction is a
code-review blocker.

### Developer sanity rule
> "If you find yourself thinking about coordinate order or manual geometry creation, STOP. Use the
> `app_private` canonical functions. Trust the abstraction."

### Persistence binding
Stored coordinates use the `(lat, lng)` convention; any derived H3/geometry is produced through the
canonical functions above. The event ledger's `lat`/`lng` and its `queue_snapshot`/`payload`
coordinates follow this rule; the writer never constructs raw geometry.
```

## PROPOSED §II (replaces current §II)

```markdown
## II. TEMPORAL RULES — TIME (Foundation, UTC-MANDATORY)

These bind every timestamp the system stores — including `event_time` on every logged event. A
naive or local-time timestamp silently breaks chronological ordering, the `(driver_id, event_time
DESC)` lookback, and any delta/keyframe reconstruction. Always UTC.

- **Canonical time:** UTC, always.
- **Postgres:** `(NOW() AT TIME ZONE 'UTC')`, or `NOW()` directly on a `timestamptz` column.
- **The engine is timezone-agnostic.** All "Texas Time" localization happens at the edge (the UI),
  never in system logic. Local-time SQL in the engine is a code-review blocker.

### Persistence binding
Event/log timestamps are `timestamptz` written via `NOW()` — never a naive datetime. Every
ordering-dependent read (latest-row lookbacks, delta folds) relies on this.
```

## REDIRECT: §XIV.G (replaces current §XIV.G body)

```markdown
### G. Coordinate and Time Canonicalization
Superseded — folded into the Foundation sections. See **§I (Space)** and **§II (Time)**. Direct
`ST_MakePoint` or local-time SQL remains a code-review blocker (stated canonically in §I/§II).
```

## PROPOSED §VIII (replaces current §VIII)

```markdown
## VIII. SEPARATION OF CONCERNS — MONITOR / DIAGNOSE / PLAN / EXECUTE

The 4-box *separation-of-concerns frame* survives the Sprint-A demolition; the state-machine file
assignments and `sm_transition()` do not. Every code path belongs to exactly one box:

    MONITOR:  Receive sensor inputs. No logic, no writes.
              → driver_heartbeat.py (heartbeat receipt / orchestration entry)
              → Android accessibility service (offer-card capture)

    DIAGNOSE: Interpret sensor data. Pure reads, no writes.
              → where_am_i.evaluate() (the WAI matcher)
              → cluster / arrest detection

    PLAN:     Business logic and strategy. No DB writes.
              → decisions/ (router, engine, triangulation_enricher)
              → dispatch (PUDO action selection)

    EXECUTE:  The write path. Two strictly separated lanes (below).

**Box assignment is per code-PATH, not per file.** A module spans boxes: `driver_heartbeat.py` is
MONITOR at heartbeat receipt AND Authoritative EXECUTE when it orchestrates the dispatch writes —
listing it under MONITOR does NOT make the file write-free. Classify each write/read path, not the
file it lives in.

### The EXECUTE two-lane rule (post-`sm_transition`)

"All writes go through one stored proc" died with `sm_transition()`. EXECUTE is now two lanes that
must never blur:

- **Authoritative / Gated lane** — writes that ALTER system state OR record the §0 product output.
  Per the write-inventory grep (below), this lane is: `offer_history` (offer lifecycle / queue
  source), `driver_trip_state` (`current_offer_id`, arrest, leg odometer), `driver_trip_locks`
  (concurrency), `decision_log` (the offer/verdict record `offer_history` hangs off), and —
  **critically, the §0/§XV product output** — the pricing cache (`pickup_market_signals`) and the
  geographic caches (`poi_cache`, `geocode_cache`, `road_membership_cache`, `street_network*`),
  written when a PUDO is observed. State writes compose `LIVE_OFFER_PREDICATE_SQL` (§XIV.H). This is
  the ONLY lane a runtime decision may read from. Note: unlike the Passive lane, the product-cache
  writes are **mandatory-on-observation, not droppable** (§0.D.4 / §XV — observation is the product).
- **Passive / Open lane** — best-effort, non-blocking, **append-only observability** writes:
  `pudo_decision_context`, `heartbeat_log`, and the event ledger. This lane:
  - MUST NOT alter application state.
  - MUST NOT be the source of any runtime/business decision. Reading a *decision* out of it makes
    it a parallel state machine and violates §VII (the ledger is observability, never truth).
  - MUST NOT block or fail a live decision — a logging error is swallowed.
  - Is NOT read by the runtime loop. The one piece of prior state the heartbeat needs (the
    queue/matcher snapshot for the emergent-reap and inflection diff) lives on `driver_trip_state`
    (the Authoritative lane the heartbeat already reads each tick), NOT on the ledger.

### Violation patterns (stop and redesign)
- A state mutation in the Passive lane, or an observability write in the Authoritative lane.
- A runtime read of the Passive lane (the ledger) to drive a decision.
- A DB write inside MONITOR or DIAGNOSE; business logic that writes inside PLAN.

### Before adding any write, ask:
1. Does this ALTER state / visibility / eligibility? → Authoritative lane; compose the predicate.
2. Or is it a record of what happened? → Passive lane; append-only, best-effort, never read by runtime.
3. If a runtime loop needs to READ prior state → it reads the Authoritative lane
   (`driver_trip_state`), never the Passive lane.
```

---

## State-write inventory (review ask #2 — verifiable, not opinable)

`grep -rhoE "INSERT INTO app_private\.[a-z_]+|UPDATE app_private\.[a-z_]+" --include="*.py" .`
(non-test), by table. This is the authoritative list to ratify the §VIII lanes against — **the
original draft enumeration (offer_history + driver_trip_state only) was incomplete; the grep
surfaced the product caches and locks.**

| Table | writes | Proposed lane |
|---|---|---|
| `offer_history` | U14 / I9 | **Authoritative** — offer lifecycle / queue source |
| `driver_trip_state` | U12 / I7 | **Authoritative** — current_offer_id, arrest, odometer |
| `driver_trip_locks` | I5 | **Authoritative** — concurrency/lock state |
| `decision_log` | I7 / U2 | **Authoritative** — offer/verdict record (offer_history parent) |
| `pickup_market_signals` | U8 / I1 | **Authoritative** — PRICING cache (§0 product output) |
| `poi_cache` | I8 / U4 | **Authoritative** — geographic/POI cache (§0) |
| `geocode_cache` | I1 / U1 | **Authoritative** — geographic cache (§0, Google-Tax displacement) |
| `road_membership_cache` | I2 / U1 | **Authoritative** — geo/topology cache |
| `street_network` / `street_network_tiles` | I2 / I1 | **Authoritative** — geo/topology cache |
| `pudo_decision_context` | I1 | **Passive** — matcher forensic log |
| `heartbeat_log` | I1 | **Passive** — telemetry |
| `contest_labels` | I1 | **Passive** — debug ground-truth taps |
| `crash_reports` | I1 | **Passive** — crash telemetry |
| `intelligence_conversations` | I1 | **Passive** — LLM conversation log |
| `driver_settings_new` / `driver_active_market` / `monitor_last_report` / `deletion_requests` | I3 / I3 / U1 / I1 | **Other subsystem** — not the PUDO decision pipeline (settings, market, monitor, GDPR) |

## Review asks for Gemini

1. **§I/§II non-merge** (adjustment #1) — agree the data-driven rule overrides the "merge into one
   section" aspiration? (§II's 6 code refs would break.)
2. **§VIII EXECUTE lanes** — ratify each table's lane in the **State-write inventory above** (the
   grep is exhaustive; completeness is verifiable, not opinable). Confirm the Authoritative/Passive/
   Other classifications; the only judgment calls are the borderline ones (`decision_log`,
   `intelligence_conversations`, the geo caches). Do NOT ratify against plausibility — ratify against
   the inventory.
3. **§VIII retitle** — acceptable to drop "4-BOX CONTROLLER" for "SEPARATION OF CONCERNS"?
4. Confirm the Passive-lane "never read by runtime" + the `driver_trip_state` diff-seed resolution
   reads cleanly as canon (it's the §6.2 fork resolution made canonical).
