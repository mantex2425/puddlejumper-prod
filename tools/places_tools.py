"""
Places discovery tools for Puddles Brain.
Helps drivers find bathrooms, gas stations, coffee shops, restaurants, etc.
"""
import os
import logging
import requests
from geopy.distance import geodesic
from langchain_core.tools import tool

MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY")


@tool
def find_nearby_places(place_type: str, current_gps: str = None):
    """
    Find nearby places like bathrooms, gas stations, coffee shops, restaurants.
    
    Use when driver needs to take a break or find facilities:
    - "bathroom", "restroom", "toilet", "need to pee"
    - "gas station", "fuel", "fill up"
    - "coffee", "caffeine", "starbucks"
    - "food", "restaurant", "hungry", "lunch"
    - "fast food", "quick bite", "drive thru"
    - "store", "groceries", "walmart"
    
    Returns: List of 2-3 nearest open places with distances.
    Note: Remember the place names you return - driver may ask to navigate to them next.
    """
    logging.info(f"🚻 PLACES TOOL: place_type='{place_type}'")
    
    if not current_gps or current_gps == "UNKNOWN":
        return {"info": "GPS signal unavailable. Please ensure location services are active."}
    
    try:
        current_coords = tuple(map(float, current_gps.split(',')))
        lat, lng = current_coords
        
        # Map user-friendly terms to Google Places types
        type_mapping = {
            "bathroom": "gas_station|cafe|restaurant|convenience_store|department_store|public_restroom|hardware_store|grocery_store",
            "restroom": "gas_station|cafe|restaurant|convenience_store|department_store|public_restroom|hardware_store|grocery_store",
            "toilet": "gas_station|cafe|restaurant|convenience_store|department_store|public_restroom|hardware_store",
            "gas": "gas_station",
            "gas station": "gas_station",
            "fuel": "gas_station",
            "fill up": "gas_station",
            "petrol station": "gas_station",
            "coffee": "cafe",
            "cafe": "cafe",
            "food": "restaurant",
            "restaurant": "restaurant",
            "fast food": "meal_takeaway|restaurant",
            "quick bite": "meal_takeaway|restaurant",
            "drive thru": "meal_takeaway",
            "drive through": "meal_takeaway",
            "store": "convenience_store|supermarket|grocery_store",
            "grocery": "supermarket|grocery_store"
        }
        
        search_type = type_mapping.get(place_type.lower(), place_type)
        
        # Places API call
        places_url = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
        params = {
            "location": f"{lat},{lng}",
            "radius": 3200,  # ~2 miles
            "type": search_type,
            "key": MAPS_API_KEY,
            "opennow": True  # Only open places
        }

        excluded_types = ["school", "university", "library", "church", "cemetery"]
        
        logging.info(f"   Searching for type: {search_type}")
        
        response = requests.get(places_url, params=params, timeout=5).json()
        
        if response.get("status") != "OK" or not response.get("results"):
            # Try again without opennow filter
            params.pop("opennow")
            response = requests.get(places_url, params=params, timeout=5).json()
        
        if response.get("status") != "OK" or not response.get("results"):
            logging.error(f"   ❌ No results found")
            return {"info": f"I couldn't find any {place_type} nearby."}
        
        # Get top 3 closest results, excluding schools/libraries
        filtered_results = [
            r for r in response["results"] 
            if not any(ex_type in r.get("types", []) for ex_type in excluded_types)
        ][:3]
        places = []
        
        places_with_coords = []
        for place in filtered_results:
            name = place.get("name", "Unknown")
            place_lat = place["geometry"]["location"]["lat"]
            place_lng = place["geometry"]["location"]["lng"]
            
            distance = geodesic((lat, lng), (place_lat, place_lng)).miles
            
            open_now = place.get("opening_hours", {}).get("open_now", None)
            status = " (open now)" if open_now else " (may be closed)" if open_now == False else ""
            
            places_with_coords.append({
                "name": name,
                "lat": place_lat,
                "lng": place_lng,
                "distance": distance,
                "status": status
            })
        
        # Build spoken response (top 2 for brevity)
        spoken = "; ".join([
            f"{p['name']}{p['status']} - {p['distance']:.1f} miles away" 
            for p in places_with_coords[:2]
        ])
        result_text = f"Nearest options: {spoken}"
        
        logging.info(f"   ✅ Found {len(places_with_coords)} places with coordinates")
        
        # Return spoken text AND structured data with coordinates
        # Gemini will see this in conversation history and can extract coords for navigation
        return {
            "info": result_text,
            "places": places_with_coords[:3]  # Store top 3 with coordinates
        }
        
    except Exception as e:
        logging.error(f"❌ PLACES TOOL ERROR: {e}", exc_info=True)
        return {"info": "I'm having trouble finding places right now."}
    
@tool
def get_place_details(place_name: str, lat: float = None, lng: float = None):
    """
    Get detailed information about a specific place (hours, phone, rating).
    
    Use when driver asks follow-up questions about a place just mentioned:
    - "Is it open?", "What are the hours?", "Is it open late?"
    - "What's the phone number?", "Can I call them?"
    - "What's the rating?", "Is it good?", "Any reviews?"
    
    Extract place_name and coordinates from the previous find_nearby_places result.
    
    Args:
        place_name: Name of the place
        lat: Latitude from previous search
        lng: Longitude from previous search
    
    Returns: Detailed information about hours, phone, rating, etc.
    """
    logging.info(f"📋 PLACE DETAILS: place='{place_name}', coords=({lat},{lng})")
    
    if not lat or not lng:
        return {"info": "I need coordinates to look up details. Which specific location?"}
    
    try:
        # Use Google Places Details API
        # First, get place_id using nearby search at exact coordinates
        places_url = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
        params = {
            "location": f"{lat},{lng}",
            "radius": 50,  # Very small radius since we have exact coords
            "key": MAPS_API_KEY
        }
        
        response = requests.get(places_url, params=params, timeout=5).json()
        
        if response.get("status") != "OK" or not response.get("results"):
            return {"info": f"Couldn't find details for {place_name}."}
        
        # Get the first result (should be our exact place)
        place = response["results"][0]
        place_id = place.get("place_id")
        
        # Now get detailed info using place_id
        details_url = "https://maps.googleapis.com/maps/api/place/details/json"
        details_params = {
            "place_id": place_id,
            "fields": "name,formatted_phone_number,opening_hours,rating,user_ratings_total,website",
            "key": MAPS_API_KEY
        }
        
        details_response = requests.get(details_url, params=details_params, timeout=5).json()
        
        if details_response.get("status") != "OK":
            return {"info": f"Couldn't get details for {place_name}."}
        
        result = details_response["result"]
        name = result.get("name", place_name)
        phone = result.get("formatted_phone_number")
        rating = result.get("rating")
        rating_count = result.get("user_ratings_total")
        website = result.get("website")
        opening_hours = result.get("opening_hours", {})
        
        # Build response
        info_parts = [f"{name}"]
        
        # Hours and open status
        if opening_hours:
            open_now = opening_hours.get("open_now")
            if open_now is not None:
                info_parts.append("Open now" if open_now else "Currently closed")
            
            weekday_text = opening_hours.get("weekday_text", [])
            if weekday_text:
                # Get today's hours
                from datetime import datetime
                today_index = datetime.now().weekday()  # 0=Monday, 6=Sunday
                # Google API uses Sunday=0, so adjust
                google_index = (today_index + 1) % 7
                if google_index < len(weekday_text):
                    today_hours = weekday_text[google_index]
                    info_parts.append(today_hours)
        
        # Rating
        if rating:
            rating_text = f"{rating} stars"
            if rating_count:
                rating_text += f" from {rating_count} reviews"
            info_parts.append(rating_text)
        
        # Phone
        if phone:
            info_parts.append(f"Phone: {phone}")
        
        result_text = ". ".join(info_parts) + "."
        
        logging.info(f"   ✅ Found details: {result_text[:100]}")
        return {"info": result_text}
        
    except Exception as e:
        logging.error(f"❌ PLACE DETAILS ERROR: {e}", exc_info=True)
        return {"info": f"I'm having trouble getting details for {place_name} right now."}