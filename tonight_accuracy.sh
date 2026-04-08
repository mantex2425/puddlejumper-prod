#!/bin/bash
# Run after a drive session to see Auto Nail It accuracy
# Usage: bash tonight_accuracy.sh

psql -h 10.128.0.2 -U postgres -d puddlejumper << 'SQL'
SELECT
    'Pickup' AS nail_type,
    COUNT(*) AS total,
    COUNT(*) FILTER (WHERE triangulation_error_m <= 400) AS bullseyes,
    COUNT(*) FILTER (WHERE triangulation_error_m <= 800) AS on_target,
    COUNT(*) FILTER (WHERE triangulation_error_m > 800) AS misses,
    ROUND(AVG(triangulation_error_m)::numeric, 0) AS avg_error_m,
    ROUND(100.0 * COUNT(*) FILTER (WHERE triangulation_error_m <= 800)
        / NULLIF(COUNT(*), 0), 1) AS pct_on_target
FROM app_private.pickup_market_signals pms
JOIN app_private.decision_log dl ON dl.id = pms.offer_id
WHERE dl.driver_id = 'UjT1hE9eBXh2q95aSZYOkzDJ8lo1'
  AND pms.actual_pickup_at > NOW() - INTERVAL '12 hours'

UNION ALL

SELECT
    'Dropoff' AS nail_type,
    COUNT(*) AS total,
    COUNT(*) FILTER (WHERE dropoff_error_m <= 400) AS bullseyes,
    COUNT(*) FILTER (WHERE dropoff_error_m <= 800) AS on_target,
    COUNT(*) FILTER (WHERE dropoff_error_m > 800) AS misses,
    ROUND(AVG(dropoff_error_m)::numeric, 0) AS avg_error_m,
    ROUND(100.0 * COUNT(*) FILTER (WHERE dropoff_error_m <= 800)
        / NULLIF(COUNT(*), 0), 1) AS pct_on_target
FROM app_private.driver_trip_state
WHERE driver_id = 'UjT1hE9eBXh2q95aSZYOkzDJ8lo1'
  AND actual_dropoff_at > NOW() - INTERVAL '12 hours';
SQL
