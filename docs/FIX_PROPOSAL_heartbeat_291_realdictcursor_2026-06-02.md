# Fix Proposal — `driver_heartbeat.py:291` RealDictCursor `KeyError: 0`

**To:** Gemini (review), then CC (apply via L-3 envelope)
**From:** Claude + Andrew
**Date:** 2026-06-02
**Target:** `driver_heartbeat.py` line 291, inside `_execute_action` (Rule XV narrative-catch-up guard)
**Severity:** P0-class — unhandled exception crashing the `FirePickup` handler on the deployed tree (`00639-ccg`). Observed in prod 2026-06-02 at 19:02:08–19:02:14 UTC, 5 occurrences ~1.5s apart (heartbeat loop re-hitting every tick).

---

## 1. The defect (proven, not inferred)

```python
# line 277
cur.execute("""
    SELECT actual_pickup_at FROM app_private.offer_history
    WHERE id = %s::bigint
""", (action.offer_id,))
existing_row = cur.fetchone()        # line 280
...
already_fired_at = existing_row[0]   # line 291  ← KeyError: 0
```

`cur` is opened with `cursor_factory=RealDictCursor` (`driver_heartbeat.py:1648`; also the `db.py` connection default at `:16`/`:39`). A `RealDictCursor` returns rows keyed by **column name**, so `existing_row[0]` looks up a key literally named `0`, which does not exist → `KeyError: 0`.

This is a known footgun already documented in this same file at line 1800: *"RealDictCursor: iteration yields KEYS not values; use key access."* Line 291 is an unconverted straggler that pre-dates or missed that cleanup.

**Scope confirmed single-line.** A sweep for positional row-indexing (`grep -nE '(_row|fetchone\(\)|fetchall\(\)|\brow)\s*\[[0-9]+\]'`) returns **only line 291**. Known grep limitation: an aliased row under a non-`*row` variable name indexed positionally would not be caught; judged low-risk here (291 was the sole ERROR class in the fire window, and the file otherwise uses key access), but stated as a limit, not a proof of total absence.

---

## 2. The fix

```python
# line 291
- already_fired_at = existing_row[0]
+ already_fired_at = existing_row['actual_pickup_at']
```

The SELECT projects exactly one column, `actual_pickup_at`, so the key is unambiguous.

---

## 3. Why it matters beyond the traceback

This line is the **Rule XV narrative-catch-up guard** (comment block lines 254–276). Its purpose: when a `FirePickup` (narrative) arrives for an offer whose pickup was already fired by a prior `FirePickupObservation` during lost-mode, the guard must **preserve the observation's true-location cache write** (`offer_history.actual_pickup_*`, `pms`, `community_offers`) and bind narrative only — NOT overwrite the cache with the later, possibly miles-away `FirePickup` position. It exists to prevent the 1.8mi/10-min drift corruption documented in `docs/RECON_IMPERIAL_VALLEY_PICKUP5_2026-05-30.md` (pickup-5 of the 2026-05-30 drive).

When line 291 raises, **the guard crashes before reaching the bind+lock logic** at 292+ (`write_nailed_position`, `queue.bind`, `acquire_lock`). Consequences:

- The `FirePickup` handler throws; the narrative-catch-up never completes.
- The 5×/1.5s repetition is the heartbeat loop re-entering the same crash every tick because nothing settles.
- **Leading (unproven) hypothesis:** an unhandled exception surfacing a 500 to the Android client at a fire moment is a candidate mechanism for the red-screen "alarm" observed today on dropoff confirms (8851, 8857). NOT yet proven — the sentinel recon was paused to fix this higher-severity crash. To be confirmed/refuted after this fix by checking whether the red screen recurs and whether the alarm condition is server- or client-side.

---

## 4. Relationship to the §XVIII validation (does NOT invalidate it)

Today's §XVIII bind validation (11/12) is unaffected. Those binds occurred via the cold-start `FirePickupObservation` path, and the bind-ratio query read `current_offer_id` directly — neither touches this catch-up branch. But the crash is adjacent to the lost-mode→bind transition the §XVIII amendment governs, so the recommendation is to **land this fix before the canon amendment applies**, so the canon is recorded against a tree with the catch-up guard actually functioning.

---

## 5. Verification plan (post-apply)

1. **Test-level:** add/confirm a unit test that exercises `_execute_action` on the `FirePickup` catch-up path with a non-NULL `actual_pickup_at`, asserting the guard binds narrative + preserves cache without raising. Pin against `RealDictCursor`.
2. **Pytest:** full suite green (baseline was 691 passed / 1 skipped on the deployed tree).
3. **Post-deploy prod:** `gcloud logging read` over the next drive for `driver_heartbeat.py:291` / `KeyError: 0` → expect zero.
4. **Red-alarm link:** observe whether the red screen recurs on the next catch-up fire; this confirms or refutes the §3 hypothesis and routes the remaining sentinel recon (server-side resolved vs Android-side pivot).

---

## 6. Apply discipline

L-3 envelope: idempotency verify (assert `existing_row[0]` present exactly once pre-patch, `existing_row['actual_pickup_at']` absent) → in-memory transform → atomic write + read-back (assert new string present, old absent). Single-line change; no migration; no schema touch.
