-- ============================================================================
-- Phase A Rollback
-- ----------------------------------------------------------------------------
-- Undoes 2026_04_25_phase_a_where_am_i_foundation.sql
--
-- Apply:
--   psql -h 10.128.0.2 -U postgres -d puddlejumper -v ON_ERROR_STOP=1 -X \
--        -f ~/puddlejumper-prod/migrations/2026_04_25_phase_a_where_am_i_foundation.rollback.sql
-- ============================================================================

BEGIN;

-- 6. Janitor function
DROP FUNCTION IF EXISTS app_private.suspected_pudos_janitor();

-- 3-5. Suspected pudos (CASCADE drops 16 partitions + all indexes)
DROP TABLE IF EXISTS app_private.suspected_pudos CASCADE;

-- 2. Stop config
DROP TABLE IF EXISTS routing.known_stops_config;

-- 1. Feature flags — drop the seeded row but KEEP the table (it's generic
--    infrastructure that other features may use once it exists).
DELETE FROM app_private.feature_flags
 WHERE flag_name = 'WAI_PLANNER_ENABLED_DRIVERS';

-- If feature_flags is empty (no other features adopted it yet), uncomment to drop:
-- DROP TABLE IF EXISTS app_private.feature_flags;

COMMIT;