-- GATE 1: prove DISPLAY_SCALE cannot change a verdict.
--
-- Each block sets a different display scale INSIDE a transaction, hashes the
-- ordered verdict vector over the clean corpus, then ROLLS BACK. The three
-- hashes must be identical. Nothing persists.

\set QUIET on
\pset pager off

CREATE TEMP VIEW real_offers AS
SELECT d.* FROM app_private.decision_log d
JOIN (SELECT fare,trip_miles,trip_minutes,pickup_miles,pickup_minutes
      FROM app_private.decision_log
      GROUP BY 1,2,3,4,5 HAVING count(*) <= 5) k
  ON k.fare=d.fare AND k.trip_miles=d.trip_miles AND k.trip_minutes=d.trip_minutes
 AND k.pickup_miles=d.pickup_miles AND k.pickup_minutes=d.pickup_minutes
WHERE d.driver_id = 'UjT1hE9eBXh2q95aSZYOkzDJ8lo1'
  AND d.fare BETWEEN 2 AND 200 AND d.trip_minutes>0 AND d.pickup_minutes>0
  AND d.trip_miles>0.2 AND d.pickup_miles>0.2
  AND d.pickup_lat IS NOT NULL AND d.dropoff_lat IS NOT NULL AND d.dropoff_lng IS NOT NULL;

CREATE OR REPLACE FUNCTION pg_temp.verdict_hash() RETURNS TABLE(n bigint, h text, sample_index numeric) AS $$
  SELECT count(*), md5(string_agg(r.verdict, ',' ORDER BY d.id)), round(max(r.dsi),2)
  FROM real_offers d CROSS JOIN LATERAL app_private.decision_engine_v3(
    d.driver_id, d.pickup_lat::numeric, d.pickup_lng::numeric,
    d.dropoff_lat::numeric, d.dropoff_lng::numeric,
    d.fare, d.trip_miles, d.trip_minutes::numeric,
    d.pickup_minutes::numeric, d.pickup_miles, d.market_id,
    (d.mode_at_decision='TOWARDS'), d.towards_target_lat::numeric,
    d.towards_target_lng::numeric, d.towards_market_id,
    d.current_lat::numeric, d.current_lng::numeric, 3.0::numeric,
    (d.mode_at_decision='PUDDLE_JUMP')) r;
$$ LANGUAGE sql;

\set QUIET off
\echo '=== scale x1 (2.9) ==='
BEGIN;
UPDATE app_private.driver_settings_new
   SET settings = settings || '{"dsi_formula":"indexed","dsi_display_scale":2.9,"dsi_threshold":7}'::jsonb
 WHERE driver_id='UjT1hE9eBXh2q95aSZYOkzDJ8lo1';
SELECT * FROM pg_temp.verdict_hash();
ROLLBACK;

\echo '=== scale x2 (5.8) ==='
BEGIN;
UPDATE app_private.driver_settings_new
   SET settings = settings || '{"dsi_formula":"indexed","dsi_display_scale":5.8,"dsi_threshold":7}'::jsonb
 WHERE driver_id='UjT1hE9eBXh2q95aSZYOkzDJ8lo1';
SELECT * FROM pg_temp.verdict_hash();
ROLLBACK;

\echo '=== scale x0.5 (1.45) ==='
BEGIN;
UPDATE app_private.driver_settings_new
   SET settings = settings || '{"dsi_formula":"indexed","dsi_display_scale":1.45,"dsi_threshold":7}'::jsonb
 WHERE driver_id='UjT1hE9eBXh2q95aSZYOkzDJ8lo1';
SELECT * FROM pg_temp.verdict_hash();
ROLLBACK;

\echo '=== live settings unchanged after rollbacks ==='
SELECT COALESCE(settings->>'dsi_formula','weighted') AS formula,
       COALESCE(settings->>'dsi_display_scale','(unset)') AS display_scale
FROM app_private.driver_settings_new WHERE driver_id='UjT1hE9eBXh2q95aSZYOkzDJ8lo1';
