-- Rollback for 2026-08-18_decision_engine_v3.sql
--
-- v3 ships ALONGSIDE v2 and is selected per-driver via
--   driver_settings_new.settings->>'engine_version' = 'v3'
--
-- FASTEST ROLLBACK (no deploy, no DDL) -- flip the driver back to v2:
--   UPDATE app_private.driver_settings_new
--      SET settings = settings - 'engine_version'
--    WHERE driver_id = '<uid>';
--
-- Full removal (only after no driver references it):
DROP FUNCTION IF EXISTS app_private.decision_engine_v3(
    text, numeric, numeric, numeric, numeric, numeric, numeric, numeric,
    numeric, numeric, text, boolean, numeric, numeric, text,
    numeric, numeric, numeric, boolean);
