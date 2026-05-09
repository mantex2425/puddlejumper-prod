import datetime
import logging
import json
import traceback

from pudo_types import Offer, TargetSpec
from tad import compute_offer_expectations


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
            # Phase 2c.2 Item 3b.W: compute the 4 expected_* anchors via
            # tad.compute_offer_expectations. On any failure (missing prior
            # row, naive datetime, missing fields), bind NULL — TAD will
            # downgrade to Lost Mode for that offer at heartbeat time.
            now_utc = datetime.datetime.now(datetime.timezone.utc)
            prev_offer_obj = None
            prev_dropoff_eta = None
            prev_dropoff_dist = None
            try:
                cur.execute("""
                    SELECT
                        oh.id AS oh_id,
                        oh.created_at,
                        oh.pickup_lat, oh.pickup_lng,
                        oh.dropoff_lat, oh.dropoff_lng,
                        oh.expected_dropoff_arrival_time,
                        oh.expected_dropoff_distance
                    FROM app_private.offer_history oh
                    JOIN app_private.decision_log dl ON dl.id = oh.decision_log_id
                    WHERE dl.driver_id = %s
                      AND oh.expected_dropoff_arrival_time IS NOT NULL
                    ORDER BY oh.created_at DESC
                    LIMIT 1
                """, (uid,))
                prev_row = cur.fetchone()
                if prev_row:
                    prev_eta = prev_row["expected_dropoff_arrival_time"]
                    # Defensive tz-attach (Q5 ratification): tad.py rejects naive
                    # datetimes at module entry per Canonical Section III.
                    if prev_eta and prev_eta.tzinfo is None:
                        prev_eta = prev_eta.replace(tzinfo=datetime.timezone.utc)
                    prev_dropoff_eta = prev_eta
                    prev_dropoff_dist = (
                        float(prev_row["expected_dropoff_distance"])
                        if prev_row["expected_dropoff_distance"] is not None
                        else None
                    )
                    prev_offer_obj = Offer(
                        offer_id=str(prev_row["oh_id"]),
                        accepted_at=prev_row["created_at"],
                        pickup=TargetSpec(
                            lat=prev_row["pickup_lat"], lng=prev_row["pickup_lng"],
                            address_class="poi", named_roads=(),
                        ),
                        dropoff=TargetSpec(
                            lat=prev_row["dropoff_lat"], lng=prev_row["dropoff_lng"],
                            address_class="poi", named_roads=(),
                        ),
                    )
            except Exception as prev_err:
                logging.warning(
                    f"[3b.W] prev_offer fetch failed (treating as idle): {prev_err}"
                )
                # Clear aborted-transaction state so the subsequent INSERT works.
                # decision_log row was already committed upstream so rollback is safe.
                try:
                    conn.rollback()
                except Exception:
                    pass

            expected_pickup_eta = None
            expected_pickup_dist = None
            expected_dropoff_eta = None
            expected_dropoff_dist = None
            cumulative_miles_value = ep.get("cumulative_miles")
            if cumulative_miles_value is not None:
                try:
                    new_offer_obj = Offer(
                        offer_id=str(decision_log_id),
                        accepted_at=now_utc,
                        pickup=TargetSpec(
                            lat=ep["p_lat"], lng=ep["p_lng"],
                            address_class="poi", named_roads=(),
                        ),
                        dropoff=TargetSpec(
                            lat=ep["d_lat"], lng=ep["d_lng"],
                            address_class="poi", named_roads=(),
                        ),
                        pickup_miles=_safe_numeric(ep.get("pickup_miles")),
                        trip_miles=_safe_numeric(ep.get("trip_miles")),
                        pickup_minutes=ep.get("pickup_min"),
                        trip_minutes=ep.get("trip_min"),
                    )
                    expectations = compute_offer_expectations(
                        new_offer=new_offer_obj,
                        prev_offer=prev_offer_obj,
                        current_odometer=float(cumulative_miles_value),
                        now=now_utc,
                        prev_expected_dropoff_arrival_time=prev_dropoff_eta,
                        prev_expected_dropoff_distance=prev_dropoff_dist,
                    )
                    if expectations is not None:
                        expected_pickup_eta = expectations.expected_pickup_arrival_time
                        expected_pickup_dist = expectations.expected_pickup_distance
                        expected_dropoff_eta = expectations.expected_dropoff_arrival_time
                        expected_dropoff_dist = expectations.expected_dropoff_distance
                except Exception as exp_err:
                    logging.warning(
                        f"[3b.W] compute_offer_expectations failed (NULL anchors): {exp_err}"
                    )

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
                    expected_pickup_arrival_time, expected_pickup_distance,
                    expected_dropoff_arrival_time, expected_dropoff_distance
                ) VALUES (
                    NOW(),
                    EXTRACT(DOY  FROM NOW() AT TIME ZONE 'America/Chicago')::smallint,
                    EXTRACT(DOW  FROM NOW() AT TIME ZONE 'America/Chicago')::smallint,
                    EXTRACT(HOUR FROM NOW() AT TIME ZONE 'America/Chicago')::smallint,
                    %s,
                    %s, %s, app_private.safe_h3(%s, %s),
                    %s, %s, app_private.safe_h3(%s, %s), %s,
                    %s, %s,
                    %s, %s, app_private.safe_h3(%s, %s), %s,
                    %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s,
                    %s, %s, %s, %s,
                    %s,
                    %s,
                    %s, %s,
                    %s, %s,
                    %s, %s
                )
            """, (
                decision_log_id,
                ep["current_lat"], ep["current_lng"],
                ep["current_lat"], ep["current_lng"],
                ep["p_lat"], ep["p_lng"], ep["p_lat"], ep["p_lng"],
                ep["pickup_address"],
                ep["pickup_miles"], ep["pickup_min"],
                ep["d_lat"], ep["d_lng"], ep["d_lat"], ep["d_lng"],
                ep["dropoff_address"],
                ep["trip_miles"], ep["trip_min"],
                ep["fare"], ep["ride_type"],
                ep["is_surge"], ep["is_priority"], ep["is_reserve"],
                _safe_numeric(result.get("hourlyRate")), _safe_numeric(result.get("dollarsPerMile")),
                result["verdict"], result.get("reason"),
                ep["mode_name"], ep["market_name"],
                # Sprint A gate-layer additions:
                ep.get("cumulative_miles"),                    # leg_start_cumulative_miles_pickup
                ep.get("cumulative_miles"),                    # miles_at_offer_receipt (same source)
                ep.get("current_lat"), ep.get("current_lng"),  # lat/lng_at_offer_receipt
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

