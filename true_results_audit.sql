-- ============================================================================
-- TIME-AWARE SIMULATION: OLD vs NEW (with hardcoded old thresholds)
-- ============================================================================

CREATE OR REPLACE FUNCTION app_private.simulate_old_vs_new(
    user_id_in text,
    days_back integer DEFAULT 28
)
RETURNS TABLE(
    scenario text,
    total_offers bigint,
    rides_accepted bigint,
    acceptance_pct numeric,
    total_revenue numeric,
    total_drive_minutes numeric,
    avg_hourly numeric
)
LANGUAGE plpgsql
AS $$
DECLARE
    r RECORD;
    
    -- OLD scenario tracking
    old_busy_until timestamp;
    old_rides bigint := 0;
    old_revenue numeric := 0;
    old_drive_minutes numeric := 0;
    old_hourly_threshold numeric;
    old_mileage_threshold numeric;
    
    -- NEW scenario tracking  
    new_busy_until timestamp;
    new_rides bigint := 0;
    new_revenue numeric := 0;
    new_drive_minutes numeric := 0;
    new_hourly_threshold numeric;
    new_mileage_threshold numeric;
    
    total_offers_count bigint := 0;
    
BEGIN
    old_busy_until := '1970-01-01'::timestamp;
    new_busy_until := '1970-01-01'::timestamp;
    
    FOR r IN 
        SELECT 
            o.created_at,
            o.fare,
            o.trip_minutes,
            o.pickup_minutes,
            o.effective_hourly_rate,
            o.dollars_per_mile,
            o.hour_of_day,
            o.day_of_week
        FROM app_private.offer_history o
        WHERE o.created_at >= NOW() - (days_back || ' days')::interval
          AND o.fare IS NOT NULL
          AND o.trip_minutes IS NOT NULL
          AND o.effective_hourly_rate IS NOT NULL
          AND o.dollars_per_mile IS NOT NULL
        ORDER BY o.created_at ASC
    LOOP
        total_offers_count := total_offers_count + 1;
        
        -- ============================================================
        -- Determine OLD thresholds (pre-optimization)
        -- ============================================================
        -- Weekend Party: Fri/Sat 6pm-1am (days 5,6, hours 18-24,0)
        IF (r.day_of_week IN (5,6) AND (r.hour_of_day >= 18 OR r.hour_of_day < 1)) THEN
            old_hourly_threshold := 20.0;
            old_mileage_threshold := 0.50;
            new_hourly_threshold := 13.0;
            new_mileage_threshold := 0.70;
        -- Late Night Party: Sat/Sun 1am-4am (days 0,6, hours 1-4)
        ELSIF (r.day_of_week IN (0,6) AND r.hour_of_day >= 1 AND r.hour_of_day < 4) THEN
            old_hourly_threshold := 24.0;
            old_mileage_threshold := 0.60;
            new_hourly_threshold := 15.0;
            new_mileage_threshold := 0.80;
        -- Evening Commute / Rush Hour: Mon-Fri 3pm-6pm (days 1-5, hours 15-18)
        ELSIF (r.day_of_week BETWEEN 1 AND 5 AND r.hour_of_day >= 15 AND r.hour_of_day < 18) THEN
            old_hourly_threshold := 14.0;
            old_mileage_threshold := 0.90;
            new_hourly_threshold := 14.0;
            new_mileage_threshold := 0.70;
        -- Morning Commute: Mon-Fri 4am-10am
        ELSIF (r.day_of_week BETWEEN 1 AND 5 AND r.hour_of_day >= 4 AND r.hour_of_day < 10) THEN
            old_hourly_threshold := 20.0;
            old_mileage_threshold := 0.70;
            new_hourly_threshold := 16.0;
            new_mileage_threshold := 0.70;
        -- Midday: Mon-Fri 10am-3pm
        ELSIF (r.day_of_week BETWEEN 1 AND 5 AND r.hour_of_day >= 10 AND r.hour_of_day < 15) THEN
            old_hourly_threshold := 16.0;
            old_mileage_threshold := 0.50;
            new_hourly_threshold := 14.0;
            new_mileage_threshold := 0.70;
        -- Weeknight Wind-down: Mon-Wed 6pm-midnight
        ELSIF (r.day_of_week BETWEEN 1 AND 3 AND r.hour_of_day >= 18 AND r.hour_of_day < 24) THEN
            old_hourly_threshold := 18.0;  -- Guessing old default
            old_mileage_threshold := 0.50;
            new_hourly_threshold := 14.0;
            new_mileage_threshold := 0.70;
        -- Sunday Grind: Sun 4am-midnight
        ELSIF (r.day_of_week = 0 AND r.hour_of_day >= 4) THEN
            old_hourly_threshold := 18.0;  -- Guessing old default
            old_mileage_threshold := 0.50;
            new_hourly_threshold := 14.0;
            new_mileage_threshold := 0.70;
        -- Saturday Morning: Sat 4am-noon
        ELSIF (r.day_of_week = 6 AND r.hour_of_day >= 4 AND r.hour_of_day < 12) THEN
            old_hourly_threshold := 18.0;
            old_mileage_threshold := 0.50;
            new_hourly_threshold := 15.0;
            new_mileage_threshold := 0.70;
        -- Default fallback
        ELSE
            old_hourly_threshold := 18.0;
            old_mileage_threshold := 0.50;
            new_hourly_threshold := 14.0;
            new_mileage_threshold := 0.70;
        END IF;
        
        -- ============================================================
        -- OLD SCENARIO
        -- ============================================================
        IF r.created_at >= old_busy_until THEN
            IF r.effective_hourly_rate >= old_hourly_threshold 
               AND r.dollars_per_mile >= old_mileage_threshold THEN
                old_rides := old_rides + 1;
                old_revenue := old_revenue + r.fare;
                old_drive_minutes := old_drive_minutes + COALESCE(r.trip_minutes, 0) + COALESCE(r.pickup_minutes, 0);
                old_busy_until := r.created_at + ((COALESCE(r.trip_minutes, 0) + COALESCE(r.pickup_minutes, 0)) || ' minutes')::interval;
            END IF;
        END IF;
        
        -- ============================================================
        -- NEW SCENARIO
        -- ============================================================
        IF r.created_at >= new_busy_until THEN
            IF r.effective_hourly_rate >= new_hourly_threshold 
               AND r.dollars_per_mile >= new_mileage_threshold THEN
                new_rides := new_rides + 1;
                new_revenue := new_revenue + r.fare;
                new_drive_minutes := new_drive_minutes + COALESCE(r.trip_minutes, 0) + COALESCE(r.pickup_minutes, 0);
                new_busy_until := r.created_at + ((COALESCE(r.trip_minutes, 0) + COALESCE(r.pickup_minutes, 0)) || ' minutes')::interval;
            END IF;
        END IF;
        
    END LOOP;
    
    RETURN QUERY SELECT 
        'OLD (Pre-Optimization)'::text,
        total_offers_count,
        old_rides,
        ROUND(100.0 * old_rides / NULLIF(total_offers_count, 0), 1),
        ROUND(old_revenue, 2),
        ROUND(old_drive_minutes, 0),
        ROUND(old_revenue / NULLIF(old_drive_minutes / 60.0, 0), 2);
    
    RETURN QUERY SELECT 
        'NEW (AI-Optimized)'::text,
        total_offers_count,
        new_rides,
        ROUND(100.0 * new_rides / NULLIF(total_offers_count, 0), 1),
        ROUND(new_revenue, 2),
        ROUND(new_drive_minutes, 0),
        ROUND(new_revenue / NULLIF(new_drive_minutes / 60.0, 0), 2);
    
END;
$$;
