-- ============================================================
-- HEX PRICING CACHE: Pre-computed Pareto-optimized thresholds
-- for Freestyle mode location-aware decision making
-- ============================================================
-- Run order:
--   1. Create table
--   2. Create batch function
--   3. Run initial population: SELECT app_private.refresh_hex_pricing_cache();
--   4. Verify: SELECT * FROM app_private.hex_pricing_cache LIMIT 20;
--   5. Schedule weekly via pg_cron or Cloud Scheduler
-- ============================================================

-- ============================================================
-- STEP 1: Cache Table
-- ============================================================
-- Stores 3 strategy options per hex/day/hour:
--   Revenue = accept more rides, maximize total earnings
--   AI      = balanced (best revenue × hourly product)  
--   Picky   = highest rates, very selective
-- ============================================================


CREATE TABLE IF NOT EXISTS app_private.hex_pricing_cache (
    h3_index    text    NOT NULL,
    day_of_week integer NOT NULL,   -- 0=Sun, 6=Sat
    hour_of_day integer NOT NULL,   -- 0-23

    -- Strategy 1: Revenue / Accept More (lowest thresholds)
    revenue_hourly  numeric(10,2),
    revenue_mileage numeric(10,2),

    -- Strategy 2: AI / Balanced (middle ground)
    ai_hourly       numeric(10,2),
    ai_mileage      numeric(10,2),

    -- Strategy 3: Picky / Sniper (highest thresholds)
    picky_hourly    numeric(10,2),
    picky_mileage   numeric(10,2),

    -- Metadata
    sample_count integer,
    data_source  text,              -- 'exact_hex', 'cluster_ring1', 'cluster_ring2', 'metro_fallback'
    updated_at   timestamptz DEFAULT now(),

    PRIMARY KEY (h3_index, day_of_week, hour_of_day)
);

-- Fast lookup index for real-time decisions
CREATE INDEX IF NOT EXISTS idx_hex_pricing_lookup 
ON app_private.hex_pricing_cache (h3_index, day_of_week, hour_of_day);

-- ============================================================
-- STEP 2: Batch Generator Function
-- ============================================================
-- Processes every hex that has offer data.
-- Cascade: exact hex → ring-1 cluster → ring-2 cluster → metro fallback
-- Uses percentiles as strategy proxies:
--   Revenue = p25 (accept 75% of rides)
--   AI      = p50 (accept 50% of rides)  
--   Picky   = p75 (accept only top 25%)
-- Dignity floor: $0.70/mi minimum on all strategies
-- ============================================================

CREATE OR REPLACE FUNCTION app_private.refresh_hex_pricing_cache(
    min_samples_exact integer DEFAULT 10,
    min_samples_cluster integer DEFAULT 10,
    lookback_days integer DEFAULT 365
)
RETURNS TABLE(
    hexes_processed integer,
    exact_count integer,
    ring1_count integer,
    ring2_count integer,
    fallback_count integer,
    duration_ms numeric
)
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
    v_start         timestamptz := clock_timestamp();
    v_cutoff        timestamptz := now() - (lookback_days || ' days')::interval;
    v_hex           record;
    v_stats         record;
    v_source        text;
    v_total         integer := 0;
    v_exact         integer := 0;
    v_ring1         integer := 0;
    v_ring2         integer := 0;
    v_fallback      integer := 0;
    v_dignity_mileage numeric := 0.70;  -- IRS mileage rate floor
BEGIN

    -- Clear old cache
    TRUNCATE TABLE app_private.hex_pricing_cache;

    -- Loop through every distinct driver_h3 + day + hour combination
    -- Using driver_h3 because Freestyle cares about WHERE THE DRIVER IS
    FOR v_hex IN
        SELECT DISTINCT
            driver_h3,
            day_of_week,
            hour_of_day
        FROM app_private.offer_history
        WHERE driver_h3 IS NOT NULL
          AND is_validated = true
          AND created_at >= v_cutoff
    LOOP
        v_source := NULL;

        -- ========================================
        -- CASCADE LEVEL 1: Exact hex match
        -- ========================================
        SELECT
            count(*)                                                          as samples,
            percentile_cont(0.25) WITHIN GROUP (ORDER BY effective_hourly_rate) as p25_hr,
            percentile_cont(0.25) WITHIN GROUP (ORDER BY dollars_per_mile)      as p25_mi,
            percentile_cont(0.50) WITHIN GROUP (ORDER BY effective_hourly_rate) as p50_hr,
            percentile_cont(0.50) WITHIN GROUP (ORDER BY dollars_per_mile)      as p50_mi,
            percentile_cont(0.75) WITHIN GROUP (ORDER BY effective_hourly_rate) as p75_hr,
            percentile_cont(0.75) WITHIN GROUP (ORDER BY dollars_per_mile)      as p75_mi
        INTO v_stats
        FROM app_private.offer_history
        WHERE driver_h3 = v_hex.driver_h3
          AND day_of_week = v_hex.day_of_week
          AND hour_of_day = v_hex.hour_of_day
          AND is_validated = true
          AND effective_hourly_rate > 0
          AND effective_hourly_rate < 150  -- filter OCR garbage
          AND dollars_per_mile > 0
          AND dollars_per_mile < 10        -- filter OCR garbage
          AND created_at >= v_cutoff;

        IF v_stats.samples >= min_samples_exact THEN
            v_source := 'exact_hex';
            v_exact := v_exact + 1;
        END IF;

        -- ========================================
        -- CASCADE LEVEL 2: Ring-1 neighbors (7 hexes)
        -- ========================================
        IF v_source IS NULL THEN
            SELECT
                count(*)                                                          as samples,
                percentile_cont(0.25) WITHIN GROUP (ORDER BY effective_hourly_rate) as p25_hr,
                percentile_cont(0.25) WITHIN GROUP (ORDER BY dollars_per_mile)      as p25_mi,
                percentile_cont(0.50) WITHIN GROUP (ORDER BY effective_hourly_rate) as p50_hr,
                percentile_cont(0.50) WITHIN GROUP (ORDER BY dollars_per_mile)      as p50_mi,
                percentile_cont(0.75) WITHIN GROUP (ORDER BY effective_hourly_rate) as p75_hr,
                percentile_cont(0.75) WITHIN GROUP (ORDER BY dollars_per_mile)      as p75_mi
            INTO v_stats
            FROM app_private.offer_history
            WHERE driver_h3 IN (
                SELECT h3_grid_disk(v_hex.driver_h3::h3index, 1)::text
            )
              AND day_of_week = v_hex.day_of_week
              AND hour_of_day = v_hex.hour_of_day
              AND is_validated = true
              AND effective_hourly_rate > 0
              AND effective_hourly_rate < 150
              AND dollars_per_mile > 0
              AND dollars_per_mile < 10
              AND created_at >= v_cutoff;

            IF v_stats.samples >= min_samples_cluster THEN
                v_source := 'cluster_ring1';
                v_ring1 := v_ring1 + 1;
            END IF;
        END IF;

        -- ========================================
        -- CASCADE LEVEL 3: Ring-2 neighbors (19 hexes)
        -- ========================================
        IF v_source IS NULL THEN
            SELECT
                count(*)                                                          as samples,
                percentile_cont(0.25) WITHIN GROUP (ORDER BY effective_hourly_rate) as p25_hr,
                percentile_cont(0.25) WITHIN GROUP (ORDER BY dollars_per_mile)      as p25_mi,
                percentile_cont(0.50) WITHIN GROUP (ORDER BY effective_hourly_rate) as p50_hr,
                percentile_cont(0.50) WITHIN GROUP (ORDER BY dollars_per_mile)      as p50_mi,
                percentile_cont(0.75) WITHIN GROUP (ORDER BY effective_hourly_rate) as p75_hr,
                percentile_cont(0.75) WITHIN GROUP (ORDER BY dollars_per_mile)      as p75_mi
            INTO v_stats
            FROM app_private.offer_history
            WHERE driver_h3 IN (
                SELECT h3_grid_disk(v_hex.driver_h3::h3index, 2)::text
            )
              AND day_of_week = v_hex.day_of_week
              AND hour_of_day = v_hex.hour_of_day
              AND is_validated = true
              AND effective_hourly_rate > 0
              AND effective_hourly_rate < 150
              AND dollars_per_mile > 0
              AND dollars_per_mile < 10
              AND created_at >= v_cutoff;

            IF v_stats.samples >= min_samples_cluster THEN
                v_source := 'cluster_ring2';
                v_ring2 := v_ring2 + 1;
            END IF;
        END IF;

        -- ========================================
        -- CASCADE LEVEL 4: Metro-wide fallback (all data for this day/hour)
        -- ========================================
        IF v_source IS NULL THEN
            SELECT
                count(*)                                                          as samples,
                percentile_cont(0.25) WITHIN GROUP (ORDER BY effective_hourly_rate) as p25_hr,
                percentile_cont(0.25) WITHIN GROUP (ORDER BY dollars_per_mile)      as p25_mi,
                percentile_cont(0.50) WITHIN GROUP (ORDER BY effective_hourly_rate) as p50_hr,
                percentile_cont(0.50) WITHIN GROUP (ORDER BY dollars_per_mile)      as p50_mi,
                percentile_cont(0.75) WITHIN GROUP (ORDER BY effective_hourly_rate) as p75_hr,
                percentile_cont(0.75) WITHIN GROUP (ORDER BY dollars_per_mile)      as p75_mi
            INTO v_stats
            FROM app_private.offer_history
            WHERE day_of_week = v_hex.day_of_week
              AND hour_of_day = v_hex.hour_of_day
              AND is_validated = true
              AND effective_hourly_rate > 0
              AND effective_hourly_rate < 150
              AND dollars_per_mile > 0
              AND dollars_per_mile < 10
              AND created_at >= v_cutoff;

            v_source := 'metro_fallback';
            v_fallback := v_fallback + 1;
        END IF;

        -- ========================================
        -- INSERT with dignity floor enforcement
        -- ========================================
        INSERT INTO app_private.hex_pricing_cache (
            h3_index, day_of_week, hour_of_day,
            revenue_hourly, revenue_mileage,
            ai_hourly, ai_mileage,
            picky_hourly, picky_mileage,
            sample_count, data_source, updated_at
        ) VALUES (
            v_hex.driver_h3,
            v_hex.day_of_week,
            v_hex.hour_of_day,
            -- Revenue: p25 (accept 75% of rides) — floor at dignity minimums
            GREATEST(COALESCE(v_stats.p25_hr, 15.0), 12.0),
            GREATEST(COALESCE(v_stats.p25_mi, v_dignity_mileage), v_dignity_mileage),
            -- AI: p50 (accept 50%) — balanced
            GREATEST(COALESCE(v_stats.p50_hr, 17.0), 12.0),
            GREATEST(COALESCE(v_stats.p50_mi, v_dignity_mileage), v_dignity_mileage),
            -- Picky: p75 (accept top 25%) — selective
            GREATEST(COALESCE(v_stats.p75_hr, 22.0), 15.0),
            GREATEST(COALESCE(v_stats.p75_mi, 1.00), v_dignity_mileage),
            v_stats.samples,
            v_source,
            now()
        )
        ON CONFLICT (h3_index, day_of_week, hour_of_day)
        DO UPDATE SET
            revenue_hourly  = EXCLUDED.revenue_hourly,
            revenue_mileage = EXCLUDED.revenue_mileage,
            ai_hourly       = EXCLUDED.ai_hourly,
            ai_mileage      = EXCLUDED.ai_mileage,
            picky_hourly    = EXCLUDED.picky_hourly,
            picky_mileage   = EXCLUDED.picky_mileage,
            sample_count    = EXCLUDED.sample_count,
            data_source     = EXCLUDED.data_source,
            updated_at      = EXCLUDED.updated_at;

        v_total := v_total + 1;

    END LOOP;

    -- Return summary
    RETURN QUERY SELECT
        v_total,
        v_exact,
        v_ring1,
        v_ring2,
        v_fallback,
        round(EXTRACT(EPOCH FROM clock_timestamp() - v_start) * 1000, 1)::numeric;
END;
$$;


-- ============================================================
-- STEP 3: Freestyle Lookup Function
-- ============================================================
-- Called by decision_engine_v2 in Freestyle mode.
-- Takes driver's current location + time, returns thresholds.
-- Fallback: hex cache → driver freestyle settings → global defaults
-- ============================================================

CREATE OR REPLACE FUNCTION app_private.get_freestyle_thresholds(
    driver_lat_in numeric,
    driver_lng_in numeric,
    strategy_in text DEFAULT 'ai',
    timezone_in text DEFAULT 'America/Chicago'
)
RETURNS TABLE(
    threshold_hourly numeric,
    threshold_mileage numeric,
    source text,
    sample_count integer
)
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
    v_hex       text;
    v_dow       integer;
    v_hour      integer;
    v_result    record;
    v_min_samples integer := 10;
BEGIN
    v_hex := h3_latlng_to_cell(POINT(driver_lng_in::float8, driver_lat_in::float8), 8)::text;
    v_dow  := EXTRACT(DOW FROM now() AT TIME ZONE timezone_in)::integer;
    v_hour := EXTRACT(HOUR FROM now() AT TIME ZONE timezone_in)::integer;

    -- Step 1: Exact hex + day + hour with enough samples
    SELECT
        CASE strategy_in
            WHEN 'revenue' THEN c.revenue_hourly
            WHEN 'picky'   THEN c.picky_hourly
            ELSE c.ai_hourly
        END as hourly,
        CASE strategy_in
            WHEN 'revenue' THEN c.revenue_mileage
            WHEN 'picky'   THEN c.picky_mileage
            ELSE c.ai_mileage
        END as mileage,
        c.data_source,
        c.sample_count
    INTO v_result
    FROM app_private.hex_pricing_cache c
    WHERE c.h3_index = v_hex
      AND c.day_of_week = v_dow
      AND c.hour_of_day = v_hour
      AND c.sample_count >= v_min_samples;

    IF FOUND THEN
        RETURN QUERY SELECT
            v_result.hourly,
            v_result.mileage,
            ('hex_cache:' || v_result.data_source)::text,
            v_result.sample_count;
        RETURN;
    END IF;

    -- Step 2: K-ring weighted average for same day + hour
    -- (catches thin exact-hex data and blends with neighbors)
    SELECT
        (SUM(
            CASE strategy_in
                WHEN 'revenue' THEN c.revenue_hourly
                WHEN 'picky'   THEN c.picky_hourly
                ELSE c.ai_hourly
            END * c.sample_count
        ) / NULLIF(SUM(c.sample_count), 0)) as hourly,
        (SUM(
            CASE strategy_in
                WHEN 'revenue' THEN c.revenue_mileage
                WHEN 'picky'   THEN c.picky_mileage
                ELSE c.ai_mileage
            END * c.sample_count
        ) / NULLIF(SUM(c.sample_count), 0)) as mileage,
        SUM(c.sample_count) as total_samples
    INTO v_result
    FROM app_private.hex_pricing_cache c
    WHERE c.h3_index IN (
        SELECT h3_grid_disk(v_hex::h3index, 1)::text
    )
      AND c.day_of_week = v_dow
      AND c.hour_of_day = v_hour;

    IF v_result.total_samples >= v_min_samples THEN
        RETURN QUERY SELECT
            v_result.hourly::numeric,
            v_result.mileage::numeric,
            ('hex_cache:kring_blend:' || v_dow || 'h' || v_hour)::text,
            v_result.total_samples::integer;
        RETURN;
    END IF;

    -- Step 3: K-ring weighted average for adjacent hours (±1)
    SELECT
        (SUM(
            CASE strategy_in
                WHEN 'revenue' THEN c.revenue_hourly
                WHEN 'picky'   THEN c.picky_hourly
                ELSE c.ai_hourly
            END * c.sample_count
        ) / NULLIF(SUM(c.sample_count), 0)) as hourly,
        (SUM(
            CASE strategy_in
                WHEN 'revenue' THEN c.revenue_mileage
                WHEN 'picky'   THEN c.picky_mileage
                ELSE c.ai_mileage
            END * c.sample_count
        ) / NULLIF(SUM(c.sample_count), 0)) as mileage,
        SUM(c.sample_count) as total_samples
    INTO v_result
    FROM app_private.hex_pricing_cache c
    WHERE c.h3_index IN (
        SELECT h3_grid_disk(v_hex::h3index, 1)::text
    )
      AND c.day_of_week = v_dow
      AND c.hour_of_day IN (v_hour, (v_hour + 1) % 24, (v_hour + 23) % 24);

    IF v_result.total_samples >= v_min_samples THEN
        RETURN QUERY SELECT
            v_result.hourly::numeric,
            v_result.mileage::numeric,
            ('hex_cache:kring_blend_adj:' || v_dow || 'h' || v_hour)::text,
            v_result.total_samples::integer;
        RETURN;
    END IF;

    -- Step 4: Global default
    RETURN QUERY SELECT
        15.0::numeric,
        0.70::numeric,
        'global_default'::text,
        0::integer;
END;
$$;
-- ============================================================================
-- get_market_rate()
-- Phase 3: Market-derived rate from offer_history community data.
-- Uses pre-stored timezone-aware day_of_week/hour_of_day columns.
-- Returns P40 rate — market reality without outlier distortion.
-- Slots into position 2 of the threshold cascade:
--   MAX(hex_cache_rate, get_market_rate(), dignity_floor) x pulse
-- ============================================================================
CREATE OR REPLACE FUNCTION app_private.get_market_rate(
    p_lat       numeric,
    p_lng       numeric,
    p_driver_id text,
    p_ts        timestamptz DEFAULT NOW()
)
RETURNS TABLE(
    market_hourly  numeric,
    market_mileage numeric,
    sample_count   integer,
    data_source    text
)
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
    v_hex         text;
    v_dow         integer;
    v_hour        integer;
    v_min_samples integer := 5;
    v_result      record;
BEGIN
    -- Compute hex at driver location
    v_hex  := h3_latlng_to_cell(POINT(p_lng::float8, p_lat::float8), 8)::text;

    -- Use canonical timezone-aware functions (not raw EXTRACT)
    v_dow  := app_private.driver_dow(p_driver_id, p_ts);
    v_hour := app_private.driver_hour(p_driver_id, p_ts);

    -- ----------------------------------------------------------------
    -- Step 1: Exact hex + day + hour
    -- ----------------------------------------------------------------
    SELECT
        PERCENTILE_CONT(0.40) WITHIN GROUP (ORDER BY effective_hourly_rate)::numeric,
        PERCENTILE_CONT(0.40) WITHIN GROUP (ORDER BY dollars_per_mile)::numeric,
        COUNT(*)::integer
    INTO v_result
    FROM app_private.offer_history
    WHERE driver_h3 = v_hex
      AND day_of_week = v_dow
      AND hour_of_day = v_hour
      AND is_validated = true
      AND effective_hourly_rate > 5
      AND effective_hourly_rate < 150;

    IF v_result.count >= v_min_samples THEN
        RETURN QUERY SELECT
            v_result.percentile_cont::numeric,
            v_result.percentile_cont_1::numeric,
            v_result.count,
            'market:exact_hex'::text;
        RETURN;
    END IF;

    -- ----------------------------------------------------------------
    -- Step 2: K-ring(1) neighbors, same day + hour
    -- ----------------------------------------------------------------
    SELECT
        PERCENTILE_CONT(0.40) WITHIN GROUP (ORDER BY effective_hourly_rate)::numeric,
        PERCENTILE_CONT(0.40) WITHIN GROUP (ORDER BY dollars_per_mile)::numeric,
        COUNT(*)::integer
    INTO v_result
    FROM app_private.offer_history
    WHERE driver_h3 IN (
        SELECT h3_grid_disk(v_hex::h3index, 1)::text
    )
      AND day_of_week = v_dow
      AND hour_of_day = v_hour
      AND is_validated = true
      AND effective_hourly_rate > 5
      AND effective_hourly_rate < 150;

    IF v_result.count >= v_min_samples THEN
        RETURN QUERY SELECT
            v_result.percentile_cont::numeric,
            v_result.percentile_cont_1::numeric,
            v_result.count,
            'market:kring1'::text;
        RETURN;
    END IF;

    -- ----------------------------------------------------------------
    -- Step 3: K-ring(2) neighbors, same day + hour
    -- ----------------------------------------------------------------
    SELECT
        PERCENTILE_CONT(0.40) WITHIN GROUP (ORDER BY effective_hourly_rate)::numeric,
        PERCENTILE_CONT(0.40) WITHIN GROUP (ORDER BY dollars_per_mile)::numeric,
        COUNT(*)::integer
    INTO v_result
    FROM app_private.offer_history
    WHERE driver_h3 IN (
        SELECT h3_grid_disk(v_hex::h3index, 2)::text
    )
      AND day_of_week = v_dow
      AND hour_of_day = v_hour
      AND is_validated = true
      AND effective_hourly_rate > 5
      AND effective_hourly_rate < 150;

    IF v_result.count >= (v_min_samples * 2) THEN
        RETURN QUERY SELECT
            v_result.percentile_cont::numeric,
            v_result.percentile_cont_1::numeric,
            v_result.count,
            'market:kring2'::text;
        RETURN;
    END IF;

    -- ----------------------------------------------------------------
    -- Step 4: Metro fallback — all offer_history for this day + hour
    -- ----------------------------------------------------------------
    SELECT
        PERCENTILE_CONT(0.40) WITHIN GROUP (ORDER BY effective_hourly_rate)::numeric,
        PERCENTILE_CONT(0.40) WITHIN GROUP (ORDER BY dollars_per_mile)::numeric,
        COUNT(*)::integer
    INTO v_result
    FROM app_private.offer_history
    WHERE day_of_week = v_dow
      AND hour_of_day = v_hour
      AND is_validated = true
      AND effective_hourly_rate > 5
      AND effective_hourly_rate < 150;

    IF v_result.count >= v_min_samples THEN
        RETURN QUERY SELECT
            v_result.percentile_cont::numeric,
            v_result.percentile_cont_1::numeric,
            v_result.count,
            'market:metro_fallback'::text;
        RETURN;
    END IF;

    -- ----------------------------------------------------------------
    -- Step 5: No data — return NULL (caller falls through to dignity_floor)
    -- ----------------------------------------------------------------
    RETURN QUERY SELECT
        NULL::numeric,
        NULL::numeric,
        0::integer,
        'market:no_data'::text;
END;
$$;

-- ============================================================================
-- get_market_rate() v2 -- fixes PERCENTILE_CONT record field naming
-- ============================================================================
CREATE OR REPLACE FUNCTION app_private.get_market_rate(
    p_lat       numeric,
    p_lng       numeric,
    p_driver_id text,
    p_ts        timestamptz DEFAULT NOW()
)
RETURNS TABLE(
    market_hourly  numeric,
    market_mileage numeric,
    sample_count   integer,
    data_source    text
)
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
    v_hex         text;
    v_dow         integer;
    v_hour        integer;
    v_min_samples integer := 5;
    v_hourly      numeric;
    v_mileage     numeric;
    v_count       integer;
BEGIN
    v_hex  := h3_latlng_to_cell(POINT(p_lng::float8, p_lat::float8), 8)::text;
    v_dow  := app_private.driver_dow(p_driver_id, p_ts);
    v_hour := app_private.driver_hour(p_driver_id, p_ts);

    -- Step 1: Exact hex + day + hour
    SELECT
        PERCENTILE_CONT(0.40) WITHIN GROUP (ORDER BY effective_hourly_rate)::numeric,
        PERCENTILE_CONT(0.40) WITHIN GROUP (ORDER BY dollars_per_mile)::numeric,
        COUNT(*)::integer
    INTO v_hourly, v_mileage, v_count
    FROM app_private.offer_history
    WHERE driver_h3 = v_hex
      AND day_of_week = v_dow
      AND hour_of_day = v_hour
      AND is_validated = true
      AND effective_hourly_rate > 5
      AND effective_hourly_rate < 150;

    IF v_count >= v_min_samples THEN
        RETURN QUERY SELECT v_hourly, v_mileage, v_count, 'market:exact_hex'::text;
        RETURN;
    END IF;

    -- Step 2: K-ring(1) neighbors, same day + hour
    SELECT
        PERCENTILE_CONT(0.40) WITHIN GROUP (ORDER BY effective_hourly_rate)::numeric,
        PERCENTILE_CONT(0.40) WITHIN GROUP (ORDER BY dollars_per_mile)::numeric,
        COUNT(*)::integer
    INTO v_hourly, v_mileage, v_count
    FROM app_private.offer_history
    WHERE driver_h3 IN (SELECT h3_grid_disk(v_hex::h3index, 1)::text)
      AND day_of_week = v_dow
      AND hour_of_day = v_hour
      AND is_validated = true
      AND effective_hourly_rate > 5
      AND effective_hourly_rate < 150;

    IF v_count >= v_min_samples THEN
        RETURN QUERY SELECT v_hourly, v_mileage, v_count, 'market:kring1'::text;
        RETURN;
    END IF;

    -- Step 3: K-ring(2) neighbors, same day + hour
    SELECT
        PERCENTILE_CONT(0.40) WITHIN GROUP (ORDER BY effective_hourly_rate)::numeric,
        PERCENTILE_CONT(0.40) WITHIN GROUP (ORDER BY dollars_per_mile)::numeric,
        COUNT(*)::integer
    INTO v_hourly, v_mileage, v_count
    FROM app_private.offer_history
    WHERE driver_h3 IN (SELECT h3_grid_disk(v_hex::h3index, 2)::text)
      AND day_of_week = v_dow
      AND hour_of_day = v_hour
      AND is_validated = true
      AND effective_hourly_rate > 5
      AND effective_hourly_rate < 150;

    IF v_count >= (v_min_samples * 2) THEN
        RETURN QUERY SELECT v_hourly, v_mileage, v_count, 'market:kring2'::text;
        RETURN;
    END IF;

    -- Step 4: Metro fallback — all offer_history for this day + hour
    SELECT
        PERCENTILE_CONT(0.40) WITHIN GROUP (ORDER BY effective_hourly_rate)::numeric,
        PERCENTILE_CONT(0.40) WITHIN GROUP (ORDER BY dollars_per_mile)::numeric,
        COUNT(*)::integer
    INTO v_hourly, v_mileage, v_count
    FROM app_private.offer_history
    WHERE day_of_week = v_dow
      AND hour_of_day = v_hour
      AND is_validated = true
      AND effective_hourly_rate > 5
      AND effective_hourly_rate < 150;

    IF v_count >= v_min_samples THEN
        RETURN QUERY SELECT v_hourly, v_mileage, v_count, 'market:metro_fallback'::text;
        RETURN;
    END IF;

    -- Step 5: No data — return NULL (caller falls through to dignity_floor)
    RETURN QUERY SELECT NULL::numeric, NULL::numeric, 0::integer, 'market:no_data'::text;
END;
$$;
