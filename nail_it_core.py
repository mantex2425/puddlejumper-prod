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

# Convergence thresholds
ARMED_RADIUS_M         = 1000   # enter linger zone — aggressive refinement begins (S29)
NAIL_CONFIRM_RADIUS_M  = 200    # hard lock inner zone — must be inside + slow (S29)
NAIL_CONFIRM_SPEED_MPH = 5.0    # speed gate for INITIAL_NAIL and DROPOFF_NAIL
STOPPED_SPEED_MPH      = 2.0    # effectively stopped threshold for final nail
ABORT_RADIUS_M         = 1200   # divergence threshold (S30)
ABORT_SPEED_MPH        = 25     # must be moving to abort
ABORT_MIN_DURATION_S   = 60     # grace period for U-turns
REFINE_SPEED_MPH       = 15     # must be slow to refine
# ── NEW: Dual-Watchdog + Elastic Net for Dropoff ─────────────────────────────
ARMED_RADIUS_NORMAL_M  = 400    # intersections, businesses
ARMED_RADIUS_VAGUE_M   = 800    # highways, vague linear addresses
WATCHDOG_A_SPEED_MPH   = 3.0    # Duration Fuse
WATCHDOG_A_DURATION_S  = 10
WATCHDOG_B_SPEED_MPH   = 5.0    # Displacement Fuse micro-stop
WATCHDOG_B_MIN_S       = 3
WATCHDOG_B_MAX_S       = 8
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
def get_armed_radius(address: str) -> int:
    """Return armed zone radius based on address type."""
    if is_vague_address(address):
        return ARMED_RADIUS_VAGUE_M
    return ARMED_RADIUS_NORMAL_M


def check_convergence(driver_id, current_lat, current_lng,
                      current_speed_mph, state_row, cur,
                      stopped_seconds=0, candidate_lat=None, candidate_lng=None):
    import logging

    state = state_row.get('state')

    if state == 'ENROUTE':
        target_lat    = state_row.get('pickup_lat')
        target_lng    = state_row.get('pickup_lng')
        current_error = state_row.get('nailed_pickup_error_m')
    elif state == 'IN_TRIP':
        target_lat    = state_row.get('dropoff_lat')
        target_lng    = state_row.get('dropoff_lng')
        current_error = state_row.get('nailed_dropoff_error_m')
    else:
        return ('HOLD', state, None)

    # S27 guard — NULL target coords -> HOLD unconditionally
    if target_lat is None or target_lng is None:
        logging.debug(f"check_convergence: target coords NULL for state={state} — HOLD")
        return ('HOLD', state, None)

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

    # 1b. ENROUTE — refinement outside catchment, INITIAL_NAIL inside
    if state == 'ENROUTE':
        effective_error = current_error if current_error is not None else 9999.0

        if dist_m < NAIL_CONFIRM_RADIUS_M:
            # Inner confirm zone — hard lock only if slow
            if current_speed_mph < NAIL_CONFIRM_SPEED_MPH:
                logging.info(
                    f"check_convergence: INITIAL_NAIL — {dist_m:.0f}m "
                    f"at {current_speed_mph:.1f}mph (confirmed)"
                )
                return ('INITIAL_NAIL', 'IN_TRIP', dist_m)
            else:
                # Passing through fast — hold, keep refining
                logging.debug(
                    f"check_convergence: HOLD (fast through confirm zone) — "
                    f"{dist_m:.0f}m at {current_speed_mph:.1f}mph"
                )
                return ('HOLD', 'ENROUTE', dist_m)

        elif dist_m < ARMED_RADIUS_M:
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

    # 2. ABORT — ENROUTE diverging at speed with grace period (S30)
    # Guard: if pickup was already nailed, driver picked up passenger and is
    # heading to dropoff — diverging at speed is normal. Never ABORT.
    _pickup_nailed = bool(state_row.get('nailed_pickup_lat')) if state_row else False
    if state == 'ENROUTE' and not _pickup_nailed and dist_m > ABORT_RADIUS_M and current_speed_mph > ABORT_SPEED_MPH:
        from datetime import datetime, timezone
        state_seconds = state_row.get('state_seconds')
        if state_seconds is not None:
            if state_seconds > ABORT_MIN_DURATION_S:
                logging.info(f"check_convergence: ABORT — {dist_m:.0f}m at {current_speed_mph:.0f}mph after {state_seconds:.0f}s")
                return ('ABORT', 'UNCOMMITTED', None)

    # 3. DROPOFF_NAIL / REFINE — IN_TRIP only, at low speed
    if state == 'IN_TRIP' and current_speed_mph < REFINE_SPEED_MPH:
        effective_error = current_error if current_error is not None else 9999.0

        # ── NEW: Dual-Watchdog + Elastic Net for Dropoff ─────────────────────
        dropoff_address = state_row.get('dropoff_address') or ""
        armed_radius = get_armed_radius(dropoff_address)

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

        # ── Dual-Watchdog Logic (NEW) ───────────────────────────────────────
        if dist_m < armed_radius:
            # Inner confirm zone — hard lock regardless of stopped_seconds (preserves S15)
            if dist_m < NAIL_CONFIRM_RADIUS_M and current_speed_mph < NAIL_CONFIRM_SPEED_MPH:
                logging.info(
                    f"check_convergence: DROPOFF_NAIL (inner confirm) — "
                    f"{dist_m:.0f}m at {current_speed_mph:.1f}mph"
                )
                return ('DROPOFF_NAIL', 'UNCOMMITTED', dist_m)

            # Watchdog A — Duration Fuse (normal drops + hot swaps)
            if (current_speed_mph < WATCHDOG_A_SPEED_MPH and
                    stopped_seconds >= WATCHDOG_A_DURATION_S):
                logging.info(
                    f"check_convergence: DROPOFF_NAIL (Watchdog A) — "
                    f"{dist_m:.0f}m stopped {stopped_seconds}s at {current_speed_mph:.1f}mph"
                )
                return ('DROPOFF_NAIL', 'UNCOMMITTED', dist_m)

            # Watchdog B — record micro-stop candidate
            if (WATCHDOG_B_MIN_S <= stopped_seconds <= WATCHDOG_B_MAX_S and
                    current_speed_mph < WATCHDOG_B_SPEED_MPH):
                logging.info(
                    f"check_convergence: SET_CANDIDATE (Watchdog B) — "
                    f"{current_lat:.5f},{current_lng:.5f} stopped {stopped_seconds}s"
                )
                return ('SET_CANDIDATE', 'IN_TRIP', dist_m, (current_lat, current_lng))

            # Existing armed-zone refinement (kept for compatibility)
            if dist_m < (effective_error - REFINEMENT_MIN_GAIN_M):
                logging.info(
                    f"check_convergence: REFINE_DROPOFF (armed) — {dist_m:.0f}m "
                    f"(was {effective_error:.0f}m) at {current_speed_mph:.1f}mph"
                )
                return ('REFINE_DROPOFF', 'IN_TRIP', dist_m)

        else:
            if dist_m < (effective_error - REFINEMENT_MIN_GAIN_M):
                return ('REFINE', 'IN_TRIP', dist_m)

        # Watchdog B — departure detection (checked regardless of armed radius)
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

    return ('HOLD', state, None)
