-- ============================================================================
-- Migration 2b: sm_transition + AAR (Uber estimate accuracy)
--
-- Adds p_cumulative_miles at position 17 (DEFAULT NULL for backward compat).
-- Writes odometer anchors into offer_history at state transitions:
--   - offer_accepted      → leg_start_cumulative_miles_pickup
--   - pickup_confirmed    → cumulative_miles_at_pickup_fire,
--                            leg_start_cumulative_miles_dropoff
--   - gps_convergence     → same as pickup_confirmed
--   - dropoff_confirmed*  → cumulative_miles_at_dropoff_fire
--
-- driver_trip_state.leg_start_cumulative_miles tracks the CURRENT leg's
-- baseline for gate 1 evaluation at heartbeat-time.
--
-- Preserves ALL existing behavior including:
--   - Patch 00566a Step 11.5 refined_dropoff_* logic
--   - current_offer_id live-pointer semantics
--   - potential_cancellation clear-on-UNCOMMITTED
--   - p_clear_coords full-slate-wipe
--   - replay_harness trigger handling
-- ============================================================================

CREATE OR REPLACE FUNCTION app_private.sm_transition(
    p_driver_id               text,
    p_trigger                 text,
    p_offer_id                text DEFAULT NULL::text,
    p_pickup_lat              double precision DEFAULT NULL::double precision,
    p_pickup_lng              double precision DEFAULT NULL::double precision,
    p_pickup_h3               text DEFAULT NULL::text,
    p_dropoff_lat             double precision DEFAULT NULL::double precision,
    p_dropoff_lng             double precision DEFAULT NULL::double precision,
    p_dropoff_h3              text DEFAULT NULL::text,
    p_nailed_pickup_lat       double precision DEFAULT NULL::double precision,
    p_nailed_pickup_lng       double precision DEFAULT NULL::double precision,
    p_nailed_pickup_error_m   double precision DEFAULT NULL::double precision,
    p_nailed_dropoff_lat      double precision DEFAULT NULL::double precision,
    p_nailed_dropoff_lng      double precision DEFAULT NULL::double precision,
    p_nailed_dropoff_error_m  double precision DEFAULT NULL::double precision,
    p_clear_coords            boolean DEFAULT false,
    p_cumulative_miles        double precision DEFAULT NULL::double precision
)
 RETURNS TABLE(success boolean, from_state text, to_state text, message text)
 LANGUAGE plpgsql
AS $function$
DECLARE
    v_current_state TEXT;
    v_new_state     TEXT;
    v_offer_id      INTEGER;
BEGIN
    SELECT state, current_offer_id INTO v_current_state, v_offer_id
    FROM app_private.driver_trip_state
    WHERE driver_id = p_driver_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN QUERY SELECT FALSE, NULL::TEXT, NULL::TEXT, 'Driver not found';
        RETURN;
    END IF;

    IF p_trigger = 'replay_harness' THEN
        v_new_state := COALESCE(p_pickup_h3, v_current_state);
    ELSE
        SELECT vst.to_state INTO v_new_state
        FROM app_private.valid_state_transitions vst
        WHERE vst.from_state = v_current_state
          AND vst.trigger    = p_trigger
        LIMIT 1;
    END IF;

    IF v_new_state IS NULL THEN
        RETURN QUERY SELECT FALSE, v_current_state, v_current_state,
            format('Invalid transition: %s → trigger=%s', v_current_state, p_trigger);
        RETURN;
    END IF;

    -- ------------------------------------------------------------------------
    -- AAR instrumentation gap warning
    -- Fires when a leg-relevant trigger arrives without p_cumulative_miles.
    -- Not an error — backward-compat; but logs a warning so we can find
    -- callers that haven't been plumbed yet.
    -- ------------------------------------------------------------------------
    IF p_cumulative_miles IS NULL AND p_trigger IN (
        'offer_accepted', 'gps_convergence', 'pickup_confirmed',
        'dropoff_confirmed', 'dropoff_confirmed_retroactive'
    ) THEN
        RAISE WARNING '[AAR_GAP] sm_transition called without p_cumulative_miles for trigger=%, driver=%',
            p_trigger, p_driver_id;
    END IF;

    -- ------------------------------------------------------------------------
    -- offer_history side-effects (AAR anchors)
    -- Must happen BEFORE the driver_trip_state UPDATE so v_offer_id is
    -- still the pre-transition value (important for dropoff_confirmed
    -- when the STACKED→ENROUTE swap is about to change current_offer_id).
    -- ------------------------------------------------------------------------

    -- 1. Pickup leg baseline — set when a new offer is accepted.
    --    p_offer_id (TEXT) here is the NEW offer, before we write it
    --    to driver_trip_state.current_offer_id. v_offer_id is the prior.
    IF p_trigger = 'offer_accepted'
       AND p_offer_id IS NOT NULL
       AND p_cumulative_miles IS NOT NULL
    THEN
        UPDATE app_private.offer_history
        SET leg_start_cumulative_miles_pickup = p_cumulative_miles
        WHERE decision_log_id = p_offer_id::integer;
    END IF;

    -- 2. Pickup fire + dropoff leg baseline — set on pickup confirmation.
    --    v_offer_id is the CURRENT active offer (the one that just got picked up).
    IF p_trigger IN ('gps_convergence', 'pickup_confirmed')
       AND v_offer_id IS NOT NULL
       AND p_cumulative_miles IS NOT NULL
    THEN
        UPDATE app_private.offer_history
        SET cumulative_miles_at_pickup_fire = p_cumulative_miles,
            leg_start_cumulative_miles_dropoff = p_cumulative_miles
        WHERE decision_log_id = v_offer_id;
    END IF;

    -- 3. Dropoff fire — set on dropoff confirmation (solo or retroactive DTF).
    --    v_offer_id is the offer whose dropoff just fired. For STACKED swap,
    --    this is the PRIMARY (about-to-complete) offer, not the secondary.
    IF p_trigger IN ('dropoff_confirmed', 'dropoff_confirmed_retroactive')
       AND v_offer_id IS NOT NULL
       AND p_cumulative_miles IS NOT NULL
    THEN
        UPDATE app_private.offer_history
        SET cumulative_miles_at_dropoff_fire = p_cumulative_miles
        WHERE decision_log_id = v_offer_id;
    END IF;

    PERFORM set_config('app.state_trigger', p_trigger, true);
    PERFORM set_config('puddle.current_offer_id', COALESCE(v_offer_id::TEXT, ''), true);

    UPDATE app_private.driver_trip_state SET
        state            = v_new_state,

        pickup_lat   = CASE WHEN p_clear_coords THEN NULL WHEN p_pickup_lat  IS NOT NULL THEN p_pickup_lat  ELSE pickup_lat  END,
        pickup_lng   = CASE WHEN p_clear_coords THEN NULL WHEN p_pickup_lng  IS NOT NULL THEN p_pickup_lng  ELSE pickup_lng  END,
        pickup_h3    = CASE WHEN p_clear_coords THEN NULL WHEN p_pickup_h3   IS NOT NULL THEN p_pickup_h3   ELSE pickup_h3   END,
        dropoff_lat  = CASE WHEN p_clear_coords THEN NULL WHEN p_dropoff_lat IS NOT NULL THEN p_dropoff_lat ELSE dropoff_lat END,
        dropoff_lng  = CASE WHEN p_clear_coords THEN NULL WHEN p_dropoff_lng IS NOT NULL THEN p_dropoff_lng ELSE dropoff_lng END,
        dropoff_h3   = CASE WHEN p_clear_coords THEN NULL WHEN p_dropoff_h3  IS NOT NULL THEN p_dropoff_h3  ELSE dropoff_h3  END,

        nailed_pickup_lat      = CASE WHEN p_clear_coords THEN NULL WHEN p_nailed_pickup_lat      IS NOT NULL THEN p_nailed_pickup_lat      ELSE nailed_pickup_lat      END,
        nailed_pickup_lng      = CASE WHEN p_clear_coords THEN NULL WHEN p_nailed_pickup_lng      IS NOT NULL THEN p_nailed_pickup_lng      ELSE nailed_pickup_lng      END,
        nailed_pickup_error_m  = CASE WHEN p_clear_coords THEN NULL WHEN p_nailed_pickup_error_m  IS NOT NULL THEN p_nailed_pickup_error_m  ELSE nailed_pickup_error_m  END,
        nailed_dropoff_lat     = CASE WHEN p_clear_coords THEN NULL WHEN p_nailed_dropoff_lat     IS NOT NULL THEN p_nailed_dropoff_lat     ELSE nailed_dropoff_lat     END,
        nailed_dropoff_lng     = CASE WHEN p_clear_coords THEN NULL WHEN p_nailed_dropoff_lng     IS NOT NULL THEN p_nailed_dropoff_lng     ELSE nailed_dropoff_lng     END,
        nailed_dropoff_error_m = CASE WHEN p_clear_coords THEN NULL WHEN p_nailed_dropoff_error_m IS NOT NULL THEN p_nailed_dropoff_error_m ELSE nailed_dropoff_error_m END,

        -- Patch 00566a Step 11.5 (Option B+ with IS DISTINCT FROM):
        -- refined_dropoff_* and refinement_source are DERIVED attributes of
        -- the current dropoff estimate. They become stale the moment the
        -- primary dropoff estimate actually changes. Clear them on:
        --   (a) p_clear_coords=TRUE (trip-end, full slate wipe), OR
        --   (b) a genuinely new p_dropoff_lat is being loaded
        --       (IS DISTINCT FROM handles NULL safely; preserves refinement
        --        across no-op preserve-same-value transitions like
        --        approaching_dropoff that re-pass the existing dropoff_lat).
        refined_dropoff_lat    = CASE
            WHEN p_clear_coords                                                              THEN NULL
            WHEN p_dropoff_lat IS NOT NULL AND p_dropoff_lat IS DISTINCT FROM dropoff_lat    THEN NULL
            ELSE refined_dropoff_lat
        END,
        refined_dropoff_lng    = CASE
            WHEN p_clear_coords                                                              THEN NULL
            WHEN p_dropoff_lng IS NOT NULL AND p_dropoff_lng IS DISTINCT FROM dropoff_lng    THEN NULL
            ELSE refined_dropoff_lng
        END,
        refinement_source      = CASE
            WHEN p_clear_coords                                                              THEN NULL
            WHEN p_dropoff_lat IS NOT NULL AND p_dropoff_lat IS DISTINCT FROM dropoff_lat    THEN NULL
            ELSE refinement_source
        END,

        current_offer_id = CASE WHEN p_clear_coords THEN NULL WHEN p_offer_id IS NOT NULL THEN p_offer_id ELSE current_offer_id END,

        potential_cancellation = CASE WHEN v_new_state = 'UNCOMMITTED' THEN FALSE ELSE potential_cancellation END,

        -- Leg-anchor tracking for gate 1 (odometer floor) evaluation.
        -- Order matters — CASE evaluates top-to-bottom, first match wins.
        --   1. Hard resets (trip-end, manual/watchdog reset) → 0
        --   2. Solo dropoff going UNCOMMITTED → 0 (no next leg)
        --   3. Leg-starting transitions → current odometer
        --      (INCLUDES STACKED→ENROUTE via dropoff_confirmed where the
        --       secondary ride's pickup leg begins NOW)
        --   4. Otherwise preserve existing value.
        leg_start_cumulative_miles = CASE
            WHEN p_clear_coords
                 OR p_trigger IN ('manual_reset', 'watchdog_auto_reset')
            THEN 0.0

            WHEN p_trigger IN ('dropoff_confirmed', 'dropoff_confirmed_retroactive')
                 AND v_new_state = 'UNCOMMITTED'
            THEN 0.0

            WHEN p_trigger IN ('offer_accepted', 'gps_convergence', 'pickup_confirmed',
                               'dropoff_confirmed', 'dropoff_confirmed_retroactive')
                 AND p_cumulative_miles IS NOT NULL
            THEN p_cumulative_miles

            ELSE leg_start_cumulative_miles
        END

    WHERE driver_id = p_driver_id;

    RETURN QUERY SELECT TRUE, v_current_state, v_new_state, NULL::TEXT;
END;
$function$;