DROP FUNCTION IF EXISTS app_private.optimize_market_settings(text,text,integer,text,text);

CREATE OR REPLACE FUNCTION app_private.optimize_market_settings(
    driver_id_in text,            -- Used ONLY to get YOUR timezone/shifts/market hexes
    market_id_in text DEFAULT NULL,
    days_back_in integer DEFAULT 28,
    mode_filter text DEFAULT NULL,
    shift_id_in text DEFAULT NULL
)
RETURNS TABLE(
    test_hourly numeric,
    test_mileage numeric,
    test_efficiency numeric,
    would_accept integer,
    total_offers integer,
    accept_pct numeric,
    total_revenue numeric,
    avg_hourly numeric,
    avg_dpm numeric,
    total_progress numeric,
    avg_progress_per_ride numeric,
    mode_analyzed text,
    market_analyzed text,
    time_period_analyzed text
)
LANGUAGE plpgsql
STABLE
AS $function$
DECLARE
    v_timezone text;
    v_settings jsonb;
    v_market_hexes text[];
    v_shift_obj jsonb := NULL;
    v_shift_days int[];
    v_shift_start int;
    v_shift_end int;
    v_shift_active boolean := false;
BEGIN
    -- 1. Load YOUR Settings (timezone, shifts, market hexes)
    SELECT settings, COALESCE(settings->>'timezone', 'America/Chicago')
    INTO v_settings, v_timezone
    FROM app_private.driver_settings_new
    WHERE driver_id = driver_id_in;

    -- 2. Get YOUR market's hex list (the geographic filter)
    IF market_id_in IS NOT NULL THEN
        SELECT array_agg(hex)
        INTO v_market_hexes
        FROM (
            SELECT jsonb_array_elements_text(elem->'greenZones') as hex
            FROM jsonb_array_elements(v_settings->'markets') elem
            WHERE elem->>'id' = market_id_in
        ) subq;
    END IF;

    -- 3. Load YOUR shift definition (the time filter)
    IF shift_id_in IS NOT NULL AND v_settings->'shifts' IS NOT NULL THEN
        SELECT elem INTO v_shift_obj
        FROM jsonb_array_elements(v_settings->'shifts') elem
        WHERE elem->>'id' = shift_id_in;

        IF v_shift_obj IS NOT NULL THEN
            v_shift_active := true;
            v_shift_start := (v_shift_obj->>'startHour')::int;
            v_shift_end := (v_shift_obj->>'endHour')::int;
            SELECT array_agg(d::int) INTO v_shift_days
            FROM jsonb_array_elements_text(v_shift_obj->'days') d;
        END IF;
    END IF;

    -- =======================================================================
    -- PART A: PUDDLE_JUMP / FREESTYLE (Community Data)
    -- =======================================================================
    IF mode_filter IS NULL OR mode_filter IN ('PUDDLE_JUMP', 'FREESTYLE') THEN
        RETURN QUERY
WITH community_offers AS (
            -- Pull from ALL drivers via offer_history (pre-computed timezone-aware DOW/hour)
            SELECT 
                effective_hourly_rate as hourly_rate,
                dollars_per_mile as dpm,
                fare,
                day_of_week,
                hour_of_day
            FROM app_private.offer_history
            WHERE 
                -- NO driver_id filter — this is COMMUNITY data
                created_at >= NOW() - (days_back_in || ' days')::interval
                AND (mode_filter IS NULL OR mode_at_decision = mode_filter)
                AND mode_at_decision IN ('PUDDLE_JUMP', 'FREESTYLE')
                -- Geographic filter: YOUR market's hexes
                AND (v_market_hexes IS NULL OR driver_h3 = ANY(v_market_hexes))
                -- The Bouncer: sanity checks
                AND effective_hourly_rate > 5
                AND effective_hourly_rate < 150
                AND is_validated = true
        ),
        offers AS (
            SELECT *
            FROM community_offers
            WHERE 
                -- Time filter: use pre-stored timezone-aware columns
                (NOT v_shift_active OR (
                    day_of_week = ANY(v_shift_days)
                    AND (
                        (v_shift_start <= v_shift_end 
                         AND hour_of_day >= v_shift_start 
                         AND hour_of_day < v_shift_end)
                        OR
                        (v_shift_start > v_shift_end 
                         AND (hour_of_day >= v_shift_start 
                              OR hour_of_day < v_shift_end))
                    )
                ))
        ),
        hourly_vals AS (SELECT unnest(ARRAY[12,13,14,15,16,17,18,19,20,21,22,23,24,25,26,27,28,29,30,35,40,50]::numeric[]) as val),
        mileage_vals AS (SELECT unnest(ARRAY[0.5,0.6,0.7,0.8,0.9,1.0,1.1,1.2,1.3,1.4,1.5,2.0]::numeric[]) as val),
        
        matrix AS (
            SELECT 
                h.val as t_hourly,
                m.val as t_mileage,
                COUNT(*) FILTER (WHERE o.hourly_rate >= h.val AND o.dpm >= m.val)::integer as accepted_count,
                COUNT(*)::integer as total_count,
                ROUND(SUM(o.fare) FILTER (WHERE o.hourly_rate >= h.val AND o.dpm >= m.val), 2) as t_revenue,
                ROUND(AVG(o.hourly_rate) FILTER (WHERE o.hourly_rate >= h.val AND o.dpm >= m.val), 2) as t_avg_hourly,
                ROUND(AVG(o.dpm) FILTER (WHERE o.hourly_rate >= h.val AND o.dpm >= m.val), 2) as t_avg_dpm
            FROM hourly_vals h
            CROSS JOIN mileage_vals m
            CROSS JOIN offers o
            GROUP BY h.val, m.val
        )
        -- Pareto optimization: best hourly for each acceptance level
        SELECT DISTINCT ON (accepted_count)
            t_hourly,
            t_mileage,
            NULL::numeric,
            accepted_count,
            total_count,
            ROUND(100.0 * accepted_count / NULLIF(total_count, 0), 1),
            t_revenue,
            t_avg_hourly,
            t_avg_dpm,
            NULL::numeric,
            NULL::numeric,
            COALESCE(mode_filter, 'PUDDLE_JUMP+FREESTYLE')::text,
            COALESCE((SELECT DISTINCT market_name FROM app_private.decision_log WHERE market_id = market_id_in LIMIT 1), 'COMMUNITY')::text,
            COALESCE(shift_id_in, 'ALL')::text
        FROM matrix
        WHERE accepted_count > 0
        ORDER BY accepted_count DESC, t_hourly DESC, t_mileage DESC;
    END IF;

    -- =======================================================================
    -- PART B: TOWARDS (Community Data)
    -- =======================================================================
    IF mode_filter IS NULL OR mode_filter = 'TOWARDS' THEN
        RETURN QUERY
        WITH community_towards AS (
            SELECT 
                (decision_result->>'hourlyRate')::numeric as hourly_rate,
                (decision_result->>'dollarsPerMile')::numeric as dpm,
                fare,
                created_at AT TIME ZONE v_timezone as local_ts,
                COALESCE(
                    (decision_result->'trace_data'->>'netProgress')::numeric,
                    (decision_result->>'netProgress')::numeric,
                    CASE
                        WHEN decision_result->>'reason' ~ '(\d+\.?\d*) mi net progress' 
                        THEN (regexp_match(decision_result->>'reason', '(\d+\.?\d*) mi net progress'))[1]::numeric
                        WHEN decision_result->>'reason' ~ 'Arrived in target \((\d+\.?\d*) mi'
                        THEN (regexp_match(decision_result->>'reason', 'Arrived in target \((\d+\.?\d*) mi'))[1]::numeric
                        WHEN decision_result->>'reason' ~ 'Only (\d+\.?\d*) mi gained'
                        THEN (regexp_match(decision_result->>'reason', 'Only (\d+\.?\d*) mi gained'))[1]::numeric
                        WHEN decision_result->>'reason' LIKE '%moves away%' THEN 0
                        ELSE NULL
                    END
                ) as net_progress,
                COALESCE(
                    (decision_result->'trace_data'->>'efficiencyPct')::numeric,
                    (decision_result->>'efficiencyPct')::numeric,
                    CASE
                        WHEN decision_result->>'reason' ~ 'Inefficient route: (\d+)%'
                        THEN (regexp_match(decision_result->>'reason', 'Inefficient route: (\d+)%'))[1]::numeric / 100.0
                        WHEN decision_result->>'verdict' = 'ACCEPT' AND decision_result->>'reason' LIKE '%progress%' THEN 0.50
                        WHEN decision_result->>'reason' LIKE 'Arrived in target%' THEN 1.0
                        WHEN decision_result->>'reason' LIKE '%moves away%' THEN 0.0
                        ELSE NULL
                    END
                ) as efficiency
            FROM app_private.decision_log
            WHERE 
                created_at >= NOW() - (days_back_in || ' days')::interval
                AND mode_at_decision = 'TOWARDS'
                AND (decision_result->>'hourlyRate')::numeric > 5
                AND (decision_result->>'hourlyRate')::numeric < 150
        ),
        towards_offers AS (
            SELECT * FROM community_towards
            WHERE net_progress IS NOT NULL
              AND (NOT v_shift_active OR (
                    EXTRACT(DOW FROM local_ts)::int = ANY(v_shift_days)
                    AND (
                        (v_shift_start <= v_shift_end 
                         AND EXTRACT(HOUR FROM local_ts) >= v_shift_start 
                         AND EXTRACT(HOUR FROM local_ts) < v_shift_end)
                        OR
                        (v_shift_start > v_shift_end 
                         AND (EXTRACT(HOUR FROM local_ts) >= v_shift_start 
                              OR EXTRACT(HOUR FROM local_ts) < v_shift_end))
                    )
                ))
        ),
        efficiency_vals AS (SELECT unnest(ARRAY[0.0, 0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80]::numeric[]) as val)
        SELECT 
            NULL::numeric,
            NULL::numeric,
            e.val,
            COUNT(*) FILTER (WHERE o.efficiency >= e.val AND o.net_progress > 0)::integer,
            COUNT(*)::integer,
            ROUND(100.0 * COUNT(*) FILTER (WHERE o.efficiency >= e.val AND o.net_progress > 0) / NULLIF(COUNT(*), 0), 1),
            ROUND(SUM(o.fare) FILTER (WHERE o.efficiency >= e.val AND o.net_progress > 0), 2),
            ROUND(AVG(o.hourly_rate) FILTER (WHERE o.efficiency >= e.val AND o.net_progress > 0), 2),
            ROUND(AVG(o.dpm) FILTER (WHERE o.efficiency >= e.val AND o.net_progress > 0), 2),
            ROUND(SUM(o.net_progress) FILTER (WHERE o.efficiency >= e.val AND o.net_progress > 0), 1),
            ROUND(AVG(o.net_progress) FILTER (WHERE o.efficiency >= e.val AND o.net_progress > 0), 1),
            'TOWARDS'::text,
            'COMMUNITY'::text,
            COALESCE(shift_id_in, 'ALL')::text
        FROM efficiency_vals e
        CROSS JOIN towards_offers o
        GROUP BY e.val
        HAVING COUNT(*) > 0
        ORDER BY e.val;
    END IF;
END;
$function$;