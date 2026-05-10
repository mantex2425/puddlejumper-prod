# Atomic GC Predicate — Sprint Handoff (P0 Launch-Blocker)

**Date authored:** 2026-05-10 (afternoon)
**For:** A fresh chat session on the puddle-jumper VM
**Authoring chat:** Sprint 1 verification + GC architectural diagnosis
**Status:** Apply script drafted (`tmp/apply_atomic_gc_predicate_2026-05-10.py`),
ratified by Andrew + Gemini, NOT YET APPLIED, NOT YET TESTED, NOT YET DEPLOYED.

---

## TL;DR

Three queries against `app_private.offer_history` were missing the wall-clock
GC predicate. Result: stale anchors (offers from days ago) were poisoning the
TAD distance computation for every present-tense offer, causing the dispatch
path to silently reject every match for 3 days straight. Auto Nail It hasn't
fired since 2026-05-08.

This handoff brings the new chat through:

1. **The diagnosis** (so it doesn't have to re-derive it).
2. **The apply script** (already drafted; located at
   `~/puddlejumper-prod/tmp/apply_atomic_gc_predicate_2026-05-10.py`).
3. **The verification gates** (pytest + tests-to-write + manual SQL +
   driving validation).
4. **The deferred work** (5 more `offer_history` query sites that need
   the same predicate, deferred to Sprint 3).
5. **The relationship to Sprint 2 (UUID schema migration)** — independent;
   this work is BEFORE Sprint 2 and stands alone.

---

## Context: How we got here

**Sprint 1** shipped today (2026-05-10) on `phase-2c-2-tad-exit-4tools`:

```
1a838c6  docs: index Identity Genesis Sprint 1 (server-side consumer)
0bfc166  test(decisions): unit tests for offerId validation and persistence
57f0c75  feat(decisions): persist offer_id into decision_log.trace_data
f72d908  feat(decisions): accept and validate offerId UUIDv7 from Android
ba92c32  docs: add Server-Side Sprint 1 evidence brief
a5618e6  docs: add Server-Side Sprint 1 brief and state handoff
```

577/577 pytest green. Cloud Run rev `puddlejumper-api-00601-kh4` deployed.
Sprint 1 verification gate: one real offer drove through cleanly:
`019e1292-6644-7d68-99e6-3c610de89003`, $12.41, mint→create 320ms.

After Sprint 1 was verified, Andrew asked to drive a full session to
validate Auto Nail It on real PUDOs. Forensic SQL on `offer_history` and
`pudo_decision_context` revealed:

- **Yesterday's session:** 7 ACCEPTed offers, 0 `actual_pickup_at` writes
- **Last 30+ hours:** 1898 PDC rows, all `current_offer_id = NULL`,
  all `planner_action = NULL`, zero dispatches
- **Daily PDC by-day breakdown:** May 5 had 7689 actions, May 6 had 38,
  May 7 had 108, May 8 onward = 0. **Step-function collapse.**

Two suspect deploys correlate:
- `00593-khf` / `00594-ph5` (May 5/6) — Sprint A gate layer landed
  (`c784e64` "motion + odometer gates, receipt schema")
- `00596-z7x` (May 8) — α-fix landed (`88c04e8` "realign _execute_action
  SQL to offer_history.id semantics")

Forensic dive into `pudo_decision_context.tad_decision_context` JSONB blob
revealed the actual cause:

```json
{
  "verdicts": {
    "7788": {
      "passed": false,
      "distance_gate": {
        "fail_reason": "delta_from_expected -0.782mi outside +-0.5mi",
        "expected_distance_miles": 1.32,
        "actual_delta_miles": 0.5381552416459908
      }
    }
  },
  "last_known_anchor_id": "7136"
}
```

`last_known_anchor_id: "7136"` is a `decision_log.id` from APRIL 26 — 14
days before the heartbeat that consulted it. The TAD distance gate was
computing `expected_pickup_distance` against a 14-day-old anchor's
odometer reading, producing nonsense expectations (1.32mi when the real
trip was different), then rejecting every match for being "premature."

**Why was 7136 returned?** The function `_get_last_known_anchor_id` in
`driver_heartbeat.py` queries `offer_history` for the most-recent row
with any `actual_pickup_at` or `actual_dropoff_at` — with **no
wall-clock predicate**. Compare to `DriverQueue._project_offers` which
correctly applies the wall-clock horizon. **Two functions, same table,
incompatible freshness rules.**

Audit revealed a third site with the same pattern:
`decisions/logger.py:~85` — the `prev_offer` SELECT that feeds
`compute_offer_expectations` with `prev_expected_dropoff_distance`.
Same shape: no GC predicate. Returns stale anchors. Poisons every new
offer's `expected_pickup_distance` calculation via the chain.

**Andrew's architectural insistence:** "We shouldn't bandage this with
a freshness check at each call site. The GC should already be doing this
work. It's a property of the data, not a property of the call site."
Gemini concurred. Single canonical predicate exported from
`driver_queue.py`, applied at every hot-path call site.

Gemini further specified the predicate must be **two-axis**: time AND
distance. Time-axis-only mirrors what's currently in `_project_offers`;
distance-axis adds protection against drivers who didn't move (or moved
opposite direction) for an "expired" period. Distance axis gracefully
degrades to time-only when `current_cumulative_miles` or
`miles_at_offer_receipt` is NULL.

---

## The fix in 4 components

### 1. The Atomic Liveness Predicate

Exported from `driver_queue.py`. SQL fragment + Python helper for params.

**Time axis** (always enforced):
```
created_at + LEAST(GREATEST(raw_min * 1.25, 15), 240) * INTERVAL '1 minute' > NOW()
where raw_min = COALESCE(pickup_minutes, 15) + COALESCE(trip_minutes, 30)
```

**Distance axis** (graceful degradation):
```
current_cumulative_miles IS NULL
OR miles_at_offer_receipt IS NULL
OR (current_cumulative_miles - miles_at_offer_receipt) < distance_horizon
where distance_horizon = LEAST(GREATEST(raw_dist * 1.25, 2.0), 50.0)
      raw_dist = COALESCE(pickup_miles, 4.0) + COALESCE(trip_miles, 8.0)
```

**Completion status:** `actual_dropoff_at IS NULL`.

### 2. Three call sites refactored to use the predicate

| File | Function | Behavior change |
|---|---|---|
| `driver_queue.py` | `_project_offers` | None — already had time axis; gains distance axis when caller passes `current_cumulative_miles` |
| `driver_heartbeat.py` | `_get_last_known_anchor_id` | YES — stale anchors now return None |
| `decisions/logger.py` | prev_offer SELECT | YES — stale prev returns None → fresh-start TAD anchor |

### 3. Test additions

(Authored fresh in the next chat — apply script does NOT include these
because tests need careful authoring against the actual DB-mock
patterns in the codebase. See "Tests to write" below.)

### 4. CANONICAL_RULES amendment

Add to Section IV (Logic Rules) — a new rule:

> **Wall-clock GC is canonical.** Every production hot-path query against
> `app_private.offer_history` MUST apply `LIVE_OFFER_PREDICATE_SQL` from
> `driver_queue.py`. No exceptions in production code. Out-of-band scripts
> (replay, backtest, harvest) may query historical data without the
> predicate, but each such call site must be documented in the
> `out_of_band_offer_history_queries.md` registry.

The amendment text is hand-written (not in the apply script) — see
"Manual edits" below.

---

## Apply script — what it does and what it doesn't

**Location:** `~/puddlejumper-prod/tmp/apply_atomic_gc_predicate_2026-05-10.py`

**It does:**
- L-3 envelope (Phase 1 verify + idempotency, Phase 2 in-memory transform
  with delta gate, Phase 3 atomic write + sentinel sweep)
- Edits 3 production files: `driver_queue.py`, `driver_heartbeat.py`,
  `decisions/logger.py`
- Asserts unique anchor matches before any write
- Forwards `current_cumulative_miles` through the call chain (snapshot,
  offers, _project_offers public method gain optional parameter)
- Imports the predicate at the right module boundaries

**It does NOT:**
- Author tests (next chat does this — see "Tests to write")
- Touch `docs/CANONICAL_RULES.md` (next chat hand-writes the amendment)
- Run the cleanup SQL for `current_offer_id = '7770'` (next chat
  decides timing — recommendation: AFTER deploy, BEFORE first drive)
- Modify the deferred Category-A query sites
  (`driver_heartbeat.py:474, 525, 559`, `nail_it_core.py:131, 191`) —
  Sprint 3 work

---

## Apply script verification gates

After running the apply script:

```bash
cd ~/puddlejumper-prod
source venv/bin/activate
python3 -m pytest -x --tb=short
git diff --stat
```

**Expected:**
- pytest: 577 passes (NO new tests yet from this apply — those come next).
  If pytest drops below 577, the apply broke something. Check
  `git diff` to see what changed and roll back via
  `git checkout -- driver_queue.py driver_heartbeat.py decisions/logger.py`.
- git diff stat: 3 files modified, ~80 insertions, ~30 deletions
  (rough — actual numbers from the apply script's Phase 2 delta report).

If pytest passes, hand-write the tests below, then commit.

---

## Tests to write (P0 acceptance gate)

Two new test files (or additions to existing):

### Test A: `tests/test_driver_queue.py` additions

Validate the new `current_cumulative_miles` parameter and distance-axis
behavior:

```python
def test_project_offers_excludes_distance_aged_offer():
    """Offer past distance horizon is excluded even if within time horizon."""
    # Arrange: insert an offer with miles_at_offer_receipt=10.0,
    # pickup_miles=2, trip_miles=3 (so distance_horizon = 6.25mi).
    # current_cumulative_miles = 100.0 (driver moved 90mi past the receipt).
    # Time horizon = ~31min, offer was 10min ago (within time).
    # Expected: distance axis excludes it. _project_offers returns ().

def test_project_offers_includes_when_distance_signal_missing():
    """When current_cumulative_miles is None, distance axis falls back to time-only."""
    # Same offer as above, but call with current_cumulative_miles=None.
    # Expected: time axis still passes (within 31min), offer is included.

def test_project_offers_excludes_time_aged_offer():
    """Existing time-axis behavior preserved (regression coverage)."""
    # Offer 1 hour old, raw_min=20 → window=25min. Excluded.
```

### Test B: `tests/test_driver_heartbeat_3b_r.py` additions

Validate the GC-aware anchor function:

```python
def test_last_known_anchor_returns_none_when_only_stale_anchors_exist():
    """Stale anchors (past time+distance horizon) excluded; returns None."""
    # Insert offer_history row with actual_pickup_at=NOW() - 14 days.
    # Call _get_last_known_anchor_id(cur, driver_id, current_cumulative_miles=100).
    # Expected: returns None (was the bug — used to return that stale anchor).

def test_last_known_anchor_returns_live_anchor():
    """Live anchor (within horizons) returned correctly."""

def test_last_known_anchor_returns_none_when_no_anchors_exist():
    """Empty table case (regression coverage)."""
```

### Test C: `tests/test_logger_trace_data.py` or `tests/test_offer_history_anchors.py` additions

Validate the prev_offer SELECT in `decisions/logger.py`:

```python
def test_prev_offer_select_excludes_stale_offers():
    """Stale offers are NOT returned as prev_offer for compute_offer_expectations."""
    # Insert stale offer_history row. Run log_decision.
    # Mock compute_offer_expectations to capture its call args.
    # Assert prev_expected_dropoff_distance=None was passed (not the stale value).
```

**All three test files use the existing mock patterns** (see
`test_logger_trace_data.py` and `test_driver_heartbeat_3b_r.py` for
prior art). Don't reinvent fixtures.

---

## Commit sequence (after tests pass)

3 atomic commits:

```bash
# Commit 1: the predicate + refactor
git add driver_queue.py driver_heartbeat.py decisions/logger.py
git commit -m "fix(gc): atomic liveness predicate for offer_history reads

Extracts LIVE_OFFER_PREDICATE_SQL from DriverQueue._project_offers and
applies it at three call sites that were missing the wall-clock GC
predicate:

  - DriverQueue._project_offers (refactored — no behavior change)
  - driver_heartbeat._get_last_known_anchor_id (was: no GC at all)
  - decisions.logger prev_offer SELECT (was: no GC at all)

Predicate enforces time horizon (always) and distance horizon (when
current_cumulative_miles supplied, gracefully degrades otherwise).

Diagnosed 2026-05-10 from pudo_decision_context.tad_decision_context
JSONB blob: stale anchors (offers 14+ days old) were poisoning every
present-tense offer's expected_pickup_distance, causing TAD distance
gate to reject all matches. Auto Nail It silent since 2026-05-08.

Fix per Andrew + Gemini ratification: 'GC happens at one place; reads
downstream trust that GC ran.' New canonical predicate, single source
of truth, exported and reused.

Five remaining offer_history query sites (driver_heartbeat.py:474, 525,
559; nail_it_core.py:131, 191) deferred to Sprint 3 audit — they're not
on the dispatch-binding critical path."

# Commit 2: tests
git add tests/test_driver_queue.py tests/test_driver_heartbeat_3b_r.py \
        tests/test_logger_trace_data.py
git commit -m "test(gc): atomic liveness predicate coverage"

# Commit 3: canonical rules
git add docs/CANONICAL_RULES.md
git commit -m "docs(canonical): mandate LIVE_OFFER_PREDICATE_SQL for hot-path reads"
```

---

## Manual edits (next chat hand-writes)

### `docs/CANONICAL_RULES.md` — Section IV addendum

Locate Section IV (Logic Rules — Post-Demolition). Add a new bullet:

> **Wall-clock GC is canonical.** Every production hot-path query against
> `app_private.offer_history` MUST apply `LIVE_OFFER_PREDICATE_SQL` from
> `driver_queue.py`. No bespoke freshness rules at call sites; the
> predicate is the single source of truth for "is this offer still
> live." Exceptions limited to out-of-band scripts (replay, backtest,
> harvest, drive_review) which document their reason in
> `docs/out_of_band_offer_history_queries.md`.

Same insertion shape as the existing rule bullets. Per
SESSION_PROTOCOL paste-safety, transfer via `create_file` →
`present_files` → scp; do NOT paste markdown blockquote content directly
into bash.

---

## Deploy + verification sequence

1. **Apply script:** `python3 tmp/apply_atomic_gc_predicate_2026-05-10.py`
2. **pytest baseline:** 577 (or the apply rolls back any failure)
3. **Hand-write tests** per Tests to Write above (3-5 new tests)
4. **pytest with new tests:** 580-582 expected
5. **Manual canonical rules edit** per above
6. **Three commits** per above
7. **Deploy:** `bash deploy.sh`
8. **Watch for new revision** in `gcloud run services describe`
9. **Manual SQL — clear stale `7770`:**
   ```sql
   UPDATE app_private.driver_trip_state
   SET current_offer_id = NULL,
       pickup_h3 = NULL, pickup_lat = NULL, pickup_lng = NULL,
       nailed_pickup_lat = NULL, nailed_pickup_lng = NULL,
       nailed_dropoff_lat = NULL, nailed_dropoff_lng = NULL
   WHERE driver_id = 'UjT1hE9eBXh2q95aSZYOkzDJ8lo1'
     AND current_offer_id = '7770';
   ```

   Run AFTER deploy. This clears the residue of an April 26 offer that
   never got refreshed.

10. **Watch the next heartbeat** in Cloud Run logs. Look for:
    - INVARIANT_VIOLATION warnings should STOP firing (no stuck pointer
      to find a violation against)
    - `pudo_decision_context.current_offer_id_at_eval` should populate
      with the *current* live offer's ID, not NULL
    - `tad_decision_context` JSONB should show `last_known_anchor_id: null`
      (or a fresh anchor) instead of `"7136"`

11. **Drive one real offer.** Watch `actual_pickup_at` populate on the
    `offer_history` row. That's the close of the dispatch-path
    verification gate.

---

## What's deferred (Sprint 3 / later)

**Five Category-A query sites against `offer_history`** that are also in
production hot path but NOT on the dispatch-binding critical path:

- `driver_heartbeat.py:474` — `_build_per_offer_state` TAD context assembly
- `driver_heartbeat.py:525` — (read source for context — likely TAD-related)
- `driver_heartbeat.py:559` — (read source for context — likely Lost Mode)
- `nail_it_core.py:131` — manual nail path
- `nail_it_core.py:191` — manual nail path

Each needs to be read in context, then either:
- Refactored to use `LIVE_OFFER_PREDICATE_SQL`, OR
- Documented in `out_of_band_offer_history_queries.md` with reason for
  exception

That's its own audit sprint. Not blocking launch.

**Two Category-B sites are intentionally NOT GC-aware** (the α-fix
translation subqueries at `driver_heartbeat.py:191, 252, 282, 341`).
They translate `action.offer_id` → `decision_log_id` for downstream
FK targeting and need to find rows regardless of GC horizon. Don't
touch.

---

## Sprint 2 relationship

Sprint 2 = UUID v7 schema migration (Identity Genesis production cutover).
That work is independent of this P0 fix:

- This P0 fix is on the existing integer-ID schema. After this fix
  ships, production uses integer IDs with proper GC.
- Sprint 2 changes the schema to UUID-keyed identity. Sprint 2's
  migration SQL will need to apply the predicate to the new schema's
  equivalent fields, but the *architectural pattern* (one canonical
  predicate, exported, reused) stays the same.
- Sprint 2 is NOT blocked by this P0 fix succeeding. It's blocked by
  the producer-side verification gate (5 real offers, in progress).

If this P0 fix lands tonight and the verification gate closes by
morning, Sprint 2 (UUID migration) opens tomorrow as planned.

---

## Files / artifacts new chat needs

Pre-load (fresh `cat` from VM):

- This brief: `docs/SPRINT_HANDOFF_ATOMIC_GC_PREDICATE_2026-05-10.md`
- Apply script: `tmp/apply_atomic_gc_predicate_2026-05-10.py`
- Standard load: `docs/CANONICAL_RULES.md`, `docs/SESSION_PROTOCOL.md`,
  `docs/INDEX.md`
- Identity Genesis: `docs/IDENTITY_GENESIS_DESIGN_2026-05-09.md`
  (for context on Sprint 2 sequencing)

Reference material if needed:

- `docs/X3_FINDINGS_2026-05-09.md` (root cause forensic — already
  superseded by this work but useful background)
- `docs/SERVER_SIDE_SPRINT_1_BRIEF_2026-05-09.md` (Sprint 1 — already
  shipped)
- `docs/SERVER_SIDE_SPRINT_1_EVIDENCE_2026-05-09.md` (real-hardware
  validation, useful for understanding the 320ms latency signal)

---

## End of brief

Hand to new chat with the kickoff prompt:

> "Loaded: SPRINT_HANDOFF_ATOMIC_GC_PREDICATE_2026-05-10.md.
> Standard load: CANONICAL_RULES, SESSION_PROTOCOL, INDEX.
>
> Mission: P0 launch-blocker — apply the atomic GC predicate fix per
> the handoff brief. The apply script is at
> tmp/apply_atomic_gc_predicate_2026-05-10.py. Author the three test
> additions per the brief's Tests to Write section, then the
> CANONICAL_RULES amendment, three commits, deploy, manual SQL clear
> 7770, and the verification drive.
>
> Per session protocol: recon first. Confirm git HEAD is at 1a838c6
> (Sprint 1 final), pytest is at 577 baseline, and the apply script
> exists at tmp/. Then propose the apply sequence for ratification."

