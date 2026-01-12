import json
import requests
import re

from flask import Blueprint, jsonify, request
from functools import wraps

from db import get_db_connection
import psycopg2.extras
import psycopg2
from psycopg2.extras import RealDictCursor

# Import necessary utilities
from utils import (
    MAPBOX_API_KEY,
    MAPBOX_GEOCODING_URL,
    H3_L7_RESOLUTION,
    get_active_market_config,
    get_h3_city_lookup,
    verify_and_get_user_id,
    ensure_user_exists,
    require_firebase_auth
)

zones_lookup_bp = Blueprint("zones_lookup", __name__)


# ---------------------------------------------------------------------
# GET /api/v1/zones/lookup
# ---------------------------------------------------------------------
@require_firebase_auth
@zones_lookup_bp.route('/api/v1/zones/lookup', methods=['GET'])
def lookup_city_h3_key():

    # 1. AUTH
    try:
        uid = verify_and_get_user_id(request)
    except Exception:
        return jsonify({"error": "Invalid or expired token"}), 403

    # 2. DB
    conn = get_db_connection()
    cur = conn.cursor()

    # 3. ENSURE USER
    ensure_user_exists(cur, uid)
    conn.commit()

    city_name = request.args.get('query')
    if not city_name:
        return jsonify({"error": "Missing 'query' parameter (city name)"}), 400

    try:
        lookup_result = get_h3_city_lookup(city_name)
        return jsonify(lookup_result), 200
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        return jsonify({"error": f"Lookup failed due to internal error: {e}"}), 500
    finally:
        conn.close()


# ---------------------------------------------------------------------
# GET /api/v1/states/<state_id>/cities
# ---------------------------------------------------------------------
@require_firebase_auth
@zones_lookup_bp.route("/api/v1/states/<state_id>/cities", methods=["GET"])
def get_state_cities(state_id):

    # AUTH
    try:
        verify_and_get_user_id(request)
    except Exception:
        return jsonify({"error": "Invalid or expired token"}), 403

    normalized_state_id = state_id.upper()

    if not re.match(r"^[A-Z]{2}$", normalized_state_id):
        return jsonify({
            "error": "Invalid State ID",
            "message": "State must be a 2-letter uppercase US code (e.g., 'TX')."
        }), 400

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)

        sql_query = """
            SELECT city, state_id, lat, lng, population, hex_l7
            FROM cities
            WHERE state_id = %s
            ORDER BY city ASC;
        """
        cursor.execute(sql_query, (normalized_state_id,))
        results = cursor.fetchall()

        if not results:
            return jsonify({
                "error": "Not Found",
                "message": f"No cities found for state '{normalized_state_id}'."
            }), 404

        city_list = [{
            "city": row["city"],
            "lat": row["lat"],
            "lng": row["lng"],
            "population": row["population"],
            "hex_l7": row["hex_l7"]
        } for row in results]

        return jsonify({
            "state": normalized_state_id,
            "count": len(city_list),
            "cities": city_list
        }), 200

    except psycopg2.Error as e:
        print(f"POSTGRES ERROR (get_state_cities): {e}")
        return jsonify({"error": "Database Query Failed"}), 500

    finally:
        if conn:
            conn.close()


# ---------------------------------------------------------------------
# GET /api/v1/neighborhoods
# ---------------------------------------------------------------------
@require_firebase_auth
@zones_lookup_bp.route("/api/v1/neighborhoods", methods=["GET"])
def get_neighborhoods():

    conn = None
    try:
        # AUTH
        try:
            verify_and_get_user_id(request)
        except Exception:
            return jsonify({"error": "Invalid or expired token"}), 403

        # PARAMS
        city_name = request.args.get('city')
        search_query = request.args.get('query')
        limit = int(request.args.get('limit', 50))

        if not city_name:
            return jsonify({
                "error": "Missing Parameter",
                "message": "The 'city' parameter is required."
            }), 400

        search_pattern = f"%{search_query.lower()}%" if search_query else None

        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)

        sql_query = """
            SELECT DISTINCT n.id, n.city_name, n.name, n.source,
                            n.center_lat, n.center_lon, n.h3_index
            FROM neighborhoods n
            LEFT JOIN neighborhood_aliases a
                ON a.neighborhood_id = n.id
            WHERE n.city_name = %s
        """

        params = [city_name]

        if search_pattern:
            sql_query += " AND (n.name ILIKE %s OR a.alias ILIKE %s)"
            params.extend([search_pattern, search_pattern])

        sql_query += " ORDER BY n.name ASC LIMIT %s"
        params.append(limit)

        cursor.execute(sql_query, tuple(params))
        results = cursor.fetchall()

        # Fetch all aliases
        ids = [r['id'] for r in results]
        aliases_map = {}

        if ids:
            id_string = ", ".join([str(i) for i in ids])
            alias_query = f"""
                SELECT neighborhood_id, alias
                FROM neighborhood_aliases
                WHERE neighborhood_id IN ({id_string});
            """
            cursor.execute(alias_query)
            alias_rows = cursor.fetchall()

            for r in alias_rows:
                aliases_map.setdefault(r['neighborhood_id'], []).append(r['alias'])

        formatted = []
        for row in results:
            formatted.append({
                "id": row["id"],
                "city": row["city_name"],
                "name": row["name"],
                "source": row["source"],
                "lat": row["center_lat"],
                "lon": row["center_lon"],
                "h3_index": row["h3_index"],
                "aliases": aliases_map.get(row["id"], [])
            })

        return jsonify(formatted), 200

    except Exception as e:
        print(f"NEIGHBORHOOD API ERROR: {e}")
        return jsonify({
            "error": "Internal Server Error",
            "message": f"Neighborhood lookup failed: {str(e)}"
        }), 500

    finally:
        if conn:
            conn.close()