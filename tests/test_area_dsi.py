"""Tests for the DSI v1 area readout (IDW interpolation) + thin-market gate.

Uses the live db_cur fixture (real community_offers surface, rolled back).
Observational only — exercises read-only interpolation, never a verdict.

§thin-market gate (2026-06-16): a market bar built on fewer than
MIN_MARKET_POINTS valid offers is noise — interpolate_area_dsi returns None so
the verdict falls back to the interim absolute threshold instead of declining a
good offer against a ~1-sample bar.
"""
from area_dsi import interpolate_area_dsi, AREA_DSI_K_RINGS, MIN_MARKET_POINTS

# Remote ocean coord with NO real community_offers nearby (see
# test_area_dsi_far_from_data_returns_none) — lets a test control the exact point
# count by seeding into the otherwise-empty disk.
_EMPTY_LAT, _EMPTY_LNG = 0.0, -150.0


def _seed_offer(cur, lat, lng, dsi):
    """Seed one valid community_offers row at (lat,lng)'s res-8 hex (rolled back by
    db_cur). Only the columns interpolate_area_dsi reads are populated."""
    cur.execute(
        """INSERT INTO public.community_offers
               (pickup_h3, dsi_v1, effective_hourly_rate, dollars_per_mile, created_at)
           VALUES (app_private.safe_h3(%s, %s)::text, %s, 20.0, 1.0, now())""",
        (lat, lng, dsi),
    )


def test_area_dsi_null_coords_returns_none(db_cur):
    assert interpolate_area_dsi(db_cur, None, None) is None
    assert interpolate_area_dsi(db_cur, 29.76, None) is None
    assert interpolate_area_dsi(db_cur, None, -95.37) is None


def test_area_dsi_far_from_data_returns_none(db_cur):
    # Middle of the Pacific — no community_offers within k rings.
    assert interpolate_area_dsi(db_cur, _EMPTY_LAT, _EMPTY_LNG) is None


def test_min_market_points_is_three():
    assert MIN_MARKET_POINTS == 3


def test_area_dsi_thin_market_returns_none(db_cur):
    # Fewer than MIN_MARKET_POINTS valid offers -> thin -> None (not a 1-sample bar).
    for _ in range(MIN_MARKET_POINTS - 1):
        _seed_offer(db_cur, _EMPTY_LAT, _EMPTY_LNG, dsi=25.0)
    assert interpolate_area_dsi(db_cur, _EMPTY_LAT, _EMPTY_LNG) is None


def test_area_dsi_sufficient_market_returns_value(db_cur):
    # Exactly MIN_MARKET_POINTS valid offers at one hex -> a real bar (~their DSI).
    for _ in range(MIN_MARKET_POINTS):
        _seed_offer(db_cur, _EMPTY_LAT, _EMPTY_LNG, dsi=25.0)
    val = interpolate_area_dsi(db_cur, _EMPTY_LAT, _EMPTY_LNG)
    assert val is not None, "n >= MIN_MARKET_POINTS should yield a market DSI"
    assert abs(val - 25.0) < 1.0, f"all points DSI=25 -> bar ~25, got {val}"


def test_area_dsi_near_real_hex_is_sane(db_cur):
    # Pick a real pickup hex, then seed enough points there to clear the
    # thin-market gate -> exercises the real-data path deterministically.
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
    for _ in range(MIN_MARKET_POINTS):
        _seed_offer(db_cur, float(r["lat"]), float(r["lng"]), dsi=23.0)
    val = interpolate_area_dsi(db_cur, float(r["lat"]), float(r["lng"]))
    assert val is not None, "expected a non-null area DSI at a seeded-dense hex"
    assert -100.0 < val < 500.0, f"area DSI {val} out of sane range"


def test_area_dsi_k_rings_constant():
    assert AREA_DSI_K_RINGS >= 1
