-- ============================================================
-- PUDDLEJUMPER SHIFT AUDIT
-- ============================================================
-- EDIT ONLY THESE TWO LINES (local CST times):
\set shift_start '2026-01-31 16:00'
\set shift_end '2026-02-01 03:30'
-- ============================================================

-- Create temp table with UTC times
DROP TABLE IF EXISTS _shift;
CREATE TEMP TABLE _shift AS
SELECT 
    :'shift_start'::timestamp + INTERVAL '6 hours' AS start_utc,
    :'shift_end'::timestamp + INTERVAL '6 hours' AS end_utc;

-- Q1: SHIFT SUMMARY
SELECT '=== SHIFT SUMMARY ===' as report;
SELECT 
    COUNT(*) as total_offers,
    COUNT(*) FILTER (WHERE decision_result->>'verdict' = 'ACCEPT') as accepted,
    COUNT(*) FILTER (WHERE decision_result->>'verdict' = 'DECLINE') as declined,
    ROUND(100.0 * COUNT(*) FILTER (WHERE decision_result->>'verdict' = 'ACCEPT') / COUNT(*), 1) as accept_pct,
    ROUND(AVG((decision_result->>'hourlyRate')::numeric) FILTER (WHERE decision_result->>'verdict' = 'ACCEPT'), 2) as avg_hourly_accepts,
    ROUND(AVG((decision_result->>'dollarsPerMile')::numeric) FILTER (WHERE decision_result->>'verdict' = 'ACCEPT'), 2) as avg_dpm_accepts
FROM app_private.decision_log, _shift
WHERE driver_id = 'Dn5j1szxY1XFbJ3HDPYEjT7Jk0Y2'
  AND created_at >= _shift.start_utc
  AND created_at <= _shift.end_utc;

-- Q2: HOURLY BREAKDOWN
SELECT '=== HOURLY BREAKDOWN ===' as report;
SELECT 
    to_char(created_at - INTERVAL '6 hours', 'HH PM') as hour_cst,
    COUNT(*) as offers,
    COUNT(*) FILTER (WHERE decision_result->>'verdict' = 'ACCEPT') as accepts,
    COUNT(*) FILTER (WHERE decision_result->>'verdict' = 'DECLINE') as declines,
    ROUND(AVG((decision_result->>'hourlyRate')::numeric) FILTER (WHERE decision_result->>'verdict' = 'ACCEPT'), 2) as avg_hourly
FROM app_private.decision_log, _shift
WHERE driver_id = 'Dn5j1szxY1XFbJ3HDPYEjT7Jk0Y2'
  AND created_at >= _shift.start_utc
  AND created_at <= _shift.end_utc
GROUP BY to_char(created_at - INTERVAL '6 hours', 'HH PM'), 
         EXTRACT(HOUR FROM created_at - INTERVAL '6 hours')
ORDER BY EXTRACT(HOUR FROM created_at - INTERVAL '6 hours');

-- Q3: DECLINE REASONS
SELECT '=== DECLINE REASONS ===' as report;
SELECT 
    CASE 
        WHEN decision_result->>'reason' ILIKE '%both rates%' THEN 'Both rates too low'
        WHEN decision_result->>'reason' ILIKE '%hourly rate too low%' THEN 'Hourly too low'
        WHEN decision_result->>'reason' ILIKE '%mileage rate too low%' THEN 'Mileage too low'
        WHEN decision_result->>'reason' ILIKE '%red zone%' THEN 'Red Zone'
        WHEN decision_result->>'reason' ILIKE '%inefficient%' THEN 'Inefficient route'
        WHEN decision_result->>'reason' ILIKE '%overshoot%' THEN 'Overshoot kills profit'
        WHEN decision_result->>'reason' ILIKE '%moves away%' THEN 'Wrong direction'
        WHEN decision_result->>'reason' ILIKE '%leaves market%' THEN 'Leaves market'
        ELSE 'Other'
    END as decline_reason,
    COUNT(*) as count,
    ROUND(AVG((decision_result->>'hourlyRate')::numeric), 2) as avg_hourly,
    ROUND(AVG((decision_result->>'dollarsPerMile')::numeric), 2) as avg_dpm
FROM app_private.decision_log, _shift
WHERE driver_id = 'Dn5j1szxY1XFbJ3HDPYEjT7Jk0Y2'
  AND created_at >= _shift.start_utc
  AND created_at <= _shift.end_utc
  AND decision_result->>'verdict' = 'DECLINE'
GROUP BY 1
ORDER BY count DESC;

-- Q4: MODE BREAKDOWN
SELECT '=== MODE BREAKDOWN ===' as report;
SELECT 
    mode_at_decision as mode,
    COUNT(*) as offers,
    COUNT(*) FILTER (WHERE decision_result->>'verdict' = 'ACCEPT') as accepts,
    COUNT(*) FILTER (WHERE decision_result->>'verdict' = 'DECLINE') as declines,
    ROUND(100.0 * COUNT(*) FILTER (WHERE decision_result->>'verdict' = 'ACCEPT') / COUNT(*), 1) as accept_pct,
    ROUND(AVG((decision_result->>'hourlyRate')::numeric) FILTER (WHERE decision_result->>'verdict' = 'ACCEPT'), 2) as avg_hourly
FROM app_private.decision_log, _shift
WHERE driver_id = 'Dn5j1szxY1XFbJ3HDPYEjT7Jk0Y2'
  AND created_at >= _shift.start_utc
  AND created_at <= _shift.end_utc
GROUP BY mode_at_decision
ORDER BY offers DESC;

-- Q5: TOP 5 BEST ACCEPTS
SELECT '=== TOP 5 BEST ACCEPTS ===' as report;
SELECT 
    to_char(created_at - INTERVAL '6 hours', 'HH12:MI AM') as time_cst,
    mode_at_decision as mode,
    ROUND((decision_result->>'hourlyRate')::numeric, 2) as hourly,
    ROUND((decision_result->>'dollarsPerMile')::numeric, 2) as dpm,
    LEFT(decision_result->>'reason', 40) as reason
FROM app_private.decision_log, _shift
WHERE driver_id = 'Dn5j1szxY1XFbJ3HDPYEjT7Jk0Y2'
  AND created_at >= _shift.start_utc
  AND created_at <= _shift.end_utc
  AND decision_result->>'verdict' = 'ACCEPT'
ORDER BY (decision_result->>'hourlyRate')::numeric DESC
LIMIT 5;

-- Q6: TOP 5 HIGH-HOURLY DECLINES (trap detection)
SELECT '=== TOP 5 HIGH-HOURLY DECLINES (traps caught) ===' as report;
SELECT 
    to_char(created_at - INTERVAL '6 hours', 'HH12:MI AM') as time_cst,
    mode_at_decision as mode,
    ROUND((decision_result->>'hourlyRate')::numeric, 2) as hourly,
    ROUND((decision_result->>'dollarsPerMile')::numeric, 2) as dpm,
    LEFT(decision_result->>'reason', 50) as reason
FROM app_private.decision_log, _shift
WHERE driver_id = 'Dn5j1szxY1XFbJ3HDPYEjT7Jk0Y2'
  AND created_at >= _shift.start_utc
  AND created_at <= _shift.end_utc
  AND decision_result->>'verdict' = 'DECLINE'
  AND (decision_result->>'hourlyRate')::numeric > 15
ORDER BY (decision_result->>'hourlyRate')::numeric DESC
LIMIT 5;

-- Q7: FULL SHIFT LOG
SELECT '=== FULL SHIFT LOG ===' as report;
SELECT 
    to_char(created_at - INTERVAL '6 hours', 'HH12:MI AM') as time_cst,
    mode_at_decision as mode,
    decision_result->>'verdict' as verdict,
    ROUND((decision_result->>'hourlyRate')::numeric, 2) as hourly,
    ROUND((decision_result->>'dollarsPerMile')::numeric, 2) as dpm,
    LEFT(decision_result->>'reason', 45) as reason
FROM app_private.decision_log, _shift
WHERE driver_id = 'Dn5j1szxY1XFbJ3HDPYEjT7Jk0Y2'
  AND created_at >= _shift.start_utc
  AND created_at <= _shift.end_utc
ORDER BY created_at;

-- Cleanup
DROP TABLE IF EXISTS _shift;