"""
backtest_triangulation.py — Backtest triangulate_pickup() against offer 2229

Real data from tonight's drive:
  Driver:         29.7393522, -95.4584512
  Geocoded pickup: 29.7844747, -95.5863709  (Katy Fwy — wrong end, 8.29mi away)
  Reported miles:  0.99
  Actual pickup:   29.783375,  -95.4697566  (confirmed via Nail It)

Expected result: new arc-banding fallback should place pickup within ~500m of actual.
"""

import sys
import os
import math
sys.path.insert(0, os.path.expanduser('~/puddlejumper-prod'))

import psycopg2
from psycopg2.extras import RealDictCursor
from triangulation import triangulate_pickup, h3_to_coords

# ── Real data from offer 2229 ─────────────────────────────────────────
DRIVER_LAT      = 29.7393522
DRIVER_LNG      = -95.4584512
PICKUP_LAT      = 29.7844747   # geocoded — hallucinated (wrong end of Katy Fwy)
PICKUP_LNG      = -95.5863709
REPORTED_MILES  = 0.99
GEOCODED_MILES  = 8.292015347341147
PICKUP_ADDRESS  = "Katy Fwy, Houston, Texas"
ACTUAL_LAT      = 29.783375    # confirmed via Nail It
ACTUAL_LNG      = -95.4697566

def haversine(lat1, lng1, lat2, lng2):
    R = 3958.8
    lat1, lng1, lat2, lng2 = map(math.radians, [lat1, lng1, lat2, lng2])
    dlat = lat2 - lat1
    dlng = lng2 - lng1
    a = math.sin(dlat/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin(dlng/2)**2
    return R * 2 * math.asin(math.sqrt(a))

def main():
    conn = psycopg2.connect(
        host=os.environ.get('DB_HOST', '10.128.0.2'),
        dbname=os.environ.get('DB_NAME', 'puddlejumper'),
        user=os.environ.get('DB_USER', 'atjb'),
        password=os.environ.get('DB_PASSWORD', '')
    )
    cur = conn.cursor(cursor_factory=RealDictCursor)

    print("=" * 60)
    print("BACKTEST: triangulate_pickup() vs offer 2229")
    print("=" * 60)
    print(f"Driver position:    {DRIVER_LAT}, {DRIVER_LNG}")
    print(f"Geocoded pickup:    {PICKUP_LAT}, {PICKUP_LNG}")
    print(f"Reported miles:     {REPORTED_MILES}")
    print(f"Geocoded miles:     {GEOCODED_MILES:.2f} — hallucination expected")
    print(f"Pickup address:     {PICKUP_ADDRESS}")
    print(f"Actual pickup GPS:  {ACTUAL_LAT}, {ACTUAL_LNG}")
    print()

    # Baseline: distance from geocoded coords to actual
    geocode_error_m = haversine(PICKUP_LAT, PICKUP_LNG, ACTUAL_LAT, ACTUAL_LNG) * 1609.34
    print(f"Geocode error:      {geocode_error_m:.0f}m ({geocode_error_m/1609.34:.2f}mi) — baseline")
    print()

    # Run triangulation
    print("Running triangulate_pickup()...")
    result_h3 = triangulate_pickup(
        DRIVER_LAT, DRIVER_LNG,
        PICKUP_LAT, PICKUP_LNG,
        REPORTED_MILES,
        GEOCODED_MILES,
        cur,
        street_name=PICKUP_ADDRESS
    )

    if result_h3:
        coords = h3_to_coords(result_h3, cur)
        if coords:
            tri_lat, tri_lng = coords
            tri_error_m = haversine(tri_lat, tri_lng, ACTUAL_LAT, ACTUAL_LNG) * 1609.34
            improvement_m = geocode_error_m - tri_error_m

            print(f"Triangulated H3:    {result_h3}")
            print(f"Triangulated coords: {tri_lat:.6f}, {tri_lng:.6f}")
            print(f"Triangulation error: {tri_error_m:.0f}m ({tri_error_m/1609.34:.2f}mi)")
            print()
            print("=" * 60)
            if tri_error_m <= 400:
                print(f"✅ BULLSEYE — {tri_error_m:.0f}m (improved by {improvement_m:.0f}m)")
            elif tri_error_m <= 800:
                print(f"🎯 ON TARGET — {tri_error_m:.0f}m (improved by {improvement_m:.0f}m)")
            else:
                print(f"❌ MISS — {tri_error_m:.0f}m (geocode was {geocode_error_m:.0f}m)")
            print("=" * 60)
        else:
            print("❌ h3_to_coords() returned None")
    else:
        print("❌ triangulate_pickup() returned None — arc-banding fallback failed")
        print("   Check logs for street match details")

    cur.close()
    conn.close()

if __name__ == "__main__":
    main()