"""DSI v1 area readout — IDW interpolation of the community_offers DSI surface.

OBSERVATIONAL ONLY: the value is shown under the frog and NEVER affects any
verdict. Interpolates dsi_v1 (inverse-distance-weighted by H3 grid distance)
from community_offers whose pickup_h3 is within AREA_DSI_K_RINGS of the driver's
current res-8 H3 cell — the same cell function (app_private.safe_h3) that wrote
pickup_h3, so the resolution/convention match. Null when no nearby data.

See dsi.py (per-offer DSI) and docs/FEATURE_PROPOSAL_DSI_2026-06-02.md.
"""
import logging

log = logging.getLogger(__name__)

# k-ring radius over res-8 cells (~1.2 km across) -> ~6 km. Tunable.
AREA_DSI_K_RINGS = 5

_AREA_DSI_SQL = """
WITH cc AS (SELECT app_private.safe_h3(%s, %s)::h3index AS cell)
SELECT
    sum(co.dsi_v1 / (h3_grid_distance(co.pickup_h3::h3index, cc.cell) + 1.0))
    / NULLIF(sum(1.0 / (h3_grid_distance(co.pickup_h3::h3index, cc.cell) + 1.0)), 0)
        AS area_dsi
FROM public.community_offers co
JOIN cc ON TRUE
JOIN (SELECT h3_grid_disk((SELECT cell FROM cc), %s) AS cell) disk
    ON co.pickup_h3::h3index = disk.cell
WHERE co.dsi_v1 IS NOT NULL
"""


def interpolate_area_dsi(cur, lat, lng):
    """Return the IDW community DSI at (lat, lng), or None.

    Best-effort: missing coords or any query failure return None so this can
    never break the heartbeat (liveness over completeness).
    """
    if lat is None or lng is None:
        return None
    try:
        cur.execute(_AREA_DSI_SQL, (lat, lng, AREA_DSI_K_RINGS))
        row = cur.fetchone()
        if not row:
            return None
        # RealDictCursor -> dict-like; fall back to positional for tuple cursors.
        val = row["area_dsi"] if hasattr(row, "keys") else row[0]
        return float(val) if val is not None else None
    except Exception as e:
        log.warning("[area_dsi] interpolation failed: %s", e)
        return None
