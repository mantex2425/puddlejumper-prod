"""
backtest_houston_ways.py — Side-by-side comparison:
  street_network vs routing.houston_ways as street geometry source

Patches get_street_geometry() to use houston_ways instead of street_network,
then runs the full 100-offer triangulation backtest on both.

Usage:
  python3 backtest_houston_ways.py
"""

import psycopg2, psycopg2.extras, sys, logging, io, json
logging.basicConfig(level=logging.WARNING, format="%(message)s")
sys.path.insert(0, '/home/andrew/puddlejumper-prod')

DB_CONFIG = dict(host="10.128.0.2", dbname="puddlejumper", user="postgres")
DRIVER_ID = "UjT1hE9eBXh2q95aSZYOkzDJ8lo1"

def run_backtest(conn, cur, use_houston_ways=False):
    """Run triangulation backtest. Returns list of (id, addr, error_m, tier)."""
    import arc_band as ab
    import triangulation as tri

    # ── Monkey-patch get_street_geometry if using houston_ways ───────────
    if use_houston_ways:
        original_get_street_geometry = ab.get_street_geometry

        def patched_get_street_geometry(street_name, center_lat, center_lng,
                                        cur, conn, bbox_margin=0.02):
            import re
            from arc_band import _normalize_street_name, _fetch_from_cache, \
                                  _store_in_cache, _fetch_from_overpass

            if not street_name:
                return None

            region_h3 = None
            try:
                cur.execute("SELECT app_private.coords_to_h3(%s,%s)::text AS h3",
                           (center_lat, center_lng))
                r = cur.fetchone()
                region_h3 = r['h3'] if r else None
            except: pass

            # Cache check
            if region_h3:
                cached = _fetch_from_cache(street_name, region_h3, cur)
                if cached:
                    return cached

            normalized = _normalize_street_name(street_name)

            # ── houston_ways exact name match ─────────────────────────────
            try:
                cur.execute("""
                    SELECT ST_AsGeoJSON(the_geom) AS geojson
                    FROM routing.houston_ways
                    WHERE lower(name) = lower(%s)
                      AND ST_DWithin(the_geom::geography,
                                    app_private.coords_to_geography(%s, %s),
                                    10000)
                      AND the_geom IS NOT NULL
                    LIMIT 100
                """, (normalized, center_lat, center_lng))
                rows = cur.fetchall()
                if rows:
                    segments = []
                    for row in rows:
                        gj = row['geojson'] if isinstance(row, dict) else row[0]
                        coords = json.loads(gj)['coordinates']
                        segments.append([[c[1], c[0]] for c in coords])
                    logging.info(f"🏙️ houston_ways hit for '{street_name}' — {len(segments)} segments")
                    if region_h3:
                        _store_in_cache(street_name, region_h3, segments, [], cur, conn)
                    return segments
            except Exception as e:
                logging.warning(f"houston_ways exact lookup failed: {e}")

            # ── houston_ways proximity fallback ───────────────────────────
            try:
                cur.execute("""
                    SELECT ST_AsGeoJSON(the_geom) AS geojson
                    FROM routing.houston_ways
                    WHERE ST_DWithin(the_geom::geography,
                                    app_private.coords_to_geography(%s, %s),
                                    2000)
                      AND the_geom IS NOT NULL
                    ORDER BY the_geom::geography <-> app_private.coords_to_geography(%s, %s)
                    LIMIT 50
                """, (center_lat, center_lng, center_lat, center_lng))
                rows = cur.fetchall()
                if rows:
                    segments = []
                    for row in rows:
                        gj = row['geojson'] if isinstance(row, dict) else row[0]
                        coords = json.loads(gj)['coordinates']
                        segments.append([[c[1], c[0]] for c in coords])
                    logging.info(f"📍 houston_ways proximity hit — {len(segments)} segments")
                    if region_h3:
                        _store_in_cache(street_name, region_h3, segments, [], cur, conn)
                    return segments
            except Exception as e:
                logging.warning(f"houston_ways proximity lookup failed: {e}")

            # Overpass fallback
            return _fetch_from_overpass(street_name, center_lat, center_lng,
                                        bbox_margin=bbox_margin)

        ab.get_street_geometry = patched_get_street_geometry

    # ── Fetch offers ──────────────────────────────────────────────────────
    cur.execute("""
        SELECT
            dl.id,
            dl.current_lat, dl.current_lng,
            dl.pickup_lat, dl.pickup_lng,
            dl.pickup_miles,
            dl.trace_data->'arc_band'->>'pickup_address' AS pickup_address
        FROM app_private.decision_log dl
        WHERE dl.driver_id = %s
          AND dl.trace_data->'arc_band'->>'pickup_address' IS NOT NULL
          AND dl.trace_data->'arc_band'->>'pickup_address' NOT IN
              ('Red Zone Test, Houston, TX', 'Far away, TX')
          AND dl.pickup_lat IS NOT NULL
          AND dl.current_lat IS NOT NULL
          AND dl.pickup_miles IS NOT NULL
        ORDER BY dl.created_at DESC
        LIMIT 100
    """, (DRIVER_ID,))
    offers = cur.fetchall()

    results = []
    tier_counts = {'Tier0': 0, 'Tier1': 0, 'Tier2': 0, 'Tier3': 0, 'None': 0}

    for o in offers:
        geocoded_miles = tri._haversine_miles(
            float(o['current_lat']), float(o['current_lng']),
            float(o['pickup_lat']), float(o['pickup_lng'])
        )

        log_capture = io.StringIO()
        handler = logging.StreamHandler(log_capture)
        handler.setLevel(logging.INFO)
        logging.getLogger().addHandler(handler)
        logging.getLogger().setLevel(logging.INFO)

        h3 = tri.triangulate_pickup(
            float(o['current_lat']), float(o['current_lng']),
            float(o['pickup_lat']), float(o['pickup_lng']),
            float(o['pickup_miles']),
            geocoded_miles,
            cur,
            street_name=o['pickup_address'],
            gps_age_sec=5.0
        )

        logging.getLogger().removeHandler(handler)
        logging.getLogger().setLevel(logging.WARNING)
        log_output = log_capture.getvalue()

        if 'Enhanced arc-banding succeeded' in log_output:
            tier = 'Tier0'
        elif 'BULLSEYE' in log_output or 'Tier1' in log_output or 'Google' in log_output:
            tier = 'Tier1'
        elif 'Arc-banding succeeded' in log_output:
            tier = 'Tier2'
        elif 'Uber snap' in log_output:
            tier = 'Tier3'
        else:
            tier = 'None'
        tier_counts[tier] += 1

        error_m = None
        if h3:
            cur.execute(
                "SELECT app_private.h3_to_lat(%s) AS lat, app_private.h3_to_lng(%s) AS lng",
                (h3, h3)
            )
            coords = cur.fetchone()
            if coords and o['pickup_lat']:
                error_m = tri._haversine_miles(
                    float(o['pickup_lat']), float(o['pickup_lng']),
                    float(coords['lat']), float(coords['lng'])
                ) * 1609.34

        results.append({
            'id': o['id'],
            'addr': o['pickup_address'],
            'tier': tier,
            'error_m': error_m
        })

    # Restore original if patched
    if use_houston_ways:
        ab.get_street_geometry = original_get_street_geometry

    return results, tier_counts


def summarize(results, tier_counts, label):
    errors = [r['error_m'] for r in results if r['error_m'] is not None]
    total = len(results)
    under_200 = sum(1 for e in errors if e < 200)
    under_500 = sum(1 for e in errors if e < 500)
    under_800 = sum(1 for e in errors if e < 800)
    avg = sum(errors) / len(errors) if errors else 0
    median = sorted(errors)[len(errors) // 2] if errors else 0

    print(f"\n{'─'*60}")
    print(f"  {label}")
    print(f"{'─'*60}")
    print(f"  Total offers:    {total}")
    print(f"  Tier breakdown:  {tier_counts}")
    print(f"  Under 200m:      {under_200}/{total} ({100*under_200/total:.1f}%)")
    print(f"  Under 500m:      {under_500}/{total} ({100*under_500/total:.1f}%)  ← headline metric")
    print(f"  Under 800m:      {under_800}/{total} ({100*under_800/total:.1f}%)")
    print(f"  Avg error:       {avg:.0f}m")
    print(f"  Median error:    {median:.0f}m")

    return under_500, avg, median


def run():
    conn = psycopg2.connect(**DB_CONFIG, cursor_factory=psycopg2.extras.RealDictCursor)
    conn.autocommit = True
    cur = conn.cursor()

    print(f"\n{'='*60}")
    print(f"  🐸 Triangulation Backtest: street_network vs houston_ways")
    print(f"{'='*60}")

    print("\nRunning street_network backtest...")
    sn_results, sn_tiers = run_backtest(conn, cur, use_houston_ways=False)
    sn_500, sn_avg, sn_median = summarize(sn_results, sn_tiers, "street_network (current)")

    print("\nRunning houston_ways backtest...")
    hw_results, hw_tiers = run_backtest(conn, cur, use_houston_ways=True)
    hw_500, hw_avg, hw_median = summarize(hw_results, hw_tiers, "routing.houston_ways (candidate)")

    print(f"\n{'='*60}")
    print(f"  📊 HEAD-TO-HEAD COMPARISON")
    print(f"{'='*60}")
    diff_500 = hw_500 - sn_500
    diff_avg = sn_avg - hw_avg
    diff_med = sn_median - hw_median
    winner = "houston_ways" if hw_500 > sn_500 else "street_network" if sn_500 > hw_500 else "TIE"
    print(f"  Under 500m:   street_network={sn_500}  houston_ways={hw_500}  diff={diff_500:+d}")
    print(f"  Avg error:    street_network={sn_avg:.0f}m  houston_ways={hw_avg:.0f}m  diff={diff_avg:+.0f}m")
    print(f"  Median:       street_network={sn_median:.0f}m  houston_ways={hw_median:.0f}m  diff={diff_med:+.0f}m")
    print(f"\n  🏆 Winner: {winner}")
    print(f"{'='*60}\n")

    conn.close()


if __name__ == "__main__":
    run()
