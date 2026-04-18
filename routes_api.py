"""routes_api.py — Google Maps Routes API client for Contest Mode (Rev 00557).

EXECUTE-layer module. Sync HTTP call to computeRoutes endpoint, returns
encoded polyline + metadata. All failures return None — never raises.

Two call patterns:
  - fetch_trip_polyline(): called at offer_accepted for initial trip route
  - fetch_feasible_change(): called during scoring when Feasible Change gates met
"""

import os
import time
import json
import logging
import urllib.request
import urllib.error

_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY")
_ENDPOINT = "https://routes.googleapis.com/directions/v2:computeRoutes"
_TIMEOUT_S = 3.0  # hard ceiling — Feasible Change must not block heartbeat long
_FIELD_MASK = "routes.duration,routes.distanceMeters,routes.polyline.encodedPolyline"


def _call_routes_api(origin_lat, origin_lng, dest_lat, dest_lng,
                     purpose: str) -> dict:
    """Internal: single HTTP call, returns dict with status + polyline + metadata.
    Never raises — always returns a dict with at minimum {'status': str}."""
    if not _API_KEY:
        return {'status': 'no_api_key'}

    body = {
        "origin":      {"location": {"latLng": {"latitude": origin_lat, "longitude": origin_lng}}},
        "destination": {"location": {"latLng": {"latitude": dest_lat,   "longitude": dest_lng}}},
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_AWARE",
        "polylineEncoding":  "ENCODED_POLYLINE",
    }

    t0 = time.time()
    try:
        req = urllib.request.Request(
            _ENDPOINT,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-Goog-Api-Key": _API_KEY,
                "X-Goog-FieldMask": _FIELD_MASK,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            raw = resp.read().decode("utf-8")
            elapsed_ms = int((time.time() - t0) * 1000)
            data = json.loads(raw)
            routes = data.get("routes", [])
            if not routes:
                logging.warning(f"[ROUTES_API/{purpose}] empty routes array, {elapsed_ms}ms")
                return {'status': 'no_route', 'latency_ms': elapsed_ms}
            r0 = routes[0]
            polyline = (r0.get("polyline") or {}).get("encodedPolyline")
            if not polyline:
                return {'status': 'no_polyline', 'latency_ms': elapsed_ms}
            return {
                'status':          'ok',
                'encoded_polyline': polyline,
                'distance_m':      r0.get("distanceMeters"),
                'duration_s':      r0.get("duration", "").rstrip("s"),
                'latency_ms':      elapsed_ms,
            }
    except urllib.error.HTTPError as e:
        elapsed_ms = int((time.time() - t0) * 1000)
        logging.warning(f"[ROUTES_API/{purpose}] HTTP {e.code}, {elapsed_ms}ms")
        return {'status': f'http_{e.code}', 'latency_ms': elapsed_ms}
    except urllib.error.URLError as e:
        elapsed_ms = int((time.time() - t0) * 1000)
        logging.warning(f"[ROUTES_API/{purpose}] URLError: {e.reason}, {elapsed_ms}ms")
        return {'status': 'network_error', 'latency_ms': elapsed_ms}
    except Exception as e:
        elapsed_ms = int((time.time() - t0) * 1000)
        logging.exception(f"[ROUTES_API/{purpose}] unexpected: {e}")
        return {'status': 'exception', 'latency_ms': elapsed_ms}


def fetch_trip_polyline(pickup_lat, pickup_lng, dropoff_lat, dropoff_lng) -> dict:
    """Called at offer_accepted. Fetches the initial trip polyline.
    Returns dict with status and (on success) encoded_polyline."""
    return _call_routes_api(pickup_lat, pickup_lng, dropoff_lat, dropoff_lng, "trip_init")


def fetch_feasible_change(current_lat, current_lng, target_lat, target_lng) -> dict:
    """Called during Scorer B when Feasible Change gates are met.
    Asks: "is there a feasible route from here to target?" Returns dict."""
    return _call_routes_api(current_lat, current_lng, target_lat, target_lng, "feasible_change")
