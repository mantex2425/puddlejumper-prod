"""dsi_heatmap.py — Drive Score Index heatmap endpoint.

GET /api/v1/dsi/heatmap?lat=&lng=&k=  ->  GeoJSON FeatureCollection of res-8 H3 cells
around (lat,lng), each carrying the time-aware, §6-filtered IDW Drive Score Index and a
RELATIVE color tier. Tiers are percentiles of the CURRENT local spread (purple = top
quartile here-and-now, green, orange, yellow = bottom), so "purple" means best-around-
here-right-now rather than an absolute number — Tue-afternoon purple and Sat-night purple
are different DSI values (spec §1).

OBSERVATIONAL: read-only, never affects a verdict. Sibling of area_dsi.py (the single
point readout under the frog); this is the same space-time IDW run over a grid of cells.
"""
import logging

from flask import Blueprint, request, jsonify
from psycopg2.extras import RealDictCursor

from db import get_db
from utils import verify_and_get_user_id

log = logging.getLogger(__name__)
dsi_heatmap_bp = Blueprint("dsi_heatmap", __name__)

# res-8 rings to render (zoom). k=8 ~ 17 km across. Capped for query cost.
DISPLAY_K_DEFAULT = 8
DISPLAY_K_MAX = 16
# How far each display cell reaches for surface points (per-cell IDW radius).
IDW_K = 6
# Temporal weighting — identical knobs to area_dsi.py (spec §1; tunable).
TIME_W = 0.4
DAYTYPE_PEN = 6.0

_HEATMAP_SQL = """
WITH params AS (
    SELECT app_private.safe_h3(%(lat)s, %(lng)s)::h3index AS center,
           EXTRACT(hour FROM now() AT TIME ZONE 'America/Chicago')::int       AS qhour,
           (EXTRACT(dow FROM now() AT TIME ZONE 'America/Chicago')::int IN (0, 6)) AS qweekend
),
display_cells AS (
    SELECT h3_grid_disk((SELECT center FROM params), %(display_k)s) AS cell
),
surface AS (
    -- §6 read-filter: drop capture-error junk (ehr/dpm <= 0), keep negative dsi.
    -- Bounded to the render area + IDW reach so we don't scan the whole surface.
    SELECT co.pickup_h3::h3index AS scell, co.dsi_v1,
           EXTRACT(hour FROM co.created_at AT TIME ZONE 'America/Chicago')::int       AS oh,
           (EXTRACT(dow FROM co.created_at AT TIME ZONE 'America/Chicago')::int IN (0, 6)) AS owe
    FROM public.community_offers co
    WHERE co.dsi_v1 IS NOT NULL
      AND co.effective_hourly_rate > 0
      AND co.dollars_per_mile > 0
      AND h3_grid_distance(co.pickup_h3::h3index, (SELECT center FROM params))
            <= %(display_k)s + %(idw_k)s
),
interp AS (
    -- space-TIME IDW per display cell (cells with no nearby surface point drop out -> unrendered)
    SELECT d.cell,
           sum(s.dsi_v1 * w.weight) / NULLIF(sum(w.weight), 0) AS dsi,
           count(*) AS pts
    FROM display_cells d
    CROSS JOIN params p
    JOIN surface s ON h3_grid_distance(s.scell, d.cell) <= %(idw_k)s
    CROSS JOIN LATERAL (
        SELECT 1.0 / (
            h3_grid_distance(s.scell, d.cell)
            + %(time_w)s * (
                LEAST(abs(s.oh - p.qhour), 24 - abs(s.oh - p.qhour))
                + CASE WHEN s.owe <> p.qweekend THEN %(daytype_pen)s ELSE 0 END)
            + 1.0
        ) AS weight
    ) w
    GROUP BY d.cell
),
tiers AS (
    -- RELATIVE breakpoints from the current local spread (not absolute DSI)
    SELECT percentile_cont(0.25) WITHIN GROUP (ORDER BY dsi) AS p25,
           percentile_cont(0.50) WITHIN GROUP (ORDER BY dsi) AS p50,
           percentile_cont(0.75) WITHIN GROUP (ORDER BY dsi) AS p75
    FROM interp WHERE dsi IS NOT NULL
)
SELECT json_build_object(
    'type', 'FeatureCollection',
    'center', json_build_object('lat', %(lat)s, 'lng', %(lng)s),
    'cell_count', (SELECT count(*) FROM interp WHERE dsi IS NOT NULL),
    'breakpoints', (SELECT json_build_object(
        'p25', round(p25::numeric, 1), 'p50', round(p50::numeric, 1),
        'p75', round(p75::numeric, 1)) FROM tiers),
    'features', COALESCE(json_agg(json_build_object(
        'type', 'Feature',
        'geometry', ST_AsGeoJSON(h3_cell_to_boundary(i.cell)::geometry)::json,
        'properties', json_build_object(
            'h3', i.cell::text,
            'dsi', round(i.dsi::numeric, 1),
            'points', i.pts,
            'tier', CASE WHEN i.dsi >= t.p75 THEN 'purple'
                         WHEN i.dsi >= t.p50 THEN 'green'
                         WHEN i.dsi >= t.p25 THEN 'orange'
                         ELSE 'yellow' END
        )
    )) FILTER (WHERE i.dsi IS NOT NULL), '[]'::json)
) AS geojson
FROM interp i CROSS JOIN tiers t;
"""


@dsi_heatmap_bp.route("/heatmap", methods=["GET"])
def get_heatmap():
    """DSI heatmap GeoJSON around (lat, lng). Observational; best-effort."""
    try:
        verify_and_get_user_id(request)
    except Exception:
        return jsonify({"error": "Auth failed"}), 403

    try:
        lat = float(request.args["lat"])
        lng = float(request.args["lng"])
    except (KeyError, ValueError, TypeError):
        return jsonify({"error": "lat and lng query params required (floats)"}), 400
    try:
        display_k = min(max(int(request.args.get("k", DISPLAY_K_DEFAULT)), 1), DISPLAY_K_MAX)
    except (ValueError, TypeError):
        display_k = DISPLAY_K_DEFAULT

    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute(_HEATMAP_SQL, {
            "lat": lat, "lng": lng, "display_k": display_k,
            "idw_k": IDW_K, "time_w": TIME_W, "daytype_pen": DAYTYPE_PEN,
        })
        row = cur.fetchone()
        return jsonify(row["geojson"] if row else
                       {"type": "FeatureCollection", "features": []})
    except Exception as e:
        log.warning("[dsi_heatmap] query failed: %s", e)
        try:
            conn.rollback()
        except Exception:
            pass
        return jsonify({"error": "heatmap unavailable"}), 500
    finally:
        cur.close()
        conn.close()
