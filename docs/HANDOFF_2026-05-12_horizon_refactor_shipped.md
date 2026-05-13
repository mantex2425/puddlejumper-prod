# Handoff — Commit A shipped, Houston Playback pending

**Date:** 2026-05-12 (late night)
**Branch:** `phase-2c-2-tad-exit-4tools`
**Pytest floor:** 584 → 592 (8 new tests across two refactors)
**Cloud Run:** unchanged — `puddlejumper-api-00604-2rs` still serving 100%
**State:** uncommitted on the VM. NOT YET DEPLOYED.

---

## What shipped tonight

Two refactors, both Rule VII compliance:

### Refactor 1 — Horizon-physics `_detect_lost_mode`

`driver_heartbeat.py::_detect_lost_mode` previously used
`actual_pickup_at IS NULL` as a "narrative broken" proxy plus
`interval '2 hours'` as a wall-clock window. Both were wrong.

Failure modes today's drive exposed:

**Houston Miss (offers 7848, 7853).** AAI missed the pickup observation
but dropoff fired cleanly. Old rule treated the offer as a permanent
ghost for 2 hours after dropoff fired, poisoning every subsequent
heartbeat with `lost_mode=true` and cascading missed pickups
throughout the rest of the drive.

**Calhoun Zombie.** Pickup fires but the dropoff address Uber gave
doesn't exist where navigation takes you. AAI never sees dropoff.
Old rule's `actual_pickup_at IS NULL` clause excluded the offer
entirely — wrong direction; Calhoun is exactly the ghost the rule
was meant to catch.

New rule uses `LIVE_OFFER_PREDICATE_SQL` directly:

> An offer triggers lost_mode iff
>   accepted
>   AND not in the active queue
>   AND still inside its time/distance horizon

The predicate's first clause is `oh.actual_dropoff_at IS NULL`, so
Houston Miss is excluded automatically. Pickup-fired-no-dropoff
offers stay in lost_mode until the horizon blows — physics, not
fire state, terminates the narrative.

### Refactor 2 — Explicit-clock predicate

`LIVE_OFFER_PREDICATE_SQL` no longer references `NOW()`. The clock is
explicit data:

- `live_offer_predicate_params(current_cumulative_miles, reference_time)` —
  both args required, no defaults. Returns 13-tuple.
- `_get_last_known_anchor_id(cur, driver_id, current_cumulative_miles, reference_time)` —
  same. All required.
- `_detect_lost_mode(cur, driver_id, queue_offer_ids, current_cumulative_miles, reference_time)` —
  same.
- `post_heartbeat` captures `_heartbeat_now = datetime.datetime.now(datetime.timezone.utc)`
  once at the start of the prelude and threads it through both queries.
  Both queries within a single heartbeat see the same "now."
- `decisions/logger.py` and `driver_queue.py` each capture their own `now`
  at call time.

The predicate is now a pure function. Same inputs always produce the
same answer regardless of server clock. Replay harnesses (Commit B,
the Houston Playback) can anchor at any historical moment.

---

## Files touched (uncommitted on VM)

```
M driver_queue.py
M driver_heartbeat.py
M decisions/logger.py
M tests/test_driver_heartbeat_3b_r.py
M tests/test_driver_queue.py
M tests/test_offer_history_anchors_live.py
?? tests/test_live_offer_predicate_imports.py   (new, 8 tests)
?? tests/test_lost_mode_horizon.py              (new, 5 stubs - intentional skips)
?? docs/HANDOFF_2026-05-12_pickup_miles_truth_shipped.md  (from earlier today)
?? docs/HANDOFF_2026-05-12_horizon_refactor_shipped.md   (this file)
```

## Test posture

```
592 passed
  5 skipped  — tests/test_lost_mode_horizon.py spec stubs (intentional)
              waiting on Commit B's Houston Playback to make them real
  3 warnings — unrelated langchain deprecations
```

`grep -n 'NOW()' driver_queue.py driver_heartbeat.py decisions/logger.py`
shows ~20 hits. Every one is a write-path timestamp (`actual_pickup_at = NOW()`,
heartbeat_at = NOW()`, denormalized DOY/DOW columns) or a docstring
mentioning NOW() by name. **Zero hits inside any predicate that decides
"is this offer alive."**

---

## Pending: Commit B — Houston Playback (next session)

The whole point of explicit-clock is to enable this test. Goal:

> Read today's real `offer_history` rows for offers 7848/7850/7852/7853/7854,
> replay heartbeats at synthetic timestamps walking through the afternoon
> (17:03 → 20:35 UTC), and assert `_detect_lost_mode` returns the right
> answer at every key moment.

Specifically:

- **17:03** (start of drive, no offers yet) → `_detect_lost_mode = False`
- **18:06** (7848 accepted) → True if checked before 7848's horizon, False after
- **18:38:48** (7848 dropoff fires) → False from this moment forward as long as no
  other offer is in horizon
- **18:55:41** (7852 pickup fires — first successful pickup of the day) → False
- **19:36** (7853 accepted, destination mode) → True briefly until 7853 fires
- **20:14:20** (7853 dropoff fires) → False
- **20:35** (end of drive) → False

The test runs against the real DB via the existing live-test pattern
(`tests/test_offer_history_anchors_live.py`). Today's rows stay queryable
because the test passes `reference_time` explicitly — no dependency on
wall-clock advance.

**Test file:** `tests/test_lost_mode_houston_playback_live.py`

Architecture: parametrize over (timestamp, expected_lost_mode, expected_anchor)
tuples. Each parametrized case calls `_detect_lost_mode` and
`_get_last_known_anchor_id` against the real DB with explicit `reference_time`
and `current_cumulative_miles` (the latter is harder — see below).

### Open question for next session

**`current_cumulative_miles` source.** For each playback timestamp, what
odometer reading do we use? Three options:

1. **Read from `pudo_decision_context.odometer_gate_result`** — the real
   odometer at each heartbeat is captured there in `.7848.pickup.odo`,
   `.7852.pickup.odo`, etc. Most accurate; uses today's actual data.
2. **Linearly interpolate** between known fire-time odometers
   (`cumulative_miles_at_pickup_fire`, `cumulative_miles_at_dropoff_fire`
   on offer_history).
3. **Pass `None`** and accept time-only evaluation. Simpler but loses
   distance-axis coverage.

Recommend option 1. Worth opening with a quick query to extract the
odometer-vs-time trace from today's heartbeats.

---

## Architecture state changes that need to land in CANONICAL_RULES.md

Should be done at first opportunity but not blocking:

1. **§H update:** the live-offer-predicate single-source-of-truth
   contract now extends to 5 production call sites (was 3). The
   structural-contract test catches drift, so no immediate risk, but
   the doc should reflect reality. New call sites:
     - `driver_queue.offer_ids_only`
     - `driver_queue._project_offers`
     - `driver_heartbeat._get_last_known_anchor_id`
     - `driver_heartbeat._detect_lost_mode` ← NEW tonight
     - `decisions/logger.py:108`

2. **New section needed: "Explicit Clock Discipline."**
   `LIVE_OFFER_PREDICATE_SQL` does not reference `NOW()`. The clock
   is data passed via `live_offer_predicate_params(.., reference_time)`.
   Production callers capture `datetime.now(timezone.utc)` once per
   request/heartbeat and thread through. The structural-contract test
   (`test_predicate_does_not_use_server_clock`) enforces this; any PR
   reintroducing `NOW()` into the predicate fails CI.

3. **Test pattern: live-DB vs synthetic.** Tonight established that
   `tests/test_offer_history_anchors_live.py` (live DB with
   savepoint/rollback) is the right pattern for predicate-semantic
   tests. Conftest provides `db_cur`, `test_driver_id`,
   `seed_decision_log`, `seed_offer_history`. Janitor cleans up
   `TEST_GC_*` driver_ids at session teardown. The forthcoming
   Houston Playback test follows this pattern but reads real (not
   seeded) `offer_history` rows.

---

## Deployment status

**No production deploy tonight.** Cloud Run `00604-2rs` is still
serving the pre-refactor code. The diff is committed-on-VM,
test-green, but:

1. Gemini should review the diff before push
2. Commit B (Houston Playback) should land before deploy — provides
   regression net against today's specific failure modes
3. Deploy in daylight when Andrew can monitor first few real heartbeats

Diff to review:
```
cd ~/puddlejumper-prod
git diff driver_queue.py driver_heartbeat.py decisions/logger.py tests/
git status   # see new test files
```

---

## What went smoothly tonight

- Two refactors landed surgically with idempotent apply scripts
- Each apply: L-3 envelope (preflight / transform / write / verify)
- Test floor went 584 → 590 → 592 across both commits, no regressions
- L-6 grep done properly before signature changes (5 prod call sites
  + 4 test files inventoried before any byte was changed)
- Structural-contract test prevented us from ever reintroducing NOW()

## What I'd flag for next time

- The first `_detect_lost_mode` verification phase was over-strict:
  checked SQL strings without stripping the docstring, false-positived
  on phrases describing the old rule. Pattern: future verification
  should `re.sub(r'"""(?:.|\\n)*?"""', '', body, count=1)` before
  asserting on SQL content.
- L-22 chat-rendering bit me once with a Python heredoc anchor that
  had subtle whitespace differences. Switching to sed-by-line-number
  fixed it. The `present_files` → `scp` → run-on-VM flow is solid;
  the brittleness was in my inline-heredoc shortcut, not the apply
  scripts.
- One scoping bug in my `patch_anchors_live` step: inserted
  `import datetime` inside a function where it was already imported
  at module top → poisoned the function's local scope → UnboundLocalError
  on the line above. Fix pattern: always check module-top imports
  before inserting local ones.

---

## Quick-start for next session

```
1. Open with this brief + CANONICAL_RULES.md + session_protocol.md
2. Confirm: cd ~/puddlejumper-prod && git status (should match
   the file list above)
3. Confirm: python3 -m pytest tests/ -x (should show 592 passed, 5 skipped)
4. First recon for Commit B: extract today's odometer/timestamp trace
   from pudo_decision_context for driver UjT1hE9eBXh2q95aSZYOkzDJ8lo1
   on 2026-05-12, then design the parametrized Playback test.
5. After Playback lands and passes: git commit both refactors + the
   playback test as separate commits, hand to Gemini for ratification,
   then deploy.
```
