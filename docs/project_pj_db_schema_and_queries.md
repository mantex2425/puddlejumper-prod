Write a new canonical memory file at 
~/.claude/projects/-Users-abruce/memory/project_pj_db_schema_and_queries.md
and add an index line to MEMORY.md.

This is canonical reference material — schemas and sample queries that every 
PJ debugging session needs. The goal is that opening a new chat means 
loading this file gives you everything you need to start querying without 
schema hunting.

Contents:

## File header
project_pj_db_schema_and_queries.md
"PuddleJumper database schema + canonical debugging queries"
Last updated 2026-05-22
Tags: pj, db, schema, debugging, canonical-reference

## Connection
psql -h 10.128.0.2 -U postgres -d puddlejumper
Driver ID for primary user: UjT1hE9eBXh2q95aSZYOkzDJ8lo1
All schemas under app_private.

## Tables documented (with full \d output and column purpose annotations)

### app_private.offer_history
The system of record for offers received from Uber. Every offer card scraped 
by Android lands here as a row. Modified by:
- Initial insert: when Android OCR's an offer card and POSTs to /decisions
- actual_pickup_at: written by server-side FirePickupObservation planner action
- actual_dropoff_at: written by server-side FireDropoffObservation planner action
- expected_dropoff_distance: written when pickup fires

Key columns to remember:
- id (BIGSERIAL): the offer ID
- app_verdict ('ACCEPT' | 'DECLINE'): driver's button press. PUDO 
  observation layer treats both equally per canonical Rule XV.
- pickup_lat/pickup_lng (DOUBLE PRECISION, nullable): from geocoding the 
  address. NULL when geocoding failed or address was truncated.
- pickup_address, dropoff_address (TEXT): from Android OCR
- miles_at_offer_receipt (NUMERIC): odometer reading when offer arrived
- pickup_miles, trip_miles (NUMERIC): from offer card
- expected_dropoff_distance (NUMERIC): computed at pickup-fire time, used 
  by post-pickup distance gate
- actual_pickup_at, actual_dropoff_at (TIMESTAMPTZ): observation timestamps

### app_private.pudo_decision_context (PDC)
Forensic flight-recorder. Every heartbeat writes one row. Not a state table 
— a log. ~6-second cadence per driver.

Key columns:
- driver_id, created_at: composite identity for a heartbeat
- lat, lng: raw GPS from Android body.get('lat'/'lng'). The smoking gun 
  for sensor-staleness bugs. If lat/lng repeats across many rows, Android 
  isn't sending fresh GPS.
- speed_mph, heading, gps_accuracy_m, gps_age_s: derived sensor fields.
  gps_age_s is GPS staleness in seconds at heartbeat moment. Populated
  by Android since APK 1.1.49 (commit e8343587, 2026-05-22; merged in
  PR #5). Sub-second values are normal and healthy (production p50
  ≈ 0.5s, p90 ≈ 0.9s, p99 ≈ 1.0s as of 2026-05-22). NULL on pre-1.1.49
  rows (legacy). Canary threshold for "something is wrong with
  location callbacks" is roughly > 5.0s sustained.
- cluster_lat, cluster_lng, cluster_size, cluster_duration_s, 
  arrest_duration_s: cluster detection output for this tick.
- planner_action: what the server decided (FirePickupObservation, 
  FireDropoffObservation, noop, etc). Indexed for non-noop.
- match_signal: 'lost_mode_observation', 'lost_mode_ambiguous_observation', 
  'no_match', or NULL. Witness for why a match happened or didn't.
- matched_offer_id (TEXT): the offer this tick resolved to, if any.
- matcher_candidates (TEXT[]): all offers the matcher considered.
- tad_decision_context (JSONB): full TAD verdict blob for this tick.
- unmatched_reason: 'wai_below_floor', 'cluster_unavailable', 
  'queue_actually_empty', 'lost_mode_no_candidate', etc.
- phase_reached (SMALLINT): **WRITE-FROZEN 2026-05-22** per §XVI.C
  amendment. Pre-2026-05-22 rows retain historical values (1-5
  mapping to §XVI Forensic Ladder phases). Post-amendment rows write
  NULL. Derive equivalent forensic state from `matched_offer_id IS
  NOT NULL` (committed), `unmatched_reason IS NOT NULL` (matcher
  consulted), `cluster_lat IS NOT NULL` (cluster diagnostics).
- peak_confidence (REAL), decay_samples (SMALLINT)
- ground_truth_label (TEXT): manual override for replay tests

### app_private.contest_labels
Driver button-press ground truth from the PuddleJumper web app at 
app.puddlejumper.io. Schema is dead simple:
- label_id (BIGSERIAL)
- driver_id, label_time, label, created_at
- CHECK constraint: label IN ('pickup', 'dropoff', 'traffic', 'other')

99 rows total as of 2026-05-22. Use this as ground truth in any "did the 
system recognize the pickup?" question.

### app_private.driver_trip_state_log
Append-only state-machine log. We use this to inject manual markers via 
direct INSERT (e.g., DRIVE_START_MARKER, SENSOR_VALIDATION_MARKER) so 
queries have a clean starting cutoff.
- driver_id, from_state, to_state, trigger_event, logged_at

### app_private.decision_log
Parent table for offer_history (FK relationship). Holds metadata about 
each /decisions POST: driver_id, fare, ping_h3_index, decision_result 
(JSONB), etc. Less directly queried than offer_history.

## Canonical queries (copy-paste ready)

### Q1: Did GPS data flow this drive? (sensor architecture validation)
SELECT 
       date_trunc('minute', created_at AT TIME ZONE 'America/Chicago')::time AS minute,
       COUNT(*) AS heartbeats,
       COUNT(DISTINCT lat) AS distinct_lats,
       COUNT(DISTINCT lng) AS distinct_lngs,
       MIN(speed_mph)::int AS min_speed,
       MAX(speed_mph)::int AS max_speed
FROM app_private.pudo_decision_context
WHERE driver_id = 'UjT1hE9eBXh2q95aSZYOkzDJ8lo1'
  AND created_at >= '<MARKER OR DATE>'
GROUP BY 1 ORDER BY 1;

Expected (healthy): distinct_lats ≈ heartbeats per minute. 10+ heartbeats 
producing 10+ distinct lats while driving means sensor is alive.
Expected (broken): distinct_lats stuck at 1-3 across many minutes = stale 
cache.

### Q2: gps_age_s populated and reasonable?
SELECT COUNT(*) AS total,
       COUNT(gps_age_s) AS with_gps_age,
       MAX(gps_age_s) AS max_age,
       AVG(gps_age_s) AS avg_age
FROM app_private.pudo_decision_context
WHERE driver_id = 'UjT1hE9eBXh2q95aSZYOkzDJ8lo1'
  AND created_at >= '<MARKER>';

### Q3: Cross-reference button presses with system actions
WITH labels AS (
    SELECT label_time, label,
           to_char(label_time AT TIME ZONE 'America/Chicago', 'HH24:MI:SS') AS label_ts
    FROM app_private.contest_labels
    WHERE driver_id = 'UjT1hE9eBXh2q95aSZYOkzDJ8lo1'
      AND label_time >= '<MARKER>'
)
SELECT l.label_ts AS user_tapped, l.label,
       COALESCE((SELECT pdc.planner_action FROM app_private.pudo_decision_context pdc
                 WHERE pdc.driver_id = 'UjT1hE9eBXh2q95aSZYOkzDJ8lo1'
                   AND pdc.created_at BETWEEN l.label_time - INTERVAL '60 seconds'
                                          AND l.label_time + INTERVAL '60 seconds'
                   AND pdc.planner_action IS NOT NULL
                 ORDER BY ABS(EXTRACT(EPOCH FROM (pdc.created_at - l.label_time)))
                 LIMIT 1), '(NONE)') AS system_action,
       COALESCE((SELECT pdc.matched_offer_id::text FROM app_private.pudo_decision_context pdc
                 WHERE pdc.driver_id = 'UjT1hE9eBXh2q95aSZYOkzDJ8lo1'
                   AND pdc.created_at BETWEEN l.label_time - INTERVAL '60 seconds'
                                          AND l.label_time + INTERVAL '60 seconds'
                   AND pdc.planner_action IS NOT NULL
                 ORDER BY ABS(EXTRACT(EPOCH FROM (pdc.created_at - l.label_time)))
                 LIMIT 1), '-') AS matched_offer
FROM labels l ORDER BY l.label_time;

### Q4: All offers in a window
SELECT oh.id, oh.app_verdict,
       oh.pickup_address, oh.pickup_lat IS NULL AS pu_null,
       oh.dropoff_address,
       oh.actual_pickup_at IS NOT NULL AS pickup_fired,
       oh.actual_dropoff_at IS NOT NULL AS dropoff_fired,
       to_char(oh.created_at AT TIME ZONE 'America/Chicago', 'HH24:MI:SS') AS created
FROM app_private.offer_history oh
JOIN app_private.decision_log dl ON dl.id = oh.decision_log_id
WHERE dl.driver_id = 'UjT1hE9eBXh2q95aSZYOkzDJ8lo1'
  AND oh.created_at >= '<MARKER>'
ORDER BY oh.id;

### Q5: TAD verdict blob for one specific PDC row
SELECT to_char(created_at AT TIME ZONE 'America/Chicago', 'HH24:MI:SS') AS t,
       matched_offer_id, planner_action,
       tad_decision_context
FROM app_private.pudo_decision_context
WHERE driver_id = 'UjT1hE9eBXh2q95aSZYOkzDJ8lo1'
  AND created_at = '<EXACT TIMESTAMP>'::timestamptz;

### Q6: Set a manual drive marker (run before a test drive)
INSERT INTO app_private.driver_trip_state_log
  (driver_id, from_state, to_state, trigger_event, logged_at)
VALUES
  ('UjT1hE9eBXh2q95aSZYOkzDJ8lo1', NULL, 'MARKER',
   'manual_test_marker', NOW())
RETURNING logged_at AT TIME ZONE 'UTC' AS marker_set_at;

### Q7: Find the most recent marker timestamp
SELECT logged_at AT TIME ZONE 'UTC' AS marker_utc
FROM app_private.driver_trip_state_log
WHERE driver_id = 'UjT1hE9eBXh2q95aSZYOkzDJ8lo1'
  AND to_state LIKE '%MARKER%'
ORDER BY logged_at DESC LIMIT 1;

### Q8: Cloud Run logs — what endpoints is Android hitting?
gcloud logging read '
  resource.type="cloud_run_revision"
  resource.labels.service_name="puddlejumper-api"
  httpRequest.userAgent="Ktor client"
  timestamp>="<ISO_TIMESTAMP>"
' --limit=200 --format="value(httpRequest.requestUrl)" | sort -u

### Q9: PUDO writes vs server actions correlation (proves who wrote PUDO data)
SELECT oh.id, oh.app_verdict, oh.actual_pickup_at AT TIME ZONE 'America/Chicago' AS pickup_ctx,
       (SELECT pdc.planner_action FROM app_private.pudo_decision_context pdc
        WHERE pdc.matched_offer_id = oh.id::text
          AND pdc.driver_id = 'UjT1hE9eBXh2q95aSZYOkzDJ8lo1'
          AND pdc.planner_action IS NOT NULL
          AND ABS(EXTRACT(EPOCH FROM (pdc.created_at - oh.actual_pickup_at))) < 5
        ORDER BY ABS(EXTRACT(EPOCH FROM (pdc.created_at - oh.actual_pickup_at)))
        LIMIT 1) AS server_action_at_pickup_time
FROM app_private.offer_history oh
JOIN app_private.decision_log dl ON dl.id = oh.decision_log_id
WHERE dl.driver_id = 'UjT1hE9eBXh2q95aSZYOkzDJ8lo1'
  AND oh.actual_pickup_at IS NOT NULL
  AND oh.actual_pickup_at >= '<DATE>'
ORDER BY oh.id DESC LIMIT 30;

## Reading patterns

- For "did sensor work today?" use Q1 + Q2
- For "did system see my pickup?" use Q3
- For "what happened on offer X?" use Q4 + Q5 with the offer's window
- For "are we writing PUDO data correctly?" use Q9
- For "is Android using only /heartbeat endpoint?" use Q8

## Cross-references
[[project-pj-architecture-android-is-sensor]] — principle this schema supports
[[project-pj-sensor-architecture-landed]] — landing record of the architecture
[[project-pj-dispatch-silence-lostmode]] — adjacent server-side open issue