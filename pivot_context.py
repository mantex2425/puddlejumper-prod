"""
pivot_context.py — shared "Blind Man's hand" tracking primitive

Looks back through a driver's heartbeat trail to determine whether they're
currently on a named road, what road they were last on, and a breadcrumb of
recent named-road segments. Pure DIAGNOSE primitive (per PuddleJumper's
4-box controller):
  - Reads from app_private.heartbeat_log + routing.houston_ways
  - No writes, no state mutation
  - Returns a structured pivot dict, or a default empty-pivot dict

Consumers:
  - bead_on_wire.compute_target() -- BMOAR Path A intersection / single-road
                                      / POI gates that need to know what
                                      road the driver is on or just left
  - where_am_i.WhereAmI() -- WAI continuous awareness primitive (Phase D)
                              uses this to compute road-topology context

Both consumers MUST go through this module. Inline pivot logic is
forbidden -- having two implementations means two definitions of "is the
driver on the right street", which is precisely the architectural smell
where_am_i() exists to eliminate.

RELOCATION HISTORY (Phase D.0, 2026-04-26):
These functions were relocated from bead_on_wire.py PRIMITIVE 5.
Changes from the original:
  1. _canonicalize_address (used by _road_names_match) was renamed to
     canonicalize_address and moved to address_utils.py per Q27 verdict;
     this module imports it back. The two call sites inside
     _road_names_match are updated for the new name.
  2. Log prefix [BEAD] -> [PIVOT] (since the function no longer lives
     in BMOAR specifically). Phase C precedent.
SQL inside get_pivot_context is BYTE-IDENTICAL to the original. The
inline call from get_pivot_context to _build_breadcrumb is unchanged
(same-module, still _build_breadcrumb).
"""

import logging

from address_utils import canonicalize_address


# ============================================================================
# _build_breadcrumb -- aggregate trail rows into named-road segments
# ============================================================================

def _build_breadcrumb(rows, min_dwell_sec: float = 10.0,
                      max_age_sec: float = 300.0,
                      max_segments: int = 4) -> list:
    """Aggregate DESC-ordered heartbeat rows into named-road segments.

    Args:
        rows: heartbeat rows ordered DESC, each with .logged_at and .road_name
        min_dwell_sec: skip segments where entered_at and exited_at differ by
                       less than this (filters out brief intersection crossings)
        max_age_sec: skip segments where exited_at is older than this from the
                     most recent heartbeat (rolling window)
        max_segments: cap return list at this many most-recent segments

    Returns:
        List of {road_name, dwell_seconds, entered_at, exited_at}, most
        recent first. Empty list if rows empty or no qualifying segments.

    Used by compute_target gate 2 to support the "re-route to other side"
    edge case: driver arrives at PUDO, passenger redirects, driver takes
    a different access road back. The CORRECT named road (matching the
    offer's single_road address) is not the most recent, but is within
    the recent breadcrumb trail.
    """
    if not rows:
        return []

    # Rows arrive DESC (most recent first). Walk through them, grouping
    # consecutive same-name rows into segments. None entries (off-wire)
    # break segments.
    segments = []  # each: {road_name, exited_at, entered_at}
    current_seg = None
    for row in rows:
        name = row.get("road_name")
        ts = row.get("logged_at")
        if name is None:
            # off-wire — close any open segment
            if current_seg is not None:
                segments.append(current_seg)
                current_seg = None
            continue
        if current_seg is None:
            # start a new segment (this is the most recent row on this road)
            current_seg = {
                "road_name": name,
                "exited_at": ts,
                "entered_at": ts,
            }
        elif current_seg["road_name"] == name:
            # extend — push entered_at earlier
            current_seg["entered_at"] = ts
        else:
            # different named road — close current, start new
            segments.append(current_seg)
            current_seg = {
                "road_name": name,
                "exited_at": ts,
                "entered_at": ts,
            }
    if current_seg is not None:
        segments.append(current_seg)

    if not segments:
        return []

    # Compute dwell + age; filter
    most_recent_ts = rows[0].get("logged_at")
    result = []
    for seg in segments:
        dwell = (seg["exited_at"] - seg["entered_at"]).total_seconds()
        age   = (most_recent_ts - seg["exited_at"]).total_seconds()
        if dwell < min_dwell_sec:
            continue
        if age > max_age_sec:
            continue
        result.append({
            "road_name": seg["road_name"],
            "dwell_seconds": dwell,
            "entered_at": seg["entered_at"],
            "exited_at": seg["exited_at"],
        })
        if len(result) >= max_segments:
            break
    return result


# ============================================================================
# get_pivot_context -- the primitive
# ============================================================================

def get_pivot_context(driver_id: str, cur,
                      anchor_time=None) -> dict:
    """Determine whether driver is on-wire, and the last named road touched.

    Implements the "Blind Man's hand" tracking. Looks back through heartbeat
    history (bounded by anchor_time to the current ride only) to find:

      - Whether the most recent heartbeat is on a named road (on_wire)
      - The name of that current road, if any
      - The name of the last named road the driver was on (could be current)
      - The time the driver pivoted off the last named road (None if still on it)

    Returns: {on_wire: bool, current_road: str|None,
              last_named_road: str|None, pivot_time: timestamp|None}

    anchor_time bounds the lookback to the current ride (e.g., the IN_TRIP
    transition). Without it, we could pick up the last named road from a
    prior ride. Required for correctness across stacked rides.
    """
    if not driver_id:
        return {"on_wire": False, "current_road": None,
                "last_named_road": None, "pivot_time": None,
                "breadcrumb": []}

    try:
        # Query: most recent N heartbeats in this ride, with each one's snap.
        # We do the snap inline as a LATERAL join so we get road_name per tick.
        anchor_clause = ""
        params = [driver_id]
        if anchor_time is not None:
            anchor_clause = "AND hb.logged_at >= %s::timestamptz"
            params.append(anchor_time)
        params_tuple = tuple(params)

        cur.execute(f"""
            WITH recent AS (
                SELECT
                    hb.logged_at,
                    hb.lat,
                    hb.lng
                FROM app_private.heartbeat_log hb
                WHERE hb.driver_id = %s
                  {anchor_clause}
                ORDER BY hb.logged_at DESC
                LIMIT 60
            ),
            snapped AS (
                SELECT
                    r.logged_at,
                    r.lat, r.lng,
                    (
                        SELECT hw.name
                        FROM routing.houston_ways hw
                        WHERE hw.name IS NOT NULL
                          AND hw.length_m < 20000
                          AND ST_DWithin(
                            hw.the_geom::geography,
                            app_private.coords_to_point(r.lat, r.lng)::geography,
                            40
                          )
                        ORDER BY hw.the_geom <-> app_private.coords_to_point(r.lat, r.lng)
                        LIMIT 1
                    ) AS road_name
                FROM recent r
            )
            SELECT logged_at, road_name FROM snapped
            ORDER BY logged_at DESC;
        """, params_tuple)
        rows = cur.fetchall()

        if not rows:
            return {"on_wire": False, "current_road": None,
                    "last_named_road": None, "pivot_time": None,
                    "breadcrumb": []}

        # Most recent heartbeat
        current = rows[0]
        on_wire = current["road_name"] is not None
        current_road = current["road_name"]

        # Build breadcrumb of recent named-road segments (for re-route cases
        # where the relevant road is not the most recent named road — e.g.,
        # driver arrives, passenger redirects to "other side", driver takes a
        # different access road back to the lot).
        #
        # rules: segments must have dwell >= 10s AND be within last 5min,
        # and we keep at most 4 most recent.
        breadcrumb = _build_breadcrumb(rows, min_dwell_sec=10.0,
                                        max_age_sec=300.0, max_segments=4)

        if on_wire:
            # Still on a named road — pivot hasn't happened
            return {
                "on_wire": True,
                "current_road": current_road,
                "last_named_road": current_road,
                "pivot_time": None,
                "breadcrumb": breadcrumb,
            }

        # Off-wire — walk backwards to find the last named road and when we left it
        last_named_road = None
        pivot_time = None
        for i, row in enumerate(rows):
            if row["road_name"] is not None:
                last_named_road = row["road_name"]
                # pivot_time = time of the LATER heartbeat (just after leaving)
                if i > 0:
                    pivot_time = rows[i - 1]["logged_at"]
                else:
                    pivot_time = row["logged_at"]
                break

        return {
            "on_wire": False,
            "current_road": None,
            "last_named_road": last_named_road,
            "pivot_time": pivot_time,
            "breadcrumb": breadcrumb,
        }
    except Exception as e:
        logging.warning(f"[PIVOT] get_pivot_context failed: {e}")
        return {"on_wire": False, "current_road": None,
                "last_named_road": None, "pivot_time": None,
                "breadcrumb": []}


# ============================================================================
# _road_names_match -- fuzzy comparison of OSM vs Uber-text road names
# ============================================================================

def _road_names_match(snapped_name: str, address_road: str) -> bool:
    """Fuzzy match between an OSM road name and a YOLO'd address road name.

    Both get lowercased and abbreviation-normalized via canonicalize_address.
    Match if either contains the other (to absorb OSM adding directional or
    county qualifiers that Uber's text doesn't include, and vice versa).
    """
    if not snapped_name or not address_road:
        return False
    s = canonicalize_address(snapped_name).strip()
    a = canonicalize_address(address_road).strip()
    if not s or not a:
        return False
    return s in a or a in s
