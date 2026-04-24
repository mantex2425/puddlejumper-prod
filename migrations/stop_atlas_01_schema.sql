-- ═══════════════════════════════════════════════════════════════════
-- Stop Atlas — Schema (Migration 1 of N)
-- ═══════════════════════════════════════════════════════════════════
-- Purpose:
--   Suppression lookup of known non-pickup stop locations (traffic
--   signals, RR grade crossings) to prevent BMOAR from firing a
--   pickup or dropoff nail at a red light or crossing.
--
-- Hot path:
--   Called during DIAGNOSE phase of BMOAR. Pure read. Returns the
--   nearest stop within per-row suppression_radius_m, or nothing.
--
-- Architectural role:
--   - Physical ground-truth layer (per "Distance over Geocode")
--   - Read in DIAGNOSE, decision made in PLAN
--   - No state-machine writes; ingestion populates from scripts
--
-- Reference: documentation/STOPS_DB_PROPOSAL.md
-- ═══════════════════════════════════════════════════════════════════

BEGIN;

-- Sanity: routing schema must already exist (houston_ways lives there)
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_namespace WHERE nspname = 'routing'
  ) THEN
    RAISE EXCEPTION 'routing schema does not exist; aborting Stop Atlas migration';
  END IF;
END
$$;

CREATE TABLE routing.stop_atlas (
    id                    bigserial PRIMARY KEY,
    lat                   double precision NOT NULL,
    lng                   double precision NOT NULL,
    geog                  geography(Point, 4326) NOT NULL,
    stop_type             text NOT NULL
                          CHECK (stop_type IN ('traffic_signal','railway_crossing')),
    source                text NOT NULL,
    source_id             text,
    suppression_radius_m  smallint NOT NULL DEFAULT 30
                          CHECK (suppression_radius_m BETWEEN 10 AND 100),
    source_extra          jsonb,
    ingested_at           timestamptz NOT NULL
                          DEFAULT (NOW() AT TIME ZONE 'UTC')
);

-- Primary access path: spatial proximity for hot-path suppression query.
CREATE INDEX idx_stop_atlas_geog
    ON routing.stop_atlas
    USING GIST (geog);

-- Secondary: filtering by stop_type during analytics / dedup.
CREATE INDEX idx_stop_atlas_type
    ON routing.stop_atlas (stop_type);

-- Uniqueness: prevent cron double-loads for authoritative sources.
-- Partial because OSM nodes may lack stable source_id.
CREATE UNIQUE INDEX uq_stop_atlas_source_id
    ON routing.stop_atlas (source, source_id)
    WHERE source_id IS NOT NULL;

-- Table documentation for future-Andrew and anyone else who opens
-- this table in pgAdmin/psql \d+ three months from now.
COMMENT ON TABLE  routing.stop_atlas                       IS 'Suppression lookup: traffic signals + RR grade crossings for BMOAR. Read in DIAGNOSE, decision made in PLAN.';
COMMENT ON COLUMN routing.stop_atlas.lat                   IS 'Stored for debugging/forensics; geog is the queried column.';
COMMENT ON COLUMN routing.stop_atlas.lng                   IS 'Stored for debugging/forensics; geog is the queried column.';
COMMENT ON COLUMN routing.stop_atlas.geog                  IS 'Populated via app_private.coords_to_geography(lat,lng). Hot-path index.';
COMMENT ON COLUMN routing.stop_atlas.stop_type             IS 'traffic_signal | railway_crossing. Schema extends to stop_sign / toll_booth / school_zone in future via new values.';
COMMENT ON COLUMN routing.stop_atlas.source                IS 'coh | harris | fbc | fra | osm';
COMMENT ON COLUMN routing.stop_atlas.source_id             IS 'Upstream stable ID when available. Nullable for OSM nodes without IDs.';
COMMENT ON COLUMN routing.stop_atlas.suppression_radius_m  IS 'Per-row radius. 30m default, 45m for signals on motorway/trunk/primary/motorway_link edges (set by ingestion).';
COMMENT ON COLUMN routing.stop_atlas.source_extra          IS 'Raw upstream row attributes stashed for forensic debugging. Unindexed.';
COMMENT ON COLUMN routing.stop_atlas.ingested_at           IS 'UTC timestamptz. Updated on conflict during cron refresh.';

-- Grants for Cloud Run service role (atjb).
-- Read-only: the API only reads the atlas on the hot path.
-- Ingestion runs from the VM shell as `postgres`, not as `atjb`.
GRANT USAGE ON SCHEMA routing TO atjb;
GRANT SELECT ON routing.stop_atlas TO atjb;

COMMIT;
