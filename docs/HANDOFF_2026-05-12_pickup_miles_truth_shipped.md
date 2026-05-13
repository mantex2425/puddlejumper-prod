# Handoff brief — pickup_miles truth-flow SHIPPED, drive-test evaluation pending

**Date:** 2026-05-12
**Branch:** `phase-2c-2-tad-exit-4tools` (server) / `feature/uuid-v7-validation` post-merge (Android)
**Cloud Run rev serving 100%:** `puddlejumper-api-00604-2rs`
**Pytest floor:** 584/584
**Last commit (server):** `66361e6` — fix(pickup_miles): trust client input, kill 0.33 heuristic
**Last commit (Android):** `95c20d20` on `feature/pickup-miles-decide`, merged into `feature/uuid-v7-validation` as `998d55d7`

---

## What just shipped

### The 0.33 heuristic is dead

For ~6 weeks, `decisions/router.py:175` silently substituted `pickup_minutes × 0.33` for `pickup_miles` because Android never sent `pickupMiles` in `/decide` payloads. The bug was invisible in headline economics (19.8 mph deadhead is a plausible Houston approximation) but corrupted TAD's pickup-leg distance gate and every downstream system that needed real pickup miles. The stored procedure had its own GPS-1.3× override that masked the bug further by replacing the corrupted heuristic with a different approximation whenever `gps_pickup_dist > pickup_miles_in`.

Both layers fixed atomically in three commits on 2026-05-12:

1. **Server router.py:** `pickup_miles = float(p.get("pickupMiles") or (pickup_min * 0.33))` → `_raw_pickup_miles = p.get("pickupMiles"); pickup_miles = float(_raw_pickup_miles) if _raw_pickup_miles is not None else None`. WARN logged when NULL. YOLO-guard hardened against `None > 0` TypeError.

2. **Server migration `2026-05-12_trust_input_distances.sql`:** in `app_private.decision_engine_v2`, replaced both `IF pickup_miles_in < 0.1 OR v_gps_pickup_dist > pickup_miles_in` and the sibling trip_miles predicate with `IF pickup_miles_in IS NULL OR pickup_miles_in < 0.1`. GPS-1.3× fallback retained only for the "input missing or garbage" case. Symmetrical fix for trip_miles per Rule VII.

3. **Android DTO + 2 call sites:** added `val pickupMiles: Double? = null` to `OfferDecisionRequest` (core-dto/.../DecisionDtos.kt), wired `pickupMiles = offer.pickupMiles` in ScreenshotMonitorService.kt:744 production constructor and TestingLabViewModel.kt:192 test harness. OCR extraction was already correct in `OfferParser.kt`; just plumbing the existing value through.

### Validated end-to-end against a real Uber offer

2026-05-12 12:03 Texas time, $11.05 UberX offer, 13 min / 5.2 mi pickup → 24 min / 13.7 mi trip from Bellaire area to Sienna Ranch Rd. Database row 8470:
- `pickup_miles = 5.20` (matches card exactly; old heuristic would have been 13 × 0.33 = 4.29)
- `trace.pickupMiles = 5.2` (stored proc accepted input verbatim, no GPS override)
- `verdict = ACCEPT`, `reason = "Rates met"`
- Zero WARN logs (post-Patch-A APK)
- Zero ERROR logs

### Apply scripts

- `tmp/apply_pickup_miles_truth_2026-05-12_v2.sh` — L-3 dual-role auth (atjb reads from Secret Manager, postgres DDL from ~/.pgpass). Pattern worth reusing for future migrations touching postgres-owned functions.
- `tmp/decision_engine_v2_pre_2026-05-12.sql` — rollback dump of pre-migration function body.

---

## Open verification: stacked-offer test drive

Andrew is doing test drives — both simple single rides and stacked offers — to evaluate the fix in real conditions. Two evaluation dimensions:

### Dimension 1 — pickup_miles correctness

Per-offer verification query (run after each offer):

```sql
SELECT id, created_at::time AS time,
       pickup_minutes AS pmin,
       pickup_miles AS pmi_stored,
       ROUND((pickup_minutes * 0.33)::numeric, 2) AS heuristic_would_have_been,
       trace_data->>'pickupMiles' AS effective_pmi,
       trace_data->>'gpsPickupDist' AS gps_pdist,
       fare, trip_miles,
       decision_result->>'verdict' AS verdict,
       CASE WHEN pickup_miles IS NULL THEN 'NULL (old apk)'
            WHEN pickup_miles = ROUND((pickup_minutes * 0.33)::numeric, 2) THEN 'heuristic match — confirm card'
            ELSE 'real OCR value (FIX WORKING)' END AS data_source
FROM app_private.decision_log
WHERE driver_id = 'UjT1hE9eBXh2q95aSZYOkzDJ8lo1'
  AND created_at > NOW() - INTERVAL '4 hours'
ORDER BY id DESC;
```

Cross-reference `pmi_stored` against the actual offer card screenshot pickup miles. Should match within OCR rounding.

### Dimension 2 — TAD pickup-leg gate behavior

The original symptom that led us to find this bug: TAD's pickup-leg gate was misfiring because it divided odometer travel by the corrupted `pickup_miles`. Now that the column is correct, TAD's 85%/115% acceptance window should land at the right odometer position. Specifically watch for:

- `pudo_decision_context.pickup_passed = true` only when driver is genuinely within 0.85×–1.15× of card pickup_miles from leg start
- No premature `narrative_violation` latches on the pickup leg
- PUDO observation fires once driver actually arrives at the pickup pin

Investigation query for any anomalies:

```sql
SELECT pdc.decision_log_id, pdc.heartbeat_ts::time, pdc.elapsed_miles,
       pdc.pickup_passed, pdc.pickup_lower_bound, pdc.pickup_upper_bound,
       pdc.tad_verdict, pdc.narrative_violation_reason
FROM app_private.pudo_decision_context pdc
WHERE pdc.heartbeat_ts > NOW() - INTERVAL '4 hours'
ORDER BY pdc.heartbeat_ts DESC
LIMIT 50;
```

### Dimension 3 — Stacked offer specifics

In a stacked offer, `trip_miles` is the most stable data point per Gemini's analysis. The trip_miles symmetrical fix should make stacked-offer economic decisions sharper since the dropoff anchor is now driven by real card miles, not GPS approximations.

Watch for:
- Stacked offer arrives mid-trip → `pickup_miles` in payload is the deadhead from current position to next pickup
- Server stores it correctly (verification query above applies)
- TAD's pickup-leg gate for stacked offer uses the right axis

---

## What the bug DID NOT break (confirmed yesterday 2026-05-11)

The handoff brief that started this investigation claimed "Auto Nail It has not fired since 2026-05-08." **This was false.** PUDO observations fired on yesterday's drive — offers 8462 (dropoff at 18:36), 8463 (full pickup-dropoff sequence), 8467 (pickup at 20:16). The brief was wrong; no fix needed there. PUDO observation path is healthy.

Also noted: offer 8462 was an `app_verdict='DECLINE'` that still got a dropoff observation. Validates Canonical Rule IV.4 ("observation is not gated on verdict"). Working as designed.

---

## Latent items worth knowing about (not blockers)

### `logger.py:214` Horizon Budget GC fallbacks
Hardcoded `or 5` (pickup_miles) and `or 10` (trip_miles) in `decisions/logger.py:200-214` for the Horizon Budget GC kill mechanism. These are **deliberate safety defaults** for when `prev_row` is missing — not the same architectural sin as router.py:175. Out of scope for this sprint; documented for future readers.

### Offer 8467 missing dropoff
Yesterday's offer 8467 had a pickup observation but no dropoff observation. Could be (a) session ended before trip completed, (b) dropoff convergence didn't trigger, or (c) trip still in flight from system perspective. Worth a glance if it bugs Andrew, but not a P0.

### Pre-Patch-A APK WARN tracking
Once Andrew starts driving more, watch for any `/decide offer missing pickupMiles` WARN logs. These identify pre-fix APK installations. Useful population data for when soft-NULL can be hardened to 400-reject. Current expectation: zero WARNs from R5CY733RF9R since it's on the post-merge build.

### Two stale SQL source files
`decision_engine_v2_current.sql` and `decision_engine_v2_live.sql` in repo root are both older than the deployed function body and now older than the migration. The migration is the source of truth. Andrew may want to either delete these or sync them to the new deployed body. Not urgent.

---

## How yesterday's session worked (process notes)

- Three rounds of paste-safety issues with SQL inside `{ }` heredoc blocks — bash interprets `(`, `*`, quotes as shell metacharacters even inside command-group redirects. **Lesson:** SQL with quotes/parens belongs in standalone `psql` calls or via `-c` directly, never nested inside `{ cmd; cmd; }` recon blocks. The grep/sed parts of recon blocks are fine; SQL needs to stand alone.
- L-3 v1 apply script hardcoded `-U postgres` and failed when the user's env had postgres pw missing. v2 dual-role auth (atjb via Secret Manager + postgres via ~/.pgpass) is the new pattern.
- Handoff brief from prior session had hallucinated DB values for offers 7838-7843; real offers were 8462-8467 (5-hour CDT/UTC offset confusion). **Lesson:** trust live DB output over handoff narrative. Always re-query.

---

## Active canonical rules to keep in mind

- Rule VII (do the right thing, not the easy thing) — applied successfully on trip_miles bundling and on declining the "TAD reads trace_data" shortcut.
- Canonical Rule IV.4 (`app_verdict` is not an observation gate) — validated by 8462's dropoff observation on a decline.
- Truth ordering GPS > Cluster > WAI > Memory — still active for matcher / PUDO commits.
- COORDS: always (lat, lng), never raw `ST_MakePoint`/`h3_latlng_to_cell`/`h3_cell_to_latlng`.
- TIME: UTC in Postgres, Texas-time at UI edge only.

---

## Honest note for the next Claude

You're inheriting a clean state. Server fix is shipped, validated, working on the first real offer that hit production. Android side is shipped and on the test device. Andrew is doing drive-tests to evaluate.

The pickup_miles bug was an architectural sin that hid in plain sight because the stored proc's GPS-1.3× override produced approximately-correct math by coincidence. The full chain (router heuristic + SQL second-guess) is dead now.

Don't trust the prior handoff's specific claims without re-verifying — the previous-previous Claude hallucinated DB row values, and yesterday's "Auto Nail It silence" claim was provably wrong against live DB data. Re-query everything.

If Andrew comes back with drive-test data, run the verification queries above and look for the pickup_miles match against offer cards. The fix is real and should hold; any anomalies are either (a) edge cases worth understanding or (b) the next bug.

Working files convention (memory #22 updated 2026-05-12): ALL VM-side ephemeral content goes in `~/puddlejumper-prod/tmp/`, NOT `/tmp/`. Apply scripts, recon dumps, SQL captures — all of it.
