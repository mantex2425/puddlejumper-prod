-- migrations/2026-05-05_phase2a_poi_cache.sql
-- Phase 2a: POI cache foundation for Operation Strip Mall.
-- Additive-only. Idempotent. Atomic.
--
-- Source of truth: OPERATION_STRIP_MALL_PROPOSAL.md REVISIONS R1, R2, R3.
-- Gemini-ratified 2026-05-05 with last_hit_at addition (A) accepted,
-- partial-index suggestion (B) declined (NOW() not IMMUTABLE),
-- and array_length -> cardinality refinement accepted (NULL semantics).

BEGIN;

CREATE TABLE IF NOT EXISTS app_private.poi_cache (
    id               bigserial PRIMARY KEY,
    cached_at        timestamptz NOT NULL DEFAULT (NOW() AT TIME ZONE 'UTC'),
    expires_at       timestamptz NOT NULL,
    last_hit_at      timestamptz,
    query_lat        double precision NOT NULL,
    query_lng        double precision NOT NULL,
    query_geog       geography(Point, 4326) NOT NULL
                     GENERATED ALWAYS AS (
                         app_private.coords_to_geography(query_lat, query_lng)
                     ) STORED,
    google_place_ids text[] NOT NULL,
    business_names   text[] NOT NULL,
    business_types   text[] NOT NULL,

    CONSTRAINT poi_cache_expires_after_cached
        CHECK (expires_at > cached_at),
    CONSTRAINT poi_cache_lat_range
        CHECK (query_lat BETWEEN -90.0 AND 90.0),
    CONSTRAINT poi_cache_lng_range
        CHECK (query_lng BETWEEN -180.0 AND 180.0),
    CONSTRAINT poi_cache_arrays_aligned
        CHECK (
            cardinality(google_place_ids) = cardinality(business_names)
            AND cardinality(business_names) = cardinality(business_types)
        )
);

CREATE INDEX IF NOT EXISTS idx_poi_cache_geog
    ON app_private.poi_cache USING GIST (query_geog);

CREATE INDEX IF NOT EXISTS idx_poi_cache_expires
    ON app_private.poi_cache (expires_at);

COMMENT ON TABLE app_private.poi_cache IS
    'Phase 2a POI radius cache. Phase 2b (Google Places integration) writes rows; '
    'Phase 2a reads only from preexisting rows (initially empty). 30-day TTL on expires_at. '
    'Lookup pattern: ST_DWithin(query_geog, target_geog, 80) per OPERATION_STRIP_MALL_PROPOSAL R3.';

COMMENT ON COLUMN app_private.poi_cache.query_geog IS
    'Generated from (query_lat, query_lng) via app_private.coords_to_geography. '
    'STORED so it can be GIST-indexed. Wrapper is IMMUTABLE per pg_proc inspection 2026-05-05.';

COMMENT ON COLUMN app_private.poi_cache.last_hit_at IS
    'NULL until first successful read; updated to (NOW() AT TIME ZONE ''UTC'') on each cache hit. '
    'Serves cache-utility analytics and future LRU eviction. Distinct from cached_at (write time) '
    'and expires_at (TTL boundary).';

COMMIT;
