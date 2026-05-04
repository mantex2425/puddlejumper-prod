# backend/nail_it_core.py
# Shared logic for Nail It confirmation — pickup and dropoff
#
# Incremental Bullseye: subsequent Nail Its overwrite with better
# coords only if improvement >= REFINEMENT_MIN_GAIN_M
#
# 2026-05-04 Commit 3b: state-machine-era scaffolding removed.
#   Deleted: check_convergence, Watchdog A/B, contest subsystem,
#            stop buffer, convergence/XTE helpers, process_heartbeat.
#   Convergence is now handled implicitly by cluster_tightness in WAI;
#   _execute_action in driver_heartbeat is the unified write surface.

import logging
import math as _math
from psycopg2.extras import RealDictCursor

# ─────────────────────────────────────────────────────────────────────────────
# Classification thresholds — used by classify() and the radius helpers
# ─────────────────────────────────────────────────────────────────────────────

BULLSEYE_THRESHOLD_M  = 400
ON_TARGET_THRESHOLD_M = 800
REFINEMENT_MIN_GAIN_M = 50  # minimum improvement to overwrite

# Median-centroid tunables (kept for bead_on_wire.py)
ROUTE_LOWSPEED_MAX_MPS = 1.0   # velocity filter for median centroid
ROUTE_MIN_LOWSPEED_PTS = 3     # minimum low-speed points to compute centroid


# ─────────────────────────────────────────────────────────────────────────────
# Error classification + computation
# ─────────────────────────────────────────────────────────────────────────────

def classify(error_m):
    """Classify triangulation error into bullseye/on_target/miss."""
    if error_m is None:
        return "no_baseline"
    if error_m <= BULLSEYE_THRESHOLD_M:
        return "bullseye"
    if error_m <= ON_TARGET_THRESHOLD_M:
        return "on_target"
    return "miss"


def compute_error(cur, actual_lat, actual_lng, triangulated_h3):
    """
    Compute actual H3 and error distance vs triangulated H3.
    Returns (actual_h3, error_m) — error_m is None if no triangulation.
    """
    if triangulated_h3:
        cur.execute("""
            WITH actual AS (
                SELECT
                    app_private.coords_to_h3(%s, %s) AS actual_h3,
                    app_private.coords_to_geography(%s, %s) AS actual_point
            ),
            triangulated AS (
                SELECT app_private.coords_to_geography(
                    app_private.h3_to_lat(%s),
                    app_private.h3_to_lng(%s)
                ) AS tri_point
            )
            SELECT
                actual.actual_h3::text,
                round(ST_Distance(actual.actual_point, triangulated.tri_point)::numeric, 1) AS error_m
            FROM actual, triangulated
        """, (actual_lat, actual_lng, actual_lat, actual_lng,
              triangulated_h3, triangulated_h3))
        row = cur.fetchone()
        return row['actual_h3'], float(row['error_m'])
    else:
        cur.execute(
            "SELECT app_private.coords_to_h3(%s, %s)::text AS actual_h3",
            (actual_lat, actual_lng)
        )
        row = cur.fetchone()
        return row['actual_h3'], None


def should_refine(existing_error_m, new_error_m):
    """
    Incremental bullseye check.
    Returns (should_update, improvement_m).
    If no existing confirmation, always update.
    Only update if new error is meaningfully better.
    """
    if existing_error_m is None:
        return True, None  # first confirmation — always write
    if new_error_m is None:
        return True, None  # no baseline — always write
    improvement = existing_error_m - new_error_m
    if improvement >= REFINEMENT_MIN_GAIN_M:
        return True, improvement
    return False, improvement


# ─────────────────────────────────────────────────────────────────────────────
# Voice + accuracy stats — used by manual-nail confirms
# ─────────────────────────────────────────────────────────────────────────────

def build_voice(total, error_m, target_type="pickup"):
    """Build TTS voice string for confirmation."""
    if error_m is None:
        return f"{target_type.capitalize()} confirmed."
    if total == 1:
        return f"First {target_type} confirmation. {int(error_m)} meters."
    return f"{total} {target_type}s confirmed. {int(error_m)} meters."


def get_accuracy_stats(cur, driver_id, target_type="pickup"):
    """Fetch running accuracy stats for this driver."""
    if target_type == "pickup":
        cur.execute("""
            SELECT
                COUNT(*) AS total_confirmed,
                round(100.0 * COUNT(*) FILTER (WHERE triangulation_error_m <= 800)
                    / NULLIF(COUNT(*), 0), 1) AS pct_on_target,
                round(AVG(triangulation_error_m)::numeric, 0) AS avg_error_m
            FROM app_private.pickup_market_signals pms
            JOIN app_private.decision_log dl ON dl.id = pms.offer_id
            WHERE dl.driver_id = %s
              AND pms.actual_pickup_at IS NOT NULL
        """, (driver_id,))
    else:
        cur.execute("""
            SELECT
                COUNT(*) AS total_confirmed,
                round(100.0 * COUNT(*) FILTER (WHERE dropoff_error_m <= 800)
                    / NULLIF(COUNT(*), 0), 1) AS pct_on_target,
                round(AVG(dropoff_error_m)::numeric, 0) AS avg_error_m
            FROM app_private.offer_history oh
            JOIN app_private.decision_log dl ON dl.id = oh.decision_log_id
            WHERE dl.driver_id = %s
              AND oh.actual_dropoff_at IS NOT NULL
        """, (driver_id,))
    row = cur.fetchone()
    return {
        "totalConfirmed": int(row['total_confirmed']),
        "pctOnTarget":    float(row['pct_on_target']) if row['pct_on_target'] else 0.0,
        "avgErrorMeters": float(row['avg_error_m']) if row['avg_error_m'] else 0.0,
    }


# ─────────────────────────────────────────────────────────────────────────────
# write_nailed_position — used by driver_heartbeat._execute_action and confirms
# ─────────────────────────────────────────────────────────────────────────────

def write_nailed_position(cur, driver_id, target_type, lat, lng, error_m):
    """
    Write confirmed nail position to driver_trip_state.
    target_type: 'pickup' or 'dropoff'
    Called by pickup_confirm.py and dropoff_confirm.py after successful Nail It.
    """
    if target_type == "pickup":
        col_lat, col_lng, col_err = (
            "nailed_pickup_lat", "nailed_pickup_lng", "nailed_pickup_error_m"
        )
    elif target_type == "dropoff":
        col_lat, col_lng, col_err = (
            "nailed_dropoff_lat", "nailed_dropoff_lng", "nailed_dropoff_error_m"
        )
    else:
        raise ValueError(f"write_nailed_position: unknown target_type '{target_type}'")

    cur.execute(f"""
        UPDATE app_private.driver_trip_state
        SET {col_lat} = %s,
            {col_lng} = %s,
            {col_err} = %s
        WHERE driver_id = %s
    """, (lat, lng, error_m, driver_id))
    logging.info(f"📍 nailed_{target_type} written: {lat:.5f},{lng:.5f} ±{error_m}m")


# ─────────────────────────────────────────────────────────────────────────────
# get_next_stacked_offer — used by dropoff_confirm.py (orphaned post-3b dropoff
# rewire; preserved for now, deferred cleanup tracked in runbook §8)
# ─────────────────────────────────────────────────────────────────────────────

def get_next_stacked_offer(cur, driver_id: str) -> dict | None:
    """DIAGNOSE: Find the next unstarted accepted offer for a STACKED swap.
    Returns dict with decision_log_id, dropoff_lat, dropoff_lng, dropoff_h3
    or None if not found.
    Uses FIFO order (ASC) — correct for triple-stack scenarios.
    Filters: verdict=ACCEPT, actual_pickup_at IS NULL (not yet started).
    """
    try:
        cur.execute("""
            SELECT oh.decision_log_id,
                   oh.dropoff_lat, oh.dropoff_lng, oh.dropoff_h3
            FROM app_private.offer_history oh
            JOIN app_private.driver_trip_state dts
                   ON dts.current_offer_id::integer = oh.decision_log_id
            WHERE dts.driver_id = %s
              AND oh.actual_pickup_at IS NULL
        """, (driver_id,))
        return cur.fetchone()
    except Exception as e:
        logging.warning(f"[S17] get_next_stacked_offer failed: {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Geometry primitives — used by bead_on_wire.py
# ─────────────────────────────────────────────────────────────────────────────

def median_centroid(cluster_points: list) -> tuple:
    """DIAGNOSE: Gemini's Median Centroid — independent medians of lat and lng
    over points where velocity < ROUTE_LOWSPEED_MAX_MPS (1.0 m/s).

    Returns (lat, lng, num_lowspeed_points) or (None, None, 0) if insufficient
    low-speed data. The caller is responsible for handling None.

    Why medians: robust to GPS jitter at low speed (multipath, urban canyons).
    Why independent (lat & lng separately): cheaper than geometric median,
    sufficient for 2D jitter correction in L1 space.
    """
    # Filter to low-speed points (curb truth, not decel/accel noise)
    # speed_mph is what the buffer stores; convert threshold to mph
    lowspeed_threshold_mph = ROUTE_LOWSPEED_MAX_MPS * 2.23694  # ~2.24 mph
    lowspeed = [p for p in cluster_points if p.get('speed_mph', 999) < lowspeed_threshold_mph]
    if len(lowspeed) < ROUTE_MIN_LOWSPEED_PTS:
        return (None, None, len(lowspeed))
    lats = sorted(p['lat'] for p in lowspeed)
    lngs = sorted(p['lng'] for p in lowspeed)
    n = len(lowspeed)
    # Median: middle value for odd n, average of two middles for even n
    if n % 2 == 1:
        return (lats[n // 2], lngs[n // 2], n)
    return ((lats[n // 2 - 1] + lats[n // 2]) / 2.0,
            (lngs[n // 2 - 1] + lngs[n // 2]) / 2.0,
            n)


def decode_polyline(encoded: str) -> list:
    """DIAGNOSE: Google's standard polyline algorithm decoder.
    Returns list of (lat, lng) tuples. Handles malformed input by returning
    an empty list (safer than raising — we want graceful degradation)."""
    if not encoded:
        return []
    try:
        points = []
        index = 0
        lat = 0
        lng = 0
        length = len(encoded)
        while index < length:
            for coord in ('lat', 'lng'):
                shift = 0
                result = 0
                while True:
                    if index >= length:
                        return points  # truncated — return what we have
                    b = ord(encoded[index]) - 63
                    index += 1
                    result |= (b & 0x1f) << shift
                    shift += 5
                    if b < 0x20:
                        break
                delta = ~(result >> 1) if (result & 1) else (result >> 1)
                if coord == 'lat':
                    lat += delta
                else:
                    lng += delta
            points.append((lat / 1e5, lng / 1e5))
        return points
    except Exception:
        return []  # fail-safe: never raise from a decoder


def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Haversine distance in meters between two lat/lng points. Private helper."""
    R = 6371000.0  # Earth radius in meters
    phi1 = _math.radians(lat1)
    phi2 = _math.radians(lat2)
    dphi = _math.radians(lat2 - lat1)
    dlam = _math.radians(lng2 - lng1)
    a = _math.sin(dphi / 2) ** 2 + _math.cos(phi1) * _math.cos(phi2) * _math.sin(dlam / 2) ** 2
    return 2 * R * _math.asin(_math.sqrt(a))


# ─────────────────────────────────────────────────────────────────────────────
# Address vagueness + radius helpers — used by bead_on_wire.py
# ─────────────────────────────────────────────────────────────────────────────

VAGUE_KEYWORDS = {
    'hwy', 'highway', 'pkwy', 'parkway', 'fwy', 'freeway',
    'beltway', 'tollway', 'loop', 'expressway',
    'motorway', 'autobahn', 'autoroute', 'autopista', 'autostrada',
    'ring road', 'orbital', 'bypass', 'skyway', 'causeway',
    'service road', 'frontage road', 'i-', 'us-', 'sr-', 'cr-'
}


def is_vague_address(address: str) -> bool:
    """Return True if address looks like a vague highway/linear feature."""
    if not address:
        return False
    lower = address.lower()
    return any(kw in lower for kw in VAGUE_KEYWORDS)


def get_pickup_confirm_radius(address: str) -> int:
    """Return pickup confirm radius based on address type.
    null address  → 1000m (triangulation only, max uncertainty)
    vague address → 800m  (road/highway centroid)
    precise       → 200m  (hard lock)
    """
    if not address:
        return 1000
    if is_vague_address(address):
        return 800
    return 200


def get_armed_radius(address: str) -> float:
    # 4-Box Symmetry Mandate: Universal 1500m net — eliminates pin-error dead zone
    return 1500.0