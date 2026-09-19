import datetime
import logging
import json
import traceback

import psycopg2

from pudo_types import ODOMETER_STATUS_DEFERRED
from driver_queue import (
    LIVE_OFFER_PREDICATE_SQL,
    live_offer_predicate_params,
    _get_alive_unpicked_offer_ids,
    compute_effective_last_move,
)
from dsi import compute_dsi_v1


def _safe_numeric(val):
    """Return float(val) or None — never lets a string poison a numeric column."""
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


# ======================================================================
# PIPELINE STAGE 3 — log_decision
# ======================================================================
def log_decision(cur, conn, uid, params, ep, result):
    """
    INSERT decision_log + offer_history.
    Returns decision_log_id (int). Raises on failure — orchestrator continues.
    """
    trace_payload = json.dumps({
        **(ep["_raw_trace"]),
        "arc_band":         ep["_arc_band_trace"],
        "gps_age_sec":      ep["gps_age_sec"],
        "cumulative_miles": ep["cumulative_miles"],
        "offer_id":         ep.get("offer_id"),
        # Exactly what ML Kit read, per crop, before parsing. Stored so an OCR
        # misread is a query rather than a reconstruction (see 2026-08-24,
        # "US$10.11" recorded as $1.10 and undiagnosable the next morning).
        "raw_ocr":          ep.get("raw_ocr"),
    })

    cur.execute("""
        INSERT INTO app_private.decision_log (
            driver_id, market_id, fare, pickup_minutes, trip_minutes,
            pickup_lat, pickup_lng, dropoff_lat, dropoff_lng,
            decision_result, mode_at_decision, market_name,
            ocr_confidence, pickup_h3_index, dropoff_h3_index,
            trace_data, current_lat, current_lng,
            trip_miles, pickup_miles,
            ping_h3_index,
            towards_market_id, towards_target_lat, towards_target_lng,
            created_at
        ) VALUES (
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s, %s,
            %s, app_private.safe_h3(%s, %s), app_private.safe_h3(%s, %s),
            %s, %s, %s,
            %s, %s,
            app_private.safe_h3(%s, %s),
            %s, %s, %s, NOW()
        ) RETURNING id
    """, (
        uid, ep["market_id"], ep["fare"], ep["pickup_min"], ep["trip_min"],
        ep["p_lat"], ep["p_lng"], ep["d_lat"], ep["d_lng"],
        json.dumps(result), ep["mode_name"], ep["market_name"],
        json.dumps(ep["ocr_confidence"]) if ep["ocr_confidence"] else None,
        ep["p_lat"], ep["p_lng"],
        ep["d_lat"], ep["d_lng"],
        trace_payload, ep["current_lat"], ep["current_lng"],
        ep["trip_miles"], ep["pickup_miles"],
        ep["current_lat"], ep["current_lng"],
        ep["towards_market_id"], ep["towards_target_lat"], ep["towards_target_lng"],
    ))
    row = cur.fetchone()
    decision_log_id = row["id"] if row else None
    conn.commit()
    logging.info(
        f"[LOG] Decision logged -- verdict: {result['verdict']}, "
        f"ocr: {'yes' if ep['ocr_confidence'] else 'no'}, id: {decision_log_id}"
    )

    # ── offer_history (non-blocking) ──────────────────────────────────
    try:
        if decision_log_id:
            # TAD removed 2026-09-19. This block computed four expected_* anchors --
            # pickup and dropoff arrival times and distances -- for a gate that was
            # evaluated at heartbeat time. Heartbeats stopped on 2026-08-20 with the PUDO
            # removal, so nothing has called evaluate_tad_gate since: every decision paid
            # for a prior-offer query, lost-mode detection and the prediction, and wrote
            # six columns that nothing read back. The columns stay on offer_history,
            # written NULL, rather than buy a migration for dead space.
            expected_pickup_eta = None
            expected_pickup_dist = None
            expected_dropoff_eta = None
            expected_dropoff_dist = None

            cur.execute("""
                INSERT INTO app_private.offer_history (
                    created_at, day_of_year, day_of_week, hour_of_day,
                    decision_log_id,
                    driver_lat, driver_lng, driver_h3,
                    pickup_lat, pickup_lng, pickup_h3, pickup_address,
                    pickup_miles, pickup_minutes,
                    dropoff_lat, dropoff_lng, dropoff_h3, dropoff_address,
                    trip_miles, trip_minutes,
                    fare, ride_type, is_surge, is_priority, is_reserve,
                    effective_hourly_rate, dollars_per_mile,
                    app_verdict, app_reason, mode_at_decision, market_name,
                    leg_start_cumulative_miles_pickup,
                    miles_at_offer_receipt,
                    lat_at_offer_receipt, lng_at_offer_receipt,
                    dsi_v1,
                    expected_odometer, expected_odometer_status,
                    expected_pickup_arrival_time, expected_pickup_distance,
                    expected_dropoff_arrival_time, expected_dropoff_distance
                ) VALUES (
                    NOW(),
                    EXTRACT(DOY  FROM NOW() AT TIME ZONE 'America/Chicago')::smallint,
                    EXTRACT(DOW  FROM NOW() AT TIME ZONE 'America/Chicago')::smallint,
                    EXTRACT(HOUR FROM NOW() AT TIME ZONE 'America/Chicago')::smallint,
                    %s,
                    -- offer_history is pooled and carries no driver column, so it keeps
                    -- the ~0.5 km H3 cell and never the coordinate itself (2026-09-18).
                    -- The driver's own coordinates stay in decision_log, which IS keyed
                    -- to them. Existing rows were cleared in the same change.
                    NULL, NULL, app_private.safe_h3(%s, %s),
                    -- ...and no address either: the policy promises the pool holds
                    -- neither coordinates nor addresses, and nothing reads these
                    -- (Time Grid and market rate work off the cell). 2026-09-18.
                    NULL, NULL, app_private.safe_h3(%s, %s), NULL,
                    %s, %s,
                    NULL, NULL, app_private.safe_h3(%s, %s), NULL,
                    %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s,
                    %s, %s, %s, %s,
                    %s,
                    %s,
                    NULL, NULL,
                    %s,
                    %s, %s,
                    %s, %s,
                    %s, %s
                )
            """, (
                decision_log_id,
                ep["current_lat"], ep["current_lng"],          # driver_h3 only
                ep["p_lat"], ep["p_lng"],                      # pickup_h3 only
                ep["pickup_miles"], ep["pickup_min"],
                ep["d_lat"], ep["d_lng"],                      # dropoff_h3 only
                ep["trip_miles"], ep["trip_min"],
                ep["fare"], ep["ride_type"],
                ep["is_surge"], ep["is_priority"], ep["is_reserve"],
                _safe_numeric(result.get("hourlyRate")), _safe_numeric(result.get("dollarsPerMile")),
                result["verdict"], result.get("reason"),
                ep["mode_name"], ep["market_name"],
                # Sprint A gate-layer additions:
                ep.get("cumulative_miles"),                    # leg_start_cumulative_miles_pickup
                ep.get("cumulative_miles"),                    # miles_at_offer_receipt (same source)
                compute_dsi_v1(_safe_numeric(result.get("hourlyRate")), _safe_numeric(result.get("dollarsPerMile"))),  # dsi_v1 (observational; NULL-strict)
                expected_pickup_dist,  # expected_odometer (band center == expected_pickup_distance, or NULL)
                ODOMETER_STATUS_DEFERRED,  # no anchors since TAD went (2026-09-19)
                # Phase 2c.2 Item 3b.W: TAD expected anchors
                expected_pickup_eta, expected_pickup_dist,
                expected_dropoff_eta, expected_dropoff_dist,
            ))
            conn.commit()
            logging.info(f"[LOG] Offer history logged -- id: {decision_log_id}")
    except Exception as oh_err:
        logging.error(f"[ERROR] Offer history insert failed (non-blocking): {oh_err}\n{traceback.format_exc()}")
        try:
            conn.rollback()
        except:
            pass

    return decision_log_id


# ======================================================================
# PIPELINE STAGE 6 — patch_decision_log
# ======================================================================
def patch_decision_log(cur, conn, decision_log_id, result):
    """
    UPDATE decision_log with all enriched fields now in result.
    Called after stages 4+5 -- all fields guaranteed present.
    Never raises.
    """
    if not decision_log_id:
        return
    try:
        potential_keys = (
            "driverState",
            "confidenceTier",         "confidenceRadius",
            "odometerFloor",          "odometerCeiling",
            "triangulatedPickupLat",  "triangulatedPickupLng",
            "triangulatedDropoffLat", "triangulatedDropoffLng",
            "radarHourly",            "radarMileage",
            "radarPointCount",        "radarAvgDistM",
            "radarConfidence",        "radarLatencyMs",
            "radarVsHexDelta",
        )
        enriched = {k: result.get(k) for k in potential_keys if result.get(k) is not None}
        cur.execute("""
            UPDATE app_private.decision_log
            SET decision_result = decision_result || %s::jsonb
            WHERE id = %s
        """, (json.dumps(enriched), decision_log_id))
        conn.commit()
        logging.info(
            f"[PATCH] decision_log updated -- "
            f"id: {decision_log_id}, tier: {enriched.get('confidenceTier')}"
        )
    except Exception as patch_err:
        logging.warning(f"[WARN] patch_decision_log failed: {patch_err}")
        try:    conn.rollback()
        except: pass

