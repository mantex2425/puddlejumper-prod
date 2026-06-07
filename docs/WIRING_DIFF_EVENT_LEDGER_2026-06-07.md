# Event-ledger wiring — DRAFT diff for Gemini C4-review (NOT applied)

**Status:** proposed `post_heartbeat` edit, **not applied to `driver_heartbeat.py`**. This diff is
the production-touching change; review it against the C4 properties, then apply (Step 4) and run all
16 tests green. The already-tested `event_ledger.py` module isn't the thing to review — this is.

**The C4 property to verify in this diff (your call-out):** the seed-advance writes the snapshot
computed from **`current`** (this tick's live set) plus `keyframe_count`/`last_keyframe_at`, all in
**one** `advance_ledger_seed` call **inside the one savepoint** — and it is a **separate
`driver_trip_state` UPDATE** from the `:1979` authoritative UPDATE (heartbeat/arrest) in the parent.
Two distinct UPDATEs to the same row; **nothing ledger is folded into `:1979`** (recon-confirmed:
`:1979` does not touch `ledger_state`).

---

## Hunk 0 — import (top of `driver_heartbeat.py`, with the other imports)

```diff
 from dispatch import (
     dispatch,
     FirePickup, FireDropoff,
     FirePickupObservation, FireDropoffObservation, ClearNarrative,
     LogNoMatch, LogPickupRematch, LogAmbiguousMatch,
 )
+import event_ledger
```

## Hunk 1 — seed-read at LOAD (after `:1948`)

```diff
     snap = queue.snapshot(cur, current_cumulative_miles=cumulative_miles, last_odometer_move_at=effective_last_move)
     current_offer_id = snap.bound_offer_id
     queue_offer_ids = set(snap.offer_ids)
+    # event-ledger: prior diff-seed (§VIII Passive lane). DEDICATED PK read — NOT folded
+    # into compute_effective_last_move, which the /decisions caller shares and must never
+    # see ledger_state. Read here at LOAD; ledger_state is WRITTEN only in the batch below.
+    _ledger_prior_seed = event_ledger.read_ledger_seed(cur, driver_id)
```

## Hunk 2 — the batch block (after the LOG `try/except` at `:2404`, before `conn.commit()` at `:2406`)

```diff
     except Exception as e:
         # LOG failure must not break the heartbeat — the API contract is
         # liveness, not forensic completeness. Surface to logs.
         log.exception("[heartbeat] pudo_decision_context INSERT failed: %s", e)
 
+    # ── EMIT (event-ledger; §VIII Passive lane; C4) ───────────────────────────────
+    # The whole block is savepoint-guarded so NO ledger fault — the probe READ, the pure
+    # gather, or the emit batch — can poison the parent heartbeat txn or its authoritative
+    # writes. TWO savepoints by design:
+    #   • SAVEPOINT ledger_block — parent-protects the probe read (a failed SELECT would
+    #     otherwise abort the parent txn and lose the authoritative writes).
+    #   • emit_batch's own SAVEPOINT ledger_batch — atomic emit(s) + seed-advance.
+    #
+    # C4 SEPARATION (review this): emit_batch's advance_ledger_seed is the SOLE writer of
+    # driver_trip_state.ledger_state — the snapshot from CURRENT (queue_offer_ids) +
+    # keyframe_count + last_keyframe_at, one UPDATE, inside its savepoint. It is a SEPARATE
+    # UPDATE from the :1979 authoritative one (heartbeat/arrest) in the parent. Two distinct
+    # UPDATEs to the same row; nothing ledger is folded into :1979.
+    try:
+        cur.execute("SAVEPOINT ledger_block")
+        _, _left = event_ledger.compute_diff(
+            (_ledger_prior_seed or {}).get("last_queue_snapshot_ids"), queue_offer_ids)
+        _reap_rows = event_ledger.probe_reaped_offers(cur, driver_id, _left)
+        _current_status = {oid: {"picked_up": (oid == current_offer_id)} for oid in queue_offer_ids}
+        _events, _new_seed = event_ledger.gather_ledger_events(
+            prior_seed=_ledger_prior_seed, current_ids=queue_offer_ids,
+            current_status=_current_status, reap_rows=_reap_rows,
+            reference_time=_heartbeat_now, cumulative_miles=cumulative_miles,
+            effective_last_move=effective_last_move, matches=matches,
+            executed_actions=executed_actions, arrest_started_at=arrest_started_at_post,
+            arrest_counter_s=arrest_counter_s_post, cluster=cluster,
+            cadence_target_hz=cadence_target_hz, now=_heartbeat_now)
+        cur.execute("RELEASE SAVEPOINT ledger_block")
+        event_ledger.emit_batch(
+            cur, driver_id, _events, _new_seed,
+            ctx={"lat": current_lat, "lng": current_lng,
+                 "gps_accuracy_m": gps_accuracy_m, "cumulative_miles": cumulative_miles})
+    except Exception as e:
+        try:
+            cur.execute("ROLLBACK TO SAVEPOINT ledger_block")
+        except Exception:
+            pass
+        log.warning("[heartbeat] event-ledger emit skipped (swallowed): %s", e)
+
     conn.commit()
```

---

## Draft decisions to ratify (Gemini)

1. **Two savepoints (probe-protection + emit_batch's own).** `emit_batch` (tested) owns the atomic
   emit+seed savepoint. But the **probe is a READ in the parent txn** — a failed SELECT would abort
   the parent and lose the authoritative writes, violating §VIII. So an outer `ledger_block`
   savepoint guards the probe+gather; the outer `except` does `ROLLBACK TO ledger_block`. Question:
   accept the two-savepoint shape (keeps `emit_batch` the single tested call), or fold the probe
   INTO `emit_batch` (one savepoint, but a bigger signature + the batch-atomicity test changes)?
2. **`current_status` is membership + `picked_up` only.** `Offer` (pudo_types.py:80) carries no
   `status`/`in_band`/`expected_distance`, so the keyframe snapshot can't populate them without
   extra reads. `picked_up = (oid == current_offer_id)` (the PRE-dispatch bound id — a Phase-1
   simplification; post-dispatch would use `_derive_post_offer_id(executed_actions, …)`). Enrich
   later if the snapshot needs `status`/`in_band`.
3. **Structural-guard compatibility (verified):** the outer `except` uses
   `cur.execute("ROLLBACK TO SAVEPOINT …")` — a SQL string, NOT a `.rollback()` method call — and
   there is no bare `raise` between `emit_batch` and `conn.commit()`. So the no-uncaught-raise
   structural test (test 16) activates and passes. (The AST guard keys on `.rollback()` Calls and
   `raise` statements, not SQL text.)

## Apply plan (Step 4, post-ratify)
Apply Hunks 0/1/2 to `driver_heartbeat.py`; `py_compile`; sync VM; `./venv/bin/python -m pytest
tests/test_event_ledger.py -q` → 16 green (the structural guard now ACTIVE, not vacuous). Then a
live `00649` heartbeat smoke (one POST) to confirm rows land + the keystone holds end-to-end.
