-- ============================================================================
-- Migration: 2026-05-13 Phase B Rule XVI Forensic Ladder
-- ============================================================================
--
-- Adds forensic columns to pudo_decision_context (per-heartbeat record of
-- Forensic Ladder phase progression) and cross-heartbeat state columns to
-- driver_trip_state (arrest counter, current phase, Phase 4 peak tracking).
--
-- Doctrine: docs/CANONICAL_RULES.md Rule XVI (ratified by Gemini 2026-05-13).
-- Plan: PHASE_B_IMPLEMENTATION_PLAN_2026-05-13.md, Section B-1.
--
-- Safety properties:
--   - All new columns are NULLABLE with no DEFAULT (no rewrites to existing rows)
--   - No constraints that could fail at migration time
--   - No index creation (defer until B-3 when columns are actually written to)
--   - Reversible via DROP COLUMN
--
-- Rollback: see end of file for matching DROP COLUMN statements.
-- ============================================================================

BEGIN;

-- ─── pudo_decision_context: Forensic Ladder per-heartbeat record ───────────

ALTER TABLE app_private.pudo_decision_context
  ADD COLUMN arrest_started_at  timestamptz,
  ADD COLUMN arrest_duration_s  real,
  ADD COLUMN matched_offer_id   text,
  ADD COLUMN match_signal       text,
  ADD COLUMN matcher_candidates text[],
  ADD COLUMN phase_reached      smallint,
  ADD COLUMN poi_source         text,
  ADD COLUMN peak_confidence    real,
  ADD COLUMN decay_samples      smallint,
  ADD COLUMN unmatched_reason   text;

COMMENT ON COLUMN app_private.pudo_decision_context.arrest_started_at IS
  'Rule XVI: timestamp when the current zero-velocity counter began. NULL when not currently stopped.';

COMMENT ON COLUMN app_private.pudo_decision_context.arrest_duration_s IS
  'Rule XVI: seconds of contiguous zero velocity at this heartbeat. NULL when not stopped.';

COMMENT ON COLUMN app_private.pudo_decision_context.matched_offer_id IS
  'Rule XVI: offer_id selected by the matcher at Phase 2b/3. NULL when no match or no fire.';

COMMENT ON COLUMN app_private.pudo_decision_context.match_signal IS
  'Rule XVI: which gate combination produced the match. Values: tad_and_wai, tad_and_wai_ambiguous, dispatch_resolved, no_match.';

COMMENT ON COLUMN app_private.pudo_decision_context.matcher_candidates IS
  'Rule XVI: full list of offer_ids that passed both TAD and WAI gates at match time. For ambiguity forensics.';

COMMENT ON COLUMN app_private.pudo_decision_context.phase_reached IS
  'Rule XVI: highest Forensic Ladder phase reached this heartbeat. Values: 1 (Perimeter), 2 (Engagement / Phase 2b WAI check), 3 (Flashbulb POI snapshot), 4 (Hill-Climb), 5 (Notarization).';

COMMENT ON COLUMN app_private.pudo_decision_context.poi_source IS
  'Rule XVI: where Phase 3 POI data came from. Values: local_cache, google_live, none (Phase 3 not reached).';

COMMENT ON COLUMN app_private.pudo_decision_context.peak_confidence IS
  'Rule XVI: peak WAI confidence observed during Phase 4 hill-climb at notarization time.';

COMMENT ON COLUMN app_private.pudo_decision_context.decay_samples IS
  'Rule XVI: count of Phase 4 samples that fell below the running peak. For future GPS-quality analysis.';

COMMENT ON COLUMN app_private.pudo_decision_context.unmatched_reason IS
  'Rule XVI: when match_signal=no_match, which gate failed. Values: tad_failed, wai_below_floor, both_failed, empty_queue.';

-- ─── driver_trip_state: cross-heartbeat state for the Forensic Ladder ──────

ALTER TABLE app_private.driver_trip_state
  ADD COLUMN arrest_counter_s    real,
  ADD COLUMN arrest_started_at   timestamptz,
  ADD COLUMN phase_current       smallint,
  ADD COLUMN peak_sample         jsonb;

COMMENT ON COLUMN app_private.driver_trip_state.arrest_counter_s IS
  'Rule XVI: accumulated zero-velocity seconds for the current stop. NULL or 0 when driver is moving.';

COMMENT ON COLUMN app_private.driver_trip_state.arrest_started_at IS
  'Rule XVI: timestamp when the current zero-velocity window began. NULL when driver is moving.';

COMMENT ON COLUMN app_private.driver_trip_state.phase_current IS
  'Rule XVI: current Forensic Ladder phase for this driver. Values: 1-5. NULL = not yet evaluated.';

COMMENT ON COLUMN app_private.driver_trip_state.peak_sample IS
  'Rule XVI: in-progress Phase 4 peak tracking. Shape: {"lat":..., "lng":..., "confidence":..., "captured_at":"ISO-8601"}. NULL when not in Phase 4.';

-- ─── Verification ──────────────────────────────────────────────────────────

-- Confirm all 14 new columns are present and nullable
DO $$
DECLARE
    pdc_count integer;
    dts_count integer;
BEGIN
    SELECT count(*) INTO pdc_count
    FROM information_schema.columns
    WHERE table_schema = 'app_private'
      AND table_name = 'pudo_decision_context'
      AND column_name IN (
        'arrest_started_at', 'arrest_duration_s', 'matched_offer_id',
        'match_signal', 'matcher_candidates', 'phase_reached',
        'poi_source', 'peak_confidence', 'decay_samples', 'unmatched_reason'
      )
      AND is_nullable = 'YES';

    IF pdc_count != 10 THEN
        RAISE EXCEPTION 'pudo_decision_context migration failed: expected 10 new nullable columns, got %', pdc_count;
    END IF;

    SELECT count(*) INTO dts_count
    FROM information_schema.columns
    WHERE table_schema = 'app_private'
      AND table_name = 'driver_trip_state'
      AND column_name IN (
        'arrest_counter_s', 'arrest_started_at', 'phase_current', 'peak_sample'
      )
      AND is_nullable = 'YES';

    IF dts_count != 4 THEN
        RAISE EXCEPTION 'driver_trip_state migration failed: expected 4 new nullable columns, got %', dts_count;
    END IF;

    RAISE NOTICE 'Migration verified: 10 pudo_decision_context columns + 4 driver_trip_state columns, all nullable';
END $$;

COMMIT;

-- ============================================================================
-- ROLLBACK (run only if reverting B-1)
-- ============================================================================
--
-- BEGIN;
--
-- ALTER TABLE app_private.pudo_decision_context
--   DROP COLUMN arrest_started_at,
--   DROP COLUMN arrest_duration_s,
--   DROP COLUMN matched_offer_id,
--   DROP COLUMN match_signal,
--   DROP COLUMN matcher_candidates,
--   DROP COLUMN phase_reached,
--   DROP COLUMN poi_source,
--   DROP COLUMN peak_confidence,
--   DROP COLUMN decay_samples,
--   DROP COLUMN unmatched_reason;
--
-- ALTER TABLE app_private.driver_trip_state
--   DROP COLUMN arrest_counter_s,
--   DROP COLUMN arrest_started_at,
--   DROP COLUMN phase_current,
--   DROP COLUMN peak_sample;
--
-- COMMIT;
