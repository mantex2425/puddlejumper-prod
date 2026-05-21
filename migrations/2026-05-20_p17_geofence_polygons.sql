-- P17 schema migration: routing.geofence_polygons
-- Andrew + Gemini ratification 2026-05-20 (revision 3)
-- Reversible via DROP TABLE IF EXISTS routing.geofence_polygons CASCADE;

BEGIN;

-- Pre-flight: ensure PostGIS is available
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'postgis') THEN
        RAISE EXCEPTION 'PostGIS extension not installed';
    END IF;
END $$;

-- Idempotency: refuse to clobber an existing table with data
DO $$
DECLARE
    row_count bigint;
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'routing' AND table_name = 'geofence_polygons'
    ) THEN
        EXECUTE 'SELECT COUNT(*) FROM routing.geofence_polygons' INTO row_count;
        IF row_count > 0 THEN
            RAISE EXCEPTION 'routing.geofence_polygons already exists with % rows. Drop manually before re-running.', row_count;
        END IF;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS routing.geofence_polygons (
    id              bigserial PRIMARY KEY,
    osm_id          bigint NOT NULL,
    osm_type        text NOT NULL CHECK (osm_type IN ('way', 'relation')),
    category        text NOT NULL,
    subcategory     text,
    name            text,
    name_normalized text,
    tags            jsonb NOT NULL DEFAULT '{}'::jsonb,
    geom            geometry(MultiPolygon, 4326) NOT NULL,
    area_m2         double precision NOT NULL,
    centroid_lat    double precision NOT NULL,
    centroid_lng    double precision NOT NULL,
    market          text NOT NULL,
    ingested_at     timestamptz NOT NULL DEFAULT (NOW() AT TIME ZONE 'UTC'),
    source_revision text NOT NULL,
    UNIQUE (osm_type, osm_id)
);

CREATE INDEX IF NOT EXISTS idx_geofence_polygons_geom
    ON routing.geofence_polygons USING GIST (geom);

CREATE INDEX IF NOT EXISTS idx_geofence_polygons_category_market
    ON routing.geofence_polygons (category, market);

COMMENT ON TABLE routing.geofence_polygons IS
    'OSM-derived venue polygons for geofence membership matching. Ingested per-market via Overpass. See docs/RFC_P17_GEOFENCE_INGESTION.md.';

COMMENT ON COLUMN routing.geofence_polygons.geom IS
    'Planar geometry SRID 4326 for sub-ms ST_Contains. area_m2 computed once via ST_Area(geom::geography) at ingest.';

COMMENT ON COLUMN routing.geofence_polygons.area_m2 IS
    'Square meters. Used for innermost-polygon resolution: ORDER BY area_m2 ASC LIMIT N. Computed via ST_Area(geom::geography) per Gemini ratification 2026-05-20.';


-- P0 FIX 2026-05-21: GRANT SELECT to application user.
-- Without this, every heartbeat that reaches _signal_geofence_membership
-- raises psycopg2.errors.InsufficientPrivilege and returns HTTP 500.
-- See docs/EVENING_2026_05_20_BACKLOG_FOUNDATION.md and
-- docs/REPLY_TO_P0_ESCALATION_2026_05_21.md.
GRANT SELECT ON routing.geofence_polygons TO atjb;

COMMIT;

-- Verification (outside transaction)
\d routing.geofence_polygons
SELECT
    schemaname, tablename, indexname, indexdef
FROM pg_indexes
WHERE schemaname = 'routing' AND tablename = 'geofence_polygons'
ORDER BY indexname;
