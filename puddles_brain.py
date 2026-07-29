import os
import requests
import psycopg
import logging
import re
from psycopg.rows import dict_row
from typing import Annotated, TypedDict, List
from flask import Blueprint, request, jsonify

# Remove unused import: from langgraph.types import Overwrite

# LangGraph & Logic Imports
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.prebuilt import ToolNode
from langchain_google_vertexai import ChatVertexAI
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import SystemMessage, HumanMessage, AnyMessage
from psycopg_pool import ConnectionPool

# Import all tools from our modular tools package
from tools import (
    get_weather,
    get_live_traffic,
    get_directions,
    send_to_navigation,
    find_nearby_places,
    get_place_details,
    trigger_app_action,
    check_internal_knowledge,
    web_search,
    set_ride_mode,        # ADD THIS
    get_current_ride_mode 
)
from tools.system_prompt import PUDDLES_SYSTEM_PROMPT

from utils import verify_and_get_user_id, require_firebase_auth

puddles_bp = Blueprint('puddles', __name__)

# --- Constants & Environment ---
PROJECT_ID = "puddle-jumper-477316"
LOCATION = "us-central1"
DB_HOST = os.environ.get("DB_HOST", "10.128.0.3")
DB_PASSWORD = os.environ.get("DB_PASSWORD")
MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY")
DB_URI = f"postgresql://atjb:{DB_PASSWORD}@{DB_HOST}:5432/puddlejumper"

# --- All Tools List ---
ALL_TOOLS = [
    get_weather,
    check_internal_knowledge,
    get_live_traffic,
    get_directions,
    send_to_navigation,
    find_nearby_places,
    get_place_details,
    trigger_app_action,
    web_search,
    set_ride_mode,
    get_current_ride_mode
]

# --- State & Graph Setup ---

class PuddlesState(TypedDict):
    messages: Annotated[List[AnyMessage], add_messages]
    current_gps: str
    current_addr: str

    navigation_url: str  # Plain str defaults to overwrite
    app_action: str  # Plain str defaults to overwrite

llm = ChatVertexAI(
    model_name="gemini-2.0-flash-001", 
    temperature=0.3,
    project=PROJECT_ID,
    location=LOCATION
).bind_tools(ALL_TOOLS)

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
    
    # Execute the tools with the modified args
    logging.info(f"   Creating ToolNode...")
    tool_node = ToolNode(ALL_TOOLS)
    result = tool_node.invoke(state)
    logging.info(f"   ToolNode completed")
    
    return result


workflow = StateGraph(PuddlesState)
workflow.add_node("agent", call_puddles)
workflow.add_node("tools", call_tools)
workflow.set_entry_point("agent")
workflow.add_conditional_edges("agent", should_continue)
workflow.add_edge("tools", "agent")

# --- Persistence Layer ---
pool = ConnectionPool(conninfo=DB_URI, max_size=10, open=True, kwargs={"row_factory": dict_row})
checkpointer = PostgresSaver(pool)
app_brain = workflow.compile(checkpointer=checkpointer)

# --- Response Extraction Helper ---

def extract_action_from_messages(messages: List[AnyMessage]) -> dict:
    """
    Extract navigation_url or app_action from tool responses,
    ensuring we only return actions from the CURRENT turn.
    """
    action_response = {}
    
    # Search messages in reverse order (most recent first)
    for msg in reversed(messages):
        # Log what we're examining
        logging.info(f"   Examining message type: {type(msg).__name__}")
        
        # 🛑 STOP: If we hit the latest HumanMessage, we have passed the current turn's tools
        if isinstance(msg, HumanMessage):
            logging.info("   Found HumanMessage boundary. Stopping search to avoid sticky data.")
            break
        
        # Check ToolMessage (this is where fresh tool results are stored)
        if hasattr(msg, '__class__') and msg.__class__.__name__ == 'ToolMessage':
            logging.info(f"   Found ToolMessage with content type: {type(msg.content)}")
            content = msg.content
            
            # ToolMessage content is usually a JSON string
            if isinstance(content, str):
                try:
                    import json
                    parsed = json.loads(content)
                    
                    if "navigation_url" in parsed:
                        action_response["navigation"] = {
                            "url": parsed["navigation_url"],
                            "place_name": parsed.get("place_name", "destination")
                        }
                        logging.info(f"   ✅ Found FRESH navigation_url: {parsed['navigation_url']}")
                        return action_response
                    
                    if "app_action" in parsed:
                        action_response["app_action"] = parsed["app_action"]
                        logging.info(f"   ✅ Found FRESH app_action: {parsed['app_action']}")
                        return action_response
                except json.JSONDecodeError:
                    logging.warning(f"   Failed to parse ToolMessage content as JSON")
                    pass
        
        # Fallback: Check AIMessage for tool outputs if not using a ToolNode
        if hasattr(msg, "content") and not isinstance(msg, HumanMessage):
            content = msg.content
            
            # Check for structured dictionaries in content list
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict):
                        if "navigation_url" in item:
                            action_response["navigation"] = {
                                "url": item["navigation_url"],
                                "place_name": item.get("place_name", "destination")
                            }
                            return action_response
                        
                        if "app_action" in item:
                            action_response["app_action"] = item["app_action"]
                            return action_response
    
    logging.info(f"   ⚠️ No FRESH action data found for this turn.")
    return action_response


# --- Flask Route ---

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
        
        # 1. Capture how many messages were in the thread BEFORE we started this turn
        # This allows us to isolate only the messages generated in THIS call
        previous_state = app_brain.get_state(config)
        pre_count = len(previous_state.values.get("messages", []))
        
        final_state = app_brain.invoke(inputs, config=config)
        logging.info(f"🧠 BRAIN COMPLETED")
        
        logging.info(f"📤 Extracting final message...")
        all_messages = final_state["messages"]
        final_message = all_messages[-1]
        
        # Extract reply text
        reply_text = final_message.content if hasattr(final_message, 'content') else "I'm having trouble responding right now."
        
        # 2. Extract actions ONLY from the messages added in THIS turn
        # We skip everything before the pre_count
        new_messages = all_messages[pre_count:]
        action_data = extract_action_from_messages(new_messages)
        
        logging.info(f"   Turn messages being examined: {len(new_messages)}")
        
        logging.info(f"📦 Building response...")
        response = {
            "reply": reply_text,
            "context_hash": thread_id
        }
        
        # Add action data if present (explicitly set to None if not found)
        response["navigation_url"] = action_data.get("navigation", {}).get("url", None)
        response["app_action"] = action_data.get("app_action", None)
        
        logging.info(f"✅ Returning response with reply length: {len(response.get('reply', ''))}")
        return jsonify(response)

    except Exception as e:
        logging.critical(f"PUDDLES CRASH: {e}", exc_info=True)
        return jsonify({"error": "Internal error — please try again"}), 500