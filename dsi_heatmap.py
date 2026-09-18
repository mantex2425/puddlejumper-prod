"""dsi_heatmap.py — Drive Score Index heatmap endpoint.

GET /api/v1/dsi/heatmap?lat=&lng=&radiusMiles=  ->  GeoJSON FeatureCollection of the
res-8 H3 cells in the driver's metro that ACTUALLY have scored offers, each carrying a
time-weighted GROSS $/hr (`grossHourly`), how many offers back it, and a RELATIVE colour
tier. Tiers are percentiles of the current spread across the metro (purple = top quartile
here-and-now, green, orange, yellow = bottom), so purple means best-around-here-right-now
rather than a fixed dollar value.

CHANGED 2026-09-18, twice over:
  * Source is `app_private.offer_history`, not `public.community_offers`. The pooled
    community rows compute their cell from coordinates the APP sends, and the app stopped
    sending them on 2026-08-19 ("the server geocodes from addresses"), so safe_h3(NULL,
    NULL) left 1,510 of 2,161 rows with no cell and the map froze at that date.
    offer_history takes its cell from the server-side geocode and never stopped.
  * The weighted value is the offer's gross $/hr, not `dsi_v1`. dsi_v1 is the retired
    weighted index -- not dollars, and never shown to a driver. Gross $/hr is the same
    number the frog shows, so a cell now means "offers around here have paid about this".
    The property is `grossHourly`; the old `dsi` property is gone (clients colour by
    `tier`, which is unchanged).

CHANGED 2026-09-14. This used to render a fixed hexagonal disk of cells (k rings,
default 8) centred on the driver and FILL every cell in it by spatial IDW from offers
up to 6 rings away - so most coloured hexes had never had an offer picked up in them,
nothing beyond the disk was shown, and a driver could not tell a reading from a guess.
It now returns every real scored cell within radiusMiles, with no spatial fill; the
time weighting (TIME_W, DAYTYPE_PEN) is unchanged. `k` is still accepted and ignored so
older clients keep working.

OBSERVATIONAL: read-only, never affects a verdict.
"""
import logging

from flask import Blueprint, request, jsonify
from psycopg2.extras import RealDictCursor

from db import get_db
from offer_copies import not_a_copy
from utils import verify_and_get_user_id

log = logging.getLogger(__name__)
dsi_heatmap_bp = Blueprint("dsi_heatmap", __name__)

# How far from the driver to include scored cells. A metro, not a neighbourhood.
RADIUS_MILES_DEFAULT = 50
RADIUS_MILES_MAX = 150
# Temporal weighting — identical knobs to area_dsi.py (spec §1; tunable).
TIME_W = 0.4
DAYTYPE_PEN = 6.0

_HEATMAP_SQL = """
WITH params AS (
    SELECT EXTRACT(hour FROM now() AT TIME ZONE 'America/Chicago')::int       AS qhour,
           (EXTRACT(dow FROM now() AT TIME ZONE 'America/Chicago')::int IN (0, 6)) AS qweekend
),
surface AS (
    -- Every scored offer in the driver's metro. §6 read-filter unchanged: drop
    -- capture-error junk (ehr/dpm <= 0), keep negative dsi.
    SELECT co.pickup_h3::h3index AS cell, co.effective_hourly_rate AS gross_hourly,
           EXTRACT(hour FROM co.created_at AT TIME ZONE 'America/Chicago')::int       AS oh,
           (EXTRACT(dow FROM co.created_at AT TIME ZONE 'America/Chicago')::int IN (0, 6)) AS owe
    FROM app_private.offer_history co
    WHERE co.effective_hourly_rate IS NOT NULL
""" + not_a_copy("co", "app_private.offer_history") + """
      AND co.pickup_h3 IS NOT NULL
      AND co.effective_hourly_rate > 0
      -- Same sanity band as the Time Grid: above this is a capture error, not a fare.
      -- Every cell over $60/hr in the first run off real data was a single offer.
      AND co.effective_hourly_rate < 150
      AND co.dollars_per_mile > 0
      -- NOT h3_grid_distance: it raises (h3 err 1) on far cells.
      AND app_private.distance_miles(
              app_private.h3_to_lat(co.pickup_h3),
              app_private.h3_to_lng(co.pickup_h3),
              %(lat)s, %(lng)s) <= %(radius_miles)s
),
cells AS (
    -- One row per hex that ACTUALLY has scored offers. Time-weighted exactly as
    -- before (nearer hour and same weekday/weekend type count for more), so the
    -- colours still mean "for now" - but with no spatial fill between cells.
    SELECT s.cell,
           sum(s.gross_hourly * w.weight) / NULLIF(sum(w.weight), 0) AS dsi,
           count(*) AS pts
    FROM surface s
    CROSS JOIN params p
    CROSS JOIN LATERAL (
        SELECT 1.0 / (
            1.0 + %(time_w)s * (
                LEAST(abs(s.oh - p.qhour), 24 - abs(s.oh - p.qhour))
                + CASE WHEN s.owe <> p.qweekend THEN %(daytype_pen)s ELSE 0 END)
        ) AS weight
    ) w
    GROUP BY s.cell
),
tiers AS (
    -- RELATIVE breakpoints from the current spread across the metro
    SELECT percentile_cont(0.25) WITHIN GROUP (ORDER BY dsi) AS p25,
           percentile_cont(0.50) WITHIN GROUP (ORDER BY dsi) AS p50,
           percentile_cont(0.75) WITHIN GROUP (ORDER BY dsi) AS p75
    FROM cells WHERE dsi IS NOT NULL
)
SELECT json_build_object(
    'type', 'FeatureCollection',
    'center', json_build_object('lat', %(lat)s, 'lng', %(lng)s),
    'radius_miles', %(radius_miles)s,
    'cell_count', (SELECT count(*) FROM cells WHERE dsi IS NOT NULL),
    'total_points', (SELECT COALESCE(sum(pts), 0) FROM cells WHERE dsi IS NOT NULL),
    'bounds', (SELECT json_build_object(
        'min_lat', min(app_private.h3_to_lat(cell::text)), 'max_lat', max(app_private.h3_to_lat(cell::text)),
        'min_lng', min(app_private.h3_to_lng(cell::text)), 'max_lng', max(app_private.h3_to_lng(cell::text)))
        FROM cells WHERE dsi IS NOT NULL),
    'breakpoints', (SELECT json_build_object(
        'p25', round(p25::numeric, 1), 'p50', round(p50::numeric, 1),
        'p75', round(p75::numeric, 1)) FROM tiers),
    'features', COALESCE(json_agg(json_build_object(
        'type', 'Feature',
        'geometry', ST_AsGeoJSON(h3_cell_to_boundary(c.cell)::geometry)::json,
        'properties', json_build_object(
            'h3', c.cell::text,
            'grossHourly', round(c.dsi::numeric, 2),
            'points', c.pts,
            'confidence', CASE WHEN c.pts >= 5 THEN 'high'
                               WHEN c.pts >= 2 THEN 'medium'
                               ELSE 'low' END,
            'tier', CASE WHEN c.dsi >= t.p75 THEN 'purple'
                         WHEN c.dsi >= t.p50 THEN 'green'
                         WHEN c.dsi >= t.p25 THEN 'orange'
                         ELSE 'yellow' END
        )
    )) FILTER (WHERE c.dsi IS NOT NULL), '[]'::json)
) AS geojson
FROM cells c CROSS JOIN tiers t;
"""


@dsi_heatmap_bp.route("/heatmap", methods=["GET"])
def get_heatmap():
    """Every scored DSI cell within radiusMiles of (lat, lng). Observational; best-effort."""
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
        radius_miles = min(max(float(request.args.get("radiusMiles", RADIUS_MILES_DEFAULT)), 1.0),
                           float(RADIUS_MILES_MAX))
    except (ValueError, TypeError):
        radius_miles = float(RADIUS_MILES_DEFAULT)

    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute(_HEATMAP_SQL, {
            "lat": lat, "lng": lng, "radius_miles": radius_miles,
            "time_w": TIME_W, "daytype_pen": DAYTYPE_PEN,
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
