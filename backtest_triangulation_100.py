"""
backtest_triangulation_100.py — 100-offer triangulation accuracy backtest

Runs triangulate_pickup() against the last 100 real offers from decision_log
and reports accuracy metrics. Use this as a regression test after any
triangulation changes.

Baseline: 82/100 under 500m (v6, April 2026)

Usage:
  python3 backtest_triangulation_100.py
"""

import psycopg2, psycopg2.extras, sys, logging, io
logging.basicConfig(level=logging.WARNING, format="%(message)s")
sys.path.insert(0, '/home/andrew/puddlejumper-prod')
from triangulation import triangulate_pickup, _haversine_miles

DRIVER_ID = "UjT1hE9eBXh2q95aSZYOkzDJ8lo1"
DB_CONFIG  = dict(host="10.128.0.2", dbname="puddlejumper", user="postgres")
BASELINE   = 87  # under 500m — beat this to claim improvement

def run():
    conn = psycopg2.connect(**DB_CONFIG, cursor_factory=psycopg2.extras.RealDictCursor)
    conn.autocommit = True
    cur = conn.cursor()

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
        LIMIT 500
    """, (DRIVER_ID,))
    all_offers = cur.fetchall()
    # Deduplicate by pickup_address — keep most recent of each
    seen = set()
    offers = []
    for o in all_offers:
        addr = o['pickup_address']
        if addr not in seen:
            seen.add(addr)
            offers.append(o)
        if len(offers) >= 100:
            break

    print(f"\n{'='*70}")
    print(f"  🐸 Triangulation Accuracy Backtest")
    print(f"  Testing {len(offers)} real offers | Baseline: {BASELINE}/100 under 500m")
    print(f"{'='*70}\n")

    results = []
    tier_counts = {'Tier0': 0, 'Tier1': 0, 'Tier2': 0, 'Tier3': 0, 'None': 0}

    for o in offers:
        geocoded_miles = _haversine_miles(
            float(o['current_lat']), float(o['current_lng']),
            float(o['pickup_lat']), float(o['pickup_lng'])
        )

        log_capture = io.StringIO()
        handler = logging.StreamHandler(log_capture)
        handler.setLevel(logging.INFO)
        logging.getLogger().addHandler(handler)
        logging.getLogger().setLevel(logging.INFO)

        h3 = triangulate_pickup(
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
        elif 'BULLSEYE' in log_output or 'Google' in log_output:
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
                error_m = _haversine_miles(
                    float(o['pickup_lat']), float(o['pickup_lng']),
                    float(coords['lat']), float(coords['lng'])
                ) * 1609.34

        results.append({
            'id':      o['id'],
            'addr':    o['pickup_address'],
            'tier':    tier,
            'error_m': error_m
        })

    # ── Results table ─────────────────────────────────────────────────────
    print(f"  {'ID':>6}  {'Tier':6}  {'Error':>8}  Address")
    print(f"  {'─'*65}")
    for r in sorted(results, key=lambda x: x['error_m'] or 99999):
        e = f"{r['error_m']:.0f}m" if r['error_m'] is not None else "N/A"
        flag = "✅" if r['error_m'] and r['error_m'] < 500 else "⚠️" if r['error_m'] and r['error_m'] < 800 else "❌"
        print(f"  {r['id']:>6}  {r['tier']:6}  {e:>8}  {flag} {r['addr'][:45]}")

    # ── Summary ───────────────────────────────────────────────────────────
    errors     = [r['error_m'] for r in results if r['error_m'] is not None]
    under_200  = sum(1 for e in errors if e < 200)
    under_500  = sum(1 for e in errors if e < 500)
    under_800  = sum(1 for e in errors if e < 800)
    avg        = sum(errors) / len(errors) if errors else 0
    median     = sorted(errors)[len(errors) // 2] if errors else 0
    total      = len(results)

    print(f"\n{'─'*70}")
    print(f"  Total:          {total}")
    print(f"  Tier breakdown: {tier_counts}")
    print(f"  Under 200m:     {under_200}/{total} ({100*under_200/total:.1f}%)")
    print(f"  Under 500m:     {under_500}/{total} ({100*under_500/total:.1f}%)  ← headline metric")
    print(f"  Under 800m:     {under_800}/{total} ({100*under_800/total:.1f}%)")
    print(f"  Avg error:      {avg:.0f}m")
    print(f"  Median error:   {median:.0f}m")

    delta = under_500 - BASELINE
    verdict = f"✅ +{delta} vs baseline" if delta > 0 else f"❌ {delta} vs baseline" if delta < 0 else "➡️ Matches baseline"
    print(f"\n  Baseline ({BASELINE}/100):  {verdict}")
    print(f"{'='*70}\n")

    conn.close()
    return under_500 >= BASELINE

if __name__ == "__main__":
    success = run()
    sys.exit(0 if success else 1)
