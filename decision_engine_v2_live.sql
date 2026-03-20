CREATE OR REPLACE FUNCTION app_private.decision_engine_v2(user_id_in text, pickup_lat_in numeric, pickup_lng_in numeric, dropoff_lat_in numeric, dropoff_lng_in numeric, gross_payout_in numeric, trip_miles_in numeric, trip_minutes_in numeric, pickup_minutes_in numeric DEFAULT 0, pickup_miles_in numeric DEFAULT 0, market_id_in text DEFAULT NULL::text, towards_active boolean DEFAULT false, towards_target_lat numeric DEFAULT NULL::numeric, towards_target_lng numeric DEFAULT NULL::numeric, towards_market_id text DEFAULT NULL::text, current_lat_in numeric DEFAULT NULL::numeric, current_lng_in numeric DEFAULT NULL::numeric, towards_backtrack_tolerance numeric DEFAULT 3.0, is_puddle_jump_mode boolean DEFAULT true)
 RETURNS TABLE(verdict text, reason text, net_pay numeric, hourly_rate numeric, dollars_per_mile numeric, deadhead_miles numeric, deadhead_cost numeric, arrival_detected boolean, switch_to_mode text, switch_to_market_id text, threshold_source text, trace_data jsonb)
 LANGUAGE plpgsql
 STABLE
AS $function$
DECLARE
    -- [Standard Variables]
    v_settings               jsonb;
    v_calc_method            text;
    v_global_per_hour        numeric;
    v_global_per_mile        numeric;
    v_threshold_per_hour     numeric;
    v_threshold_per_mile     numeric;
    v_deadhead_percent       numeric;
    v_deadhead_basis         text;
    v_max_pickup_miles       numeric;
    H3_RES                   constant integer := 8;
    v_pickup_hex             text;
    v_dropoff_hex            text;
    v_active_market          jsonb;
    v_local_green_zones      text[];
    v_red_zones              jsonb;
    v_current_to_target      numeric := 0;
    v_pickup_to_target       numeric := 0;
    v_dropoff_to_target      numeric := 0;
    v_backtrack_miles        numeric := 0;
    v_net_progress           numeric := 0;
    v_other_market           jsonb;
    v_other_market_name      text;
    v_other_market_id        text;
    v_lands_in_other_market  boolean := FALSE;
    v_dist_to_green          integer := 0;
    v_return_miles           numeric := 0;
    v_return_minutes         numeric := 0;
    v_calculated_cost        numeric := 0;
    v_total_time_hr          numeric;
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

    -- PHYSICS VARIABLES
    v_gps_pickup_dist        numeric := 0;
    v_effective_pickup_miles numeric := 0;
    v_gps_trip_dist          numeric := 0;
    v_effective_trip_miles   numeric := 0;

    -- ARRIVAL & OVERSHOOT VARIABLES
    v_target_hex             h3index;
    v_target_cluster         h3index[];
    v_target_market_zones    text[];
    v_overshoot_h3_dist      integer := 0;
    v_overshoot_miles        numeric := 0;
    v_overshoot_cost         numeric := 0;
    v_overshoot_return_min   numeric := 0;
    v_overshoot_total_time   numeric := 0;
    v_overshoot_net_pay      numeric := 0;
    v_overshoot_hourly       numeric := 0;

    -- EFFICIENCY VARIABLES
    v_efficiency             numeric := 0;
    v_total_work_miles       numeric := 0;
    v_trace_data             jsonb;

    -- [SHIFT] VARIABLES
    v_shift                  jsonb := NULL;
    v_shift_id               text := NULL;
    v_shift_name             text := NULL;
    v_current_hour           integer;
    v_current_dow            integer;
    v_towards_efficiency_threshold numeric;
    v_timezone               text;
    v_auto_optimize          boolean;

    -- ADDED: For improved deadhead calculation
    v_nearest_hex            text;
    v_nearest_hex_lat        numeric;
    v_nearest_hex_lng        numeric;
    v_cost_per_mile          numeric;

    -- *** NEW: TOWARDS dynamic target variables ***
    v_towards_market         jsonb;           -- The TOWARDS destination market object
    v_towards_green_zones    text[];          -- Green zones of the TOWARDS market
    v_dynamic_target_lat     numeric;         -- Computed nearest green hex lat
    v_dynamic_target_lng     numeric;         -- Computed nearest green hex lng
    v_dynamic_target_hex     text;            -- Which hex was chosen as target
    v_has_towards_target     boolean := FALSE; -- Whether we have a valid target

BEGIN
    -- Get Driver Settings
    SELECT settings INTO v_settings FROM app_private.driver_settings_new WHERE driver_id = user_id_in;

    IF v_settings IS NULL THEN
        RETURN QUERY SELECT 'ERROR'::text, 'No settings found'::text, 0.0::numeric, 0.0::numeric, 0.0::numeric,
                            0.0::numeric, 0.0::numeric, FALSE, NULL::text, NULL::text, 'error'::text, '{}'::jsonb;
        RETURN;
    END IF;

    -- Find Active Market
    SELECT elem INTO v_active_market
    FROM jsonb_array_elements(v_settings->'markets') elem
    WHERE (elem->>'id') = market_id_in LIMIT 1;

    -- Set Thresholds & Settings
    v_calc_method           := COALESCE(v_settings->>'calculation_method', 'both');
    v_global_per_hour       := COALESCE((v_settings->>'min_effective_hourly_rate')::numeric, 22.0);
    v_global_per_mile       := COALESCE((v_settings->>'min_effective_dollar_per_mile')::numeric, 1.67);
    v_deadhead_percent      := COALESCE((v_settings->>'deadhead_percent')::numeric, 1.0);
    v_deadhead_basis        := COALESCE(v_settings->>'deadhead_basis', 'hourly');
    v_cost_per_mile         := COALESCE((v_settings->>'cost_per_mile')::numeric, 0.67);
    v_max_pickup_miles      := COALESCE((v_settings->>'max_pickup_miles')::numeric, 25.0);
    v_red_zones             := v_settings->'redZones';

    -- [TIMEZONE]
    v_timezone              := COALESCE(v_settings->>'timezone', 'America/Chicago');

    -- [AUTO-OPTIMIZE]
    v_auto_optimize         := COALESCE((v_settings->>'autoOptimizeEnabled')::boolean, true);

    -- [SHIFT] Get configurable TOWARDS efficiency threshold
    v_towards_efficiency_threshold := COALESCE((v_settings->>'towardsEfficiencyThreshold')::numeric, 0.40);

    -- [SHIFT] DETERMINE ACTIVE SHIFT
    v_current_hour := EXTRACT(HOUR FROM NOW() AT TIME ZONE v_timezone)::integer;
    v_current_dow := EXTRACT(DOW FROM NOW() AT TIME ZONE v_timezone)::integer;

    IF v_settings->'shifts' IS NOT NULL THEN
        SELECT elem INTO v_shift
        FROM jsonb_array_elements(v_settings->'shifts') elem
        WHERE
            EXISTS (SELECT 1 FROM jsonb_array_elements_text(elem->'days') d WHERE d::integer = v_current_dow)
            AND (
                ((elem->>'startHour')::integer <= (elem->>'endHour')::integer AND v_current_hour >= (elem->>'startHour')::integer AND v_current_hour < (elem->>'endHour')::integer)
                OR
                ((elem->>'startHour')::integer > (elem->>'endHour')::integer AND (v_current_hour >= (elem->>'startHour')::integer OR v_current_hour < (elem->>'endHour')::integer))
            )
        LIMIT 1;

        IF v_shift IS NOT NULL THEN
            v_shift_id := v_shift->>'id';
            v_shift_name := v_shift->>'name';
        END IF;
    END IF;

    -- Green Zone Check
    IF v_active_market IS NOT NULL THEN
        SELECT array_agg(elem) INTO v_local_green_zones
        FROM jsonb_array_elements_text(v_active_market->'greenZones') elem;
    END IF;

    v_pickup_hex  := h3_latlng_to_cell(point(pickup_lng_in::float8, pickup_lat_in::float8), H3_RES)::text;
    v_dropoff_hex := h3_latlng_to_cell(point(dropoff_lng_in::float8, dropoff_lat_in::float8), H3_RES)::text;

    -- Red Zone Check
    IF v_red_zones IS NOT NULL AND jsonb_array_length(v_red_zones) > 0 THEN
        IF EXISTS (SELECT 1 FROM jsonb_array_elements_text(v_red_zones) r(hex) WHERE r.hex IN (v_pickup_hex, v_dropoff_hex)) THEN
            RETURN QUERY SELECT 'DECLINE'::text, 'Location in Red Zone'::text, 0.0::numeric, 0.0::numeric, 0.0::numeric,
                                0.0::numeric, 0.0::numeric, FALSE, NULL::text, NULL::text, 'n/a'::text, '{}'::jsonb;
            RETURN;
        END IF;
    END IF;

    -- PHYSICS CHECK
    IF current_lat_in IS NOT NULL AND current_lng_in IS NOT NULL AND pickup_lat_in IS NOT NULL AND pickup_lng_in IS NOT NULL THEN
        v_gps_pickup_dist := ST_Distance(
            ST_SetSRID(ST_MakePoint(current_lng_in::float8, current_lat_in::float8), 4326)::geography,
            ST_SetSRID(ST_MakePoint(pickup_lng_in::float8, pickup_lat_in::float8), 4326)::geography
        ) / 1609.34;
    END IF;

    v_effective_pickup_miles := pickup_miles_in;
    IF pickup_miles_in < 0.1 OR v_gps_pickup_dist > pickup_miles_in THEN
        IF v_gps_pickup_dist > 0.1 THEN v_effective_pickup_miles := v_gps_pickup_dist; END IF;
    END IF;

    IF pickup_lat_in IS NOT NULL AND pickup_lng_in IS NOT NULL AND dropoff_lat_in IS NOT NULL AND dropoff_lng_in IS NOT NULL THEN
        v_gps_trip_dist := ST_Distance(
            ST_SetSRID(ST_MakePoint(pickup_lng_in::float8, pickup_lat_in::float8), 4326)::geography,
            ST_SetSRID(ST_MakePoint(dropoff_lng_in::float8, dropoff_lat_in::float8), 4326)::geography
        ) / 1609.34;
    END IF;

    v_effective_trip_miles := trip_miles_in;
    IF trip_miles_in < 0.1 OR trip_miles_in < v_gps_trip_dist THEN
        IF v_gps_trip_dist > 0.1 THEN v_effective_trip_miles := v_gps_trip_dist * 1.3; END IF;
    END IF;

    -- PRE-CALCULATION
    v_total_work_miles := v_effective_pickup_miles + v_effective_trip_miles;
    v_total_time_hr := (trip_minutes_in + pickup_minutes_in) / 60.0;
    v_net_pay := gross_payout_in;
    v_hourly_rate := gross_payout_in / NULLIF(v_total_time_hr, 0);
    v_dollars_per_mile := gross_payout_in / NULLIF(v_total_work_miles, 0);

    -- =========================================================================
    -- TOWARDS MODE LOGIC (FIXED: Dynamic Nearest Green Hex Targeting)
    -- =========================================================================
    IF towards_active THEN

        -- STEP 1: Resolve the TOWARDS target coordinates
        IF towards_market_id IS NOT NULL AND current_lat_in IS NOT NULL AND current_lng_in IS NOT NULL THEN
            -- === OPTION B: SQL computes nearest green hex edge ===
            -- Load the TOWARDS destination market
            SELECT elem INTO v_towards_market
            FROM jsonb_array_elements(v_settings->'markets') elem
            WHERE (elem->>'id') = towards_market_id LIMIT 1;

            IF v_towards_market IS NOT NULL THEN
                -- Load green zones of the TOWARDS market
                SELECT array_agg(gz) INTO v_towards_green_zones
                FROM jsonb_array_elements_text(v_towards_market->'greenZones') gz;

                IF v_towards_green_zones IS NOT NULL AND array_length(v_towards_green_zones, 1) > 0 THEN
                    -- Find nearest green hex to driver's CURRENT position
                    -- Uses PostGIS for accurate distance, not H3 grid_distance
                    BEGIN
                        SELECT 
                            g_hex,
                            (h3_cell_to_latlng(g_hex::h3index))[1],  -- lat
                            (h3_cell_to_latlng(g_hex::h3index))[0]   -- lng
                        INTO v_dynamic_target_hex, v_dynamic_target_lat, v_dynamic_target_lng
                        FROM unnest(v_towards_green_zones) g_hex
                        ORDER BY ST_Distance(
                            ST_SetSRID(ST_MakePoint(current_lng_in::float8, current_lat_in::float8), 4326)::geography,
                            ST_SetSRID(ST_MakePoint(
                                (h3_cell_to_latlng(g_hex::h3index))[0]::float8,
                                (h3_cell_to_latlng(g_hex::h3index))[1]::float8
                            ), 4326)::geography
                        )
                        LIMIT 1;

                        IF v_dynamic_target_lat IS NOT NULL THEN
                            v_has_towards_target := TRUE;
                        END IF;
                    EXCEPTION WHEN OTHERS THEN
                        -- If H3/PostGIS fails, fall through to legacy
                        v_has_towards_target := FALSE;
                    END;
                END IF;
            END IF;
        END IF;

        -- Fallback: use passed-in target coords (legacy behavior)
        IF NOT v_has_towards_target AND towards_target_lat IS NOT NULL AND towards_target_lng IS NOT NULL THEN
            v_dynamic_target_lat := towards_target_lat;
            v_dynamic_target_lng := towards_target_lng;
            v_dynamic_target_hex := 'legacy_target';
            v_has_towards_target := TRUE;
        END IF;

        -- STEP 2: Calculate distances using resolved target
        IF v_has_towards_target THEN
            v_current_to_target := ST_Distance(
                ST_SetSRID(ST_MakePoint(current_lng_in::float8, current_lat_in::float8), 4326)::geography,
                ST_SetSRID(ST_MakePoint(v_dynamic_target_lng::float8, v_dynamic_target_lat::float8), 4326)::geography
            ) / 1609.34;

            v_pickup_to_target := ST_Distance(
                ST_SetSRID(ST_MakePoint(pickup_lng_in::float8, pickup_lat_in::float8), 4326)::geography,
                ST_SetSRID(ST_MakePoint(v_dynamic_target_lng::float8, v_dynamic_target_lat::float8), 4326)::geography
            ) / 1609.34;

            v_dropoff_to_target := ST_Distance(
                ST_SetSRID(ST_MakePoint(dropoff_lng_in::float8, dropoff_lat_in::float8), 4326)::geography,
                ST_SetSRID(ST_MakePoint(v_dynamic_target_lng::float8, v_dynamic_target_lat::float8), 4326)::geography
            ) / 1609.34;

            v_backtrack_miles := v_pickup_to_target - v_current_to_target;
            v_net_progress := v_current_to_target - v_dropoff_to_target;

            -- Efficiency Calc
            IF v_total_work_miles > 0 THEN v_efficiency := v_net_progress / v_total_work_miles;
            ELSE v_efficiency := 1.0; END IF;

            -- DETECT ARRIVAL: Check if dropoff lands in TOWARDS market green zones
            IF towards_market_id IS NOT NULL AND v_towards_market IS NOT NULL THEN
                IF v_towards_green_zones IS NOT NULL AND v_dropoff_hex = ANY(v_towards_green_zones) THEN
                    v_arrival_detected := TRUE;
                    v_switch_to_mode := 'puddle_jump';
                    v_switch_to_market_id := towards_market_id;
                END IF;
            ELSIF v_dynamic_target_lat IS NOT NULL AND v_dynamic_target_hex != 'legacy_target' THEN
                -- Market-based arrival (shouldn't hit this path if towards_market_id was set, but safety)
                v_target_hex := h3_latlng_to_cell(point(v_dynamic_target_lng::float8, v_dynamic_target_lat::float8), H3_RES);
                SELECT array_agg(hex) INTO v_target_cluster FROM h3_grid_disk(v_target_hex, 1) hex;
                IF v_dropoff_hex::h3index = ANY(v_target_cluster) THEN
                    v_arrival_detected := TRUE;
                END IF;
            ELSIF v_dynamic_target_hex = 'legacy_target' THEN
                -- Legacy: arrival if dropoff is within 1 hex ring of target
                v_target_hex := h3_latlng_to_cell(point(v_dynamic_target_lng::float8, v_dynamic_target_lat::float8), H3_RES);
                SELECT array_agg(hex) INTO v_target_cluster FROM h3_grid_disk(v_target_hex, 1) hex;
                IF v_dropoff_hex::h3index = ANY(v_target_cluster) THEN
                    v_arrival_detected := TRUE;
                END IF;
            END IF;

            -- DECISION LOGIC (TOWARDS)
            IF v_arrival_detected THEN
                 v_verdict := 'ACCEPT';
                 v_reason := format('Arrived in target (%s mi progress)', round(v_net_progress, 1));

            ELSIF v_net_progress > 0 AND v_backtrack_miles <= towards_backtrack_tolerance THEN

              -- DYNAMIC EFFICIENCY: stricter when close, lenient when far
             v_towards_efficiency_threshold := CASE
                 WHEN v_current_to_target >= 200 THEN 0.15
                 WHEN v_current_to_target >= 100 THEN 0.20
                 WHEN v_current_to_target >= 50  THEN 0.30
                 WHEN v_current_to_target >= 20  THEN 0.35
                 WHEN v_current_to_target >= 10  THEN 0.40
                 WHEN v_current_to_target >= 5   THEN 0.50
                 ELSE 0.65
             END;

             IF v_efficiency < v_towards_efficiency_threshold THEN
                     v_verdict := 'DECLINE';
                     v_reason := format('Inefficient route: %s%% vs %s%% required at %s mi out (%s mi gained on %s mi trip)',
                                        round(v_efficiency * 100, 0),
                                        round(v_towards_efficiency_threshold * 100, 0),
                                        round(v_current_to_target, 1),
                                        round(v_net_progress, 1),
                                        round(v_total_work_miles, 1));                ELSE
                     -- OVERSHOOT CHECK
                     v_overshoot_h3_dist := 0;
                     IF v_dropoff_to_target > 0.5 AND v_gps_trip_dist > v_pickup_to_target THEN
                          v_overshoot_miles := v_dropoff_to_target;
                          v_overshoot_h3_dist := 1;
                     END IF;

                     IF v_overshoot_h3_dist > 0 THEN
                         v_overshoot_cost := v_overshoot_miles * v_cost_per_mile * v_deadhead_percent;
                         v_overshoot_return_min := v_overshoot_miles * 2.0;
                         v_overshoot_total_time := (trip_minutes_in + pickup_minutes_in + v_overshoot_return_min) / 60.0;
                         v_overshoot_net_pay := gross_payout_in - v_overshoot_cost;
                         v_overshoot_hourly := v_overshoot_net_pay / NULLIF(v_overshoot_total_time, 0);

                         IF v_overshoot_hourly >= v_global_per_hour THEN
                             v_verdict := 'ACCEPT';
                             v_reason := format('%s mi progress, %s mi overshoot ($%s/hr after return)', round(v_net_progress, 1), round(v_overshoot_miles, 1), round(v_overshoot_hourly, 2));
                             v_return_miles := v_overshoot_miles;
                             v_calculated_cost := v_overshoot_cost;
                             v_net_pay := v_overshoot_net_pay;
                             v_hourly_rate := v_overshoot_hourly;
                         ELSE
                             v_verdict := 'DECLINE';
                             v_reason := format('Overshoot %s mi kills profit ($%s/hr after return)', round(v_overshoot_miles, 1), round(v_overshoot_hourly, 2));
                             v_net_pay := v_overshoot_net_pay;
                             v_hourly_rate := v_overshoot_hourly;
                         END IF;
                     ELSE
                         v_verdict := 'ACCEPT';
                         v_reason := format('%s mi net progress (%s mi backtrack)', round(v_net_progress, 1), round(v_backtrack_miles, 1));
                     END IF;
                 END IF;

            ELSE
                v_verdict := 'DECLINE';
                IF v_net_progress <= 0 THEN
                    v_reason := format('Ride moves away from target (would earn $%s/hr, $%s/mi)', round(v_hourly_rate, 2), round(v_dollars_per_mile, 2));
                ELSE
                    v_reason := format('Backtrack (%s mi) exceeds tolerance (would earn $%s/hr, $%s/mi)', round(v_backtrack_miles, 1), round(v_hourly_rate, 2), round(v_dollars_per_mile, 2));
                END IF;
            END IF;

        ELSE
            -- TOWARDS active but NO valid target could be resolved
            v_verdict := 'DECLINE';
            v_reason := 'TOWARDS mode active but no target could be resolved (missing market zones or GPS)';
        END IF;

    -- =========================================================================
    -- FREESTYLE / PUDDLE JUMP LOGIC
    -- =========================================================================
    ELSE
        -- SAFETY DEFAULTS
        v_threshold_per_hour := v_global_per_hour;
        v_threshold_per_mile := v_global_per_mile;

        IF NOT is_puddle_jump_mode THEN
            v_return_miles := 0;
            -- Check for manual freestyle overrides first
            IF (v_settings->>'freestyleMinHourly') IS NOT NULL 
               AND (v_settings->>'freestyleMinMileage') IS NOT NULL THEN
                v_threshold_per_hour := (v_settings->>'freestyleMinHourly')::numeric;
                v_threshold_per_mile := (v_settings->>'freestyleMinMileage')::numeric;
                v_threshold_source := 'freestyle_manual';
            -- Freestyle: use location-aware hex pricing cache
            ELSIF current_lat_in IS NOT NULL AND current_lng_in IS NOT NULL THEN
                SELECT f.threshold_hourly, f.threshold_mileage, f.source
                INTO v_threshold_per_hour, v_threshold_per_mile, v_threshold_source
                FROM app_private.get_freestyle_thresholds(
                    current_lat_in, current_lng_in,
                    COALESCE(v_settings->>'freestyleStrategy', 'ai')
                ) f;
            ELSE
                v_threshold_source := 'global';
            END IF;
                   
        ELSIF v_local_green_zones IS NOT NULL AND v_dropoff_hex = ANY(v_local_green_zones) THEN
            -- Stay in market
            v_return_miles := 0;
            v_threshold_source := COALESCE(v_active_market->>'name', 'current_market');

            -- [SHIFT] PRIORITY LOOKUP (Conditional on Auto-Optimize)
            v_threshold_per_hour := COALESCE(
                CASE WHEN v_auto_optimize THEN (v_active_market->'shiftThresholds'->v_shift_id->>'hourly')::numeric ELSE NULL END,
                (v_active_market->>'min_effective_hourly_rate')::numeric,
                v_global_per_hour
            );
            v_threshold_per_mile := COALESCE(
                CASE WHEN v_auto_optimize THEN (v_active_market->'shiftThresholds'->v_shift_id->>'mileage')::numeric ELSE NULL END,
                (v_active_market->>'min_effective_dollar_per_mile')::numeric,
                v_global_per_mile
            );
            IF v_auto_optimize AND v_shift_id IS NOT NULL AND v_active_market->'shiftThresholds'->v_shift_id IS NOT NULL THEN
                v_threshold_source := v_threshold_source || ':shift:' || v_shift_id;
            END IF;
        ELSE
            -- Check for other markets
            FOR v_other_market IN SELECT elem FROM jsonb_array_elements(v_settings->'markets') elem WHERE (elem->>'id') != market_id_in LOOP
                IF EXISTS (SELECT 1 FROM jsonb_array_elements_text(v_other_market->'greenZones') gz WHERE gz = v_dropoff_hex) THEN
                    v_lands_in_other_market := TRUE;
                    v_other_market_name := v_other_market->>'name';
                    v_other_market_id := v_other_market->>'id';
                    v_return_miles := 0;
                    v_threshold_source := 'destination_market:' || v_other_market_name;

                    -- [SHIFT] PRIORITY LOOKUP
                    v_threshold_per_hour := COALESCE(
                        CASE WHEN v_auto_optimize THEN (v_other_market->'shiftThresholds'->v_shift_id->>'hourly')::numeric ELSE NULL END,
                        (v_other_market->>'min_effective_hourly_rate')::numeric,
                        v_global_per_hour
                    );
                    v_threshold_per_mile := COALESCE(
                        CASE WHEN v_auto_optimize THEN (v_other_market->'shiftThresholds'->v_shift_id->>'mileage')::numeric ELSE NULL END,
                        (v_other_market->>'min_effective_dollar_per_mile')::numeric,
                        v_global_per_mile
                    );
                    IF v_auto_optimize AND v_shift_id IS NOT NULL AND v_other_market->'shiftThresholds'->v_shift_id IS NOT NULL THEN
                        v_threshold_source := v_threshold_source || ':shift:' || v_shift_id;
                    END IF;
                    EXIT;
                END IF;
            END LOOP;

            IF NOT v_lands_in_other_market THEN
                -- ================================================================
                -- LEAVING MARKET: FIXED DEADHEAD CALCULATION
                -- ================================================================
                BEGIN
                    SELECT g_hex INTO v_nearest_hex
                    FROM unnest(v_local_green_zones) g_hex
                    ORDER BY h3_grid_distance(v_dropoff_hex::h3index, g_hex::h3index)
                    LIMIT 1;

                    IF v_nearest_hex IS NOT NULL THEN
                        v_nearest_hex_lng := (h3_cell_to_latlng(v_nearest_hex::h3index))[0];
                        v_nearest_hex_lat := (h3_cell_to_latlng(v_nearest_hex::h3index))[1];

                        v_return_miles := (ST_Distance(
                            ST_SetSRID(ST_MakePoint(dropoff_lng_in::float8, dropoff_lat_in::float8), 4326)::geography,
                            ST_SetSRID(ST_MakePoint(v_nearest_hex_lng::float8, v_nearest_hex_lat::float8), 4326)::geography
                        ) / 1609.34) * 1.4;
                    ELSE
                        v_return_miles := 20.0;
                    END IF;
                EXCEPTION WHEN OTHERS THEN
                    v_return_miles := 20.0;
                END;
                -- ================================================================

                IF v_active_market IS NOT NULL THEN
                    v_threshold_source := 'leaving_market:' || COALESCE(v_active_market->>'name', 'current');

                    v_threshold_per_hour := COALESCE(
                        CASE WHEN v_auto_optimize THEN (v_active_market->'shiftThresholds'->v_shift_id->>'hourly')::numeric ELSE NULL END,
                        (v_active_market->>'min_effective_hourly_rate')::numeric,
                        v_global_per_hour
                    );
                    v_threshold_per_mile := COALESCE(
                        CASE WHEN v_auto_optimize THEN (v_active_market->'shiftThresholds'->v_shift_id->>'mileage')::numeric ELSE NULL END,
                        (v_active_market->>'min_effective_dollar_per_mile')::numeric,
                        v_global_per_mile
                    );
                    IF v_auto_optimize AND v_shift_id IS NOT NULL AND v_active_market->'shiftThresholds'->v_shift_id IS NOT NULL THEN
                        v_threshold_source := v_threshold_source || ':shift:' || v_shift_id;
                    END IF;
                END IF;
            END IF;
        END IF;

        -- ================================================================
        -- DEADHEAD COST CALCULATION
        -- ================================================================
        v_return_minutes := v_return_miles * 2.0;

        IF v_deadhead_basis = 'hourly' THEN
            v_calculated_cost := (v_return_minutes / 60.0) * v_threshold_per_hour * v_deadhead_percent;
        ELSE
            v_calculated_cost := v_return_miles * v_cost_per_mile * v_deadhead_percent;
        END IF;
        -- ================================================================

        v_total_time_hr := (trip_minutes_in + COALESCE(pickup_minutes_in, 0) + v_return_minutes) / 60.0;
        v_net_pay := gross_payout_in - v_calculated_cost;
        v_hourly_rate := v_net_pay / NULLIF(v_total_time_hr, 0);
        v_dollars_per_mile := v_net_pay / NULLIF((v_effective_pickup_miles + v_effective_trip_miles + v_return_miles), 0);

        v_pass_hourly := TRUE;
        v_pass_mileage := TRUE;
        IF v_calc_method IN ('per_hour', 'both') AND v_hourly_rate < v_threshold_per_hour THEN v_pass_hourly := FALSE; END IF;
        IF v_calc_method IN ('per_mile', 'both') AND v_dollars_per_mile < v_threshold_per_mile THEN v_pass_mileage := FALSE; END IF;

        IF v_pass_hourly AND v_pass_mileage THEN
            v_verdict := 'ACCEPT';
            IF v_lands_in_other_market THEN v_reason := 'Lands in: ' || v_other_market_name;
            ELSIF v_return_miles = 0 THEN
                IF is_puddle_jump_mode THEN v_reason := 'Stays in market';
                ELSE v_reason := 'Rates met'; END IF;
            ELSE v_reason := 'Rates met'; END IF;
        ELSE
            v_verdict := 'DECLINE';
            IF NOT v_pass_hourly AND NOT v_pass_mileage THEN
                v_reason := format('Both rates too low ($%s/hr, $%s/mi)', round(v_hourly_rate, 2), round(v_dollars_per_mile, 2));
            ELSIF NOT v_pass_hourly THEN
                v_reason := format('Hourly rate too low ($%s/hr)', round(v_hourly_rate, 2));
            ELSE
                v_reason := format('Mileage rate too low ($%s/mi)', round(v_dollars_per_mile, 2));
            END IF;
        END IF;

        IF v_calculated_cost > (gross_payout_in * 0.2) AND v_return_miles > 0 THEN
            v_reason := v_reason || format('. High return cost: $%s', round(v_calculated_cost, 2));
        END IF;
    END IF;

    -- [FINAL STEP] CONSTRUCT THE TRACE DATA
    v_trace_data := jsonb_build_object(
        'tripMiles', round(v_effective_trip_miles, 2),
        'pickupMiles', round(v_effective_pickup_miles, 2),
        'totalWorkMiles', round(v_effective_pickup_miles + v_effective_trip_miles + v_return_miles, 2),
        'returnMiles', round(v_return_miles, 2),
        'calculatedCost', round(v_calculated_cost, 2),
        'grossPayout', gross_payout_in,
        'netProgress', round(v_net_progress, 2),
        'backtrackMiles', round(v_backtrack_miles, 2),
        'efficiencyPct', round(v_efficiency, 2),
        'distanceToTarget', round(v_current_to_target, 2),
        'dropoffToTarget', round(v_dropoff_to_target, 2),
        'targetLat', v_dynamic_target_lat,          
        'targetLng', v_dynamic_target_lng,          
        'targetHex', v_dynamic_target_hex,          
        'targetSource', CASE 
            WHEN v_dynamic_target_hex = 'legacy_target' THEN 'app_passed'
            WHEN v_dynamic_target_hex IS NOT NULL THEN 'nearest_green_hex'
            ELSE 'none'
        END,
        'activeShiftId', v_shift_id,
        'activeShiftName', v_shift_name,
        'towardsEfficiencyThreshold', v_towards_efficiency_threshold,
        'timezone', v_timezone,
        'autoOptimizeEnabled', v_auto_optimize,
        'deadheadBasis', v_deadhead_basis,
        'deadheadPercent', v_deadhead_percent,
        'nearestGreenHex', v_nearest_hex,
        -- *** NEW FIELDS FOR DEBUGGING (ADDED HERE) ***
        'threshold_hourly', v_threshold_per_hour,
        'threshold_mileage', v_threshold_per_mile,
        'source', v_threshold_source,
        'cache_value', v_threshold_per_hour
    );

    RETURN QUERY SELECT v_verdict, v_reason, round(v_net_pay, 2), round(v_hourly_rate, 2), round(v_dollars_per_mile, 2),
                        round(v_return_miles, 1), round(v_calculated_cost, 2), v_arrival_detected, v_switch_to_mode,
                        v_switch_to_market_id, v_threshold_source, v_trace_data;
END;
$function$

