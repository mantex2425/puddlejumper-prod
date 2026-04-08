import logging
from arc_band import correct_dropoff



# ======================================================================
# PIPELINE STAGE 2 — run_decision_engine
# ======================================================================
def run_decision_engine(cur, conn, uid, params):
    """
    Arc band correction + geocode hallucination guard + SQL decision engine.
    Returns (basic_result dict, arc_band_trace dict, effective_params dict).
    effective_params is params with corrected/nulled coordinates applied.
    Raises ValueError on SQL engine failure — orchestrator returns 500.
    """
    import time
    from math import radians, cos, sin, asin, sqrt

    ep             = dict(params)   # effective params — mutable coordinate copy
    arc_band_trace = {}

    # ── Arc band correction ───────────────────────────────────────────
    _t_arc = time.time()
    try:
        cur.execute("""
            SELECT jsonb_array_elements_text(settings->'redZones') AS hex
            FROM app_private.driver_settings_new WHERE driver_id = %s
        """, (uid,))
        red_zone_set = {row["hex"] for row in cur.fetchall()}

        green_zone_set = set()
        if ep["market_id"]:
            cur.execute("""
                SELECT jsonb_array_elements_text(elem->'greenZones')
                FROM app_private.driver_settings_new,
                     jsonb_array_elements(settings->'markets') elem
                WHERE driver_id = %s AND elem->>'id' = %s
            """, (uid, ep["market_id"]))
            green_zone_set = {row["jsonb_array_elements_text"] for row in cur.fetchall()}

        correction = correct_dropoff(
            pickup_lat=ep["p_lat"],   pickup_lng=ep["p_lng"],
            dropoff_lat=ep["d_lat"],  dropoff_lng=ep["d_lng"],
            trip_miles=ep["trip_miles"],
            dropoff_address=ep["dropoff_address"],
            red_zone_set=red_zone_set, green_zone_set=green_zone_set,
            is_puddle_jump=ep["is_puddle_jump"], cur=cur, conn=conn
        )

        arc_band_trace                       = correction.get("trace", {})
        arc_band_trace["arc_band_triggered"] = correction["arc_band_triggered"]
        arc_band_trace["is_red_zone_risk"]   = correction["is_red_zone_risk"]
        arc_band_trace["use_ocr_distance"]   = correction["use_ocr_distance"]
        arc_band_trace["dropoff_address"]    = ep["dropoff_address"]
        arc_band_trace["pickup_address"]     = ep["pickup_address"]

        if correction["corrected_lat"] != ep["d_lat"] or correction["corrected_lng"] != ep["d_lng"]:
            arc_band_trace["original_dropoff"] = {"lat": ep["d_lat"], "lng": ep["d_lng"]}
            ep["d_lat"] = correction["corrected_lat"]
            ep["d_lng"] = correction["corrected_lng"]
            logging.info(f"[ARC] Dropoff corrected to {ep['d_lat']}, {ep['d_lng']}")

        if correction["use_ocr_distance"]:
            logging.info(f"[ARC] Using OCR distance: {ep['trip_miles']} mi")
        if correction["is_red_zone_risk"]:
            logging.warning(f"[RED] Red zone risk: '{ep['dropoff_address']}'")

    except Exception as arc_err:
        logging.error(f"Arc band error (non-blocking): {arc_err}")
        arc_band_trace["error"] = str(arc_err)
    logging.info(f"[TIMER] Arc band: {(time.time()-_t_arc)*1000:.0f}ms")

    # ── Geocode hallucination guard ───────────────────────────────────
    def _haversine_mi(lat1, lng1, lat2, lng2):
        lat1, lng1, lat2, lng2 = map(radians, [lat1, lng1, lat2, lng2])
        dlat = lat2 - lat1
        dlng = lng2 - lng1
        a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlng/2)**2
        return 3956 * 2 * asin(sqrt(a))

    if ep["current_lat"] and ep["current_lng"] and ep["p_lat"] and ep["p_lng"]:
        geocode_dist = _haversine_mi(
            ep["current_lat"], ep["current_lng"], ep["p_lat"], ep["p_lng"]
        )
        arc_band_trace["geocode_distance_mi"] = round(geocode_dist, 2)
        if geocode_dist > 30:
            logging.warning(
                f"[HALLUCINATION] pickup ({ep['p_lat']},{ep['p_lng']}) is "
                f"{geocode_dist:.0f}mi from driver. Nulling coords, using OCR "
                f"pickup_miles={ep['pickup_miles']}"
            )
            arc_band_trace["geocode_hallucination"] = True
            arc_band_trace["geocode_distance_mi"]   = round(geocode_dist, 1)
            arc_band_trace["original_pickup_lat"]   = ep["p_lat"]
            arc_band_trace["original_pickup_lng"]   = ep["p_lng"]
            ep["p_lat"] = None
            ep["p_lng"] = None

    # ── SQL decision engine ───────────────────────────────────────────
    _t_sql = time.time()
    cur.execute("""
        SELECT * FROM app_private.decision_engine_v2(
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s, %s
        )
    """, (
        uid,
        ep["p_lat"], ep["p_lng"],
        ep["d_lat"], ep["d_lng"],
        ep["fare"], ep["trip_miles"], ep["trip_min"],
        ep["pickup_min"], ep["pickup_miles"], ep["market_id"],
        ep["towards_active"], ep["towards_target_lat"], ep["towards_target_lng"],
        ep["towards_market_id"],
        ep["current_lat"], ep["current_lng"],
        ep["towards_backtrack_tolerance"], ep["is_puddle_jump"],
    ))
    row = cur.fetchone()
    logging.info(f"[TIMER] Decision engine SQL: {(time.time()-_t_sql)*1000:.0f}ms")
    if not row:
        raise ValueError("Decision engine returned no result")

    basic_result = {
        "verdict":          row["verdict"],
        "reason":           row["reason"],
        "netPay":           float(row["net_pay"]          or 0),
        "hourlyRate":       float(row["hourly_rate"]      or 0),
        "dollarsPerMile":   float(row["dollars_per_mile"] or 0),
        "deadheadMiles":    float(row["deadhead_miles"]   or 0),
        "deadheadCost":     float(row["deadhead_cost"]    or 0),
        "arrivalDetected":  row["arrival_detected"],
        "switchToMode":     row["switch_to_mode"],
        "switchToMarketId": row["switch_to_market_id"],
        "thresholdSource":  row["threshold_source"],
    }

    # Stash raw trace for log_decision (piggybacks on ep dict)
    ep["_raw_trace"]      = row.get("trace_data") or {}
    ep["_arc_band_trace"] = arc_band_trace

    return basic_result, arc_band_trace, ep

