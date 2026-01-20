"""
Weather tool using Apple WeatherKit API.
"""
import jwt
import time
import requests
import os
import logging
from langchain_core.tools import tool

# Apple WeatherKit credentials
TEAM_ID = "9D6CPTUV83"
KEY_ID = "6BXN3CFB73"
SERVICE_ID = "com.atjb.puddles.weather"

@tool
def get_weather(current_gps: str) -> str:
    """
    Get current weather conditions and forecast. Use this for ANY weather-related questions.
    
    Triggers: "weather", "forecast", "temperature", "rain", "hot", "cold", "sunny", "cloudy"
    
    Args:
        current_gps: GPS coordinates in "lat,lng" format (e.g., "29.7604,-95.3698")
    
    Returns:
        Human-readable weather description with temperature and conditions.
    """
    try:
        # Get the private key from environment
        private_key = os.environ.get("APPLE_WEATHER_KEY")
        if not private_key:
            logging.error("APPLE_WEATHER_KEY environment variable not set")
            return "Weather service is currently unavailable."

        # Ensure proper newline formatting in the key
        if "\\n" in private_key:
            private_key = private_key.replace("\\n", "\n")

        # Generate JWT for Apple WeatherKit
        header = {
            "alg": "ES256",
            "kid": KEY_ID,
            "id": f"{TEAM_ID}.{SERVICE_ID}"
        }
        
        payload = {
            "iss": TEAM_ID,
            "iat": int(time.time()),
            "exp": int(time.time()) + 3600,
            "sub": SERVICE_ID
        }
        
        token = jwt.encode(payload, private_key, algorithm="ES256", headers=header)

        # Call Apple WeatherKit API
        # Split coordinates
        lat, lng = current_gps.split(",")
        url = f"https://weatherkit.apple.com/api/v1/weather/en/{lat}/{lng}"
        headers = {"Authorization": f"Bearer {token}"}
        params = {"dataSets": "currentWeather,forecastHourly"}
        
        logging.info(f"Calling WeatherKit API: {url}")
        response = requests.get(url, headers=headers, params=params, timeout=5)
        
        logging.info(f"WeatherKit response status: {response.status_code}")
        logging.info(f"WeatherKit response body: {response.text[:500]}")
        
        response.raise_for_status()
        data = response.json()
        
        # Extract current weather
        current = data.get('currentWeather', {})
        temp_c = current.get('temperature')
        condition = current.get('conditionCode', 'Unknown').replace('_', ' ').title()
        
        # Convert Celsius to Fahrenheit
        temp_f = round((temp_c * 9/5) + 32) if temp_c is not None else None
        
        if temp_f is None:
            return "Weather data is currently unavailable."
        
        return f"Currently {temp_f}°F and {condition}."

    except requests.exceptions.RequestException as e:
        logging.error(f"WeatherKit API Error: {e}")
        return "I can't reach the weather service right now."
    except Exception as e:
        logging.error(f"WeatherKit Error: {e}", exc_info=True)
        return "Weather service encountered an error."
