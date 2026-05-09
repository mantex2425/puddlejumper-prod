-- Phase 2c.2 commit 1: drop unbound wai_status column.
--
-- Column was added during early Phase 1B planning, never populated by
-- any producer, and has been NULL in every row since creation. Sole
-- consumer was driver_status.py (the dev web monitor at
-- app.puddlejumper.io/monitor), which is knowingly-broken-by-attrition
-- pre-launch and will be rebuilt post-launch against whatever surface
-- exists then.
--
-- Per Canonical Rule V, transient diagnostic data belongs in
-- tad_decision_context JSONB (which already classifies via
-- classify_commit_rule), not flat columns. If Phase 2g tuning ever
-- needs band classification ("STRONG_MATCH" vs "WEAK_MATCH"
-- forensics), the band lands in tad_decision_context alongside
-- commit_rule, not as a re-added flat column.
--
-- Reversible: re-adding the column is a one-line ALTER TABLE if
-- needed. Pre-flight pg_dump captured at:
--   ~/puddlejumper-prod/tmp/pdc_schema_backup_2026-05-09.sql

BEGIN;

ALTER TABLE app_private.pudo_decision_context
    DROP COLUMN IF EXISTS wai_status;

COMMIT;
