-- migrations/2026-05-17_motion_gate_writefreeze.sql
--
-- Phase 2a tripwire: COMMENT ON COLUMN for the four motion_gate-era columns
-- in app_private.pudo_decision_context. As of commit 93dc542 these columns
-- are WRITE-FROZEN — production code writes NULL into all four positions.
-- Historical rows (pre-2026-05-17) retain their data and are still used by
-- regression tests (tests/test_lost_mode_houston_playback_live.py reads
-- odometer_gate_result for §XVIII Houston Playback assertions).
--
-- Why no DROP COLUMN?
--   1. test_lost_mode_houston_playback_live.py issues a live SELECT against
--      odometer_gate_result. Dropping the column breaks a passing test.
--   2. ~62% of historical rows (113,264 of 183,373 as of 2026-05-17 evening)
--      have motion_gate_result populated. Analytical value unclear yet.
--   3. Rule VII says do the right thing, not the easy thing. Right thing is
--      preserve the historical signal and document the freeze. DROP can be
--      revisited in a future session after downstream consumers are
--      enumerated and the test migration is authored.
--
-- Why COMMENT ON COLUMN?
--   PostgreSQL surfaces COMMENT ON COLUMN data in `psql \d` output, in
--   information_schema.columns.column_comment, in pg_dump output, and in
--   any ORM introspection. A future developer who runs \d on
--   pudo_decision_context sees the write-freeze status alongside the
--   column type. This closes the silent-analytics-corruption risk where
--   somebody queries one of these columns assuming it's live, gets data
--   on pre-2026-05-17 rows and NULL on post-, and produces wrong analysis.
--
-- Idempotency: COMMENT ON COLUMN always overwrites. Safe to re-run.
--
-- Validation: after running, `\d+ app_private.pudo_decision_context`
-- in psql will display the Description column with the WRITE-FROZEN text
-- next to each of the four columns.
--
-- Forensic chain:
--   Bug discovered: 2026-05-17 evening session via offer 7938 phantom
--     dropoff diagnostic (Cloud Run log 19:39:20.276Z [heartbeat]
--     FireDropoffObservation offer=7938, sandwiched between two
--     [heartbeat] no_match log entries).
--   Code purge committed: 93dc542 (phase-2c-2-tad-exit-4tools)
--   Deploy: revision puddlejumper-api-00615-9s6
--   Tripwires (this file): Phase 2a of motion_gate purge follow-up


-- ── 1. motion_gate_result (text) ──────────────────────────────────────
-- Was: motion_verdict ("closed" | "moving" | "transient" | "no_cluster")
-- Now: NULL on all new rows (post-93dc542).
COMMENT ON COLUMN app_private.pudo_decision_context.motion_gate_result IS
  'WRITE-FROZEN 2026-05-17 (commit 93dc542). motion_gate.py purged per §XIV.A '
  'Naked-List Contract. Pre-93dc542 rows retain historical motion_verdict values '
  '(closed/moving/transient/no_cluster). New rows write NULL. Analytical queries '
  'must scope by created_at relative to 2026-05-17 18:00 UTC to avoid mixing '
  'epoch boundaries.';


-- ── 2. odometer_gate_result (jsonb) ───────────────────────────────────
-- Was: motion_gate jsonb_payload() — per-leg odometer eligibility blob.
-- Now: NULL on all new rows.
-- Still READ by: tests/test_lost_mode_houston_playback_live.py for
-- §XVIII Houston Playback regression assertions against historical data.
COMMENT ON COLUMN app_private.pudo_decision_context.odometer_gate_result IS
  'WRITE-FROZEN 2026-05-17 (commit 93dc542). motion_gate.py purged per §XIV.A '
  'Naked-List Contract. Pre-93dc542 rows retain per-leg odometer eligibility '
  'blobs. New rows write NULL. test_lost_mode_houston_playback_live.py reads '
  'this column for §XVIII regression assertions against historical data — do '
  'not migrate that test away from this column without verifying replacement '
  'data source preserves the same forensic fidelity.';


-- ── 3. gate_held_offer_ids (text[]) ───────────────────────────────────
-- Was: gate_verdict.held_offer_ids_and_legs()[0]
-- Now: NULL on all new rows.
COMMENT ON COLUMN app_private.pudo_decision_context.gate_held_offer_ids IS
  'WRITE-FROZEN 2026-05-17 (commit 93dc542). motion_gate.py purged per §XIV.A '
  'Naked-List Contract. Pre-93dc542 rows retain held-offer-ids arrays from '
  'motion_gate evaluation. New rows write NULL. Companion to gate_held_legs '
  '(positional pairing).';


-- ── 4. gate_held_legs (text[]) ────────────────────────────────────────
-- Was: gate_verdict.held_offer_ids_and_legs()[1]
-- Now: NULL on all new rows.
COMMENT ON COLUMN app_private.pudo_decision_context.gate_held_legs IS
  'WRITE-FROZEN 2026-05-17 (commit 93dc542). motion_gate.py purged per §XIV.A '
  'Naked-List Contract. Pre-93dc542 rows retain held-legs arrays from '
  'motion_gate evaluation. New rows write NULL. Companion to gate_held_offer_ids '
  '(positional pairing).';


-- ── Verification query ────────────────────────────────────────────────
-- Run after applying to confirm all four comments are set:
--
--   SELECT column_name,
--          col_description(
--            'app_private.pudo_decision_context'::regclass,
--            ordinal_position
--          ) AS comment
--   FROM information_schema.columns
--   WHERE table_schema = 'app_private'
--     AND table_name = 'pudo_decision_context'
--     AND column_name IN ('motion_gate_result',
--                         'odometer_gate_result',
--                         'gate_held_offer_ids',
--                         'gate_held_legs')
--   ORDER BY ordinal_position;
--
-- Expected: four rows, each with a WRITE-FROZEN comment starting with the
-- text 'WRITE-FROZEN 2026-05-17 (commit 93dc542)'.
