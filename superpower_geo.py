import os
import math
import difflib
import requests
import uuid
import h3
import re
from datetime import datetime
from flask import Blueprint, request, jsonify
from auth import verify_and_get_user_id
from db import get_db
from psycopg2.extras import RealDictCursor
import json

superpower_geo_bp = Blueprint('superpower_geo', __name__)

GOOGLE_MAPS_TOKEN = os.getenv("GOOGLE_MAPS_API_KEY")

LOW_CONFIDENCE_THRESHOLD = -3000

# === INTERSECTION DETECTION ===
INTERSECTION_REGEX = re.compile(r'\s*(?:&|and|/|@|\+)\s*', re.IGNORECASE)

# === POI TYPE MAP ===
POI_TYPE_MAP = {
    "park", "airport", "mall", "hospital", "hotel",
    "stadium", "zoo", "museum"
}

MAX_DISTANCE_MULTIPLIER = 1.25
MAX_DISTANCE_PADDING_MILES = 2.0
HARD_DISTANCE_PENALTY = -20000
SOFT_DISTANCE_PENALTY = -4000

# 🟢 NEW: Helper to fetch Red Zones
def get_driver_red_zones(uid):
    """Fetches the driver's personalized redZones from Postgres."""
    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        # We assume settings are stored in the 'settings' column as JSON
        cur.execute(
            "SELECT settings FROM app_private.driver_settings_new WHERE driver_id = %s",
            (uid,)
        )
        row = cur.fetchone()
        if row and row.get('settings'):
            return row['settings'].get('redZones', [])
        return []
    except Exception as e:
        print(f"Error fetching red zones: {e}")
        return []
    finally:
        conn.close()

def validate_trip_distance(pickup_lat, pickup_lng, drop_lat, drop_lng, trip_miles):
    """
    Validates that the distance between Pickup and Dropoff matches the reported Trip Miles.
    """
    actual = haversine_miles(pickup_lat, pickup_lng, drop_lat, drop_lng)
    
    # Allow some buffer: (Trip * 1.25) + 2 miles padding
    max_allowed = (trip_miles * MAX_DISTANCE_MULTIPLIER) + MAX_DISTANCE_PADDING_MILES

    if actual > max_allowed:
        return False, HARD_DISTANCE_PENALTY, actual
    if actual > trip_miles:
        return True, SOFT_DISTANCE_PENALTY, actual

    return True, 0, actual

def haversine_miles(lat1, lng1, lat2, lng2):
    R = 3959
    lat1, lat2 = math.radians(lat1), math.radians(lat2)
    dlat = lat2 - lat1
    dlng = math.radians(lng2 - lng1)
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return R * (2 * math.atan2(math.sqrt(a), math.sqrt(1 - a)))

def call_google_geocode(query, metro_lat, metro_lng, metro_name, radius_meters):
      full_query = query.strip()

      if metro_name and "," not in full_query:
          city = metro_name.split('–')[0].strip()
          full_query += f", {city}"

      print(f"[GOOGLE] Calling API: '{full_query}' near ({metro_lat}, {metro_lng})", flush=True)

      resp = requests.get(
          "https://maps.googleapis.com/maps/api/geocode/json",
          params={
              "address": full_query,
              "bounds": f"{metro_lat-0.5},{metro_lng-0.5}|{metro_lat+0.5},{metro_lng+0.5}",
              "components": "country:US",
              "key": GOOGLE_MAPS_TOKEN
          }
      )

      data = resp.json()
      status = data.get("status")
      results = data.get("results", [])

      print(f"[GOOGLE] Response: status={status}, count={len(results)}", flush=True)

      if results:
          top = results[0]
          loc = top.get("geometry", {}).get("location", {})
          print(f"[GOOGLE] Top result: '{top.get('formatted_address')}' -> ({loc.get('lat')}, {loc.get('lng')})", flush=True)

      candidates = []
      for r in results:
          loc = r.get("geometry", {}).get("location", {})
          candidates.append({
              "formatted_address": r.get("formatted_address"),
              "lat": loc.get("lat"),
              "lng": loc.get("lng"),
              "types": r.get("types", []),
              "location_type": r.get("geometry", {}).get("location_type", "")
          })

      return candidates, None


def score_google_results(
    results,
    cleaned_query,
    pickup_lat,
    pickup_lng,
    metro_lat,
    metro_lng,
    trip_miles,
    is_dropoff 
):
    is_intersection = bool(INTERSECTION_REGEX.search(cleaned_query))
    is_likely_poi = any(k in cleaned_query for k in POI_TYPE_MAP)

    scored_candidates = []

    for cand in results:
        score = 0
        name = (cand.get("formatted_address") or "").lower()
        categories = cand.get("types", [])
        location_type = cand.get("location_type", "")
        cand_lat = cand.get("lat")
        cand_lng = cand.get("lng")

        if not cand_lat or not cand_lng:
            continue

        # String similarity
        score += int(
            difflib.SequenceMatcher(None, cleaned_query, name).ratio() * 1000
        )

        # Type boosts
        if is_intersection and "route" in categories:
            score += 3000
        if "street_address" in categories:
            score += 2000
        if is_likely_poi and "establishment" in categories:
            score += 1500

        # Geometry precision
        if location_type == "ROOFTOP":
            score += 2000
        elif location_type == "RANGE_INTERPOLATED":
            score += 1000
        elif location_type == "APPROXIMATE":
            score -= 2000

        # Broad rejection
        if "country" in categories or "administrative_area_level_1" in categories:
            score -= 15000
        if "administrative_area_level_2" in categories:
            score -= 8000

        
        distance_to_report = 0.0

        if is_dropoff:
            # 1. STRICT DISTANCE VALIDATION (Too Far Check)
            is_valid, penalty, actual_miles = validate_trip_distance(
                pickup_lat, pickup_lng, cand_lat, cand_lng, trip_miles
            )
            if not is_valid:
                continue
            score += penalty
            distance_to_report = actual_miles

            # 2. CIRCUITY CHECK (The "Too Close" Fix)
            # If trip is 27 miles but destination is 5 miles away, ratio is 5.4. Suspicious.
            circuity = trip_miles / max(actual_miles, 0.1)

            if circuity > 4.0:
                score -= 6000  # Massive penalty (United Neighborhood Scenario)
            elif circuity > 2.5:
                score -= 2000  # Moderate penalty (River Crossing / Winding Road)
        else:
            # RELAXED: Pickup just needs to be near the Driver (e.g., < 30 miles)
            dist_from_driver = haversine_miles(metro_lat, metro_lng, cand_lat, cand_lng)
            
            if dist_from_driver > 30.0:
                continue 
            
            if dist_from_driver > 15.0:
                score -= 1000
            
            distance_to_report = dist_from_driver

        scored_candidates.append({
            "name": cand.get("formatted_address"),
            "lat": cand_lat,
            "lng": cand_lng,
            "final_score": score,
            "categories": categories,
            "distance_miles": distance_to_report,
            "location_type": location_type,
            "circuity": round(trip_miles / max(distance_to_report, 0.1), 2) if is_dropoff else 0
        })

    scored_candidates.sort(key=lambda x: x["final_score"], reverse=True)
    return scored_candidates


def geocode_single(
    query,
    metro_lat,
    metro_lng,
    metro_name,
    uid,
    trip_miles,
    is_dropoff,
    pickup_lat=None,
    pickup_lng=None
):
    if not query or not query.strip():
        return {"status": "no_match"}

    cleaned = query.strip().replace("fm ", "Farm to Market Road ")
    
    # Use driver location as center for bounds
    radius_meters = 50000  # ~30 miles

    # --- PASS 1: STANDARD GEOCODE ---
    results, err = call_google_geocode(
        cleaned, metro_lat, metro_lng, metro_name, radius_meters
    )

    if err or not results:
        print(f"[GEOCODE] No results for '{cleaned}': err={err}", flush=True)
        return {"status": "no_match"}

    # --- PASS 2: VALIDATION & SCORING ---
    # Only run deep validation if this is a Dropoff AND we have trip distance
    if is_dropoff and pickup_lat and pickup_lng and trip_miles > 0:
        
        # Score candidates against the trip distance
        scored = score_google_results(
            results,
            cleaned.lower(),
            pickup_lat,
            pickup_lng,
            metro_lat,
            metro_lng,
            trip_miles,
            is_dropoff=True
        )

        # --- PASS 3: AIRPORT FALLBACK (always compete on long trips) ---
        if trip_miles > 15:
            print(f"[GEOCODE] Long trip ({trip_miles}mi) - running airport competition for {metro_name}...", flush=True)
            airport_results, _ = call_google_geocode(
                f"Airport {metro_name}", 
                metro_lat, metro_lng, metro_name, radius_meters
            )
            
            if airport_results:
                airport_scored = score_google_results(
                    airport_results,
                    "airport",
                    pickup_lat,
                    pickup_lng,
                    metro_lat,
                    metro_lng,
                    trip_miles,
                    is_dropoff=True
                )
                scored.extend(airport_scored)
                scored.sort(key=lambda x: x["final_score"], reverse=True)

        # If we STILL have no matches, fail safely
        if not scored:
             print(f"[GEOCODE] FAILED: '{cleaned}' - no candidates match trip distance {trip_miles} mi", flush=True)
             return {"status": "no_match", "reason": "distance_mismatch"}

        # We have a winner!
        winner = scored[0]
        print(f"[GEOCODE] WINNER: '{cleaned}' -> {winner['name']} "
              f"(Dist: {winner['distance_miles']:.1f}mi vs Trip: {trip_miles}mi, "
              f"Circuity: {winner.get('circuity', 'N/A')})", flush=True)
        
        return {
            "status": "success",
            "winner_name": winner["name"],
            "lat": winner["lat"],
            "lng": winner["lng"],
            "h3_index": h3.latlng_to_cell(winner["lat"], winner["lng"], 8),
            "score": winner["final_score"],
            "distance_miles": winner["distance_miles"],
            "audit_trail": scored[:3],
            "source": "google_scored"
        }

    # --- FALLBACK: PICKUP OR NO TRIP DATA ---
    # If it's a pickup, or we don't know the trip length, trust Google's #1 result
    top = results[0]
    lat = top.get("lat")
    lng = top.get("lng")

    if lat is None or lng is None:
        return {"status": "no_match"}

    print(f"[GEOCODE] PICKUP/SIMPLE: '{cleaned}' -> {top.get('formatted_address')}", flush=True)

    return {
        "status": "success",
        "winner_name": top.get("formatted_address"),
        "lat": lat,
        "lng": lng,
        "h3_index": h3.latlng_to_cell(lat, lng, 8),
        "score": 1000,
        "audit_trail": results[:3],
        "source": "google_simple"
    }



@superpower_geo_bp.route('/superpower-geocode-batch', methods=['POST'])
def superpower_geocode_batch():
    try:
        uid = verify_and_get_user_id(request)
    except:
        return jsonify({"error": "Unauthorized"}), 401
        
    data = request.json

    print(f"[DEBUG] Batch geocode request: pickup='{data.get('pickup_query')}', "
            f"dropoff='{data.get('dropoff_query')}', lat={data.get('lat')}, lng={data.get('lng')}, "
            f"metro='{data.get('metro_name')}', tripMiles={data.get('tripMiles')}", 
            flush=True)
    
    trip_miles = float(data.get("tripMiles", 0.0))

    # STEP 1 DYNAMIC BIAS: Get actual driver coordinates from request
    driver_lat = float(data.get("lat", 0.0))
    driver_lng = float(data.get("lng", 0.0))

    # 1. FETCH RED ZONES
    red_zones = get_driver_red_zones(uid)
    red_zone_set = set(red_zones) if red_zones else set()

    # 2. GEOCODE PICKUP - Now centered on actual driver location
    pickup = geocode_single(
        data.get("pickup_query", ""),
        driver_lat,
        driver_lng,
        data.get("metro_name", ""),
        uid,
        trip_miles,
        is_dropoff=False
    )

    # 3. GEOCODE DROPOFF
    dropoff = geocode_single(
        data.get("dropoff_query", ""),
        driver_lat,
        driver_lng,
        data.get("metro_name", ""),
        uid,
        trip_miles,
        is_dropoff=True,
        pickup_lat=pickup.get("lat"),
        pickup_lng=pickup.get("lng")
    )

    # 4. CHECK RED ZONES
    if pickup.get("status") == "success" and "h3_index" in pickup:
        pickup["is_red_zone"] = pickup["h3_index"] in red_zone_set

    if dropoff.get("status") == "success" and "h3_index" in dropoff:
        dropoff["is_red_zone"] = dropoff["h3_index"] in red_zone_set


    return jsonify({
        "status": "success" if pickup.get("status") == "success" and dropoff.get("status") == "success" else "partial",
        "pickup": pickup,
        "dropoff": dropoff
    })