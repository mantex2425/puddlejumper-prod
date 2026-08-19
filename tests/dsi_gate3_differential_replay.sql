-- GATE 3: differential replay, weighted vs net_hourly vs indexed.
--
-- SAFETY: one transaction. Settings are varied on Andrew's row, results are
-- accumulated into a TEMP table, all analysis runs, then ROLLBACK restores the
-- row. If psql dies mid-run the transaction aborts and rolls back anyway.

\pset pager off
BEGIN;

CREATE TEMP VIEW pop AS
SELECT d.* FROM app_private.decision_log d
JOIN (SELECT fare,trip_miles,trip_minutes,pickup_miles,pickup_minutes
      FROM app_private.decision_log GROUP BY 1,2,3,4,5 HAVING count(*) <= 5) k
  ON k.fare=d.fare AND k.trip_miles=d.trip_miles AND k.trip_minutes=d.trip_minutes
 AND k.pickup_miles=d.pickup_miles AND k.pickup_minutes=d.pickup_minutes
WHERE d.driver_id='UjT1hE9eBXh2q95aSZYOkzDJ8lo1'
  AND d.fare BETWEEN 2 AND 200 AND d.trip_minutes>0 AND d.pickup_minutes>0
  AND d.trip_miles>0.2 AND d.pickup_miles>0.2
  AND d.pickup_lat IS NOT NULL AND d.dropoff_lat IS NOT NULL AND d.dropoff_lng IS NOT NULL;

CREATE TEMP TABLE snap (
  who text, id int, verdict text, dsi numeric, net_hr numeric, mph numeric,
  dsi_w numeric, pu_mi numeric, tr_mi numeric, fare numeric
);

CREATE OR REPLACE FUNCTION pg_temp.capture(tag text) RETURNS void AS $$
INSERT INTO snap
SELECT tag, d.id, r.verdict, r.dsi,
       (r.trace_data->>'netHourlyUsd')::numeric,
       (r.trace_data->>'mph')::numeric,
       (r.trace_data->>'dsiWeighted')::numeric,
       d.pickup_miles, d.trip_miles, d.fare
FROM pop d CROSS JOIN LATERAL app_private.decision_engine_v3(
  d.driver_id, d.pickup_lat::numeric, d.pickup_lng::numeric,
  d.dropoff_lat::numeric, d.dropoff_lng::numeric,
  d.fare, d.trip_miles, d.trip_minutes::numeric,
  d.pickup_minutes::numeric, d.pickup_miles, d.market_id,
  (d.mode_at_decision='TOWARDS'), d.towards_target_lat::numeric,
  d.towards_target_lng::numeric, d.towards_market_id,
  d.current_lat::numeric, d.current_lng::numeric, 3.0::numeric,
  (d.mode_at_decision='PUDDLE_JUMP')) r
WHERE (r.trace_data->>'mph')::numeric BETWEEN 5 AND 80;
$$ LANGUAGE sql;

UPDATE app_private.driver_settings_new SET settings = settings ||
  '{"dsi_formula":"weighted","dsi_threshold":20.0}'::jsonb
 WHERE driver_id='UjT1hE9eBXh2q95aSZYOkzDJ8lo1';
SELECT pg_temp.capture('W');

UPDATE app_private.driver_settings_new SET settings = settings ||
  '{"dsi_formula":"net_hourly","dsi_threshold":5.54}'::jsonb
 WHERE driver_id='UjT1hE9eBXh2q95aSZYOkzDJ8lo1';
SELECT pg_temp.capture('NH');

UPDATE app_private.driver_settings_new SET settings = settings ||
  '{"dsi_formula":"indexed","dsi_threshold":5.54,"dsi_display_scale":2.942}'::jsonb
 WHERE driver_id='UjT1hE9eBXh2q95aSZYOkzDJ8lo1';
SELECT pg_temp.capture('IX');

\echo '=== 3a. CONSISTENCY: indexed vs net_hourly at the same bar (must be 0) ==='
SELECT count(*) AS rows_compared,
       count(*) FILTER (WHERE a.verdict <> b.verdict) AS disagreements
FROM (SELECT * FROM snap WHERE who='NH') a JOIN (SELECT * FROM snap WHERE who='IX') b USING (id);

\echo ''
\echo '=== 3b. MOVERS: indexed vs weighted, by committed-leg speed band ==='
SELECT CASE WHEN a.mph < 20 THEN 'a. <20 mph'
            WHEN a.mph < 30 THEN 'b. 20-30'
            WHEN a.mph < 35 THEN 'c. 30-35'
            WHEN a.mph < 45 THEN 'd. 35-45'
            ELSE 'e. 45+' END AS speed_band,
       count(*) AS n,
       count(*) FILTER (WHERE a.verdict='ACCEPT' AND b.verdict='DECLINE') AS now_declined,
       count(*) FILTER (WHERE a.verdict='DECLINE' AND b.verdict='ACCEPT') AS now_accepted,
       round(100.0*count(*) FILTER (WHERE a.verdict<>b.verdict)/count(*),1) AS pct_moved
FROM (SELECT * FROM snap WHERE who='W') a JOIN (SELECT * FROM snap WHERE who='IX') b USING (id)
GROUP BY 1 ORDER BY 1;

\echo ''
\echo '=== 3c. OFFER-6 CLASS: pickup>trip AND mph>35 AND net<bar -- ALL must DECLINE ==='
SELECT count(*) AS class_size,
       count(*) FILTER (WHERE b.verdict='DECLINE') AS declined_by_indexed,
       count(*) FILTER (WHERE a.verdict='ACCEPT')  AS was_accepted_by_weighted
FROM (SELECT * FROM snap WHERE who='W') a JOIN (SELECT * FROM snap WHERE who='IX') b USING (id)
WHERE b.pu_mi > b.tr_mi AND b.mph > 35 AND b.net_hr < 5.54;

\echo ''
\echo '=== 3d. EXAMPLES (old weighted -> new indexed) ==='
SELECT b.fare, b.pu_mi, b.tr_mi, round(b.mph,1) AS mph,
       round(a.dsi_w,1) AS old_dsi, a.verdict AS old_v,
       round(b.net_hr,2) AS net_hr, round(b.dsi,1) AS new_idx, b.verdict AS new_v
FROM (SELECT * FROM snap WHERE who='W') a JOIN (SELECT * FROM snap WHERE who='IX') b USING (id)
WHERE b.pu_mi > b.tr_mi AND b.mph > 35 AND b.net_hr < 5.54
ORDER BY a.dsi_w DESC LIMIT 8;

ROLLBACK;

\echo ''
\echo '=== POST-ROLLBACK: live settings restored ==='
SELECT COALESCE(settings->>'dsi_formula','weighted') AS formula,
       COALESCE(settings->>'dsi_threshold','(unset)') AS threshold,
       COALESCE(settings->>'dsi_display_scale','(unset)') AS display_scale
FROM app_private.driver_settings_new WHERE driver_id='UjT1hE9eBXh2q95aSZYOkzDJ8lo1';
