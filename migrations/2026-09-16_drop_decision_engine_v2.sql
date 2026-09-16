-- 2026-09-16: drop decision_engine_v2 (applied to prod 2026-09-16 by postgres).
-- v3 is the only engine; the Python fallback to v2 was removed in 57124ec and nothing in the
-- database (functions, views, triggers, cron) depended on it.
-- The final definition is archived at migrations/archive/decision_engine_v2_final_2026-09-16.sql.
DROP FUNCTION IF EXISTS app_private.decision_engine_v2(text,numeric,numeric,numeric,numeric,numeric,numeric,numeric,numeric,numeric,text,boolean,numeric,numeric,text,numeric,numeric,numeric,boolean);
