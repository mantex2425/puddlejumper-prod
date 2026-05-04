"""
geo_utils.py — Geocoding infrastructure (Google API + result cache).

SCOPE: Infrastructure module. OUTSIDE the 4-box framework.

The 4-box framework (MONITOR/DIAGNOSE/PLAN/EXECUTE) governs ride
lifecycle state — driver_trip_state.current_offer_id (the 1-bit memory),
and the pickup→nailed→dropoff invariants that _execute_action in
driver_heartbeat enforces.

This module handles a side-car cache: a performance/cost optimization
for Google Geocoding API calls. No ride-lifecycle invariants apply.
Cache writes here are not lifecycle transitions and do not flow
through _execute_action. That is deliberate and correct.

DO NOT add business logic or ride-state mutations here. If a future
function would affect driver_trip_state, offer_history, or the
ride-lifecycle state machine, it does not belong in this file.
"""

import os
import re
import logging
import requests

GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY")


def _google_geocode(address: str, bias_lat: float = None,
                    bias_lng: float = None) -> tuple | None:
    """Call Google's geocoding API. Returns (lat, lng) or None.

    Bounded to 2-second timeout. Never raises — returns None on any error.
    """
    if not GOOGLE_MAPS_API_KEY or not address:
        return None
    try:
        params = {"address": address, "key": GOOGLE_MAPS_API_KEY}
        if bias_lat is not None and bias_lng is not None:
            params["location"] = f"{bias_lat},{bias_lng}"
            params["radius"] = "50000"
        r = requests.get(
            "https://maps.googleapis.com/maps/api/geocode/json",
            params=params,
            timeout=2.0
        )
        data = r.json()
        if data["status"] == "OK":
            loc = data["results"][0]["geometry"]["location"]
            logging.info(
                f"[Google] '{address}' → ({loc['lat']:.5f}, {loc['lng']:.5f}) "
                f"[{data['results'][0]['formatted_address']}]"
            )
            return loc["lat"], loc["lng"]
        logging.warning(f"[Google] Geocode failed for '{address}': {data['status']}")
    except requests.Timeout:
        logging.warning(f"[Google] Geocode timeout for '{address}'")
    except Exception as e:
        logging.warning(f"[Google] Geocode exception: {e}")
    return None


def _normalize_address(address: str) -> str:
    """Cache-key normalization: strip punctuation, lowercase, collapse spaces."""
    if not address:
        return ""
    cleaned = re.sub(r'[^\w\s]', ' ', address)
    return ' '.join(cleaned.strip().lower().split())


def _lookup_geocode_cache(address: str, cur) -> tuple | None:
    """Cache read. Returns (lat, lng) or None.

    Updates hit_count + last_used_at on hit. This is a cache-hit-metric
    bookkeeping write, not a ride-state mutation.
    """
    if not address:
        return None
    norm = _normalize_address(address)
    try:
        cur.execute(
            "SELECT lat, lng FROM app_private.geocode_cache WHERE address_text = %s",
            (norm,)
        )
        row = cur.fetchone()
        if row:
            logging.info(f"[Geocode] CACHE HIT: '{address}'")
            cur.execute(
                "UPDATE app_private.geocode_cache "
                "SET last_used_at = NOW(), hit_count = hit_count + 1 "
                "WHERE address_text = %s",
                (norm,)
            )
            return row['lat'], row['lng']
    except Exception as e:
        logging.warning(f"[Geocode] Cache lookup failed: {e}")
    return None


def _write_geocode_cache(address: str, lat: float, lng: float, cur):
    """Cache write. Upsert on conflict."""
    if not address or lat is None or lng is None:
        return
    norm = _normalize_address(address)
    try:
        cur.execute("""
            INSERT INTO app_private.geocode_cache (address_text, lat, lng)
            VALUES (%s, %s, %s)
            ON CONFLICT (address_text) DO UPDATE
            SET last_used_at = NOW(),
                hit_count = geocode_cache.hit_count + 1
        """, (norm, lat, lng))
    except Exception as e:
        logging.warning(f"[Geocode] Cache write failed for '{address}': {e}")
