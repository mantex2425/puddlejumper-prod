-- ============================================================================
-- Phase A Verification
-- ----------------------------------------------------------------------------
-- Run AFTER applying 2026_04_25_phase_a_where_am_i_foundation.sql
-- Each block prints a clear PASS / FAIL signal.
-- ============================================================================

\echo
\echo === V1: feature_flags table exists with WAI seed row ===
SELECT
    flag_name,
    flag_value,
    description
FROM app_private.feature_flags
WHERE flag_name = 'WAI_PLANNER_ENABLED_DRIVERS';
-- EXPECT: 1 row, flag_value contains "UjT1hE9eBXh2q95aSZYOkzDJ8lo1"

\echo
\echo === V2: known_stops_config seeded ===
SELECT * FROM routing.known_stops_config;
-- EXPECT: 1 row, min_cluster_duration_s = 15

\echo
\echo === V3: suspected_pudos parent is hash-partitioned ===
SELECT
    n.nspname AS schema,
    c.relname AS table,
    c.relkind,
    pg_get_partkeydef(c.oid) AS partition_strategy
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'app_private' AND c.relname = 'suspected_pudos';
-- EXPECT: relkind='p' (partitioned table), partition_strategy='HASH (driver_id)'

\echo
\echo === V4: all 16 partitions present ===
SELECT
    inhrelid::regclass AS partition_name,
    pg_get_expr(c.relpartbound, c.oid) AS partition_bound
FROM pg_inherits i
JOIN pg_class c ON c.oid = i.inhrelid
WHERE i.inhparent = 'app_private.suspected_pudos'::regclass
ORDER BY partition_name;
-- EXPECT: 16 rows, p00 through p15, each FOR VALUES WITH (modulus 16, remainder N)

\echo
\echo === V5: autovacuum settings on partitions ===
SELECT
    c.relname AS partition_name,
    c.reloptions
FROM pg_class c
JOIN pg_inherits i ON i.inhrelid = c.oid
WHERE i.inhparent = 'app_private.suspected_pudos'::regclass
ORDER BY c.relname
LIMIT 3;
-- EXPECT: reloptions array contains autovacuum_vacuum_threshold=25,
--         autovacuum_vacuum_scale_factor=0.05, and analyze counterparts

\echo
\echo === V6: indexes propagated to all partitions ===
SELECT
    c.relname AS partition,
    COUNT(idx.indexname) AS index_count,
    string_agg(idx.indexname, ', ' ORDER BY idx.indexname) AS indexes
FROM pg_class c
JOIN pg_inherits i ON i.inhrelid = c.oid
LEFT JOIN pg_indexes idx
       ON idx.schemaname = 'app_private' AND idx.tablename = c.relname
WHERE i.inhparent = 'app_private.suspected_pudos'::regclass
GROUP BY c.relname
ORDER BY c.relname
LIMIT 3;
-- EXPECT: each partition has 3 indexes (geog GIST, active partial, retention)

\echo
\echo === V7: janitor function exists and is callable ===
SELECT
    proname,
    pg_get_function_result(oid) AS returns,
    pg_get_function_arguments(oid) AS args
FROM pg_proc
WHERE pronamespace = 'app_private'::regnamespace
  AND proname = 'suspected_pudos_janitor';
-- EXPECT: 1 row, returns TABLE(expired_marked_false_positive bigint, pruned_past_retention bigint)

\echo
\echo === V8: dry-run janitor (empty table -> 0 marked, 0 pruned) ===
SELECT * FROM app_private.suspected_pudos_janitor();
-- EXPECT: expired_marked_false_positive=0, pruned_past_retention=0

\echo
\echo === V9: smoke insert + read + cleanup ===
-- Verify INSERT works through hash routing, GIST is queryable, then clean up.
INSERT INTO app_private.suspected_pudos (
    driver_id, lat, lng, geog, pickup_h3,
    cluster_spread_m, cluster_duration_s, on_wire, confidence
) VALUES (
    'SMOKE_TEST_DRIVER',
    29.6246, -95.5102,
    app_private.coords_to_geography(29.6246, -95.5102),
    app_private.coords_to_h3(29.6246, -95.5102),
    15.0, 30, true, 0.82
);

SELECT
    driver_id,
    lat, lng,
    pickup_h3,
    on_wire,
    confidence,
    match_expires_at > NOW()                                   AS match_window_active,
    retention_expires_at > NOW() + INTERVAL '47 hours'         AS retention_window_correct,
    resolved_as IS NULL                                        AS unresolved
FROM app_private.suspected_pudos
WHERE driver_id = 'SMOKE_TEST_DRIVER';
-- EXPECT: 1 row, all booleans = t

-- GIST index test: should hit our smoke row at <50m
SELECT COUNT(*) AS gist_hits
FROM app_private.suspected_pudos
WHERE driver_id = 'SMOKE_TEST_DRIVER'
  AND ST_DWithin(geog, app_private.coords_to_geography(29.6246, -95.5102), 50.0);
-- EXPECT: gist_hits = 1

-- Cleanup smoke row
DELETE FROM app_private.suspected_pudos WHERE driver_id = 'SMOKE_TEST_DRIVER';

\echo
\echo === Phase A verification complete ===