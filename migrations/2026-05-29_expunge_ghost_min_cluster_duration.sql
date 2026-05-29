-- Migration: expunge ghost min_cluster_duration_s config row
-- Date: 2026-05-29
-- Sprint: One Arrest Period (arrest-vs-cluster timing alignment)
-- Ratified: Andrew + Claude + Gemini paired-programming protocol, 2026-05-29
--
-- WHY:
--   routing.known_stops_config.min_cluster_duration_s = '15' was seeded by
--   2026_04_25_phase_a_where_am_i_foundation.sql but is UNREAD by any runtime
--   code path (verified 2026-05-29: only references are docstring ghost text
--   at cluster_detection.py:90 and migration/verify artifacts, none of which
--   read the value at execution time). The live cluster dwell threshold is
--   governed by detect_cluster's min_duration_s signature default, now unified
--   to ARREST_DURATION_THRESHOLD_S = 5.0 in the companion code change.
--
--   An inert config row contradicting the live execution parameter (15 vs the
--   former 10.0 default vs the arrest counter's 5.0) is an operational trap.
--   It cost ~10 minutes of misdirection during the 2026-05-29 forensic. Expunge.
--
-- SUPERSEDES (documentation only — these are NOT edited; historical record):
--   - 2026_04_25_phase_a_where_am_i_foundation.sql (seeded the row)
--   - phase_a_verify.sql (asserts the row exists; its EXPECT line is now stale.
--     If phase_a_verify.sql is still run in any verify loop, update its
--     assertion separately; if it was a one-time post-phase-a check, it is inert.)
--
-- IDEMPOTENT: re-running after the row is gone deletes 0 rows and raises no
--   error. The guarded assertion only fires on the FIRST run.

BEGIN;

-- Guard: confirm we are deleting exactly the ghost key and nothing else.
-- (On re-run the row is already absent; the DO block reports 0 and continues.)
DO $$
DECLARE
    n integer;
BEGIN
    DELETE FROM routing.known_stops_config
    WHERE config_key = 'min_cluster_duration_s';
    GET DIAGNOSTICS n = ROW_COUNT;
    RAISE NOTICE 'expunge_ghost_min_cluster_duration: deleted % row(s)', n;
    IF n > 1 THEN
        RAISE EXCEPTION 'Expected at most 1 row for config_key=min_cluster_duration_s, deleted %', n;
    END IF;
END $$;

COMMIT;
