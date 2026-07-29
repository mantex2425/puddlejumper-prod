"""
backtest_harwin_replay.py — Harwin Replay (SB3 accuracy gate)
Patch 00566a Step 15.

For each historical trip with ground-truth actual_dropoff coords, compare:
  original_error_m = haversine(offer_history.dropoff_lat/lng, actual_dropoff_*)
  refined_error_m  = haversine(handle_post_nail_refinement(...), actual_dropoff_*)

Uses the full Plan module (handle_post_nail_refinement with mode='dropoff')
so the gate policy matches production. Trips where the gate blocks refinement
count as 'no-op' (refined = original).

Usage:
  python3 backtest_harwin_replay.py
"""

import sys, os, math, importlib.util
sys.path.insert(0, os.path.expanduser('~/puddlejumper-prod'))

import psycopg2
from psycopg2.extras import RealDictCursor

# Standalone import (bypass decisions/__init__.py which needs firebase)
_spec = importlib.util.spec_from_file_location("nail_manager", "decisions/nail_manager.py")
nm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nm)

DB_CONFIG = dict(host="10.128.0.3", dbname="puddlejumper", user="postgres")

def haversine_m(lat1, lng1, lat2, lng2):
    R = 6371000.0
    lat1, lng1, lat2, lng2 = map(math.radians, [lat1, lng1, lat2, lng2])
    dlat = lat2 - lat1
    dlng = lng2 - lng1
    a = math.sin(dlat/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin(dlng/2)**2
    return R * 2 * math.asin(math.sqrt(a))


def classify_error(m):
    if m < 100:    return "<100m"
    if m < 500:    return "100-500m"
    if m < 2000:   return "500-2000m"
    if m < 5000:   return "2000-5000m"
    return ">5000m"


def main():
    conn = psycopg2.connect(cursor_factory=RealDictCursor, **DB_CONFIG)
    conn.autocommit = True
    cur = conn.cursor()

    # Pull all trips with ground-truth actual_dropoff + all required inputs
    cur.execute("""
        SELECT decision_log_id,
               pickup_address, dropoff_address,
               actual_pickup_lat, actual_pickup_lng,
               dropoff_lat, dropoff_lng,
               actual_dropoff_lat, actual_dropoff_lng,
               trip_miles
        FROM app_private.offer_history
        WHERE decision_log_id IS NOT NULL
          AND actual_pickup_lat IS NOT NULL
          AND dropoff_lat IS NOT NULL
          AND actual_dropoff_lat IS NOT NULL
          AND trip_miles IS NOT NULL
          AND trip_miles > 0.5
        ORDER BY decision_log_id ASC
    """)
    rows = cur.fetchall()
    print(f"Found {len(rows)} trips with ground-truth actual_dropoff coords\n")

    results = []
    for r in rows:
        original_err_m = haversine_m(
            float(r['dropoff_lat']),        float(r['dropoff_lng']),
            float(r['actual_dropoff_lat']), float(r['actual_dropoff_lng']),
        )

        try:
            plan = nm.handle_post_nail_refinement(
                offer_id=r['decision_log_id'],
                driver_id="UjT1hE9eBXh2q95aSZYOkzDJ8lo1",
                nailed_anchor_lat=float(r['actual_pickup_lat']),
                nailed_anchor_lng=float(r['actual_pickup_lng']),
                cur=cur,
                mode="dropoff",
            )
        except Exception as e:
            results.append({
                "offer_id": r['decision_log_id'],
                "addr": r.get('dropoff_address', '')[:40],
                "original_err_m": original_err_m,
                "refined_err_m": original_err_m,
                "source": None,
                "outcome": "exception",
                "delta_m": 0,
                "error": str(e)[:80],
            })
            continue

        if plan is None:
            # Gate blocked or engine miss — refined coord IS the original
            refined_err_m = original_err_m
            source = "(no-op)"
            outcome = "no_op"
        else:
            refined_err_m = haversine_m(
                plan['refined_lat'], plan['refined_lng'],
                float(r['actual_dropoff_lat']), float(r['actual_dropoff_lng']),
            )
            source = plan['refinement_source']
            if   refined_err_m < original_err_m - 10:  outcome = "improved"
            elif refined_err_m > original_err_m + 10:  outcome = "degraded"
            else:                                      outcome = "neutral"

        results.append({
            "offer_id":       r['decision_log_id'],
            "addr":           (r.get('dropoff_address') or '')[:40],
            "original_err_m": original_err_m,
            "refined_err_m":  refined_err_m,
            "source":         source,
            "outcome":        outcome,
            "delta_m":        original_err_m - refined_err_m,  # positive = improvement
        })

    # ── Per-row table, sorted by original error descending ─────────────
    results.sort(key=lambda x: -x['original_err_m'])
    print(f"{'Offer':>6}  {'orig':>6}  {'refn':>6}  {'Δ':>7}  {'bucket':>12}  {'outcome':>9}  {'source':>18}  address")
    print("─" * 130)
    for x in results:
        bucket = classify_error(x['original_err_m'])
        print(f"{x['offer_id']:>6}  "
              f"{x['original_err_m']:>6.0f}  "
              f"{x['refined_err_m']:>6.0f}  "
              f"{x['delta_m']:>+7.0f}  "
              f"{bucket:>12}  "
              f"{x['outcome']:>9}  "
              f"{str(x['source']):>18}  "
              f"{x['addr']}")

    # ── Aggregate summary ───────────────────────────────────────────────
    print("\n" + "═" * 70)
    print("SUMMARY")
    print("═" * 70)
    n = len(results)
    improved = sum(1 for x in results if x['outcome'] == 'improved')
    degraded = sum(1 for x in results if x['outcome'] == 'degraded')
    neutral  = sum(1 for x in results if x['outcome'] == 'neutral')
    no_op    = sum(1 for x in results if x['outcome'] == 'no_op')
    exc      = sum(1 for x in results if x['outcome'] == 'exception')

    print(f"  Total trips:          {n}")
    print(f"  Improved:             {improved:>4}  ({100*improved/n:.0f}%)")
    print(f"  Degraded:             {degraded:>4}  ({100*degraded/n:.0f}%)")
    print(f"  Neutral (±10m):       {neutral:>4}  ({100*neutral/n:.0f}%)")
    print(f"  No-op (gate/miss):    {no_op:>4}  ({100*no_op/n:.0f}%)")
    print(f"  Exception:            {exc:>4}")

    # Mean / median comparison
    orig_errs = [x['original_err_m'] for x in results]
    refn_errs = [x['refined_err_m']  for x in results]
    def _median(lst):
        s = sorted(lst); k = len(s)
        return (s[k//2] + s[(k-1)//2]) / 2.0
    print(f"\n  Mean original error:  {sum(orig_errs)/n:>7.0f}m")
    print(f"  Mean refined error:   {sum(refn_errs)/n:>7.0f}m")
    print(f"  Median original:      {_median(orig_errs):>7.0f}m")
    print(f"  Median refined:       {_median(refn_errs):>7.0f}m")

    # Improvement magnitude among the improved
    improvements = [x['delta_m'] for x in results if x['outcome'] == 'improved']
    degradations = [abs(x['delta_m']) for x in results if x['outcome'] == 'degraded']
    if improvements:
        print(f"\n  Mean improvement (when it helped):   {sum(improvements)/len(improvements):>7.0f}m")
        print(f"  Max improvement:                     {max(improvements):>7.0f}m")
    if degradations:
        print(f"  Mean degradation (when it hurt):     {sum(degradations)/len(degradations):>7.0f}m")
        print(f"  Max degradation:                     {max(degradations):>7.0f}m")

    # By bucket — where does SB3 help most
    print(f"\n  Outcome by original-error bucket:")
    buckets = ["<100m", "100-500m", "500-2000m", "2000-5000m", ">5000m"]
    for b in buckets:
        in_b = [x for x in results if classify_error(x['original_err_m']) == b]
        if not in_b: continue
        imp = sum(1 for x in in_b if x['outcome'] == 'improved')
        deg = sum(1 for x in in_b if x['outcome'] == 'degraded')
        nop = sum(1 for x in in_b if x['outcome'] == 'no_op')
        neu = sum(1 for x in in_b if x['outcome'] == 'neutral')
        print(f"    {b:>12}: n={len(in_b):>3}  improved={imp:>3}  degraded={deg:>3}  neutral={neu:>3}  no_op={nop:>3}")

    # Source distribution
    print(f"\n  Engine source distribution (when Plan returned):")
    srcs = {}
    for x in results:
        if x['source'] != '(no-op)':
            srcs[x['source']] = srcs.get(x['source'], 0) + 1
    for src, count in sorted(srcs.items(), key=lambda kv: -kv[1]):
        print(f"    {src:>20}: {count}")

    conn.close()

if __name__ == "__main__":
    main()
