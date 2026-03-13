-- ============================================================
-- Driver Timezone Utility Functions
-- Single source of truth for all time calculations.
-- Every function that needs DOW or hour should call these
-- instead of doing its own EXTRACT/AT TIME ZONE.
-- Created: 2026-03-13
-- Bug fixed: offer_history was storing UTC day/hour since launch
-- ============================================================

CREATE OR REPLACE FUNCTION app_private.driver_dow(driver_id_in text, ts timestamptz DEFAULT now())
RETURNS integer AS $$
    SELECT EXTRACT(DOW FROM ts AT TIME ZONE COALESCE(
        (SELECT settings->>'timezone' FROM app_private.driver_settings_new WHERE driver_id = driver_id_in),
        'America/Chicago'))::integer;
$$ LANGUAGE sql STABLE;

CREATE OR REPLACE FUNCTION app_private.driver_hour(driver_id_in text, ts timestamptz DEFAULT now())
RETURNS integer AS $$
    SELECT EXTRACT(HOUR FROM ts AT TIME ZONE COALESCE(
        (SELECT settings->>'timezone' FROM app_private.driver_settings_new WHERE driver_id = driver_id_in),
        'America/Chicago'))::integer;
$$ LANGUAGE sql STABLE;
