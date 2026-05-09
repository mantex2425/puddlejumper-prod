# Phase 2c.2 Sprint — State Handoff (2026-05-09, ~00:30 UTC)

**Branch:** `phase-2c-2-tad-exit-4tools` @ commit `e42d88f` (or whatever HEAD reads at session-open — see "Recon at session start" below)
**Pytest floor:** 552/552
**Production deploy:** rev `puddlejumper-api-00596-z7x`, 100% traffic, healthy
**Sprint progress:** Brain wiring fully validated end-to-end. Sensor modules (Phase 2 Bible spec) remain to ship.

---

## Read order at session start

1. `docs/PHASE_2C_2_SPRINT_BIBLE.md` — operative spec (especially Phase 2 module list: 1 escape_detection / 2 router / 3 logger / 4 heartbeat / 5 poi_service / 6 validation harness)
2. `docs/SESSION_PROTOCOL.md` — paired-programming workflow, paste hazards, L-3
3. `docs/PHASE_2C_2_ITEM_3B_3D_HANDOFF.md` — original Item 3b/3d operative spec
4. `docs/PHASE_2C_2_ITEM_3B_W_COMPLETE_2026-05-08.md` — Item 3b.W shipped
5. `docs/PHASE_2C_2_ITEM_3B_R_AND_ALPHA_COMPLETE_2026-05-08.md` — Item 3b.R + Alpha-Patch shipped
6. `docs/IDENTITY_CRISIS_TECHDEBT_2026-05-08.md` — Option β deferred to future sprint
7. `docs/SPRINT_STATE_HANDOFF_2026-05-08.md` — prior handoff (Path-1 thinking, now superseded — see "What changed since the prior handoff" below)
8. **This document** — the current state

---

## Recon at session start

```bash
cd ~/puddlejumper-prod && git log --oneline f3f5dc9..HEAD | head -30
source venv/bin/activate && python3 -m pytest --tb=short -q 2>&1 | tail -5
```

Expected: top commit is the most recent doc commit. Pytest **552/552 passing**.

---

## What changed since the prior handoff

The prior handoff (`SPRINT_STATE_HANDOFF_2026-05-08.md`) framed the next step as "Path-1 backend wiring (5-line change)" pending Android transmitting a session-cumulative odometer. **That entire framing was over-engineered and is now superseded.** The current session (2026-05-08 evening) discovered:

1. **Android `cumulativeMiles` is monotonic-since-launch in practice.** Claude Code's prior recon of `AutoNailItManager.kt` had described it as a "per-leg odometer that resets at lifecycle events." Re-recon under the brief at `CLAUDE_CODE_BRIEF_ANDROID_CUMULATIVE_MILES_DTO.md` confirmed 5 of 7 reset paths are dead post-Sprint-A demolition (heartbeat responses no longer carry `driverState`). The accumulator never resets in the current production code.

2. **The decision-request DTO never sent the odometer in the first place.** Production `trace_data` blob from offer 7774 (2026-05-08 20:31 UTC) had no `cumulativeMiles` field, despite `decisions/router.py:203` having read `p.get("cumulativeMiles")` since the Sprint A gate-layer addition in late April. Claude Code shipped APK versionCode 116 (commit `54e87972` on Android side, 2026-05-08) adding `cumulativeMiles` to the `OfferDecisionRequest` DTO and wiring `ScreenshotMonitorService.kt:774` to set it from `AutoNailItManager.cumulativeMiles`.

3. **Wire format is camelCase, definitively confirmed.** Logcat capture at `2026-05-08 19:14:57.164 CDT` of a Testing Lab decision-request shows the body uses `pickupMinutes`, `tripMinutes`, `tripMiles`, `dropoffLat`, `dropoffLng`, `marketId`, `useOcrDistance`, `towardsActive`, `isPuddleJumpMode`, `towardsBacktrackTolerance`. All camelCase. No snake_case sneak-throughs.

4. **The Bruno collection has been lying to us for months.** Bruno's hand-authored decision-request body uses snake_case. The router's `float(p.get("tripMiles") or 0)` patterns silently zeroed every snake_case field. Bruno requests have been returning `verdict: ACCEPT` with mathematically-broken `expected_*` columns the entire time. Today's debugging caught it because we finally checked the `offer_history` columns post-request instead of trusting the response code.

5. **Backend chain is correct end-to-end.** Bruno test row 8400 (2026-05-09 00:03:38 UTC), with corrected camelCase body and `cumulativeMiles=42.5`, produced:
   - `miles_at_offer_receipt=42.50` ✓
   - `expected_pickup_distance=43.0` (= 42.5 + 0.5 pickupMiles) ✓
   - `expected_pickup_arrival_time = now + 5min` (pickupMinutes=5) ✓
   - `expected_dropoff_distance=51.6` (= 43.0 + 8.6 tripMiles) ✓
   - `expected_dropoff_arrival_time = pickup + 20min` (tripMinutes=20) ✓
   - `trip_duration=00:20:00` ✓

   No backend bug. No Item 3b.W writer fix needed. No Path-1 apply script needed. The only thing missing from the production chain was `cumulativeMiles` on the wire from Android, which Claude Code has shipped.

---

## What's locked (validated, no further work needed)

- ✅ TAD bouncer (Items 3a/3c/3e) shipped + 21 tests
- ✅ Item 3b.W (writer-side `expected_*` anchors) shipped + 8 tests + Bruno-validated end-to-end
- ✅ Item 3b.R (heartbeat reader activates dual commit rule) shipped + 21 tests
- ✅ Alpha-patch (`_execute_action` ID realignment) shipped + 10 tests
- ✅ Android decision-request DTO (`cumulativeMiles` camelCase): APK 116 deployed, logcat-confirmed
- ✅ Android `AutoNailItManager.cumulativeMiles` accumulator: confirmed live (heartbeat blob shows 37.92 from earlier today's drive, Testing Lab body shows accumulator hookup correct)
- ✅ Wire-format convention: camelCase definitively confirmed via logcat capture

---

## What's pending (work remaining for sprint completion)

In dependency order, all work is in `puddlejumper-prod` (architecture-chat sessions, paired-programming protocol):

### 1. Real-OCR verification of `cumulativeMiles` on the production path

**Status:** awaits next drive with a real Uber offer.

The Testing Lab path at `TestingLabViewModel.kt:190` does NOT set `cumulativeMiles` (named-args-without-the-field, plus `explicitNulls = false` in serializer config). The production OCR path at `ScreenshotMonitorService.kt:774` DOES set it. We have not yet captured a real OCR-driven decision request showing `trace_data->>'cumulativeMiles'` populated with a non-null value.

**Definition of done:** the following query returns non-null `cm_in_payload` after a real-Uber-offer:

```bash
psql -h 10.128.0.2 -U postgres -d puddlejumper -c "
  SELECT dl.id AS dl_id,
         dl.created_at,
         dl.trace_data->>'cumulativeMiles' AS cm_in_payload,
         oh.miles_at_offer_receipt,
         oh.expected_pickup_distance,
         oh.expected_dropoff_distance,
         (oh.expected_dropoff_arrival_time - oh.expected_pickup_arrival_time) AS trip_duration
  FROM app_private.decision_log dl
  LEFT JOIN app_private.offer_history oh ON oh.decision_log_id = dl.id
  WHERE dl.driver_id = 'UjT1hE9eBXh2q95aSZYOkzDJ8lo1'
  ORDER BY dl.created_at DESC LIMIT 3;
"
```

If after a drive the column persists as NULL, that's a real bug to investigate (likely in `ScreenshotMonitorService` or the OCR pipeline). High prior probability of success given everything upstream is verified — but this remains the only end-to-end production proof.

**Side note:** Andrew attempted a "manual screenshot decision" trigger on 2026-05-08 evening that did not produce a new `decision_log` row. Triage of that trigger is its own Android-side debug task, NOT sprint-blocking. Real-Uber-offer is the cleaner validation.

---

### 2. `escape_detection.py` (Sprint Bible Module 1)

**Status:** not built. Bible spec is fully ratified.

Module: `escape_detection.py` (new file). Constants:
- `EXIT_VELOCITY_DISTANCE_M = 150`
- `EXIT_VELOCITY_SPEED_MPH = 15`
- `EXIT_VELOCITY_SPEED_DURATION_S = 30`
- `EXIT_VELOCITY_TIMEOUT_S = 30 * 60`

Public function: `check_exit_velocity(pickup_centroid, pickup_confirmed_at, recent_heartbeats) -> ExitVelocityResult`. Logic + tests fully specified in Sprint Bible "Module 1" section.

Heartbeat schema must expose `speed_mph`. Verified during recon: `body.get('speed_mph')` is read at `driver_heartbeat.py:794`, so the data is in scope. Need to confirm it flows into the `Heartbeat` dataclass form that `check_exit_velocity` will consume.

Tests: ~6 (distance trigger, speed trigger, sustain semantics, timeout, parking-garage scenario).

Estimated work: 1-2 sessions including Gemini ratification + apply script.

---

### 3. `driver_heartbeat.py` exit-velocity wiring (Sprint Bible Module 4)

**Status:** depends on Module 1 above.

Wire `check_exit_velocity` into the post-pickup heartbeat handler. Snippet from Bible:

```python
if (current
    and current.actual_pickup_at is not None
    and current.pickup_exit_time is None
    and not current.exit_velocity_timeout):
    
    result = check_exit_velocity(...)
    if result.detected:
        _update_offer_exit_velocity(...)
    elif result.timeout:
        _set_offer_exit_velocity_timeout(...)
```

Tests: ~3 (heartbeat triggers detection / no detection / timeout).

Estimated work: same session as Module 1 ratification.

---

### 4. `poi_service.py` constant bump (Sprint Bible Module 5)

**Status:** not done. One-line change.

```python
API_SEARCH_RADIUS_M = 50  →  API_SEARCH_RADIUS_M = 150
```

Justification: Bible Module 5 — audit-validated from 2026-05-06 audit data. 50m was missing legitimate POI matches at strip-mall plazas where the cluster centroid sits in a parking lot offset from the storefronts.

Estimated work: 5 minutes.

---

### 5. Item 3d — inferred-dropoff event table

**Status:** design ratified (Q6 in 3b.W handoff: separate event table, Option C). Schema locked. Not built.

Schema (per `SPRINT_STATE_HANDOFF_2026-05-08.md`):

```sql
CREATE TABLE app_private.inferred_dropoffs (
    offer_id                  bigint PRIMARY KEY REFERENCES app_private.offer_history(id),
    inferred_at               timestamptz NOT NULL DEFAULT NOW(),
    lat                       double precision NOT NULL,
    lng                       double precision NOT NULL,
    source                    text NOT NULL CHECK (source IN ('cluster_centroid', 'gps_fallback')),
    source_pdc_id             bigint REFERENCES app_private.pudo_decision_context(id),
    trigger_decision_log_id   integer NOT NULL REFERENCES app_private.decision_log(id),
    created_at                timestamptz NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_inferred_dropoffs_offer ON app_private.inferred_dropoffs(offer_id);
```

Writer: `_check_inferred_dropoff(cur, driver_id, fired_pickup_offer_id)` helper called from `post_heartbeat()` after FirePickup execution. Reads `pudo_decision_context.cluster_lat/cluster_lng` for primary location source; falls back to `heartbeat_log` for `gps_fallback`.

Trigger conditions (Bible Rule 8): pickup B commits + ride A's `actual_pickup_at IS NOT NULL` + `actual_dropoff_at IS NULL` + current_time ≥ both expected_*_arrival_times.

Tests: ~5-8.

Estimated work: 1-2 sessions including ratification + apply script + migration.

---

### 6. `tmp/validate_phase_2c_2.py` validation harness (Sprint Bible Module 6)

**Status:** not built. Bible spec exists.

Modeled on `validate_gate_layer.py`. Loads 2026-05-07 shift data (8 rides) + 2026-05-06 audit cases (Excel Dental, Pappasito's, US-90 strip mall, Houston no-zoning false-positive). Runs full pipeline end-to-end and asserts expected commit/skip outcomes per ride.

Estimated work: 1-2 sessions. Higher uncertainty because depends on availability of clean 2026-05-07 shift data and forensic blob captures.

---

### 7. Two cleanup commits (carry-over from prior handoff)

a. **`_get_last_known_anchor_id` recency filter.** Currently returns oldest historical anchor. Add `AND oh.created_at > NOW() - interval '24 hours'` to the SQL. One-line fix in `driver_heartbeat.py`.

b. **Lost Mode firing on stale data.** 2026-05-08 trip showed `lost_mode: true` on every blob because `_detect_lost_mode` found in-window orphan from before deploy. Likely self-resolves once `expected_*` columns populate in real-time post-real-OCR-verification. If not, separate diagnostic.

Estimated work: combined cleanup commit, ~30 minutes.

---

### 8. Forensic gap — `cumulativeMiles` not in `trace_data` allowlist

**Status:** identified during 2026-05-08 evening Bruno testing. Not in any prior handoff.

`decisions/logger.py` writes a curated allowlist of fields from the request payload into `decision_log.trace_data` for forensic replay. `cumulativeMiles` is consumed correctly (binds into `miles_at_offer_receipt`) but is NOT preserved in `trace_data`. Future debugging that reads `trace_data` to understand "what did Android send us" will be blind to the odometer.

Fix: add `"cumulativeMiles"` to the allowlist write in `decisions/logger.py`. One-line fix.

Same pattern as the `gpsAgeSec → trace_data` TODO already in Andrew's memory notes — could ship together as one allowlist-extension commit.

Estimated work: 5 minutes, bundle with item 7.

---

### 9. Sprint completion report

**Status:** not authored.

Per Sprint Bible "Sprint completion report" section, author `docs/PHASE_2C_2_SPRINT_REPORT.md` covering:

1. TL;DR
2. What landed (file-by-file commits)
3. Phase 1 / Phase 2 split
4. Test results (final pytest count vs. baseline)
5. Deviations from the Bible (alpha-patch, the Path-1 reframing tonight, the snake_case-Bruno-trap surfacing)
6. Pre-existing oddities flagged (link to `IDENTITY_CRISIS_TECHDEBT_2026-05-08.md`)
7. Operational state (final commit SHA, deploy revision)
8. Open decisions (Option β for future sprint)
9. What's left

Estimated work: 1 session at sprint close.

---

## What is NOT Claude Code's territory

Recent strategic notes from Gemini suggested Claude Code would handle Modules 1, 2, 3 (`escape_detection.py`, `decisions/router.py` updates, `decisions/logger.py` updates) and the validation harness. **That framing was based on stale context.**

The reality:

- **`decisions/router.py`**: already correct. `cumulativeMiles` read site at line 203 has been there since Sprint A gate-layer. Router does NOT need modification.
- **`decisions/logger.py`**: already correct for `compute_offer_expectations` chain. Only addition needed is the trace_data allowlist line (Item 8 above) — trivial backend work, not Android.
- **`escape_detection.py`**: pure Python in `puddlejumper-prod`, zero Android entanglement. Architecture-chat work.
- **`driver_heartbeat.py` wiring (Module 4)**: same — pure Python, architecture-chat work.
- **Validation harness (Module 6)**: same — pure Python, architecture-chat work.

**Claude Code's actual remaining work: approximately none.** APK 116 is shipped and confirmed correct. Optional: the one-line `TestingLabViewModel.kt:190` change to add `cumulativeMiles = AutoNailItManager.cumulativeMiles` so the test harness exercises the field too. Nice-to-have, not sprint-blocking.

If a future session feels tempted to ask Claude Code for sprint-completion work, re-read this section first. The `puddlejumper-prod` repo is architecture-chat territory.

---

## Operational reminders

- **Test floor: 552/552.** Verify every change with full pytest run.
- **Apply scripts: L-3 envelope, dynamic delta computation.** Comment-line counts around SQL replacements are finicky; trust the L-2 arithmetic gates.
- **Paste hazards: L-22.** `[name.py](http://name.py)` is chat-display linkification. Read past it. File transfer via `create_file` → `present_files` → Andrew's local-then-scp.
- **psql output: file-and-cat.** `psql ... > /tmp/recon.txt 2>&1 && cat`. Tabular formatting doesn't survive chat copy-paste otherwise.
- **Pre-modification discipline:** `sed -n` the current code before authoring any patch. Don't trust prior-session recon without verifying.
- **Paired programming:** Claude proposes → Gemini reviews → consensus → Andrew runs.
- **Push back when direction is wrong.** Tonight's session caught Gemini conflating "Path 1 (5-edit backend wiring)" with the actual ground truth (router was already wired; Android needed one DTO field). The same pushback discipline applies to Gemini's last memo about Claude Code's scope — large parts of it are stale.

---

## Bruno collection — note for future sessions

The Bruno `POST Decisions` body in Andrew's local collection has been using snake_case field names (`trip_miles`, `pickup_miles`, `time_minutes`, etc.) for months. The real Android wire format is camelCase (`tripMiles`, `pickupMiles`, `pickupMinutes`, `tripMinutes`, `cumulativeMiles`, etc.). Bruno requests historically returned `verdict: ACCEPT` while silently zeroing every numeric field via `float(p.get("tripMiles") or 0)` patterns.

**Action item for Andrew:** update the saved Bruno body to camelCase so future ad-hoc tests are accurate. Otherwise the next person to "verify" something with Bruno gets the same false-positive trap. The corrected body that produced row 8400 successfully on 2026-05-09 00:03 UTC:

```json
{
    "driver_id": "UjT1hE9eBXh2q95aSZYOkzDJ8lo1",
    "fare": 35.00,
    "pickupMiles": 0.5,
    "tripMiles": 8.6,
    "pickupMinutes": 5,
    "tripMinutes": 20,
    "lat": 29.7099153,
    "lng": -95.5455771,
    "pickupAddress": "Bellwood Ln & Ranchester Dr, Houston, Texas",
    "dropoffLat": 29.628468,
    "dropoffLng": -95.6509822,
    "dropoffAddress": "S TX-6, Sugar Land",
    "currentLat": 29.7200,
    "currentLng": -95.5400,
    "cumulativeMiles": 42.5
}
```

(Note `driver_id` stays snake_case — read in the auth/decorator path before `extract_payload`, has its own convention.)

---

## Open question worth flagging

`decisions/router.py:174`:

```python
pickup_miles = float(p.get("pickupMiles") or (pickup_min * 0.33))
```

That fallback derives `pickup_miles` from `pickupMinutes * 0.33`. With both at 0, the fallback also yields 0 (which is what Bruno's snake_case body produced). But if a real Android payload arrives with `pickupMinutes` populated and `pickupMiles` missing, the fallback would compute a fictional pickup distance and bind it as `expected_pickup_distance`, gating real PUDO commits via the 85%–115% TAD window.

**Question for the next session:** does real Android always populate `pickupMiles` in production payloads? If never-omitted, kill the fallback. If sometimes-omitted, the fallback's correctness needs verification. Audit before sprint close.

---

End of handoff.
