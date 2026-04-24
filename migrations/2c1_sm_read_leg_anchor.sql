-- ============================================================================
-- Migration 2c-1: sm_read exposes leg_start_cumulative_miles
--
-- sm_read has explicit column list in RETURNS TABLE. New columns on
-- driver_trip_state are invisible to callers until sm_read exposes them.
--
-- Adds leg_start_cumulative_miles at the end of the output so existing
-- positional callers are unaffected. Named callers (state_machine.py
-- populates state_row dict from cursor) pick up the new key automatically.
--
-- CREATE OR REPLACE won't work if RETURNS TABLE shape changes — must DROP
-- first, same as sm_transition in Step 2b.
-- ============================================================================

DROP FUNCTION IF EXISTS app_private.sm_read(text);

CREATE OR REPLACE FUNCTION app_private.sm_read(p_driver_id text)
 RETURNS TABLE(
    state                      text,
    current_offer_id           text,
    pickup_lat                 double precision,
    pickup_lng                 double precision,
    pickup_h3                  text,
    dropoff_lat                double precision,
    dropoff_lng                double precision,
    dropoff_h3                 text,
    nailed_pickup_lat          double precision,
    nailed_pickup_lng          double precision,
    nailed_pickup_error_m      double precision,
    nailed_dropoff_lat         double precision,
    nailed_dropoff_lng         double precision,
    nailed_dropoff_error_m     double precision,
    state_updated_at           timestamp with time zone,
    heartbeat                  jsonb,
    heartbeat_at               timestamp with time zone,
    arc_center_lat             double precision,
    arc_center_lng             double precision,
    leg_start_cumulative_miles numeric(6,2)
 )
 LANGUAGE plpgsql
 SECURITY DEFINER
AS $function$
DECLARE
    r app_private.driver_trip_state%ROWTYPE;
BEGIN
    SELECT * INTO r
    FROM app_private.driver_trip_state
    WHERE driver_id = p_driver_id;

    IF NOT FOUND THEN
        RETURN;
    END IF;

    state                      := r.state;
    current_offer_id           := r.current_offer_id;
    pickup_lat                 := r.pickup_lat;
    pickup_lng                 := r.pickup_lng;
    pickup_h3                  := r.pickup_h3;
    dropoff_lat                := r.dropoff_lat;
    dropoff_lng                := r.dropoff_lng;
    dropoff_h3                 := r.dropoff_h3;
    nailed_pickup_lat          := r.nailed_pickup_lat;
    nailed_pickup_lng          := r.nailed_pickup_lng;
    nailed_pickup_error_m      := r.nailed_pickup_error_m;
    nailed_dropoff_lat         := r.nailed_dropoff_lat;
    nailed_dropoff_lng         := r.nailed_dropoff_lng;
    nailed_dropoff_error_m     := r.nailed_dropoff_error_m;
    state_updated_at           := r.state_updated_at;
    heartbeat                  := r.heartbeat;
    heartbeat_at               := r.heartbeat_at;

    -- arc_center: best known pickup location for triangulation
    -- UNCOMMITTED = no active trip, caller uses raw GPS
    IF r.state IN ('ENROUTE', 'IN_TRIP', 'STACKED') THEN
        arc_center_lat := COALESCE(r.nailed_pickup_lat, r.pickup_lat);
        arc_center_lng := COALESCE(r.nailed_pickup_lng, r.pickup_lng);
    ELSE
        arc_center_lat := NULL;
        arc_center_lng := NULL;
    END IF;

    -- Gate 1 (odometer floor) baseline for current leg
    -- Used by check_convergence to compute miles_since_leg_start
    leg_start_cumulative_miles := r.leg_start_cumulative_miles;

    RETURN NEXT;
END;
$function$;

-- Grant EXECUTE — DROP may have revoked
GRANT EXECUTE ON FUNCTION app_private.sm_read(text) TO atjb;