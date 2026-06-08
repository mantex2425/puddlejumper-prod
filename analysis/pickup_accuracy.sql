-- ============================================================================
-- pickup_accuracy.sql — THE mission metric: pickup LOCATION accuracy (<100 m),
-- per drive, with the miss breakdown by lever. (Mission: >95%.)
--
-- Runs off app_private.event_ledger (00653+): ground_truth_tap carries the tap
-- location + the live queue; pickup_detected carries the system's pinned point;
-- offer_left_queue carries band-reap reasons. So every tap is self-classifying —
-- no archaeology, no time-window fuzz. LOCATION is the product, not time.
--
-- Run (auto-detects the latest drive_start_marker for the driver):
--   psql ... -v drv="'<driver_id>'" -f analysis/pickup_accuracy.sql
-- or use analysis/pickup_accuracy.sh (defaults drv to the real driver).
--
-- "Caught" = a pickup_detected within 100 m of your manual tap. Misses split into
-- the three levers: band_reaped (offer killed before you arrived — the parked
-- pickup-band question), never_queued (offer never projected — availability), and
-- in_queue_no_fire (offer was right there, matcher/hot-swap didn't fire).
-- ============================================================================
\set ON_ERROR_STOP on

WITH drive AS (
    SELECT COALESCE(
        (SELECT max(event_time) FROM app_private.event_ledger
         WHERE driver_id = :drv AND event_type = 'drive_start_marker'),
        NOW() - INTERVAL '12 hours'
    ) AS since
),
taps AS (   -- pickup ground-truth taps in this drive (location + live queue)
    SELECT el.id, el.event_time, el.lat, el.lng,
           (el.queue_snapshot->'ids' IS NOT NULL
            AND jsonb_array_length(COALESCE(el.queue_snapshot->'ids', '[]'::jsonb)) > 0) AS had_queue
    FROM app_private.event_ledger el, drive
    WHERE el.driver_id = :drv AND el.event_type = 'ground_truth_tap'
      AND el.payload->>'label' = 'pickup' AND el.event_time >= drive.since
),
fires AS (   -- system pickup detections in this drive (the pinned points)
    SELECT el.lat, el.lng
    FROM app_private.event_ledger el, drive
    WHERE el.driver_id = :drv AND el.event_type = 'pickup_detected'
      AND el.event_time >= drive.since AND el.lat IS NOT NULL
),
reaps AS (   -- pickup-leg band reaps in this drive
    SELECT el.event_time
    FROM app_private.event_ledger el, drive
    WHERE el.driver_id = :drv AND el.event_type = 'offer_left_queue'
      AND el.event_time >= drive.since
      AND el.queue_delta->'left'->0->>'reason' LIKE 'band_overshot:pickup%'
),
scored AS (
    SELECT t.id, t.had_queue,
        (SELECT MIN(2*6371000*asin(sqrt(
                power(sin(radians(f.lat - t.lat)/2), 2)
                + cos(radians(t.lat))*cos(radians(f.lat))
                  *power(sin(radians(f.lng - t.lng)/2), 2))))
         FROM fires f WHERE t.lat IS NOT NULL) AS nearest_fire_m,
        EXISTS (SELECT 1 FROM reaps r
                WHERE r.event_time BETWEEN t.event_time - INTERVAL '30 min' AND t.event_time) AS pickup_reaped
    FROM taps t
)
SELECT
    count(*)                                                              AS pickup_taps,
    count(*) FILTER (WHERE nearest_fire_m <= 100)                         AS caught_100m,
    round(100.0 * count(*) FILTER (WHERE nearest_fire_m <= 100)
          / NULLIF(count(*), 0), 0)                                       AS pct_within_100m,
    round(percentile_cont(0.5) WITHIN GROUP (ORDER BY nearest_fire_m)
          FILTER (WHERE nearest_fire_m <= 100)::numeric, 0)               AS caught_median_m,
    -- miss breakdown by lever (each maps to a known fix):
    count(*) FILTER (WHERE (nearest_fire_m IS NULL OR nearest_fire_m > 100)
                       AND NOT had_queue AND pickup_reaped)               AS miss_band_reaped,
    count(*) FILTER (WHERE (nearest_fire_m IS NULL OR nearest_fire_m > 100)
                       AND NOT had_queue AND NOT pickup_reaped)           AS miss_never_queued,
    count(*) FILTER (WHERE (nearest_fire_m IS NULL OR nearest_fire_m > 100)
                       AND had_queue)                                     AS miss_in_queue_no_fire
FROM scored;

-- Per-tap detail (which tap, caught/missed, distance, lever):
WITH drive AS (
    SELECT COALESCE(
        (SELECT max(event_time) FROM app_private.event_ledger
         WHERE driver_id = :drv AND event_type = 'drive_start_marker'),
        NOW() - INTERVAL '12 hours') AS since
),
taps AS (
    SELECT el.id, el.event_time, el.lat, el.lng,
           (jsonb_array_length(COALESCE(el.queue_snapshot->'ids', '[]'::jsonb)) > 0) AS had_queue
    FROM app_private.event_ledger el, drive
    WHERE el.driver_id = :drv AND el.event_type = 'ground_truth_tap'
      AND el.payload->>'label' = 'pickup' AND el.event_time >= drive.since
),
fires AS (
    SELECT el.lat, el.lng FROM app_private.event_ledger el, drive
    WHERE el.driver_id = :drv AND el.event_type = 'pickup_detected'
      AND el.event_time >= drive.since AND el.lat IS NOT NULL
)
SELECT to_char(t.event_time AT TIME ZONE 'America/Chicago', 'HH24:MI:SS') AS tap_ct,
    round((SELECT MIN(2*6371000*asin(sqrt(power(sin(radians(f.lat-t.lat)/2),2)
            +cos(radians(t.lat))*cos(radians(f.lat))*power(sin(radians(f.lng-t.lng)/2),2))))
           FROM fires f WHERE t.lat IS NOT NULL)::numeric, 0) AS nearest_fire_m,
    t.had_queue
FROM taps t ORDER BY t.event_time;
