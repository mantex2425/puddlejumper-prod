# backend/nail_it_core.py
# Shared logic for Nail It confirmation — pickup and dropoff
#
# Incremental Bullseye: subsequent Nail Its overwrite with better
# coords only if improvement >= REFINEMENT_MIN_GAIN_M

import logging
from psycopg2.extras import RealDictCursor

BULLSEYE_THRESHOLD_M  = 400
ON_TARGET_THRESHOLD_M = 800
REFINEMENT_MIN_GAIN_M = 50  # minimum improvement to overwrite


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


# ---------------------------------------------------------------------------
# Convergence engine — called on every heartbeat from driver_heartbeat.py
# ---------------------------------------------------------------------------

def _log_nail_instrument(driver_id, path_name, fire_lat, fire_lng,
                          geocode_lat, geocode_lng):
    """Per-nail instrumentation: dist from fire coord to geocode + cluster median.
    Pure DIAGNOSE — reads buffer, writes only to logging. No DB, no state change.
    Fail-safe: any exception is swallowed (instrumentation must not break fires)."""
    try:
        # Dist to geocode
        if geocode_lat is not None and geocode_lng is not None:
            dlat_m = (float(geocode_lat) - float(fire_lat)) * 111320.0
            dlng_m = (float(geocode_lng) - float(fire_lng)) * 111320.0 * \
                     _math.cos(_math.radians(float(fire_lat)))
            dist_geo = _math.sqrt(dlat_m ** 2 + dlng_m ** 2)
            dist_geo_str = f"{dist_geo:.0f}m"
        else:
            dist_geo_str = "n/a"

        # Dist to cluster median
        buf = list(get_buffer(driver_id))
        med_lat, med_lng, n_pts = median_centroid(buf) if buf else (None, None, 0)
        if med_lat is not None and med_lng is not None:
            dlat_m = (med_lat - float(fire_lat)) * 111320.0
            dlng_m = (med_lng - float(fire_lng)) * 111320.0 * \
                     _math.cos(_math.radians(float(fire_lat)))
            dist_cluster = _math.sqrt(dlat_m ** 2 + dlng_m ** 2)
            dist_cluster_str = f"{dist_cluster:.0f}m (n={n_pts})"
        else:
            dist_cluster_str = f"n/a (n={n_pts})"

        logging.info(
            f"[NAIL_INSTRUMENT] path={path_name} "
            f"dist_to_geocode={dist_geo_str} "
            f"dist_to_cluster={dist_cluster_str} "
            f"fire_at=({float(fire_lat):.5f},{float(fire_lng):.5f})"
        )
    except Exception as _e:
        logging.warning(f"[NAIL_INSTRUMENT] failed (non-fatal): {_e}")



# Convergence thresholds
ARMED_RADIUS_M         = 1000   # enter linger zone — aggressive refinement begins (S29)
# NAIL_CONFIRM_SPEED_MPH: legacy inner-confirm speed gate, deleted from
# check_convergence. Retained solely for detect_passenger_stops() buffer
# segmentation — the low-speed threshold that defines a cluster segment.
# Rename deferred; semantic move to its own constant is cleanup-pass work.
NAIL_CONFIRM_SPEED_MPH = 5.0    # buffer segmentation threshold (detect_passenger_stops only)
REFINE_SPEED_MPH       = 15     # must be slow to refine
# ── NEW: Dual-Watchdog + Elastic Net for Dropoff ─────────────────────────────
ARMED_RADIUS_NORMAL_M  = 400    # intersections, businesses
ARMED_RADIUS_VAGUE_M   = 1500   # highways, vague linear addresses
WATCHDOG_A_SPEED_MPH   = 3.0    # Duration Fuse
WATCHDOG_A_DURATION_S  = 300   # Last resort only — 5 minutes (red lights can be 90s+)
WATCHDOG_B_SPEED_MPH   = 5.0    # Displacement Fuse micro-stop
WATCHDOG_B_MIN_S       = 3
WATCHDOG_B_MAX_S       = 8

# ── Phase 0: Unified Stop Buffer (DIAGNOSE box — pure reads, no writes) ──────
# Grok + Claude + Gemini consensus — shadow mode only
# Buffer fills silently; current Watchdog B / High Score logic 100% unchanged
# Passes all three enforcement gates: read-only, no DB, no sm_transition()
from collections import deque
import time as _time

BUFFER_SECONDS = 600  # 10 min — covers 5–7 min passenger waits
TEMPORAL_WINDOW_S = 60   # Houston-tuned: 45-50s passenger walk-ups still win

_stop_buffers: dict[str, deque] = {}

def process_heartbeat(driver_id: str, lat: float, lng: float,
                      speed_mph: float, heading=None) -> None:
    """DIAGNOSE: Append + prune raw heartbeat. Called from MONITOR only."""
    if not driver_id or lat is None or lng is None or speed_mph is None:
        return
    if driver_id not in _stop_buffers:
        _stop_buffers[driver_id] = deque()
    buffer = _stop_buffers[driver_id]
    now = _time.time()
    buffer.append({
        'lat':       lat,
        'lng':       lng,
        'speed_mph': speed_mph,
        'ts':        now,
        'heading':   heading,
    })
    while buffer and buffer[0]['ts'] < now - BUFFER_SECONDS:
        buffer.popleft()

def get_buffer(driver_id: str) -> deque:
    """DIAGNOSE: Read-only accessor for Phase 1+ scoring (shadow only)."""
    return _stop_buffers.get(driver_id, deque())

def clear_buffer(driver_id: str) -> int:
    """DIAGNOSE: Called from EXECUTE on trip-cycle boundaries.

    Returns the number of stops discarded (0 if buffer was empty or absent).
    Idempotent — safe to call on any state transition.
    """
    buffer = _stop_buffers.pop(driver_id, None)
    return len(buffer) if buffer is not None else 0


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


def _add_scored_segment(candidates, segment, min_speed, seg_start_ts, seg_end_ts, trigger_time):
    """Internal helper — pure DIAGNOSE, no side effects."""
    if not segment:
        return
    lats = [p['lat'] for p in segment]
    lngs = [p['lng'] for p in segment]
    centroid_lat = sum(lats) / len(lats)
    centroid_lng = sum(lngs) / len(lngs)
    mean_lat = centroid_lat
    variance_m = sum((lat - mean_lat)**2 for lat in lats) / len(lats)
    duration_s = seg_end_ts - seg_start_ts
    num_points = len(segment)
    time_since_trigger = trigger_time - seg_end_ts
    temporal_bonus = max(0.0, 1.0 - (time_since_trigger / TEMPORAL_WINDOW_S))
    score = (
        0.40 * (1.0 / (1.0 + variance_m)) +
        0.25 * (5.0 - min_speed) / 5.0 +
        0.15 * (num_points / 30.0) +
        0.10 * temporal_bonus +
        0.10 * min(1.0, duration_s / 420.0)
    )
    candidates.append({
        'centroid_lat':           centroid_lat,
        'centroid_lng':           centroid_lng,
        'duration_s':             duration_s,
        'min_speed_mph':          min_speed,
        'position_variance_m':    variance_m,
        'num_points':             num_points,
        'temporal_proximity_bonus': temporal_bonus,
        'score':                  score,
        'end_ts':                 seg_end_ts,
    })



# ════════════════════════════════════════════════════════════════════════════
# Route-Context Scoring Helpers (Revision 00557)
# ═══════════════════════════════════════════════════════════════════════════
# Pure DIAGNOSE functions — no DB, no I/O, no state, no side effects.
# Implements Gemini's technical spec for Scorer B (route-context).

import math as _math

# Configurable thresholds (tunable post-contest, not hardcoded deep in logic)
ROUTE_XTE_GATE_M           = 30.0    # ±30m snap window for on-track / deviation
ROUTE_LOWSPEED_MAX_MPS     = 1.0     # velocity filter for median centroid
ROUTE_UTURN_HEADING_DEG    = 120.0   # heading deviation for U-turn tolerance
ROUTE_LONG_STOP_S          = 15.0    # Feasible Change trigger: long-stop floor
ROUTE_NEAR_TARGET_M        = 200.0   # Feasible Change trigger: near-target gate
ROUTE_FINAL_NAIL_M         = 100.0   # (reserved — not used in Round 1)
ROUTE_MIN_LOWSPEED_PTS     = 3       # minimum low-speed points to compute centroid


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


def _xte_to_segment_m(lat: float, lng: float,
                      lat_a: float, lng_a: float,
                      lat_b: float, lng_b: float) -> float:
    """Perpendicular distance from (lat,lng) to the segment A→B, in meters.
    Uses planar projection — accurate for segments under ~10km, which is
    always true for individual polyline segments from Routes API."""
    # Convert to local planar coordinates (meters from segment start)
    # This is a small-area approximation; error is <0.1% for sub-km segments
    dlat_m = (lat_b - lat_a) * 111320.0
    dlng_m = (lng_b - lng_a) * 111320.0 * _math.cos(_math.radians(lat_a))
    plat_m = (lat - lat_a) * 111320.0
    plng_m = (lng - lng_a) * 111320.0 * _math.cos(_math.radians(lat_a))
    seg_len_sq = dlat_m ** 2 + dlng_m ** 2
    if seg_len_sq < 1e-6:
        # Degenerate segment — A and B are the same point
        return _math.sqrt(plat_m ** 2 + plng_m ** 2)
    # Project point onto segment, clamp to [0,1] to stay within segment bounds
    t = max(0.0, min(1.0, (plat_m * dlat_m + plng_m * dlng_m) / seg_len_sq))
    proj_lat_m = t * dlat_m
    proj_lng_m = t * dlng_m
    return _math.sqrt((plat_m - proj_lat_m) ** 2 + (plng_m - proj_lng_m) ** 2)


def cross_track_error(lat: float, lng: float, polyline_points: list) -> float:
    """DIAGNOSE: Minimum perpendicular distance (meters) from point to the
    nearest segment of the polyline. Returns None if polyline has <2 points."""
    if not polyline_points or len(polyline_points) < 2:
        return None
    min_xte = float('inf')
    for i in range(len(polyline_points) - 1):
        lat_a, lng_a = polyline_points[i]
        lat_b, lng_b = polyline_points[i + 1]
        d = _xte_to_segment_m(lat, lng, lat_a, lng_a, lat_b, lng_b)
        if d < min_xte:
            min_xte = d
    return min_xte if min_xte < float('inf') else None


def great_circle_xte(lat: float, lng: float,
                     origin_lat: float, origin_lng: float,
                     dest_lat: float, dest_lng: float) -> float:
    """DIAGNOSE: Fallback XTE when no cached polyline is available.
    Computes perpendicular distance from point to the straight-line path
    between origin and destination. Single-segment special case of cross_track_error."""
    return _xte_to_segment_m(lat, lng, origin_lat, origin_lng, dest_lat, dest_lng)


def _bearing_deg(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Initial bearing from point 1 to point 2, in degrees (0=N, 90=E)."""
    phi1 = _math.radians(lat1)
    phi2 = _math.radians(lat2)
    dlam = _math.radians(lng2 - lng1)
    y = _math.sin(dlam) * _math.cos(phi2)
    x = _math.cos(phi1) * _math.sin(phi2) - _math.sin(phi1) * _math.cos(phi2) * _math.cos(dlam)
    return (_math.degrees(_math.atan2(y, x)) + 360.0) % 360.0


def heading_deviation_deg(heading: float,
                          from_lat: float, from_lng: float,
                          to_lat: float, to_lng: float) -> float:
    """DIAGNOSE: Absolute deviation (0-180°) between a vehicle heading
    and the bearing from-to. Used for U-turn tolerance rule.
    Returns None if heading is None (some heartbeats lack heading)."""
    if heading is None:
        return None
    target_bearing = _bearing_deg(from_lat, from_lng, to_lat, to_lng)
    diff = abs(heading - target_bearing) % 360.0
    return diff if diff <= 180.0 else 360.0 - diff


def route_context_score(centroid_lat: float, centroid_lng: float,
                        xte_m: float,
                        heading_deviation: float = None) -> tuple:
    """DIAGNOSE: Scorer B's score, in [0.0, 1.0].

    Returns (score, u_turn_tolerance_applied: bool).

    Semantics (from Gemini's spec):
      - XTE ≤ 30m           → high confidence on-track (likely traffic/light)
                              LOW PUDO score (this is NOT a passenger stop)
      - XTE > 30m           → deviation candidate (likely PUDO)
                              HIGH PUDO score scaled by deviation magnitude
      - Heading >120° from target bearing + within ±30m gate → U-turn tolerance
                              Treat as on-track despite heading (Texas feeder U-turn)

    Note: Scorer B scores the probability that a cluster IS a PUDO stop.
    Higher score = more likely PUDO. Inverse of Scorer A's semantics.
    """
    if xte_m is None:
        return (None, False)

    u_turn_applied = False

    # U-turn tolerance: if centroid is within gate and heading is reversed,
    # classify as on-track regardless of heading
    if (xte_m <= ROUTE_XTE_GATE_M
            and heading_deviation is not None
            and heading_deviation > ROUTE_UTURN_HEADING_DEG):
        u_turn_applied = True

    if xte_m <= ROUTE_XTE_GATE_M:
        # On-track: low PUDO probability. Score scales inversely with proximity.
        # At XTE=0: score=0.10 (very unlikely PUDO)
        # At XTE=30m: score=0.40 (edge case, modest PUDO probability)
        return (0.10 + 0.30 * (xte_m / ROUTE_XTE_GATE_M), u_turn_applied)
    else:
        # Off-track: high PUDO probability. Score saturates past ~150m deviation.
        # At XTE=30m: score=0.60
        # At XTE=150m+: score=0.95
        deviation_beyond_gate = xte_m - ROUTE_XTE_GATE_M
        saturation = min(1.0, deviation_beyond_gate / 120.0)
        return (0.60 + 0.35 * saturation, u_turn_applied)


def feasible_change_triggered(stop_duration_s: float,
                              xte_m: float,
                              distance_to_target_m: float) -> bool:
    """DIAGNOSE: Evaluates all three of Gemini's Feasible Change gates.
    Returns True only if all three are met."""
    if xte_m is None or distance_to_target_m is None:
        return False
    return (stop_duration_s >= ROUTE_LONG_STOP_S
            and xte_m > ROUTE_XTE_GATE_M
            and distance_to_target_m < ROUTE_NEAR_TARGET_M)


# ════════════════════════════════════════════════════════════════════════════
# End of route-context scoring helpers
# ═══════════════════════════════════════════════════════════════════════════


def detect_passenger_stops(driver_id: str, trigger_time: float, verdict_type: str) -> list:
    """DIAGNOSE: Pure read-only. Segments buffer into micro-stops and scores them.
    Called from check_convergence() for shadow scoring (Phase 1) or full nailing (Phase 2).
    Returns list of dicts sorted by composite score (highest first)."""
    buffer = get_buffer(driver_id)
    if len(buffer) < 3:
        return []
    candidates = []
    current_segment = []
    min_speed_in_seg = 999.0
    seg_start_ts = None
    for pt in buffer:
        speed = pt['speed_mph']
        ts = pt['ts']
        if speed < NAIL_CONFIRM_SPEED_MPH:
            if not current_segment:
                seg_start_ts = ts
                min_speed_in_seg = speed
            current_segment.append(pt)
            min_speed_in_seg = min(min_speed_in_seg, speed)
        else:
            if current_segment and (ts - seg_start_ts) >= WATCHDOG_B_MIN_S:
                _add_scored_segment(candidates, current_segment, min_speed_in_seg,
                                    seg_start_ts, ts, trigger_time)
            current_segment = []
            min_speed_in_seg = 999.0
    if current_segment and (buffer[-1]['ts'] - seg_start_ts) >= WATCHDOG_B_MIN_S:
        _add_scored_segment(candidates, current_segment, min_speed_in_seg,
                            seg_start_ts, buffer[-1]['ts'], trigger_time)
    candidates.sort(key=lambda c: c['score'], reverse=True)
    return candidates
DEPARTURE_DISTANCE_M   = 300
DEPARTURE_SPEED_MPH    = 15.0
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

# ════════════════════════════════════════════════════════════════════════════
# Contest Mode (Revision 00557) — Module-level state
# ════════════════════════════════════════════════════════════════════════════
# Guards 1 & 3 from Round 1 design:
#   - _contest_kill_switches[(driver_id, trip_id)] = {'scorer_a': bool, 'scorer_b': bool}
#     Tracks whether each scorer has raised an exception during this trip.
#     Tripped kill switch → that scorer skipped for remainder of trip.
#   - _contest_nails_fired[(driver_id, trip_id)] = {'scorer_a': bool, 'scorer_b': bool}
#     Guard 3: one-nail-per-trip-per-scorer. Prevents double-nailing.
#
# Both dicts reset when a trip transitions to UNCOMMITTED (cleared via the
# same trip-boundary mechanism that clears _stop_buffers).

_contest_kill_switches: dict[tuple, dict] = {}
_contest_nails_fired: dict[tuple, dict] = {}

# Scorer B must beat Scorer A by this margin to override candidate selection.
# Tunable post-contest — too low and Scorer B hijacks on noise; too high and
# Scorer B never gets to demonstrate improvement.
CONTEST_SCORER_B_MARGIN = 0.15

# Armed-radius multiplier for Scorer B centroid override.
# Even if Scorer B wins the score contest, its chosen centroid must lie within
# this multiple of the armed_radius to be allowed to override Scorer A.
# Guard 2: "implausible nail" sanity gate.
CONTEST_OVERRIDE_RADIUS_MULTIPLIER = 1.0  # i.e., within armed_radius


def _contest_trip_key(driver_id: str, state_row: dict) -> tuple:
    """Contest bookkeeping is per-(driver, trip). Use offer_id as trip identifier.
    Falls back to driver_id alone if offer_id is missing (UNCOMMITTED state)."""
    offer_id = state_row.get('current_offer_id') if state_row else None
    return (driver_id, offer_id) if offer_id else (driver_id, None)


def _contest_kill_switch_check(trip_key: tuple, scorer: str) -> bool:
    """Returns True if scorer is tripped (should be skipped). False if healthy."""
    return _contest_kill_switches.get(trip_key, {}).get(scorer, False)


def _contest_trip_kill(trip_key: tuple, scorer: str, reason: str) -> None:
    """Guard 1: trip the kill switch for a scorer on this trip."""
    import logging
    _contest_kill_switches.setdefault(trip_key, {})[scorer] = True
    logging.warning(f"[CONTEST] Kill switch tripped: {scorer} on trip {trip_key} — {reason}")


def _contest_nail_already_fired(trip_key: tuple, scorer: str) -> bool:
    """Guard 3: has this scorer already fired a nail this trip?"""
    return _contest_nails_fired.get(trip_key, {}).get(scorer, False)


def _contest_mark_nail_fired(trip_key: tuple, scorer: str) -> None:
    """Guard 3: record that this scorer has nailed on this trip."""
    _contest_nails_fired.setdefault(trip_key, {})[scorer] = True


def _contest_clear_trip_state(driver_id: str) -> None:
    """Called from trip-boundary events (UNCOMMITTED transition) to reset
    kill switches and nail flags. Invoked from state_machine.transition()
    alongside clear_buffer()."""
    # Drop every entry whose first tuple element is this driver_id
    for d in (_contest_kill_switches, _contest_nails_fired):
        for k in list(d.keys()):
            if k[0] == driver_id:
                d.pop(k, None)


def _scorer_a_numeric_score(dist_m: float) -> float:
    """Formalizes the existing [HIGH_SCORE] logic as a numeric score in [0,1].
    Higher score = more confident this is a real PUDO stop at the pin.

    Production semantics preserved: closer to pin = better. 50m→0.67, 100m→0.50,
    200m→0.33, 500m→0.17. Smooth decay, no discontinuities.

    This is what the existing [HIGH_SCORE] block already computes implicitly
    via `dist_m < prev_dist`. We just reify it as a number for the contest."""
    if dist_m is None or dist_m < 0:
        return 0.0
    return 1.0 / (1.0 + dist_m / 100.0)


def _write_contest_event(cur, conn, driver_id, state, state_row,
                         current_lat, current_lng, current_speed_mph,
                         stopped_seconds, dist_m,
                         score_a, score_b, scorer_b_data, nail_fired_by):
    """EXECUTE: Write one contest_events row. Fail-safe — exceptions logged
    but never propagated (contest infrastructure must not block a trip)."""
    import logging
    import time as _time
    try:
        xte_m                    = scorer_b_data.get('xte_m')
        xte_source               = scorer_b_data.get('xte_source')
        heading_deviation        = scorer_b_data.get('heading_deviation')
        u_turn_applied           = scorer_b_data.get('u_turn_applied', False)
        fc_fired                 = scorer_b_data.get('feasible_change_fired', False)
        fc_xte_m                 = scorer_b_data.get('feasible_change_xte_m')
        fc_polyline              = scorer_b_data.get('feasible_change_polyline')
        fc_latency_ms            = scorer_b_data.get('feasible_change_latency_ms')
        median_lat               = scorer_b_data.get('median_lat')
        median_lng               = scorer_b_data.get('median_lng')
        num_lowspeed_points      = scorer_b_data.get('num_lowspeed_points', 0)
        scorer_a_status          = scorer_b_data.get('scorer_a_status', 'ok')
        scorer_b_status          = scorer_b_data.get('scorer_b_status', 'ok')
        target_lat               = scorer_b_data.get('target_lat')
        target_lng               = scorer_b_data.get('target_lng')

        cur.execute("""
            INSERT INTO app_private.contest_events (
                driver_id, offer_id, trip_state,
                cluster_centroid_lat, cluster_centroid_lng, cluster_centroid_time,
                cluster_median_lat, cluster_median_lng,
                cluster_entry_time, cluster_exit_time,
                num_points, num_lowspeed_points,
                duration_s, min_speed_mph,
                target_lat, target_lng, distance_to_target_m,
                score_pin, score_route,
                xte_meters, xte_source, heading_deviation_deg, u_turn_tolerance_applied,
                feasible_change_fired, feasible_change_xte_m,
                feasible_change_polyline, feasible_change_latency_ms,
                scorer_a_status, scorer_b_status, nail_fired_by
            ) VALUES (
                %s, %s, %s,
                %s, %s, NOW(),
                %s, %s,
                NOW() - make_interval(secs => %s), NOW(),
                %s, %s,
                %s, %s,
                %s, %s, %s,
                %s, %s,
                %s, %s, %s, %s,
                %s, %s,
                %s, %s,
                %s, %s, %s
            )
        """, (
            driver_id, state_row.get('current_offer_id'), state,
            current_lat, current_lng,
            median_lat, median_lng,
            float(stopped_seconds or 0),
            1, num_lowspeed_points,  # num_points placeholder — Round 2 uses current point only
            float(stopped_seconds or 0), current_speed_mph,
            target_lat, target_lng, dist_m,
            score_a, score_b,
            xte_m, xte_source, heading_deviation, u_turn_applied,
            fc_fired, fc_xte_m,
            fc_polyline, fc_latency_ms,
            scorer_a_status, scorer_b_status, nail_fired_by,
        ))
        conn.commit()
    except Exception as e:
        logging.warning(f"[CONTEST] _write_contest_event failed (non-fatal): {e}")
        try:
            conn.rollback()
        except Exception:
            pass


def write_nail_contest_event(
    cur, conn,
    driver_id: str,
    state_row: dict,
    nail_path: str,
    current_lat: float,
    current_lng: float,
    current_speed_mph: float,
    current_heading: float,
    stopped_seconds: float,
    dist_to_target_m: float,
    target_lat: float,
    target_lng: float,
    pickup_lat: float,
    pickup_lng: float,
    dropoff_lat: float,
    dropoff_lng: float,
) -> None:
    """Patch 00562: Public entry point for contest instrumentation at real
    nail-fire sites. Call AFTER a successful state transition.

    Computes Scorer A + Scorer B, writes one contest_events row with nail_path.
    Fail-safe — all exceptions logged and swallowed.

    Pickup paths (nail_path ending in 'pickup' or 'pickup_s12') are scored by
    Scorer A only; Scorer B returns 'pickup_not_scorable' because the driver
    is at the start of the polyline (XTE would be rigged to ~0).

    Watchdog A paths may have insufficient cluster data; Scorer B returns
    'insufficient_cluster_data' if num_lowspeed_points < 3.
    """
    import logging
    try:
        state = state_row.get("state", "UNKNOWN")
        trip_key = _contest_trip_key(driver_id, state_row)
        _is_pickup = nail_path.startswith("gps_convergence_pickup") or nail_path == "manual_pickup"

        # Scorer A: always computable — the reified distance-to-target score
        try:
            score_a = _scorer_a_numeric_score(dist_to_target_m)
            scorer_a_status = "ok"
        except Exception as _a_err:
            logging.warning(f"[CONTEST] Scorer A failed (non-fatal): {_a_err}")
            score_a = 0.0
            scorer_a_status = "error"

        # Scorer B: gated by nail_path and cluster quality
        if _is_pickup:
            # Option C: pickup is the start of the polyline — Scorer B rigged to 0 XTE.
            # Skip the math, capture the label path only.
            scorer_b_data = {
                'median_lat': None, 'median_lng': None,
                'num_lowspeed_points': 0,
                'xte_m': None, 'xte_source': None,
                'heading_deviation': None, 'u_turn_applied': False,
                'feasible_change_fired': False,
                'feasible_change_xte_m': None,
                'feasible_change_polyline': None,
                'feasible_change_latency_ms': None,
                'target_lat': target_lat, 'target_lng': target_lng,
                'scorer_a_status': scorer_a_status,
                'scorer_b_status': 'pickup_not_scorable',
            }
            score_b = None
        elif _contest_kill_switch_check(trip_key, 'b'):
            scorer_b_data = {
                'median_lat': None, 'median_lng': None,
                'num_lowspeed_points': 0,
                'xte_m': None, 'xte_source': None,
                'heading_deviation': None, 'u_turn_applied': False,
                'feasible_change_fired': False,
                'feasible_change_xte_m': None,
                'feasible_change_polyline': None,
                'feasible_change_latency_ms': None,
                'target_lat': target_lat, 'target_lng': target_lng,
                'scorer_a_status': scorer_a_status,
                'scorer_b_status': 'trip_killed',
            }
            score_b = None
        else:
            try:
                score_b, scorer_b_data = _compute_scorer_b(
                    current_lat, current_lng, current_heading,
                    target_lat, target_lng,
                    pickup_lat, pickup_lng, dropoff_lat, dropoff_lng,
                    stopped_seconds, dist_to_target_m,
                    cur, trip_key,
                )
                # Patch 00562 gate: thin cluster → mark insufficient
                _n_lowspeed = scorer_b_data.get('num_lowspeed_points', 0)
                if _n_lowspeed < 3:
                    scorer_b_data['scorer_b_status'] = 'insufficient_cluster_data'
                    score_b = None
            except Exception as _b_err:
                logging.warning(f"[CONTEST] Scorer B failed (non-fatal): {_b_err}")
                _contest_trip_kill(trip_key, 'b', f"exception in scorer: {_b_err}")
                scorer_b_data = {
                    'median_lat': None, 'median_lng': None,
                    'num_lowspeed_points': 0,
                    'xte_m': None, 'xte_source': None,
                    'heading_deviation': None, 'u_turn_applied': False,
                    'feasible_change_fired': False,
                    'feasible_change_xte_m': None,
                    'feasible_change_polyline': None,
                    'feasible_change_latency_ms': None,
                    'target_lat': target_lat, 'target_lng': target_lng,
                    'scorer_a_status': scorer_a_status,
                    'scorer_b_status': 'scorer_error',
                }
                score_b = None

        scorer_b_data['scorer_a_status'] = scorer_a_status

        # Write the row with nail_path stamped
        _write_nail_contest_row(
            cur, conn,
            driver_id=driver_id,
            state=state,
            state_row=state_row,
            nail_path=nail_path,
            current_lat=current_lat,
            current_lng=current_lng,
            current_speed_mph=current_speed_mph,
            stopped_seconds=stopped_seconds,
            dist_m=dist_to_target_m,
            score_a=score_a,
            score_b=score_b,
            scorer_b_data=scorer_b_data,
            nail_fired_by='production',
        )
        logging.info(
            f"[CONTEST] wrote event: nail_path={nail_path} "
            f"score_a={score_a:.3f} "
            f"score_b={(f'{score_b:.3f}' if score_b is not None else scorer_b_data.get('scorer_b_status'))} "
            f"dist={dist_to_target_m:.0f}m"
        )
    except Exception as e:
        logging.warning(f"[CONTEST] write_nail_contest_event failed (non-fatal): {e}")


def _write_nail_contest_row(cur, conn, driver_id, state, state_row, nail_path,
                             current_lat, current_lng, current_speed_mph,
                             stopped_seconds, dist_m,
                             score_a, score_b, scorer_b_data, nail_fired_by):
    """Internal: INSERT one contest_events row with nail_path column.
    Mirrors _write_contest_event() but adds nail_path. Fail-safe."""
    import logging
    try:
        xte_m                    = scorer_b_data.get('xte_m')
        xte_source               = scorer_b_data.get('xte_source')
        heading_deviation        = scorer_b_data.get('heading_deviation')
        u_turn_applied           = scorer_b_data.get('u_turn_applied', False)
        fc_fired                 = scorer_b_data.get('feasible_change_fired', False)
        fc_xte_m                 = scorer_b_data.get('feasible_change_xte_m')
        fc_polyline              = scorer_b_data.get('feasible_change_polyline')
        fc_latency_ms            = scorer_b_data.get('feasible_change_latency_ms')
        median_lat               = scorer_b_data.get('median_lat')
        median_lng               = scorer_b_data.get('median_lng')
        num_lowspeed_points      = scorer_b_data.get('num_lowspeed_points', 0)
        scorer_a_status          = scorer_b_data.get('scorer_a_status', 'ok')
        scorer_b_status          = scorer_b_data.get('scorer_b_status', 'ok')
        target_lat               = scorer_b_data.get('target_lat')
        target_lng               = scorer_b_data.get('target_lng')

        cur.execute("""
            INSERT INTO app_private.contest_events (
                driver_id, offer_id, trip_state, nail_path,
                cluster_centroid_lat, cluster_centroid_lng, cluster_centroid_time,
                cluster_median_lat, cluster_median_lng,
                cluster_entry_time, cluster_exit_time,
                num_points, num_lowspeed_points,
                duration_s, min_speed_mph,
                target_lat, target_lng, distance_to_target_m,
                score_pin, score_route,
                xte_meters, xte_source, heading_deviation_deg, u_turn_tolerance_applied,
                feasible_change_fired, feasible_change_xte_m,
                feasible_change_polyline, feasible_change_latency_ms,
                scorer_a_status, scorer_b_status, nail_fired_by
            ) VALUES (
                %s, %s, %s, %s,
                %s, %s, NOW(),
                %s, %s,
                NOW() - make_interval(secs => %s), NOW(),
                %s, %s,
                %s, %s,
                %s, %s, %s,
                %s, %s,
                %s, %s, %s, %s,
                %s, %s,
                %s, %s,
                %s, %s, %s
            )
        """, (
            driver_id, state_row.get('current_offer_id'), state, nail_path,
            current_lat, current_lng,
            median_lat, median_lng,
            float(stopped_seconds or 0),
            1, num_lowspeed_points,
            float(stopped_seconds or 0), current_speed_mph,
            target_lat, target_lng, dist_m,
            score_a, score_b,
            xte_m, xte_source, heading_deviation, u_turn_applied,
            fc_fired, fc_xte_m,
            fc_polyline, fc_latency_ms,
            scorer_a_status, scorer_b_status, nail_fired_by,
        ))
        conn.commit()
    except Exception as e:
        logging.warning(f"[CONTEST] _write_nail_contest_row failed (non-fatal): {e}")
        try:
            conn.rollback()
        except Exception:
            pass


def _compute_scorer_b(current_lat, current_lng, current_heading,
                      target_lat, target_lng,
                      pickup_lat, pickup_lng, dropoff_lat, dropoff_lng,
                      stopped_seconds, dist_to_target_m,
                      cur, trip_key: tuple) -> tuple:
    """DIAGNOSE: Compute Scorer B's route-context score for the current stop.

    Returns (score_b, scorer_b_data_dict) where score_b is a float in [0,1]
    or None if Scorer B could not produce a score.

    In Round 2, no Routes API call is made — Feasible Change gates are
    evaluated but the API call itself is stubbed. Round 3 adds the live call.
    """
    import logging
    data = {
        'xte_m': None, 'xte_source': 'none',
        'heading_deviation': None, 'u_turn_applied': False,
        'feasible_change_fired': False, 'feasible_change_xte_m': None,
        'feasible_change_polyline': None, 'feasible_change_latency_ms': None,
        'median_lat': None, 'median_lng': None, 'num_lowspeed_points': 0,
        'scorer_a_status': 'ok', 'scorer_b_status': 'ok',
        'target_lat': target_lat, 'target_lng': target_lng,
    }

    if _contest_kill_switch_check(trip_key, 'scorer_b'):
        data['scorer_b_status'] = 'killed'
        return (None, data)

    try:
        # Median centroid from low-speed buffer points
        buffer = get_buffer(current_lat if False else None) if False else list(_stop_buffers.get(trip_key[0], []))
        med_lat, med_lng, n_lowspeed = median_centroid(buffer) if buffer else (None, None, 0)
        data['median_lat'] = med_lat
        data['median_lng'] = med_lng
        data['num_lowspeed_points'] = n_lowspeed

        # Use median centroid if available, otherwise fall back to current position
        score_lat = med_lat if med_lat is not None else current_lat
        score_lng = med_lng if med_lng is not None else current_lng

        # ── Attempt polyline lookup from cache (Round 3 will populate this) ──
        polyline_points = []
        offer_id = trip_key[1]
        if offer_id:
            try:
                cur.execute("""
                    SELECT encoded_polyline FROM app_private.trip_route_cache
                    WHERE driver_id = %s AND offer_id = %s
                """, (trip_key[0], offer_id))
                row = cur.fetchone()
                if row and row.get('encoded_polyline'):
                    polyline_points = decode_polyline(row['encoded_polyline'])
            except Exception as e:
                logging.warning(f"[CONTEST] polyline cache read failed: {e}")

        # ── Compute XTE ──────────────────────────────────────────────────
        if polyline_points and len(polyline_points) >= 2:
            xte_m = cross_track_error(score_lat, score_lng, polyline_points)
            data['xte_source'] = 'polyline'
        elif pickup_lat and pickup_lng and dropoff_lat and dropoff_lng:
            xte_m = great_circle_xte(score_lat, score_lng,
                                     pickup_lat, pickup_lng,
                                     dropoff_lat, dropoff_lng)
            data['xte_source'] = 'great_circle'
            data['scorer_b_status'] = 'great_circle_fallback'
        else:
            data['scorer_b_status'] = 'no_reference_path'
            return (None, data)

        data['xte_m'] = xte_m

        # ── Heading deviation for U-turn tolerance ────────────────────────
        head_dev = None
        if current_heading is not None and target_lat and target_lng:
            head_dev = heading_deviation_deg(current_heading,
                                              score_lat, score_lng,
                                              target_lat, target_lng)
        data['heading_deviation'] = head_dev

        # ── Score ────────────────────────────────────────────────────────
        score_b, u_turn = route_context_score(score_lat, score_lng, xte_m,
                                               heading_deviation=head_dev)
        data['u_turn_applied'] = u_turn

        # ── Feasible Change gates ────────────────────────────────────────
        # Round 3: fire live Routes API call when all three Gemini gates met.
        # Guard: fire ONCE per cluster (enforced by caller — this function is
        # invoked once per SET_CANDIDATE decision in check_convergence).
        if feasible_change_triggered(float(stopped_seconds or 0), xte_m, dist_to_target_m):
            try:
                # routes_api.py / Scorer B polyline fetch removed in arc-band nuke.
                # Feasible Change now always reports "failed" — the Scorer B
                # status path handles this gracefully and logs accordingly.
                # Full Scorer A/B removal is Phase 5.
                fc_result = {'status': 'disabled_post_arc_nuke', 'latency_ms': 0}
                data['feasible_change_fired'] = True
                data['feasible_change_latency_ms'] = fc_result.get('latency_ms')
                if fc_result.get('status') == 'ok' and fc_result.get('encoded_polyline'):
                    fc_polyline = fc_result['encoded_polyline']
                    data['feasible_change_polyline'] = fc_polyline
                    # Recompute XTE against the reroute polyline
                    fc_points = decode_polyline(fc_polyline)
                    if fc_points and len(fc_points) >= 2:
                        fc_xte = cross_track_error(score_lat, score_lng, fc_points)
                        data['feasible_change_xte_m'] = fc_xte
                        # If the reroute places this stop on-track, upgrade Scorer B score
                        if fc_xte is not None and fc_xte <= ROUTE_XTE_GATE_M:
                            score_b, _uturn_ignored = route_context_score(
                                score_lat, score_lng, fc_xte,
                                heading_deviation=head_dev,
                            )
                            logging.info(
                                f"[CONTEST/FEASIBLE] reroute found — "
                                f"XTE {xte_m:.0f}m → {fc_xte:.0f}m, "
                                f"score → {score_b:.3f}"
                            )
                        else:
                            logging.info(
                                f"[CONTEST/FEASIBLE] reroute didn't help — "
                                f"fc_xte={fc_xte:.0f}m still > gate"
                            )
                else:
                    data['scorer_b_status'] = 'feasible_change_failed'
                    logging.warning(
                        f"[CONTEST/FEASIBLE] API {fc_result.get('status')}, "
                        f"{fc_result.get('latency_ms')}ms"
                    )
            except Exception as fc_e:
                logging.exception(f"[CONTEST/FEASIBLE] exception: {fc_e}")
                data['scorer_b_status'] = 'feasible_change_failed'

        return (score_b, data)

    except Exception as e:
        logging.exception(f"[CONTEST] Scorer B exception: {e}")
        _contest_trip_kill(trip_key, 'scorer_b', str(e))
        data['scorer_b_status'] = 'error'
        return (None, data)


# ════════════════════════════════════════════════════════════════════════════
# End of Contest Mode module-level state
# ═══════════════════════════════════════════════════════════════════════════


def check_convergence(driver_id, current_lat, current_lng,
                      current_speed_mph, state_row, cur,
                      stopped_seconds=0, candidate_lat=None, candidate_lng=None,
                      cumulative_miles=None):
    import logging

    state = state_row.get('state')

    if state == 'ENROUTE':
        target_lat    = state_row.get('pickup_lat')
        target_lng    = state_row.get('pickup_lng')
        current_error = state_row.get('nailed_pickup_error_m')
    elif state in ('IN_TRIP', 'REFINE_DROPOFF', 'STACKED'):
        target_lat    = state_row.get('dropoff_lat')
        target_lng    = state_row.get('dropoff_lng')
        current_error = state_row.get('nailed_dropoff_error_m')
    else:
        return ('HOLD', state, None)

    # ════════════════════════════════════════════════════════════════════════
    # Bead-on-wire target refinement (DIAGNOSE)
    # ════════════════════════════════════════════════════════════════════════
    # Runs BEFORE the distance calculation. If the Blind Man produces a
    # higher-confidence target (via OSM intersection, Google intersection
    # fallback, or a Blind-Man single_road/POI fire), we override the
    # target_lat/target_lng coming from state_row.
    #
    # When compute_target() returns None (HOLD), we keep the existing
    # Tier-0 / Tier-1 target that state_row gave us. No regression.
    try:
        from bead_on_wire import compute_target as _bead_compute_target

        _bead_offer_id = state_row.get('current_offer_id')
        if _bead_offer_id:
            # Look up: (a) address text + expected miles from offer_history
            #          (b) anchor time from driver_trip_state_log
            if state == 'ENROUTE':
                _bead_addr_col, _bead_miles_col = 'pickup_address', 'pickup_miles'
                _bead_anchor_to_state = 'ENROUTE'
                _bead_anchor_trigger = 'offer_accepted'
            else:
                _bead_addr_col, _bead_miles_col = 'dropoff_address', 'trip_miles'
                _bead_anchor_to_state = 'IN_TRIP'
                _bead_anchor_trigger = None  # accept any transition INTO IN_TRIP

            # current_offer_id in driver_trip_state is the decision_log.id
            # stored as text. offer_history.decision_log_id is the matching
            # FK. Direct lookup — no timestamp proximity, no ordering.
            cur.execute(f"""
                SELECT oh.id AS oh_id,
                       oh.{_bead_addr_col} AS addr,
                       oh.{_bead_miles_col} AS miles
                FROM app_private.offer_history oh
                WHERE oh.decision_log_id = %s::integer
                LIMIT 1
            """, (_bead_offer_id,))
            _bead_row = cur.fetchone()
            if _bead_row:
                logging.info(
                    f"[BEAD] bound offer_id={_bead_offer_id} "
                    f"oh_id={_bead_row.get('oh_id')} "
                    f"addr={str(_bead_row.get('addr'))[:40]!r} "
                    f"miles={_bead_row.get('miles')}"
                )
            if _bead_row and _bead_row.get('addr') and _bead_row.get('miles'):
                _bead_addr = _bead_row['addr']
                _bead_miles = float(_bead_row['miles'])

                # Anchor time — most recent transition INTO the relevant state
                if _bead_anchor_trigger:
                    cur.execute("""
                        SELECT logged_at FROM app_private.driver_trip_state_log
                        WHERE driver_id = %s
                          AND to_state = %s
                          AND trigger_event = %s
                        ORDER BY logged_at DESC LIMIT 1
                    """, (driver_id, _bead_anchor_to_state,
                          _bead_anchor_trigger))
                else:
                    cur.execute("""
                        SELECT logged_at FROM app_private.driver_trip_state_log
                        WHERE driver_id = %s
                          AND to_state = %s
                        ORDER BY logged_at DESC LIMIT 1
                    """, (driver_id, _bead_anchor_to_state))
                _bead_anchor_row = cur.fetchone()
                if _bead_anchor_row:
                    _bead_anchor_time = _bead_anchor_row['logged_at']
                    _bead_result = _bead_compute_target(
                        address_text=_bead_addr,
                        anchor_time=_bead_anchor_time,
                        expected_miles=_bead_miles,
                        driver_id=driver_id,
                        cur=cur,
                    )
                    if _bead_result:
                        _orig_lat, _orig_lng = target_lat, target_lng
                        target_lat = _bead_result['lat']
                        target_lng = _bead_result['lng']
                        logging.info(
                            f"[BEAD] target override state={state} "
                            f"source={_bead_result.get('source')} "
                            f"tier={_bead_result.get('tier')} "
                            f"reason={_bead_result.get('reason')} "
                            f"orig=({_orig_lat},{_orig_lng}) "
                            f"new=({target_lat},{target_lng})"
                        )

                        # ────────────────────────────────────────────────────
                        # BMOAR FIRE: if Blind Man says "we're here" (all 3
                        # gates passed), return the NAIL verdict directly.
                        # Symmetric pickup/dropoff — the only diff is which
                        # verdict tuple goes back.
                        #
                        # source="blind_man" or "blind_man_poi" → FIRE
                        # source="intersection_snap" / "google_cache" → just
                        # a geocode improvement, fall through to distance
                        # logic below.
                        # ────────────────────────────────────────────────────
                        _src = _bead_result.get('source', '')
                        if _src.startswith('blind_man'):
                            # ────────────────────────────────────────────
                            # GATE 1: Odometer floor (MANDATORY per spec §4)
                            # miles_since_leg_start >= 0.9 * expected_miles
                            # Prevents mid-trip false-fires where pivot+cluster
                            # accidentally match at a red light near the pin.
                            # ────────────────────────────────────────────
                            _leg_start = state_row.get('leg_start_cumulative_miles')
                            _leg_start_f = float(_leg_start) if _leg_start is not None else 0.0
                            _cm = float(cumulative_miles) if cumulative_miles is not None else 0.0
                            _miles_since_leg_start = _cm - _leg_start_f
                            _gate1_floor = 0.9 * float(_bead_miles)
                            if _miles_since_leg_start < _gate1_floor:
                                logging.info(
                                    f"[BEAD] gate 1 FAIL — blind_man fire blocked: "
                                    f"miles_since_leg_start={_miles_since_leg_start:.2f} "
                                    f"< floor={_gate1_floor:.2f} "
                                    f"(expected_miles={_bead_miles:.2f}, "
                                    f"leg_start={_leg_start_f:.2f}, "
                                    f"cumulative={_cm:.2f})"
                                )
                                # Do not fire via blind_man. Fall through to
                                # Path B (proximity) or Watchdog A.
                                raise StopIteration("gate_1_failed")

                            # Compute distance at the override coord for the
                            # returned dist_m (downstream audit consistency).
                            cur.execute(
                                "SELECT app_private.distance_miles(%s, %s, %s, %s) * 1609.34 AS dist_m",
                                (current_lat, current_lng, target_lat, target_lng)
                            )
                            _fire_dist_m = float(cur.fetchone()['dist_m'])

                            # Cluster median is the stationary position — use THAT
                            # as the nail coord, not current_lat/lng which is a
                            # moving snapshot. 4-tuple signals "use _extra as nail".
                            _bmoar_coords = (target_lat, target_lng)
                            if state == 'ENROUTE':
                                logging.info(
                                    f"[BEAD] ✅ INITIAL_NAIL (pickup) via Blind Man: "
                                    f"dist_m={_fire_dist_m:.0f} source={_src} "
                                    f"nail_at=({target_lat:.5f},{target_lng:.5f})"
                                )
                                _log_nail_instrument(driver_id, 'bmoar_pickup',
                                                     target_lat, target_lng,
                                                     _orig_lat, _orig_lng)
                                return ('INITIAL_NAIL', 'IN_TRIP', _fire_dist_m, _bmoar_coords)
                            elif state in ('IN_TRIP', 'REFINE_DROPOFF', 'STACKED'):
                                logging.info(
                                    f"[BEAD] ✅ DROPOFF_NAIL via Blind Man: "
                                    f"dist_m={_fire_dist_m:.0f} source={_src} "
                                    f"nail_at=({target_lat:.5f},{target_lng:.5f})"
                                )
                                _log_nail_instrument(driver_id, 'bmoar_dropoff',
                                                     target_lat, target_lng,
                                                     _orig_lat, _orig_lng)
                                return ('DROPOFF_NAIL', 'UNCOMMITTED', _fire_dist_m, _bmoar_coords)
                            # Fall through if in some other state — unexpected
                            # but don't force a fire in an ambiguous state.
    except StopIteration:
        # Gate 1 failed — blind_man fire blocked. Falls through to Path B.
        pass
    except Exception as _bead_err:
        logging.warning(f"[BEAD] compute_target integration failed: {_bead_err}")
    # ════════════════════════════════════════════════════════════════════════

    # S27 guard — NULL target coords -> HOLD unconditionally
    if target_lat is None or target_lng is None:
        logging.warning(
            f"check_convergence: S27 NULL target coords — HOLD "
            f"state={state} target=({target_lat},{target_lng}) "
            f"current=({current_lat},{current_lng})"
        )
        return ('HOLD', state, None)

    # S27b — distance calculation diagnostic
    logging.debug(
        f"check_convergence: computing dist "
        f"state={state} target=({target_lat},{target_lng}) "
        f"current=({current_lat},{current_lng})"
    )

    cur.execute("""
        SELECT app_private.distance_miles(%s, %s, %s, %s) * 1609.34 AS dist_m
    """, (current_lat, current_lng, target_lat, target_lng))
    dist_m = float(cur.fetchone()['dist_m'])

    # 1a. S11 — UNCOMMITTED + GPS at recent declined offer pickup → IN_TRIP
    # Driver overrode system decline and physically went to the pickup
    if state == 'UNCOMMITTED':
        try:
            cur.execute("""
                SELECT oh.id, oh.dropoff_lat, oh.dropoff_lng,
                       app_private.distance_miles(%s, %s, oh.pickup_lat, oh.pickup_lng) AS dist_mi
                FROM app_private.offer_history oh
                JOIN app_private.decision_log dl ON dl.id = oh.decision_log_id
                WHERE dl.driver_id = %s
                  AND oh.app_verdict = 'DECLINE'
                  AND oh.created_at > NOW() - INTERVAL '5 minutes'
                  AND oh.pickup_lat IS NOT NULL
                ORDER BY oh.created_at DESC
                LIMIT 5
            """, (current_lat, current_lng, driver_id))
            nearby = [r for r in cur.fetchall() if r['dist_mi'] is not None and r['dist_mi'] < 0.3]
            if nearby and current_speed_mph < 15:
                best = nearby[0]
                logging.warning(
                    f"S11: UNCOMMITTED→IN_TRIP — driver at declined offer pickup "
                    f"({best['dist_mi']:.3f}mi, {current_speed_mph:.1f}mph)"
                )
                # Store dropoff coords so state machine has a target
                state_row['_s11_dropoff_lat'] = best['dropoff_lat']
                state_row['_s11_dropoff_lng'] = best['dropoff_lng']
                state_row['_s11_offer_id']    = best['id']
                return ('INITIAL_NAIL', 'IN_TRIP', dist_m)
        except Exception as s11_err:
            logging.warning(f"S11 guard failed: {s11_err}")

    # 1b. ENROUTE — BMOAR fires above; refinement + Watchdog A mercy-kill here
    if state == 'ENROUTE':
        effective_error = current_error if current_error is not None else 9999.0

        # ════════════════════════════════════════════════════════════════
        # PATH B — Pickup unified predicate (proximity-enable)
        # Fires when BMOAR (Path A) didn't fire but cluster forms within
        # 200m of target + gate 1 (odometer) passes.
        # Per UNIFIED_INTENT spec §4: fire requires
        #   (gate 1 odometer) AND cluster AND (proximity OR pivot-match)
        # Path A (BMOAR above) covers the pivot-match enable.
        # Path B (this block) covers the proximity enable.
        # Both fire at cluster median (never current position).
        # ════════════════════════════════════════════════════════════════
        try:
            from bead_on_wire import detect_cluster as _detect_cluster_b
            from bead_on_wire import get_pivot_context as _get_pivot_context_b

            _b_offer_id = state_row.get('current_offer_id')
            _b_pickup_miles = None
            if _b_offer_id:
                cur.execute(
                    "SELECT pickup_miles FROM app_private.offer_history "
                    "WHERE decision_log_id = %s::integer LIMIT 1",
                    (_b_offer_id,)
                )
                _b_row = cur.fetchone()
                if _b_row and _b_row.get('pickup_miles') is not None:
                    _b_pickup_miles = float(_b_row['pickup_miles'])

            _b_leg_start = state_row.get('leg_start_cumulative_miles')
            _b_leg_start_f = float(_b_leg_start) if _b_leg_start is not None else 0.0
            _b_cm = float(cumulative_miles) if cumulative_miles is not None else 0.0
            _b_miles_since_leg_start = _b_cm - _b_leg_start_f

            _b_gate1 = (_b_pickup_miles is not None
                        and _b_miles_since_leg_start >= 0.9 * _b_pickup_miles)
            _b_proximity = dist_m < 200.0

            if _b_gate1 and _b_proximity:
                _b_pivot = _get_pivot_context_b(driver_id, cur, anchor_time=None)
                _b_on_wire = bool(_b_pivot.get('on_wire')) if _b_pivot else False
                _b_spread = 25.0 if _b_on_wire else 70.0
                _b_cluster = _detect_cluster_b(driver_id, cur, max_spread_m=_b_spread)

                if _b_cluster:
                    _b_nail_lat = float(_b_cluster['median_lat'])
                    _b_nail_lng = float(_b_cluster['median_lng'])
                    logging.info(
                        f"[PATH_B] ✅ INITIAL_NAIL (pickup proximity) — "
                        f"dist_m={dist_m:.0f} miles_since_leg={_b_miles_since_leg_start:.2f}/"
                        f"{_b_pickup_miles:.2f} "
                        f"cluster_n={_b_cluster.get('n')} spread={_b_cluster.get('spread_m', 0):.0f}m "
                        f"on_wire={_b_on_wire} "
                        f"nail_at=({_b_nail_lat:.5f},{_b_nail_lng:.5f})"
                    )
                    _log_nail_instrument(driver_id, 'path_b_pickup',
                                         _b_nail_lat, _b_nail_lng,
                                         target_lat, target_lng)
                    return ('INITIAL_NAIL', 'IN_TRIP', dist_m, (_b_nail_lat, _b_nail_lng))
        except Exception as _path_b_err:
            logging.warning(f"[PATH_B] pickup predicate failed (non-fatal): {_path_b_err}")

        if dist_m < ARMED_RADIUS_M:
            # Watchdog A (pickup) — Last Resort (5 min timeout)
            # Fires when BMOAR's gates can't pass: UH-class address mismatch,
            # GPS jitter preventing cluster formation, legitimate long wait.
            # Symmetric with dropoff Watchdog A below.
            if (current_speed_mph < WATCHDOG_A_SPEED_MPH and
                    stopped_seconds >= WATCHDOG_A_DURATION_S):
                logging.warning(
                    f"check_convergence: INITIAL_NAIL (Watchdog A LAST RESORT) — "
                    f"{dist_m:.0f}m stopped {stopped_seconds}s at {current_speed_mph:.1f}mph"
                )
                _log_nail_instrument(driver_id, 'watchdog_a_pickup',
                                     current_lat, current_lng,
                                     target_lat, target_lng)
                return ('INITIAL_NAIL', 'IN_TRIP', dist_m)

            # Armed zone — aggressive continuous refinement, no speed gate
            if dist_m < (effective_error - REFINEMENT_MIN_GAIN_M):
                logging.info(
                    f"check_convergence: REFINE_PICKUP (armed) — {dist_m:.0f}m "
                    f"(was {effective_error:.0f}m) at {current_speed_mph:.1f}mph"
                )
                return ('REFINE_PICKUP', 'ENROUTE', dist_m)
            return ('HOLD', 'ENROUTE', dist_m)

        else:
            # Outside armed zone — refine only if slow and improving
            if (current_speed_mph < REFINE_SPEED_MPH and
                    dist_m < (effective_error - REFINEMENT_MIN_GAIN_M)):
                logging.info(
                    f"check_convergence: REFINE pickup — {dist_m:.0f}m "
                    f"(was {effective_error:.0f}m) at {current_speed_mph:.1f}mph"
                )
                return ('REFINE', 'ENROUTE', dist_m)

    # 3. DROPOFF_NAIL / REFINE — IN_TRIP only, at low speed
    if state in ('IN_TRIP', 'REFINE_DROPOFF', 'STACKED'):
        effective_error = current_error if current_error is not None else 9999.0

        # ── NEW: Dual-Watchdog + Elastic Net for Dropoff ─────────────────────
        dropoff_address = state_row.get('dropoff_address') or ""
        armed_radius = get_armed_radius(dropoff_address)

        # ── Round-trip gate ──────────────────────────────────────────────
        # If pickup and dropoff address are identical, this is an errand/round trip.
        # Hold REFINE_DROPOFF until driver has driven at least 1.0 mile — prevents
        # Watchdog B firing the moment the driver departs the pickup location.
        # Coordinate-based round-trip detection: pickup and dropoff coords within ~55m.
        # More reliable than address strings (which can be NULL, mis-normalized, or
        # have capitalization / whitespace / abbreviation variants).
        _p_lat = state_row.get('pickup_lat')
        _p_lng = state_row.get('pickup_lng')
        _d_lat = state_row.get('dropoff_lat')
        _d_lng = state_row.get('dropoff_lng')
        _is_round_trip = (
            _p_lat is not None and _p_lng is not None
            and _d_lat is not None and _d_lng is not None
            and abs(float(_p_lat) - float(_d_lat)) < 0.0005
            and abs(float(_p_lng) - float(_d_lng)) < 0.0005
        )
        if _is_round_trip and state in ('IN_TRIP', 'REFINE_DROPOFF'):
            _miles_driven = float(cumulative_miles or 0)
            if _miles_driven < 1.0:
                logging.info(
                    f"[ROUND_TRIP] Gate active — {_miles_driven:.2f}mi of 1.0mi floor"
                )
                return ('HOLD', state, None)

        # Continuous pickup refinement (unchanged)
        nailed_pickup_error_m = state_row.get('nailed_pickup_error_m') if state_row else None
        nailed_pickup_lat = state_row.get('nailed_pickup_lat') if state_row else None
        nailed_pickup_lng = state_row.get('nailed_pickup_lng') if state_row else None
        if nailed_pickup_lat and nailed_pickup_lng and nailed_pickup_error_m is not None:
            cur.execute(
                "SELECT app_private.distance_miles(%s, %s, %s, %s) * 1609.34 AS dist_m",
                (current_lat, current_lng, float(nailed_pickup_lat), float(nailed_pickup_lng))
            )
            dist_to_pickup_m = float(cur.fetchone()['dist_m'])
            if (dist_to_pickup_m < 100 and
                    dist_to_pickup_m < (nailed_pickup_error_m - REFINEMENT_MIN_GAIN_M)):
                logging.info(
                    f"check_convergence: REFINE_PICKUP — "
                    f"{dist_to_pickup_m:.0f}m (was {nailed_pickup_error_m:.0f}m)"
                )
                return ('REFINE_PICKUP', 'IN_TRIP', dist_to_pickup_m)

        # ── Watchdog B departure detection — HIGHEST PRIORITY ────────────
        # Must run before armed-radius block to prevent REFINE_DROPOFF early return
        # from hijacking departure detection when candidate exists.
        if candidate_lat is not None and candidate_lng is not None:
            cur.execute(
                "SELECT app_private.distance_miles(%s,%s,%s,%s) * 1609.34 AS dist_m",
                (current_lat, current_lng, candidate_lat, candidate_lng)
            )
            dist_from_candidate_m = float(cur.fetchone()['dist_m'])
            if (dist_from_candidate_m > DEPARTURE_DISTANCE_M and
                    current_speed_mph > DEPARTURE_SPEED_MPH):
                logging.info(
                    f"check_convergence: DROPOFF_NAIL_B (Watchdog B departure) — "
                    f"retroactive nail at candidate, {dist_from_candidate_m:.0f}m departed"
                )
                return ('DROPOFF_NAIL_B', 'UNCOMMITTED', dist_from_candidate_m,
                        (candidate_lat, candidate_lng))

        # ════════════════════════════════════════════════════════════════
        # PATH B — Dropoff unified predicate (proximity-enable)
        # Symmetric to pickup Path B above. Fires when BMOAR didn't but
        # proximity + cluster + gate 1 all pass.
        # ════════════════════════════════════════════════════════════════
        try:
            from bead_on_wire import detect_cluster as _detect_cluster_d
            from bead_on_wire import get_pivot_context as _get_pivot_context_d

            _d_offer_id = state_row.get('current_offer_id')
            _d_trip_miles = None
            if _d_offer_id:
                cur.execute(
                    "SELECT trip_miles FROM app_private.offer_history "
                    "WHERE decision_log_id = %s::integer LIMIT 1",
                    (_d_offer_id,)
                )
                _d_row = cur.fetchone()
                if _d_row and _d_row.get('trip_miles') is not None:
                    _d_trip_miles = float(_d_row['trip_miles'])

            _d_leg_start = state_row.get('leg_start_cumulative_miles')
            _d_leg_start_f = float(_d_leg_start) if _d_leg_start is not None else 0.0
            _d_cm = float(cumulative_miles) if cumulative_miles is not None else 0.0
            _d_miles_since_leg_start = _d_cm - _d_leg_start_f

            _d_gate1 = (_d_trip_miles is not None
                        and _d_miles_since_leg_start >= 0.9 * _d_trip_miles)
            _d_proximity = dist_m < 200.0

            if _d_gate1 and _d_proximity:
                _d_pivot = _get_pivot_context_d(driver_id, cur, anchor_time=None)
                _d_on_wire = bool(_d_pivot.get('on_wire')) if _d_pivot else False
                _d_spread = 25.0 if _d_on_wire else 70.0
                _d_cluster = _detect_cluster_d(driver_id, cur, max_spread_m=_d_spread)

                if _d_cluster:
                    _d_nail_lat = float(_d_cluster['median_lat'])
                    _d_nail_lng = float(_d_cluster['median_lng'])
                    logging.info(
                        f"[PATH_B] ✅ DROPOFF_NAIL (dropoff proximity) — "
                        f"dist_m={dist_m:.0f} miles_since_leg={_d_miles_since_leg_start:.2f}/"
                        f"{_d_trip_miles:.2f} "
                        f"cluster_n={_d_cluster.get('n')} spread={_d_cluster.get('spread_m', 0):.0f}m "
                        f"on_wire={_d_on_wire} "
                        f"nail_at=({_d_nail_lat:.5f},{_d_nail_lng:.5f})"
                    )
                    _log_nail_instrument(driver_id, 'path_b_dropoff',
                                         _d_nail_lat, _d_nail_lng,
                                         target_lat, target_lng)
                    return ('DROPOFF_NAIL', 'UNCOMMITTED', dist_m, (_d_nail_lat, _d_nail_lng))
        except Exception as _path_b_err:
            logging.warning(f"[PATH_B] dropoff predicate failed (non-fatal): {_path_b_err}")

        # ── Dual-Watchdog Logic (NEW) ───────────────────────────────────────
        if dist_m < armed_radius:
            # Legacy dropoff inner-confirm removed — BMOAR owns dropoff fires.
            # Watchdog A (below) + Watchdog B departure are the mercy-kills.

            # Watchdog A — Last Resort (5 min timeout, only if no candidate exists)
            # If candidate exists, trust Watchdog B departure detection instead
            if (current_speed_mph < WATCHDOG_A_SPEED_MPH and
                    stopped_seconds >= WATCHDOG_A_DURATION_S):
                logging.warning(
                    f"check_convergence: DROPOFF_NAIL (Watchdog A LAST RESORT) — "
                    f"{dist_m:.0f}m stopped {stopped_seconds}s at {current_speed_mph:.1f}mph "
                    f"candidate={'yes' if candidate_lat else 'no'}"
                )
                _log_nail_instrument(driver_id, 'watchdog_a_dropoff',
                                     current_lat, current_lng,
                                     target_lat, target_lng)
                return ('DROPOFF_NAIL', 'UNCOMMITTED', dist_m)

            # Watchdog B — record micro-stop candidate
            # Proximity gate: must be within 500m of geocoded pin to qualify
            # Prevents traffic light stops from poisoning the candidate
            if (WATCHDOG_B_MIN_S <= stopped_seconds <= WATCHDOG_B_MAX_S and
                    current_speed_mph < WATCHDOG_B_SPEED_MPH):
                if dist_m <= 500.0:
                    # ── High Score promotion logging ──────────────────────────
                    if candidate_lat is not None and candidate_lng is not None:
                        cur.execute(
                            "SELECT app_private.distance_miles(%s,%s,%s,%s) * 1609.34 AS prev_dist_m",
                            (candidate_lat, candidate_lng,
                             state_row.get('dropoff_lat'), state_row.get('dropoff_lng'))
                        )
                        _prev = cur.fetchone()
                        _prev_dist = float(_prev['prev_dist_m']) if _prev and _prev['prev_dist_m'] else 9999.0
                        if dist_m < _prev_dist:
                            logging.info(
                                f"[HIGH_SCORE] 🏆 PROMOTED: {dist_m:.0f}m beats previous {_prev_dist:.0f}m — "
                                f"new candidate {current_lat:.5f},{current_lng:.5f}"
                            )
                        else:
                            logging.info(
                                f"[HIGH_SCORE] ⚪ IGNORED: {dist_m:.0f}m worse than current best {_prev_dist:.0f}m — "
                                f"keeping existing candidate"
                            )
                            return ('HOLD', state, None)
                    else:
                        logging.info(
                            f"[HIGH_SCORE] 🎯 FIRST CANDIDATE: {dist_m:.0f}m from pin — "
                            f"{current_lat:.5f},{current_lng:.5f} stopped {stopped_seconds}s"
                        )

                    # ── CONTEST MODE (Revision 00557) ─────────────────────
                    # Scorer A is the existing [HIGH_SCORE] distance-based ranker.
                    # Scorer B is route-context (XTE + U-turn tolerance).
                    # Both scores written to contest_events. If Scorer B beats
                    # Scorer A by CONTEST_SCORER_B_MARGIN AND its chosen centroid
                    # lies within the armed_radius (Guard 2), Scorer B's centroid
                    # overrides the SET_CANDIDATE tuple below.
                    _contest_trip_key_v = _contest_trip_key(driver_id, state_row)
                    _contest_candidate_lat = current_lat
                    _contest_candidate_lng = current_lng
                    _contest_nail_fired_by = None
                    try:
                        _score_a = _scorer_a_numeric_score(dist_m)
                        _score_b, _sb_data = _compute_scorer_b(
                            current_lat, current_lng, None,  # heading: not plumbed in Round 2
                            state_row.get('dropoff_lat'), state_row.get('dropoff_lng'),
                            state_row.get('pickup_lat'), state_row.get('pickup_lng'),
                            state_row.get('dropoff_lat'), state_row.get('dropoff_lng'),
                            stopped_seconds, dist_m,
                            cur, _contest_trip_key_v,
                        )

                        # Scorer B override decision
                        if (_score_b is not None
                                and _score_a is not None
                                and _score_b > _score_a + CONTEST_SCORER_B_MARGIN
                                and not _contest_nail_already_fired(_contest_trip_key_v, 'scorer_b')):
                            _med_lat = _sb_data.get('median_lat')
                            _med_lng = _sb_data.get('median_lng')
                            if _med_lat is not None and _med_lng is not None:
                                # Guard 2: centroid must be within armed_radius of target
                                cur.execute("""
                                    SELECT app_private.distance_miles(%s,%s,%s,%s) * 1609.34 AS d
                                """, (_med_lat, _med_lng,
                                      state_row.get('dropoff_lat'), state_row.get('dropoff_lng')))
                                _override_dist = float(cur.fetchone()['d'])
                                if _override_dist <= armed_radius * CONTEST_OVERRIDE_RADIUS_MULTIPLIER:
                                    _contest_candidate_lat = _med_lat
                                    _contest_candidate_lng = _med_lng
                                    _contest_nail_fired_by = 'scorer_b'
                                    _contest_mark_nail_fired(_contest_trip_key_v, 'scorer_b')
                                    logging.warning(
                                        f"[CONTEST] 🏁 Scorer B OVERRIDE: score_b={_score_b:.3f} > "
                                        f"score_a={_score_a:.3f}+margin, centroid "
                                        f"({_med_lat:.5f},{_med_lng:.5f}) {_override_dist:.0f}m from dropoff"
                                    )
                                else:
                                    logging.info(
                                        f"[CONTEST] Scorer B won score but centroid "
                                        f"{_override_dist:.0f}m > armed {armed_radius:.0f}m (Guard 2)"
                                    )
                        if _contest_nail_fired_by is None:
                            # Scorer A won (or B had no usable centroid or was killed)
                            _contest_nail_fired_by = 'scorer_a'
                            _contest_mark_nail_fired(_contest_trip_key_v, 'scorer_a')

                        _write_contest_event(
                            cur, cur.connection, driver_id, state, state_row,
                            current_lat, current_lng, current_speed_mph,
                            stopped_seconds, dist_m,
                            _score_a, _score_b, _sb_data, _contest_nail_fired_by,
                        )
                    except Exception as _ce:
                        logging.exception(f"[CONTEST] contest block failed (non-fatal): {_ce}")
                        _contest_trip_kill(_contest_trip_key_v, 'scorer_a', str(_ce))
                        # Fall through — candidate stays as current position (existing behavior)
                    # ── END CONTEST MODE ──────────────────────────────────
                    # Phase 0 shadow: confirm buffer health at candidate record time
                    _buf = get_buffer(driver_id)
                    if _buf:
                        _last = _buf[-1]
                        logging.info(f"[BUFFER] {len(_buf)} pts | last_speed={_last['speed_mph']:.1f}mph")
                    return ('SET_CANDIDATE', 'IN_TRIP', dist_m, (_contest_candidate_lat, _contest_candidate_lng))
                else:
                    logging.info(
                        f"[HIGH_SCORE] ❌ REJECTED: stop at {dist_m:.0f}m exceeds 500m proximity gate"
                    )

            # Existing armed-zone refinement (kept for compatibility)
            # STACKED excluded — dropoff_lat points to secondary ride, refinement is nonsensical
            if state != 'STACKED' and dist_m < (effective_error - REFINEMENT_MIN_GAIN_M):
                logging.info(
                    f"check_convergence: REFINE_DROPOFF (armed) — {dist_m:.0f}m "
                    f"(was {effective_error:.0f}m) at {current_speed_mph:.1f}mph"
                )
                return ('REFINE_DROPOFF', 'IN_TRIP', dist_m)

        else:
            if state != 'STACKED' and dist_m < (effective_error - REFINEMENT_MIN_GAIN_M):
                return ('REFINE', 'IN_TRIP', dist_m)

    return ('HOLD', state, None)
