-- P19: forensic anchor for cluster stillness runs.
--
-- Added 2026-05-20 alongside cluster_detection.py stillness refactor.
-- See docs/RFC_P19_CLUSTER_STILLNESS_REFACTOR.md section 11.4.
--
-- The column carries Cluster.started_at (MIN(logged_at) of the contiguous
-- speed=0 run that the planner observed at decision time). Enables:
--   - Double-fire detection (same driver_id + cluster_started_at + different planner_action)
--   - Departure_grace audit (when created_at - cluster_started_at > duration_s)
--   - Multi-cluster correlation (same current_offer_id across different cluster_started_at)
--
-- Additive, nullable, backward compatible. Already-applied to production
-- via apply_p19_schema_heartbeat.py at deploy time.

ALTER TABLE app_private.pudo_decision_context
    ADD COLUMN IF NOT EXISTS cluster_started_at timestamptz;
