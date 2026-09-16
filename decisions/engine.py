import logging



# ======================================================================
# PIPELINE STAGE 2 — run_decision_engine
# ======================================================================
def run_decision_engine(cur, conn, uid, params):
    """
    Arc band correction + geocode hallucination guard + SQL decision engine.
    Returns (basic_result dict, arc_band_trace dict, effective_params dict).
    effective_params is params with corrected/nulled coordinates applied.
    A v3 failure returns engine_error_result() (a visible decline), not an exception.
    """
    import time
    from math import radians, cos, sin, asin, sqrt

    ep             = dict(params)   # effective params — mutable coordinate copy
    arc_band_trace = {}

    # ── Phase 1: Server-side geocoding consolidation ──────────────────
    # Android now sends null coords. Server geocodes all addresses using
    # driver GPS as location bias. Cache key = normalized address text only.
    from geo_utils import _lookup_geocode_cache, _google_geocode, _write_geocode_cache

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
    # §single_road projection (2026-06-18): a bare road name geocodes to the road
    # CENTROID (measured median 1.5km from the real pickup). Project onto the
    # named road at pickup_miles from the driver instead (precomputed roads_by_name,
    # ~ms). Decision-time sampling only (radar/DSI/distance); firing uses arrest
    # (§XVI). None -> keep geocode.
    from bead_on_wire import project_single_road_pickup as _proj_sr
    _sr = _proj_sr(cur, _pickup_addr, _bias_lat, _bias_lng,
                   ep.get("pickup_miles"), ep["p_lat"], ep["p_lng"])
    if _sr is not None:
        logging.info(f"[GEO] single_road projected onto road @ pickup_miles: "
                     f"{_sr[0]:.5f},{_sr[1]:.5f} (geocode was {ep['p_lat']},{ep['p_lng']})")
        ep["p_lat"], ep["p_lng"] = _sr
    if _same_addr:
        ep["d_lat"], ep["d_lng"] = ep["p_lat"], ep["p_lng"]
        logging.info(f"[GEO] Same-address errand — mirroring pickup to dropoff")
    else:
        ep["d_lat"], ep["d_lng"] = _server_geocode(
            _dropoff_addr, _bias_lat, _bias_lng,
            ep.get("d_lat"), ep.get("d_lng")
        )

    # Arc-band dropoff correction removed with arc_band.py. Bead-on-wire
    # handles target refinement at heartbeat time via check_convergence.
    # The arc_band_trace dict is retained for schema compatibility with
    # downstream consumers (decision_log trace_data, shadow signals).
    # It now carries only geocode-hallucination + pickup/dropoff address
    # annotations, not arc-band state.
    arc_band_trace["pickup_address"]  = ep["pickup_address"]
    arc_band_trace["dropoff_address"] = ep["dropoff_address"]

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

    # TOWARDS removed 2026-08-20: directional filtering on random offers yielded
    # a 15% accept rate (theoretical ceiling ~37% for a +/-66 deg cone against
    # uniformly distributed bearings), and pricing the position instead scored
    # every offer negative. Getting somewhere by end of shift is a shift-level
    # constraint; this engine decides per offer and has no vocabulary for it.
    _sql_args = (
        uid,
        ep["p_lat"], ep["p_lng"],
        ep["d_lat"], ep["d_lng"],
        ep["fare"], ep["trip_miles"], ep["trip_min"],
        ep["pickup_min"], ep["pickup_miles"], ep["market_id"],
        ep["current_lat"], ep["current_lng"],
        ep["is_puddle_jump"],
    )

    # ── SQL decision engine ───────────────────────────────────────────
    _t_sql = time.time()
    basic_result = None

    # decision_engine_v3 is the ONLY engine (2026-09-16). v2, its per-driver rollback
    # switch in driver settings and its silent fallback are gone: if v3 errored,
    # v2 answered by different rules (weighted index, market-rate override, no minimum
    # on-screen gross) with no sign on the device that anything was wrong. A v3 failure
    # now declines VISIBLY -- "Can't score offer" -- and logs an error.
    #
    # SAVEPOINT: a failing v3 call aborts the transaction; rolling back to it keeps the
    # connection usable so the decision (and the failure) can still be logged.
    row = None
    cur.execute("SAVEPOINT sp_engine_v3")
    try:
        cur.execute("""
            SELECT * FROM app_private.decision_engine_v3(
                %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s
            )
        """, _sql_args)
        row = cur.fetchone()
        if not row:
            raise ValueError("decision_engine_v3 returned no result")
        cur.execute("RELEASE SAVEPOINT sp_engine_v3")

        _v3_trace = row.get("trace_data") or {}
        basic_result = {
            "verdict":          row["verdict"],
            "reason":           row["reason"],
            # v3 never subtracts a return cost from the payout — the unpaid
            # leg lives in the denominator — so net == gross by construction.
            "netPay":           float(ep["fare"] or 0),
            "hourlyRate":       float(row["hourly_rate"]      or 0),
            "dollarsPerMile":   float(row["dollars_per_mile"] or 0),
            "dsi":              float(row["dsi"]) if row["dsi"] is not None else None,
            # The number the DEVICE renders. It is the exact value the
            # verdict was made on -- no client-side recomputation, no
            # second formula. See DsiConstants deletion (2026-08-19).
            "netHourlyUsd":     float(row["net_hourly_usd"]) if row["net_hourly_usd"] is not None else None,
            # None on ACCEPT. On DECLINE: "threshold" (score below bar),
            # "gate:<name>" (hard gate fired ahead of scoring), or
            # "config:<name>"/"null_strict". The device shows the reason
            # for a gate decline rather than a bare red glow.
            "declineClass":     row["decline_class"],
            "deadheadMiles":    float(row["return_miles"]     or 0),
            "deadheadCost":     0.0,   # no longer a cost; it is denominator
            "arrivalDetected":  bool(_v3_trace.get("arrivalDetected") or False),
            "switchToMode":     None,  # auto-switch removed (ruling 2026-08-18)
            "switchToMarketId": None,
            "thresholdSource":  row["threshold_source"],
            # The driver's bar and the formula it was tested with, so the device
            # can say "below your $14.00/hr bar" without a second source of truth.
            # Both come from the same engine call as the verdict (trace_data).
            "dsiThreshold":     float(_v3_trace["thresholdUsed"]) if _v3_trace.get("thresholdUsed") is not None else None,
            "dsiFormula":       _v3_trace.get("dsiFormula"),
            # What the driver SEES (2026-09-15): the offer's gross $/hr (floored),
            # the gross this trip needed to clear the bar (rounded up), whether the
            # "needs" line is worth showing (gap >= 50c), and whether a threshold
            # decline was a rate miss or the modeled return leg. Display only; the
            # verdict above is DSI.
            "grossHourlyUsd":       float(_v3_trace["grossHourlyShown"]) if _v3_trace.get("grossHourlyShown") is not None else None,
            "neededGrossHourlyUsd": float(_v3_trace["neededGrossHourly"]) if _v3_trace.get("neededGrossHourly") is not None else None,
            "needsShown":           bool(_v3_trace.get("needsShown") or False),
            "declineCause":         _v3_trace.get("declineCause"),
            "engineVersion":    "v3",
        }
    except Exception as _v3_err:
        logging.error(f"[ENGINE] v3 FAILED — declining as unscorable: {_v3_err}")
        try:
            cur.execute("ROLLBACK TO SAVEPOINT sp_engine_v3")
        except Exception:
            pass
        row = None
        basic_result = engine_error_result(ep.get("fare"))

    logging.info(
        f"[TIMER] Decision engine SQL ({basic_result['engineVersion']}): "
        f"{(time.time()-_t_sql)*1000:.0f}ms"
    )

    # Stash raw trace for log_decision (piggybacks on ep dict)
    ep["_raw_trace"]      = (row.get("trace_data") if row else None) or {}
    ep["_arc_band_trace"] = arc_band_trace

    return basic_result, arc_band_trace, ep


# ======================================================================
# v3 failure -> a visible, honest decline
# ======================================================================
ENGINE_ERROR_REASON = "Can't score offer"


def engine_error_result(fare):
    """The decision returned when decision_engine_v3 errors or returns nothing.

    DECLINE, because nothing was scored and accepting blind is worse. Classed
    'config:engine_error' so the device shows the reason ("Can't score offer")
    instead of a money figure, and carries no rates, so nothing on screen or in
    the Bar Tuner can mistake it for a real verdict. Pure, so it is testable.
    """
    return {
        "verdict":              "DECLINE",
        "reason":               ENGINE_ERROR_REASON,
        "netPay":               float(fare or 0),
        "hourlyRate":           0.0,
        "dollarsPerMile":       0.0,
        "dsi":                  None,
        "netHourlyUsd":         None,
        "declineClass":         "config:engine_error",
        "deadheadMiles":        0.0,
        "deadheadCost":         0.0,
        "arrivalDetected":      False,
        "switchToMode":         None,
        "switchToMarketId":     None,
        "thresholdSource":      "engine_error",
        "dsiThreshold":         None,
        "dsiFormula":           None,
        "grossHourlyUsd":       None,
        "neededGrossHourlyUsd": None,
        "needsShown":           False,
        "declineCause":         None,
        "engineVersion":        "v3",
    }
