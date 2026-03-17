CREATE OR REPLACE FUNCTION app_private.hex_pulse(p_hex text)
RETURNS TABLE (
    pickup_pulse numeric(5,2),
    dropoff_pulse numeric(5,2),
    offer_count integer
) AS $$
DECLARE
    lambda numeric := 0.00385;
    cutoff_interval interval := INTERVAL '20 minutes';
    ring_discount numeric := 0.7;
BEGIN
    RETURN QUERY
    WITH target_hexes AS (
        SELECT h3_grid_disk(p_hex::h3index, 1)::text AS hex
    ),
    recent_offers AS (
        SELECT
            d.id,
            d.created_at,
            d.pickup_h3_index,
            d.dropoff_h3_index,
            EXTRACT(EPOCH FROM (NOW() - d.created_at)) AS age_sec
        FROM app_private.decision_log d
        WHERE d.created_at > NOW() - cutoff_interval
          AND (
              d.pickup_h3_index = ANY(SELECT hex FROM target_hexes)
              OR d.dropoff_h3_index = ANY(SELECT hex FROM target_hexes)
          )
    )
    SELECT
        COALESCE(SUM(
            CASE WHEN r.pickup_h3_index = ANY(SELECT t.hex FROM target_hexes t)
                THEN EXP(-lambda * r.age_sec) *
                    CASE WHEN r.pickup_h3_index = p_hex THEN 1.0 ELSE ring_discount END
                ELSE 0
            END
        ), 0)::numeric(5,2),
        COALESCE(SUM(
            CASE WHEN r.dropoff_h3_index = ANY(SELECT t.hex FROM target_hexes t)
                THEN EXP(-lambda * r.age_sec) *
                    CASE WHEN r.dropoff_h3_index = p_hex THEN 1.0 ELSE ring_discount END
                ELSE 0
            END
        ), 0)::numeric(5,2),
        COUNT(*)::integer
    FROM recent_offers r;
END;
$$ LANGUAGE plpgsql STABLE;