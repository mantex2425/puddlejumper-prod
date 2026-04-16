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

    # ── Phase 1: Server-side geocoding consolidation ──────────────────
    # Android now sends null coords. Server geocodes all addresses using
    # driver GPS as location bias. Cache key = normalized address text only.
    from triangulation import _lookup_geocode_cache, _google_geocode, _write_geocode_cache

    # Vague address keywords — addresses matching these are highway/road centroids
    # and should never be cached (single point on a 15-mile road is worse than no cache)
    _VAGUE_KEYWORDS = {
        'hwy', 'highway', 'pkwy', 'parkway', 'fwy', 'freeway',
        'beltway', 'tollway', 'loop', 'expressway', 'sw fwy',
        'west loop', 'east loop', 'north loop', 'south loop',
    }

    def _is_vague_address(addr: str) -> bool:
        """True if address is a highway/road with no intersection anchor.
        Intersections (e.g. "Allen Pkwy & Damico St") and numbered addresses
        (e.g. "7745 S Sam Houston Pkwy") are considered precise enough to cache.
        Pure highway names (e.g. "Southwest Fwy, Houston") are vague — don't cache.
        """
        if not addr:
            return True
        tokens = addr.lower().split()
        # Single or two token addresses are too vague (e.g. "Pkwy" or "W Loop")
        if len(tokens) <= 2:
            return True
        # Has a street number → precise enough to cache
        if tokens[0].isdigit():
            return False
        # Has an intersection marker → precise enough to cache
        if '&' in addr or ' and ' in addr.lower():
            return False
        # Pure highway/road name with no anchor
        return any(kw in addr.lower() for kw in _VAGUE_KEYWORDS)

    def _server_geocode(addr, bias_lat, bias_lng, fallback_lat, fallback_lng):
        if not addr:
            return fallback_lat or 0.0, fallback_lng or 0.0
        # Vague addresses (highways, loops, freeways) bypass cache entirely —
        # a single cached point on a 15-mile road is worse than a fresh geocode
        _vague = _is_vague_address(addr)
        if not _vague:
            cached = _lookup_geocode_cache(addr, cur)
            if cached:
                logging.info(f"[GEO] Cache hit: {addr!r} → {cached[0]:.5f},{cached[1]:.5f}")
                return cached[0], cached[1]
        else:
            logging.info(f"[GEO] Vague address — bypassing cache: {addr!r}")
        coords = _google_geocode(addr, bias_lat=bias_lat, bias_lng=bias_lng)
        if coords:
            if not _vague:
                _write_geocode_cache(addr, coords[0], coords[1], cur)
            logging.info(f"[GEO] Server geocode: {addr!r} → {coords[0]:.5f},{coords[1]:.5f}")
            return coords[0], coords[1]
        logging.warning(f"[GEO] Geocode failed for {addr!r} — using fallback coords")
        return fallback_lat or 0.0, fallback_lng or 0.0

    _bias_lat     = ep.get("current_lat")
    _bias_lng     = ep.get("current_lng")
    _pickup_addr  = ep.get("pickup_address")
    _dropoff_addr = ep.get("dropoff_address")
    _same_addr    = bool(
        _pickup_addr and _dropoff_addr and
        _pickup_addr.strip().lower() == _dropoff_addr.strip().lower()
    )

    ep["p_lat"], ep["p_lng"] = _server_geocode(
        _pickup_addr, _bias_lat, _bias_lng,
        ep.get("p_lat"), ep.get("p_lng")
    )
    if _same_addr:
        ep["d_lat"], ep["d_lng"] = ep["p_lat"], ep["p_lng"]
        logging.info(f"[GEO] Same-address errand — mirroring pickup to dropoff")
    else:
        ep["d_lat"], ep["d_lng"] = _server_geocode(
            _dropoff_addr, _bias_lat, _bias_lng,
            ep.get("d_lat"), ep.get("d_lng")
        )

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

