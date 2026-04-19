"""
nail_manager.py — Plan module for SB3 dropoff refinement at nail-fire time.
Patch 00566a Step 9.

4-Box classification: PLAN.
  - Reads offer context from offer_history (DB reads are Plan-legitimate)
  - Calls refinement_gates.should_refine_dropoff() (Plan policy)
  - Calls triangulation.refine_target_by_geometry() (Plan geometry)
  - Returns a decision object (dict) for the caller to execute
  - Does NOT write to any DB table. Does NOT call sm_transition().

Used by:
  - driver_heartbeat.py INITIAL_NAIL branch (commercial Auto Nail path)
  - pickup_confirm.py (dev-path manual Nail It)
  - Both call handle_post_nail_refinement() and emit the UPDATE SQL
    themselves using the returned plan.

Design principle: Plan decides, Execute writes.
"""

import logging

from triangulation import refine_target_by_geometry, _compute_geocoded_miles
from refinement_gates import should_refine_dropoff


# Option C source classification — determines whether to overwrite
# driver_trip_state.dropoff_lat/lng or shadow to refined_dropoff_lat/lng only.
# Sources where we overwrite: arc-band tiers, where the initial estimate was
# known-bad (hallucination detector fired) and the refinement supplants it.
# Sources where we shadow: Google and scored-snap, where the initial estimate
# was roughly OK and the refinement is a precision improvement only.
# Source estimate_snap means no refinement happened (fell through to last
# resort) — treat as "no refinement" and return None.
_OVERWRITE_SOURCES = {"enhanced_arc_band", "arc_band"}
_SHADOW_SOURCES = {"google_live", "google_cache", "scored_snap"}
_NO_REFINEMENT_SOURCES = {"estimate_snap"}


def _load_offer_context(offer_id, cur):
    """
    Load the offer's context needed for dropoff refinement.

    Returns dict with pickup_address, dropoff_address, dropoff_lat, dropoff_lng,
    trip_miles — or None if the offer can't be found or is missing required fields.

    Canonical rule: offer_id throughout the codebase refers to decision_log.id,
    which offer_history references as decision_log_id.
    """
    try:
        cur.execute("""
            SELECT
                pickup_address,
                dropoff_address,
                dropoff_lat,
                dropoff_lng,
                trip_miles
            FROM app_private.offer_history
            WHERE decision_log_id = %s
            LIMIT 1
        """, (offer_id,))
        row = cur.fetchone()
        if not row:
            logging.warning(f"[nail_manager] offer_id={offer_id}: no offer_history row")
            return None
        return {
            "pickup_address":  row.get("pickup_address"),
            "dropoff_address": row.get("dropoff_address"),
            "dropoff_lat":     float(row["dropoff_lat"])  if row.get("dropoff_lat")  is not None else None,
            "dropoff_lng":     float(row["dropoff_lng"])  if row.get("dropoff_lng")  is not None else None,
            "trip_miles":      float(row["trip_miles"])   if row.get("trip_miles")   is not None else None,
        }
    except Exception as e:
        logging.warning(f"[nail_manager] offer_id={offer_id}: context load failed: {e}")
        return None


def handle_post_nail_refinement(
    offer_id,
    driver_id,
    nailed_pickup_lat,
    nailed_pickup_lng,
    cur,
):
    """
    Decide whether and how to refine the dropoff coordinate at pickup-nail time.

    Contract:
      - Called immediately after a successful INITIAL_NAIL transition
        (or its manual equivalent in pickup_confirm.py).
      - Returns a refinement plan dict on success, or None on any skip /
        failure / non-actionable outcome. Caller always continues either way.
      - Does not raise. All exceptions swallowed with warning log.

    Returns:
      None — if any of: context load failed, policy gate blocked, engine
             returned no result, or source is estimate_snap (no refinement).

      dict with keys:
        {
          "refined_lat":             float,
          "refined_lng":             float,
          "refined_h3":              str,
          "refinement_source":       str,    # for DB column
          "overwrite_driver_state":  bool,   # Option C branch decision
          "log_reason":              str,    # for caller's log line
        }
    """
    try:
        # Step 1: Load offer context
        context = _load_offer_context(offer_id, cur)
        if context is None:
            return None
        if not all([context["dropoff_lat"], context["dropoff_lng"], context["trip_miles"]]):
            logging.info(
                f"[nail_manager] offer_id={offer_id}: missing required fields, skipping"
            )
            return None

        # Step 2: Policy gate
        should_refine, skip_reason = should_refine_dropoff(
            pickup_addr=context["pickup_address"],
            dropoff_addr=context["dropoff_address"],
            anchor_lat=nailed_pickup_lat,
            anchor_lng=nailed_pickup_lng,
            initial_dropoff_estimate_lat=context["dropoff_lat"],
            initial_dropoff_estimate_lng=context["dropoff_lng"],
            trip_miles=context["trip_miles"],
        )
        if not should_refine:
            logging.info(
                f"[nail_manager] offer_id={offer_id}: gate skip ({skip_reason})"
            )
            return None

        # Step 3: Run refinement
        geocoded_miles = _compute_geocoded_miles(
            nailed_pickup_lat, nailed_pickup_lng,
            context["dropoff_lat"], context["dropoff_lng"],
            cur,
        )
        result = refine_target_by_geometry(
            anchor_lat=nailed_pickup_lat,
            anchor_lng=nailed_pickup_lng,
            initial_estimate_lat=context["dropoff_lat"],
            initial_estimate_lng=context["dropoff_lng"],
            intended_miles=context["trip_miles"],
            geocoded_miles=geocoded_miles,
            cur=cur,
            mode="dropoff",
            street_name=context["dropoff_address"],
        )

        if result is None:
            logging.info(
                f"[nail_manager] offer_id={offer_id}: engine returned None"
            )
            return None

        # Step 4: Option C branching
        source = result.get("source", "unknown")
        if source in _NO_REFINEMENT_SOURCES:
            logging.info(
                f"[nail_manager] offer_id={offer_id}: source={source} "
                f"(no actionable refinement)"
            )
            return None
        elif source in _OVERWRITE_SOURCES:
            overwrite = True
            action = "overwrite"
        elif source in _SHADOW_SOURCES:
            overwrite = False
            action = "shadow"
        else:
            logging.warning(
                f"[nail_manager] offer_id={offer_id}: unrecognized source "
                f"{source!r}, treating as shadow (conservative)"
            )
            overwrite = False
            action = "shadow"

        return {
            "refined_lat":            result["lat"],
            "refined_lng":            result["lng"],
            "refined_h3":             result["h3"],
            "refinement_source":      source,
            "overwrite_driver_state": overwrite,
            "log_reason": (
                f"offer_id={offer_id} source={source} tier={result.get('tier')} "
                f"action={action} refined=({result['lat']:.5f},{result['lng']:.5f})"
            ),
        }

    except Exception as e:
        logging.warning(
            f"[nail_manager] offer_id={offer_id}: unhandled exception "
            f"(non-fatal, pickup nail proceeds): {e}"
        )
        return None
