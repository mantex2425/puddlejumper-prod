# Server-Side Sprint 1 (Identity Genesis) — Accept and Persist `offerId`

**Date:** 2026-05-09 (late evening)
**For:** A fresh session on the `puddle-jumper` VM in `~/puddlejumper-prod`, branch context to be confirmed at session start
**From:** Architecture-chat session, after consuming Claude Code's handoff brief and applying corrections
**Supersedes:** `~/Downloads/CLAUDE_CODE_HANDOFF_SERVER_SIDE_OFFER_ID_2026-05-09.md` (Mac-side brief had two factual errors corrected here)

---

## TL;DR

Android APK 1.1.20 (PR #1 on `mantex2425/Puddle_Jumper`, branch `feature/uuid-v7-validation`) sends `offerId` (UUIDv7, RFC 9562 §5.7) on every `POST /api/v1/decisions/`. Real-device validation across 4 distinct presentations confirms one-presentation-equals-one-UUID after Sprint 2 client-side dedup.

**Server-side mission:** accept the field, validate format, persist into `decision_log.trace_data`, deploy. **Forward-compat only — no schema migration, no PK changes, no consumer-side use of the field yet.**

After this lands, Andrew drives 3-5 real offers. Each must produce a populated `decision_log.trace_data->>'offerId'` matching the UUID logged on his phone. That is the verification gate before Server-Side Sprint 2 (the schema migration) begins.

---

## Wire format — verified on real hardware

Captured from APK 1.1.20 on driver `UjT1hE9eBXh2q95aSZYOkzDJ8lo1`:

```json
{
  "offerId": "019e100f-0c25-7c84-84e9-51ed3be48a72",
  "fare": 30.16,
  "pickupMinutes": ...,
  "tripMinutes": ...,
  "tripMiles": ...,
  "marketId": "...",
  "lat": ..., "lng": ...,
  "currentLat": ..., "currentLng": ...,
  "gpsAgeSec": ...,
  "isPuddleJumpMode": true,
  "towardsActive": false,
  "pickupAddress": "...",
  "dropoffAddress": "...",
  "cumulativeMiles": ...
}
```

Field invariants:
- **camelCase** `offerId` — matches existing `p.get("tripMiles")` convention in `decisions/router.py`
- **Required** on the wire — Android's DTO has no default; every Android caller supplies one
- **UUIDv7** per RFC 9562 §5.7 — version digit `7` at canonical position, time-ordered, lexicographically sortable by mint time
- **One per Uber offer presentation** — Sprint 2 client-side dedup collapses burst-shot duplicates from a single `enterOfferMode` window. Distinct Uber presentations → distinct UUIDs.

---

## What server-side actually needs to do

### Correction to the Mac-side brief

The Mac-side brief said "Possibly zero code if `trace_data` already passes the body through." **This is incorrect.** Recon Step 4 (saved to `/tmp/x3_recon_step4.txt` from earlier in this session) shows `decisions/logger.py:32-37` builds `trace_payload` as a curated dict, not a passthrough:

```python
trace_payload = json.dumps({
    **(ep["_raw_trace"]),
    "arc_band":         ep["_arc_band_trace"],
    "gps_age_sec":      ep["gps_age_sec"],
    "cumulative_miles": ep["cumulative_miles"],
})
```

So `offerId` will NOT appear in `decision_log.trace_data` automatically. Code change is required.

### Required changes

**1. `decisions/router.py` — accept and validate the field.**

Locate the `parse_request(p, uid)` block (around lines 170-216 in the current `phase-2c-2-tad-exit-4tools` branch — verify with `grep -n "p.get" decisions/router.py | head -20` at session start since line numbers may drift).

Add field extraction with format validation:

```python
import uuid as uuid_module

# ... existing p.get(...) extractions ...

raw_offer_id = p.get("offerId")
if raw_offer_id is None:
    return jsonify({"error": "missing required field: offerId"}), 400
try:
    parsed = uuid_module.UUID(raw_offer_id)
    if parsed.version != 7:
        return jsonify({
            "error": "offerId must be UUID v7 (RFC 9562)",
            "got_version": parsed.version,
        }), 400
    offer_id = str(parsed)  # canonical form
except (ValueError, AttributeError):
    return jsonify({
        "error": "offerId is not a valid UUID",
        "got": raw_offer_id[:50] if isinstance(raw_offer_id, str) else type(raw_offer_id).__name__,
    }), 400
```

Then thread `offer_id` into the `ep` dict construction (or wherever `_raw_trace` is assembled — the existing pattern for other request fields).

**2. `decisions/logger.py:32-37` — persist into `trace_data`.**

Add `offer_id` to the curated `trace_payload` dict:

```python
trace_payload = json.dumps({
    **(ep["_raw_trace"]),
    "arc_band":         ep["_arc_band_trace"],
    "gps_age_sec":      ep["gps_age_sec"],
    "cumulative_miles": ep["cumulative_miles"],
    "offer_id":         ep.get("offer_id"),  # NEW — UUIDv7 from Android
})
```

(Note the snake_case key inside the JSONB — matches the rest of the trace fields' style. The wire-format field is `offerId` camelCase to match Android conventions; the persisted key is `offer_id` snake_case to match Python/Postgres conventions. The `parse_request` step is the conversion point.)

### Why server-side validation despite Android contract

Push-back on the Mac-side brief's claim that validation is unnecessary because "Android guarantees v7":

1. **Defense in depth.** Android's contract honoring is real today; tomorrow there could be a buggy build, an iOS client, or a malicious-replay scenario. Server-side validation is contract enforcement.
2. **Cost is trivial** (~10 LoC) and the failure mode is loud (HTTP 400 with diagnostic body) rather than silent (corrupt data in trace_data).
3. **The X3 bug existed because the producer's contract claim was trusted blindly.** The α-fix comments said "`action.offer_id` IS `offer_history.id`" and were wrong; nobody verified. Server-side validation breaks that anti-pattern at the gateway.

If validation is rejected by Andrew or Gemini, document the rejection reason. Don't silently skip it.

---

## What this is NOT

- ❌ A schema migration. `decision_log.id` stays `integer`. `offer_history.id` stays `bigint`. The `offerId` lives **only** in `trace_data` JSONB for now.
- ❌ Anything in `pudo_decision_context`, `driver_trip_state`, `heartbeat_log`, or the heartbeat path. Those tables continue using their existing integer IDs.
- ❌ Server-side dedup. Android already enforces 1:1 via Sprint 2 dedup. The server can trust the field.
- ❌ A change to the `Offer` Python class or the queue layer. WAI/dispatch still ship `decision_log.id`-based identity. The new `offer_id` is a parallel field that future migration work will promote to canonical.
- ❌ A merge of PR #1. That waits until server-side acceptance is verified.

---

## Verification SQL

After deploy, on the next decision request from Andrew's phone:

```sql
SELECT dl.id              AS dl_id,
       dl.created_at,
       dl.trace_data->>'offer_id'   AS offer_id_persisted,
       LENGTH(dl.trace_data->>'offer_id')        AS uuid_len,
       SUBSTRING(dl.trace_data->>'offer_id' FROM 15 FOR 1) AS version_nibble
FROM   app_private.decision_log dl
WHERE  dl.driver_id = 'UjT1hE9eBXh2q95aSZYOkzDJ8lo1'
ORDER BY dl.created_at DESC
LIMIT  3;
```

Expected on the most recent post-deploy row:
- `offer_id_persisted` is non-NULL — UUID-shaped string
- `uuid_len` = 36
- `version_nibble` = `7` (RFC 9562 §5.7 layout)

Cross-reference against the Android logcat line `Offer minted: <uuid> fare=<n>` from `ScreenshotMonitor` tag at the same wall-clock minute. The two values must be equal.

---

## Test surface

Two unit tests should land alongside the implementation, in `tests/test_decisions_router.py` (or wherever decision-router unit tests already live — `find tests/ -name "test_*router*"` to locate):

1. **Valid UUID v7 accepted, persisted to trace_data:** mock request body with `"offerId": "019e100f-0c25-7c84-84e9-51ed3be48a72"`, assert handler returns 2xx, assert `trace_data["offer_id"]` matches.
2. **Invalid format rejected with 400:** test cases for missing field, non-UUID string ("foo"), UUID v4 (random), UUID v1 (timestamp-based but wrong version), empty string, non-string type.

These are pure unit tests against the router function; no DB required. Should run inside the existing `pytest -x --tb=short` invocation that the SESSION_PROTOCOL doc specifies.

---

## Forward-compat verification gate (the launch-readiness moment)

After deploy, **Andrew drives 3-5 real offers in production.** For each:

1. Check Android logcat: capture `Offer minted: <uuid> fare=<n>` lines
2. Check `decision_log.trace_data->>'offer_id'` for that driver/minute
3. Confirm match

If all 3-5 match cleanly, **Server-Side Sprint 2 (the schema migration to make UUID canonical) is unblocked.** That's the next major sprint and lives in its own design conversation with INDEX.md, CANONICAL_RULES.md, the recon outputs, and this brief loaded fresh.

If any of the 5 fails, diagnose before proceeding. Likely failure modes:

- HTTP 400 returned because Android sent something Python's `uuid.UUID()` can't parse → check Kotlin library output format (shouldn't happen given Sprint 1 validation, but verify)
- Field arrives as None despite client sending → likely DTO serialization issue or proxy stripping; investigate request body capture
- Validation passes but trace_data write fails → check the `trace_payload` dict construction in `logger.py`

---

## Out of scope: optional follow-ups (architecture decision required before pulling forward)

These came up during recon but were deliberately excluded from this brief's mandatory scope:

- **Symmetric `[DEDUP]` audit-log coverage on the Android side.** Tracked in PuddleJumper polish backlog. Doesn't affect server-side work.
- **Promote `offer_id` from `trace_data` JSONB to a first-class indexed column on `decision_log`.** This is part of Server-Side Sprint 2's work, not this sprint.
- **`UNIQUE` constraint on `(driver_id, trace_data->>'offer_id')`.** Same — Sprint 2.

---

## Branch & commit guidance

This work lands on the existing `phase-2c-2-tad-exit-4tools` branch (or a child branch off it — Andrew's call). Discrete commits:

1. `feat(decisions): accept and validate offerId UUIDv7 from Android decision request`
2. `feat(decisions): persist offer_id into decision_log.trace_data`
3. `test(decisions): unit tests for offerId validation and persistence`

Total expected diff: ~30 LoC across 2 files plus 1 test file. Per the L-3 envelope rule, an apply script is overkill for this size — direct edits via `str_replace` or careful manual edits with `git diff` review are fine. Andrew runs the apply, runs `pytest -x --tb=short` to verify the new tests pass and existing tests don't break, deploys via `bash deploy.sh` from `~/puddlejumper-prod/`.

---

## Coordination

Once the server-side change is deployed and the verification SQL returns a populated `offer_id_persisted` on a fresh decision_log row from Andrew driving:

1. Andrew announces server-side acceptance complete
2. PR #1 on the Android side becomes mergeable to main (Andrew's call on timing — could merge immediately, could hold pending more validation drives)
3. Server-Side Sprint 2 (the canonical-identity migration) opens in a fresh chat with the design doc fully loaded

---

## Recon outputs to load into the fresh session

If the next session opens cold (recommended — this session is past natural complexity boundary), pre-load:

- `docs/IDENTITY_GENESIS_DESIGN_2026-05-09.md` — the architectural decision, ratified by Gemini
- `docs/X3_FINDINGS_2026-05-09.md` — the root-cause diagnostic
- `docs/recon/x3_recon_step{1,2,3,4,5,6}.txt` — verbatim recon outputs (`cp /tmp/x3_recon_step*.txt ~/puddlejumper-prod/docs/recon/` if not yet persisted)
- `CANONICAL_RULES.md`, `SESSION_PROTOCOL.md`, `INDEX.md` — standard load
- This brief

---

End of brief.
