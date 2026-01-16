import os
import requests
import psycopg
import logging
import re
import urllib.parse
from psycopg.rows import dict_row
from typing import Annotated, TypedDict, List
from flask import Blueprint, request, jsonify

# LangGraph & Logic Imports
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.prebuilt import ToolNode
from langchain_google_vertexai import ChatVertexAI
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from langchain_core.tools import tool
from langchain_core.messages import SystemMessage, HumanMessage, AnyMessage
from psycopg_pool import ConnectionPool
from geopy.distance import geodesic

from utils import verify_and_get_user_id, require_firebase_auth

puddles_bp = Blueprint('puddles', __name__)

# --- Constants & Environment ---
PROJECT_ID = "puddle-jumper-477316"
LOCATION = "us-central1"
DB_HOST = os.environ.get("DB_HOST", "10.128.0.2")
DB_PASSWORD = os.environ.get("DB_PASSWORD")
MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY")
DB_URI = f"postgresql://atjb:{DB_PASSWORD}@{DB_HOST}:5432/puddlejumper"

PUDDLES_SYSTEM_PROMPT = """You are Puddles, a professional AI co-pilot for rideshare drivers.
- PRODUCT EXPERT: Always use check_internal_knowledge FIRST if the driver asks about app features, hex codes, or PuddleJumper rules.
- SENSOR_DATA contains GPS and the driver's current address for local context.

- NAVIGATION RULES:
  * For traffic/time questions ("how's traffic?", "how long to?") → Call get_live_traffic
  * For NEW destinations with full addresses → Call get_directions for spoken turn-by-turn
  * For places YOU JUST MENTIONED in your previous response → Call send_to_navigation to open maps app

- BREAK/FACILITY RULES:
  * "pee", "bathroom", "restroom", "toilet", "need to go", "need to use", "I need to pee" → IMMEDIATELY call find_nearby_places with place_type="bathroom"
  * "gas", "fuel", "fill up", "gas station", "petrol" → IMMEDIATELY call find_nearby_places with place_type="gas station"
  * "coffee", "caffeine", "starbucks", "dunkin" → IMMEDIATELY call find_nearby_places with place_type="coffee"
  * "eat", "food", "hungry", "restaurant", "lunch", "dinner" → IMMEDIATELY call find_nearby_places with place_type="food"
  * "fast food", "quick bite", "drive thru" → IMMEDIATELY call find_nearby_places with place_type="fast food"
  * "store", "shop", "groceries", "walmart", "target" → IMMEDIATELY call find_nearby_places with place_type="store"

- OPENING MAPS APP (send_to_navigation):
  * ALWAYS use send_to_navigation when user asks to navigate to a place you just told them about
  * Triggers: "go to", "navigate to", "directions to", "take me to", "open in maps", "send to maps"
  * Extract the EXACT place name from your previous response (e.g., "7-Eleven", "Bean Here Coffee")
  * Examples: 
    - You said: "Nearest options: Starbucks, 7-Eleven"
    - User says: "go to 7 eleven" → Call send_to_navigation with place_name="7-Eleven"
    - User says: "take me to the first one" → Call send_to_navigation with place_name="Starbucks"
  * Default app is Google Maps unless user specifies "Waze" or "Apple Maps"
  * IMPORTANT: send_to_navigation opens the maps app - it does NOT provide spoken turn-by-turn directions

- ACTION-FIRST: Call tools immediately. Do not narrate your intent or ask for clarification.
- Responses are SPOKEN. Keep them to 1-2 concise sentences. No markdown or bold text."""

# --- 1. Tool Definitions ---

embeddings_model = GoogleGenerativeAIEmbeddings(model="models/text-embedding-004")

@tool
def check_internal_knowledge(query: str):
    """Consult this FIRST for PuddleJumper product knowledge, technical rules, or procedures."""
    try:
        query_vector = embeddings_model.embed_query(query)
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT content FROM knowledge_base 
                    ORDER BY embedding <=> %s::vector LIMIT 1
                """, (query_vector,))
                row = cur.fetchone()
                return row["content"] if row else "No specific internal rule found."
    except Exception as e:
        return f"Knowledge base error: {e}"


# --- SHARED HELPER FUNCTION (NOT A TOOL) ---

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


# --- TOOL 1: TRAFFIC INFO ONLY ---

@tool
def get_live_traffic(destination: str, current_gps: str = None):
    """
    Get current traffic conditions and travel time to a destination.
    Use this when driver asks about traffic, delays, or how long it will take.
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


# --- TOOL 2: DIRECTIONS WITH ROUTE SUMMARY ---

@tool
def get_directions(destination: str, current_gps: str = None):
    """
    Get turn-by-turn directions and route guidance to a destination.
    Use this when driver asks how to get somewhere, best route, or directions.
    """
    logging.info(f"🗺️ DIRECTIONS TOOL CALLED: destination='{destination}'")
    
    if not current_gps or current_gps == "UNKNOWN":
        return {"info": "GPS signal unavailable. Please ensure location services are active."}
    
    try:
        # Use shared geocoding logic
        dest_address, dest_lat, dest_lng, distance_miles, error = geocode_destination(destination, current_gps)
        
        if error:
            return {"info": error}
        
        # Call Routes API with navigation instructions
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
def find_nearby_places(place_type: str, current_gps: str = None):
    """
    Find nearby places like bathrooms, gas stations, coffee shops, grocery stores.
    Use for: "bathroom near me", "find gas station", "coffee shop", "where can I take a break"
    
    Common place_types: restroom, gas_station, cafe, convenience_store, restaurant, store
    """
    logging.info(f"🚻 PLACES TOOL CALLED: place_type='{place_type}'")
    
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
        
        for place in filtered_results:
            name = place.get("name", "Unknown")
            vicinity = place.get("vicinity", "")
            place_lat = place["geometry"]["location"]["lat"]
            place_lng = place["geometry"]["location"]["lng"]
            
            distance = geodesic((lat, lng), (place_lat, place_lng)).miles
            
            open_now = place.get("opening_hours", {}).get("open_now", None)
            status = " (open now)" if open_now else " (may be closed)" if open_now == False else ""
            
            places.append(f"{name}{status} - {distance:.1f} miles away")
        
        result_text = f"Nearest options: " + "; ".join(places[:2])  # Top 2 for brevity
        
        logging.info(f"   ✅ Found {len(filtered_results)} places")
        return {"info": result_text}
        
    except Exception as e:
        logging.error(f"❌ PLACES TOOL ERROR: {e}", exc_info=True)
        return {"info": "I'm having trouble finding places right now."}


@tool
def send_to_navigation(place_name: str, navigation_app: str = "google"):
    """
    Generate a navigation link to send a previously mentioned place to a navigation app.
    Use when driver says "send directions to X" or "navigate to X" where X was just mentioned.
    
    Args:
        place_name: The name of the place from recent conversation
        navigation_app: "google", "waze", or "apple" (default: google)
    """
    logging.info(f"🧭 NAVIGATION LINK: place='{place_name}', app='{navigation_app}'")
    # 1. URL Encode the name to handle spaces/special chars
    encoded_name = urllib.parse.quote(place_name)
    logging.info(f"🧭 NAVIGATION LINK: encoded place='{encoded_name}', app='{navigation_app}'")

    # Normalize app name
    app = navigation_app.lower()
    if "waze" in app:
        app = "waze"
    elif "apple" in app or "map" in app:
        app = "apple"
    else:
        app = "google"
    
    # URL schemes for each app
    # Note: These will open the respective apps if installed
    if app == "google":
        url = f"https://www.google.com/maps/search/?api=1&query={encoded_name}"
        app_name = "Google Maps"
    elif app == "waze":
        url = f"https://waze.com/ul?q={encoded_name}&navigate=yes"
        app_name = "Waze"
    else:  # apple
        url = f"http://maps.apple.com/?q={encoded_name}"
        app_name = "Apple Maps"
    
    logging.info(f"   Generated URL: {url}")
    
    return {
        "info": f"Opening {place_name} in {app_name}.",
        "navigation_url": url
    }


@tool
def web_search(query: str):
    """General info fallback for news or weather."""
    try:
        res = requests.post("https://api.tavily.com/search", json={"api_key": TAVILY_API_KEY, "query": query}, timeout=6).json()
        return " ".join([r.get('content', '')[:300] for r in res.get('results', [])])
    except: return "Search offline."


# --- 2. State & Graph Setup ---

class PuddlesState(TypedDict):
    messages: Annotated[List[AnyMessage], add_messages]
    current_gps: str
    current_addr: str

llm = ChatVertexAI(
    model_name="gemini-2.0-flash-001", 
    temperature=0.3,
    project=PROJECT_ID,
    location=LOCATION
).bind_tools([check_internal_knowledge, get_live_traffic, get_directions, find_nearby_places, send_to_navigation, web_search])

def call_puddles(state: PuddlesState):
    sys_msg = SystemMessage(content=f"SENSOR_DATA: [GPS={state['current_gps']}] [ADDR={state['current_addr']}]\n{PUDDLES_SYSTEM_PROMPT}")
    return {"messages": [llm.invoke([sys_msg] + state["messages"])]}

def should_continue(state: PuddlesState):
    logging.info(f"🔀 should_continue called")
    last = state["messages"][-1]
    if hasattr(last, "tool_calls") and last.tool_calls:
        logging.info(f"   Routing to 'tools' (found {len(last.tool_calls)} tool calls)")
        return "tools"
    logging.info(f"   Routing to END (no tool calls)")
    return END

def call_tools(state: PuddlesState):
    """Execute tools with GPS injection for navigation tools."""
    logging.info(f"📞 call_tools invoked")
    logging.info(f"   State GPS: {state.get('current_gps', 'MISSING')}")
    
    last_message = state["messages"][-1]
    logging.info(f"   Last message type: {type(last_message)}")
    
    # Inject current_gps into navigation tool calls
    if hasattr(last_message, "tool_calls"):
        logging.info(f"   Found {len(last_message.tool_calls)} tool calls")
        for tool_call in last_message.tool_calls:
            logging.info(f"   Tool: {tool_call['name']}")
            if tool_call["name"] in ["get_live_traffic", "get_directions", "find_nearby_places"]:
                logging.info(f"   Injecting GPS: {state['current_gps']}")
                tool_call["args"]["current_gps"] = state["current_gps"]
                logging.info(f"   Tool args after injection: {tool_call['args']}")
    
    # Now execute the tools with the modified args
    logging.info(f"   Creating ToolNode...")
    tool_node = ToolNode([check_internal_knowledge, get_live_traffic, get_directions, find_nearby_places, send_to_navigation, web_search])
    result = tool_node.invoke(state)
    logging.info(f"   ToolNode completed")
    
    return result


workflow = StateGraph(PuddlesState)
workflow.add_node("agent", call_puddles)
workflow.add_node("tools", call_tools)
workflow.set_entry_point("agent")
workflow.add_conditional_edges("agent", should_continue)
workflow.add_edge("tools", "agent")

# --- 3. Persistence Layer (MUST BE BEFORE ROUTE) ---

pool = ConnectionPool(conninfo=DB_URI, max_size=10, open=True, kwargs={"row_factory": dict_row})
checkpointer = PostgresSaver(pool)
app_brain = workflow.compile(checkpointer=checkpointer)

# --- 4. Flask Route ---

@puddles_bp.route('/ask_puddles', methods=['POST'])
@require_firebase_auth
def ask_puddles():
    try:
        driver_id = verify_and_get_user_id(request)
        data = request.get_json() or {}
        thread_id = data.get("context_hash") or f"session_{driver_id}"
        
        location = data.get("location", {})
        lat, lng = location.get('lat'), location.get('lng')
        location_str = f"{lat},{lng}" if lat and lng else "UNKNOWN"

        # LOG THE ACTUAL USER INPUT
        user_message = data.get("message", "")
        logging.info(f"👤 USER SAID: '{user_message}'")
        logging.info(f"📍 GPS: {location_str}")

        # DYNAMIC ADDRESS LOOKUP
        current_addr = "Unknown location"
        if location_str != "UNKNOWN":
            try:
                geo_url = f"https://maps.googleapis.com/maps/api/geocode/json?latlng={location_str}&key={MAPS_API_KEY}"
                r = requests.get(geo_url, timeout=2).json()
                if r.get("status") == "OK" and r.get("results"):
                    current_addr = r["results"][0]["formatted_address"]
            except Exception as e:
                logging.warning(f"Reverse Geocode Error: {e}")
                current_addr = "GPS location acquired, but address lookup failed."

        config = {"configurable": {"thread_id": thread_id}}
        inputs = {
            "messages": [HumanMessage(content=user_message)], 
            "current_gps": location_str, 
            "current_addr": current_addr
        }
        
        logging.info(f"🧠 INVOKING BRAIN with thread_id={thread_id}")
        final_state = app_brain.invoke(inputs, config=config)
        logging.info(f"🧠 BRAIN COMPLETED")
        
        logging.info(f"📤 Extracting final message...")
        final_message = final_state["messages"][-1]
        logging.info(f"   Final message type: {type(final_message)}")
        logging.info(f"   Final message has content: {hasattr(final_message, 'content')}")
        if hasattr(final_message, 'content'):
            content_preview = str(final_message.content)[:200] if final_message.content else "EMPTY"
            logging.info(f"   Content preview: {content_preview}")
        
        # Check if any tool returned a navigation URL
        navigation_url = None
        for msg in reversed(final_state["messages"]):
            if hasattr(msg, "content") and isinstance(msg.content, str):
                continue
            elif hasattr(msg, "content") and isinstance(msg.content, list):
                for item in msg.content:
                    if isinstance(item, dict) and "navigation_url" in item:
                        navigation_url = item["navigation_url"]
                        break
            if navigation_url:
                break
        
        logging.info(f"📦 Building response...")
        response = {
            "reply": final_state["messages"][-1].content,
            "context_hash": thread_id
        }
        
        if navigation_url:
            response["navigation_url"] = navigation_url
        
        logging.info(f"✅ Returning response with reply length: {len(response.get('reply', ''))}")
        return jsonify(response)

    except Exception as e:
        logging.critical(f"PUDDLES CRASH: {e}", exc_info=True)
        return jsonify({"error": "Internal error — please try again"}), 500

