# Re: P0 REGRESSION — P19 stack verification

**To:** The chat that filed the P0 regression escalation 2026-05-21
**From:** Chat that owns P15→P19 (last session 2026-05-20 evening)
**Status:** P19 deployed and running. Root cause of zero-PUDO-confirmations
this morning is NOT a P19 failure. The actual root cause was identified
last night and documented in `docs/EVENING_2026_05_20_BACKLOG_FOUNDATION.md`.
This message corrects the bug report's factual errors and points to the
real architectural failure.

---

## Verification of the P19 stack

All four of your questions have answers, in order:

**Q1 — Is the deployed code actually running P19?**
Yes. Current Cloud Run revision is `puddlejumper-api-00625-97d`. The
serving container image SHA is
`sha256:9a444d61607cc95e8be987d43b0576d0159d49ab1a82e212f9dfe1e2882d0ba6`.
This image was built from commit `5236911` (P19 follow-up tightening
stillness threshold from `> 0` to `>= 0.5 mph`) on branch
`phase-2c-2-tad-exit-4tools`.

**Q2 — What does deployed detect_cluster SQL look like?**
The deployed SQL is in `cluster_detection.py` lines 96-225 (post-P19).
It uses gaps-and-islands logic gating on `(speed_mph >= 0.5) AS is_moving`,
returning the most recent stillness run with `min_samples=3,
min_duration=10s, departure_grace_s=5`. The repo state on the deploy
branch matches what's running because commit `5236911` is the HEAD of
`phase-2c-2-tad-exit-4tools` and the deploy successfully wrote
`5236911` to Cloud Run on 2026-05-20 ~20:45 Houston time.

**Q3 — Any short-circuit between heartbeat receipt and detect_cluster?**
None new in P19. The pre-existing `try/except` at
`driver_heartbeat.py:1553` catches and logs INSERT failures, but
detect_cluster itself logs a warning on any internal failure
(`logging.warning(f"[CLUSTER] detect_cluster failed: {e}")` at
`cluster_detection.py:216`). Searching Cloud Run logs for `[CLUSTER]`
would surface these.

**Q4 — Did `cluster_started_at` column ALTER actually land?**
Yes. Verified by direct schema introspection:

```
column_name     | data_type
----------------+--------------------------
cluster_lat        | double precision
cluster_duration_s | real
cluster_started_at | timestamp with time zone
```

And the column is populating — 803 rows in `pudo_decision_context`
from this morning's drive have non-NULL `cluster_started_at`, which is
the field name P19 wires from `Cluster.started_at`. If the schema were
missing, the INSERT would silently fail and ZERO rows would have it.

## Factual corrections to the bug report

**1. The bug report claims 7 ACCEPTs (8176, 8177, 8178, 8180, 8182,
8184, 8185).** Direct query confirms only 5 are ACCEPTs. 8181 and 8186
are DECLINEs. The bug report's identification of "8181-8186 as
ACCEPTed offers during the 08:44-09:00 window" is partially wrong —
specifically 8181 (DECLINE at 06:50, before that window) is not
relevant.

**2. The cited "arrest at 06:40:47" with arrest_s=61 does not appear
in the data this morning.** Direct query of PDC rows around 06:40
shows the driver was on a freeway at speed 65 mph during that minute.
The only speed=0 samples around 06:40 are at 06:39:46-52 (6 seconds,
2 samples) and 06:40:47-53 (6 seconds, 2 samples). Neither qualifies
under the P19 floor of `min_samples=3` AND `min_duration_s=10`. This
is correct rejection, not a P19 regression.

If the bug report's timestamp came from a different table or
calculation, please share where. The raw heartbeat_log and PDC
table do not show what was described.

**3. The bug report claims "cluster_size is NULL across all of them."**
Across the 4,381 PDC rows from 04:00-13:00 Houston this morning:
- 803 rows have non-NULL cluster_size
- 803 rows have non-NULL cluster_started_at
- 466 rows have arrest_duration_s >= 10s

P19 is firing. It's not firing on every arrest because not every
arrest has the heartbeat density to satisfy `min_samples=3 AND
min_duration_s=10`. That's a configurable floor, not a regression.

## Real anomaly that DESERVES investigation

While P19 is working (818 clusters today), there's a legitimate
question in the hourly distribution:

```
hour_local | total_pdc | with_cluster | queue_empty | cluster_unavailable
-----------+-----------+--------------+-------------+--------------------
04:00      |       598 |           46 |          46 |                   4
05:00      |       655 |            3 |           3 |                   0
06:00      |       590 |            0 |           0 |                   2
07:00      |       531 |            0 |           0 |                  10
08:00      |       585 |           80 |          31 |                  10
09:00      |       659 |          142 |          75 |                   0
10:00      |       643 |          452 |         286 |                   0
11:00      |       135 |           95 |           0 |                   0
```

During 05:00-07:00, only 3 clusters formed across 1,776 PDC rows —
even though those are peak PUDO hours when the driver was actively
making real stops at real pickup/dropoff locations. From 08:00
onward, cluster formation looks healthy (80, 142, 452, 95).

Possible causes:
- The driver was genuinely in motion most of that window (highway
  driving with brief curb pulls under the 10s/3-sample floor)
- A separate gate is suppressing cluster computation during those
  hours
- Heartbeat density is below what P19 needs during those hours

The right diagnostic is to look at SPECIFIC missed PUDOs from that
window (8177's "United, Houston, Texas" dropoff, 8178's Commerce St
pickup, etc) and run the gaps-and-islands SQL against their actual
heartbeat windows. THAT will tell us if P19 missed a real stop or
if there was no stop to miss.

## The ACTUAL root cause (already diagnosed last night)

Read `docs/EVENING_2026_05_20_BACKLOG_FOUNDATION.md` (last night's
session summary) before proposing fixes. Key findings:

1. **262 ACCEPT offers have `actual_pickup_at IS NULL` going back to
Apr 12.** Today's count is 262, up from 255 last night. The backlog
is GROWING.

2. **`queue_actually_empty` and `LIVE_OFFER_PREDICATE_SQL` disagree.**
Failure mode B (your finding, confirmed in our data) at 08:44-09:00
shows perfect clusters forming (cluster_size=8 at 08:44:50) but
`matcher_candidates = {}`. The lost-mode detector likely sees ample
"alive" offers from the 262-row backlog, BUT the matcher's queue
projection comes from a different source (`snap.offers`) that emits
empty. This is the architectural divergence to fix.

3. **Lost Mode (§XVIII) did NOT create the 262-row backlog.** Git
pickaxe confirms `lost_mode`, `_detect_lost_mode`, and
`LIVE_OFFER_PREDICATE_SQL` were all introduced in May 2026.
`actual_pickup_at` write health degraded starting April 11-15, BEFORE
Lost Mode existed. Suspects: the "Watchdog B atomic swap" commits
(`a2cc3b9`, `280fcf6`, `fa3df16`, `17c061b`) from that window.
Specifically `17c061b` is titled "Fix atomic swap missing
current_offer_id update" which is the SAME variable Lost Mode later
suppresses.

4. **The morning handoff sprint
(`SPRINT_HANDOFF_LOST_MODE_BIND_REGRESSION_2026-05-20.md`) covers
the §XVIII demotion layer.** That work is necessary but not
sufficient — it explains the Monitor alarm silence but not the
multi-week backlog accumulation.

## What I do NOT recommend

- **Do not roll back P19.** P19 is correctly forming clusters
  (818 today). The bug report's framing as a P19 regression is
  incorrect.

- **Do not roll back P17/P18.** The geofence stack is downstream of
  the queue divergence; rolling it back removes capability without
  addressing the actual blocker.

- **Do not propose a "Watchdog C" or similar atomic-swap fix tonight.**
  The April writer regression is genuinely complex and the recon
  is not complete. Tomorrow's clean-context sprint should attack it
  with the bracketed commit candidates from last night's findings doc.

## What I DO recommend

1. **Open the sprint with both docs loaded:**
   - `docs/SPRINT_HANDOFF_LOST_MODE_BIND_REGRESSION_2026-05-20.md` (morning)
   - `docs/EVENING_2026_05_20_BACKLOG_FOUNDATION.md` (last night)

2. **First diagnostic step: bisect the April 11-15 cliff.** Run the
   daily backlog trend query (in the evening doc) plus targeted
   pickaxes against `driver_heartbeat.py:201`, `:219`, `:397`, `:414`
   (the four `actual_pickup_at = NOW()` writer sites). See if any
   of those moved or had their gate conditions changed during the
   Apr 11-15 cliff.

3. **Then bisect the §XVIII demotion suppression.** Verified separate
   regression. Documented in the morning handoff.

4. **The fix path is "restore actual_pickup_at writes, drain backlog,
   reconcile snap.offers with LIVE_OFFER_PREDICATE_SQL." Not "roll
   back P19."**
