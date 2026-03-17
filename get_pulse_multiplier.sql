-- get_pulse_multiplier.sql
-- PuddleJumper Pulse: Real-time demand premium from offer velocity
--
-- Two-tier approach:
--   1. LOCAL (hex_pulse): When p_pickup_hex is provided, calls the existing
--      hex_pulse() function which uses exponential decay (lambda=0.00385)
--      across the hex + k-ring(1). Market-wide signal (all drivers).
--      Maps the decay-weighted pickup score to a multiplier.
--
--   2. GLOBAL (median gap): Falls back to driver-specific median inter-offer
--      gap when hex is not provided or hex_pulse has insufficient data.
--
-- Returns 1.0 (neutral) when insufficient data or mid-range velocity.
-- Returns > 1.0 when market is hot (be pickier).
-- Returns < 1.0 when market is dead (be less picky).

CREATE OR REPLACE FUNCTION app_private.get_pulse_multiplier(
    p_driver_id text,
    p_pickup_hex text DEFAULT NULL,
    p_window_minutes integer DEFAULT 15,
    p_min_samples integer DEFAULT 3
)
RETURNS numeric AS $$
DECLARE
    v_pickup_score  numeric;
    v_dropoff_score numeric;
    v_offer_count   integer;
    v_median_gap    numeric;
    v_sample_count  integer;
BEGIN
    -- ================================================================
    -- TIER 1: LOCAL DEMAND via hex_pulse (market-wide, decay-weighted)
    -- Uses existing hex_pulse() which looks at ALL drivers' offers in
    -- the hex + k-ring(1) with exponential time decay.
    -- ================================================================
    IF p_pickup_hex IS NOT NULL THEN
        BEGIN
            SELECT hp.pickup_pulse, hp.dropoff_pulse, hp.offer_count
            INTO v_pickup_score, v_dropoff_score, v_offer_count
            FROM app_private.hex_pulse(p_pickup_hex) hp;

            IF v_offer_count >= p_min_samples THEN
                RETURN CASE
                    WHEN v_pickup_score >= 4.0 THEN 1.35  -- GUSHING
                    WHEN v_pickup_score >= 2.0 THEN 1.20  -- HOT
                    WHEN v_pickup_score >= 0.8 THEN 1.00  -- WARM
                    WHEN v_pickup_score >= 0.2 THEN 0.90  -- COOL
                    ELSE 0.80                              -- DEAD
                END;
            END IF;
        EXCEPTION WHEN OTHERS THEN
            -- hex_pulse failed (bad hex, missing table, etc.) — fall through
            NULL;
        END;
    END IF;

    -- ================================================================
    -- TIER 2: GLOBAL DEMAND via median inter-offer gap (driver-specific)
    -- Counts gaps between consecutive offers where the driver was
    -- available (previous verdict = DECLINE). Pure velocity signal.
    -- ================================================================
    WITH recent_offers AS (
        SELECT
            created_at,
            decision_result->>'verdict' AS verdict,
            LAG(created_at) OVER (ORDER BY created_at) AS prev_at,
            LAG(decision_result->>'verdict') OVER (ORDER BY created_at) AS prev_verdict
        FROM app_private.decision_log
        WHERE driver_id = p_driver_id
          AND created_at > NOW() - (p_window_minutes || ' minutes')::interval
    ),
    valid_gaps AS (
        SELECT EXTRACT(EPOCH FROM (created_at - prev_at)) AS gap_sec
        FROM recent_offers
        WHERE prev_verdict = 'DECLINE'
          AND prev_at IS NOT NULL
          AND EXTRACT(EPOCH FROM (created_at - prev_at)) < 300
    )
    SELECT
        percentile_cont(0.5) WITHIN GROUP (ORDER BY gap_sec),
        count(*)
    INTO v_median_gap, v_sample_count
    FROM valid_gaps;

    -- Not enough data: return neutral
    IF v_sample_count < p_min_samples OR v_median_gap IS NULL THEN
        RETURN 1.0;
    END IF;

    -- Map median gap to multiplier
    --   < 30s   GUSHING  → 1.35 (be very picky, pipeline is fire)
    --   30-60s  HOT      → 1.20 (be picky, next offer is close)
    --   60-120s WARM     → 1.00 (neutral, standard thresholds)
    --   120-300s COOL    → 0.90 (relax, offers are slowing)
    --   > 300s  DEAD     → 0.80 (take what you can get)
    RETURN CASE
        WHEN v_median_gap < 30  THEN 1.35
        WHEN v_median_gap < 60  THEN 1.20
        WHEN v_median_gap < 120 THEN 1.00
        WHEN v_median_gap < 300 THEN 0.90
        ELSE 0.80
    END;
END;
$$ LANGUAGE plpgsql;