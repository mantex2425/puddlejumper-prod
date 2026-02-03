"""
Ride mode control tools for Puddles Brain.
Handles switching between Puddle Jump, Towards, and Freestyle modes.
"""
import logging
from langchain_core.tools import tool


@tool
def set_ride_mode(
    mode: str,
    market_name: str = None,
    market_id: str = None,
    target_lat: float = None,
    target_lng: float = None
):
    """
    Change the driver's current ride operating mode.
    
    Use this tool when the driver wants to change how rides are evaluated:
    
    PUDDLE JUMP MODE - Stay in a profitable local market
    Examples: "puddle jump", "puddle jump home", "stay in money market", "work the home market"
    → mode="puddle_jump", market_name="Home" (or "Money")
    
    TOWARDS MODE - Accept rides that move toward a destination
    Examples: "towards money", "head to money market", "go towards home", "heading downtown"
    → mode="towards", market_name="Money", target_lat=29.7604, target_lng=-95.3698
    
    FREESTYLE MODE - Accept any profitable ride regardless of location
    Examples: "freestyle", "go anywhere", "take anything good", "no restrictions"
    → mode="freestyle"
    
    Args:
        mode: One of "puddle_jump", "towards", or "freestyle"
        market_name: Name of the market (required for puddle_jump and towards)
        market_id: UUID of the market (optional, Android can look up)
        target_lat: Latitude of target location (required for towards mode)
        target_lng: Longitude of target location (required for towards mode)
    
    Returns: Verbal confirmation and app_action for Android to execute.
    """
    logging.info(f"🎯 RIDE MODE CHANGE: mode={mode}, market={market_name}, target=({target_lat}, {target_lng})")
    
    mode = mode.lower().strip()
    
    if mode == "puddle_jump":
        if not market_name:
            market_name = "current"
        
        reply = f"Switching to Puddle Jump mode, {market_name} market."
        app_action = f"SET_MODE_PUDDLE_JUMP|{market_name}|{market_id or ''}"
    
    elif mode == "towards":
        if not market_name:
            reply = "I need to know where you want to head. Which market - Home or Money?"
            app_action = None
        else:
            reply = f"Switching to Towards mode, heading to {market_name}."
            lat = target_lat or ""
            lng = target_lng or ""
            app_action = f"SET_MODE_TOWARDS|{market_name}|{lat}|{lng}|{market_id or ''}"
    
    elif mode == "freestyle":
        reply = "Switching to Freestyle mode. Accepting profitable rides anywhere."
        app_action = "SET_MODE_FREESTYLE"
    
    else:
        reply = f"I don't recognize the mode '{mode}'. Try 'puddle jump', 'towards', or 'freestyle'."
        app_action = None
    
    logging.info(f"🎯 RIDE MODE RESPONSE: reply='{reply}', app_action='{app_action}'")
    
    result = {"info": reply}
    if app_action:
        result["app_action"] = app_action
    
    return result


@tool
def get_current_ride_mode():
    """
    Get the driver's current ride operating mode.
    
    Use when driver asks:
    - "what mode am I in"
    - "what's my current mode"
    - "am I in puddle jump"
    - "where am I heading"
    
    Returns: Request for Android to report current mode status.
    """
    logging.info(f"🎯 GET CURRENT MODE requested")
    
    return {
        "info": "Let me check your current mode.",
        "app_action": "GET_CURRENT_MODE"
    }