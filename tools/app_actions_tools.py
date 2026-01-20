"""
App action tools for Puddles Brain.
Handles Android UI control commands that don't involve external APIs.
"""
import logging
from langchain_core.tools import tool


@tool
def trigger_app_action(action_type: str, parameters: dict = None):
    """
    Trigger Android app UI actions and features.
    
    Use when driver wants to control the app itself:
    
    Navigation modes:
    - "towards red zone", "towards green zone" → action_type="navigate_towards", parameters={"zone_color": "red"}
    - "go home", "head home" → action_type="navigate_towards", parameters={"destination": "home"}
    - "towards downtown" → action_type="navigate_towards", parameters={"destination": "downtown"}
    
    Trip controls:
    - "puddlejump", "end trip", "start trip" → action_type="puddlejump", parameters={}
    - "accept ride", "decline ride" → action_type="ride_decision", parameters={"accept": true/false}
    
    App features:
    - "show my stats", "show logs", "show history" → action_type="show_logs", parameters={}
    - "toggle auto accept", "turn on auto mode" → action_type="toggle_feature", parameters={"feature": "auto_accept"}
    
    Args:
        action_type: The action category (see examples above)
        parameters: Dictionary with action-specific data
    
    Returns: Confirmation message. Android app will handle the actual UI changes.
    """
    if parameters is None:
        parameters = {}
    
    logging.info(f"📱 APP ACTION: type={action_type}, params={parameters}")
    
    # Build response message based on action type
    if action_type == "navigate_towards":
        zone_color = parameters.get("zone_color")
        destination = parameters.get("destination")
        
        if zone_color:
            msg = f"Navigating towards {zone_color} zones."
        elif destination:
            msg = f"Heading towards {destination}."
        else:
            msg = "Starting navigation mode."
    
    elif action_type == "puddlejump":
        msg = "Executing PuddleJump."
    
    elif action_type == "show_logs":
        msg = "Opening your activity logs."
    
    elif action_type == "toggle_feature":
        feature = parameters.get("feature", "feature")
        msg = f"Toggling {feature.replace('_', ' ')}."
    
    elif action_type == "ride_decision":
        accept = parameters.get("accept", True)
        msg = "Accepting ride." if accept else "Declining ride."
    
    else:
        msg = f"Executing {action_type}."
    
    return {
        "info": msg,
        "app_action": {
            "type": action_type,
            "parameters": parameters
        }
    }
