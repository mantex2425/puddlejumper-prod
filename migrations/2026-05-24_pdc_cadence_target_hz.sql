-- migrations/2026-05-24_pdc_cadence_target_hz.sql
--
-- Add cadence_target_hz column to pudo_decision_context for forensic
-- persistence of the §XVI.C amendment's cadence hint.
--
-- Motivation: cadence_target_hz is computed every heartbeat per the
-- §XVI.C amendment (2026-05-22) but is only emitted on the response wire,
-- never persisted. Without this column, cadence behavior can only be
-- reconstructed from live adb logcat capture. Forensic chain incomplete.
--
-- With this column, every PDC row captures the hint that was emitted to
-- the client at that heartbeat's moment. This enables time-correlated
-- analysis of cadence transitions vs. matcher state, vs. WAI confidence,
-- vs. PUDO commits — all via SQL after the fact.
--
-- Semantics:
--   NULL on pre-migration rows (heartbeats before 2026-05-24 deploy)
--   1.0 when server emitted Horny cadence hint to client
--   0.2 when server emitted cold cadence hint to client
--   Other float values reserved for future cadence tiers
--
-- Companion to:
--   - §XVI.C amendment (commits 298864a, 8f351d6)
--   - migrations/2026-05-22_xvi_c_tad_as_input.sql (write-freeze tripwires)
--   - Apply Script 5 (apply_cadence_target_hz_pdc_forensic_2026-05-24.py)
--
-- Idempotent: IF NOT EXISTS guard on ADD COLUMN. Safe to re-run.

-- ── Add the column ─────────────────────────────────────────────────
ALTER TABLE app_private.pudo_decision_context
  ADD COLUMN IF NOT EXISTS cadence_target_hz REAL;


-- ── Document semantics via COMMENT ON COLUMN ───────────────────────
COMMENT ON COLUMN app_private.pudo_decision_context.cadence_target_hz IS
  'Cadence hint emitted to the Android client in the heartbeat response '
  'at this heartbeat moment. Added 2026-05-24 for forensic capture of '
  '§XVI.C amendment cadence behavior. Values: 1.0 (Horny — server '
  'detected WAI ≥ 0.40 AND speed_mph < 5.0, client should heartbeat at '
  '1Hz), 0.2 (cold — normal cruising, client should heartbeat at 5s), '
  'NULL (pre-2026-05-24 rows). Server emission is stateless per heartbeat; '
  'this column records what was actually sent to the client. Compare with '
  'PDC.matched_offer_id and PDC.match_signal columns to analyze cadence '
  'behavior vs. matcher state correlation.';


-- ── Verification query ─────────────────────────────────────────────
-- Run after applying to confirm column exists with correct type:
--
--   SELECT column_name, data_type, is_nullable
--   FROM information_schema.columns
--   WHERE table_schema = 'app_private'
--     AND table_name = 'pudo_decision_context'
--     AND column_name = 'cadence_target_hz';
--
-- Expected: one row, cadence_target_hz | real | YES
