-- DSI v1 schema — step 1 (Gemini-ratified 2026-06-02).
--
-- Adds the versioned, observational `dsi_v1` column to both offer tables.
-- Additive, nullable, idempotent (ADD COLUMN IF NOT EXISTS). This column is
-- READ-ONLY/observational — it is NOT wired into any Accept/Decline verdict.
--
-- NO BACKFILL in this pass: historical rows stay NULL until step 2 (a
-- separate, quarantined, row-level-error-logged migration).
--
-- Ref: docs/FEATURE_PROPOSAL_DSI_2026-06-02.md
BEGIN;

ALTER TABLE app_private.offer_history ADD COLUMN IF NOT EXISTS dsi_v1 double precision;
ALTER TABLE public.community_offers   ADD COLUMN IF NOT EXISTS dsi_v1 double precision;

COMMIT;
