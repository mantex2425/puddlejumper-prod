#!/usr/bin/env python3
"""
Replay a drive session against the live backend.
Pulls real offer data from decision_log and replays as POSTs.
Usage: python3 replay_drive.py 2026-04-03
"""
import sys
import json
import time
import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime

DRIVER_ID  = "UjT1hE9eBXh2q95aSZYOkzDJ8lo1"
BACKEND    = "https://puddlejumper-api-152974241923.us-central1.run.app"
DB_HOST    = "10.128.0.2"
REQUIRED_FIELDS = ["driverState", "confidenceTier", "confidenceRadius",
                   "triangulatedPickupLat", "triangulatedPickupLng"]

date_str = sys.argv[1] if len(sys.argv) > 1 else "2026-04-03"

conn = psycopg2.connect(host=DB_HOST, dbname="puddlejumper", user="postgres")
cur  = conn.cursor(cursor_factory=RealDictCursor)

cur.execute("""
    SELECT id, fare, pickup_minutes, trip_minutes, pickup_miles, trip_miles,
           pickup_lat, pickup_lng, dropoff_lat, dropoff_lng,
           current_lat, current_lng, mode_at_decision,
           decision_result->>'verdict' AS original_verdict,
           created_at
    FROM app_private.decision_log
    WHERE driver_id = %s
      AND DATE(created_at AT TIME ZONE 'America/Chicago') = %s
    ORDER BY created_at
""", (DRIVER_ID, date_str))

offers = cur.fetchall()
print(f"Replaying {len(offers)} offers from {date_str}\n")

for o in offers:
    payload = {
        "driver_id":      DRIVER_ID,
        "fare":           float(o["fare"] or 0),
        "tripMiles":      float(o["trip_miles"] or 0),
        "tripMinutes":    float(o["trip_minutes"] or 0),
        "pickupMinutes":  float(o["pickup_minutes"] or 0),
        "pickupMiles":    float(o["pickup_miles"] or 0),
        "lat":            o["pickup_lat"],
        "lng":            o["pickup_lng"],
        "dropoffLat":     o["dropoff_lat"],
        "dropoffLng":     o["dropoff_lng"],
        "currentLat":     o["current_lat"],
        "currentLng":     o["current_lng"],
        "mode":           o["mode_at_decision"] or "FREESTYLE",
    }

    try:
        r = requests.post(f"{BACKEND}/api/v1/decisions", json=payload, timeout=15, headers={"X-Internal-Replay": "puddlejumper-replay-2026", "X-Driver-Id": DRIVER_ID})
        resp = r.json()
        missing = [f for f in REQUIRED_FIELDS if f not in resp]
        status = "✅ FULL" if not missing else f"❌ TRUNCATED — missing: {missing}"
        print(f"Offer {o['id']} | {o['original_verdict']:7} | {status}")
        if missing:
            print(f"  Response: {json.dumps(resp, indent=2)[:200]}")
    except Exception as e:
        print(f"Offer {o['id']} | ERROR: {e}")

    time.sleep(0.5)

conn.close()
print("\nDone.")
