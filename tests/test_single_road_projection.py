"""Tests for the scoped single_road pickup projection (precomputed roads_by_name)."""
import pytest
from bead_on_wire import single_road_band_m, project_single_road_pickup, snap_to_road


def test_single_road_band_m():
    lo, hi = single_road_band_m(3.0)
    assert abs(lo - 3.0 * 1609.344 / 2.5) < 0.5
    assert abs(hi - 3.0 * 1609.344 / 0.9) < 0.5
    assert single_road_band_m(0) is None
    assert single_road_band_m(None) is None


def test_project_single_road_lands_on_named_road(db_cur):
    # Real long road from the precomputed table: driver at start, geocode at the
    # midpoint, pickup_miles so the tortuosity band covers the straight-line gap.
    db_cur.execute("""
        SELECT name,
               ST_Y(ST_StartPoint(geom)) d_lat, ST_X(ST_StartPoint(geom)) d_lng,
               ST_Y(ST_LineInterpolatePoint(geom,0.5)) g_lat,
               ST_X(ST_LineInterpolatePoint(geom,0.5)) g_lng,
               ST_Distance(ST_StartPoint(geom)::geography,
                           ST_LineInterpolatePoint(geom,0.5)::geography) straight_m
        FROM app_private.roads_by_name
        WHERE name ~ ' (Rd|Dr|Blvd|St|Ln|Way|Pkwy)$'
          AND GeometryType(geom)='LINESTRING'
          AND ST_Length(geom::geography) BETWEEN 4000 AND 15000
        ORDER BY ST_Length(geom::geography) DESC LIMIT 1
    """)
    r = db_cur.fetchone()
    if r is None or not r["straight_m"]:
        pytest.skip("no suitable test road")
    pickup_miles = (float(r["straight_m"]) * 1.5) / 1609.344   # mid-band tortuosity
    out = project_single_road_pickup(
        db_cur, f"{r['name']}, Houston, Texas",
        float(r["d_lat"]), float(r["d_lng"]), pickup_miles,
        float(r["g_lat"]), float(r["g_lng"]))
    assert out is not None, "expected a projection onto the named road"
    snap = snap_to_road(out[0], out[1], db_cur, max_distance_m=75)
    assert snap is not None, f"projected point {out} is not on any road"


def test_project_returns_none_for_intersection(db_cur):
    # intersection address must NOT be projected (only single_road)
    out = project_single_road_pickup(
        db_cur, "Becky Ln & County Road 103, Pearland, Texas",
        29.54, -95.31, 3.0, 29.54, -95.31)
    assert out is None
