# Phase 2c.2 Item 3b.R + Alpha-Patch Complete — Handoff for Next Session

**For:** the next architecture-chat session continuing this sprint
**From:** the chat that shipped Items 3b.R + Alpha-Patch + Session-Odometer-Path-1-Decision (2026-05-08)
**Date:** 2026-05-08
**Branch:** `phase-2c-2-tad-exit-4tools` @ `88c04e8`, 18 commits ahead of `f3f5dc9`

---

## Read order at session start

1. `docs/PHASE_2C_2_SPRINT_BIBLE.md` — operative spec (Rules 1, 3a, 3b, 5, 7, 8)
2. `docs/SESSION_PROTOCOL.md` — paired-programming workflow, paste hazards, L-3
3. `docs/PHASE_2C_2_ITEM_3B_3D_HANDOFF.md` (commit `733502d`) — original Item 3b/3d operative spec
4. `docs/PHASE_2C_2_ITEM_3B_W_COMPLETE_2026-05-08.md` (commit `6b55357`) — Item 3b.W completion
5. `docs/IDENTITY_CRISIS_TECHDEBT_2026-05-08.md` (commit `88c04e8`) — Option β deferred audit
6. **This document** — captures Item 3b.R + Alpha-Patch deltas + the session-odometer Path-1 decision

After loading docs, run the standard recon:

```bash
cd ~/puddlejumper-prod && git log --oneline f3f5dc9..HEAD
source venv/bin/activate && python3 -m pytest --tb=short -q 2>&1 | tail -5
```

Expected: **18 commits ahead of `f3f5dc9`**, top commit `88c04e8`. Pytest **552/552 passing**.

---

## What shipped this session (2026-05-08)

### Item 3b.R — heartbeat reader activates dual commit rule (commit `95f037f`)

`driver_heartbeat.py` extended with five caller-layer helpers:

- `_to_utc(dt)` — defensive tz-attach against psycopg2 connection drift
- `_assemble_per_offer_state(cur, driver_id, queue_offer_ids)` — fetches `dict[offer_id, OfferTadState]` via JOIN on `decision_log` (offer_history has no driver_id column)
- `_get_last_known_anchor_id(cur, driver_id)` — forensic anchor lookup
- `_detect_lost_mode(cur, driver_id, queue_offer_ids)` — Bible Rule 7a narrative_blindness detection (2-hour ACCEPT-only filter)
- `_build_tad_decision_context(diagnostics, matches, lost_mode, last_known_anchor_id)` — JSONB blob serializer

`post_heartbeat()` now calls those helpers and passes the four kwargs through `evaluate_with_diagnostics()`. `_log_decision_context()` extended with `lost_mode` and `last_known_anchor_id` kwargs and serializes the JSONB into the `tad_decision_context` column.

Production deploy `puddlejumper-api-00596-z7x`. Bridge state preserved (when no per_offer_state, the 542-test floor's behavior is unchanged).

### Alpha-patch — _execute_action ID realignment (commit `88c04e8`)

Late-stage discovery during 3d recon: `_execute_action` had been treating `action.offer_id` as `decision_log.id` when in fact `Offer.offer_id` is `offer_history.id` per `driver_queue._project_offers`. Six SQL statements (3 in FirePickup, 3 in FireDropoff) were silently writing to wrong rows or zero rows. 874 of 2251 offer_history rows have `id != decision_log_id` (sequence offset of ~624 as of 2026-05-08).

Fix: subquery translation at the schema boundary. `Offer.offer_id` stays canonical as `offer_history.id`. `pickup_market_signals.offer_id` (FK to `decision_log.id`) gets the translated value via `(SELECT decision_log_id FROM offer_history WHERE id = %s::bigint)`. Two rowcount guards added on the canonical `offer_history` UPDATEs (FirePickup + FireDropoff) — log WARNING and return `(False, "fire_*_zero_rows")` on zero-row UPDATE. pms UPDATEs deliberately do NOT get guards (sparse table; 91% pms-orphan rate makes zero-row UPDATEs there routine).

**Architectural realignment to Option β (making `decision_log.id` the canonical `Offer.offer_id`) deferred to a future sprint.** See `docs/IDENTITY_CRISIS_TECHDEBT_2026-05-08.md` for the audit and the 6 prerequisite recon questions before β can safely proceed.

### Test floor delta

- 521/521 at session start
- 542/542 after 3b.R (+21 tests in `tests/test_driver_heartbeat_3b_r.py`)
- 552/552 after alpha-patch (+10 tests in `tests/test_execute_action_id_fix.py`)

---

## Production deploy state

Cloud Run revision `puddlejumper-api-00596-z7x`. 100% traffic. Healthy startup (no import errors).

Real-world test driving on 2026-05-08 confirmed:

- **3b.R is alive:** 900 heartbeats during a real Uber trip, 146 of those wrote `tad_decision_context` JSONB blobs to `pudo_decision_context`.
- **3b.R is correctly skipping all candidates:** every TAD verdict returned `passed: false, mode: "missing_state", fail_reason: "no per_offer_state entry"`. This is the defensive design firing exactly as expected — the upstream `expected_*` columns are NULL on offer_history rows, so `_assemble_per_offer_state` excluded them, so the TAD bouncer correctly recorded `missing_state` failure.
- **No FirePickup or FireDropoff fires** — TAD blocked all candidates. Auto-nailer dormant pending the upstream feed fix below.
- **Alpha-patch is correctly waiting:** `actual_pickup_at` and `actual_dropoff_at` columns NULL on the trip's offers because no FirePickup ever fired (which is upstream of the SQL realignment).

The system is operating *correctly given broken inputs*. The architecture works. The blocker is upstream.

---

## The blocker: session-odometer feed

### Discovery

`miles_at_offer_receipt` has been NULL on EVERY offer_history row (32 ACCEPTs and 27 DECLINEs over the past 7 days). Without it, `compute_offer_expectations()` short-circuits via the bind-NULL fallback path in `decisions/logger.py:142`, producing NULL `expected_*` columns. Without those, 3b.R correctly excludes the offer from `per_offer_state`, and TAD records `missing_state`. The chain breaks at the very first link.

### Root cause

The Android `cumulativeMiles` field in `AutoNailItManager` resets at multiple lifecycle events (pickup fire, dropoff fire, state transitions, coord changes). It's a **per-leg odometer** designed for the motion gate's odometer-ratio progress check.

But `miles_at_offer_receipt` and TAD's distance gates need a **session-cumulative odometer** that monotonically increases across an entire shift, never resets at pickup/dropoff. Bible Rule 6 defines this contract; the existing field cannot fulfill it.

### Decision: Path 1 (architecturally correct)

Ratified 2026-05-08 by Andrew + Gemini after considering Path 2 (reinterpret existing field — breaks chain math) and Path 3 (server-side session tracking — kludge). Path 1 keeps both odometers as separate concepts on Android.

### Path 1 work breakdown

**Android side (Claude Code's territory):**

1. New `@Volatile var sessionCumulativeMiles: Double = 0.0; private set` in `AutoNailItManager` alongside existing `cumulativeMiles`. Resets ONLY at app launch (Kotlin object property initializer = once-per-process).
2. Parallel accumulation line in GPS-tick handler (within the same outlier-rejection guard as the per-leg odometer).
3. New `@SerialName("session_cumulative_miles") val sessionCumulativeMiles: Double` (non-nullable) in heartbeat DTO.
4. New `@SerialName("session_cumulative_miles") val sessionCumulativeMiles: Double? = null` (nullable) in `OfferDecisionRequest` DTO.
5. Wire into `ScreenshotMonitorService` decision-request builder.
6. Wire into heartbeat send path.

**Status as of session close (2026-05-08):** Claude Code reported all 6 edits applied. Andrew installed the new APK via Claude Code's adb pipeline and drove a real Uber trip. Backend confirms: `session_cumulative_miles` field DOES NOT appear in any heartbeat JSONB blob from the trip. Either the new app didn't actually run (cached process), or there's a serialization issue, or there's a second heartbeat code path that wasn't found in the original recon.

A separate Claude Code debug brief was authored to diagnose the discrepancy. Resolution is on the Android side, not the backend.

### Backend wiring (waiting on Android — exact spec)

When Andrew confirms `session_cumulative_miles` arrives in the heartbeat JSONB, the backend wiring is approximately 5 lines, in 2 files. Details:

**`decisions/router.py`:**

Add near line 203 (where `cumulative_miles = p.get("cumulativeMiles")` currently lives — but rename for clarity):

```python
# [path-1] Session odometer (new contract; Bible Rule 6).
# Source: Android AutoNailItManager.sessionCumulativeMiles, which resets
# only at app launch — not at pickup/dropoff/state transitions.
session_cumulative_miles = p.get("session_cumulative_miles")
if session_cumulative_miles is not None:
    session_cumulative_miles = float(session_cumulative_miles)
```

In the `params` dict around line 243:

```python
"cumulative_miles": session_cumulative_miles,  # [path-1] CHANGED: was cumulativeMiles
```

(Note the existing per-leg `cumulativeMiles` field is intentionally NOT used here. The decision-engine path needs the session value, not the per-leg value. The per-leg value continues to flow through the heartbeat path for motion gate use.)

**`driver_heartbeat.py`:**

Around line 555 where `cumulative_miles = body.get('cumulative_miles')` currently is, add a sibling read:

```python
# [path-1] Session odometer for TAD distance math (Bible Rule 6).
session_cumulative_miles = body.get('session_cumulative_miles')
```

Around line 624 where `cumulative_miles=cumulative_miles` is passed into `evaluate_with_diagnostics(...)`, change to:

```python
current_odometer=session_cumulative_miles,  # [path-1] NOT cumulative_miles
```

The existing `cumulative_miles` reads stay in place for the motion gate's `evaluate_gates()` call at line 590 — motion gate continues to use the per-leg odometer.

Update the JSONB blob written to `driver_trip_state.heartbeat` at line 565 to include `"session_cumulative_miles"` for forensic parity.

Tests for the backend wiring are minimal: 2-3 unit tests confirming that `router.py` reads the new field and `post_heartbeat` passes the right value into the WAI brain. No new `tad.py` or `where_am_i.py` changes — they consume `current_odometer` agnostically.

---

## Sprint state at session close

**Branch:** `phase-2c-2-tad-exit-4tools` @ `88c04e8`, 18 commits ahead of `f3f5dc9`
**Pytest floor:** 552/552
**Production:** revision `puddlejumper-api-00596-z7x`, 100% traffic, dormant pending upstream feed fix

**What's shipped (in tree + production):**
- Item 3a/3c/3e (where_am_i.py dual commit rule + forensic blob)
- Item 3b.W (writer-side expected_* anchors at offer-receipt)
- Item 3b.R (heartbeat reader activates dual commit rule)
- Alpha-patch (_execute_action ID realignment)
- Identity-crisis tech-debt doc

**What's pending:**
- **BLOCKED:** Path-1 session odometer (Android serialization issue — Claude Code investigating)
- **READY ON RESUME:** Backend wiring of `session_cumulative_miles` (5-line change, ~3 tests, one apply script)
- **NOT YET STARTED:** Item 3d (inferred-dropoff event table) — design and Gemini ratification complete, just needs apply script + migration
- **DEFERRED FUTURE SPRINT:** Option β (full identity-crisis realignment per `docs/IDENTITY_CRISIS_TECHDEBT_2026-05-08.md`)

**Two minor cleanup commits identified during forensics (not blocking):**

1. **`_get_last_known_anchor_id` needs a recency filter.** Currently returns the oldest historical anchor in the DB if no recent ones exist. Forensics from 2026-05-08 trip showed it returning offer `7136` from before the 3b code shipped. Add `AND oh.created_at > NOW() - interval '24 hours'` to the SQL. One-line fix, future commit.

2. **Lost Mode firing on stale data.** The 2026-05-08 trip showed `lost_mode: true` on every blob because `_detect_lost_mode` found an in-window orphan from before deploy. Likely resolves itself once `expected_*` columns start populating in real time, but worth a focused observation pass after the Android fix lands.

---

## Resume sequence (next session)

1. Read this doc + the standard read order
2. Check Claude Code's findings on the Android session_cumulative_miles serialization
3. Once Andrew confirms `session_cumulative_miles` shows up in heartbeat JSONB on the new app:
   - Author backend wiring apply script (`tmp/apply_path_1_backend.sh`)
   - 5-line code change + 3 unit tests + L-3 envelope
   - Apply, verify pytest 552 → ~555, deploy, observe
4. After observation confirms `expected_*` columns start populating and TAD verdicts shift from `missing_state` to real `pickup` / `dropoff` evaluations:
   - Item 3d ratification + apply (inferred-dropoff event table per Bible Rule 8)
5. Address the two minor cleanup items as a follow-up commit (post-3d, pre-sprint-close)
6. Author final sprint completion report (`docs/PHASE_2C_2_SPRINT_REPORT.md` per Bible's "Sprint completion report" section)

---

## Operational reminders

- Test floor: 552/552. Every change verified by full pytest run.
- Apply scripts: L-3 envelope, dynamic delta computation. The 2026-05-08 session burned 4 patch attempts on delta-prediction errors (3b.R Patch 3 off by 2; alpha-patch Patches 1/5/6). The L-2 arithmetic gates caught all of them cleanly without disk writes — working as designed. Future apply scripts should be more careful about counting comment lines around SQL replacements.
- Paste hazards: L-22 chat-display linkification of `[name.py](http://name.py)` is real. Read past it. File transfer via `create_file` → `present_files` → Andrew's local-then-scp pattern is the only safe path for substantive code.
- File-and-cat for psql output (psql tabular formatting doesn't survive chat copy-paste).

---

End of handoff.
