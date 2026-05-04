# State Machine Demolition Plan — 2026-05-04

**Status:** RATIFIED — clear to execute (Andrew + Gemini second-pass, 2026-05-04)
**Author:** Claude, session 2026-05-04
**Predecessor:** `docs/SPRINT_A_FINDINGS_2026-05-04.md` identified the bridge-half-built failure mode. `docs/SIMPLIFIED_ARCHITECTURE.md` ratified the destination. This document is how we cross.
**Target branch:** new branch `demolition-2026-05-04` off `patch-00566a-unified-refinement`
**Companion documents:**
  - `docs/RIDE_LIFECYCLE.md` — the new architectural reference replacing `CANONICAL_RULES.md` sections III, VIII, IX, X, XI, XII
  - `migrations/2026-05-04_demolish_state_machine.sql` — the demolition itself
  - `migrations/2026-05-04_verify_demolition.sql` — post-demolition assertions

---

## 1. Why this is a single coordinated change

The current production system has two architectures coexisting: the legacy state machine (Postgres triggers, functions, columns, `DriverStateMachine` Python class, all callers) and the new pure-dispatch architecture (WAI matcher, naked-list contract, dispatch as pure function, executor in heartbeat handler).

These two architectures conflict on every write to `driver_trip_state`. Today's production data demonstrated the symptom: a single completed dropoff produced 9 successive `FireDropoff` actions because the `BEFORE UPDATE` trigger `enforce_state_transition_trigger` silently rejected the `current_offer_id = NULL` write when issued outside `sm_transition()`'s context.

A phased migration preserves the conflict. The conflict IS the bug. Therefore: total demolition in a single coordinated change, with the new wiring fully specified before execution.

This document is the executable runbook. Andrew runs it step-by-step. Each step has a verification command and a recovery path.

---

## 2. Five ratified design decisions (locked 2026-05-04)

1. **Only `_execute_action` writes `current_offer_id`.** Manual nail (`pickup_confirm.py`) is band-aid debugging infrastructure that dies in a follow-up commit. Until then it routes through `_execute_action(FirePickup(offer_id), ...)`.
2. **`current_offer_id IS NOT NULL` is the only "ride in progress" signal.** No ENROUTE/IN_TRIP/STACKED distinction anywhere in the post-demolition codebase. The triangulation pricing engine reads `current_offer_id` directly.
3. **No state-stuck watchdog. Liveness watchdog refactored.** Per Gemini's review, `monitor.py` is not deleted. Its state-stuck-reset logic dies (no states, nothing to be stuck in), but its liveness role is preserved by refactoring to detect stale heartbeats: surface drivers whose `heartbeat_at < NOW() - INTERVAL '5 minutes'` while `current_offer_id IS NOT NULL`. A hung `current_offer_id` from a normal driving session clears when the next pickup is recognized (Case D / implicit cancellation). A hung `current_offer_id` from a phone-died scenario surfaces via the liveness watchdog to whatever existing alerting plumbing `monitor.py` already drives (Discord, dashboards, etc.). The system remains self-healing for in-flight observations; the watchdog becomes purely a liveness/alerting concern, not a state-correctness concern.
4. **`pudo_decision_context.state_at_eval` renamed to `current_offer_id_at_eval`** (text NULL). Historical rows with legacy state strings (`'ENROUTE'`, `'IN_TRIP'`, `'STACKED'`) are not preserved — the data is wrong by the new architecture's lights, and the new column carries the meaningful 1-bit memory snapshot.
5. **`suspected_pudos` family is deleted.** All 16 partition tables, the parent table, and any related code. Not useful anymore.

---

## 3. Inventory of artifacts to remove

### 3a. Postgres functions (6)

```
app_private.enforce_state_transition()      -- BEFORE-trigger validator
app_private.log_state_transition()          -- AFTER-trigger audit writer
app_private.set_state_timestamp()           -- BEFORE-trigger updated_at setter
app_private.sm_find_nearby_offer()          -- helper used by sm_transition
app_private.sm_read()                       -- DriverStateMachine.read backend
app_private.sm_transition()                 -- DriverStateMachine.transition backend
```

### 3b. Postgres triggers (5, all on `driver_trip_state`)

```
tr_log_state_transition           AFTER  INSERT
tr_log_state_transition           AFTER  UPDATE
enforce_state_transition_trigger  BEFORE INSERT
enforce_state_transition_trigger  BEFORE UPDATE
tr_set_state_timestamp            BEFORE UPDATE
```

### 3c. Postgres tables and columns

**Drop entirely:**
- `app_private.valid_state_transitions` (transition rule set, used only by sm_transition)
- `app_private.suspected_pudos` and 16 partitions `suspected_pudos_p00` … `suspected_pudos_p15`

**Drop columns:**
- `driver_trip_state.state` (NOT NULL today, has CHECK constraint `valid_state`)
- `driver_trip_state.state_updated_at` (no longer meaningful)

**Rename column:**
- `pudo_decision_context.state_at_eval` → `current_offer_id_at_eval` (legacy values not preserved; existing rows get NULL)

**Drop CHECK constraint:**
- `driver_trip_state.valid_state`

**Leave alone (forensic, harmless after writers stop):**
- `app_private.driver_trip_state_log` — historical audit table; new architecture does not write here
- `app_private.contest_events.trip_state` (text NULL, vestigial)
- `app_private.heartbeat_log.state` (text NULL, vestigial; `driver_heartbeat.py` stops populating it)
- `app_private.monitor_last_report.last_state` (vestigial; `monitor.py` is being deleted, see §3d)
- `app_private.pudo_decision_context.planner_target_state` (text NULL, vestigial)
- `app_private.ride_audit.state_transitions` (text NULL, vestigial)

### 3d. Python files

**Delete entirely:**
- `state_machine.py` — `DriverStateMachine` class, no longer referenced
- `replay_drive_stateful.py` — replay tool depends on state-transition log; superseded by `scripts/replay_pudo.py`
- `decisions/state_enricher.py` — wraps state machine; replaced by `current_offer_id` reads

**Rewrite:**
- `decisions/triangulation_enricher.py` — strip 5 `DriverStateMachine.transition()` calls; read `current_offer_id` directly for pricing context per `SIMPLIFIED_ARCHITECTURE.md` §8 (Triangulation Filter)
- `nail_it_core.py` — `write_nailed_position` writes coords directly to `driver_trip_state` without going through state-machine paths; `compute_target` legacy ENROUTE block (lines ~1143–1210) deleted entirely
- `driver_heartbeat.py` — remove `state_at_eval = 'UNCOMMITTED'` default, the state read from `driver_trip_state`, and the comment block at lines 7–33 referencing state-machine writes
- `driver_status.py` — remove `state` from API response shape, remove the `driver_trip_state_log` query block
- `monitor.py` — refactor from "stuck-state auto-reset" to "stale-heartbeat liveness check." Two predicates:
  1. **Stale heartbeat:** `heartbeat_at < NOW() - INTERVAL '5 minutes'` while `current_offer_id IS NOT NULL` (active ride with no recent heartbeat = phone died, app crashed, or driver lost connection)
  2. **Stale GPS while active:** `gpsAgeSec > 60` while `current_offer_id IS NOT NULL` (heartbeats still arriving but GPS frozen — high-value liveness signal per Gemini's review)

  Existing Discord notification / alerting paths preserved; only the state-machine read/transition logic is removed.
- `pickup_confirm.py` — rewire instead of delete. Manual nail endpoint stays available for Andrew's dev-build emergency override. Handler body rewritten to construct a `FirePickup(offer_id)` action and pass it to `_execute_action(...)` (the same executor the heartbeat handler uses), bypassing the state machine entirely. Per Gemini's Q5 ratification: `_execute_action`'s `cluster` argument becomes optional; when called from the manual-nail path with `cluster=None`, side-effect writers (e.g., `write_nailed_position`) use the heartbeat's `lat/lng` as the nailed position rather than a cluster centroid. Returns the action's effect (success / error) to the caller.
- `dropoff_confirm.py` (if present) — same rewire pattern as `pickup_confirm.py`. Manual override constructs `FireDropoff(offer_id)` and routes through `_execute_action` with `cluster=None`.
- `driver_heartbeat.py` `_execute_action` signature — `cluster: Optional[Cluster] = None`. Side-effect writers consult cluster when present, fall back to caller-supplied lat/lng (request body for heartbeat path; manual-nail handler for confirm endpoints) when absent.

**Update (smaller surface):**
- `test_endpoints.py` — `reset_driver` no longer needs to clear state-machine artifacts beyond `current_offer_id = NULL`
- `bead_on_wire.py` — the `compute_target` callers in `nail_it_core` are gone, so `bead_on_wire`'s entire dead-decider section (lines ~545–680, the BMOAR `single_road` and `poi` deciders) becomes truly dead. Header rewrite documenting this. Actual deletion deferred to follow-up commit per scope discipline.

### 3e. Endpoints (Cloud Run)

`pickup_confirm.py` (and `dropoff_confirm.py` if present) are kept and rewired per §3d. Routes remain mounted at `/api/v1/confirm/pickup` and `/api/v1/confirm/dropoff`. Andrew's Android dev build continues to function for diagnostic emergency-override purposes. Production (8th-grader) drivers do not see these endpoints because the dev build is not the production build — the architectural commitment that "production has no manual nail" remains intact at the build-target level, while the source endpoints survive for dev-build use.

When the dev-build manual nail surface is eventually retired (post-launch), the rewired endpoints can be deleted in one further commit.

### 3f. Tests

- Any test referencing `DriverStateMachine`, `sm_transition`, `valid_state_transitions`, or hardcoded state strings (`'ENROUTE'`, `'IN_TRIP'`, `'STACKED'`, `'UNCOMMITTED'`, `'STACKED'`, `'REFINE_PICKUP'`, `'REFINE_DROPOFF'`) deleted or rewritten.
- `tests/schema_contract.py` updated to assert the new schema (no state column, etc.).
- Integration tests: `tests/test_integration.sh` likely references the legacy schema; the 22/61 floor mentioned in userMemories will shift; aim for new floor of 100% on whatever survives.

### 3g. Documentation

- `docs/CANONICAL_RULES.md` sections III, VIII, IX, X, XI, XII — replaced by content in `docs/RIDE_LIFECYCLE.md`. Old sections deleted, with a one-line pointer at each section's former location: `(removed in demolition 2026-05-04; see RIDE_LIFECYCLE.md)`.
- `docs/SIMPLIFIED_ARCHITECTURE.md` §6 (Queue Synchronization) — amend the queue predicate. Current text says `WHERE actual_pickup_at IS NULL OR actual_dropoff_at IS NULL` (in-flight only). Andrew flagged this as wrong: the queue must include declined offers within the GC window, otherwise the canonical decline-but-driven scenarios (S04, Sheraton) can't be matched. Correct predicate uses the GC window formula `LEAST(GREATEST((pu_min + trip_min) * 1.5, 15), 240)` minutes from offer creation.

---

## 4. Pre-flight checklist

These five items must be complete before any DDL runs.

1. **Branch off cleanly:** `git checkout -b demolition-2026-05-04`. All work happens on this branch. The branch is rebased onto `patch-00566a-unified-refinement` if origin moves during the work.

2. **`pg_dump` of `app_private` schema** — full schema and data, written to a timestamped file outside the repo. This is the rollback insurance.

3. **Read-only `pg_dump` of just the doomed artifacts** — schema only, for archival. So if a future session asks "what did `enforce_state_transition()` actually do," the answer exists.

4. **Tag the current `main` HEAD with `pre-demolition-2026-05-04`** for fast `git revert` to pre-demolition state if needed.

5. **Confirm no production driving sessions in flight.** This work needs ~30 minutes of "the API may be intermittently broken" tolerance. Andrew is the only driver; this is purely scheduling discipline. (Per 2026-05-04 confirmation: Andrew has exclusive system access during the demolition window.)

6. **Grep Discord notification templates for hardcoded state strings.** Per Gemini Q1: any template containing `{state}` or hardcoded `'ENROUTE'`/`'IN_TRIP'`/`'STACKED'` in its rendered text needs updating to use `current_offer_id`-based phrasing (e.g., `IN_TRIP` if `current_offer_id` is set, else `IDLE`). Run before any code changes:
   ```bash
   grep -rn "{state}\|'ENROUTE'\|'IN_TRIP'\|'STACKED'" --include="*.py" --include="*.md" --include="*.j2" --include="*.html" | grep -iE "discord|webhook|notify|alert"
   ```
   Any hits surface templates that need a follow-up commit before the demolition lands.

---

## 5. Execution sequence

This is the order in which work happens. Each step is independently verifiable. Roll back via §6 if any step fails.

### Step 1 — Pre-flight captures

```bash
# pg_dump of the entire app_private schema + data
pg_dump -h 10.128.0.2 -U postgres -d puddlejumper \
  --schema=app_private \
  --file=/tmp/pre-demolition-app-private-2026-05-04.sql

# Targeted schema-only dump of the doomed artifacts
pg_dump -h 10.128.0.2 -U postgres -d puddlejumper \
  --schema-only \
  --table='app_private.driver_trip_state' \
  --table='app_private.driver_trip_state_log' \
  --table='app_private.valid_state_transitions' \
  --table='app_private.suspected_pudos*' \
  --file=/tmp/doomed-artifacts-schema-2026-05-04.sql

# Function source archive (pg_dump --schema-only catches these)
psql -h 10.128.0.2 -U postgres -d puddlejumper -c "\df app_private.*" \
  > /tmp/pre-demolition-functions-list.txt
```

**Verification:** all three files exist and are non-empty.

```bash
ls -la /tmp/pre-demolition-app-private-2026-05-04.sql \
       /tmp/doomed-artifacts-schema-2026-05-04.sql \
       /tmp/pre-demolition-functions-list.txt
```

### Step 2 — Tag pre-demolition HEAD

```bash
git tag pre-demolition-2026-05-04
git push origin pre-demolition-2026-05-04
git checkout -b demolition-2026-05-04
```

**Verification:**

```bash
git tag | grep pre-demolition
git branch --show-current
```

### Step 3 — Application code changes (state-agnostic deploy first)

This is where the new wiring lands. The order within this step matters: each subsequent commit builds on the prior one.

**Commit 3a — Triangulation enricher state-agnostic.** Strip `DriverStateMachine.transition()` calls from `decisions/triangulation_enricher.py`. Replace state reads with `current_offer_id` reads per `RIDE_LIFECYCLE.md` §3 (pricing context selection rule).

**Commit 3b — Nail it core direct writes.** `nail_it_core.write_nailed_position` writes `nailed_pickup_lat/lng/error_m` directly via raw `UPDATE`, no `sm_transition`. Delete the ENROUTE legacy block (lines ~1143–1210). Delete `compute_target` callers.

**Commit 3c — Heartbeat handler cleanup.** Remove the `state` SELECT and `state_at_eval` default from `driver_heartbeat.py`. Replace `state_at_eval` write to `pudo_decision_context` with `current_offer_id_at_eval`.

**Commit 3d — Driver status API state-agnostic.** Remove `state` column read and the `driver_trip_state_log` join from `driver_status.py`. API response shape simplifies; document the change.

**Commit 3e — Test endpoints update.** `test_endpoints.py` `reset_driver` no longer clears state-machine debris; just `current_offer_id = NULL`.

**Commit 3f — Monitor refactor.** Rewrite `monitor.py` to watch stale heartbeats (`heartbeat_at < NOW() - INTERVAL '5 minutes' AND current_offer_id IS NOT NULL`) instead of stuck states. Strip `DriverStateMachine` import and the state-stuck-reset code path. Preserve any existing Discord notification / alerting plumbing.

**Commit 3g — Confirm endpoint rewires.** `pickup_confirm.py` handler rewritten: build `FirePickup(offer_id)` action, call `_execute_action` (importing from `driver_heartbeat`), return the action's outcome. Strip `DriverStateMachine` import and the `state IN ('ENROUTE', 'IN_TRIP', 'STACKED')` filter on the `driver_trip_state` read. Same pattern applied to `dropoff_confirm.py` if it exists.

**Commit 3h — Delete dead modules.** `git rm`:
- `state_machine.py`
- `replay_drive_stateful.py`
- `decisions/state_enricher.py`

**Commit 3i — Test suite realignment.** Delete or rewrite tests that reference deleted modules, hardcoded state strings, or `DriverStateMachine`. New green floor target: 100% of surviving tests.

**Verification at end of Step 3** — full test suite green:

```bash
source ~/puddlejumper-prod/venv/bin/activate
python3 -m pytest tests/ -v 2>&1 | tail -30
```

If any test fails: do not proceed to Step 4. Diagnose the failure as a wiring gap and resolve before continuing.

### Step 4 — DB migration (atomic)

This is a single transaction. Either everything drops or nothing does. The migration script and a separate verification script are both committed to the branch:
- `migrations/2026-05-04_demolish_state_machine.sql` — the demolition itself
- `migrations/2026-05-04_verify_demolition.sql` — schema state assertions to run immediately after

```sql
-- migrations/2026-05-04_demolish_state_machine.sql
SET lock_timeout = '5s';   -- per Gemini Q2: fail fast rather than block heartbeats
                             -- if anything is holding driver_trip_state

BEGIN;

-- Triggers first (so subsequent column drops don't fire them)
DROP TRIGGER IF EXISTS enforce_state_transition_trigger ON app_private.driver_trip_state;
DROP TRIGGER IF EXISTS tr_log_state_transition ON app_private.driver_trip_state;
DROP TRIGGER IF EXISTS tr_set_state_timestamp ON app_private.driver_trip_state;

-- Functions (now safely orphaned)
DROP FUNCTION IF EXISTS app_private.enforce_state_transition() CASCADE;
DROP FUNCTION IF EXISTS app_private.log_state_transition() CASCADE;
DROP FUNCTION IF EXISTS app_private.set_state_timestamp() CASCADE;
DROP FUNCTION IF EXISTS app_private.sm_find_nearby_offer(text) CASCADE;
DROP FUNCTION IF EXISTS app_private.sm_read(text) CASCADE;
DROP FUNCTION IF EXISTS app_private.sm_transition(text, text, jsonb) CASCADE;

-- Tables: state-machine rule set and suspected_pudos family
DROP TABLE IF EXISTS app_private.valid_state_transitions CASCADE;
DROP TABLE IF EXISTS app_private.suspected_pudos CASCADE;  -- partitions cascade

-- driver_trip_state column changes
ALTER TABLE app_private.driver_trip_state DROP CONSTRAINT IF EXISTS valid_state;
ALTER TABLE app_private.driver_trip_state DROP COLUMN IF EXISTS state;
ALTER TABLE app_private.driver_trip_state DROP COLUMN IF EXISTS state_updated_at;

-- pudo_decision_context column rename (drop + add per decision 4 — historical
-- state_at_eval values are conceptually incompatible with the new 1-bit memory)
ALTER TABLE app_private.pudo_decision_context
  DROP COLUMN IF EXISTS state_at_eval;
ALTER TABLE app_private.pudo_decision_context
  ADD COLUMN current_offer_id_at_eval text;

COMMIT;
```

**Verification — immediately after COMMIT:**

```bash
psql -h 10.128.0.2 -U postgres -d puddlejumper -f migrations/2026-05-04_verify_demolition.sql
```

The verification script asserts every demolition target is gone and every new artifact is present. If any assertion fails, it raises NOTICE and the operator inspects before proceeding to Step 5.

### Step 5 — Deploy to Cloud Run

```bash
cd ~/puddlejumper-prod
bash deploy.sh
```

The auto-promote `--to-latest` from commit `698e217` ensures traffic flows to the new revision immediately.

**Verification — API health check:**

```bash
curl -s https://puddlejumper-api-XXX.run.app/health   # adjust URL
```

Expected: 200 OK. (Note: the actual health endpoint may not exist; substitute a known endpoint.)

**Verification — heartbeat smoke test:**

```bash
# Run the Bruno PUDO Simulation collection
cd ~/path-to-bruno  # actually this is on Andrew's laptop
bru run "PUDO Simulation" --env "PuddleJumper Dev" \
  --env-var TEST_EMAIL="$TEST_EMAIL" --env-var TEST_PASSWORD="$TEST_PASSWORD" \
  --delay 5500
```

Expected: 10/10 requests pass, 18/18 tests green. (Bruno is on Andrew's laptop; this verification step happens off-VM.)

### Step 6 — Documentation commit

The `docs/` updates land last so the rest of the demolition is verifiable independently.

- `docs/CANONICAL_RULES.md` — sections III, VIII, IX, X, XI, XII removed; one-line pointer at each former location to `RIDE_LIFECYCLE.md`. The §XIV deprecation notice at the top of the file is also removed (no longer in flux).
- `docs/RIDE_LIFECYCLE.md` — committed as the new permanent reference (companion document to this runbook).
- `docs/SIMPLIFIED_ARCHITECTURE.md` §6 — queue predicate corrected to use the GC window formula.
- `docs/SPRINT_A_FINDINGS_2026-05-04.md` — superseded by this demolition; add a status note at the top: "Findings closed via demolition 2026-05-04; see DEMOLITION_PLAN_2026-05-04.md."

### Step 7 — Field validation

Andrew drives three distinct scenarios per Gemini's Q7 ratification. Each must produce the expected behavior before the demolition is considered validated.

**Scenario 1 — The Clean Sweep (happy path).**
Drive a single accepted ride from start to finish. Expected behavior:
- Pickup fires once (Case B): `LogNoMatch` heartbeats while approaching → single `FirePickup` when cluster forms at pickup
- `current_offer_id` set to the offer_id
- Dropoff fires once (Case C): `LogNoMatch` heartbeats while driving → single `FireDropoff` when cluster forms at dropoff
- `current_offer_id` clears to NULL
- No redundant fires, no wedge state, no 9-loop bug

**Scenario 2 — The Houston Pivot (Case D, implicit cancellation).**
Accept ride A. Start driving toward A's pickup. While en route, accept ride B (or have one auto-stack). Drive to B's pickup instead of A's. Expected behavior:
- A's pickup never fires (driver never went there)
- When cluster forms at B's pickup: single `FireDropoff(A, canceled)` + single `FirePickup(B)` in the same heartbeat (Case D)
- `current_offer_id` transitions A → NULL → B in one heartbeat
- Voice plays the "implicit cancel, new ride starting" message

**Scenario 3 — The Ghost Hunt (false-positive resistance).**
After completing Scenario 1 or 2, drive past a previous PUDO geocode (e.g., the dropoff from Scenario 1) at any point during a subsequent ride or while idle. Stop at a red light or park briefly within 80m of that geocode. Expected behavior:
- If `current_offer_id IS NULL`: heartbeats produce `LogNoMatch` (the prior ride is no longer in the queue, so WAI has nothing to match against). No spurious fires.
- If `current_offer_id` is set to a different active ride: heartbeats produce `LogNoMatch` or `LogPickupRematch` if cluster matches the active ride's pickup. No spurious `FireDropoff` for the unrelated old PUDO.

**Verification queries** between scenarios (run from the VM):

```sql
-- After each scenario, check pudo_decision_context for the action distribution
SELECT planner_action, COUNT(*) AS n
FROM app_private.pudo_decision_context
WHERE created_at > NOW() - INTERVAL '30 minutes'
GROUP BY planner_action
ORDER BY n DESC;

-- Verify current_offer_id state matches expected after each scenario
SELECT current_offer_id, heartbeat_at AT TIME ZONE 'America/Chicago' AS last_local
FROM app_private.driver_trip_state
WHERE driver_id = '<andrew_driver_id>';
```

**Pass criterion:** all three scenarios produce the expected fire patterns. No redundant FireDropoffs. `current_offer_id` clears correctly between rides.

If validation reveals a bug: it's a wiring issue, not an architecture issue. Triage normally; the demolition is durable.

---

## 6. Recovery procedures

If Step 4 fails partway: `ROLLBACK` if still inside the BEGIN/COMMIT. The migration is atomic by design.

If Step 4 succeeds but Step 5 (deploy) reveals a fatal application bug:

```bash
# Restore the schema and data from the pre-flight pg_dump
psql -h 10.128.0.2 -U postgres -d puddlejumper -f /tmp/pre-demolition-app-private-2026-05-04.sql

# Revert the application code
git checkout patch-00566a-unified-refinement
bash deploy.sh
```

If Step 7 (field validation) reveals a non-fatal bug: fix forward on `demolition-2026-05-04`. Do not roll back the demolition; the new architecture is correct, the wiring may need adjustment.

---

## 7. Risks and known unknowns

**Risk 1: undiscovered production callers.** This document inventoried 11 Python files. If a 12th exists outside the search patterns used (e.g., a script that imports `state_machine` via a non-standard path), it will break at runtime. Mitigation: the test suite catch in Step 3 should surface most; the Bruno smoke test catches integration-level breakage; field validation catches anything else.

**Risk 2: monitor.py refactor scope drift.** Per Gemini's review, `monitor.py` is refactored (not deleted) to preserve liveness/alerting concerns. The refactor's scope is bounded: replace state-stuck-reset with stale-heartbeat detection. Any existing Discord notification, error-rate alerting, or operational tooling inside `monitor.py` is preserved as-is, with only the state-machine read/transition code paths excised. If `monitor.py` turns out to do more than expected during the rewrite, that's discovered and dealt with as part of Commit 3f rather than this demolition's scope creeping. Mitigation: read `monitor.py` end-to-end before Commit 3f to confirm the bounded scope.

**Risk 3: confirm-endpoint rewire correctness.** Manual nail goes through `_execute_action` post-rewire. Per Gemini's Q5 ratification, `_execute_action`'s `cluster` argument becomes optional; the manual-nail path passes `cluster=None` and side-effect writers fall back to the caller-supplied `lat/lng` (from the heartbeat request body or the confirm-endpoint payload) as the nailed position. Mitigation: include a unit test for the manual-nail path in Commit 3i asserting that `_execute_action(FirePickup(...), cluster=None, lat=29.x, lng=-95.y)` succeeds and writes the supplied lat/lng to `nailed_pickup_lat/lng`.

**Risk 4: `nail_it_core.compute_target` ENROUTE block deletion.** This was the last live path through `bead_on_wire.compute_target`. After deletion, nothing in `nail_it_core` calls it. `bead_on_wire.py` is then fully dead except for `classify_address`. The header rewrite captures this; full file deletion is a follow-up commit.

**Risk 5: `driver_trip_state_log` becomes write-orphaned.** `tr_log_state_transition` is dropped; nothing writes to this table after demolition. Existing rows remain queryable. Decision: this is fine; the table is preserved per Gemini's "leave the attic alone" rule for forensic data. It can be dropped in a future cleanup commit if desired.

**Risk 6: production heartbeat traffic during deploy.** If a heartbeat arrives mid-deploy with the old code reading `state` and the new schema not having `state`, the heartbeat returns 500. Cloud Run rolling deploys minimize this window to seconds. Andrew should not be driving during the deploy. (Per 2026-05-04 confirmation: Andrew is the only driver and has exclusive system access during the demolition window. Concurrent-traffic risk is effectively zero.)

---

## 8. Post-demolition deferred work

Tracked as separate commits, not part of this demolition:

1. **Delete `bead_on_wire.py`'s dead deciders** (`single_road`, `poi`, `snap_to_intersection`, `snap_to_road`, `odometer_miles_since`). After this demolition, these are fully unreferenced.
2. **Relocate `classify_address`** to `address_utils.py` or a new `address_classification.py`. Renames the load-bearing primitive into a file whose name reflects what it does.
3. **G1 odometer gate enforcement** (per `SPRINT_A_FINDINGS_2026-05-04.md` §5). Soft-confidence-penalty model: `confidence_after_g1 = confidence - penalty(driven_miles, expected_miles)`.
4. **G2a (POI adjacency) implementation.** After G1.
5. **Bruno test driver password rotation** (priority #5 in original Sprint A list).
6. **Drop `driver_trip_state_log`** if the forensic data is no longer needed. Probably 30+ days post-demolition.

---

## 9. Questions for Gemini's review (RESOLVED 2026-05-04)

All seven questions resolved by Gemini's second-pass audit. Recorded here for provenance; resolutions folded into the body of this document.

1. **Demolition completeness** → SOLID. Discord/LangGraph templates need a grep check for hardcoded state strings (folded into §4 Pre-flight item 6).
2. **Migration atomicity/locking** → Triggers → Functions → Tables → Columns ordering ratified. Add `SET lock_timeout = '5s'` to fail fast (folded into §5 Step 4 SQL).
3. **`current_offer_id_at_eval` migration** → Leave NULL. No backfill. Demolition date is the line in the sand (already encoded in §2 decision 4).
4. **`monitor.py` refactor surface** → 5-min stale heartbeat is MVP; add `gpsAgeSec > 60` while active as second predicate (folded into §3d).
5. **Confirm-endpoint cluster argument** → Option (b): refactor `_execute_action` to make cluster optional; manual nail uses `heartbeat.lat/lng` as nailed position (folded into §3d).
6. **Triangulation pricing context** → 1-to-1 swap: `if state == 'IN_TRIP'` becomes `if current_offer_id is not None`. Simple refactor, no surprise iceberg.
7. **Field validation cadence** → Three scenarios required: Clean Sweep, Houston Pivot, Ghost Hunt (folded into §5 Step 7).

---

## 10. Provenance

- 2026-05-04 morning: Sprint A findings report identified the bridge-half-built failure mode
- 2026-05-04 afternoon: ride 7706's data showed the failure mode in production (9 redundant FireDropoffs)
- 2026-05-04 afternoon: schema inventory revealed full state-machine still wired (6 functions, 5 triggers, 2 dedicated tables, 11 Python files)
- 2026-05-04 afternoon: Andrew + Gemini paired ratification of Big Bang demolition
- 2026-05-04 afternoon: five design decisions locked (§2)
- 2026-05-04 afternoon: this runbook drafted by Claude for Gemini's review
- 2026-05-04 afternoon: Gemini's first-pass review amendments incorporated — `monitor.py` refactor (not delete) for liveness preservation; `pickup_confirm.py`/`dropoff_confirm.py` rewire (not delete) to keep dev-build manual nail functional through `_execute_action`. Andrew confirmed exclusive system access during demolition window, neutralizing concurrent-traffic risk.
- 2026-05-04 afternoon: Gemini's second-pass review ratified the runbook and answered all seven §9 questions. Amendments folded in: `SET lock_timeout = '5s'` in migration SQL, `gpsAgeSec > 60` predicate added to `monitor.py` refactor scope, `_execute_action` cluster argument refactored optional with heartbeat lat/lng fallback, Discord template grep added to pre-flight, three explicit field-validation scenarios documented (Clean Sweep, Houston Pivot, Ghost Hunt). Status flipped from draft to RATIFIED.

🎩🐸🏁