# backend/unified_search.py
from flask import Blueprint, request, jsonify
from psycopg2.extras import RealDictCursor
from db import get_db

bp = Blueprint("unified_search", __name__)

@bp.get("/search")
def unified_search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"results": []})

    conn = get_db()
    param_ilike = f"%{q}%"

    queries = [
        ("city", """
            SELECT
                city AS name,
                state_name AS city,
                lat AS center_lat,
                lng AS center_lng,
                hex_l7 AS h3_index_l7,
                similarity(city, %s) AS score
            FROM cities
            WHERE city ILIKE %s OR city %% %s
            ORDER BY score DESC
            LIMIT 50
        """),
        ("neighborhood", """
            SELECT
                n.name AS name,
                n.city_name AS city,
                n.center_lat,
                n.center_lon AS center_lng,
                n.h3_index AS h3_index_l7,
                similarity(n.name, %s) AS score
            FROM neighborhoods n
            WHERE n.name ILIKE %s OR n.name %% %s
            ORDER BY score DESC
            LIMIT 50
        """),
        ("alias", """
            SELECT
                a.alias AS name,
                n.city_name AS city,
                n.center_lat,
                n.center_lon AS center_lng,
                n.h3_index AS h3_index_l7,
                similarity(a.alias, %s) AS score
            FROM neighborhood_aliases a
            JOIN neighborhoods n ON a.neighborhood_id = n.id
            WHERE a.alias ILIKE %s OR a.alias %% %s
            ORDER BY score DESC
            LIMIT 50
        """),
        ("market", """
            SELECT
                name,
                NULL AS city,
                center_lat,
                center_lng,
                h3_index_l7,
                similarity(name, %s) AS score
            FROM market_areas
            WHERE name ILIKE %s OR name %% %s
            ORDER BY score DESC
            LIMIT 50
        """),
    ]

    results = []
    try:
        for source, sql in queries:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(sql, (q, param_ilike, q))
                for row in cur.fetchall():
                    results.append({
                        "name": row["name"],
                        "city": row.get("city"),
                        "center_lat": row.get("center_lat") or row.get("center_lng"),  # fallback
                        "center_lng": row.get("center_lng"),
                        "h3_l8": [row["h3_index_l7"]] if row.get("h3_index_l7") else [],
                        "score": float(row["score"] or 0),
                        "source": source
                    })

        results.sort(key=lambda x: x["score"], reverse=True)
        return jsonify({"results": results[:100]})

    except Exception as e:
        print(f"[unified_search] ERROR: {e}")
        print(f"SQL: {sql}")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()
