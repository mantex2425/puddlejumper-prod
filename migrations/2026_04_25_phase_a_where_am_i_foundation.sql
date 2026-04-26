-- ============================================================================
-- Phase A: where_am_i() Foundation Schemas
-- ----------------------------------------------------------------------------
-- Date:    2026-04-25
-- RFC:     ~/puddlejumper-prod/WHERE_AM_I_PROPOSAL_v2.md
-- Sprint:  PuddleJumper commercial launch (~30 days out)
-- Author:  Andrew Bruce + Claude (paired) + Gemini review
--
-- Creates:
--   1. app_private.feature_flags         (TEXT PK + JSONB value, generic)
--   2. routing.known_stops_config        (1 row: min_cluster_duration_s = 15)
--   3. app_private.suspected_pudos       (hash partitioned 16 ways on driver_id)
--   4. 16 partitions with autovacuum_vacuum_threshold=25, scale_factor=0.05
--   5. Indexes (GIST on geog, partial active, retention)
--   6. app_private.suspected_pudos_janitor() function
--
-- Apply (entire file is one transaction — all-or-nothing):
--   psql -h 10.128.0.2 -U postgres -d puddlejumper -v ON_ERROR_STOP=1 -X \
--        -f ~/puddlejumper-prod/migrations/2026_04_25_phase_a_where_am_i_foundation.sql
--
-- Rollback file: 2026_04_25_phase_a_where_am_i_foundation.rollback.sql
-- ============================================================================

BEGIN;

-- ----------------------------------------------------------------------------
-- 1. app_private.feature_flags
-- ----------------------------------------------------------------------------
-- Generic flag store. JSONB chosen so allowlists, booleans, and configs all
-- fit without schema churn. WAI_PLANNER_ENABLED_DRIVERS is the SOLE gate for
-- the WAI fire path (no kill switch — Gemini consensus 2026-04-25).

CREATE TABLE IF NOT EXISTS app_private.feature_flags (
    flag_name      TEXT PRIMARY KEY,
    flag_value     JSONB NOT NULL,
    description    TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE app_private.feature_flags IS
    'Generic feature flag storage. JSONB value supports allowlists, booleans, configs.';
COMMENT ON COLUMN app_private.feature_flags.flag_value IS
    'JSONB. Allowlist: ["UjT1...","..."]. Boolean: true|false. Config: {...}.';

-- Seed: WAI planner driver allowlist (Andrew first)
INSERT INTO app_private.feature_flags (flag_name, flag_value, description) VALUES (
    'WAI_PLANNER_ENABLED_DRIVERS',
    '["UjT1hE9eBXh2q95aSZYOkzDJ8lo1"]'::jsonb,
    'Drivers for whom pudo_planner.py consumes where_am_i() output and may fire sm_transition. SOLE gate for the WAI fire path.'
)
ON CONFLICT (flag_name) DO NOTHING;


-- ----------------------------------------------------------------------------
-- 2. routing.known_stops_config
-- ----------------------------------------------------------------------------
-- Stop Atlas table itself is deferred to v1.1. This config row is referenced
-- by where_am_i.py for cluster duration tuning. Created now so the consuming
-- code path has a stable place to look.

CREATE TABLE IF NOT EXISTS routing.known_stops_config (
    config_key     TEXT PRIMARY KEY,
    config_value   TEXT NOT NULL,
    description    TEXT,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO routing.known_stops_config (config_key, config_value, description) VALUES
    ('min_cluster_duration_s', '15',
     'Minimum cluster duration (seconds) for a PUDO candidate to be considered. Tunable.')
ON CONFLICT (config_key) DO NOTHING;


-- ----------------------------------------------------------------------------
-- 3. app_private.suspected_pudos (hash-partitioned parent)
-- ----------------------------------------------------------------------------
-- Hash partitioning on driver_id for write distribution at 10k-driver scale.
-- Postgres requires the partition key inside the PRIMARY KEY, so PK is
-- (id, driver_id). id is BIGINT IDENTITY at the parent level.
--
-- Two-timer lifecycle (per kickoff §6.2 + Apr 25 update):
--   match_expires_at      = detected_at + 15 min  (retroactive recovery window)
--   retention_expires_at  = detected_at + 48 h    (analytics retention window)
--
-- Resolution is APPEND-ONLY. Never rewrite history. Late corrections are
-- recorded via state_log reconciliation entries elsewhere.

CREATE TABLE IF NOT EXISTS app_private.suspected_pudos (
    id                          BIGINT GENERATED ALWAYS AS IDENTITY,
    driver_id                   TEXT NOT NULL,
    detected_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Location (canonical: lat then lng — never reversed)
    lat                         DOUBLE PRECISION NOT NULL,
    lng                         DOUBLE PRECISION NOT NULL,
    geog                        GEOGRAPHY(Point, 4326) NOT NULL,
    pickup_h3                   TEXT NOT NULL,

    -- Cluster characteristics
    cluster_spread_m            REAL NOT NULL,
    cluster_duration_s          INT NOT NULL,
    cluster_median_speed_mph    REAL,
    on_wire                     BOOLEAN NOT NULL,
    current_road                TEXT,
    stop_context                TEXT,

    -- Context snapshot at detection time
    offer_id_at_time            TEXT,
    state_at_time               TEXT,

    -- Confidence (scalar in dataclass; full breakdown stored for analytics)
    confidence                  REAL NOT NULL,
    confidence_breakdown        JSONB,

    -- Resolution (append-only — never UPDATE history rows)
    resolved_as                 TEXT,
        -- 'current_pudo' | 'previous_pudo_retroactive' | 'false_positive' | NULL
    resolved_at                 TIMESTAMPTZ,
    resolved_by_offer           TEXT,

    -- Lifecycle (two timers)
    match_expires_at            TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '15 minutes'),
    retention_expires_at        TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '48 hours'),

    PRIMARY KEY (id, driver_id)
)
PARTITION BY HASH (driver_id);

COMMENT ON TABLE app_private.suspected_pudos IS
    'Ghost cache. Cluster detected but no current-ride offer matched. Used for retroactive correction within match window and product-health analytics within retention window. Hash-partitioned 16 ways on driver_id.';
COMMENT ON COLUMN app_private.suspected_pudos.match_expires_at IS
    '15-min window. After expiry, pudo_planner.py will not match this row to a new cluster.';
COMMENT ON COLUMN app_private.suspected_pudos.retention_expires_at IS
    '48-h window. After expiry, janitor hard-deletes the row.';
COMMENT ON COLUMN app_private.suspected_pudos.resolved_as IS
    'NULL = unresolved. Append-only — set once at resolution time, never overwritten.';


-- ----------------------------------------------------------------------------
-- 4. 16 hash partitions with tuned autovacuum
-- ----------------------------------------------------------------------------
-- Per-partition autovacuum settings:
--   threshold = 25            (small-table-trap mitigation; default 50 too lazy)
--   scale_factor = 0.05       (5% growth triggers vacuum once partition is large)
--
-- Formula: vacuum_when (dead_tuples > threshold + scale_factor * live_tuples)
-- Effective trigger:
--   100  rows -> 25 + 5  = 30 dead tuples
--   1000 rows -> 25 + 50 = 75 dead tuples
--   10k  rows -> 25 + 500 = 525 dead tuples
-- Keeps GIST indexes tight at all scales.

DO $$
DECLARE
    i INT;
    pname TEXT;
BEGIN
    FOR i IN 0..15 LOOP
        pname := format('suspected_pudos_p%s', lpad(i::text, 2, '0'));
        EXECUTE format($f$
            CREATE TABLE IF NOT EXISTS app_private.%I
            PARTITION OF app_private.suspected_pudos
            FOR VALUES WITH (MODULUS 16, REMAINDER %s)
            WITH (
                autovacuum_vacuum_threshold = 25,
                autovacuum_vacuum_scale_factor = 0.05,
                autovacuum_analyze_threshold = 25,
                autovacuum_analyze_scale_factor = 0.05
            )
        $f$, pname, i);
    END LOOP;
END $$;


-- ----------------------------------------------------------------------------
-- 5. Indexes (defined on parent — propagate to all 16 partitions)
-- ----------------------------------------------------------------------------

-- GIST on geog: ghost-match spatial lookups (within 50m of new cluster).
CREATE INDEX IF NOT EXISTS idx_suspected_pudos_geog
    ON app_private.suspected_pudos USING GIST (geog);

-- Active suspects per driver (the hot path: pudo_planner.py asks
-- "are there unresolved ghosts for this driver still in match window?").
CREATE INDEX IF NOT EXISTS idx_suspected_pudos_active
    ON app_private.suspected_pudos (driver_id, match_expires_at)
    WHERE resolved_at IS NULL;

-- Retention pruning support (janitor scans for past-retention rows).
CREATE INDEX IF NOT EXISTS idx_suspected_pudos_retention
    ON app_private.suspected_pudos (retention_expires_at);


-- ----------------------------------------------------------------------------
-- 6. Janitor function
-- ----------------------------------------------------------------------------
-- Called hourly via VM crontab on puddle-jumper:
--   psql -h 10.128.0.2 -U postgres -d puddlejumper \
--        -c "SELECT * FROM app_private.suspected_pudos_janitor();" \
--        >> /home/andrew/cron_wai_janitor.log 2>&1
--
-- Two operations, in order:
--   (a) Mark unresolved rows past match_expires_at as 'false_positive'
--   (b) Hard-delete rows past retention_expires_at

CREATE OR REPLACE FUNCTION app_private.suspected_pudos_janitor()
RETURNS TABLE (
    expired_marked_false_positive BIGINT,
    pruned_past_retention BIGINT
)
LANGUAGE plpgsql
AS $$
DECLARE
    v_marked BIGINT;
    v_pruned BIGINT;
BEGIN
    -- (a) Mark unresolved match-window expirations as false_positive
    WITH marked AS (
        UPDATE app_private.suspected_pudos
        SET resolved_as = 'false_positive',
            resolved_at = NOW()
        WHERE resolved_at IS NULL
          AND match_expires_at < NOW()
        RETURNING 1
    )
    SELECT COUNT(*) INTO v_marked FROM marked;

    -- (b) Hard-delete past retention
    WITH pruned AS (
        DELETE FROM app_private.suspected_pudos
        WHERE retention_expires_at < NOW()
        RETURNING 1
    )
    SELECT COUNT(*) INTO v_pruned FROM pruned;

    RETURN QUERY SELECT v_marked, v_pruned;
END
$$;

COMMENT ON FUNCTION app_private.suspected_pudos_janitor() IS
    'Hourly cron. Marks expired match-window rows as false_positive; hard-deletes past 48h retention. Returns counts.';


COMMIT;

-- ============================================================================
-- End of Phase A migration.
-- Next: run verification block (separate file: phase_a_verify.sql)
-- ============================================================================