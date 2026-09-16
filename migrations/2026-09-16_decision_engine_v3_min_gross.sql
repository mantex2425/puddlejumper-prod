-- 2026-09-16  decision_engine_v3: user-settable minimum on-screen gross $/hr (default $18).
--
-- Based on 2026-09-15_decision_engine_v3_gross_display.sql (live, md5(prosrc) 19d2b6d8).
-- VERDICT CHANGE, approved by Andrew 2026-09-16:
--   - settings.min_gross_hourly (default 18.0, 0 = off) is checked AFTER the DSI test. An
--     offer that clears the DSI bar but shows less than the minimum on screen is declined
--     (decline_class 'threshold', declineCause 'gross_floor'). Either check failing declines.
--   - The gross compared is the cent figure the driver sees: floor(round(ehr, 6) * 100) / 100.
--   - "needs $X" uses X = max(DSI-needed gross, minimum), rounded up, same 50c rule.
--   - A return-leg decline is only called "out of market" if the offer also meets the minimum.
-- Unchanged: the bar ($14 for Andrew), cost per mile ($0.18), formula, gates, mileage floor.
CREATE OR REPLACE FUNCTION app_private.decision_engine_v3(user_id_in text, pickup_lat_in numeric, pickup_lng_in numeric, dropoff_lat_in numeric, dropoff_lng_in numeric, gross_payout_in numeric, trip_miles_in numeric, trip_minutes_in numeric, pickup_minutes_in numeric DEFAULT 0, pickup_miles_in numeric DEFAULT 0, market_id_in text DEFAULT NULL::text, current_lat_in numeric DEFAULT NULL::numeric, current_lng_in numeric DEFAULT NULL::numeric, is_puddle_jump_mode boolean DEFAULT true)
 RETURNS TABLE(verdict text, reason text, hourly_rate numeric, dollars_per_mile numeric, dsi numeric, net_hourly_usd numeric, decline_class text, return_miles numeric, return_minutes numeric, threshold_source text, trace_data jsonb)
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
    -- DISPLAY SCALE. 1.0 means the driver sees real dollars per hour -- the
    -- SAME number the verdict used. No transform, nothing to explain away.
    --
    -- RECOVERABLE BRANDED-INDEX CONSTANT: 2.942
    --   Solved 2026-08-19 on 2,321 genuine offers (fixtures excluded) as
    --   median weighted 24.51 / median net-hourly 8.33, so the median offer's
    --   index reproduces the familiar 'weighted' number. To ship a branded
    --   index instead of dollars, set settings.dsi_display_scale = 2.942.
    --   One config change, no re-derivation. Also recorded in the methodology
    --   doc appendix and as an inert settings key (see below).
    K_DISPLAY_SCALE       CONSTANT numeric := 1.0;
    K_BRANDED_INDEX_SCALE CONSTANT numeric := 2.942;  -- inert; documented above
    -- Minimum on-screen gross $/hr when the driver has not set one (2026-09-16).
    -- Must match bar_tuner.DEFAULT_MIN_GROSS_HOURLY and the new-driver defaults.
    K_DEFAULT_MIN_GROSS   CONSTANT numeric := 18.0;

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
    v_committed_mph       numeric;
    v_hourly_rate         numeric;
    v_dollars_per_mile    numeric;
    v_dsi                 numeric;   -- the value RETURNED, per formula
    v_dsi_formula         text;
    -- The two numbers, deliberately separate variables (see section 0 of the
    -- 2026-08-19 work order). v_score is what the verdict reads; v_dsi_index is
    -- assigned AFTER the verdict and is never read by it.
    v_net_hourly_usd      numeric;   -- honest: real $/hr after vehicle cost
    v_dsi_weighted        numeric;   -- legacy index, always computed for audit
    v_dsi_index           numeric;   -- cosmetic: DISPLAY_SCALE x net_hourly_usd
    v_display_scale       numeric;
    v_score               numeric;   -- the verdict metric
    v_decline_class       text;

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

    v_nearest_hex         text;
    -- What the DRIVER sees (2026-09-15): gross, not DSI. See section 11b.
    v_gross_shown         numeric;   -- gross $/hr of the offer, floored to the cent
    v_needed_gross        numeric;   -- gross $/hr THIS trip needed, rounded up to the cent
    v_net_no_return       numeric;   -- DSI without the modeled return leg
    v_decline_cause       text;      -- 'rate' | 'return_leg' | 'gross_floor' on a threshold decline
    v_gross_floor         numeric;   -- driver's minimum on-screen gross $/hr; 0 = off
    v_gross_check         numeric;   -- the offer's gross $/hr as shown, compared with the floor
    v_gross_text          text;      -- '$27.03/hr'
    v_needs_text          text;      -- 'needs $29.35 on this trip'; NULL when the gap is under 50c
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
            NULL::numeric, NULL::numeric, NULL::numeric,
            NULL::numeric, 'config:no_settings'::text, 0::numeric, 0::numeric,
            'no_settings'::text, '{}'::jsonb;
        RETURN;
    END IF;

    SELECT elem INTO v_active_market
    FROM jsonb_array_elements(v_settings->'markets') elem
    WHERE (elem->>'id') = market_id_in LIMIT 1;

    v_cost_per_mile    := COALESCE((v_settings->>'cost_per_mile')::numeric, K_IRS_RATE_PER_MILE);
    v_max_pickup_miles := COALESCE((v_settings->>'max_pickup_miles')::numeric, 25.0);
    -- Minimum on-screen gross (2026-09-16). A $14 net bar at $0.18/mi lets slow, cheap city
    -- trips through at $16-17/hr on screen; this is a second, independent check in the
    -- driver's own gross terms. Negative or missing values fall back to the default; 0 is off.
    v_gross_floor      := GREATEST(COALESCE((v_settings->>'min_gross_hourly')::numeric, K_DEFAULT_MIN_GROSS), 0);
    v_deadhead_percent := COALESCE((v_settings->>'deadhead_percent')::numeric, 1.0);
    v_red_zones        := v_settings->'redZones';

    -- DSI formula. 'net_hourly' is the product DEFAULT (2026-09-15): DSI is net
    -- dollars per hour, so a driver with no formula set scores on it. 'weighted'
    -- is the retired index, still computed for the trace, reachable only by an
    -- explicit dsi_formula = 'weighted'.
    --   weighted   : hourly + MILE_WEIGHT * (dpm - cost)   -- unitless index
    --   net_hourly : hourly - mph * cost                   -- real $/hr after
    --                (identically  mph * (dpm - cost))        vehicle cost
    -- The weighted form charges miles as though driving MILE_WEIGHT (12) mph.
    -- At real road speeds (28-44 mph observed) that under-charges mileage and
    -- over-rewards fast, long-pickup offers -- a $11.14 offer with a 16.8 mi
    -- pickup scored 2nd best of 7 on 2026-08-18 while netting $5.97/hr, 5th of 7.
    -- net_hourly uses the offer's OWN implied speed and needs no constant.
    --   indexed    : verdict on net_hourly_usd; DISPLAY-ONLY rescale for the UI
    v_dsi_formula := COALESCE(v_settings->>'dsi_formula', 'net_hourly');

    -- DISPLAY_SCALE lifts honest $/hr onto the number range drivers already
    -- trust. It is UX only. It appears nowhere in the verdict path, which is
    -- why it provably cannot move a decision.
    v_display_scale := COALESCE((v_settings->>'dsi_display_scale')::numeric,
                                K_DISPLAY_SCALE);

    -- Driver-set threshold. Its UNITS depend on the formula, so the fallback
    -- must too -- otherwise the mile weight would leak into a verdict it has no
    -- business touching (invariant 5).
    --
    --   weighted            -> threshold is in index points. Derived default
    --                          reproduces v2 strictness: 22.0 + 12*(1.67-0.725).
    --   net_hourly/indexed  -> threshold is in DOLLARS PER HOUR. There is no
    --                          honest way to derive that from the legacy floors,
    --                          and per the 2026-08-19 work order the threshold is
    --                          Andrew's deliberate choice. So it is REQUIRED, and
    --                          a missing one fails CLOSED rather than inventing a
    --                          bar out of a constant from the other formula.
    v_dsi_threshold := (v_settings->>'dsi_threshold')::numeric;

    IF v_dsi_threshold IS NULL THEN
        IF v_dsi_formula = 'weighted' THEN
            v_dsi_threshold :=
                COALESCE((v_settings->>'min_effective_hourly_rate')::numeric, 22.0)
                + K_DSI_MILE_WEIGHT * (
                    COALESCE((v_settings->>'min_effective_dollar_per_mile')::numeric, 1.67)
                    - K_IRS_RATE_PER_MILE
                  );
        ELSE
            RETURN QUERY SELECT 'DECLINE'::text,
                format('dsi_formula=%s requires an explicit dsi_threshold in $/hr', v_dsi_formula)::text,
                NULL::numeric, NULL::numeric, NULL::numeric,
                NULL::numeric, 'config:threshold_not_set'::text, 0::numeric, 0::numeric,
                'threshold_not_set'::text,
                jsonb_build_object('dsiFormula', v_dsi_formula,
                                   'error', 'dsi_threshold missing');
            RETURN;
        END IF;
    END IF;

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
                NULL::numeric, NULL::numeric, NULL::numeric,
                NULL::numeric, 'gate:red_zone'::text, 0::numeric, 0::numeric,
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
            NULL::numeric, NULL::numeric, NULL::numeric,
            NULL::numeric, 'gate:max_pickup'::text, 0::numeric, 0::numeric,
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
    -- ======================================================================
    IF NOT is_puddle_jump_mode THEN
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
    v_committed_mph := v_committed_miles / NULLIF(v_committed_minutes / 60.0, 0);

    -- ---- (A) THE HONEST NUMBER -- computed from primitives, always ----------
    -- net_hourly_usd = ehr - mph*cost = mph*(dpm - cost). Real $/hr in pocket
    -- after vehicle cost. Auditable by hand off the card. Correct at 12 mph and
    -- at 44 mph. No free constant: the speed comes from the offer itself.
    -- NULL-STRICT: any missing primitive yields no score and no verdict.
    IF v_hourly_rate IS NULL OR v_dollars_per_mile IS NULL
       OR v_committed_mph IS NULL OR v_cost_per_mile IS NULL THEN
        v_net_hourly_usd := NULL;
        v_dsi_weighted   := NULL;
    ELSE
        v_net_hourly_usd := v_hourly_rate - v_committed_mph * v_cost_per_mile;
        -- legacy index, computed regardless of active formula so history stays
        -- comparable and nothing is computed then thrown away
        v_dsi_weighted   := v_hourly_rate
                            + K_DSI_MILE_WEIGHT * (v_dollars_per_mile - v_cost_per_mile);
    END IF;

    -- ---- the verdict metric -------------------------------------------------
    -- 'net_hourly' and 'indexed' are the SAME economics; they differ only in
    -- what gets displayed. So both score on net_hourly_usd, and at matched
    -- thresholds they must return identical verdicts.
    IF v_dsi_formula = 'weighted' THEN
        v_score := v_dsi_weighted;
    ELSE
        v_score := v_net_hourly_usd;
    END IF;
    v_dsi := v_score;   -- provisional; 'indexed' overrides the RETURNED value
                        -- below, AFTER the verdict has been decided

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
    --     DISABLED 2026-08-25 at the driver's instruction. Still COMPUTED and
    --     returned, so it remains visible as a diagnostic; it just no longer
    --     moves the verdict.
    --
    --     Why: on 2026-08-24 it multiplied a $12.00 bar to $16.20 (x1.35) and
    --     declined three rides that cleared the bar the driver actually set --
    --     net $13.73, $12.26 and $13.31. Worse, the reasons read "below
    --     threshold 16.2", a number he never chose and could not account for.
    --     A bar the driver did not set, moving invisibly, is worse than no
    --     opportunity-cost model at all.
    --
    --     TO RESTORE: uncomment the single assignment below. Nothing else
    --     about this block changed.
    -- ======================================================================
    BEGIN
        v_pulse_multiplier := COALESCE(
            app_private.get_pulse_multiplier(user_id_in, v_pickup_hex), 1.0);
    EXCEPTION WHEN OTHERS THEN
        v_pulse_multiplier := 1.0;
    END;
    -- v_dsi_threshold := v_dsi_threshold * v_pulse_multiplier;   -- disabled 2026-08-25

    -- ======================================================================
    -- 11b. DRIVER-FACING NUMBERS -- gross $/hr, and the gross this trip needed.
    --      DISPLAY ONLY (2026-09-15). Nothing here is read by the verdict.
    --
    --      Drivers think in gross dollars per hour, so that is what they see; the
    --      verdict stays on DSI. Because DSI = gross $/hr - mph x cost, an offer
    --      clears the bar exactly when
    --          gross $/hr >= bar + committed mph x cost per mile
    --      so that right-hand side is the gross THIS trip needed. It explains the
    --      declines that look backwards on gross alone: a $27.03/hr freeway run
    --      at 48 mph needed $29.35; a $15.45/hr city trip at 17 mph needed $13.74.
    --      Gross is FLOORED and needed is ROUNDED UP, so a decline can never show
    --      a gross at or above what it needed.
    --
    --      A decline that would have cleared the bar without the modeled return
    --      leg is geographic, not a rate problem: "pulls you out of market".
    -- ======================================================================
    -- The gross compared with the floor is the SAME cent figure the driver sees. round(.., 6)
    -- first so a $3.30, 11-minute offer (exactly $18.00/hr) is not floored to $17.99 by
    -- decimal division noise and declined against an $18.00 minimum.
    IF v_hourly_rate IS NOT NULL THEN
        v_gross_check := floor(round(v_hourly_rate, 6) * 100) / 100;
    END IF;

    IF v_dsi_formula <> 'weighted' AND v_hourly_rate IS NOT NULL THEN
        v_gross_shown  := v_gross_check;
        v_gross_text   := '$' || to_char(v_gross_shown, 'FM999990.00') || '/hr';
        IF v_committed_mph IS NOT NULL THEN
            -- What this trip needed on screen to pass BOTH checks: the DSI-needed gross or the
            -- driver's minimum, whichever is higher.
            v_needed_gross := GREATEST(ceil((v_dsi_threshold + v_committed_mph * v_cost_per_mile) * 100) / 100,
                                       v_gross_floor);
        END IF;
        IF v_return_miles > 0 AND (COALESCE(trip_minutes_in, 0) + COALESCE(pickup_minutes_in, 0)) > 0 THEN
            v_net_no_return := (gross_payout_in - (v_eff_pickup_miles + v_eff_trip_miles) * v_cost_per_mile)
                               / ((COALESCE(trip_minutes_in, 0) + COALESCE(pickup_minutes_in, 0)) / 60.0);
        END IF;
    END IF;

    -- ======================================================================
    -- 12. VERDICT
    -- ======================================================================
    IF v_dsi IS NULL THEN
        v_verdict := 'DECLINE';
        v_reason  := 'Incomplete offer data';
        v_threshold_source := 'null_strict';

    ELSE
        -- WORDING ONLY. The three conditions below are unchanged. The reason is
        -- written in GROSS dollars per hour (section 11b); DSI stays in the trace.
        -- A driver on the legacy 'weighted' formula was scored on an index, which
        -- is never shown, so their wording carries no number at all.

        -- PUDDLE_JUMP and FREESTYLE share the identical rule. They differ by
        -- exactly one thing: whether the return leg is in the committed leg.
        IF v_dsi < v_dsi_threshold THEN
            v_verdict := 'DECLINE';
            IF v_gross_text IS NULL THEN
                v_reason := 'Below your bar';
            ELSIF v_net_no_return IS NOT NULL AND v_net_no_return >= v_dsi_threshold
                  AND v_gross_check >= v_gross_floor THEN
                v_decline_cause := 'return_leg';
                v_reason := format('%s — pulls you out of market', v_gross_text);
            ELSE
                v_decline_cause := 'rate';
                -- Only name the needed figure when it is a real gap; within 50c
                -- it adds a number without adding a reason.
                IF v_needed_gross IS NOT NULL AND v_needed_gross - v_gross_shown >= 0.50 THEN
                    v_needs_text := 'needs $' || to_char(v_needed_gross, 'FM999990.00') || ' on this trip';
                    v_reason := format('%s — %s', v_gross_text, v_needs_text);
                ELSE
                    v_reason := v_gross_text;
                END IF;
            END IF;
        ELSIF v_gross_floor > 0 AND v_gross_check IS NOT NULL AND v_gross_check < v_gross_floor THEN
            -- Cleared the DSI bar, but shows less than the driver's minimum on screen.
            v_verdict := 'DECLINE';
            v_decline_cause := 'gross_floor';
            IF v_gross_text IS NULL THEN
                v_reason := 'Below your minimum';
            ELSIF v_needed_gross IS NOT NULL AND v_needed_gross - v_gross_shown >= 0.50 THEN
                v_needs_text := 'needs $' || to_char(v_needed_gross, 'FM999990.00') || ' on this trip';
                v_reason := format('%s — %s', v_gross_text, v_needs_text);
            ELSE
                v_reason := v_gross_text;
            END IF;
        ELSIF v_dollars_per_mile < v_effective_per_mile THEN
            v_verdict := 'DECLINE';
            v_reason  := format('Below mileage floor ($%s/mi, floor $%s/mi)',
                                to_char(floor(v_dollars_per_mile * 100) / 100, 'FM999990.00'),
                                to_char(v_effective_per_mile, 'FM999990.00'));
        ELSE
            v_verdict := 'ACCEPT';
            v_reason  := CASE
                WHEN v_return_source = 'dropoff_in_green' THEN format('%s, stays in market', coalesce(v_gross_text, 'Clears your bar'))
                WHEN v_return_miles > 0 THEN format('%s after %s mi return',
                                                    coalesce(v_gross_text, 'Clears your bar'), round(v_return_miles, 1))
                ELSE coalesce(v_gross_text, 'Clears your bar')
            END;
        END IF;
    END IF;

    -- Classify a decline so the device can render the right thing: a hard
    -- gate gets its reason shown, a threshold miss just shows the number.
    v_decline_class := CASE
        WHEN v_verdict = 'ACCEPT'            THEN NULL
        WHEN v_threshold_source = 'null_strict' THEN 'null_strict'
        -- The mileage floor only runs after the score has CLEARED the bar, so
        -- its net $/hr is at or above the bar. As 'threshold' the device would
        -- show that passing number under a red light. As a gate it shows the
        -- reason instead. Classification only; the verdict is unchanged.
        WHEN v_reason LIKE 'Below mileage floor%' THEN 'gate:mileage_floor'
        ELSE 'threshold'
    END;

    -- ======================================================================
    -- 12b. DISPLAY INDEX -- computed AFTER the verdict, on purpose.
    -- ======================================================================
    -- Everything above this line decided the outcome. Nothing below it can.
    -- dsi_index is a single positive constant times the honest $/hr, so it is
    -- strictly increasing through the origin: same sign, same rank, zero at
    -- break-even, by construction. The verdict never reads it.
    v_dsi_index := v_display_scale * v_net_hourly_usd;

    -- The RETURNED value depends only on presentation preference.
    IF v_dsi_formula = 'indexed' THEN
        v_dsi := v_dsi_index;
    END IF;
    -- ('weighted' and 'net_hourly' keep v_dsi = v_score, set before the verdict.)

    -- ======================================================================
    -- 13. TRACE -- every primitive persisted on EVERY decision, whichever
    --     formula is active, so any past decision stays auditable from the log
    --     and history remains comparable across a formula change.
    -- ======================================================================
    v_trace := jsonb_build_object(
        'engine',              'v3',
        'fare',                gross_payout_in,
        'pickupMi',            round(v_eff_pickup_miles, 2),
        'pickupMin',           COALESCE(pickup_minutes_in, 0),
        'tripMi',              round(v_eff_trip_miles, 2),
        'tripMin',             COALESCE(trip_minutes_in, 0),
        'deadheadMi',          round(v_return_miles, 2),
        'deadheadMin',         round(v_return_minutes, 1),
        'totalMi',             round(v_committed_miles, 2),
        'totalMin',            round(v_committed_minutes, 1),
        'dpm',                 round(v_dollars_per_mile, 4),
        'ehr',                 round(v_hourly_rate, 2),
        'mph',                 round(v_committed_mph, 2),
        'costPerMileUsed',     v_cost_per_mile,
        'netHourlyUsd',        round(v_net_hourly_usd, 2),
        'dsiWeighted',         round(v_dsi_weighted, 2),
        'dsiIndex',            round(v_dsi_index, 2),
        'displayScale',        v_display_scale,
        'scoreUsedForVerdict', round(v_score, 2),
        'thresholdUsed',       round(v_dsi_threshold, 2),
        'grossHourlyShown',    v_gross_shown,
        'neededGrossHourly',   v_needed_gross,
        'needsShown',          (v_needs_text IS NOT NULL),
        'netHourlyNoReturn',   round(v_net_no_return, 2),
        'declineCause',        v_decline_cause,
        'grossFloorUsed',      v_gross_floor
    ) || jsonb_build_object(
        'pickupMiles',         round(v_eff_pickup_miles, 2),
        'tripMiles',           round(v_eff_trip_miles, 2),
        'returnMiles',         round(v_return_miles, 2),
        'returnMinutes',       round(v_return_minutes, 1),
        'returnSource',        v_return_source,
        'committedMiles',      round(v_committed_miles, 2),
        'committedMinutes',    round(v_committed_minutes, 1),
        'committedMph',        round(v_committed_mph, 1),
        'dsiFormula',          v_dsi_formula,
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
        'nearestGreenHex',     v_nearest_hex
    );

    RETURN QUERY SELECT
        v_verdict, v_reason,
        round(v_hourly_rate, 2), round(v_dollars_per_mile, 2), round(v_dsi, 2),
        -- FLOORED, not rounded: the device shows this number next to the verdict,
        -- and round() turned a declined 13.996 into 14.00 against a $14.00 bar.
        floor(v_net_hourly_usd * 100) / 100, v_decline_class,
        round(v_return_miles, 1), round(v_return_minutes, 1),
        v_threshold_source, v_trace;
END;
$function$

