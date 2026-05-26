-- =============================================================================
-- Migration: app_private.driver_trip_locks
-- Purpose:   §XVI.G Transaction Lock state storage
-- Authors:   Andrew + Claude (paired-programming) + Gemini (ratified 2026-05-26)
-- Date:      2026-05-26
-- Sprint:    A (post-§XVI.C amendment validation forensic re-analysis)
-- =============================================================================
--
-- Background
-- ----------
-- §XVI.G of CANONICAL_RULES.md documents a per-offer/per-leg Transaction Lock
-- that prevents same-leg refire until the driver moves 500ft from the fire
-- location OR sustains speed_mph > 5 for 10 contiguous seconds. Forensic
-- analysis of the 2026-05-24 / 2026-05-26 validation drives revealed that
-- the lock was never implemented in code. Offer 8355 received 68 redundant
-- FirePickupObservation actions in 116 seconds on a single sustained arrest,
-- exemplifying the bug class.
--
-- This migration introduces the storage substrate for the lock state.
-- Implementation in the matcher (driver_heartbeat.py) follows in Sprint A
-- Step 2 (TDD baseline) and Step 3 (matcher integration).
--
-- Design decisions
-- ----------------
-- A. GC policy: explicit DELETE from matcher code on lock release and on
--    actual_dropoff_at stamp. No foreign key to offer_history (this table
--    is a transient operational ledger, not an analytical record).
-- B. pudo_type uses TEXT + CHECK constraint rather than enum (zero-downtime
--    expansion path for future multi-leg scenarios).
-- C. Coordinates stored as DOUBLE PRECISION lat/lng, consistent with
--    offer_history, driver_trip_state, and pudo_decision_context.
--    Distance math at runtime via app_private.distance_miles() per §I.
-- D. The 500ft / 10s release-condition constants live in driver_heartbeat.py,
--    not in this table. Tuning them requires a canonical-rule amendment.
--
-- Multi-row composite-key design: the (driver_id, offer_id, pudo_type) PK
-- protects against the stacked-offer lock-overwrite vulnerability identified
-- during Sprint A design review. Each (offer, leg) holds its own lock row
-- independently; firing offer B does not disturb offer A's lock state.
--
-- Idempotency
-- -----------
-- This script is re-runnable. Every DDL statement uses IF NOT EXISTS or
-- DO ... BEGIN/EXCEPTION blocks to evaluate to a safe no-op when the
-- target already exists. Running twice produces identical end-state and
-- exits 0 both times.
--
-- Verification
-- ------------
-- After applying, expect:
--   - SELECT to_regclass('app_private.driver_trip_locks') → not NULL
--   - \d app_private.driver_trip_locks shows 7 columns + PK + CHECK
--   - SELECT COUNT(*) FROM app_private.driver_trip_locks → 0
--   - re-running this script produces zero changes (no errors, no DDL)
-- =============================================================================

BEGIN;

-- -----------------------------------------------------------------------------
-- Phase 1: Idempotent table creation
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app_private.driver_trip_locks (
    driver_id              TEXT             NOT NULL,
    offer_id               TEXT             NOT NULL,
    pudo_type              TEXT             NOT NULL,
    fired_at               TIMESTAMPTZ      NOT NULL,
    fired_lat              DOUBLE PRECISION NOT NULL,
    fired_lng              DOUBLE PRECISION NOT NULL,
    fired_cumulative_miles NUMERIC          NOT NULL,
    PRIMARY KEY (driver_id, offer_id, pudo_type)
);

-- -----------------------------------------------------------------------------
-- Phase 2: Idempotent CHECK constraint
-- -----------------------------------------------------------------------------
-- pudo_type must be one of two values. ADD CONSTRAINT is not idempotent on
-- its own (it fails with "constraint already exists" on second run), so we
-- wrap it in a DO block that checks the catalog first.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM   pg_constraint
        WHERE  conname = 'driver_trip_locks_pudo_type_check'
        AND    conrelid = 'app_private.driver_trip_locks'::regclass
    ) THEN
        ALTER TABLE app_private.driver_trip_locks
            ADD CONSTRAINT driver_trip_locks_pudo_type_check
            CHECK (pudo_type IN ('pickup', 'dropoff'));
    END IF;
END
$$;

-- -----------------------------------------------------------------------------
-- Phase 3: Comments (forensic documentation embedded in the catalog)
-- -----------------------------------------------------------------------------
COMMENT ON TABLE app_private.driver_trip_locks IS
'§XVI.G Transaction Lock state. One row per (driver, offer, leg) currently '
'within the lock window. Rows are deleted explicitly by matcher code on '
'lock release (driver moved 500ft OR sustained >5mph for 10s) and on '
'actual_dropoff_at stamp. No foreign keys: this is a transient operational '
'ledger, not an analytical record. See CANONICAL_RULES.md §XVI.G and '
'docs/sprint_notes/xvi_c_validation_amendment_brief_2026-05-26.md.';

COMMENT ON COLUMN app_private.driver_trip_locks.driver_id IS
'Firebase UID of the locked driver. Composite PK component.';

COMMENT ON COLUMN app_private.driver_trip_locks.offer_id IS
'Offer ID whose leg is locked. Composite PK component. Independent locks '
'per offer prevent the stacked-offer overwrite vulnerability.';

COMMENT ON COLUMN app_private.driver_trip_locks.pudo_type IS
'Which leg of the offer is locked: pickup or dropoff. Composite PK component. '
'CHECK constraint enforces the two-value domain at the storage boundary.';

COMMENT ON COLUMN app_private.driver_trip_locks.fired_at IS
'UTC timestamp of the fire that engaged this lock. Used to compute lock_age_s '
'in PDC forensic rows.';

COMMENT ON COLUMN app_private.driver_trip_locks.fired_lat IS
'Driver latitude at fire moment. Used with fired_lng and live GPS to compute '
'the 500ft spatial release condition via app_private.distance_miles().';

COMMENT ON COLUMN app_private.driver_trip_locks.fired_lng IS
'Driver longitude at fire moment. Paired with fired_lat per §I lat-first ordering.';

COMMENT ON COLUMN app_private.driver_trip_locks.fired_cumulative_miles IS
'Driver odometer reading at fire moment. Backup release signal when GPS noise '
'makes distance_miles computation unreliable (delta cumulative_miles vs live '
'odometer is a noise-free distance proxy).';

COMMIT;

-- =============================================================================
-- Post-migration verification (run separately after the BEGIN/COMMIT closes)
-- =============================================================================
-- These SELECTs return the verification evidence. They are read-only and
-- safe to run any number of times.

\echo
\echo === V1: Table exists ===
SELECT to_regclass('app_private.driver_trip_locks') AS table_oid;

\echo
\echo === V2: Column inventory (expect 7 columns) ===
SELECT column_name, data_type, is_nullable
FROM   information_schema.columns
WHERE  table_schema = 'app_private'
AND    table_name = 'driver_trip_locks'
ORDER  BY ordinal_position;

\echo
\echo === V3: Primary key composition (expect 3-column composite) ===
SELECT a.attname AS column_name, i.indisprimary
FROM   pg_index i
JOIN   pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
WHERE  i.indrelid = 'app_private.driver_trip_locks'::regclass
AND    i.indisprimary;

\echo
\echo === V4: CHECK constraint present ===
SELECT conname, pg_get_constraintdef(oid) AS definition
FROM   pg_constraint
WHERE  conrelid = 'app_private.driver_trip_locks'::regclass
AND    contype = 'c';

\echo
\echo === V5: Row count (expect 0) ===
SELECT COUNT(*) AS lock_rows FROM app_private.driver_trip_locks;

\echo
\echo === V6: Comment on table (forensic doc embedded in catalog) ===
SELECT obj_description('app_private.driver_trip_locks'::regclass, 'pg_class') AS table_comment;
