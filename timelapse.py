from flask import Blueprint, jsonify, request
from db import get_db
from utils import verify_and_get_user_id, require_firebase_auth

timelapse_bp = Blueprint('timelapse', __name__)

@require_firebase_auth
@timelapse_bp.route('/timelapse', methods=['GET'])
def get_timelapse_frame():
    """
    Returns hex pricing data for a single day+hour frame.
    Query params: day (0-6, DOW), hour (0-23)
    Optional: include_fallback (true/false, default false)
    """
    try:
        uid = verify_and_get_user_id(request)
    except:
        return jsonify({"status": "auth_failed"}), 403

    day = request.args.get('day', type=int)
    hour = request.args.get('hour', type=int)
    include_fallback = request.args.get('include_fallback', 'false').lower() == 'true'

    if day is None or hour is None:
        return jsonify({"error": "day and hour required"}), 400
    if day < 0 or day > 6 or hour < 0 or hour > 23:
        return jsonify({"error": "day must be 0-6, hour must be 0-23"}), 400

    conn = get_db()
    cur = conn.cursor()

    try:
        # Get hex pricing data for this frame
        if include_fallback:
            cur.execute("""
                SELECT h3_index, ai_hourly, ai_mileage,
                       revenue_hourly, revenue_mileage,
                       picky_hourly, picky_mileage,
                       sample_count, data_source
                FROM app_private.hex_pricing_cache
                WHERE day_of_week = %s AND hour_of_day = %s
                ORDER BY sample_count DESC
            """, (day, hour))
        else:
            cur.execute("""
                SELECT h3_index, ai_hourly, ai_mileage,
                       revenue_hourly, revenue_mileage,
                       picky_hourly, picky_mileage,
                       sample_count, data_source
                FROM app_private.hex_pricing_cache
                WHERE day_of_week = %s AND hour_of_day = %s
                  AND data_source != 'metro_fallback'
                ORDER BY sample_count DESC
            """, (day, hour))

        hexes = []
        for row in cur.fetchall():
            hexes.append({
                "h3": row['h3_index'],
                "aiHourly": float(row['ai_hourly']),
                "aiMileage": float(row['ai_mileage']),
                "revenueHourly": float(row['revenue_hourly']),
                "revenueMileage": float(row['revenue_mileage']),
                "pickyHourly": float(row['picky_hourly']),
                "pickyMileage": float(row['picky_mileage']),
                "samples": row['sample_count'],
                "source": row['data_source']
            })

        # Get metro-wide summary for this frame (for the "average" indicator)
        cur.execute("""
            SELECT round(avg(ai_hourly)::numeric, 2) AS avg_hourly,
                   round(avg(ai_mileage)::numeric, 2) AS avg_mileage,
                   count(*) AS hex_count,
                   sum(sample_count) AS total_samples
            FROM app_private.hex_pricing_cache
            WHERE day_of_week = %s AND hour_of_day = %s
        """, (day, hour))
        summary_row = cur.fetchone()

        summary = {
            "avgHourly": float(summary_row['avg_hourly']) if summary_row['avg_hourly'] else 0,
            "avgMileage": float(summary_row['avg_mileage']) if summary_row['avg_mileage'] else 0,
            "hexCount": summary_row['hex_count'] or 0,
            "totalSamples": summary_row['total_samples'] or 0
        }

        return jsonify({
            "day": day,
            "hour": hour,
            "hexes": hexes,
            "summary": summary
        })

    finally:
        cur.close()
        conn.close()
