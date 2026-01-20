"""
Shared helper functions for Puddles navigation tools.
These are NOT LangChain tools - just utility functions.
"""
import os
import logging
import re
import requests
from geopy.distance import geodesic

MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY")


def geocode_destination(destination: str, current_gps: str):
    """
    Geocode a destination with proximity bias, smart query cleaning, and fallback logic.
    
    Returns:
        tuple: (dest_address, dest_lat, dest_lng, distance_miles, error_message)
               If successful, error_message is None
               If failed, first 4 values are None and error_message contains the reason
    """
    try:
        logging.info(f"📍 GEOCODING: destination='{destination}', gps={current_gps}")
        
        # --- STEP 1: Get Current City and Country ---
        current_city = None
        current_country = None
        try:
            geo_url = f"https://maps.googleapis.com/maps/api/geocode/json?latlng={current_gps}&key={MAPS_API_KEY}"
            r = requests.get(geo_url, timeout=2).json()
            if r.get("status") == "OK" and r.get("results"):
                for component in r["results"][0].get("address_components", []):
                    if "locality" in component.get("types", []):
                        current_city = component.get("long_name", "").lower()
                    if "country" in component.get("types", []):
                        current_country = component.get("short_name", "").lower()
        except Exception as e:
            logging.warning(f"   Reverse geocode failed: {e}")

        # --- STEP 2: Clean Query ---
        search_query = destination.strip()
        
        vague_terms = [
            "downtown", "uptown", "midtown", "city center", "town center",
            "business district", "financial district", "historic district", "eado", "woodlands",
            "airport", "train station", "bus station", "transit center", "ferry terminal", "port",
            "hospital", "med center", "medical center", "emergency room", "urgent care", "clinic",
            "mall", "galleria", "shopping center", "plaza", "outlet", "market", "marketplace",
            "museum", "museum district", "art district", "theater district", "convention center", "civic center",
            "park", "botanical garden", "arboretum", "zoo", "aquarium", "stadium", "arena", "fairgrounds",
            "university", "college", "campus", "library",
            "waterfront", "beach", "pier", "boardwalk", "riverwalk"
        ]
        
        search_query_lower = search_query.lower()
        search_query_clean = search_query_lower.replace("the ", "").replace("a ", "").strip()
        
        words = search_query_clean.split()
        if len(words) == 2:
            first_word = words[0]
            if first_word in vague_terms:
                search_query_clean = first_word
                logging.info(f"   Stripped city/area from query → {search_query_clean}")

        matched_term = next((term for term in vague_terms if term == search_query_clean or term in search_query_clean), None)
        is_vague_query = (
            matched_term is not None
            and len(search_query_clean.split()) <= 3
            and "," not in search_query
        )
        
        if is_vague_query:
            search_query = search_query_clean + " near me"
        else:
            search_query = search_query_clean
        
        geo_params = {
            "address": search_query,
            "key": MAPS_API_KEY,
            "location": current_gps,
            "radius": 120000
        }
        
        if current_country:
            geo_params["region"] = current_country

        geo_res = requests.get("https://maps.googleapis.com/maps/api/geocode/json", params=geo_params, timeout=4).json()
        
        if geo_res.get("status") != "OK" and is_vague_query:
            fallback = f"{search_query_clean} houston"
            geo_params["address"] = fallback
            geo_res = requests.get("https://maps.googleapis.com/maps/api/geocode/json", params=geo_params, timeout=4).json()

        if geo_res.get("status") != "OK" or not geo_res.get("results"):
            return None, None, None, None, f"Couldn't find '{destination}'."

        dest_address = geo_res["results"][0]["formatted_address"]
        dest_coords = geo_res["results"][0]["geometry"]["location"]
        dest_lat = dest_coords["lat"]
        dest_lng = dest_coords["lng"]
        
        current_coords = tuple(map(float, current_gps.split(',')))
        distance_miles = geodesic(current_coords, (dest_lat, dest_lng)).miles
        
        if is_vague_query and distance_miles > 75:
            return None, None, None, None, f"Found {dest_address} but it's {int(distance_miles)} miles away. Closer one?"

        return dest_address, dest_lat, dest_lng, distance_miles, None
        
    except Exception as e:
        logging.error(f"GEOCODING ERROR: {e}", exc_info=True)
        return None, None, None, None, "Having trouble finding that location right now."


def nice_place_name(formatted: str) -> str:
    """Try to make a cleaner, more driver-friendly name"""
    parts = [p.strip() for p in formatted.split(',')]
    if len(parts) < 2:
        return formatted
    # Airport / major landmark special cases
    if "Airport" in parts[0] or "Center" in parts[0] or "Stadium" in parts[0]:
        return parts[0]
    # Avoid duplicating city if already in primary name
    primary = parts[0].lower()
    city = parts[1].lower()
    if city in primary or primary.endswith(city):
        return parts[0]
    # Typical: Neighborhood or Street + City
    return f"{parts[0]} ({parts[1]})" if len(parts) >= 2 else parts[0]