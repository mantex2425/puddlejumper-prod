-- migrations/2026-06-05_deferred_sentinel_columns.sql
-- §5.5 Deferred Sentinel — storage substrate (FINDING §9, ratified 2026-06-05 PM)
--
-- Two additive nullable columns on offer_history:
--   expected_odometer        — the leg's band center (cumulative miles), or NULL
--                              when no band could be computed (the deferred sentinel).
--   expected_odometer_status — 'active' | 'deferred' | NULL(legacy/pre-sentinel).
--
-- NULL/NULL on legacy rows means "pre-sentinel, unknown" — NEVER interpreted as
-- deferred (§9.1: NULL forces consumers to branch; no magic number leaks into band math).
--
-- Idempotent: ADD COLUMN IF NOT EXISTS. Safe to re-run.

ALTER TABLE app_private.offer_history
  ADD COLUMN IF NOT EXISTS expected_odometer        double precision,
  ADD COLUMN IF NOT EXISTS expected_odometer_status text;

-- Verification (run after): both columns present, nullable.
-- SELECT column_name, data_type, is_nullable
-- FROM information_schema.columns
-- WHERE table_schema='app_private' AND table_name='offer_history'
--   AND column_name IN ('expected_odometer','expected_odometer_status');
