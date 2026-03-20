from flask import Blueprint, jsonify, request
from db import get_db
from utils import verify_and_get_user_id, require_firebase_auth

timelapse_bp = Blueprint('timelapse', __name__)

@require_firebase_auth
@timelapse_bp.route('/timelapse', methods=['GET'])
def get_timelapse_frame():
    try:
        uid = verify_and_get_user_id(request)
    except:
        return jsonify({"status": "auth_failed"}), 403

    day = request.args.get('day', type=int)       # optional: 0-6
    hour = request.args.get('hour', type=int)      # optional: 0-23
    min_samples = request.args.get('min_samples', 2, type=int)

    if day is not None and (day < 0 or day > 6):
        return jsonify({"error": "day must be 0-6"}), 400
    if hour is not None and (hour < 0 or hour > 23):
        return jsonify({"error": "hour must be 0-23"}), 400

    conn = get_db()
    cur = conn.cursor()

    try:
        params = []
        day_clause = ""
        hour_clause = ""

        if day is not None:
            day_clause = "AND day_of_week = %s"
            params.append(day)
        if hour is not None:
            hour_clause = "AND hour_of_day = %s"
            params.append(hour)

        cur.execute(f"""
            SELECT 
                driver_h3 as h3_index,
                count(*) as samples,
                round(percentile_cont(0.50) WITHIN GROUP (ORDER BY effective_hourly_rate)::numeric, 2) as ai_hourly,
                round(percentile_cont(0.50) WITHIN GROUP (ORDER BY dollars_per_mile)::numeric, 2) as ai_mileage
            FROM app_private.offer_history
            WHERE driver_h3 IS NOT NULL
              AND is_validated = true
              AND effective_hourly_rate > 0 AND effective_hourly_rate < 150
              AND dollars_per_mile > 0 AND dollars_per_mile < 10
              {day_clause}
              {hour_clause}
            GROUP BY driver_h3
            HAVING count(*) >= %s
            ORDER BY count(*) DESC
        """, params + [min_samples])

        hexes = []
        for row in cur.fetchall():
            s = row['samples']
            if s >= 10:
                opacity = 0.50
            elif s >= 5:
                opacity = 0.30
            else:
                opacity = 0.15

            hexes.append({
                "h3": row['h3_index'],
                "hourly": float(row['ai_hourly']),
                "mileage": float(row['ai_mileage']),
                "samples": s,
                "opacity": opacity
            })

        # Summary using same filters
        cur.execute(f"""
            SELECT 
                count(*) as hex_count,
                sum(samples) as total_samples,
                round(avg(ai_hourly)::numeric, 2) as avg_hourly,
                round(avg(ai_mileage)::numeric, 2) as avg_mileage
            FROM (
                SELECT driver_h3,
                    count(*) as samples,
                    percentile_cont(0.50) WITHIN GROUP (ORDER BY effective_hourly_rate)::numeric as ai_hourly,
                    percentile_cont(0.50) WITHIN GROUP (ORDER BY dollars_per_mile)::numeric as ai_mileage
                FROM app_private.offer_history
                WHERE driver_h3 IS NOT NULL
                  AND is_validated = true
                  AND effective_hourly_rate > 0 AND effective_hourly_rate < 150
                  AND dollars_per_mile > 0 AND dollars_per_mile < 10
                  {day_clause}
                  {hour_clause}
                GROUP BY driver_h3
                HAVING count(*) >= %s
            ) sub
        """, params + [min_samples])
        s = cur.fetchone()

        return jsonify({
            "day": day,
            "hour": hour,
            "hexes": hexes,
            "summary": {
                "avgHourly": float(s['avg_hourly']) if s['avg_hourly'] else 0,
                "avgMileage": float(s['avg_mileage']) if s['avg_mileage'] else 0,
                "hexCount": s['hex_count'] or 0,
                "totalSamples": s['total_samples'] or 0
            }
        })
    finally:
        cur.close()
        conn.close()

# ============================================================
# APPEND THIS to ~/puddlejumper-prod/timelapse.py
# (after the existing get_timelapse_frame function)
# No new imports needed — uses same Blueprint and decorators.
# Endpoint: GET /api/v1/timelapse/grid?min_samples=2
# ============================================================

@require_firebase_auth
@timelapse_bp.route('/timelapse/grid', methods=['GET'])
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
            FROM app_private.offer_history
            WHERE driver_h3 IS NOT NULL
              AND is_validated = true
              AND effective_hourly_rate > 0 AND effective_hourly_rate < 150
              AND dollars_per_mile > 0 AND dollars_per_mile < 10
            GROUP BY day_of_week, hour_of_day
            HAVING count(*) >= %s
            ORDER BY day_of_week, hour_of_day
        """, (min_samples,))

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
