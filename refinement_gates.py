"""
refinement_gates.py — Policy gates for coordinate refinement (Patch 00566a Step 3)

Single source of truth for *whether* to run dropoff coordinate refinement.
Separate from the geometric engine (triangulation.refine_target_by_geometry) —
this module answers "should we refine?", the engine answers "how do we refine?".

Used by all three dropoff-refinement sites to guarantee identical behavior:
  1. decisions/triangulation_enricher.py  — offer-time refinement
  2. pickup_confirm.py                    — dev-path SB3 (manual Nail It)
  3. driver_heartbeat.py INITIAL_NAIL     — commercial-path SB3 (Auto Nail)

Design: return (bool, reason) tuple. Callers log the reason on skip so
post-hoc analysis can catch edge cases we didn't anticipate.
"""

import logging
import math


def _haversine_miles(lat1, lng1, lat2, lng2):
    """Great-circle distance in miles. Self-contained (originally duplicated from triangulation._haversine_miles before that module was removed)
    to keep this module free of circular imports. Small function; small cost."""
    R = 3958.8
    lat1_r, lng1_r, lat2_r, lng2_r = map(math.radians, [lat1, lng1, lat2, lng2])
    dlat = lat2_r - lat1_r
    dlng = lng2_r - lng1_r
    a = (math.sin(dlat / 2) ** 2
         + math.cos(lat1_r) * math.cos(lat2_r) * math.sin(dlng / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


# Noise-floor threshold — trips shorter than this are errands/circular
# and refinement has more variance than signal. Value is empirical; revisit
# with Round 2 data.
NOISE_FLOOR_MILES = 0.3

# Circular-trip guard — if the anchor (nailed pickup) and our current
# dropoff estimate are within this many meters, treat as errand/return-trip
# class and skip. (Note: Uber does not provide a dropoff pin; the 'estimate'
# is our own earlier geocoding of the dropoff address.)
CIRCULAR_GUARD_METERS = 50.0


def should_refine_target(
    pickup_addr,
    dropoff_addr,
    anchor_lat,
    anchor_lng,
    initial_target_estimate_lat,
    initial_target_estimate_lng,
    trip_miles,
):
    """
    Decide whether to run dropoff coordinate refinement for a given offer.

    Returns (should_refine: bool, reason: str).

    Guards (short-circuit on first fail):
      1. same_address_errand — pickup and dropoff address strings match
         (case-insensitive, trimmed)
      2. circular_within_50m — anchor and current dropoff estimate are
         within CIRCULAR_GUARD_METERS of each other (anchor ≈ estimate)
      3. trip_miles_below_noise_floor — trip_miles is None, zero, or
         below NOISE_FLOOR_MILES

    Pass condition: ("ok" reason) all three guards clear.

    Intentionally excluded (not this function's job):
      - Street-name sanity (handled inside engine Tier 0/1/2 by _is_vague_single_road)
      - Anchor validity (caller must verify nailed_pickup coords before calling)
      - Database state (caller owns transaction scope)
    """
    # Guard 1: same-address errand
    p = (pickup_addr or "").strip().lower()
    d = (dropoff_addr or "").strip().lower()
    if p and d and p == d:
        return False, "same_address_errand"

    # Guard 2: circular within 50m
    if all([anchor_lat, anchor_lng, initial_target_estimate_lat, initial_target_estimate_lng]):
        dist_m = _haversine_miles(
            anchor_lat, anchor_lng,
            initial_target_estimate_lat, initial_target_estimate_lng,
        ) * 1609.34
        if dist_m < CIRCULAR_GUARD_METERS:
            return False, "circular_within_50m"

    # Guard 3: noise floor
    if trip_miles is None or trip_miles < NOISE_FLOOR_MILES:
        return False, "trip_miles_below_noise_floor"

    return True, "ok"
