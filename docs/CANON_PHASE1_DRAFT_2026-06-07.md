# Canon rewrite — Phase 1 DRAFT (§I, §II, §VIII) for Gemini review

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

### The EXECUTE two-lane rule (post-`sm_transition`)

"All writes go through one stored proc" died with `sm_transition()`. EXECUTE is now two lanes that
must never blur:

- **Authoritative / Gated lane** — writes that ALTER system state: offer-history lifecycle stamps
  (`actual_pickup_at`/`actual_dropoff_at` via fired dispatch actions), `driver_trip_state`
  mutations (`current_offer_id` swap, arrest counters, leg odometer), and anything that changes
  queue visibility or matching eligibility. These compose `LIVE_OFFER_PREDICATE_SQL` (§XIV.H). This
  is the ONLY lane a runtime decision may read from.
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

## Review asks for Gemini

1. **§I/§II non-merge** (adjustment #1) — agree the data-driven rule overrides the "merge into one
   section" aspiration? (§II's 6 code refs would break.)
2. **§VIII EXECUTE lanes** — is the Authoritative-lane enumeration complete/correct? (offer_history
   stamps, driver_trip_state mutations, predicate-composed eligibility.) Any state-write path missed?
3. **§VIII retitle** — acceptable to drop "4-BOX CONTROLLER" for "SEPARATION OF CONCERNS"?
4. Confirm the Passive-lane "never read by runtime" + the `driver_trip_state` diff-seed resolution
   reads cleanly as canon (it's the §6.2 fork resolution made canonical).
