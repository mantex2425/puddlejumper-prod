"""
Puddles Brain Tools Package
Exports all LangChain tools for the Puddles AI assistant.
"""

# Navigation tools
from .navigation_tools import (
    get_live_traffic,
    get_directions,
    send_to_navigation
)

# Places discovery
from .places_tools import find_nearby_places, get_place_details

# App UI actions
from .app_actions_tools import trigger_app_action

# Knowledge & search
from .knowledge_tools import (
    check_internal_knowledge,
    web_search
)

# Weather tool (New)
from .weather import get_weather  # 👈 This line is required!

# Helper functions (not tools, but useful for direct imports)
from .helpers import geocode_destination, nice_place_name

__all__ = [
    # Navigation
    'get_live_traffic',
    'get_directions',
    'send_to_navigation',
    # Places
    'find_nearby_places',
    'get_place_details', 
    # App actions
    'trigger_app_action',
    # Knowledge
    'check_internal_knowledge',
    'web_search',
    # Helpers
    'geocode_destination',
    'nice_place_name',
    # Weather
    'get_weather'  # 👈 Now this reference is valid
]