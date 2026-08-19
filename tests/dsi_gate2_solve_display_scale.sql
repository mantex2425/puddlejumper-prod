-- GATE 2: solve DISPLAY_SCALE and report threshold equivalences.
--
-- POPULATION (reproducible filter, documented per work order 3):
--   driver UjT1hE9eBXh2q95aSZYOkzDJ8lo1 only
--   EXCLUDE fixture rows: any (fare,trip_miles,trip_minutes,pickup_miles,
--     pickup_minutes) tuple occurring >5 times in decision_log. That removes
--     the ~2,986x $18.50/8.2mi-22min/1.1mi-3min synthetic offer and 5 others.
--   fare 2..200, trip_minutes>0, pickup_minutes>0, trip_miles>0.2,
--     pickup_miles>0.2, pickup/dropoff coords present
--   committed-leg mph between 5 and 80
--
-- Both dsiWeighted and netHourlyUsd are persisted on every decision now, so a
-- single weighted-mode pass yields everything.

\pset pager off

CREATE TEMP VIEW c AS
SELECT d.id, d.fare, r.verdict,
       (r.trace_data->>'dsiWeighted')::numeric  AS dsi_weighted,
       (r.trace_data->>'netHourlyUsd')::numeric AS net_hr,
       (r.trace_data->>'mph')::numeric          AS mph
FROM app_private.decision_log d
JOIN (SELECT fare,trip_miles,trip_minutes,pickup_miles,pickup_minutes
      FROM app_private.decision_log
      GROUP BY 1,2,3,4,5 HAVING count(*) <= 5) k
  ON k.fare=d.fare AND k.trip_miles=d.trip_miles AND k.trip_minutes=d.trip_minutes
 AND k.pickup_miles=d.pickup_miles AND k.pickup_minutes=d.pickup_minutes
CROSS JOIN LATERAL app_private.decision_engine_v3(
  d.driver_id, d.pickup_lat::numeric, d.pickup_lng::numeric,
  d.dropoff_lat::numeric, d.dropoff_lng::numeric,
  d.fare, d.trip_miles, d.trip_minutes::numeric,
  d.pickup_minutes::numeric, d.pickup_miles, d.market_id,
  (d.mode_at_decision='TOWARDS'), d.towards_target_lat::numeric,
  d.towards_target_lng::numeric, d.towards_market_id,
  d.current_lat::numeric, d.current_lng::numeric, 3.0::numeric,
  (d.mode_at_decision='PUDDLE_JUMP')) r
WHERE d.driver_id='UjT1hE9eBXh2q95aSZYOkzDJ8lo1'
  AND d.fare BETWEEN 2 AND 200 AND d.trip_minutes>0 AND d.pickup_minutes>0
  AND d.trip_miles>0.2 AND d.pickup_miles>0.2
  AND d.pickup_lat IS NOT NULL AND d.dropoff_lat IS NOT NULL AND d.dropoff_lng IS NOT NULL
  AND (r.trace_data->>'netHourlyUsd') IS NOT NULL
  AND (r.trace_data->>'mph')::numeric BETWEEN 5 AND 80;

\echo '=== POPULATION ==='
SELECT count(*) AS genuine_offers,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY mph)::numeric,1) AS median_mph,
       round(100.0*count(*) FILTER (WHERE verdict='ACCEPT')/count(*),1) AS current_accept_pct
FROM c;

\echo ''
\echo '=== SOLVED DISPLAY_SCALE (median index reproduces median weighted) ==='
SELECT round(percentile_cont(0.5) WITHIN GROUP (ORDER BY dsi_weighted)::numeric,2) AS median_weighted,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY net_hr)::numeric,2)       AS median_net_hourly,
       round((percentile_cont(0.5) WITHIN GROUP (ORDER BY dsi_weighted)
              / NULLIF(percentile_cont(0.5) WITHIN GROUP (ORDER BY net_hr),0))::numeric, 3) AS solved_display_scale
FROM c;

\echo ''
\echo '=== DISTRIBUTION: weighted vs indexed (at solved scale) ==='
WITH s AS (SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY dsi_weighted)
                  / NULLIF(percentile_cont(0.5) WITHIN GROUP (ORDER BY net_hr),0) AS k FROM c)
SELECT 'weighted' AS metric,
       round(percentile_cont(0.25) WITHIN GROUP (ORDER BY dsi_weighted)::numeric,1) AS p25,
       round(percentile_cont(0.50) WITHIN GROUP (ORDER BY dsi_weighted)::numeric,1) AS median,
       round(percentile_cont(0.75) WITHIN GROUP (ORDER BY dsi_weighted)::numeric,1) AS p75 FROM c
UNION ALL
SELECT 'indexed',
       round((percentile_cont(0.25) WITHIN GROUP (ORDER BY net_hr)*(SELECT k FROM s))::numeric,1),
       round((percentile_cont(0.50) WITHIN GROUP (ORDER BY net_hr)*(SELECT k FROM s))::numeric,1),
       round((percentile_cont(0.75) WITHIN GROUP (ORDER BY net_hr)*(SELECT k FROM s))::numeric,1) FROM c
UNION ALL
SELECT 'net_hourly_usd',
       round(percentile_cont(0.25) WITHIN GROUP (ORDER BY net_hr)::numeric,2),
       round(percentile_cont(0.50) WITHIN GROUP (ORDER BY net_hr)::numeric,2),
       round(percentile_cont(0.75) WITHIN GROUP (ORDER BY net_hr)::numeric,2) FROM c;

\echo ''
\echo '=== THRESHOLD LADDER: $/hr -> accept rate -> index equivalent ==='
WITH s AS (SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY dsi_weighted)
                  / NULLIF(percentile_cont(0.5) WITHIN GROUP (ORDER BY net_hr),0) AS k FROM c),
     base AS (SELECT round(100.0*count(*) FILTER (WHERE verdict='ACCEPT')/count(*),1) AS cur FROM c)
SELECT t.thr AS threshold_usd_per_hr,
       round(100.0*count(*) FILTER (WHERE c.net_hr >= t.thr)/count(*),1) AS accept_pct,
       round((t.thr*(SELECT k FROM s))::numeric,1) AS same_bar_in_index_units,
       (SELECT cur FROM base) AS current_accept_pct
FROM c CROSS JOIN (VALUES (3),(4),(5),(6),(7),(8),(10),(12),(15)) t(thr)
GROUP BY t.thr ORDER BY t.thr;
