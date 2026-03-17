-- ============================================================================
-- RPC: get_freestyle_preview
-- Description: Fetches all 3 pricing strategy thresholds (Revenue, AI, Picky)
--              for a given location. Used by the Android app to populate the
--              Freestyle settings UI so drivers can see the actual dollar amounts.
-- ============================================================================

CREATE OR REPLACE FUNCTION public.get_freestyle_preview(lat numeric, lng numeric, driver_id text)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
AS $$
DECLARE
    v_rev record;
    v_ai record;
    v_picky record;
BEGIN
    -- Fetch all three strategies using the existing private helper
    SELECT * INTO v_rev FROM app_private.get_freestyle_thresholds(lat, lng, 'revenue', driver_id);
    SELECT * INTO v_ai FROM app_private.get_freestyle_thresholds(lat, lng, 'ai', driver_id);
    SELECT * INTO v_picky FROM app_private.get_freestyle_thresholds(lat, lng, 'picky', driver_id);

    -- Build the JSON response for the Android app
    RETURN jsonb_build_object(
        'revenue', jsonb_build_object('hourly', v_rev.threshold_hourly, 'mileage', v_rev.threshold_mileage),
        'ai', jsonb_build_object('hourly', v_ai.threshold_hourly, 'mileage', v_ai.threshold_mileage),
        'picky', jsonb_build_object('hourly', v_picky.threshold_hourly, 'mileage', v_picky.threshold_mileage),
        'source', v_ai.source,
        'sample_count', v_ai.sample_count
    );
END;
$$;