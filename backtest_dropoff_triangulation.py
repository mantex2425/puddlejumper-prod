"""
backtest_dropoff_triangulation.py — Backtest triangulate_dropoff() for offer 2227

The chain we're testing:
  1. Offer 2227: Barrington Cir → Katy Fwy, 25.4mi trip
  2. Confirmed pickup (Nail It): 29.498626, -95.5270149
  3. Geocoded dropoff: 29.7844747, -95.5863709 (wrong end of Katy Fwy — Beltway 8 West)
  4. Actual dropoff (confirmed via offer 2229 pickup proximity): ~29.783, -95.469 (near Loop 610)

If arc-banding correctly places the dropoff near Loop 610:
  → That becomes the arc center for offer 2229
  → Offer 2229 pickup triangulates correctly
  → The whole chain works
"""

import sys
import os
import math
sys.path.insert(0, os.path.expanduser('~/puddlejumper-prod'))

import psycopg2
from psycopg2.extras import RealDictCursor
from triangulation import triangulate_dropoff, h3_to_coords

# ── Real data from offer 2227 ─────────────────────────────────────────
CONFIRMED_PICKUP_LAT = 29.498626      # Nail It ground truth
CONFIRMED_PICKUP_LNG = -95.5270149
GEOCODED_DROPOFF_LAT = 29.7844747    # hallucinated — wrong end of Katy Fwy
GEOCODED_DROPOFF_LNG = -95.5863709
TRIP_MILES           = 25.40
DROPOFF_ADDRESS      = "Katy Fwy, Houston, Texas"

# Actual dropoff — inferred from offer 2229 actual pickup (same location)
ACTUAL_DROPOFF_LAT   = 29.783375
ACTUAL_DROPOFF_LNG   = -95.4697566

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
    print("BACKTEST: triangulate_dropoff() for offer 2227")
    print("=" * 60)
    print(f"Confirmed pickup (arc center): {CONFIRMED_PICKUP_LAT}, {CONFIRMED_PICKUP_LNG}")
    print(f"Geocoded dropoff:  {GEOCODED_DROPOFF_LAT}, {GEOCODED_DROPOFF_LNG}")
    print(f"Trip miles:        {TRIP_MILES}")
    print(f"Dropoff address:   {DROPOFF_ADDRESS}")
    print(f"Actual dropoff:    {ACTUAL_DROPOFF_LAT}, {ACTUAL_DROPOFF_LNG}")
    print()

    # Baseline: geocoded dropoff vs actual
    geocode_error_m = haversine(
        GEOCODED_DROPOFF_LAT, GEOCODED_DROPOFF_LNG,
        ACTUAL_DROPOFF_LAT, ACTUAL_DROPOFF_LNG
    ) * 1609.34
    print(f"Geocode error:     {geocode_error_m:.0f}m ({geocode_error_m/1609.34:.2f}mi) — baseline")
    print()

    # Run dropoff triangulation using confirmed pickup as arc center
    print("Running triangulate_dropoff() from confirmed pickup...")
    result_h3 = triangulate_dropoff(
        CONFIRMED_PICKUP_LAT, CONFIRMED_PICKUP_LNG,
        GEOCODED_DROPOFF_LAT, GEOCODED_DROPOFF_LNG,
        TRIP_MILES,
        cur,
        street_name=DROPOFF_ADDRESS,
        driver_lat=29.7393522,
        driver_lng=-95.4584512
    )

    if result_h3:
        coords = h3_to_coords(result_h3, cur)
        if coords:
            tri_lat, tri_lng = coords
            tri_error_m = haversine(
                tri_lat, tri_lng,
                ACTUAL_DROPOFF_LAT, ACTUAL_DROPOFF_LNG
            ) * 1609.34
            improvement_m = geocode_error_m - tri_error_m

            print(f"Triangulated H3:   {result_h3}")
            print(f"Triangulated coords: {tri_lat:.6f}, {tri_lng:.6f}")
            print(f"Triangulation error: {tri_error_m:.0f}m ({tri_error_m/1609.34:.2f}mi)")
            print()

            # Now test offer 2229 pickup triangulation using THIS as arc center
            print("=" * 60)
            print("CHAIN TEST: Would offer 2229 pickup be correct?")
            print(f"New arc center: {tri_lat:.6f}, {tri_lng:.6f}")
            
            from triangulation import triangulate_pickup
            OFFER_2229_PICKUP_LAT = 29.7844747
            OFFER_2229_PICKUP_LNG = -95.5863709
            OFFER_2229_REPORTED   = 0.99
            OFFER_2229_GEOCODED   = 8.292015347341147
            OFFER_2229_ACTUAL_LAT = 29.783375
            OFFER_2229_ACTUAL_LNG = -95.4697566

            pickup_h3 = triangulate_pickup(
                tri_lat, tri_lng,
                OFFER_2229_PICKUP_LAT, OFFER_2229_PICKUP_LNG,
                OFFER_2229_REPORTED,
                OFFER_2229_GEOCODED,
                cur,
                street_name="Katy Fwy, Houston, Texas"
            )

            if pickup_h3:
                pickup_coords = h3_to_coords(pickup_h3, cur)
                if pickup_coords:
                    p_lat, p_lng = pickup_coords
                    pickup_error_m = haversine(
                        p_lat, p_lng,
                        OFFER_2229_ACTUAL_LAT, OFFER_2229_ACTUAL_LNG
                    ) * 1609.34
                    print(f"Offer 2229 pickup: {p_lat:.6f}, {p_lng:.6f}")
                    print(f"Offer 2229 error:  {pickup_error_m:.0f}m")
            else:
                print("Offer 2229 pickup triangulation returned None")

            print()
            print("=" * 60)
            if tri_error_m <= 400:
                print(f"✅ DROPOFF BULLSEYE — {tri_error_m:.0f}m (improved {improvement_m:.0f}m)")
            elif tri_error_m <= 800:
                print(f"🎯 DROPOFF ON TARGET — {tri_error_m:.0f}m (improved {improvement_m:.0f}m)")
            else:
                print(f"❌ DROPOFF MISS — {tri_error_m:.0f}m (geocode was {geocode_error_m:.0f}m)")
            print("=" * 60)
    else:
        print("❌ triangulate_dropoff() returned None")

    cur.close()
    conn.close()

if __name__ == "__main__":
    main()