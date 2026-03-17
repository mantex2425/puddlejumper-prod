CREATE OR REPLACE FUNCTION app_private.get_pareto_frontier(
    user_id_in text,
    market_id_in text,
    days_back integer DEFAULT 28,
    mode_filter text DEFAULT NULL,
    time_period text DEFAULT NULL,
    min_accept_pct numeric DEFAULT 20.0,
    min_hourly_jump numeric DEFAULT 1.0,
    min_mileage_floor numeric DEFAULT 0.70,
    min_marginal_wage numeric DEFAULT 18.0 -- NEW: Don't work extra hours for less than this
)
RETURNS TABLE(
    test_hourly numeric,
    test_mileage numeric,
    would_accept bigint,
    total_offers bigint,
    accept_pct numeric,
    avg_hourly numeric,
    avg_dpm numeric,
    total_revenue numeric,
    est_hours_worked numeric,
    marginal_wage numeric, -- The pay rate for the "extra" work vs the previous option
    is_max_revenue boolean,
    is_max_hourly boolean,
    is_ai_pick boolean
)
LANGUAGE sql
STABLE
AS $$
    WITH base AS (
        SELECT 
            o.test_hourly,
            o.test_mileage,
            o.would_accept,
            o.total_offers,
            o.accept_pct,
            o.avg_hourly,
            o.avg_dpm,
            o.total_revenue
        FROM app_private.optimize_market_settings(
            user_id_in, market_id_in, days_back, mode_filter, time_period
        ) o
        WHERE o.total_revenue IS NOT NULL 
          AND o.avg_hourly IS NOT NULL
          AND o.accept_pct >= min_accept_pct
          AND o.test_mileage >= min_mileage_floor
    ),
    -- 1. Identify True Pareto Frontier
    pareto AS (
        SELECT b.*
        FROM base b
        WHERE NOT EXISTS (
            SELECT 1 FROM base other
            WHERE other.total_revenue >= b.total_revenue
              AND other.avg_hourly >= b.avg_hourly
              AND (other.total_revenue > b.total_revenue OR other.avg_hourly > b.avg_hourly)
        )
    ),
    -- 2. Calculate Marginal Wage vs the "Next Best" Option (lower revenue, higher hourly)
    ranked_calc AS (
        SELECT 
            p.*,
            -- Estimate hours worked (Total Revenue / Avg Hourly)
            (p.total_revenue / NULLIF(p.avg_hourly, 0)) as est_hours,
            
            -- Compare to the option with the next-lower revenue (which implies higher hourly)
            LAG(p.total_revenue) OVER (ORDER BY p.total_revenue ASC) as prev_revenue,
            LAG(p.total_revenue / NULLIF(p.avg_hourly, 0)) OVER (ORDER BY p.total_revenue ASC) as prev_hours
        FROM pareto p
    ),
    final_metrics AS (
        SELECT 
            r.*,
            r.est_hours - COALESCE(r.prev_hours, 0) as delta_hours,
            r.total_revenue - COALESCE(r.prev_revenue, 0) as delta_revenue,
            CASE 
                WHEN (r.est_hours - COALESCE(r.prev_hours, 0)) <= 0.01 THEN 999.0 -- Infinite wage (free money)
                ELSE (r.total_revenue - COALESCE(r.prev_revenue, 0)) / (r.est_hours - COALESCE(r.prev_hours, 0))
            END as calculated_marginal_wage
        FROM ranked_calc r
    ),
    -- 3. Pick the Winner
    tagged AS (
        SELECT 
            f.*,
            f.total_revenue = (SELECT MAX(total_revenue) FROM final_metrics) as _is_max_revenue,
            f.avg_hourly = (SELECT MAX(avg_hourly) FROM final_metrics) as _is_max_hourly,
            
            -- NEW AI PICK LOGIC:
            -- Select the option with the HIGHEST Revenue...
            -- ...that still maintains a Marginal Wage >= Threshold (e.g. $18/hr)
            -- ...relative to the "Sniper" baseline.
            RANK() OVER (
                ORDER BY 
                    CASE WHEN f.calculated_marginal_wage >= min_marginal_wage THEN f.total_revenue ELSE 0 END DESC
            ) as ai_rank
        FROM final_metrics f
    )
    SELECT 
        t.test_hourly,
        t.test_mileage,
        t.would_accept,
        t.total_offers,
        t.accept_pct,
        t.avg_hourly,
        t.avg_dpm,
        t.total_revenue,
        ROUND(t.est_hours, 1) as est_hours_worked,
        ROUND(t.calculated_marginal_wage, 2) as marginal_wage,
        t._is_max_revenue as is_max_revenue,
        t._is_max_hourly as is_max_hourly,
        (t.ai_rank = 1) as is_ai_pick
    FROM tagged t
    ORDER BY t.total_revenue DESC;
$$;