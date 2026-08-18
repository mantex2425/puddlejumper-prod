-- ============================================================================
-- app_private.decision_engine_v3
-- ----------------------------------------------------------------------------
-- PuddleJumper 2.0 consolidated decision engine.
--
-- Supersedes decision_engine_v2. Ships ALONGSIDE v2 (v2 is not dropped) so
-- verdicts can be diffed against the 669k-row decision history before cutover.
--
-- WHY v3 EXISTS -- three defects in v2, all fixed here by construction:
--
--   1. DSI was computed in three places with three different rate bases:
--        router.py:657   compute_dsi_v1(card rates)          -> trip-only
--        logger.py:456   compute_dsi_v1(engine rates)        -> full-leg
--        engine.py       compute_personal_dsi(fallback)      -> full-leg
--      The verdict then compared a full-leg Personal DSI against a trip-only
--      Local Market DSI. Apples to oranges, biased toward over-declining.
--      v3 computes rates ONCE and derives DSI ONLY from those rates.
--
--   2. The return leg was charged TWICE -- its minutes/miles inflated both
--      denominators AND its imputed cost was subtracted from the numerator
--      (v_net_pay := gross - v_calculated_cost). v3 puts it in the denominator
--      only, where an unpaid drive actually belongs.
--
--   3. Pulse was inert: line 478 short-circuited on the UNRAISED bar before
--      the pulse-raised threshold was consulted, so any offer good enough to
--      trigger pulse was accepted anyway. v3 applies it to the threshold with
--      no short-circuit and no guard.
--
-- THREE INVARIANTS -- every one of the above is a violation of one of these:
--   I1. Rates are computed EXACTLY ONCE, from the committed leg.
--   I2. DSI is derived ONLY from those rates. Never recomputed, never a second
--       DSI from a different basis.
--   I3. Unpaid segments live in the DENOMINATOR. Never also in the numerator.
--
-- REMOVED FROM v2 (ratified 2026-08-18):
--   * market-rate thresholds (get_market_rate / Price Radar) -> driver threshold
--   * arrival detection + switch_to_mode / switch_to_market_id  (A6)
--   * shifts (time-of-day threshold sets)
--   * calculation_method (per_hour / per_mile / both) -- DSI fuses both
--   * deadhead_basis -- a denominator model needs no either/or
--   * towardsEfficiencyThreshold setting (was dead: overwritten by the ladder)
--   * the separate overshoot block -- now just a non-zero return leg
--
-- RETAINED: mileageFloorConfig, the TOWARDS distance-scaled efficiency ladder,
--   max_pickup_miles gate, red-zone hard decline, GPS x1.3 distance fallback,
--   the 20-mile return fallback, deadhead_percent, x1.4 return circuity.
-- ============================================================================

CREATE OR REPLACE FUNCTION app_private.decision_engine_v3(
    user_id_in                  text,
    pickup_lat_in               numeric,
    pickup_lng_in               numeric,
    dropoff_lat_in              numeric,
    dropoff_lng_in              numeric,
    gross_payout_in             numeric,
    trip_miles_in               numeric,
    trip_minutes_in             numeric,
    pickup_minutes_in           numeric DEFAULT 0,
    pickup_miles_in             numeric DEFAULT 0,
    market_id_in                text    DEFAULT NULL,
    towards_active              boolean DEFAULT false,
    towards_target_lat          numeric DEFAULT NULL,
    towards_target_lng          numeric DEFAULT NULL,
    towards_market_id           text    DEFAULT NULL,
    current_lat_in              numeric DEFAULT NULL,
    current_lng_in              numeric DEFAULT NULL,
    towards_backtrack_tolerance numeric DEFAULT 3.0,
    is_puddle_jump_mode         boolean DEFAULT true
)
RETURNS TABLE(
    verdict          text,
    reason           text,
    hourly_rate      numeric,
    dollars_per_mile numeric,
    dsi              numeric,
    return_miles     numeric,
    return_minutes   numeric,
    threshold_source text,
    trace_data       jsonb
)
LANGUAGE plpgsql
AS $function$
DECLARE
    -- ---- tunables (named, never magic literals) ----------------------------
    K_IRS_RATE_PER_MILE   CONSTANT numeric := 0.725;  -- unified with dsi.py
    K_DSI_MILE_WEIGHT     CONSTANT numeric := 12;     -- unified with dsi.py
    K_RETURN_CIRCUITY     CONSTANT numeric := 1.4;    -- straight-line -> road
    K_RETURN_FALLBACK_MI  CONSTANT numeric := 20.0;   -- zones unresolvable
    K_GPS_DIST_FACTOR     CONSTANT numeric := 1.3;    -- straight-line -> road
    K_MIN_IMPLIED_MPH     CONSTANT numeric := 15.0;   -- clamp floor
    K_MAX_IMPLIED_MPH     CONSTANT numeric := 55.0;   -- clamp ceiling
    K_DEFAULT_IMPLIED_MPH CONSTANT numeric := 30.0;   -- trip time missing

    v_settings            jsonb;
    v_active_market       jsonb;
    v_red_zones           jsonb;
    v_local_green_zones   text[];

    v_cost_per_mile       numeric;
    v_max_pickup_miles    numeric;
    v_deadhead_percent    numeric;
    v_dsi_threshold       numeric;
    v_threshold_source    text := 'driver_threshold';

    v_pickup_hex          text;
    v_dropoff_hex         text;
    v_gps_pickup_dist     numeric := 0;
    v_gps_trip_dist       numeric := 0;
    v_eff_pickup_miles    numeric := 0;
    v_eff_trip_miles      numeric := 0;

    v_implied_mph         numeric;
    v_return_miles        numeric := 0;
    v_return_minutes      numeric := 0;
    v_return_source       text := 'none';

    v_committed_miles     numeric;
    v_committed_minutes   numeric;
    v_hourly_rate         numeric;
    v_dollars_per_mile    numeric;
    v_dsi                 numeric;

    -- mileage floor
    v_mf_config           jsonb;
    v_mf_base_per_mile    numeric;
    v_mf_min_per_mile     numeric;
    v_mf_discount_start   numeric;
    v_mf_discount_per_mi  numeric;
    v_mf_extra_miles      numeric;
    v_mf_discount         numeric;
    v_effective_per_mile  numeric;

    -- pulse
    v_pulse_multiplier    numeric := 1.0;

    -- towards
    v_towards_market      jsonb;
    v_towards_green_zones text[];
    v_target_lat          numeric;
    v_target_lng          numeric;
    v_target_hex          text;
    v_has_target          boolean := FALSE;
    v_current_to_target   numeric;
    v_pickup_to_target    numeric;
    v_dropoff_to_target   numeric;
    v_backtrack_miles     numeric;
    v_net_progress        numeric;
    v_efficiency          numeric;
    v_efficiency_required numeric;
    v_arrival_detected    boolean := FALSE;
    v_target_disk         h3index[];

    v_nearest_hex         text;
    v_verdict             text;
    v_reason              text;
    v_trace               jsonb;
BEGIN
    -- ======================================================================
    -- 0. SETTINGS
    -- ======================================================================
    SELECT settings INTO v_settings
    FROM app_private.driver_settings_new WHERE driver_id = user_id_in;

    IF v_settings IS NULL THEN
        RETURN QUERY SELECT 'DECLINE'::text, 'No driver settings found'::text,
            NULL::numeric, NULL::numeric, NULL::numeric, 0::numeric, 0::numeric,
            'no_settings'::text, '{}'::jsonb;
        RETURN;
    END IF;

    SELECT elem INTO v_active_market
    FROM jsonb_array_elements(v_settings->'markets') elem
    WHERE (elem->>'id') = market_id_in LIMIT 1;

    v_cost_per_mile    := COALESCE((v_settings->>'cost_per_mile')::numeric, K_IRS_RATE_PER_MILE);
    v_max_pickup_miles := COALESCE((v_settings->>'max_pickup_miles')::numeric, 25.0);
    v_deadhead_percent := COALESCE((v_settings->>'deadhead_percent')::numeric, 1.0);
    v_red_zones        := v_settings->'redZones';

    -- Driver-set DSI threshold (spec 5). Default derives from the legacy
    -- floors so v3 starts at exactly v2's strictness:
    --   22.0 + 12 * (1.67 - 0.725) = 33.34
    v_dsi_threshold := COALESCE(
        (v_settings->>'dsi_threshold')::numeric,
        COALESCE((v_settings->>'min_effective_hourly_rate')::numeric, 22.0)
          + K_DSI_MILE_WEIGHT * (
              COALESCE((v_settings->>'min_effective_dollar_per_mile')::numeric, 1.67)
              - K_IRS_RATE_PER_MILE
            )
    );

    IF v_active_market IS NOT NULL THEN
        SELECT array_agg(elem) INTO v_local_green_zones
        FROM jsonb_array_elements_text(v_active_market->'greenZones') elem;
    END IF;

    v_pickup_hex  := app_private.coords_to_h3(pickup_lat_in, pickup_lng_in);
    v_dropoff_hex := app_private.coords_to_h3(dropoff_lat_in, dropoff_lng_in);

    -- ======================================================================
    -- 1. RED ZONE -- hard decline, before any rate work.
    --    Distinct from out-of-market: red zone means "do not send me there
    --    at all", out-of-market means "I will go, but the return costs me".
    -- ======================================================================
    IF v_red_zones IS NOT NULL AND jsonb_array_length(v_red_zones) > 0 THEN
        IF EXISTS (
            SELECT 1 FROM jsonb_array_elements_text(v_red_zones) r(hex)
            WHERE r.hex IN (v_pickup_hex, v_dropoff_hex)
        ) THEN
            RETURN QUERY SELECT 'DECLINE'::text, 'Red Zone'::text,
                NULL::numeric, NULL::numeric, NULL::numeric, 0::numeric, 0::numeric,
                'red_zone'::text,
                jsonb_build_object('pickupHex', v_pickup_hex, 'dropoffHex', v_dropoff_hex);
            RETURN;
        END IF;
    END IF;

    -- ======================================================================
    -- 2. DISTANCES -- trust OCR input; GPS x1.3 only when absent/implausible
    -- ======================================================================
    IF current_lat_in IS NOT NULL AND pickup_lat_in IS NOT NULL THEN
        v_gps_pickup_dist := app_private.distance_miles(
            current_lat_in, current_lng_in, pickup_lat_in, pickup_lng_in);
    END IF;

    v_eff_pickup_miles := pickup_miles_in;
    IF pickup_miles_in IS NULL OR pickup_miles_in < 0.1 THEN
        IF v_gps_pickup_dist > 0.1 THEN
            v_eff_pickup_miles := v_gps_pickup_dist * K_GPS_DIST_FACTOR;
        END IF;
    END IF;

    IF pickup_lat_in IS NOT NULL AND dropoff_lat_in IS NOT NULL THEN
        v_gps_trip_dist := app_private.distance_miles(
            pickup_lat_in, pickup_lng_in, dropoff_lat_in, dropoff_lng_in);
    END IF;

    v_eff_trip_miles := trip_miles_in;
    IF trip_miles_in IS NULL OR trip_miles_in < 0.1 THEN
        IF v_gps_trip_dist > 0.1 THEN
            v_eff_trip_miles := v_gps_trip_dist * K_GPS_DIST_FACTOR;
        END IF;
    END IF;

    -- ======================================================================
    -- 3. MAX PICKUP GATE -- hard decline before rate work
    -- ======================================================================
    IF v_eff_pickup_miles > v_max_pickup_miles THEN
        RETURN QUERY SELECT 'DECLINE'::text,
            format('Pickup too far: %s mi (max %s mi)',
                   round(v_eff_pickup_miles, 1), round(v_max_pickup_miles, 1))::text,
            NULL::numeric, NULL::numeric, NULL::numeric, 0::numeric, 0::numeric,
            'max_pickup'::text,
            jsonb_build_object(
                'effectivePickupMiles', round(v_eff_pickup_miles, 2),
                'maxPickupMiles', v_max_pickup_miles,
                'ocrPickupMiles', pickup_miles_in);
        RETURN;
    END IF;

    -- ======================================================================
    -- 4. IMPLIED SPEED -- derived from the trip itself (ruling 2026-08-18),
    --    clamped. Unclamped, a stop-and-go short trip implies ~2 mph and a
    --    15-mile return becomes 7.5 hours, declining every out-of-market offer.
    -- ======================================================================
    IF trip_minutes_in IS NOT NULL AND trip_minutes_in > 0 AND v_eff_trip_miles > 0 THEN
        v_implied_mph := LEAST(GREATEST(
            v_eff_trip_miles / (trip_minutes_in / 60.0),
            K_MIN_IMPLIED_MPH), K_MAX_IMPLIED_MPH);
    ELSE
        v_implied_mph := K_DEFAULT_IMPLIED_MPH;
    END IF;

    -- ======================================================================
    -- 5. RETURN LEG -- one concept, both modes.
    --      FREESTYLE            -> 0 (no obligation to come back)
    --      dropoff in green     -> 0 (already somewhere good)
    --      PUDDLE_JUMP else     -> distance to nearest green hex x circuity
    --      TOWARDS overshoot    -> handled in the TOWARDS branch below
    -- ======================================================================
    IF towards_active THEN
        v_return_miles  := 0;   -- set in the TOWARDS branch
        v_return_source := 'towards_pending';
    ELSIF NOT is_puddle_jump_mode THEN
        v_return_miles  := 0;
        v_return_source := 'freestyle';
    ELSIF v_local_green_zones IS NOT NULL AND v_dropoff_hex = ANY(v_local_green_zones) THEN
        v_return_miles  := 0;
        v_return_source := 'dropoff_in_green';
    ELSE
        BEGIN
            SELECT g_hex INTO v_nearest_hex
            FROM unnest(v_local_green_zones) g_hex
            ORDER BY h3_grid_distance(v_dropoff_hex::h3index, g_hex::h3index)
            LIMIT 1;

            IF v_nearest_hex IS NOT NULL THEN
                v_return_miles := app_private.distance_miles(
                    dropoff_lat_in, dropoff_lng_in,
                    app_private.h3_to_lat(v_nearest_hex),
                    app_private.h3_to_lng(v_nearest_hex)
                ) * K_RETURN_CIRCUITY;
                v_return_source := 'nearest_green_hex';
            ELSE
                v_return_miles  := K_RETURN_FALLBACK_MI;
                v_return_source := 'fallback_no_green_zones';
            END IF;
        EXCEPTION WHEN OTHERS THEN
            v_return_miles  := K_RETURN_FALLBACK_MI;
            v_return_source := 'fallback_exception';
        END;
        v_return_miles := v_return_miles * v_deadhead_percent;
    END IF;

    -- ======================================================================
    -- 6. TOWARDS -- directional evaluation. Sets the return leg on overshoot,
    --    then falls through to the single rate calc like every other mode.
    -- ======================================================================
    IF towards_active THEN
        -- Dynamic target: nearest green hex of the destination market,
        -- else the app-supplied point. (Arrival detection removed -- A6.)
        IF towards_market_id IS NOT NULL AND current_lat_in IS NOT NULL THEN
            SELECT elem INTO v_towards_market
            FROM jsonb_array_elements(v_settings->'markets') elem
            WHERE (elem->>'id') = towards_market_id LIMIT 1;

            IF v_towards_market IS NOT NULL THEN
                SELECT array_agg(gz) INTO v_towards_green_zones
                FROM jsonb_array_elements_text(v_towards_market->'greenZones') gz;

                IF v_towards_green_zones IS NOT NULL
                   AND array_length(v_towards_green_zones, 1) > 0 THEN
                    BEGIN
                        SELECT g_hex,
                               app_private.h3_to_lat(g_hex),
                               app_private.h3_to_lng(g_hex)
                        INTO v_target_hex, v_target_lat, v_target_lng
                        FROM unnest(v_towards_green_zones) g_hex
                        ORDER BY app_private.distance_miles(
                            current_lat_in, current_lng_in,
                            app_private.h3_to_lat(g_hex),
                            app_private.h3_to_lng(g_hex))
                        LIMIT 1;
                        v_has_target := v_target_lat IS NOT NULL;
                    EXCEPTION WHEN OTHERS THEN
                        v_has_target := FALSE;
                    END;
                END IF;
            END IF;
        END IF;

        IF NOT v_has_target AND towards_target_lat IS NOT NULL THEN
            v_target_lat := towards_target_lat;
            v_target_lng := towards_target_lng;
            v_target_hex := 'app_supplied';
            v_has_target := TRUE;
        END IF;

        IF NOT v_has_target THEN
            RETURN QUERY SELECT 'DECLINE'::text,
                'Towards active but no target could be resolved'::text,
                NULL::numeric, NULL::numeric, NULL::numeric, 0::numeric, 0::numeric,
                'towards_no_target'::text, '{}'::jsonb;
            RETURN;
        END IF;

        v_current_to_target := app_private.distance_miles(
            current_lat_in, current_lng_in, v_target_lat, v_target_lng);
        v_pickup_to_target  := app_private.distance_miles(
            pickup_lat_in, pickup_lng_in, v_target_lat, v_target_lng);
        v_dropoff_to_target := app_private.distance_miles(
            dropoff_lat_in, dropoff_lng_in, v_target_lat, v_target_lng);

        v_backtrack_miles := v_pickup_to_target - v_current_to_target;
        v_net_progress    := v_current_to_target - v_dropoff_to_target;

        -- ARRIVAL DETECTION (ruling 2026-08-18: the detection was sound; only
        -- the auto-switching was broken). Landing in the destination is the
        -- point of the mode, so it short-circuits the directional tests.
        -- What is NOT restored: switch_to_mode / switch_to_market_id.
        IF v_towards_green_zones IS NOT NULL
           AND v_dropoff_hex = ANY(v_towards_green_zones) THEN
            v_arrival_detected := TRUE;
        ELSIF v_target_lat IS NOT NULL THEN
            BEGIN
                SELECT array_agg(hex) INTO v_target_disk
                FROM h3_grid_disk(
                    app_private.coords_to_h3(v_target_lat, v_target_lng)::h3index, 1) hex;
                IF v_dropoff_hex::h3index = ANY(v_target_disk) THEN
                    v_arrival_detected := TRUE;
                END IF;
            EXCEPTION WHEN OTHERS THEN
                v_arrival_detected := FALSE;
            END;
        END IF;

        -- Overshoot IS a return leg: the drive back from past the target.
        IF v_dropoff_to_target > 0.5 AND v_gps_trip_dist > v_pickup_to_target THEN
            v_return_miles  := v_dropoff_to_target * K_RETURN_CIRCUITY * v_deadhead_percent;
            v_return_source := 'towards_overshoot';
        ELSE
            v_return_miles  := 0;
            v_return_source := 'towards_no_overshoot';
        END IF;
    END IF;

    -- ======================================================================
    -- 7. THE COMMITTED LEG -- pickup + trip + return. Unpaid segments live
    --    HERE and nowhere else (I3).
    -- ======================================================================
    v_return_minutes    := (v_return_miles / v_implied_mph) * 60.0;
    v_committed_miles   := v_eff_pickup_miles + v_eff_trip_miles + v_return_miles;
    v_committed_minutes := COALESCE(trip_minutes_in, 0)
                         + COALESCE(pickup_minutes_in, 0)
                         + v_return_minutes;

    -- ======================================================================
    -- 8. RATES, ONCE (I1) -- gross payout, never net. The return leg is
    --    already paid for by the denominator; subtracting a cost as well is
    --    the v2 double-count this engine exists to remove.
    -- ======================================================================
    v_hourly_rate      := gross_payout_in / NULLIF(v_committed_minutes / 60.0, 0);
    v_dollars_per_mile := gross_payout_in / NULLIF(v_committed_miles, 0);

    -- ======================================================================
    -- 9. DSI, ONCE (I2) -- NULL-STRICT: a missing rate yields no score.
    -- ======================================================================
    IF v_hourly_rate IS NULL OR v_dollars_per_mile IS NULL THEN
        v_dsi := NULL;
    ELSE
        v_dsi := v_hourly_rate + K_DSI_MILE_WEIGHT * (v_dollars_per_mile - v_cost_per_mile);
    END IF;

    -- ======================================================================
    -- 10. MILEAGE FLOOR -- a safety floor, not part of the score. Long trips
    --     may accept a lower $/mi, down to a hard minimum.
    -- ======================================================================
    v_mf_config          := v_settings->'mileageFloorConfig';
    v_mf_base_per_mile   := COALESCE((v_mf_config->>'basePerMile')::numeric,
                              COALESCE((v_settings->>'min_effective_dollar_per_mile')::numeric, 1.67));
    v_mf_min_per_mile    := COALESCE((v_mf_config->>'minPerMile')::numeric, 0.35);
    v_mf_discount_start  := COALESCE((v_mf_config->>'discountStartMiles')::numeric, 10.0);
    v_mf_discount_per_mi := COALESCE((v_mf_config->>'discountPerExtraMile')::numeric, 0.015);

    v_mf_extra_miles     := GREATEST(v_eff_trip_miles - v_mf_discount_start, 0);
    v_mf_discount        := v_mf_extra_miles * v_mf_discount_per_mi;
    v_effective_per_mile := GREATEST(v_mf_base_per_mile - v_mf_discount, v_mf_min_per_mile);

    -- ======================================================================
    -- 11. PULSE -- opportunity cost from the driver's own offer cadence.
    --     Applied straight to the threshold: no guard, no short-circuit.
    --     (v2 had both, which made it inert under calculation_method='both'.)
    -- ======================================================================
    BEGIN
        v_pulse_multiplier := COALESCE(
            app_private.get_pulse_multiplier(user_id_in, v_pickup_hex), 1.0);
    EXCEPTION WHEN OTHERS THEN
        v_pulse_multiplier := 1.0;
    END;
    v_dsi_threshold := v_dsi_threshold * v_pulse_multiplier;

    -- ======================================================================
    -- 12. VERDICT
    -- ======================================================================
    IF v_dsi IS NULL THEN
        v_verdict := 'DECLINE';
        v_reason  := 'Incomplete offer data';
        v_threshold_source := 'null_strict';

    ELSIF towards_active THEN
        -- Distance-scaled efficiency ladder: the further out you are, the
        -- less each hop must gain. (Replaces the dead towardsEfficiencyThreshold.)
        v_efficiency := CASE WHEN v_committed_miles > 0
                             THEN v_net_progress / v_committed_miles ELSE 1.0 END;
        v_efficiency_required := CASE
            WHEN v_current_to_target >= 200 THEN 0.15
            WHEN v_current_to_target >= 100 THEN 0.20
            WHEN v_current_to_target >= 50  THEN 0.30
            WHEN v_current_to_target >= 20  THEN 0.35
            WHEN v_current_to_target >= 10  THEN 0.40
            WHEN v_current_to_target >= 5   THEN 0.50
            ELSE 0.65
        END;
        v_threshold_source := 'towards';

        IF v_arrival_detected THEN
            -- Arriving IS the objective. Accepted regardless of DSI: the ride
            -- takes you where you were driving anyway.
            v_verdict := 'ACCEPT';
            v_reason  := format('Arrived in target (%s mi progress)', round(v_net_progress, 1));
        ELSIF v_net_progress <= 0 THEN
            v_verdict := 'DECLINE';
            v_reason  := format('Moves away from target ($%s/hr, $%s/mi)',
                                round(v_hourly_rate, 2), round(v_dollars_per_mile, 2));
        ELSIF v_backtrack_miles > towards_backtrack_tolerance THEN
            v_verdict := 'DECLINE';
            v_reason  := format('Backtrack %s mi exceeds tolerance %s mi',
                                round(v_backtrack_miles, 1), round(towards_backtrack_tolerance, 1));
        ELSIF v_efficiency < v_efficiency_required THEN
            v_verdict := 'DECLINE';
            v_reason  := format('Inefficient: %s%% vs %s%% required at %s mi out',
                                round(v_efficiency * 100, 0),
                                round(v_efficiency_required * 100, 0),
                                round(v_current_to_target, 1));
        ELSIF v_dsi < v_dsi_threshold THEN
            v_verdict := 'DECLINE';
            v_reason  := format('Towards, but DSI %s below %s',
                                round(v_dsi, 1), round(v_dsi_threshold, 1));
        ELSE
            v_verdict := 'ACCEPT';
            v_reason  := format('%s mi progress toward target', round(v_net_progress, 1));
        END IF;

    ELSE
        -- PUDDLE_JUMP and FREESTYLE share the identical rule. They differ by
        -- exactly one thing: whether the return leg is in the committed leg.
        IF v_dsi < v_dsi_threshold THEN
            v_verdict := 'DECLINE';
            v_reason  := format('DSI %s below threshold %s ($%s/hr, $%s/mi)',
                                round(v_dsi, 1), round(v_dsi_threshold, 1),
                                round(v_hourly_rate, 2), round(v_dollars_per_mile, 2));
        ELSIF v_dollars_per_mile < v_effective_per_mile THEN
            v_verdict := 'DECLINE';
            v_reason  := format('Below mileage floor ($%s/mi, floor $%s/mi)',
                                round(v_dollars_per_mile, 2), round(v_effective_per_mile, 2));
        ELSE
            v_verdict := 'ACCEPT';
            v_reason  := CASE
                WHEN v_return_source = 'dropoff_in_green' THEN 'Stays in market'
                WHEN v_return_miles > 0 THEN format('DSI %s after %s mi return',
                                                    round(v_dsi, 1), round(v_return_miles, 1))
                ELSE format('DSI %s', round(v_dsi, 1))
            END;
        END IF;
    END IF;

    -- ======================================================================
    -- 13. TRACE
    -- ======================================================================
    v_trace := jsonb_build_object(
        'engine',              'v3',
        'pickupMiles',         round(v_eff_pickup_miles, 2),
        'tripMiles',           round(v_eff_trip_miles, 2),
        'returnMiles',         round(v_return_miles, 2),
        'returnMinutes',       round(v_return_minutes, 1),
        'returnSource',        v_return_source,
        'committedMiles',      round(v_committed_miles, 2),
        'committedMinutes',    round(v_committed_minutes, 1),
        'impliedMph',          round(v_implied_mph, 1),
        'impliedMphClamped',   (v_implied_mph IN (K_MIN_IMPLIED_MPH, K_MAX_IMPLIED_MPH)),
        'grossPayout',         gross_payout_in,
        'hourlyRate',          round(v_hourly_rate, 2),
        'dollarsPerMile',      round(v_dollars_per_mile, 2),
        'dsi',                 round(v_dsi, 2),
        'dsiThreshold',        round(v_dsi_threshold, 2),
        'costPerMile',         v_cost_per_mile,
        'deadheadPercent',     v_deadhead_percent,
        'pulseMultiplier',     v_pulse_multiplier,
        'effectivePerMile',    round(v_effective_per_mile, 4),
        'mileageDiscount',     round(v_mf_discount, 4),
        'maxPickupMiles',      v_max_pickup_miles,
        'gpsPickupDist',       round(v_gps_pickup_dist, 2),
        'gpsTripDist',         round(v_gps_trip_dist, 2),
        'ocrPickupMiles',      pickup_miles_in,
        'ocrTripMiles',        trip_miles_in,
        'pickupHex',           v_pickup_hex,
        'dropoffHex',          v_dropoff_hex,
        'nearestGreenHex',     v_nearest_hex,
        'towardsActive',       towards_active,
        'towardsTargetHex',    v_target_hex,
        'arrivalDetected',     v_arrival_detected,
        'netProgress',         round(v_net_progress, 2),
        'backtrackMiles',      round(v_backtrack_miles, 2),
        'efficiency',          round(v_efficiency, 3),
        'efficiencyRequired',  v_efficiency_required
    );

    RETURN QUERY SELECT
        v_verdict, v_reason,
        round(v_hourly_rate, 2), round(v_dollars_per_mile, 2), round(v_dsi, 2),
        round(v_return_miles, 1), round(v_return_minutes, 1),
        v_threshold_source, v_trace;
END;
$function$;
