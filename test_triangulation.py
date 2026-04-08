"""
Simulation: real offer — $17.61 UberX
Pickup: Barrington Cir & Scanlan Trce, Missouri City
Driver: Teal Run area (Fort Bend Pkwy & Hwy 6)
"""

import os, sys, logging, requests
sys.path.insert(0, os.path.expanduser("~/puddlejumper-prod"))
import psycopg2, psycopg2.extras
from triangulation import triangulate_pickup, _haversine_miles

# Force logging to be clean for the simulation
logging.basicConfig(level=logging.INFO, format="%(message)s")

DRIVER_LAT   = 29.6220
DRIVER_LNG   = -95.5720
PICKUP_ADDR  = "Barrington Cir & Scanlan Trce, Missouri City, TX"
PICKUP_MILES = 3.2
GPS_AGE_SEC  = 5.0
DB_HOST      = "10.128.0.2"

# 1. Fetch the absolute truth from Google first
MAPS_KEY = os.environ.get("GOOGLE_MAPS_API_KEY")
r = requests.get(
    "https://maps.googleapis.com/maps/api/geocode/json",
    params={"address": PICKUP_ADDR, "key": MAPS_KEY}, timeout=3
)
data = r.json()
p_lat = data["results"][0]["geometry"]["location"]["lat"]
p_lng = data["results"][0]["geometry"]["location"]["lng"]
geocoded_miles = _haversine_miles(DRIVER_LAT, DRIVER_LNG, p_lat, p_lng)

print(f"\n{'='*60}")
print(f"OFFER:   $17.61 UberX  |  YOLO={PICKUP_MILES}mi  |  GPS age={GPS_AGE_SEC}s")
print(f"Driver:  ({DRIVER_LAT}, {DRIVER_LNG})  [Teal Run]")
print(f"Pickup:  {PICKUP_ADDR}")
print(f"Uber geocode → ({p_lat:.5f}, {p_lng:.5f})  [{geocoded_miles:.2f}mi from driver]")
print(f"{'='*60}\n")

# 2. Run the Triangulation Engine
conn = psycopg2.connect(host=DB_HOST, dbname="puddlejumper", user="postgres")
conn.autocommit = True
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

result = triangulate_pickup(
    current_lat=DRIVER_LAT, 
    current_lng=DRIVER_LNG,
    p_lat=p_lat, 
    p_lng=p_lng,
    pickup_miles=PICKUP_MILES,
    geocoded_miles=geocoded_miles,
    cur=cur,
    street_name=PICKUP_ADDR,
    gps_age_sec=GPS_AGE_SEC
)

# 3. Final Output Analysis
print(f"\n{'='*60}")
if result:
    print(f"✅ H3: {result}")
    # Convert the H3 back to coordinates to check for 'drift'
    cur.execute(
        "SELECT app_private.h3_to_lat(%s) AS lat, app_private.h3_to_lng(%s) AS lng",
        (result, result)
    )
    coords = cur.fetchone()
    if coords:
        snap_mi = _haversine_miles(p_lat, p_lng, float(coords['lat']), float(coords['lng']))
        print(f"   Center of Hex: ({coords['lat']:.5f}, {coords['lng']:.5f})")
        print(f"   Drift from Google Truth: {snap_mi:.5f}mi")
else:
    print("❌ None — triangulation failed")
print(f"{'='*60}\n")

cur.close()
conn.close()