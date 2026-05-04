-- migrations/2026-05-04_demolish_state_machine.sql
--
-- State Machine Demolition — atomic SQL migration.
--
-- Companion: migrations/2026-05-04_verify_demolition.sql (run after this)
-- Runbook:   docs/DEMOLITION_PLAN_2026-05-04.md
-- Reference: docs/RIDE_LIFECYCLE.md
--
-- This script removes the legacy state-machine subsystem from app_private:
--   - 5 triggers on driver_trip_state
--   - 6 functions implementing the state machine
--   - 2 tables (valid_state_transitions, suspected_pudos + 16 partitions)
--   - 2 columns on driver_trip_state (state, state_updated_at)
--   - 1 column rename on pudo_decision_context (state_at_eval -> current_offer_id_at_eval)
--   - 1 CHECK constraint (valid_state)
--
-- Pre-flight requirements (executed BEFORE this script):
--   1. pg_dump of app_private schema saved to /tmp/pre-demolition-app-private-2026-05-04.sql
--   2. Targeted schema-only dump of doomed artifacts saved to /tmp/doomed-artifacts-schema-2026-05-04.sql
--   3. Function source archive captured
--   4. Pre-demolition tag pushed to origin (pre-demolition-2026-05-04)
--   5. Andrew has exclusive system access (no concurrent driver traffic)
--   6. All Step 3 application-code commits landed on demolition-2026-05-04 branch
--   7. Full test suite green on the branch
--
-- Run with:
--   psql -h 10.128.0.2 -U postgres -d puddlejumper -f migrations/2026-05-04_demolish_state_machine.sql
--
-- If anything fails: ROLLBACK is automatic (single transaction).
-- If the lock_timeout fires: re-run when no other session holds driver_trip_state.

-- Per Gemini Q2: fail fast rather than block heartbeats if anything is
-- holding driver_trip_state. With Andrew's exclusive access during the
-- demolition window this should not fire, but defense-in-depth.
SET lock_timeout = '5s';

BEGIN;

-- ─────────────────────────────────────────────────────────────────────────────
-- 1. Drop triggers (so subsequent column drops don't fire them)
-- ─────────────────────────────────────────────────────────────────────────────

DROP TRIGGER IF EXISTS enforce_state_transition_trigger ON app_private.driver_trip_state;
DROP TRIGGER IF EXISTS tr_log_state_transition ON app_private.driver_trip_state;
DROP TRIGGER IF EXISTS tr_set_state_timestamp ON app_private.driver_trip_state;

-- ─────────────────────────────────────────────────────────────────────────────
-- 2. Drop functions (now safely orphaned — no triggers reference them)
-- ─────────────────────────────────────────────────────────────────────────────

DROP FUNCTION IF EXISTS app_private.enforce_state_transition() CASCADE;
DROP FUNCTION IF EXISTS app_private.log_state_transition() CASCADE;
DROP FUNCTION IF EXISTS app_private.set_state_timestamp() CASCADE;
DROP FUNCTION IF EXISTS app_private.sm_find_nearby_offer(text) CASCADE;
DROP FUNCTION IF EXISTS app_private.sm_read(text) CASCADE;
DROP FUNCTION IF EXISTS app_private.sm_transition(text, text, jsonb) CASCADE;

-- ─────────────────────────────────────────────────────────────────────────────
-- 3. Drop tables
--    valid_state_transitions: state-machine rule set, no longer needed
--    suspected_pudos + 16 partitions: deprecated experiment, no longer useful
-- ─────────────────────────────────────────────────────────────────────────────

DROP TABLE IF EXISTS app_private.valid_state_transitions CASCADE;
DROP TABLE IF EXISTS app_private.suspected_pudos CASCADE;  -- partitions cascade

-- ─────────────────────────────────────────────────────────────────────────────
-- 4. driver_trip_state column changes
--    Drop the CHECK constraint first (otherwise the column drop may complain)
--    Then drop the legacy state column and its updated_at companion
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE app_private.driver_trip_state
  DROP CONSTRAINT IF EXISTS valid_state;

ALTER TABLE app_private.driver_trip_state
  DROP COLUMN IF EXISTS state;

ALTER TABLE app_private.driver_trip_state
  DROP COLUMN IF EXISTS state_updated_at;

-- ─────────────────────────────────────────────────────────────────────────────
-- 5. pudo_decision_context column rename
--    Per design decision 4: drop the old column entirely (historical state
--    values are conceptually incompatible with the new 1-bit memory) and
--    add the new column. Existing rows get NULL for current_offer_id_at_eval.
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE app_private.pudo_decision_context
  DROP COLUMN IF EXISTS state_at_eval;

ALTER TABLE app_private.pudo_decision_context
  ADD COLUMN current_offer_id_at_eval text;

-- ─────────────────────────────────────────────────────────────────────────────
-- Commit. Transaction is atomic: either everything dropped or nothing did.
-- ─────────────────────────────────────────────────────────────────────────────

COMMIT;

-- After commit, run migrations/2026-05-04_verify_demolition.sql to confirm
-- the schema state matches expectations before redeploying the application.