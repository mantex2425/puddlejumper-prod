"""
nail_manager.py — Plan module for post-nail coordinate refinement.
Patch 00566a Step 9 (Fork B refactor: mode-symmetric API).

4-Box classification: PLAN.
  - Reads offer context from offer_history (DB reads are Plan-legitimate)
  - Calls refinement_gates.should_refine_target() (Plan policy)
  - Calls triangulation.refine_target_by_geometry() (Plan geometry)
  - Returns a decision object (dict) for the caller to execute
  - Does NOT write to any DB table. Does NOT call sm_transition().

Mode-symmetric: serves both
  - mode="dropoff" — anchor=nailed_pickup, target=dropoff estimate
  - mode="pickup"  — anchor=nailed_dropoff, target=pickup estimate (future use)

Used by:
  - driver_heartbeat.py INITIAL_NAIL branch (commercial Auto Nail path, dropoff mode)
  - pickup_confirm.py (dev-path manual Nail It, dropoff mode)
  - Both call handle_post_nail_refinement(mode=...) and emit the UPDATE SQL
    themselves using the returned plan.

Design principle: Plan decides, Execute writes.
"""

import logging

from triangulation import refine_target_by_geometry, _compute_geocoded_miles
from refinement_gates import should_refine_target


# Option C source classification — determines whether to overwrite the
# target's primary coords in driver_trip_state or shadow to refined_*
# columns only. Arc-band sources represent "hallucination rescue" where
# the initial estimate was known-bad. Google/scored_snap represent a
# precision nudge. estimate_snap means no refinement happened (fell
# through to last resort) — treat as "no refinement" and return None.
_OVERWRITE_SOURCES = {"enhanced_arc_band", "arc_band"}
_SHADOW_SOURCES = {"google_live", "google_cache", "scored_snap"}
_NO_REFINEMENT_SOURCES = {"estimate_snap"}


def _load_offer_context(offer_id, cur):
    """
    Load the offer's context needed for refinement.

    Returns dict with pickup_address, dropoff_address, pickup_lat, pickup_lng,
    dropoff_lat, dropoff_lng, trip_miles — or None if the offer can't be
    found or is missing required fields.

    Canonical rule: offer_id throughout the codebase refers to decision_log.id,
    which offer_history references as decision_log_id.
    """
    try:
        cur.execute("""
            SELECT
                pickup_address,
                dropoff_address,
                pickup_lat,
                pickup_lng,
                dropoff_lat,
                dropoff_lng,
                trip_miles
            FROM app_private.offer_history
            WHERE decision_log_id = %s
            LIMIT 1
        """, (offer_id,))
        row = cur.fetchone()
        if not row:
            logging.warning(f"[REFINEMENT] offer_id={offer_id}: no offer_history row")
            return None
        return {
            "pickup_address":  row.get("pickup_address"),
            "dropoff_address": row.get("dropoff_address"),
            "pickup_lat":      float(row["pickup_lat"])   if row.get("pickup_lat")   is not None else None,
            "pickup_lng":      float(row["pickup_lng"])   if row.get("pickup_lng")   is not None else None,
            "dropoff_lat":     float(row["dropoff_lat"])  if row.get("dropoff_lat")  is not None else None,
            "dropoff_lng":     float(row["dropoff_lng"])  if row.get("dropoff_lng")  is not None else None,
            "trip_miles":      float(row["trip_miles"])   if row.get("trip_miles")   is not None else None,
        }
    except Exception as e:
        logging.warning(f"[REFINEMENT] offer_id={offer_id}: context load failed: {e}")
        return None


def handle_post_nail_refinement(
    offer_id,
    driver_id,
    nailed_anchor_lat,
    nailed_anchor_lng,
    cur,
    *,
    mode,
):
    """
    Decide whether and how to refine a target coordinate at nail-fire time.

    Mode-symmetric:
      mode="dropoff" — anchor=nailed_pickup, refines the dropoff estimate.
      mode="pickup"  — anchor=nailed_dropoff, refines the pickup estimate.

    Contract:
      - Called immediately after a successful nail transition.
      - Returns a refinement plan dict on success, or None on any skip /
        failure / non-actionable outcome. Caller always continues either way.
      - Does not raise. All exceptions swallowed with warning log.

    Args:
      offer_id:           decision_log.id for this offer.
      driver_id:          driver_id string.
      nailed_anchor_lat:  GPS-verified anchor latitude (nailed_pickup for
                          mode="dropoff", nailed_dropoff for mode="pickup").
      nailed_anchor_lng:  GPS-verified anchor longitude.
      cur:                psycopg2 cursor.
      mode:               "dropoff" or "pickup" (required, kwarg-only).

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
          "mode":                    str,    # "dropoff" or "pickup"
          "log_reason":              str,    # for caller's log line
        }
    """
    if mode not in ("dropoff", "pickup"):
        logging.warning(f"[REFINEMENT] offer_id={offer_id}: invalid mode={mode!r}")
        return None

    try:
        # Step 1: Load offer context
        context = _load_offer_context(offer_id, cur)
        if context is None:
            return None

        # Resolve target coords based on mode
        if mode == "dropoff":
            target_lat     = context["dropoff_lat"]
            target_lng     = context["dropoff_lng"]
            target_address = context["dropoff_address"]
        else:  # mode == "pickup"
            target_lat     = context["pickup_lat"]
            target_lng     = context["pickup_lng"]
            target_address = context["pickup_address"]

        if not all([target_lat, target_lng, context["trip_miles"]]):
            logging.info(
                f"[REFINEMENT] mode={mode} offer_id={offer_id}: missing target coords or trip_miles"
            )
            return None

        # Step 2: Policy gate (mode-symmetric)
        should_refine, skip_reason = should_refine_target(
            pickup_addr=context["pickup_address"],
            dropoff_addr=context["dropoff_address"],
            anchor_lat=nailed_anchor_lat,
            anchor_lng=nailed_anchor_lng,
            initial_target_estimate_lat=target_lat,
            initial_target_estimate_lng=target_lng,
            trip_miles=context["trip_miles"],
        )
        if not should_refine:
            logging.info(
                f"[REFINEMENT] mode={mode} offer_id={offer_id}: gate_skip={skip_reason}"
            )
            return None

        # Step 3: Run refinement engine
        geocoded_miles = _compute_geocoded_miles(
            nailed_anchor_lat, nailed_anchor_lng,
            target_lat, target_lng,
            cur,
        )
        result = refine_target_by_geometry(
            anchor_lat=nailed_anchor_lat,
            anchor_lng=nailed_anchor_lng,
            initial_estimate_lat=target_lat,
            initial_estimate_lng=target_lng,
            intended_miles=context["trip_miles"],
            geocoded_miles=geocoded_miles,
            cur=cur,
            mode=mode,
            street_name=target_address,
        )

        if result is None:
            logging.info(
                f"[REFINEMENT] mode={mode} offer_id={offer_id}: engine returned None"
            )
            return None

        # Step 4: Option C branching
        source = result.get("source", "unknown")
        if source in _NO_REFINEMENT_SOURCES:
            logging.info(
                f"[REFINEMENT] mode={mode} offer_id={offer_id} source={source} "
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
                f"[REFINEMENT] mode={mode} offer_id={offer_id}: unrecognized source "
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
            "mode":                   mode,
            "log_reason": (
                f"mode={mode} offer_id={offer_id} source={source} tier={result.get('tier')} "
                f"action={action} refined=({result['lat']:.5f},{result['lng']:.5f})"
            ),
        }

    except Exception as e:
        logging.warning(
            f"[REFINEMENT] mode={mode} offer_id={offer_id}: unhandled exception "
            f"(non-fatal, nail proceeds): {e}"
        )
        return None
