-- =============================================================================
-- Phase 2c.2 schema migration
-- =============================================================================
--
-- Sprint:   phase-2c-2-tad-exit-4tools
-- Branch:   off f3f5dc9 on demolition-2026-05-04
-- Date:     2026-05-07
-- Spec:     docs/PHASE_2C_2_SPRINT_BIBLE.md
--
-- Purpose:
--   Schema foundation for Phase 2c.2's TAD bouncer (Time And Distance candidate
--   filter), exit velocity primitive, and forensic black-box recorder.
--
-- Idempotency:
--   All ADD COLUMN statements use IF NOT EXISTS. Safe to re-run.
--   Wrapped in BEGIN/COMMIT for atomicity — partial application is impossible.
--
-- Apply:
--   psql -h 10.128.0.2 -U postgres -d puddlejumper -f tmp/phase_2c_2_schema.sql
--
-- Verify:
--   See "Verification queries" at bottom of file.
--
-- Rollback:
--   This migration is additive only. No data loss risk.
--   To revert: ALTER TABLE ... DROP COLUMN ... for each column added below.
--   Note: dropping tad_decision_context loses forensic data; do not roll back
--   without exporting first.
-- =============================================================================

BEGIN;

-- =============================================================================
-- offer_history: TAD expectation columns (4)
-- =============================================================================
--
-- Computed at offer receipt by tad.compute_offer_expectations() and persisted
-- by decisions/router.py. These are the "what we expected to happen" snapshot
-- against which cluster evaluations are scored.
--
-- All four are nullable: defensive — if compute fails or upstream data is
-- missing, the offer still records but TAD evaluation skips for this offer.
--
-- Pricing decisions never depend on these columns; they're a TAD-bouncer
-- input only.
-- =============================================================================

ALTER TABLE app_private.offer_history
  ADD COLUMN IF NOT EXISTS expected_pickup_arrival_time timestamptz;
COMMENT ON COLUMN app_private.offer_history.expected_pickup_arrival_time IS
  'Phase 2c.2 TAD: when driver is expected to arrive at pickup. Computed at '
  'offer receipt by tad.compute_offer_expectations(). Used by tad.evaluate_tad_'
  'gate() to score cluster_time vs expected for the time-signal soft boost. '
  'Idle case: now + (pickup_minutes * 60). Stacked case: chained from prev '
  'offer''s expected_dropoff_arrival_time.';

ALTER TABLE app_private.offer_history
  ADD COLUMN IF NOT EXISTS expected_pickup_distance numeric;
COMMENT ON COLUMN app_private.offer_history.expected_pickup_distance IS
  'Phase 2c.2 TAD: cumulative odometer (miles) at which driver is expected '
  'to arrive at pickup. Computed at offer receipt. Distance gate (hard): '
  'cluster current_odometer >= 0.85 * expected_pickup_distance for trips '
  '>= 2mi, OR within ±0.5mi absolute for short trips. Idle case: '
  'current_odometer + pickup_miles. Stacked case: chained from prev offer''s '
  'expected_dropoff_distance.';

ALTER TABLE app_private.offer_history
  ADD COLUMN IF NOT EXISTS expected_dropoff_arrival_time timestamptz;
COMMENT ON COLUMN app_private.offer_history.expected_dropoff_arrival_time IS
  'Phase 2c.2 TAD: when driver is expected to arrive at dropoff. Equals '
  'expected_pickup_arrival_time + (trip_minutes * 60). Used for dropoff-side '
  'TAD evaluation. Cancellation detection: if a new offer arrives with this '
  'value already in the past, the previous offer is treated as orphaned.';

ALTER TABLE app_private.offer_history
  ADD COLUMN IF NOT EXISTS expected_dropoff_distance numeric;
COMMENT ON COLUMN app_private.offer_history.expected_dropoff_distance IS
  'Phase 2c.2 TAD: cumulative odometer (miles) at which driver is expected '
  'to arrive at dropoff. Equals expected_pickup_distance + trip_miles. '
  'Distance gate fires same as pickup but against this anchor.';

-- =============================================================================
-- offer_history: exit velocity columns (3)
-- =============================================================================
--
-- Written by driver_heartbeat.py post-pickup-confirmation. The pickup_exit_*
-- pair anchors the next leg's TAD math correctly — without these, stacked
-- offers compound timing error from passenger-loading delays.
--
-- Sticky semantics: once pickup_exit_time IS NOT NULL OR
-- exit_velocity_timeout = TRUE, the heartbeat handler stops re-evaluating.
-- See escape_detection.check_exit_velocity() for trigger logic.
-- =============================================================================

ALTER TABLE app_private.offer_history
  ADD COLUMN IF NOT EXISTS pickup_exit_time timestamptz;
COMMENT ON COLUMN app_private.offer_history.pickup_exit_time IS
  'Phase 2c.2 exit velocity: heartbeat timestamp at which driver is detected '
  'to have left pickup cluster. Trigger: distance > 150m from cluster '
  'centroid OR sustained speed > 15mph for 30s. NULL until detected. Used as '
  'time anchor for next leg''s expected_dropoff_arrival_time when chaining '
  'stacked offers.';

ALTER TABLE app_private.offer_history
  ADD COLUMN IF NOT EXISTS pickup_exit_odometer numeric;
COMMENT ON COLUMN app_private.offer_history.pickup_exit_odometer IS
  'Phase 2c.2 exit velocity: cumulative odometer (miles) at exit detection. '
  'Used as distance anchor for next leg''s expected_dropoff_distance. NULL '
  'until detected. Note: this is MILES; the 150m distance trigger inside '
  'escape_detection.check_exit_velocity() uses METERS (haversine). Do not '
  'mix units.';

ALTER TABLE app_private.offer_history
  ADD COLUMN IF NOT EXISTS exit_velocity_timeout boolean NOT NULL DEFAULT FALSE;
COMMENT ON COLUMN app_private.offer_history.exit_velocity_timeout IS
  'Phase 2c.2 exit velocity: TRUE if 30 minutes elapsed post-pickup-'
  'confirmation without exit detection (festival/rodeo gridlock case). '
  'When TRUE: subsequent TAD evaluations on this offer''s queue use '
  'pickup_confirmation_time as time anchor and force time_signal boost = 0. '
  'Distance gate still applies normally (odometer remains valid).';

-- =============================================================================
-- pudo_decision_context: TAD forensic blob (1)
-- =============================================================================
--
-- Self-contained black-box recorder for one cluster evaluation. Captures
-- the TAD bouncer's per-candidate verdicts, the time-signal boost decisions,
-- and the spatial matcher's final ruling under the elevator rule.
--
-- One row per cluster evaluation. NULL when the row predates Phase 2c.2 or
-- when the cluster was rejected before TAD ran (e.g., motion gate failed).
--
-- Mirrors the pattern of odometer_gate_result jsonb (gate-layer sprint).
-- See pudo_decision_context.tad_decision_context structure below.
-- =============================================================================

ALTER TABLE app_private.pudo_decision_context
  ADD COLUMN IF NOT EXISTS tad_decision_context jsonb;

COMMENT ON COLUMN app_private.pudo_decision_context.tad_decision_context IS
$tad$Phase 2c.2 forensic black-box. JSONB structure:

{
  "evaluated_at": "ISO8601 timestamp UTC",
  "cluster_centroid": [lat, lng],
  "current_odometer_miles": numeric,
  "candidates": [
    {
      "offer_id": "uuid",
      "location_type": "pickup" | "dropoff",
      "tad_verdict": "passed" | "failed",
      "distance_gate": {
        "mode": "percentage" | "absolute_short_trip",
        "expected_distance_miles": numeric,
        "actual_delta_miles": numeric,
        "completion_pct": numeric (mode=percentage only),
        "tolerance_miles": numeric (mode=absolute_short_trip only),
        "passed": bool,
        "fail_reason": string (if passed=false)
      },
      "time_signal": null | {
        "expected_arrival_time": "ISO8601",
        "cluster_time": "ISO8601",
        "error_pct": numeric,
        "boost": 0.15 | 0.05 | 0.0,
        "applied": bool (false if exit_velocity_timeout was set)
      },
      "matcher_results": null | {
        "weighted_confidence": numeric,
        "poi_type_match": bool,
        "elevator_triggered": bool,
        "final_verdict": "COMMIT" | "SKIP"
      }
    }
  ],
  "summary": {
    "candidates_total": int,
    "candidates_passed_tad": int,
    "candidates_committed": int
  }
}

Field semantics:
  - distance_gate.mode: "percentage" for trips >= 2mi (gate is 0.85 * expected); "absolute_short_trip" for trips < 2mi (gate is +/-0.5mi tolerance).
  - time_signal: null when distance_gate.passed = false (no point computing time error for a TAD-rejected candidate).
  - time_signal.boost: +0.15 if error_pct <= 15%; +0.05 if <= 50%; 0.0 otherwise. Never negative (no penalty for late arrivals).
  - matcher_results: null when tad_verdict = "failed" (spatial matcher never ran). Populated when TAD passed.
  - matcher_results.elevator_triggered: TRUE when weighted_confidence < 0.90 AND >= 0.80 AND poi_type_match = TRUE.
  - matcher_results.final_verdict: "COMMIT" if (weighted >= 0.90) OR (weighted >= 0.80 AND poi_type_match TRUE); else "SKIP".

Phase 2g tuning queries:
  - Slice short-trip behavior:
    WHERE tad_decision_context->'candidates'->0->'distance_gate'->>'mode' = 'absolute_short_trip'
  - Find elevator rescues:
    WHERE EXISTS (SELECT 1 FROM jsonb_array_elements(tad_decision_context->'candidates') c
          WHERE c->'matcher_results'->>'elevator_triggered' = 'true')
  - Find near-miss skips for threshold tuning:
    WHERE EXISTS (SELECT 1 FROM jsonb_array_elements(tad_decision_context->'candidates') c
          WHERE c->'matcher_results'->>'final_verdict' = 'SKIP'
          AND (c->'matcher_results'->>'weighted_confidence')::numeric BETWEEN 0.7 AND 0.89)$tad$;

COMMIT;

-- =============================================================================
-- Verification queries (run after COMMIT to confirm migration landed cleanly)
-- =============================================================================
--
-- 1. Confirm all 7 offer_history columns present:
--
--    SELECT column_name, data_type, is_nullable, column_default
--    FROM information_schema.columns
--    WHERE table_schema = 'app_private'
--      AND table_name = 'offer_history'
--      AND column_name IN (
--        'expected_pickup_arrival_time', 'expected_pickup_distance',
--        'expected_dropoff_arrival_time', 'expected_dropoff_distance',
--        'pickup_exit_time', 'pickup_exit_odometer', 'exit_velocity_timeout'
--      )
--    ORDER BY column_name;
--
--    Expected: 7 rows. exit_velocity_timeout has default 'false', NOT NULL.
--    All others nullable, no default.
--
-- 2. Confirm tad_decision_context column present:
--
--    SELECT column_name, data_type
--    FROM information_schema.columns
--    WHERE table_schema = 'app_private'
--      AND table_name = 'pudo_decision_context'
--      AND column_name = 'tad_decision_context';
--
--    Expected: 1 row, data_type = 'jsonb'.
--
-- 3. Confirm column comments are queryable:
--
--    SELECT col_description(
--      'app_private.pudo_decision_context'::regclass,
--      (SELECT ordinal_position FROM information_schema.columns
--       WHERE table_schema = 'app_private'
--         AND table_name = 'pudo_decision_context'
--         AND column_name = 'tad_decision_context')
--    );
--
--    Expected: long string starting with "Phase 2c.2 forensic black-box."
--
-- 4. Confirm zero existing rows have populated values (sanity check —
--    migration is purely additive, no backfill expected):
--
--    SELECT
--      COUNT(*) FILTER (WHERE expected_pickup_arrival_time IS NOT NULL) AS pre_filled_eta,
--      COUNT(*) FILTER (WHERE pickup_exit_time IS NOT NULL) AS pre_filled_exit,
--      COUNT(*) FILTER (WHERE exit_velocity_timeout = TRUE) AS pre_filled_timeout
--    FROM app_private.offer_history;
--
--    Expected: 0, 0, 0. (Anything else means data corruption or a re-run
--    after partial Phase 2 work — investigate before proceeding.)
--
-- =============================================================================