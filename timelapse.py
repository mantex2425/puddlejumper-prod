"""
Time Grid: a 7x24 grid of pooled offer economics by weekday and hour.

Named timelapse for historical reasons. The map timelapse this module was written for
was removed on 2026-09-14 (split across hex x hour, nearly every frame held one or two
offers, so it animated noise), and its /api/v1/timelapse frame endpoint went with it on
2026-09-17. The remaining endpoint keeps its /api/v1/timelapse/grid path because
installed apps call it; the Android screen is called Time Grid.
"""

from flask import Blueprint, jsonify, request
from db import get_db
from offer_copies import not_a_copy
from utils import verify_and_get_user_id, require_firebase_auth

# NOTE 2026-08-20: these queries group by pickup_h3 (where the rider
# is), NOT driver_h3 (where the driver was sitting). They agree only
# 4.3% of the time. pickup_h3 is what generalises across drivers.
timelapse_bp = Blueprint('timelapse', __name__)

@timelapse_bp.route('/timelapse/grid', methods=['GET'])
@require_firebase_auth
def get_time_grid():
    """
    Returns a 7x24 aggregated grid of offer pricing data.
    Each cell = one day_of_week + hour_of_day combination.
    Optional: min_samples (default 2)
    """
    try:
        uid = verify_and_get_user_id(request)
    except:
        return jsonify({"status": "auth_failed"}), 403

    min_samples = request.args.get('min_samples', 2, type=int)

    # Spatial scope, parsed exactly as get_timelapse() does above. The scoped
    # SQL below was added to this handler without these four names, so every
    # request raised NameError: name 'scoped' is not defined and returned 500 -
    # the Time Grid screen showed "Time grid API failed: 500 Internal Server
    # Error" whatever data existed (found 2026-09-14; 145 of 168 slots had data).
    lat = request.args.get('lat', type=float)
    lng = request.args.get('lng', type=float)
    radius_mi = min(max(request.args.get('radiusMiles', 40, type=int), 1), 300)
    scoped = lat is not None and lng is not None

    conn = get_db()
    cur = conn.cursor()

    try:
        cur.execute("""
            SELECT
                day_of_week,
                hour_of_day,
                count(*) as samples,
                round(avg(effective_hourly_rate)::numeric, 2) as avg_hourly,
                round(percentile_cont(0.50) WITHIN GROUP (ORDER BY effective_hourly_rate)::numeric, 2) as median_hourly,
                round(avg(dollars_per_mile)::numeric, 2) as avg_mileage,
                round(percentile_cont(0.50) WITHIN GROUP (ORDER BY dollars_per_mile)::numeric, 2) as median_mileage
            FROM app_private.offer_history c
            WHERE pickup_h3 IS NOT NULL
              AND is_validated = true
              AND effective_hourly_rate > 0 AND effective_hourly_rate < 150
              AND dollars_per_mile > 0 AND dollars_per_mile < 10
              {scope_clause}
              {copy_clause}
            GROUP BY day_of_week, hour_of_day
            HAVING count(*) >= %s
            ORDER BY day_of_week, hour_of_day
        """.format(scope_clause=(
            # Scope by the stored H3 cell's centre, not by a stored coordinate:
            # offer_history is pooled, so it keeps cells only (2026-09-18). point[1]
            # is the latitude and point[0] the longitude.
            "AND pickup_h3 IS NOT NULL "
            "AND app_private.distance_miles("
            "  (h3_cell_to_latlng(pickup_h3::h3index))[1], "
            "  (h3_cell_to_latlng(pickup_h3::h3index))[0], %s, %s) <= %s"
            if scoped else ""
        ), copy_clause=not_a_copy("c", "app_private.offer_history")), (([lat, lng, radius_mi] if scoped else []) + [min_samples]))

        rows = cur.fetchall()

        cells = []
        for row in rows:
            samples = row['samples']
            if samples >= 10:
                confidence = "high"
            elif samples >= 5:
                confidence = "medium"
            else:
                confidence = "low"

            cells.append({
                "day": row['day_of_week'],
                "hour": row['hour_of_day'],
                "samples": samples,
                "avgHourly": float(row['avg_hourly']),
                "medianHourly": float(row['median_hourly']),
                "avgMileage": float(row['avg_mileage']),
                "medianMileage": float(row['median_mileage']),
                "confidence": confidence
            })

        total_samples = sum(c['samples'] for c in cells)
        coverage = len(cells)
        weighted_hourly = sum(c['medianHourly'] * c['samples'] for c in cells) / total_samples if total_samples > 0 else 0
        best_cell = max(cells, key=lambda c: c['medianHourly']) if cells else None

        return jsonify({
            "status": "ok",
            "cells": cells,
            "scoped": scoped,
            "scopeRadiusMiles": radius_mi if scoped else None,
            "summary": {
                "cellCount": coverage,
                "totalSlots": 168,
                "coveragePercent": round(coverage / 168 * 100, 1),
                "totalSamples": total_samples,
                "weightedAvgHourly": round(weighted_hourly, 2),
                "bestDay": best_cell['day'] if best_cell else None,
                "bestHour": best_cell['hour'] if best_cell else None,
                "bestMedianHourly": best_cell['medianHourly'] if best_cell else None
            }
        })

    except Exception as e:
        import logging
        logging.error(f"Time grid error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()
        conn.close()
