"""
Navigation tools for Puddles Brain.
Handles traffic queries, turn-by-turn directions, and sending places to map apps.
"""
import os
import logging
import requests
import urllib.parse
from langchain_core.tools import tool
from .helpers import geocode_destination, nice_place_name

MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY")


@tool
def get_live_traffic(destination: str, current_gps: str = None):
    """
    Check current traffic conditions and travel time to a destination.
    
    Use ONLY when driver asks about traffic status or delays:
    - "How's traffic to downtown?"
    - "Any delays to the airport?"
    - "How long will it take to get to Hermann Park?"
    
    Do NOT use when driver wants:
    - Turn-by-turn directions (use get_directions)
    - To open navigation app (use send_to_navigation)
    
    Returns: Traffic status, distance, and estimated time with current conditions.
    """
    logging.info(f"🚦 TRAFFIC TOOL: {destination}")
    
    if not current_gps or current_gps == "UNKNOWN":
        return {"info": "No GPS signal. Turn on location services."}
    
    try:
        dest_address, dest_lat, dest_lng, _, error = geocode_destination(destination, current_gps)
        if error:
            return {"info": error}
        
        routes_url = "https://routes.googleapis.com/directions/v2:computeRoutes"
        headers = {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": MAPS_API_KEY,
            "X-Goog-FieldMask": "routes.duration,routes.staticDuration,routes.distanceMeters,routes.warnings"
        }
        
        lat, lng = map(float, current_gps.split(','))
        
        body = {
            "origin": {"location": {"latLng": {"latitude": lat, "longitude": lng}}},
            "destination": {"location": {"latLng": {"latitude": dest_lat, "longitude": dest_lng}}},
            "travelMode": "DRIVE",
            "routingPreference": "TRAFFIC_AWARE_OPTIMAL",
            "computeAlternativeRoutes": True,
            "units": "IMPERIAL"
        }
        
        resp = requests.post(routes_url, json=body, headers=headers, timeout=8)
        logging.info(f"Routes API status: {resp.status_code}")
        route_json = resp.json()
        
        if resp.status_code != 200:
            err_msg = route_json.get("error", {}).get("message", "Unknown error")
            return {"info": f"Navigation service issue: {err_msg}"}

        if "error" in route_json:
            return {"info": f"Route error: {route_json['error'].get('message', 'Unknown')}"}

        if "routes" not in route_json or not route_json["routes"]:
            logging.error(f"No routes - full response: {route_json}")
            return {"info": f"Couldn't calculate route to {destination}. Try more specific address?"}
        
        route = route_json["routes"][0]
        traffic_secs = int(route["duration"].removesuffix('s'))
        standard_secs = int(route["staticDuration"].removesuffix('s'))
        dist_miles = round(route["distanceMeters"] * 0.000621371, 1)
        dur_mins = traffic_secs // 60
        
        efficiency = (standard_secs / traffic_secs) * 100
        if efficiency >= 90: status = "Traffic is light"
        elif efficiency >= 75: status = "Moderate traffic"
        else: status = "Heavy traffic"
        
        warnings = route.get("warnings", [])
        note = f" Note: {warnings[0]}." if warnings else ""
        
        place_name = nice_place_name(dest_address)
        
        weather_alert = ""
        try:
            w_res = requests.get(
                f"https://weather.googleapis.com/v1/publicAlerts:lookup?key={MAPS_API_KEY}&location.latitude={lat}&location.longitude={lng}",
                timeout=3
            ).json()
            if "alerts" in w_res:
                weather_alert = f" Careful: {w_res['alerts'][0]['alertTitle']}."
        except:
            pass
        
        return {"info": f"{status}.{note}{weather_alert} To {place_name} is {dist_miles} miles, ~{dur_mins} min."}
        
    except Exception as e:
        logging.error(f"TRAFFIC TOOL CRASH: {e}", exc_info=True)
        return {"info": "Maps are slow right now. Try again in a sec."}


@tool
def get_directions(destination: str, current_gps: str = None):
    """
    Provide SPOKEN turn-by-turn route summary for a NEW destination.
    
    Use ONLY when:
    - Driver provides a FULL NEW ADDRESS they want directions to
    - Examples: "How do I get to 5000 Main St Houston?", "Directions to IAH airport"
    - The destination is NOT from a recent find_nearby_places result
    
    Do NOT use when:
    - Driver says "go to [place]" referring to a place you just mentioned
    - For those cases, use send_to_navigation instead to open the map app
    
    Returns: Spoken route summary with major roads, exits, and travel time.
    """
    logging.info(f"🗺️ DIRECTIONS TOOL: destination='{destination}'")
    
    if not current_gps or current_gps == "UNKNOWN":
        return {"info": "GPS signal unavailable. Please ensure location services are active."}
    
    try:
        dest_address, dest_lat, dest_lng, distance_miles, error = geocode_destination(destination, current_gps)
        
        if error:
            return {"info": error}
        
        logging.info(f"   Calling Routes API for directions...")
        
        routes_url = "https://routes.googleapis.com/directions/v2:computeRoutes"
        headers = {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": MAPS_API_KEY,
            "X-Goog-FieldMask": "routes.duration,routes.staticDuration,routes.distanceMeters,routes.legs.steps.navigationInstruction"
        }
        
        current_coords = tuple(map(float, current_gps.split(',')))
        lat, lng = current_coords
        
        body = {
            "origin": {"location": {"latLng": {"latitude": lat, "longitude": lng}}},
            "destination": {"location": {"latLng": {"latitude": dest_lat, "longitude": dest_lng}}},
            "travelMode": "DRIVE",
            "routingPreference": "TRAFFIC_AWARE_OPTIMAL",
            "computeAlternativeRoutes": True,
            "units": "IMPERIAL"
        }
        
        resp = requests.post(routes_url, json=body, headers=headers, timeout=8)
        logging.info(f"Routes API status: {resp.status_code}")
        route_json = resp.json()
        
        if resp.status_code != 200:
            err_msg = route_json.get("error", {}).get("message", "Unknown error")
            return {"info": f"Navigation service issue: {err_msg}"}

        if "error" in route_json:
            return {"info": f"Route error: {route_json['error'].get('message', 'Unknown')}"}

        if "routes" not in route_json or not route_json["routes"]:
            logging.error(f"No routes - full response: {route_json}")
            return {"info": f"Couldn't calculate route to {destination}. Try more specific address?"}
        
        route = route_json["routes"][0]
        traffic_secs = int(route["duration"].removesuffix('s'))
        dist_miles = round(route["distanceMeters"] * 0.000621371, 1)
        dur_mins = traffic_secs // 60
        
        # Extract route guidance with more detail
        major_roads = []
        exit_info = None
        seen_roads = set()
        
        import re
        
        for leg in route.get("legs", []):
            steps = leg.get("steps", [])
            
            for i, step in enumerate(steps):
                nav = step.get("navigationInstruction", {})
                instruction = nav.get("instructions", "")
                
                # Extract highway/road names
                road_patterns = [
                    (r'I-\d+', 'interstate'),
                    (r'US[-\s]\d+', 'us_highway'),
                    (r'(?:Highway|Hwy|TX|FM|State Highway)\s*\d+', 'highway'),
                    (r'(?:Loop|Beltway)\s*\d+', 'loop')
                ]
                
                for pattern, road_type in road_patterns:
                    matches = re.findall(pattern, instruction, re.IGNORECASE)
                    for match in matches:
                        normalized = match.strip().replace('Highway ', '').replace('Hwy ', '')
                        
                        # Add direction if present (north, south, etc.)
                        direction_match = re.search(r'\b(north|south|east|west|N|S|E|W)\b', instruction, re.IGNORECASE)
                        if direction_match:
                            direction = direction_match.group(1).lower()
                            if direction in ['n', 's', 'e', 'w']:
                                direction = {'n': 'north', 's': 'south', 'e': 'east', 'w': 'west'}[direction]
                            normalized = f"{normalized} {direction}"
                        
                        if normalized.lower() not in seen_roads:
                            major_roads.append(normalized)
                            seen_roads.add(normalized.lower())
                
                # Check if this is near the end (last 20% of steps) and mentions an exit
                if i > len(steps) * 0.8:
                    exit_match = re.search(r'exit\s+(?:at\s+)?([^.]+)', instruction, re.IGNORECASE)
                    if exit_match and not exit_info:
                        exit_info = exit_match.group(1).strip()
        
        place_name = nice_place_name(dest_address)
        
        # Build the response
        if len(major_roads) == 0:
            route_summary = f"Head to {place_name}"
        elif len(major_roads) == 1:
            route_summary = f"Take {major_roads[0]}"
        elif len(major_roads) <= 4:
            route_summary = f"Take {' to '.join(major_roads)}"
        else:
            # Too many roads, just use first 4
            route_summary = f"Take {' to '.join(major_roads[:4])}"
        
        # Add exit info if available
        if exit_info:
            route_summary += f", then exit {exit_info}"
        
        logging.info(f"   ✅ Route: {route_summary}")
        return {"info": f"{route_summary}. That's {dist_miles} miles, about {dur_mins} minutes with current traffic."}
    
    except Exception as e:
        logging.error(f"❌ DIRECTIONS TOOL ERROR: {e}", exc_info=True)
        return {"info": "I'm having trouble getting directions right now."}


@tool
def send_to_navigation(place_name: str, lat: float = None, lng: float = None, navigation_app: str = "google"):
    """
    Generate a map app URL to open a specific place in navigation.
    
    Use when driver says "go to [place]" referring to a place you just mentioned.
    
    Args:
        place_name: Name of the place
        lat: Latitude of the exact location (if known from find_nearby_places)
        lng: Longitude of the exact location (if known from find_nearby_places)
        navigation_app: "google" (default), "waze", or "apple"
    
    Returns: Dictionary with "info" message and "navigation_url" for the Android app to open.
    """
    logging.info(f"🧭 NAVIGATION LINK: place='{place_name}', lat={lat}, lng={lng}, app='{navigation_app}'")
    
    # Normalize app name
    app = navigation_app.lower()
    if "waze" in app:
        app = "waze"
    elif "apple" in app or "map" in app:
        app = "apple"
    else:
        app = "google"
    
    # If we have coordinates, use them for precise navigation
    if lat is not None and lng is not None:
        if app == "google":
            # Google Maps turn-by-turn to exact coordinates
            url = f"https://www.google.com/maps/dir/?api=1&destination={lat},{lng}"
            app_name = "Google Maps"
        elif app == "waze":
            # Waze navigation to exact coordinates
            url = f"https://waze.com/ul?ll={lat},{lng}&navigate=yes"
            app_name = "Waze"
        else:  # apple
            # Apple Maps to exact coordinates
            url = f"http://maps.apple.com/?daddr={lat},{lng}"
            app_name = "Apple Maps"
        
        logging.info(f"   Generated URL with coordinates: {url}")
    else:
        # Fallback: search by name (less precise)
        encoded_name = urllib.parse.quote(place_name)
        
        if app == "google":
            url = f"https://www.google.com/maps/search/?api=1&query={encoded_name}"
            app_name = "Google Maps"
        elif app == "waze":
            url = f"https://waze.com/ul?q={encoded_name}&navigate=yes"
            app_name = "Waze"
        else:  # apple
            url = f"http://maps.apple.com/?q={encoded_name}"
            app_name = "Apple Maps"
        
        logging.info(f"   Generated URL with name search: {url}")
    
    return {
        "info": f"Opening {place_name} in {app_name}.",
        "navigation_url": url
    }