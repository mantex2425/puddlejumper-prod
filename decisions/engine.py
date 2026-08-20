import logging



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

    # ── engine selection ──────────────────────────────────────────────
    # Per-driver, read from settings, so a mid-drive rollback is one UPDATE
    # and needs no redeploy:
    #   UPDATE app_private.driver_settings_new
    #      SET settings = settings - 'engine_version' WHERE driver_id = '<uid>';
    try:
        cur.execute(
            "SELECT COALESCE(settings->>'engine_version', 'v2') AS ev "
            "FROM app_private.driver_settings_new WHERE driver_id = %s",
            (uid,),
        )
        _ev_row = cur.fetchone()
        engine_version = (_ev_row["ev"] if _ev_row else None) or "v2"
    except Exception as _ev_err:
        logging.warning(f"[ENGINE] version lookup failed, using v2: {_ev_err}")
        engine_version = "v2"

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

    if engine_version == "v3":
        # v3 consolidates rates + DSI + verdict into ONE path, so it needs no
        # _apply_dsi_decision override — that override layer is precisely what
        # produced the three-way DSI split v3 exists to remove.
        #
        # SAVEPOINT: a failing v3 call would otherwise abort the transaction and
        # take the v2 fallback down with it. Better a v2 verdict than no frog.
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
                "engineVersion":    "v3",
            }
        except Exception as _v3_err:
            logging.error(f"[ENGINE] v3 FAILED — falling back to v2: {_v3_err}")
            try:
                cur.execute("ROLLBACK TO SAVEPOINT sp_engine_v3")
            except Exception:
                pass
            basic_result = None

    if basic_result is None:
        # v2 retains the old 19-arg signature (kept for diffing against history)
        _v2_args = (
            uid,
            ep["p_lat"], ep["p_lng"],
            ep["d_lat"], ep["d_lng"],
            ep["fare"], ep["trip_miles"], ep["trip_min"],
            ep["pickup_min"], ep["pickup_miles"], ep["market_id"],
            ep.get("towards_active", False), ep.get("towards_target_lat"),
            ep.get("towards_target_lng"), ep.get("towards_market_id"),
            ep["current_lat"], ep["current_lng"],
            ep.get("towards_backtrack_tolerance", 3.0), ep["is_puddle_jump"],
        )
        cur.execute("""
            SELECT * FROM app_private.decision_engine_v2(
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s
            )
        """, _v2_args)
        row = cur.fetchone()
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
            "engineVersion":    "v2",
        }

        # ── DSI verdict override (spec §6) — v2 path ONLY ──
        # When the driver has opted into Drive Score Index decisions, OVERRIDE the
        # verdict with the §6 comparison, preserving the SQL verdict as legacyVerdict.
        # Flag OFF (the default) = traditional $/hr & $/mi, unchanged. Best-effort:
        # any failure keeps the SQL verdict.
        try:
            basic_result.update(_apply_dsi_decision(cur, uid, ep, basic_result))
        except Exception as _dsi_err:
            logging.warning(f"[DSI] override skipped (SQL verdict kept): {_dsi_err}")

    logging.info(
        f"[TIMER] Decision engine SQL ({basic_result['engineVersion']}): "
        f"{(time.time()-_t_sql)*1000:.0f}ms"
    )

    # Stash raw trace for log_decision (piggybacks on ep dict)
    ep["_raw_trace"]      = row.get("trace_data") or {}
    ep["_arc_band_trace"] = arc_band_trace

    return basic_result, arc_band_trace, ep


# ======================================================================
# DSI (Drive Score Index) verdict override — spec §6
# ======================================================================
# Threshold below which the radar-less fallback declines (spec §4 interim).
DSI_FALLBACK_THRESHOLD = 22.0

# §thin-market gate (2026-06-18). The verdict trusts the local-market DSI bar
# ONLY when the TIGHT radar radius (app_private.get_price_radar, ~1km) has at
# least this many points. interpolate_area_dsi's 5-k-ring (~6km) disk is ~always
# >=3 in dense markets, so thinness must be judged on the tight count (== the
# logged radarPointCount). Thin -> bar nulled -> fall back to the absolute
# threshold rather than decline a good offer against a ~1-sample bar (06-17:
# declines 22.9<23.6 and 24.8<25.7 against radarPointCount=1 bars).
MIN_MARKET_POINTS = 3


def _radar_point_count(cur, lat, lng):
    """Tight-radius (~1km) community point count from get_price_radar — the same
    count router logs as radarPointCount. 0 on missing coords / any failure
    (fail-open to the no-market fallback; never break the decision)."""
    if lat is None or lng is None:
        return 0
    try:
        cur.execute("SELECT point_count FROM app_private.get_price_radar(%s, %s)", (lat, lng))
        row = cur.fetchone()
        if not row:
            return 0
        pc = row["point_count"] if hasattr(row, "keys") else row[0]
        return int(pc or 0)
    except Exception:
        return 0


def _gate_market_dsi(local_market_dsi, tight_point_count):
    """Return the local-market DSI bar only when the tight radar has
    >= MIN_MARKET_POINTS points; else None (caller falls back to the absolute
    threshold instead of comparing against a ~1-sample bar)."""
    if local_market_dsi is None or (tight_point_count or 0) < MIN_MARKET_POINTS:
        return None
    return local_market_dsi


# Three driver-selectable decision modes (UserPreferences -> settings.decision_mode):
DSI_MODE_TRADITIONAL   = "TRADITIONAL"        # $/hr & $/mi only (SQL verdict; DSI hidden)
DSI_MODE_OBSERVATIONAL = "DSI_OBSERVATIONAL"  # SQL verdict, but DSI numbers shown
DSI_MODE_ACTIVE        = "DSI_ACTIVE"          # DSI makes the call


def _dsi_verdict(personal_dsi, local_market_dsi, decision_mode, sql_result):
    """Pure §6 decision logic (no I/O — unit-testable).

    `decision_mode` is the driver's choice: TRADITIONAL, DSI_OBSERVATIONAL, or DSI_ACTIVE.
    ALWAYS returns DSI telemetry (personalDsi / localMarketDsi / legacyVerdict /
    decisionMode) so the client can show the numbers in OBSERVATIONAL/ACTIVE and the
    backend can measure DSI vs the SQL engine in every mode. Overrides verdict/reason ONLY
    in DSI_ACTIVE (and only when personal_dsi is computable):
        Personal DSI >= Local Market DSI -> ACCEPT, else DECLINE.
    No local-market data -> graceful fallback to the §4 interim absolute threshold.

    SWITCH-MODE COHERENCE: a DSI ACCEPT means "take THIS ride", so it CLEARS any
    switchToMode / switchToMarketId the SQL engine attached to its (now-overridden)
    decline — otherwise the client would see "accept + go switch markets". A DSI DECLINE
    leaves the SQL's reposition advice intact (decline + reposition is coherent).
    """
    out = {
        "personalDsi":    round(personal_dsi, 1) if personal_dsi is not None else None,
        "localMarketDsi": round(local_market_dsi, 1) if local_market_dsi is not None else None,
        "legacyVerdict":  sql_result.get("verdict"),
        "decisionMode":   decision_mode,
    }
    # Only DSI_ACTIVE overrides the verdict; TRADITIONAL and DSI_OBSERVATIONAL keep the SQL
    # verdict (telemetry above still flows, so the client can display it / we can measure).
    if decision_mode != DSI_MODE_ACTIVE or personal_dsi is None:
        return out

    if local_market_dsi is not None:
        accept = personal_dsi >= local_market_dsi
        out["reason"] = (f"Drive Score {personal_dsi:.1f} "
                         f"{'>=' if accept else '<'} local market {local_market_dsi:.1f}")
    else:
        accept = personal_dsi >= DSI_FALLBACK_THRESHOLD
        out["reason"] = (f"Drive Score {personal_dsi:.1f} vs threshold "
                         f"{DSI_FALLBACK_THRESHOLD:.0f} (no local market data)")
    out["verdict"] = "ACCEPT" if accept else "DECLINE"
    if accept:
        # take THIS ride -> suppress any market/mode switch tied to the SQL's decline
        out["switchToMode"] = None
        out["switchToMarketId"] = None
    return out


def _apply_dsi_decision(cur, uid, ep, basic_result):
    """Read the driver's cost_per_mile + decision_mode, compute Personal DSI (card rates +
    driver cost) and Local Market DSI (time-aware community radar at the driver's location),
    then apply _dsi_verdict. Never touches the SQL decision engine. cost_per_mile is READ
    from settings (NULL-strict downstream — never assumed); decision_mode defaults to
    TRADITIONAL when unset."""
    from dsi import compute_personal_dsi
    from area_dsi import interpolate_area_dsi
    cur.execute(
        """SELECT (settings->>'cost_per_mile')::float      AS cost_per_mile,
                  COALESCE(settings->>'decision_mode', %s)  AS decision_mode
           FROM app_private.driver_settings_new WHERE driver_id = %s""",
        (DSI_MODE_TRADITIONAL, uid),
    )
    s = cur.fetchone() or {}
    # The /decisions request does NOT carry the Uber card rates (only the community harvest
    # path does). The SQL engine's computed hourlyRate/dollarsPerMile are always present and
    # match the community surface (both ~ fare/time gross), so fall back to them when the card
    # rates are absent — otherwise Personal DSI is silently null and DSI_ACTIVE no-ops.
    ehr = ep.get("effective_hourly_rate")
    if ehr is None:
        ehr = basic_result.get("hourlyRate")
    dpm = ep.get("dollars_per_mile")
    if dpm is None:
        dpm = basic_result.get("dollarsPerMile")
    personal_dsi = compute_personal_dsi(ehr, dpm, s.get("cost_per_mile"))
    _lat, _lng = ep.get("current_lat"), ep.get("current_lng")
    # §thin-market gate (2026-06-18): trust the local-market bar only when the
    # tight radar (get_price_radar ~1km) has >= MIN_MARKET_POINTS points; the 6km
    # interpolate disk is ~always dense, so judge thinness on the tight count.
    local_market_dsi = _gate_market_dsi(
        interpolate_area_dsi(cur, _lat, _lng),
        _radar_point_count(cur, _lat, _lng),
    )
    return _dsi_verdict(personal_dsi, local_market_dsi,
                        s.get("decision_mode") or DSI_MODE_TRADITIONAL, basic_result)

