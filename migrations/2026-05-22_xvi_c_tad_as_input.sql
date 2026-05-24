-- migrations/2026-05-22_xvi_c_tad_as_input.sql
--
-- §XVI.C amendment write-freeze tripwires (companion to commits landing
-- driver_heartbeat.py changes + CANONICAL_RULES.md doctrine sync).
--
-- As of the §XVI.C amendment (2026-05-22, ratified via Andrew + Claude +
-- Gemini paired-programming protocol), three columns in
-- app_private.pudo_decision_context have changed semantics:
--
--   1. phase_reached — WRITE-FROZEN. Pre-amendment rows retain values
--      1-5 mapping to §XVI Forensic Ladder phases. Post-amendment rows
--      write NULL. The §XVI Phase economy collapsed when TAD-as-gate
--      was removed (Phase 1/2 distinctions no longer load-bearing).
--      Derive equivalent forensic state from canonical fields:
--          matched_offer_id IS NOT NULL  → "committed"
--          unmatched_reason IS NOT NULL  → "matcher consulted"
--          cluster_lat IS NOT NULL       → "cluster diagnostics captured"
--
--   2. match_signal — TAXONOMY CHANGED. Pre-amendment rows retain the
--      historical values 'tad_and_wai' and 'tad_and_wai_ambiguous'.
--      Post-amendment rows write 'wai_above_floor' for single-match
--      commits ('dispatch_resolved' for §5.3 ambiguity is unchanged;
--      lost-mode values unchanged per §XVIII.D.1).
--
--   3. unmatched_reason — TAXONOMY COLLAPSED. Pre-amendment rows retain
--      'tad_failed' (TAD distance gate rejected the closest candidate).
--      Post-amendment rows that would have emitted 'tad_failed' now
--      emit 'wai_below_floor' unconditionally (TAD is no longer a gate,
--      so it can no longer fail as one). The 'wai_below_floor' label
--      is now the canonical non-lost-mode miss reason.
--
-- Why no DROP COLUMN?
--   Same discipline as the 2026-05-17 motion_gate write-freeze:
--     1. Historical rows retain analytical value — pre-amendment data
--        is used by regression tests (test_lost_mode_houston_playback_live
--        reads PDC columns for §XVIII Houston Playback assertions).
--     2. Rule VII says do the right thing, not the easy thing. Right
--        thing is preserve historical signal + document the change.
--     3. DROP can be revisited in a future session after downstream
--        consumers are enumerated and test migration is authored.
--
-- Why COMMENT ON COLUMN?
--   PostgreSQL surfaces COMMENT ON COLUMN data in `psql \d` output, in
--   information_schema.columns.column_comment, in pg_dump output, and
--   in any ORM introspection. A future developer who runs \d on
--   pudo_decision_context sees the write-freeze status and taxonomy
--   change alongside the column type. This closes the silent-analytics-
--   corruption risk where somebody queries one of these columns
--   assuming the old taxonomy/semantics and gets a misleading result.
--
-- Idempotency: COMMENT ON COLUMN always overwrites. Safe to re-run.
--
-- Validation: after running, `\d+ app_private.pudo_decision_context`
-- displays the Description column with the §XVI.C amendment annotation
-- next to each of the three columns.
--
-- Forensic chain:
--   §XVI.C amendment ratified: 2026-05-22 (Andrew + Claude + Gemini)
--   Apply Script 1 (code): driver_heartbeat.py removed TAD gate from
--     candidate-building loop; arrest threshold 6.0 → 5.0; taxonomy
--     emissions updated; cadence_target_hz added to heartbeat response
--   Apply Script 2 (doctrine): CANONICAL_RULES.md §XVI.C body rewrite,
--     §XVI.F Phase 2b commit gate, §XVI.G taxonomy annotations,
--     §XVIII.C.1 subsumption note; schema doc updates
--   Apply Script 3 (this file): COMMENT ON COLUMN tripwires
--
-- All three apply-scripts land in a single git commit; Cloud Run
-- deploy + DB migration run atomically as one unit.


-- ── 1. phase_reached (smallint) — WRITE-FROZEN ────────────────────
COMMENT ON COLUMN app_private.pudo_decision_context.phase_reached IS
  'WRITE-FROZEN 2026-05-22 (§XVI.C amendment). §XVI Phase economy '
  'collapsed when TAD-as-gate was removed. Pre-amendment rows retain '
  'values 1-5 mapping to historical Forensic Ladder phases. New rows '
  'write NULL. Derive equivalent forensic state from canonical fields: '
  'matched_offer_id IS NOT NULL (committed), unmatched_reason IS NOT '
  'NULL (matcher consulted), cluster_lat IS NOT NULL (cluster captured).';


-- ── 2. match_signal (text) — TAXONOMY CHANGED ─────────────────────
COMMENT ON COLUMN app_private.pudo_decision_context.match_signal IS
  'Post-§XVI.C amendment (2026-05-22) values: wai_above_floor '
  '(single-match commit), dispatch_resolved (§5.3 ambiguity), or one '
  'of the §XVIII lost-mode signals (lost_mode_observation, '
  'lost_mode_ambiguous_observation). Pre-amendment rows retain '
  'historical labels tad_and_wai and tad_and_wai_ambiguous; those '
  'rows remain forensically valid but represent the prior dual-gate '
  'doctrine where TAD was a separate gate. Analytical queries that '
  'distinguish doctrine eras should scope by created_at relative to '
  '2026-05-22 18:00 UTC.';


-- ── 3. unmatched_reason (text) — TAXONOMY COLLAPSED ───────────────
COMMENT ON COLUMN app_private.pudo_decision_context.unmatched_reason IS
  'Post-§XVI.C amendment (2026-05-22) canonical values for non-lost-'
  'mode misses: wai_below_floor (no offer cleared the WAI 0.40 floor), '
  'cluster_unavailable (cluster detector returned no cluster), '
  'queue_actually_empty (no live offers in queue per LIVE_OFFER_'
  'PREDICATE_SQL), odometer_unavailable (heartbeat lacked odometer). '
  'Lost-mode values: lost_mode_no_candidate, lost_mode_three_plus_'
  'matches (per §XVIII.D.2). Pre-amendment rows may carry the '
  'historical label tad_failed (TAD distance gate rejected the '
  'closest candidate) — that label is deprecated as of 2026-05-22 '
  'because TAD is no longer a gate; queries that previously filtered '
  'on tad_failed should now use wai_below_floor for post-amendment '
  'data.';


-- ── Verification query ────────────────────────────────────────────
-- Run after applying to confirm all three comments are set. Expected:
-- three rows, each Description column starting with the appropriate
-- §XVI.C amendment annotation text.
--
--   SELECT column_name,
--          col_description(
--            'app_private.pudo_decision_context'::regclass,
--            ordinal_position
--          ) AS comment
--   FROM information_schema.columns
--   WHERE table_schema = 'app_private'
--     AND table_name = 'pudo_decision_context'
--     AND column_name IN ('phase_reached',
--                         'match_signal',
--                         'unmatched_reason')
--   ORDER BY ordinal_position;
