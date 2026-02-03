-- file: decision_engine_v2_1.sql
-- Description: v2.1 - Full spec implementation with backtrack, net progress, market overrides
-- Changes from v2.0:
--   - Added current_lat_in, current_lng_in for accurate backtrack calculation
--   - Added towards_backtrack_tolerance parameter (default 3.0 miles)
--   - Towards Mode now DECLINES with profitability metrics instead of falling through
--   - Added market override threshold selection logic
--   - Added cross-market green zone detection
--   - Added arrival_detected, switch_to_mode, switch_to_market_id return fields

DROP FUNCTION IF EXISTS app_private.decision_engine_v2(text, numeric, numeric, numeric, numeric, numeric, numeric, numeric, numeric, numeric, text, boolean, numeric, numeric, text);
DROP FUNCTION IF EXISTS app_private.decision_engine_v2(text, numeric, numeric, numeric, numeric, numeric, numeric, numeric, numeric, numeric, text, boolean, numeric, numeric, text, numeric, numeric, numeric);

CREATE OR REPLACE FUNCTION app_private.decision_engine_v2(
    user_id_in        text,
    pickup_lat_in     numeric,
    pickup_lng_in     numeric,
    dropoff_lat_in    numeric,
    dropoff_lng_in    numeric,
    gross_payout_in   numeric,
    trip_miles_in     numeric,
    trip_minutes_in   numeric,
    pickup_minutes_in numeric DEFAULT 0,
    pickup_miles_in   numeric DEFAULT 0,
    market_id_in      text DEFAULT NULL,
    -- Towards Mode Parameters
    towards_active    boolean DEFAULT FALSE,
    towards_target_lat numeric DEFAULT NULL,
    towards_target_lng numeric DEFAULT NULL,
    towards_market_id  text DEFAULT NULL,
    -- NEW PARAMETERS
    current_lat_in     numeric DEFAULT NULL,  -- Driver's current GPS position
    current_lng_in     numeric DEFAULT NULL,
    towards_backtrack_tolerance numeric DEFAULT 3.0  -- Miles willing to backtrack
)
RETURNS TABLE(
    verdict                  text,
    reason                   text,
    net_pay                  numeric,
    hourly_rate              numeric,
    dollars_per_mile         numeric,
    deadhead_miles           numeric,
    deadhead_cost            numeric,
    arrival_detected         boolean,
    switch_to_mode           text,
    switch_to_market_id      text,
    threshold_source         text
)
LANGUAGE plpgsql
STABLE
AS $function$
DECLARE
    -- Settings
    v_settings               jsonb;
    v_calc_method            text;
    v_global_per_hour        numeric;
    v_global_per_mile        numeric;
    v_threshold_per_hour     numeric;
    v_threshold_per_mile     numeric;
    v_deadhead_percent       numeric;
    v_max_pickup_miles       numeric;

    -- Geospatial
    H3_RES                   constant integer := 8;
    v_pickup_hex             text;
    v_dropoff_hex            text;
    v_active_market          jsonb;
    v_local_green_zones      text[];
    v_red_zones              jsonb;

    -- Towards Mode
    v_current_to_target      numeric;
    v_pickup_to_target       numeric;
    v_dropoff_to_target      numeric;
    v_backtrack_miles        numeric;
    v_net_progress           numeric;
    v_hypothetical_hourly    numeric;
    v_hypothetical_per_mile  numeric;

    -- Cross-market detection
    v_other_market           jsonb;
    v_other_market_name      text;
    v_other_market_id        text;
    v_lands_in_other_market  boolean := FALSE;

    -- Calculation internals
    v_dist_to_green          integer := 0;
    v_return_miles           numeric := 0;
    v_return_minutes         numeric := 0;
    v_calculated_cost        numeric := 0;
    v_total_time_hr          numeric;

    -- Outputs
    v_net_pay                numeric;
    v_hourly_rate            numeric;
    v_dollars_per_mile       numeric;
    v_verdict                text;
    v_reason                 text;
    v_pass_hourly            boolean := TRUE;
    v_pass_mileage           boolean := TRUE;
    v_arrival_detected       boolean := FALSE;
    v_switch_to_mode         text := NULL;
    v_switch_to_market_id    text := NULL;
    v_threshold_source       text := 'global';

BEGIN
    -- 1. Fetch User Settings
    SELECT settings INTO v_settings FROM app_private.driver_settings_new WHERE driver_id = user_id_in;
    IF v_settings IS NULL THEN
        RETURN QUERY SELECT 'ERROR'::text, 'No settings found'::text, 0.0::numeric, 0.0::numeric, 0.0::numeric, 
                            0.0::numeric, 0.0::numeric, FALSE, NULL::text, NULL::text, 'error'::text;
        RETURN;
    END IF;

    -- 2. Extract Active Market & Global Settings
    SELECT elem INTO v_active_market
    FROM jsonb_array_elements(v_settings->'markets') elem
    WHERE (elem->>'id') = market_id_in LIMIT 1;

    v_calc_method         := COALESCE(v_settings->>'calculation_method', 'both');
    v_global_per_hour     := COALESCE((v_settings->>'min_effective_hourly_rate')::numeric, 22.0);
    v_global_per_mile     := COALESCE((v_settings->>'min_effective_dollar_per_mile')::numeric, 1.67);
    v_deadhead_percent    := COALESCE((v_settings->>'deadhead_percent')::numeric, 1.0);
    v_max_pickup_miles    := COALESCE((v_settings->>'max_pickup_miles')::numeric, 5.0);
    v_red_zones           := v_settings->'redZones';

    IF v_active_market IS NOT NULL THEN
        SELECT array_agg(elem) INTO v_local_green_zones
        FROM jsonb_array_elements_text(v_active_market->'greenZones') elem;
    END IF;

    -- 3. H3 Conversion
    v_pickup_hex  := h3_latlng_to_cell(point(pickup_lng_in::float8, pickup_lat_in::float8), H3_RES)::text;
    v_dropoff_hex := h3_latlng_to_cell(point(dropoff_lng_in::float8, dropoff_lat_in::float8), H3_RES)::text;

    -- 4. Layer 1: Red Zone Veto
    IF v_red_zones IS NOT NULL AND jsonb_array_length(v_red_zones) > 0 THEN
        IF EXISTS (SELECT 1 FROM jsonb_array_elements_text(v_red_zones) r(hex) WHERE r.hex IN (v_pickup_hex, v_dropoff_hex)) THEN
            RETURN QUERY SELECT 'DECLINE'::text, 'Location in Red Zone'::text, 0.0::numeric, 0.0::numeric, 0.0::numeric, 
                                0.0::numeric, 0.0::numeric, FALSE, NULL::text, NULL::text, 'n/a'::text;
            RETURN;
        END IF;
    END IF;

    -- 5. Layer 2: Pickup Distance Filter
    IF pickup_miles_in > v_max_pickup_miles THEN
        RETURN QUERY SELECT 'DECLINE'::text, format('Pickup too far (%s mi)', pickup_miles_in)::text, 0.0::numeric, 0.0::numeric, 
                            0.0::numeric, 0.0::numeric, 0.0::numeric, FALSE, NULL::text, NULL::text, 'n/a'::text;
        RETURN;
    END IF;

    -- 6. Layer 3: TOWARDS MODE
    IF towards_active AND towards_target_lat IS NOT NULL AND towards_target_lng IS NOT NULL THEN
        -- Use current location if provided, else use pickup location
        IF current_lat_in IS NOT NULL AND current_lng_in IS NOT NULL THEN
            v_current_to_target := ST_Distance(
                ST_SetSRID(ST_MakePoint(current_lng_in::float8, current_lat_in::float8), 4326)::geography,
                ST_SetSRID(ST_MakePoint(towards_target_lng::float8, towards_target_lat::float8), 4326)::geography
            ) / 1609.34;
        ELSE
            v_current_to_target := ST_Distance(
                ST_SetSRID(ST_MakePoint(pickup_lng_in::float8, pickup_lat_in::float8), 4326)::geography,
                ST_SetSRID(ST_MakePoint(towards_target_lng::float8, towards_target_lat::float8), 4326)::geography
            ) / 1609.34;
        END IF;

        v_pickup_to_target := ST_Distance(
            ST_SetSRID(ST_MakePoint(pickup_lng_in::float8, pickup_lat_in::float8), 4326)::geography,
            ST_SetSRID(ST_MakePoint(towards_target_lng::float8, towards_target_lat::float8), 4326)::geography
        ) / 1609.34;

        v_dropoff_to_target := ST_Distance(
            ST_SetSRID(ST_MakePoint(dropoff_lng_in::float8, dropoff_lat_in::float8), 4326)::geography,
            ST_SetSRID(ST_MakePoint(towards_target_lng::float8, towards_target_lat::float8), 4326)::geography
        ) / 1609.34;

        v_backtrack_miles := v_pickup_to_target - v_current_to_target;
        v_net_progress := v_current_to_target - v_dropoff_to_target;

        -- Check for arrival (if target is a market)
        IF towards_market_id IS NOT NULL THEN
            SELECT elem INTO v_other_market
            FROM jsonb_array_elements(v_settings->'markets') elem
            WHERE (elem->>'id') = towards_market_id LIMIT 1;
            
            IF v_other_market IS NOT NULL THEN
                IF EXISTS (
                    SELECT 1 FROM jsonb_array_elements_text(v_other_market->'greenZones') gz
                    WHERE gz = v_dropoff_hex
                ) THEN
                    v_arrival_detected := TRUE;
                    v_switch_to_mode := 'puddle_jump';
                    v_switch_to_market_id := towards_market_id;
                END IF;
            END IF;
        END IF;

        -- Accept if net progress > 0 AND backtrack within tolerance
        IF v_net_progress > 0 AND v_backtrack_miles <= towards_backtrack_tolerance THEN
            RETURN QUERY SELECT
                'ACCEPT'::text,
                format('Towards: %s mi net progress (%s mi backtrack)', 
                       round(v_net_progress, 1), round(v_backtrack_miles, 1))::text,
                gross_payout_in::numeric,
                (gross_payout_in / NULLIF((trip_minutes_in + pickup_minutes_in) / 60.0, 0))::numeric,
                (gross_payout_in / NULLIF(pickup_miles_in + trip_miles_in, 0))::numeric,
                0.0::numeric,
                0.0::numeric,
                v_arrival_detected,
                v_switch_to_mode,
                v_switch_to_market_id,
                'global'::text;
            RETURN;
        END IF;

        -- DECLINE with profitability metrics
        v_hypothetical_hourly := gross_payout_in / NULLIF((trip_minutes_in + pickup_minutes_in) / 60.0, 0);
        v_hypothetical_per_mile := gross_payout_in / NULLIF(pickup_miles_in + trip_miles_in, 0);

        IF v_net_progress <= 0 THEN
            v_reason := format('Towards mode: ride moves away from target (would earn $%s/hr, $%s/mi)',
                             round(v_hypothetical_hourly, 2), round(v_hypothetical_per_mile, 2));
            IF v_hypothetical_hourly > (v_global_per_hour * 1.5) OR v_hypothetical_per_mile > (v_global_per_mile * 1.5) THEN
                v_reason := 'HIGH VALUE: ' || v_reason;
            END IF;
        ELSE
            v_reason := format('Towards mode: backtrack (%s mi) exceeds tolerance (would earn $%s/hr, $%s/mi)',
                             round(v_backtrack_miles, 1), round(v_hypothetical_hourly, 2), round(v_hypothetical_per_mile, 2));
            IF v_hypothetical_hourly > (v_global_per_hour * 1.5) OR v_hypothetical_per_mile > (v_global_per_mile * 1.5) THEN
                v_reason := 'HIGH VALUE: ' || v_reason;
            END IF;
        END IF;

        RETURN QUERY SELECT 'DECLINE'::text, v_reason, gross_payout_in::numeric, v_hypothetical_hourly, v_hypothetical_per_mile,
                            0.0::numeric, 0.0::numeric, FALSE, NULL::text, NULL::text, 'global'::text;
        RETURN;
    END IF;

    -- 7. Layer 3: PUDDLE JUMP MODE - Check dropoff location
    
    -- Step 1: Check if dropoff in current market's green zones
    IF v_local_green_zones IS NOT NULL AND v_dropoff_hex = ANY(v_local_green_zones) THEN
        v_return_miles := 0;
        v_return_minutes := 0;
        v_threshold_source := COALESCE(v_active_market->>'name', 'current_market');
        -- Use current market overrides
        v_threshold_per_hour := COALESCE((v_active_market->>'min_effective_hourly_rate')::numeric, v_global_per_hour);
        v_threshold_per_mile := COALESCE((v_active_market->>'min_effective_dollar_per_mile')::numeric, v_global_per_mile);
    ELSE
        -- Step 2: Check if dropoff in OTHER market's green zones
        FOR v_other_market IN 
            SELECT elem FROM jsonb_array_elements(v_settings->'markets') elem
            WHERE (elem->>'id') != market_id_in
        LOOP
            IF EXISTS (
                SELECT 1 FROM jsonb_array_elements_text(v_other_market->'greenZones') gz
                WHERE gz = v_dropoff_hex
            ) THEN
                v_lands_in_other_market := TRUE;
                v_other_market_name := v_other_market->>'name';
                v_other_market_id := v_other_market->>'id';
                v_return_miles := 0;
                v_return_minutes := 0;
                v_threshold_source := 'destination_market:' || v_other_market_name;
                -- Use destination market overrides
                v_threshold_per_hour := COALESCE((v_other_market->>'min_effective_hourly_rate')::numeric, v_global_per_hour);
                v_threshold_per_mile := COALESCE((v_other_market->>'min_effective_dollar_per_mile')::numeric, v_global_per_mile);
                EXIT;
            END IF;
        END LOOP;

        -- Step 3: Grey Zone - Calculate deadhead
        IF NOT v_lands_in_other_market THEN
            IF v_local_green_zones IS NOT NULL AND array_length(v_local_green_zones, 1) > 0 THEN
                BEGIN
                    SELECT MIN(h3_grid_distance(v_dropoff_hex::h3index, g_hex::h3index))
                    INTO v_dist_to_green
                    FROM unnest(v_local_green_zones) g_hex;
                    v_return_miles := COALESCE(v_dist_to_green, 50) * 0.4;
                EXCEPTION WHEN OTHERS THEN
                    v_return_miles := 20.0;
                END;
            ELSE
                v_return_miles := 20.0;
            END IF;
            v_return_minutes := v_return_miles * 2.0;
            v_threshold_source := COALESCE(v_active_market->>'name', 'current_market');
            -- Use current market overrides (still working this market)
            v_threshold_per_hour := COALESCE((v_active_market->>'min_effective_hourly_rate')::numeric, v_global_per_hour);
            v_threshold_per_mile := COALESCE((v_active_market->>'min_effective_dollar_per_mile')::numeric, v_global_per_mile);
        END IF;
    END IF;

    -- 8. Layer 5: Calculate deadhead cost
    v_calculated_cost := v_return_miles * COALESCE((v_settings->>'cost_per_mile')::numeric, 0.67) * v_deadhead_percent;

    -- 9. Layer 5: Calculate financial metrics
    v_total_time_hr := (trip_minutes_in + COALESCE(pickup_minutes_in, 0) + v_return_minutes) / 60.0;
    v_net_pay := gross_payout_in - v_calculated_cost;
    v_hourly_rate := gross_payout_in / NULLIF(v_total_time_hr, 0);
    v_dollars_per_mile := gross_payout_in / NULLIF((pickup_miles_in + trip_miles_in + v_return_miles), 0);

    -- 10. Layer 5: Apply thresholds
    IF v_calc_method IN ('per_hour', 'both') AND v_hourly_rate < v_threshold_per_hour THEN v_pass_hourly := FALSE; END IF;
    IF v_calc_method IN ('per_mile', 'both') AND v_dollars_per_mile < v_threshold_per_mile THEN v_pass_mileage := FALSE; END IF;

    -- 11. Layer 6: Final verdict
    IF v_pass_hourly AND v_pass_mileage THEN
        v_verdict := 'ACCEPT';
        IF v_lands_in_other_market THEN
            v_reason := 'Lands in: ' || v_other_market_name;
        ELSIF v_return_miles = 0 THEN
            v_reason := 'Stays in market';
        ELSE
            v_reason := 'Rates met';
        END IF;
    ELSE
        v_verdict := 'DECLINE';
        IF NOT v_pass_hourly AND NOT v_pass_mileage THEN
            v_reason := format('Both rates too low ($%s/hr, $%s/mi)', round(v_hourly_rate, 2), round(v_dollars_per_mile, 2));
        ELSIF NOT v_pass_hourly THEN
            v_reason := format('Hourly rate too low ($%s/hr)', round(v_hourly_rate, 2));
        ELSIF NOT v_pass_mileage THEN
            v_reason := format('Mileage rate too low ($%s/mi)', round(v_dollars_per_mile, 2));
        END IF;

        IF v_calculated_cost > (gross_payout_in * 0.2) AND v_return_miles > 0 THEN
            v_reason := v_reason || format('. High return cost: $%s', round(v_calculated_cost, 2));
        END IF;
    END IF;

    RETURN QUERY SELECT
        v_verdict,
        v_reason,
        round(v_net_pay, 2),
        round(v_hourly_rate, 2),
        round(v_dollars_per_mile, 2),
        round(v_return_miles, 1),
        round(v_calculated_cost, 2),
        v_arrival_detected,
        v_switch_to_mode,
        v_switch_to_market_id,
        v_threshold_source;
END;
$function$;