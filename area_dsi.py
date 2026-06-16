"""DSI v1 area readout — IDW interpolation of the community_offers DSI surface.

OBSERVATIONAL ONLY: the value is shown under the frog and NEVER affects any
verdict. Interpolates dsi_v1 inverse-distance-weighted across BOTH space (H3 grid
distance from the driver's res-8 cell, via the same app_private.safe_h3 that wrote
pickup_h3) AND time (hour-of-day + weekday/weekend distance, market-local) — so the
readout reflects the local market at THIS time of day, not an all-hours average
(spec §1: "Sat night != Mon morning"). Spatially bounded to AREA_DSI_K_RINGS; the
temporal weight is SOFT (down-weights, never excludes) so sparse weekend/late-night
buckets degrade gracefully instead of starving the radar to NULL. Null when no
nearby data.

See dsi.py (per-offer DSI) and docs/FEATURE_PROPOSAL_DSI_2026-06-02-v2.md.
"""
import logging

log = logging.getLogger(__name__)

# k-ring radius over res-8 cells (~1.2 km across) -> ~6 km. Tunable.
AREA_DSI_K_RINGS = 5

# Empty-disk fallback: when the primary disk has NO eligible offers (an uncovered/edge
# area), widen to this radius once before giving up (~14 km). Graceful degradation —
# the decision rule needs a number; only a truly data-empty WIDE disk returns None, and
# the caller then falls back to the interim absolute threshold.
AREA_DSI_FALLBACK_K_RINGS = 12

# §thin-market gate (spec "sparse rows -> NULL", FEATURE_PROPOSAL_DSI_2026-06-02;
# documented-never-built until 2026-06-16). A local-market bar built on fewer than
# this many VALID offers is noise, not a market — interpolate_area_dsi returns None
# so the verdict falls back to the interim absolute threshold rather than declining
# a good offer against a ~1-sample bar. TUNABLE product param.
MIN_MARKET_POINTS = 3

# Temporal weighting (spec §1). The IDW weight folds a TIME distance in alongside the
# spatial H3-ring distance, so same-time-same-place offers dominate while distant-in-time
# offers still contribute (down-weighted, never excluded — SOFT, so sparse weekend/
# late-night buckets degrade gracefully rather than starving the radar to NULL).
#   temporal_distance = circular_hour_diff(0..12) + DAYTYPE_PENALTY (weekday<->weekend)
# scaled by TIME_WEIGHT into H3-ring-equivalents. Both are TUNABLE product params (spec
# §7: the weight family has no data-derived optimum). At 0.4 a 12h swing ~= 5 rings
# (= the disk radius); a weekday/weekend mismatch ~= 2.4 rings.
AREA_DSI_TIME_WEIGHT = 0.4
AREA_DSI_DAYTYPE_PENALTY = 6.0

# §6 read-filter: drop capture-error junk (effective_hourly_rate <= 0 OR
# dollars_per_mile <= 0) but KEEP legitimately-negative dsi_v1 — real money-losers are
# the core signal, so NEVER filter on dsi_v1 > 0. Declined offers are valid market signal.
_AREA_DSI_SQL = """
WITH cc AS (
    SELECT app_private.safe_h3(%s, %s)::h3index AS cell,
           EXTRACT(hour FROM now() AT TIME ZONE 'America/Chicago')::int       AS qhour,
           (EXTRACT(dow FROM now() AT TIME ZONE 'America/Chicago')::int IN (0, 6)) AS qweekend
)
SELECT sum(s.dsi_v1 * s.w) / NULLIF(sum(s.w), 0) AS area_dsi,
       count(*) AS n
FROM (
    SELECT co.dsi_v1,
        1.0 / (
            h3_grid_distance(co.pickup_h3::h3index, cc.cell)
            + %s * (
                LEAST(
                    abs(EXTRACT(hour FROM co.created_at AT TIME ZONE 'America/Chicago')::int - cc.qhour),
                    24 - abs(EXTRACT(hour FROM co.created_at AT TIME ZONE 'America/Chicago')::int - cc.qhour)
                )
                + CASE WHEN (EXTRACT(dow FROM co.created_at AT TIME ZONE 'America/Chicago')::int IN (0, 6)) <> cc.qweekend
                       THEN %s ELSE 0.0 END
            )
            + 1.0
        ) AS w
    FROM public.community_offers co
    JOIN cc ON TRUE
    JOIN (SELECT h3_grid_disk((SELECT cell FROM cc), %s) AS cell) disk
        ON co.pickup_h3::h3index = disk.cell
    WHERE co.dsi_v1 IS NOT NULL
      AND co.effective_hourly_rate > 0
      AND co.dollars_per_mile > 0
) s
"""


def interpolate_area_dsi(cur, lat, lng):
    """Return the time-aware IDW community DSI at (lat, lng), or None.

    Tries the primary disk (AREA_DSI_K_RINGS); on an EMPTY disk, widens once to
    AREA_DSI_FALLBACK_K_RINGS before giving up (graceful degradation — the decision
    rule needs a number). Best-effort: missing coords or any query failure return None
    so this can never break the heartbeat / decision (liveness over completeness; the
    caller falls back to the interim threshold on None).
    """
    if lat is None or lng is None:
        return None
    for k_rings in (AREA_DSI_K_RINGS, AREA_DSI_FALLBACK_K_RINGS):
        try:
            cur.execute(_AREA_DSI_SQL, (lat, lng, AREA_DSI_TIME_WEIGHT,
                                        AREA_DSI_DAYTYPE_PENALTY, k_rings))
            row = cur.fetchone()
            # RealDictCursor -> dict-like; fall back to positional for tuple cursors.
            if row is not None:
                area_dsi = row["area_dsi"] if hasattr(row, "keys") else row[0]
                n = (row["n"] if hasattr(row, "keys") else row[1]) or 0
            else:
                area_dsi, n = None, 0
            # §thin-market gate (2026-06-16): a bar built on fewer than
            # MIN_MARKET_POINTS valid offers is noise, not a market. Treat as
            # insufficient -> widen once; if the wide disk is also sparse, return
            # None so _dsi_verdict falls back to the interim absolute threshold
            # instead of declining a good offer against a ~1-sample bar (offer
            # 11402 Buffalo Speedway 2026-06-15: personal 28.4 vs a 29.4 n=1 bar).
            if area_dsi is not None and n >= MIN_MARKET_POINTS:
                return float(area_dsi)
            # empty OR thin disk: widen and retry (loop continues).
        except Exception as e:
            log.warning("[area_dsi] interpolation failed (k=%s): %s", k_rings, e)
            return None
    return None
