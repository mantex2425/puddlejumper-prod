-- Voice dedup state for §IV Auto-Nail audio feedback.
-- 2026-05-27
--
-- Context: _voice_for_actions in driver_heartbeat.py needs to emit voice
-- for FireDropoffObservation / FirePickupObservation events (since §XVIII
-- lost-mode demotes nearly all PUDO events to Observation variants in the
-- current backlog state). Observations fire multiple times per
-- (offer, leg) within a short window; without dedup the driver would
-- hear the same utterance 2-3+ times per real PUDO event.
--
-- This migration adds two columns to track the last-voiced (offer_id,
-- action_type) tuple per driver. _voice_for_actions reads them as inputs
-- on each heartbeat and suppresses voice when the candidate utterance
-- matches the prior. The call site UPDATEs these columns when a voice
-- utterance fires.
--
-- Both columns are nullable text; NULL means "no prior voice" (fresh
-- driver session or post-deploy first heartbeat). Nullable preserves
-- backward compatibility with existing rows.
--
-- Idempotent via IF NOT EXISTS. Safe to re-run.

ALTER TABLE app_private.driver_trip_state
    ADD COLUMN IF NOT EXISTS last_voiced_offer_id text,
    ADD COLUMN IF NOT EXISTS last_voiced_action_type text;

COMMENT ON COLUMN app_private.driver_trip_state.last_voiced_offer_id IS
    'Offer ID of the last voice utterance emitted to this driver. NULL = no prior voice. Used by _voice_for_actions dedup gate to suppress repeated utterances within a Lost Mode Observation storm.';

COMMENT ON COLUMN app_private.driver_trip_state.last_voiced_action_type IS
    'Action type (pickup/dropoff/cancel/dropoff_missed_pickup) of the last voice utterance. Combined with last_voiced_offer_id as composite dedup key.';
