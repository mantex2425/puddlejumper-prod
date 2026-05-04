-- migrations/2026-05-04_verify_demolition.sql
--
-- Post-demolition verification — assert the state-machine subsystem is gone
-- and the new artifacts are present.
--
-- Companion: migrations/2026-05-04_demolish_state_machine.sql
-- Runbook:   docs/DEMOLITION_PLAN_2026-05-04.md
--
-- Run AFTER the demolition migration commits:
--   psql -h 10.128.0.2 -U postgres -d puddlejumper -f migrations/2026-05-04_verify_demolition.sql
--
-- Each block raises NOTICE on success and RAISE EXCEPTION on failure.
-- Failure means the demolition did not complete cleanly; do not redeploy
-- application code until investigated.

\echo '═══════════════════════════════════════════════════════════════════════'
\echo ' STATE MACHINE DEMOLITION — POST-MIGRATION VERIFICATION'
\echo ' Generated: 2026-05-04'
\echo '═══════════════════════════════════════════════════════════════════════'

-- ─────────────────────────────────────────────────────────────────────────────
-- 1. State-machine functions: must be 0
-- ─────────────────────────────────────────────────────────────────────────────

DO $$
DECLARE
  fn_count int;
  fn_names text;
BEGIN
  SELECT COUNT(*), string_agg(routine_name, ', ' ORDER BY routine_name)
  INTO fn_count, fn_names
  FROM information_schema.routines
  WHERE routine_schema = 'app_private'
    AND (routine_name LIKE 'sm_%'
         OR routine_name IN ('enforce_state_transition',
                             'log_state_transition',
                             'set_state_timestamp'));

  IF fn_count = 0 THEN
    RAISE NOTICE 'OK  : state-machine functions removed (count=0)';
  ELSE
    RAISE EXCEPTION 'FAIL: % state-machine function(s) still present: %',
      fn_count, fn_names;
  END IF;
END $$;

-- ─────────────────────────────────────────────────────────────────────────────
-- 2. Triggers on driver_trip_state: must be 0
-- ─────────────────────────────────────────────────────────────────────────────

DO $$
DECLARE
  trig_count int;
  trig_names text;
BEGIN
  SELECT COUNT(*), string_agg(DISTINCT trigger_name, ', ' ORDER BY trigger_name)
  INTO trig_count, trig_names
  FROM information_schema.triggers
  WHERE event_object_schema = 'app_private'
    AND event_object_table = 'driver_trip_state';

  IF trig_count = 0 THEN
    RAISE NOTICE 'OK  : triggers on driver_trip_state removed (count=0)';
  ELSE
    RAISE EXCEPTION 'FAIL: % trigger(s) still present on driver_trip_state: %',
      trig_count, trig_names;
  END IF;
END $$;

-- ─────────────────────────────────────────────────────────────────────────────
-- 3. driver_trip_state legacy columns: must be 0
-- ─────────────────────────────────────────────────────────────────────────────

DO $$
DECLARE
  col_count int;
  col_names text;
BEGIN
  SELECT COUNT(*), string_agg(column_name, ', ' ORDER BY column_name)
  INTO col_count, col_names
  FROM information_schema.columns
  WHERE table_schema = 'app_private'
    AND table_name = 'driver_trip_state'
    AND column_name IN ('state', 'state_updated_at');

  IF col_count = 0 THEN
    RAISE NOTICE 'OK  : driver_trip_state.state and state_updated_at removed';
  ELSE
    RAISE EXCEPTION 'FAIL: legacy columns still on driver_trip_state: %', col_names;
  END IF;
END $$;

-- ─────────────────────────────────────────────────────────────────────────────
-- 4. driver_trip_state CHECK constraint: must be gone
-- ─────────────────────────────────────────────────────────────────────────────

DO $$
DECLARE
  con_count int;
BEGIN
  SELECT COUNT(*) INTO con_count
  FROM information_schema.table_constraints
  WHERE table_schema = 'app_private'
    AND table_name = 'driver_trip_state'
    AND constraint_name = 'valid_state';

  IF con_count = 0 THEN
    RAISE NOTICE 'OK  : valid_state CHECK constraint removed';
  ELSE
    RAISE EXCEPTION 'FAIL: valid_state CHECK constraint still present';
  END IF;
END $$;

-- ─────────────────────────────────────────────────────────────────────────────
-- 5. valid_state_transitions table: must be gone
-- ─────────────────────────────────────────────────────────────────────────────

DO $$
DECLARE
  exists_flag boolean;
BEGIN
  SELECT EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema = 'app_private' AND table_name = 'valid_state_transitions'
  ) INTO exists_flag;

  IF NOT exists_flag THEN
    RAISE NOTICE 'OK  : valid_state_transitions table removed';
  ELSE
    RAISE EXCEPTION 'FAIL: valid_state_transitions table still present';
  END IF;
END $$;

-- ─────────────────────────────────────────────────────────────────────────────
-- 6. suspected_pudos family: must be 0 tables (parent + 16 partitions)
-- ─────────────────────────────────────────────────────────────────────────────

DO $$
DECLARE
  table_count int;
  table_names text;
BEGIN
  SELECT COUNT(*), string_agg(table_name, ', ' ORDER BY table_name)
  INTO table_count, table_names
  FROM information_schema.tables
  WHERE table_schema = 'app_private'
    AND table_name LIKE 'suspected_pudos%';

  IF table_count = 0 THEN
    RAISE NOTICE 'OK  : suspected_pudos family removed (parent + 16 partitions)';
  ELSE
    RAISE EXCEPTION 'FAIL: % suspected_pudos table(s) still present: %',
      table_count, table_names;
  END IF;
END $$;

-- ─────────────────────────────────────────────────────────────────────────────
-- 7. pudo_decision_context.state_at_eval: must be gone
-- ─────────────────────────────────────────────────────────────────────────────

DO $$
DECLARE
  exists_flag boolean;
BEGIN
  SELECT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'app_private'
      AND table_name = 'pudo_decision_context'
      AND column_name = 'state_at_eval'
  ) INTO exists_flag;

  IF NOT exists_flag THEN
    RAISE NOTICE 'OK  : pudo_decision_context.state_at_eval removed';
  ELSE
    RAISE EXCEPTION 'FAIL: pudo_decision_context.state_at_eval still present';
  END IF;
END $$;

-- ─────────────────────────────────────────────────────────────────────────────
-- 8. pudo_decision_context.current_offer_id_at_eval: must exist (text NULL)
-- ─────────────────────────────────────────────────────────────────────────────

DO $$
DECLARE
  rec record;
BEGIN
  SELECT data_type, is_nullable
  INTO rec
  FROM information_schema.columns
  WHERE table_schema = 'app_private'
    AND table_name = 'pudo_decision_context'
    AND column_name = 'current_offer_id_at_eval';

  IF NOT FOUND THEN
    RAISE EXCEPTION 'FAIL: pudo_decision_context.current_offer_id_at_eval not added';
  ELSIF rec.data_type <> 'text' THEN
    RAISE EXCEPTION 'FAIL: current_offer_id_at_eval has wrong type (%, expected text)', rec.data_type;
  ELSIF rec.is_nullable <> 'YES' THEN
    RAISE EXCEPTION 'FAIL: current_offer_id_at_eval is NOT NULL (expected NULL-allowed)';
  ELSE
    RAISE NOTICE 'OK  : pudo_decision_context.current_offer_id_at_eval added (text NULL)';
  END IF;
END $$;

-- ─────────────────────────────────────────────────────────────────────────────
-- 9. driver_trip_state still exists with current_offer_id (sanity check)
-- ─────────────────────────────────────────────────────────────────────────────

DO $$
DECLARE
  exists_flag boolean;
BEGIN
  SELECT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'app_private'
      AND table_name = 'driver_trip_state'
      AND column_name = 'current_offer_id'
  ) INTO exists_flag;

  IF exists_flag THEN
    RAISE NOTICE 'OK  : driver_trip_state.current_offer_id preserved (1-bit memory)';
  ELSE
    RAISE EXCEPTION 'FAIL: driver_trip_state.current_offer_id missing — demolition over-reached';
  END IF;
END $$;

-- ─────────────────────────────────────────────────────────────────────────────
-- 10. driver_trip_state_log preserved as forensic archive
-- ─────────────────────────────────────────────────────────────────────────────

DO $$
DECLARE
  exists_flag boolean;
BEGIN
  SELECT EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema = 'app_private'
      AND table_name = 'driver_trip_state_log'
  ) INTO exists_flag;

  IF exists_flag THEN
    RAISE NOTICE 'OK  : driver_trip_state_log preserved (forensic archive, no longer written to)';
  ELSE
    RAISE EXCEPTION 'FAIL: driver_trip_state_log was removed but should have been preserved';
  END IF;
END $$;

-- ─────────────────────────────────────────────────────────────────────────────
-- 11. contest_events table: must be gone (demolished alongside check_convergence)
-- ─────────────────────────────────────────────────────────────────────────────

DO $$
DECLARE
  exists_flag boolean;
BEGIN
  SELECT EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema = 'app_private' AND table_name = 'contest_events'
  ) INTO exists_flag;

  IF NOT exists_flag THEN
    RAISE NOTICE 'OK  : contest_events table removed';
  ELSE
    RAISE EXCEPTION 'FAIL: contest_events table still present';
  END IF;
END $$;

\echo '═══════════════════════════════════════════════════════════════════════'
\echo ' VERIFICATION COMPLETE — all 11 assertions passed'
\echo ' Safe to redeploy application code (Step 5 of runbook)'
\echo '═══════════════════════════════════════════════════════════════════════'