# Runbook — Live end-to-end validation of §5.5 trigger + §9.9 abandoned-status

**Purpose.** Drive the §5.5 lost-mode deferral trigger (`9acd504`) and the §9.9
out-of-window→`abandoned` sweep (`23dabb1`) through the **real
`post_heartbeat`/`/decisions` commit cycle** on the live service — the boundary the
unit floor (§XIV.J) mocks. Last run 2026-06-06 against rev `puddlejumper-api-00649-7sq`:
full matrix green (A1–A4, B1, B2, B3).

**Companion docs:** `docs/SENTINEL_DESIGN_SPEC.md` (what the behavior must be), the
auto-memory `live-e2e-validation-recipe` + `witness-infra-access` (access paths).

---

## 0. Safety (read first — non-negotiable)

- **Synthetic driver ONLY:** `C7FRKHbnvFcDPZ0RcXXjtPwGXgw2` (Firebase
  `pj_test@atjbruce.com`). NEVER the real driver `UjT1hE9eBXh2q95aSZYOkzDJ8lo1`. This
  run writes persistent `abandoned`/`active` status + fires dropoffs against the REAL
  DB with no rollback; on the real id that's permanent pricing/geographic cache poison
  (§0 — the caches are the product). The synthetic driver is the quarantine, enforced
  by construction because every request carries the synthetic JWT.
- **Identity gate before any write:** `GET /api/v1/test/inspect` must return `200` +
  `driver_id C7FRK…`.
- **Deploy ≠ push.** A `git push` does NOT redeploy Cloud Run. Confirm the deployed
  revision actually contains the fix under test before trusting any result.

---

## 1. Quick start (new Claude Code session)

1. Open a session in this repo (memory index auto-loads).
2. Tell the agent: *"Resume the §5.5/§9.9 live E2E harness — read
   `live-e2e-validation-recipe` from memory and follow `docs/RUNBOOK_LIVE_E2E_VALIDATION.md`."*
3. **Provide a fresh test-driver JWT** (the only thing the agent can't produce): mint via
   Bruno **"test login_1"** (Firebase `pj_test@`). ~1h life — it WILL expire mid-run
   (`/decisions` starts returning 401); paste a fresh one when asked.
4. Agent runs the preflight (§3), then whatever matrix rows you ask for (§5).

---

## 2. Environment

- Base URL: `https://puddlejumper-api-152974241923.us-central1.run.app`
- JWT in `/tmp/pj_jwt` (chmod 600); `AUTH="Authorization: Bearer $(cat /tmp/pj_jwt)"`.
- Endpoints:
  - `POST /api/v1/decisions` — the receipt (§5.5 trigger lives here). `offerId` is
    **required** and must be **UUID v7** (`python3 -c 'import uuid;print(uuid.uuid7())'`).
    Geocodes `dropoffAddress`, overwriting sent dropoff coords ~1km off.
  - `POST /api/v1/driver/heartbeat` — body `{lat,lng,speed_mph,gps_accuracy_m,cumulative_miles}`.
    The §9.9 sweep runs here, only on a real FireDropoff/FireDropoffObservation.
  - `POST /api/v1/test/seed_offer`, `POST /api/v1/test/reset_driver`,
    `GET /api/v1/test/inspect` (test-harness endpoints, allowlisted to the synthetic driver).
- DB read/verify (psql on the VM):
  ```
  gcloud compute ssh puddle-jumper --zone us-central1-f --project puddle-jumper-477316 \
    --tunnel-through-iap --ssh-flag="-o ConnectTimeout=15" \
    --command 'PW=$(gcloud secrets versions access latest --secret=DB_PASSWORD \
      --project puddle-jumper-477316); PGPASSWORD="$PW" \
      psql -h 10.128.0.2 -U atjb -d puddlejumper -P pager=off -c "…"'
  ```
  **Bound every VM call** (Bash timeout ≤80s). The IAP tunnel can hang mid-session
  (once hung 14 min); a bounded call dies fast and is retried.

---

## 3. Preflight (agent runs before any write)

1. Confirm deployed revision contains the fix under test:
   `gcloud run services describe puddlejumper-api --region us-central1 --project puddle-jumper-477316 --format="value(status.latestReadyRevisionName)"`
   (rev must postdate the commit you're testing).
2. `GET /test/inspect` → `200`, `driver_id C7FRK…`, and ideally `0/0/0` rows.
3. If not clean, run the teardown (§4).

---

## 4. Teardown (driver-scoped — REQUIRED between independent scenarios)

`reset_driver` only deletes `audit_source='test_harness'` rows, so it does NOT clean
`/decisions` rows (`audit_source=NULL`). Use this driver-scoped psql teardown instead.
`pickup_market_signals.offer_id` is **INTEGER** (no `::text` cast):

```sql
DELETE FROM app_private.pickup_market_signals WHERE offer_id IN (
  SELECT oh.id FROM app_private.offer_history oh
    JOIN app_private.decision_log dl ON dl.id=oh.decision_log_id
    WHERE dl.driver_id='C7FRKHbnvFcDPZ0RcXXjtPwGXgw2'
  UNION SELECT id FROM app_private.decision_log
    WHERE driver_id='C7FRKHbnvFcDPZ0RcXXjtPwGXgw2');
DELETE FROM app_private.offer_history WHERE decision_log_id IN (
  SELECT id FROM app_private.decision_log WHERE driver_id='C7FRKHbnvFcDPZ0RcXXjtPwGXgw2');
DELETE FROM app_private.pudo_decision_context WHERE driver_id='C7FRKHbnvFcDPZ0RcXXjtPwGXgw2';
DELETE FROM app_private.decision_log WHERE driver_id='C7FRKHbnvFcDPZ0RcXXjtPwGXgw2';
UPDATE app_private.driver_trip_state
  SET current_offer_id=NULL, heartbeat=NULL, heartbeat_at=NULL,
      last_odometer_move_at=NULL, arrest_started_at=NULL, arrest_counter_s=NULL
  WHERE driver_id='C7FRKHbnvFcDPZ0RcXXjtPwGXgw2';
```
Leave the synthetic driver clean (`0/0/0`) at the end of every session.

---

## 5. The matrix

### Group A — §5.5 trigger (via real `/decisions`)
A receipt `D` carrying `cumulativeMiles` defers **only** via lost-mode; if it lands
`deferred` with `miles_at_offer_receipt` non-NULL, that's the lost-mode path (the
odometer-absent net would have NULL recv). Confirm with the log line
`[§5.5 lost-mode defer] … alive-unpicked peer(s) [...]`.

| Row | Setup | Assert |
|---|---|---|
| **A1** lost-mode | seed peer X (alive-unpicked) → heartbeat (fresh `driver_trip_state`) → `/decisions` D with `cumulativeMiles` | D `deferred`, odo NULL, recv non-NULL; log shows lost-mode |
| **A2** idle (INV-A) | reset → `/decisions` D, `cumulativeMiles` present, NO peer | D `active`, idle anchor = cum + pickup_miles |
| **A3** stacked | seed/promote a picked-up live prev → `/decisions` D | D `active`, stacked anchor = prev.expected_dropoff_distance + pickup_miles |
| **A4** odometer-absent | reset → `/decisions` D **omitting** `cumulativeMiles` | D `deferred`, recv NULL |

Note: after a fresh `reset`, A2/A4 don't strictly need a heartbeat (no peer → staleness
irrelevant). A1 needs a heartbeat so the staleness gate is permissive (else X is reaped).

### Group B — §9.9 sweep (requires a REAL dropoff fire — see §6)
| Row | Setup | Assert |
|---|---|---|
| **B1** out-of-window → abandoned | defer D, then fire a dropoff for X whose window (`[X.actual_pickup_at, NOW()]`) does NOT contain D.created_at | D → `abandoned` |
| **B2** predicate excludes (live read) | after B1 | `GET /driver/status` `planner_queue` excludes D; `IS DISTINCT FROM 'abandoned'` = false for D |
| **B3** in-window → active | defer D INSIDE X's window | D → `active`, chained anchor = X.expected_dropoff_distance + D.pickup_miles |
| **B4** abandon w/o X anchor | unit-covered (`test_out_of_window_abandoned_even_when_x_has_no_anchor`) — skip live |
| **C1** regression / resurrection | ≡ B3 (the offer-10202-class resurrection) + prior witness |

**Ordering gotcha (B1/B3):** the §5.5 trigger defers ANY offer received while a
deferred/alive-unpicked peer exists. So receive **X first** (no peer → `active`), then
D. For **B1** promote X picked-up *after* D.created_at (D out-of-window). For **B3**
backdate `X.actual_pickup_at` *before* D.created_at (D in-window). Defer D via
odometer-absent (omit `cumulativeMiles`) when X is already picked-up (X no longer a peer).

---

## 6. The dropoff-fire recipe (the load-bearing part of Group B)

The WAI matcher is **not flaky** — it requires a cluster ≥5s dwell
(`cluster_detection.ARREST_DURATION_THRESHOLD_S = 5.0`). Requirements to fire a dropoff
for offer X:

1. **Dwell ≥5s, spaced.** Back-to-back curls (~0.2s apart) span <2s → no cluster →
   `matcher_candidates={}`, no fire. Send heartbeats **≥1s apart over ≥6s**. Foreground
   `sleep` is blocked in the harness → run the dwell as a `run_in_background` bash loop
   with `sleep 1`. (Verified: 12 hb @1s → `cluster_duration_s≈15` → fires.)
2. **Dwell at an exact cached-POI coord:** Terminal C IAH `29.98671,-95.34951`,
   `speed_mph=0`, `cumulative_miles ≈ X.expected_dropoff_distance`. POI lookup logs
   `[RESURRECTION] … N witnesses`.
3. **X must be in `per_offer_state`:** `_assemble_per_offer_state` EXCLUDES rows with
   NULL `expected_pickup_arrival_time` / `expected_pickup_distance` /
   `miles_at_offer_receipt` (logs `offer … missing from per_offer_state — TAD-failed`).
   A bare `seed_offer` row lacks the pickup anchors → use an X received via `/decisions`
   (which sets them) or psql-set them.
4. **X must be picked-up + bound + dropoff-pinned:** `actual_pickup_at NOT NULL`,
   `current_offer_id = X`, and re-pin `dropoff_lat/lng` + `dropoff_h3=app_private.safe_h3(...)`
   to the exact POI coord (because `/decisions` geocoded it ~1km off).
5. **Verify via forensic:** `app_private.pudo_decision_context` (latest row) —
   `arrest_duration_s` (≥5), `cluster_size`, `cluster_duration_s`, `matched_offer_id`,
   `unmatched_reason`, `dispatch_executed`. `queue_actually_empty` after a fire means the
   dropoff completed and the live projection now excludes the swept offer.

---

## 7. Operational notes

- **JWT expiry (~1h):** `/decisions` starts returning `401`. The psql/teardown calls
  use the DB password (unaffected). Ask for a fresh `idToken` and continue.
- **gcloud ssh / IAP tunnel hangs:** bound every VM call; retry on stall.
- **Other heartbeat traffic:** real devices (`Ktor client`) post heartbeats too —
  separate `driver_id`, harmless to the synthetic driver. For perfectly isolated runs,
  deploy a **no-traffic tagged** Cloud Run revision (`--no-traffic --tag <t>`) and point
  the harness at the tagged revision URL; live traffic stays on the current revision.
- **Doctrine:** fix → test → **drive**. This runbook is the "test" leg. A real lost-mode
  drive remains the final confirmation, but every path it exercises has now fired green
  here against the synthetic driver.
