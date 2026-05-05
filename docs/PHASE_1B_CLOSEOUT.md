# Phase 1B Closeout — Forensic Restoration

**Date:** 2026-05-05
**Commit:** `7fe4391` on `demolition-2026-05-04`
**Predecessor:** `574b53e` (Phase 1A transit-class adjacency gate)
**Deployed:** `puddlejumper-api-00591-vj7` (Cloud Run, us-central1)
**Test floor:** 241 → 250 (9 new, 0 regressions)
**Bruno:** 17/17 against `00591-vj7`
**Reviewer:** Gemini (PHASE_1B_PROPOSAL_v2.md ratified, Option B fold catch)
**Status:** ✅ Shipped, deployed, forensically validated end-to-end

---

## What shipped

### Schema (`migrations/2026-05-05_phase1b_forensic_restoration.sql`)

Additive-only migration on `app_private.pudo_decision_context`:

- ADD `wai_current_road_class text` — Phase 1A signal exposure
- ADD `poi_lookup_source text` — Phase 2 prep (NULL until Phase 2 wires it)
- ADD `poi_match_score real` — Phase 2 prep (NULL until Phase 2)
- ADD `poi_top_names text[]` — Phase 2 prep (NULL until Phase 2)
- WIDEN `wai_on_target_road` from `boolean` to `real` — preserves the
  0.0–1.0 score from `MatchOutcome.signals['on_target_road']` instead
  of lossy `bool(score)` collapse

Idempotent (`ADD COLUMN IF NOT EXISTS` plus a `DO $migration$` block
guarding the ALTER TYPE). Atomic (single `BEGIN/COMMIT`).

### Code

**`where_am_i.py`** — added `DiagnosticContext.outcome_for(match)` helper.
Bridges the `WAIMatch` ↔ `MatchOutcome` encapsulation gap defined by
SIMPLIFIED_ARCHITECTURE.md s10 A8. Lookup is by `(offer_id, location_type)`
against `per_target_outcomes`. Returns None defensively (test code that
constructs WAIMatch manually can legitimately get None, but every WAIMatch
returned by `evaluate()` is projected from a MatchOutcome so production
paths always succeed). +32 lines.

**`driver_heartbeat.py`** — three coordinated changes (+100 lines):

1. Added module-level `_derive_post_offer_id(executed_actions, current_offer_id)`
   helper. Implements Gemini's Deterministic Fold (Option B). Mirrors
   `dispatch.py`'s wiring rules:
   - `FirePickup(offer_id)` → `current_offer_id = offer_id`
   - `FireDropoff(...)` → `current_offer_id = None`
   - `Log*` actions → no state change
   - Case D ordering preserved (FireDropoff first, then FirePickup, fold
     yields the new offer_id)

2. Rewrote `_log_decision_context`:
   - Restored bindings for `wai_target_address`, `wai_reason`,
     `wai_on_target_road` via `top_outcome = diagnostics.outcome_for(top_match)`
   - Restored bindings for `wai_current_road`, `wai_current_road_class`,
     `wai_off_wire_duration_s` via `topo = diagnostics.topology`
   - Fixed Cut B3 double-bind bug: `current_offer_id_at_eval` now binds
     PRE-dispatch value (parameter), `current_offer_id` binds POST-dispatch
     value via `_derive_post_offer_id`
   - poi_* columns bound NULL until Phase 2 wires them
   - Defensive NULL guards: `topo.X if topo else None`, same for top_outcome

3. Renamed parameter from `actions` to `executed_actions`. Orchestrator
   passes `executed_actions` (what actually fired) instead of `actions`
   (dispatch intent). Forensic truthfulness: log what fired, not what
   was dispatched. Dispatch failures still surface via `dispatch_error`.

### Tests

**`tests/test_where_am_i.py`** — Step F: 4 outcome_for unit tests (+118 lines)
- `test_outcome_for_returns_matching_outcome`
- `test_outcome_for_disambiguates_pickup_vs_dropoff`
- `test_outcome_for_returns_none_when_no_match`
- `test_outcome_for_empty_per_target_outcomes_returns_none`

**`tests/test_heartbeat_dispatch.py`** — Step F: 5 _derive_post_offer_id
unit tests (+44 lines)
- `test_derive_post_offer_id_no_actions_returns_pre`
- `test_derive_post_offer_id_fire_pickup_binds`
- `test_derive_post_offer_id_fire_dropoff_clears`
- `test_derive_post_offer_id_case_d_dropoff_then_pickup`
- `test_derive_post_offer_id_log_actions_unchanged`

---

## End-to-end validation evidence

### Pytest
- 250 passed, 0 failed (was 241, +9 new tests)

### Bruno PUDO Simulation (Forum Park scenario)
- 17/17 against `00591-vj7`
- Exercises FirePickup case which routes through `_derive_post_offer_id`
- Forum Park has dense `houston_ways` coverage, so the on-wire snap path
  is exercised

### Live heartbeat round-trip (Andrew's McKeever walk)
On 2026-05-05 12:23:38 UTC, with Andrew physically standing 17.7m from
McKeever Road (3.0m GPS accuracy), the cluster threshold was crossed
(size=12, duration=59.8s) and `pudo_decision_context` recorded:

```
wai_current_road        = 'McKeever Road'
wai_current_road_class  = 'transit'
wai_off_wire_duration_s = 0
cluster_size            = 12
cluster_dur_s           = 59.8
```

Pre-cluster heartbeats (12:22:54 to 12:23:32) correctly recorded NULL
across all topology/match fields — the §3 step 8 invariant is honored
(row inserts even when no cluster, but with NULL values).

This is the canonical end-to-end validation: GPS → cluster → topology →
binding → DB write, all working.

---

## Things deferred (post-1B follow-ups)

### State-machine schema cleanup
DROP COLUMN on the 6 vestigial columns left over from the demolished
state machine:
- `primary_offer_id` (state machine's primary vs current dual identifier)
- `wai_status` (state-machine status labels)
- `planner_target_state` (transition target)
- `planner_corrected_lat` (planner outputs, separate concern)
- `planner_corrected_lng`
- `planner_reason`

Cut B3's docstring (now removed in 1B) explicitly anticipated this
cleanup. Kept additive-only in 1B because DROP COLUMN is irreversible
and we may want the historical data for forensic replay of pre-demolition
heartbeats.

### `current_offer_id` rename
Andrew's observation: "current" is misleading because pre-pickup we
have a queue but no committed offer. The 1-bit memory is actually
"has a pickup fired since the last dropoff?" — and `current_offer_id`
is the witness of *which* pickup. More accurate names: `committed_offer_id`,
`bound_offer_id`, `in_flight_offer_id`. Touches multiple tables and
many code sites; its own commit.

### Per-target outcome fan-out logging
Currently `_log_decision_context` only logs the top match's outcome.
The full `per_target_outcomes` is available on the DiagnosticContext.
A future column (JSONB or sidecar table) could capture every match
attempt for each heartbeat. Out of 1B scope.

### `stop_context` logging
DiagnosticContext carries `stop_context` (Stop Atlas v1.1 stub) but
1B doesn't log it. Defer until Stop Atlas matures past stub state.

### Confidence Heatmap dashboard
Gemini's suggestion: a visualization of where WAI is most confident vs
fragile. Visualization concern, separate from forensic surface work.

---

## Lessons logged this sprint

### L-12: Test 5e missed L-6 corollary
When writing `LogAmbiguousMatch(())` in Test 5e, I passed an empty tuple
to a constructor whose actual signature is `(candidates, reason)`. The
existing test file already had 4+ correct constructions of `LogAmbiguousMatch`
in the parametrize section that I should have grepped before authoring.
Cost: 2 hotfix iterations, ~10 minutes.

**Mitigation rule:** Before constructing a class in a new test, grep the
test file for existing constructions of that class. If any exist, mirror
their pattern verbatim.

### L-13: scp can produce 0-byte files silently
`tmp/apply_phase1b_step13_hotfix_v2.py` arrived on the VM as 0 bytes
(reason unclear — possibly a UI hiccup during download or scp). Running
`python3` on a 0-byte file is a clean no-op (exit 0, no output). The
&& chain continued without flagging anything. The bug was only caught
when a downstream test still showed pre-hotfix state.

**Mitigation rule:** For tiny (1-3 line) edits, prefer inline `python3 -c '...'`
over file scripts to avoid scp transfer issues. For larger patches,
add a sanity check at the start of any apply script that validates its
own size (e.g., `assert len(__doc__) > 100, "script appears truncated"`).

### L-14: Time window estimation needs explicit boundaries
When validating Andrew's McKeever walk, I estimated the wrong time window
(12:25:30–12:27:00) and queried for empty data. The actual stand was
12:23:05–12:23:27, found only by sorting heartbeats by distance to McKeever.

**Mitigation rule:** When testing a known-time event, ask for the user's
explicit timestamp, or query the broadest plausible window first and let
the data narrow itself.

### L-15: grep -c returns exit 1 on zero matches
A pre-flight check using `grep -c PATTERN FILE` in a `&&` chain broke
the chain when the count was 0. grep returns exit 1 for "no matches"
even though the count itself was correctly printed.

**Mitigation rule:** For pre-flight count checks in `&&` chains, use
`grep -c ... || echo 0` or `grep -q ... && echo found || echo absent`.
Don't rely on grep's exit code reflecting "did the command succeed?"
because zero-match is reported as failure.

---

## Branch state

```
HEAD -> demolition-2026-05-04
7fe4391 phase 1b: forensic restoration of pudo_decision_context  ← THIS SPRINT
574b53e phase 1a: transit-class adjacency gate (Operation Strip Mall)
e61054c demolition 3i: test suite realignment + dangling reference cleanup
ab0b2bf demolition 3h: delete dead modules + driver_state_reset endpoint
6c702a8 (tag: pre-demolition-2026-05-04) state machine demolition plan
```

Branch is local-only. `git push origin demolition-2026-05-04` is a
deferred-cleanup item.

---

## Next sprint: Phase 2 (Operation Strip Mall main course)

Per `PHASE_1B_PROPOSAL_v2.md` Section 7 and the original handoff brief
Section 4. Fresh-sprint scope, ~4-8 hours. Recommended in a new chat.

Locked design decisions Phase 2 inherits:
- 80m radius cache (not H3 hex bucket)
- 80/20 business-name/street-number fuzzy match weighting
- 0.30 confidence floor when poi_match >= 0.8 (down from 0.40)
- GiST geography column for `poi_cache`
- 30-day TTL on cache entries
- rapidfuzz==3.10.1 (already in requirements)
- Distance-weighted dedupe (Option C) when multiple cache rows in range

Phase 2 lands its bindings into the columns Phase 1B just opened
(`poi_lookup_source`, `poi_match_score`, `poi_top_names`).