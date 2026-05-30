-- WAI per-offer signal-score capture for pudo_decision_context.
--
-- Added 2026-05-30 to close a forensic gap surfaced by the 12:22:38 /
-- 13:59:31 starved-pickup recon (see
-- docs/RECON_ROAD_NAME_CANONICALIZATION_GAP_2026-05-30.md §4.2): the
-- existing wai_* flat columns on pudo_decision_context only populate
-- for the WINNING outcome of a heartbeat's WAI evaluation. Offers that
-- scored below WAI_CONFIDENCE_THRESHOLD (or below the winning
-- candidate) leave no trace — making `wai_below_floor` and
-- `lost_mode_no_candidate` failure modes opaque without re-running WAI
-- against historical data.
--
-- This column captures all per-offer outcomes from
-- diagnostics.per_target_outcomes as a JSONB list. Each entry records
-- offer_id, leg, matched bool, confidence, per-signal breakdown
-- (proximity, breadcrumb_match, on_target_road, cluster_tightness,
-- cluster_duration, off_wire_pivot, adjacent_road_match), target
-- address, and reason. Enables diagnosis of why specific offers
-- failed to clear the floor without re-running WAI.
--
-- Additive, nullable, backward compatible.
-- Apply via apply_p19_schema_heartbeat.py-style runner at deploy time.

ALTER TABLE app_private.pudo_decision_context
    ADD COLUMN IF NOT EXISTS wai_per_offer_scores jsonb;
