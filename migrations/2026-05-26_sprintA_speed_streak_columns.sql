-- =============================================================================
-- Migration: app_private.driver_trip_state speed-streak columns
-- Purpose:   §XVI.G Transaction Lock temporal release-condition substrate
-- Authors:   Andrew + Claude (paired-programming) + Gemini (Sprint A ratified)
-- Date:      2026-05-26
-- Sprint:    A Step 3.2a (matcher integration prerequisite)
-- =============================================================================
--
-- Background
-- ----------
-- §XVI.G's temporal release condition requires tracking contiguous seconds
-- above LOCK_RELEASE_SPEED_MPH (5.0 mph). The arrest counter on
-- driver_trip_state tracks the OPPOSITE condition (contiguous seconds at
-- zero velocity) using two columns:
--   arrest_started_at TIMESTAMPTZ
--   arrest_counter_s  REAL
-- This migration mirrors that storage pattern for the speed-streak signal.
--
-- Design decisions
-- ----------------
-- A. Schema column pattern: paired (started_at, counter_s) per existing
--    arrest convention (§XVI.B-2). Matcher computes counter via
--    EXTRACT(EPOCH FROM (NOW() - started_at))::real at UPDATE time.
-- B. NULL semantics: speed_streak_started_at IS NULL means "speed currently
--    at or below threshold" (no streak in progress). current_speed_streak_s
--    is 0.0 in that state.
-- C. Existing rows: backfilled to NULL/0.0 (no streak). The matcher's
--    UPDATE on the next heartbeat will start tracking from current state.
--
-- Idempotency
-- -----------
-- Both ADD COLUMN statements use IF NOT EXISTS. Re-running this script
-- produces no changes when the columns already exist.
--
-- Verification
-- ------------
-- After applying, expect both columns present, both nullable, both with
-- default values matching the "no streak in progress" semantics.
-- =============================================================================

BEGIN;

ALTER TABLE app_private.driver_trip_state
    ADD COLUMN IF NOT EXISTS speed_streak_started_at TIMESTAMPTZ;

ALTER TABLE app_private.driver_trip_state
    ADD COLUMN IF NOT EXISTS current_speed_streak_s REAL NOT NULL DEFAULT 0.0;

COMMENT ON COLUMN app_private.driver_trip_state.speed_streak_started_at IS
'§XVI.G Transaction Lock temporal release condition: when did the driver '
'last cross from speed_mph <= LOCK_RELEASE_SPEED_MPH (5.0) up to speed_mph > 5.0. '
'NULL when the driver is currently at or below the threshold. Mirrors the '
'arrest_started_at pattern but for the inverse velocity gate.';

COMMENT ON COLUMN app_private.driver_trip_state.current_speed_streak_s IS
'§XVI.G Transaction Lock temporal release condition: derived contiguous '
'seconds above LOCK_RELEASE_SPEED_MPH (5.0 mph), computed at heartbeat-UPDATE '
'time as EXTRACT(EPOCH FROM (NOW() - speed_streak_started_at))::real or 0.0 '
'when speed_streak_started_at IS NULL. Mirrors the arrest_counter_s pattern.';

COMMIT;

-- =============================================================================
-- Post-migration verification (read-only, idempotent)
-- =============================================================================

\echo
\echo === V1: New columns present ===
SELECT column_name, data_type, is_nullable, column_default
FROM   information_schema.columns
WHERE  table_schema = 'app_private'
AND    table_name = 'driver_trip_state'
AND    column_name IN ('speed_streak_started_at', 'current_speed_streak_s')
ORDER  BY column_name;

\echo
\echo === V2: Comments populated ===
SELECT col_description(
    ('app_private.driver_trip_state'::regclass)::oid,
    ordinal_position
) AS comment_text,
       column_name
FROM information_schema.columns
WHERE table_schema = 'app_private'
AND   table_name = 'driver_trip_state'
AND   column_name IN ('speed_streak_started_at', 'current_speed_streak_s')
ORDER BY column_name;

\echo
\echo === V3: Backfill state (expect all NULL/0.0) ===
SELECT
    COUNT(*) AS total_drivers,
    COUNT(*) FILTER (WHERE speed_streak_started_at IS NOT NULL) AS in_progress_streaks,
    SUM(current_speed_streak_s) AS sum_counter_s
FROM app_private.driver_trip_state;
