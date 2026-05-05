-- migrations/2026-05-05_phase2b_poi_cache_jsonb.sql
-- Phase 2b Step 1: poi_cache schema refactor — parallel arrays → single jsonb.
-- Atomic. Idempotent.
--
-- Source of truth: Operation Strip Mall iron-fist mandate 2026-05-05.
-- Architectural Reset (Option C): drop the three text[] arrays plus the
-- alignment CHECK constraint; add a single `places` jsonb column holding
-- the full Google Places v1 payload.
--
-- Safe-to-run premises (verified 2026-05-05):
--   - poi_cache row count = 0 in prod (zero data loss on column drop)
--   - poi_service.py is on-branch only; no production code path imports it
--   - tests/test_poi_service.py rewrites land in Phase 2b Step 3
--
-- Rollback: this migration is reversible by re-creating the dropped columns
-- with NOT NULL = false initially, then backfilling from `places`. Given
-- row count = 0, rollback is functionally cost-free.

BEGIN;

-- 1. Drop the alignment constraint that depends on the parallel arrays.
--    Must precede column drops; DROP COLUMN IF EXISTS would CASCADE the
--    constraint anyway but explicit ordering is clearer for review.
ALTER TABLE app_private.poi_cache
    DROP CONSTRAINT IF EXISTS poi_cache_arrays_aligned;

-- 2. Drop the three parallel arrays.
ALTER TABLE app_private.poi_cache
    DROP COLUMN IF EXISTS google_place_ids,
    DROP COLUMN IF EXISTS business_names,
    DROP COLUMN IF EXISTS business_types;

-- 3. Add the single jsonb column. NOT NULL with `[]` default so any future
--    insert that omits places still satisfies the constraint, and any
--    pre-existing rows (zero in prod, defensive) get a valid empty array.
ALTER TABLE app_private.poi_cache
    ADD COLUMN IF NOT EXISTS places jsonb NOT NULL DEFAULT '[]'::jsonb;

-- 4. CHECK constraint: places must be a json array, never a json object,
--    string, number, or null. Negative cache stores '[]'; positive cache
--    stores '[{...}, {...}]'. Postgres lacks IF NOT EXISTS for ADD
--    CONSTRAINT, so we guard with a DO block for idempotency.
DO $migration$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'poi_cache_places_is_array'
          AND conrelid = 'app_private.poi_cache'::regclass
    ) THEN
        ALTER TABLE app_private.poi_cache
            ADD CONSTRAINT poi_cache_places_is_array
            CHECK (jsonb_typeof(places) = 'array');
    END IF;
END
$migration$;

-- 5. Update comments to reflect the new schema shape.
COMMENT ON TABLE app_private.poi_cache IS
    'Phase 2b POI radius cache. Operation Strip Mall: Google Places v1 '
    '(searchNearby) is called on cache miss within 80m of cluster centroid. '
    '30-day TTL on expires_at. places jsonb holds the full v1 payload per '
    'place: {id, displayName.text, types[], location}. Negative cache rows '
    'have places = ''[]''. Lookup: ST_DWithin(query_geog, target_geog, 80). '
    'Schema refactored 2026-05-05 from 2a parallel arrays to single jsonb.';

COMMENT ON COLUMN app_private.poi_cache.places IS
    'JSONB array of Google Places v1 searchNearby results within 50m of '
    '(query_lat, query_lng) at fetch time. Each element: '
    '{"id":"ChIJ...","displayName":{"text":"Shell","languageCode":"en"},'
    '"types":["gas_station","point_of_interest",...],'
    '"location":{"latitude":...,"longitude":...}}. '
    'Empty array = negative cache (Google returned zero results). '
    'Replaces 2a parallel arrays google_place_ids/business_names/business_types.';

COMMIT;