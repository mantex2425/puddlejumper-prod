"""
triangulation_enricher.py — Pipeline Stage 5 enricher.

Despite the filename, this module no longer triangulates. Name retained
for now to avoid cross-module rename churn — rename to decision_enricher.py
is queued as a separate commit.

Job:
  1. Validate geocoded pickup distance vs Uber-reported pickup_miles
     (delta_pct, is_validated).
  2. Assign confidence tier (high/medium/low) + radius + odometer floor.
  3. Write the result dict with triangulatedPickupLat/Lng etc.
     (field names kept for Android compat; values are now the Google-
     geocoded coords, not arc-band triangulation output).
  4. Log the shadow market signal into pickup_market_signals (feeds
     get_price_radar via community_offers later in lifecycle).

State-machine handoff removed in demolition 2026-05-04. Pickup/dropoff
coordinates now land via heartbeat-time FirePickup / FireDropoff actions
in driver_heartbeat.py._execute_action. See docs/RIDE_LIFECYCLE.md.

4-box classification: PLAN (business logic).
  - Reads ep (Plan-legitimate)
  - Writes to pickup_market_signals directly (pre-existing 4-box
    violation; FIXME flagged for post-bead refactor)

Removed in the arc-band nuke (2026-04-22):
  - triangulate_pickup / triangulate_dropoff calls (arc-band math)
  - _cache_trip_polyline_on_accept (Scorer B polyline fetch)
  - _cache_stacked_polyline_async (same, for STACKED offers)
  - refine_dropoff_background (async arc-band refinement)
  - _bearing / _project_point / _hav_miles (arc-band geometry utils)
"""

import logging
import traceback


def enrich_with_triangulation(cur, conn, uid, ep, result, decision_log_id):
    """
    Validate geocode, assign confidence tier, log shadow signal, fire state
    machine transition. Never raises. Enriches result in-place. Returns result.
    """
    try:
        if not (ep["current_lat"] and ep["current_lng"]):
            return result

        reported       = ep["pickup_miles"] or 0
        geocoded_miles = None
        delta_pct      = 1.0
        is_validated   = False
        pickup_h3      = None

        # Use Google-geocoded coords directly. Arc-band triangulation is gone;
        # bead-on-wire handles target refinement at heartbeat time in
        # nail_it_core.check_convergence.
        final_pickup_lat  = ep.get("p_lat")
        final_pickup_lng  = ep.get("p_lng")
        final_dropoff_lat = ep.get("d_lat")
        final_dropoff_lng = ep.get("d_lng")

        if final_pickup_lat and final_pickup_lng:
            # Geocoded distance driver → pickup
            cur.execute(
                "SELECT app_private.distance_miles(%s, %s, %s, %s) AS dist",
                (ep["current_lat"], ep["current_lng"], final_pickup_lat, final_pickup_lng)
            )
            geo_row = cur.fetchone()
            geocoded_miles = float(geo_row["dist"]) if (geo_row and geo_row.get("dist") is not None) else None

            if geocoded_miles is not None and reported > 0:
                raw_delta    = abs(geocoded_miles - reported) / reported
                is_validated = (
                    abs(geocoded_miles - reported) < 1.5
                    if reported < 3
                    else raw_delta < 0.60
                )
                delta_pct = raw_delta
            else:
                delta_pct    = 1.0
                is_validated = False

            cur.execute(
                "SELECT app_private.safe_h3(%s, %s)::text AS h3",
                (final_pickup_lat, final_pickup_lng)
            )
            r = cur.fetchone()
            pickup_h3 = r["h3"] if (r and r.get("h3")) else None

        # ── Confidence tier ───────────────────────────────────────────
        # Simplified from the arc-band era: no more "triangulated_h3"
        # second tier — we either have a validated geocode or we don't.
        if is_validated:
            confidence_tier   = "high"
            confidence_radius = 400
            odometer_floor    = 0.92
        elif pickup_h3:
            confidence_tier   = "medium"
            confidence_radius = 600
            odometer_floor    = 0.88
        else:
            confidence_tier   = "low"
            confidence_radius = 1000
            odometer_floor    = 0.75
        odometer_ceiling = 1.40

        # ── Enrich result (ALL fields set before any DB write) ─────────
        # Key names preserved for Android compat. Values now come from
        # Google geocode, not arc-band.
        result["confidenceTier"]         = confidence_tier
        result["confidenceRadius"]       = confidence_radius
        result["odometerFloor"]          = odometer_floor
        result["odometerCeiling"]        = odometer_ceiling
        result["triangulatedPickupLat"]  = final_pickup_lat
        result["triangulatedPickupLng"]  = final_pickup_lng
        result["triangulatedDropoffLat"] = final_dropoff_lat
        result["triangulatedDropoffLng"] = final_dropoff_lng

        # ── Shadow signal ─────────────────────────────────────────────
        # FIXME [4-box violation, tracked for post-bead refactor]:
        # This INSERT runs from PLAN layer (via router → this module),
        # not EXECUTE. Pre-existing pattern; defer until bead validated.
        cur.execute(
            "SELECT app_private.safe_h3(%s, %s)::text AS h3",
            (ep["current_lat"], ep["current_lng"])
        )
        r = cur.fetchone()
        driver_h3 = r.get("h3") if r else None

        if driver_h3:
            data_source = "geocode" if pickup_h3 else "unresolved"

            cur.execute(
                "INSERT INTO app_private.pickup_market_signals "
                "(offer_id, driver_h3, pickup_h3, reported_miles, geocoded_miles, "
                "distance_delta_pct, is_validated, hourly_rate_offered, "
                "dollars_per_mile, day_of_week, hour_of_day, is_accepted, "
                "data_source, offer_status) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, "
                "EXTRACT(DOW  FROM NOW() AT TIME ZONE 'America/Chicago')::integer, "
                "EXTRACT(HOUR FROM NOW() AT TIME ZONE 'America/Chicago')::integer, "
                "%s, %s, %s)",
                (
                    decision_log_id, driver_h3, pickup_h3,
                    reported, geocoded_miles, delta_pct, is_validated,
                    result.get("hourlyRate"), result.get("dollarsPerMile"),
                    result["verdict"] == "ACCEPT",
                    data_source,
                    "pending" if result["verdict"] == "ACCEPT" else "declined",
                )
            )
            conn.commit()
            _accepted = result["verdict"] == "ACCEPT"
            logging.info(
                f"[SHADOW] Signal logged -- validated: {is_validated}, "
                f"accepted: {_accepted}"
            )

            # State-machine handoff removed in demolition 2026-05-04.
            # Coordinates land via heartbeat-time _execute_action. The
            # offer-decision path no longer mutates driver_trip_state.

    except Exception as tri_err:
        logging.error(
            f"[ERROR] enrich_with_triangulation failed: "
            f"{type(tri_err).__name__}: {tri_err}\n"
            f"{traceback.format_exc()}"
        )
        try:    conn.rollback()
        except: pass

    return result
