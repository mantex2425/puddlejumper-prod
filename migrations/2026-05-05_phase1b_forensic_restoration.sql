-- Phase 1B Forensic Restoration — schema migration
-- Date:        2026-05-05
-- Predecessor: 574b53e (Phase 1A transit-class adjacency gate)
-- Reviewer:    Gemini (ratified PHASE_1B_PROPOSAL_v2.md)
--
-- Changes:
--   1. ADD 4 new columns to pudo_decision_context:
--      - wai_current_road_class     (Phase 1A signal exposure)
--      - poi_lookup_source          (Phase 2 prep)
--      - poi_match_score            (Phase 2 prep)
--      - poi_top_names              (Phase 2 prep)
--   2. WIDEN wai_on_target_road from boolean to real (forensic fidelity)
--
-- Idempotent: re-running on partial state is safe.
-- Atomic:    single BEGIN/COMMIT.

BEGIN;

-- ============================================================================
-- 1. Additive: new forensic surface columns
-- ============================================================================

ALTER TABLE app_private.pudo_decision_context
    ADD COLUMN IF NOT EXISTS wai_current_road_class text,
    ADD COLUMN IF NOT EXISTS poi_lookup_source      text,
    ADD COLUMN IF NOT EXISTS poi_match_score        real,
    ADD COLUMN IF NOT EXISTS poi_top_names          text[];

-- ============================================================================
-- 2. Widening: wai_on_target_road boolean -> real
-- ============================================================================
-- Existing NULL/false/true rows convert losslessly.
-- Going forward, the column carries the float score from
-- MatchOutcome.signals['on_target_road'] directly (0.0-1.0 range).
-- Guarded by a type-introspection block so re-running the migration
-- after the ALTER has been applied does not error.

DO $migration$
BEGIN
    IF (SELECT data_type FROM information_schema.columns
        WHERE table_schema = 'app_private'
          AND table_name = 'pudo_decision_context'
          AND column_name = 'wai_on_target_road') = 'boolean'
    THEN
        ALTER TABLE app_private.pudo_decision_context
            ALTER COLUMN wai_on_target_road TYPE real
            USING (CASE
                WHEN wai_on_target_road IS NULL THEN NULL
                WHEN wai_on_target_road THEN 1.0::real
                ELSE 0.0::real
            END);
    END IF;
END
$migration$;

-- ============================================================================
-- 3. Documentation
-- ============================================================================

COMMENT ON COLUMN app_private.pudo_decision_context.wai_current_road_class IS
    'Phase 1A: OSM-derived road class of the GPS-snapped current_road. Values: transit | residential | off_wire | unknown | NULL. Drives the transit-gate suppression of _signal_adjacent_road_match.';

COMMENT ON COLUMN app_private.pudo_decision_context.wai_on_target_road IS
    'Phase 1B: float 0.0-1.0 score from MatchOutcome.signals[on_target_road]. Phase 1A and earlier stored as boolean; widened to real for soft-match fidelity.';

COMMENT ON COLUMN app_private.pudo_decision_context.poi_lookup_source IS
    'Phase 2 (Operation Strip Mall): how the POI signal was sourced for this evaluation. Values: cache_hit | api_call | api_error | skipped | NULL.';

COMMENT ON COLUMN app_private.pudo_decision_context.poi_match_score IS
    'Phase 2: 0.0-1.0 score returned by _signal_poi_match for the top match. NULL when poi_lookup_source = skipped or no match.';

COMMENT ON COLUMN app_private.pudo_decision_context.poi_top_names IS
    'Phase 2: top 3 POI business names by distance, for forensic debugging. NULL when no POI lookup performed.';

COMMIT;