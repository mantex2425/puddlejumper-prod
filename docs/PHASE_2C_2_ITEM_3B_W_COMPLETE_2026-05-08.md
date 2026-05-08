# Phase 2c.2 Item 3b.W — Shipped. Resume Brief for Items 3b.R + 3d.

**For:** the next architecture-chat session continuing this sprint
**From:** the chat that shipped Items 3a/3c/3e (last session) AND Item 3b.W (this session)
**Date:** 2026-05-08
**Branch:** `phase-2c-2-tad-exit-4tools` off `f3f5dc9`

---

## Read order at session start

1. `docs/PHASE_2C_2_SPRINT_BIBLE.md` — operative spec (Rules 1, 3a, 3b, 5, 7, 8)
2. `docs/SESSION_PROTOCOL.md` — paired-programming workflow, paste hazards, L-3
3. `docs/PHASE_2C_2_ITEM_3B_3D_HANDOFF.md` (commit `733502d`) — operative spec for Items 3b + 3d. Item 3b.R + Item 3d are the unfinished work.
4. **This document** — captures the Item 3b.W delta from the 2026-05-08 session.

After loading, run the standard recon:

```bash
cd ~/puddlejumper-prod && git log --oneline f3f5dc9..HEAD
source venv/bin/activate && python3 -m pytest --tb=short -q 2>&1 | tail -5
```

Expected: **14 commits ahead of `f3f5dc9`**, top commit is `1fab701`. Pytest **521/521 passing**.

---

## What Item 3b.W shipped (2026-05-08)

**Scope discovery:** the original 3b handoff did not identify the writer gap. Recon revealed `compute_offer_expectations` had zero production callers — the 4 `expected_*` columns existed in schema (Phase 1 migration) but nothing populated them. Per Canonical Rules ("design gaps go INTO the current solution package"), Item 3b absorbed the writer.

**Two commits:**

- **`673ff12`** — `decisions/logger.py` (+112 / -1, 178 → 289 lines). Wires `compute_offer_expectations` into `log_decision()`. Fetches prev_offer's dropoff anchors via indexed JOIN (`decision_log.driver_id` index), constructs minimal `Offer` dataclass stubs, computes the 4 anchors, binds them into the `offer_history` INSERT.

- **`1fab701`** — `tests/test_offer_history_anchors.py` (+294). 8 tests in new class `TestExpectedAnchorBindings`: idle/stacked/orphaned/missing-fields/null-odometer/select-failure/compute-failure paths. Adds module-level helper `_mock_cursor_with_prev()` for two-stage `fetchone()` mocking.

**Ratifications locked during this session (Gemini-confirmed):**

- **Q1:** `now=datetime.datetime.now(timezone.utc)` at receipt time. Python wall-clock vs Postgres `NOW()` discrepancy is microseconds, immaterial for minute-resolution TAD math.
- **Q2:** prev_offer SELECT runs unconditionally on every offer-receipt (one extra indexed query, cheap).
- **Q3:** bind NULL on `compute_offer_expectations` returning None / missing odometer / any failure. The offer_history row always persists.
- **Q4:** `Offer.offer_id` in DriverQueue is the `oh.id` (offer_history primary key). For Item 3b.R, the `OfferTadState` SQL fetches offers by `oh.id IN (queue_offer_ids)` joined through `decision_log` for driver filtering.
- **Q5:** Defensive tz-attach (`if dt and dt.tzinfo is None: dt = dt.replace(tzinfo=utc)`) at every fetch boundary. Belt-and-suspenders against psycopg2 connection-config drift.
- **Q6 (Item 3d):** Inferred-dropoff persistence = **Option (c) separate event table** `app_private.inferred_dropoffs`. Schema design lands at 3d implementation time.

**Bridge state preserved:** the heartbeat reader (`where_am_i.evaluate`) does not yet read the new columns. Production behavior unchanged. Activation lands in Item 3b.R.

**Pre-existing canonical violation surfaced (NOT in scope):** `decisions/logger.py:91-93` uses `EXTRACT(... FROM NOW() AT TIME ZONE 'America/Chicago')` for `day_of_year` / `day_of_week` / `hour_of_day` denormalized columns. v2.1 Section III + Apr 27 supersession says system logic is UTC. Three lines, but the consequences span Price Radar's seasonal echo, market_rate H3 buckets, and analytics queries — needs its own ratification cycle. Add to post-sprint backlog.

---

## What remains: Items 3b.R + 3d

### Item 3b.R — heartbeat reader

**Touchpoint:** `driver_heartbeat.py:621` (`post_heartbeat()` function — NOT `_handle_post_pickup` as the original handoff approximated; that function does not exist).

**Established landmarks** (from this session's recon):

- `cumulative_miles` is in scope at line 556 (from request body)
- `current_offer_id` is in scope at line 572 (from `snap.bound_offer_id`)
- `_log_decision_context()` at line 390 is the JSONB serialization extension point. Already takes `diagnostics` as parameter — clean extension; bind `tad_decision_context jsonb` and `commit_rule_classification text` into the existing INSERT.
- `tad_decision_context jsonb` column is live on `pudo_decision_context` (Phase 1 migration).
- 7 Phase 1 columns + `miles_at_offer_receipt` confirmed live on `offer_history`.

**Work shape** (per the prior 3b/3d handoff):

1. Fetch `OfferTadState` rows for `snap.offers` — one SQL query joining `offer_history.id IN (...)` to `decision_log.driver_id = uid`. Returns the 9 fields locked at `tad.py:316`.
2. Detect Lost Mode (3 narrative_blindness conditions: queue empty post-GC, prior offer GC'd before pickup confirmation, stacked offer with no prior pickup-confirmation).
3. Track `last_known_anchor_id` from session state across heartbeats (most recent offer with spatially-confirmed pickup OR dropoff).
4. Pass all four kwargs into `evaluate_with_diagnostics(driver_id, queue, per_offer_state=..., lost_mode=..., last_known_anchor_id=..., current_odometer=...)`.
5. Extend `_log_decision_context` to serialize `diagnostics.tad_verdicts` to JSONB, plus `classify_commit_rule(outcome, verdict)` label per committed match.

**Tests:** ~12-15 new tests (estimate per prior handoff). Likely a new file `tests/test_driver_heartbeat_3b_r.py` since `tests/test_driver_heartbeat.py` doesn't exist (verified this session — only `tests/test_heartbeat_dispatch.py` exists, scope-different).

**Note on `pickup_exit_time`:** populated by Phase 2 (`escape_detection.py`, separate Claude Code session). For 3b.R's first cut, it'll always be NULL — `evaluate_tad_gate` handles NULL cleanly per `tad.py` Section F. When Phase 2 ships, the column starts populating and dropoff time signals start activating without code change.

### Item 3d — inferred-dropoff event table

Per Q6 ratification, mechanism is locked: separate event table `app_private.inferred_dropoffs`.

Schema (proposed, ratify at implementation):
```sql
CREATE TABLE app_private.inferred_dropoffs (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    offer_id bigint NOT NULL REFERENCES app_private.offer_history(id),
    inferred_at timestamptz NOT NULL,
    inferred_lat double precision,
    inferred_lng double precision,
    source text NOT NULL CHECK (source IN ('cluster_centroid', 'gps_fallback')),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_inferred_dropoffs_offer ON app_private.inferred_dropoffs(offer_id);
```

Trigger conditions (from Sprint Bible Rule 8): pickup B commits via Rule 3 + ride A's `actual_pickup_at IS NOT NULL` + `actual_dropoff_at IS NULL` + current time ≥ both expected_*_arrival_time anchors.

Inferred location: cluster centroid before pickup B (primary), last GPS coord before pickup B's cluster (fallback). No accuracy threshold; the lat/lng is forensic context — chain math anchors on the odometer reading.

---

## Operational reminders for the next session

- **Test floor:** 521/521. Bridge state must remain intact — every change verified by full pytest run.
- **Apply scripts:** L-3 envelope, dynamic delta computation (`new.count("\n") - old.count("\n")`).
- **Paste hazards:** L-22 chat-display linkification of `[name.py](http://name.py)` is real. Read past it.
- **File transfer:** `create_file` → `present_files` → user scp from local Downloads. Skip VS Code in the chain.
- **Recon pattern:** `{ echo "=== A ==="; cmd; echo ""; echo "=== B ==="; cmd; } > /tmp/recon_stepN.txt && cat` works well for multi-question lookups.
- **Anti-waterfall:** if it isn't code or a test, it doesn't get written. Findings get one paragraph; bugs get a commit message.
- **The 4 expected_* columns are live in production after the next deploy.** Validate via:
  ```sql
  SELECT id, created_at,
         expected_pickup_arrival_time, expected_pickup_distance,
         expected_dropoff_arrival_time, expected_dropoff_distance
  FROM app_private.offer_history
  ORDER BY created_at DESC LIMIT 5;
  ```
  If columns populate post-deploy, writer is healthy. If NULL, check Cloud Run logs for `[3b.W]` warnings.

---

End of handoff.
