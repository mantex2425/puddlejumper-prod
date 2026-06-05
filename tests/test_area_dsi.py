"""Tests for the DSI v1 area readout (IDW interpolation).

Uses the live db_cur fixture (real community_offers surface, rolled back).
Observational only — exercises read-only interpolation, never a verdict.
"""
from area_dsi import interpolate_area_dsi, AREA_DSI_K_RINGS


def test_area_dsi_null_coords_returns_none(db_cur):
    assert interpolate_area_dsi(db_cur, None, None) is None
    assert interpolate_area_dsi(db_cur, 29.76, None) is None
    assert interpolate_area_dsi(db_cur, None, -95.37) is None


def test_area_dsi_far_from_data_returns_none(db_cur):
    # Middle of the Pacific — no community_offers within k rings.
    assert interpolate_area_dsi(db_cur, 0.0, -150.0) is None


def test_area_dsi_near_real_hex_is_sane(db_cur):
    # Use an actual pickup hex center -> guaranteed >= 1 nearby point.
    db_cur.execute(
        """
        SELECT (h3_cell_to_lat_lng(pickup_h3::h3index))[1] AS lat,
               (h3_cell_to_lat_lng(pickup_h3::h3index))[0] AS lng
        FROM public.community_offers
        WHERE pickup_h3 IS NOT NULL AND dsi_v1 IS NOT NULL
        LIMIT 1
        """
    )
    r = db_cur.fetchone()
    val = interpolate_area_dsi(db_cur, float(r["lat"]), float(r["lng"]))
    assert val is not None, "expected a non-null area DSI at a real pickup hex"
    # Sane bound — DSI can be negative in bad areas, but never wildly out of range.
    assert -100.0 < val < 500.0, f"area DSI {val} out of sane range"


def test_area_dsi_k_rings_constant():
    assert AREA_DSI_K_RINGS >= 1
