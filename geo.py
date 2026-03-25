from flask import Blueprint, request, jsonify
from db import get_db
from psycopg2.extras import RealDictCursor

# Firebase auth
from utils import verify_and_get_user_id, require_firebase_auth

# H3 resolution for canonical key
H3_RESOLUTION = 8

bp = Blueprint("geo", __name__)

# ---------------------------------------------------------
# Core Helper Function: H3 Lookup
# ---------------------------------------------------------
def lookup_city_h3_key(city_name: str, conn) -> dict:
    if not city_name:
        return {
            "success": False,
            "message": "City name is required for lookup.",
            "data": {}
        }

    search_term = f"%{city_name.strip()}%"

    # UPDATED: Calculate H3 directly in SQL using the extension
    # Function: h3_latlng_to_cell(point(lng, lat), resolution)
    sql_query = """
        SELECT
            m.name,
            c.intptlat,
            c.intptlong,
            h3_latlng_to_cell(POINT(c.intptlong, c.intptlat), %s) as h3_key
        FROM
            metroplexes m
        JOIN
            metroplex_centers c ON m.id = c.metro_id
        WHERE
            m.name ILIKE %s
        LIMIT 1;
    """

    try:
        # Use RealDictCursor to access columns by name
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        # Pass resolution first, then the search term
        cur.execute(sql_query, (H3_RESOLUTION, search_term))
        result = cur.fetchone()

        if result:
            return {
                "success": True,
                "message": "Market lookup successful.",
                "data": {
                    "city_name": result["name"],
                    "latitude": float(result["intptlat"]),
                    "longitude": float(result["intptlong"]),
                    "canonical_h3_key": result["h3_key"], # Now coming directly from DB
                    "h3_resolution": H3_RESOLUTION
                }
            }

    except Exception as e:
        print(f"SQL execution error in lookup_city_h3_key: {e}")
        return {
            "success": False,
            "message": f"Database error during lookup: {e}",
            "data": {}
        }

    return {
        "success": False,
        "message": f"Could not find a metroplex matching '{city_name}'.",
        "data": {}
    }


# ---------------------------------------------------------
# Endpoint: Market Zone Lookup
# ---------------------------------------------------------
@require_firebase_auth
@bp.get("/zones/lookup")
def lookup_market_zone():
    try:
        verify_and_get_user_id(request)
    except Exception as e:
        return jsonify({"error": str(e)}), 401

    city_name = request.args.get("city_name")
    conn = None

    try:
        conn = get_db()
        result_dict = lookup_city_h3_key(city_name, conn)
        status_code = 200 if result_dict.get("success") else 404
        return jsonify(result_dict), status_code

    except Exception as e:
        print("LOOKUP MARKET ZONE ERROR:", repr(e))
        return jsonify({"success": False, "message": str(e)}), 500

    finally:
        if conn:
            conn.close()


# ---------------------------------------------------------
# Endpoint: Cities for Metro
# ---------------------------------------------------------
@require_firebase_auth
@bp.get("/cities")
def get_cities_for_metro():
    try:
        verify_and_get_user_id(request)
    except Exception as e:
        return jsonify({"error": str(e)}), 401

    metro_id = request.args.get("cbsa") 
    if not metro_id:
        return jsonify({"error": "Missing cbsa param"}), 400

    conn = None
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        cur.execute(
            """
            SELECT city_name, state_code
            FROM metroplex_cities
            WHERE metro_id = %s
            ORDER BY city_name
            """,
            (metro_id,)
        )

        rows = cur.fetchall()
        results = [{"city_name": r["city_name"], "state_code": r["state_code"]} for r in rows]

        return jsonify(results)

    except Exception as e:
        print("GEO ERROR:", repr(e))
        return jsonify({"error": str(e)}), 500

    finally:
        if conn:
            conn.close()


# ---------------------------------------------------------
# Endpoint: Complete Dump for Metro (Sanitized)
# ---------------------------------------------------------
@require_firebase_auth
@bp.get("/dump")
def dump_geo_for_metro():
    try:
        verify_and_get_user_id(request)
    except Exception as e:
        return jsonify({"error": str(e)}), 401

    metro_id = request.args.get("cbsa")
    if not metro_id:
        return jsonify({"error": "Missing cbsa param"}), 400

    conn = None
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # 1. Cities
        cur.execute("""
            SELECT city_name, state_code
            FROM metroplex_cities
            WHERE metro_id = %s
            ORDER BY city_name
        """, (metro_id,))
        cities = [{"city_name": r["city_name"], "state_code": r["state_code"]} for r in cur.fetchall()]

        # 2. Neighborhoods & Aliases (REMOVED: Tables dropped)
        # Returning empty lists to maintain JSON structure for legacy clients
        neighborhoods = []
        aliases = []

        return jsonify({
            "id": metro_id,
            "cities": cities,
            "neighborhoods": neighborhoods,
            "aliases": aliases
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500

    finally:
        if conn:
            conn.close()


# ---------------------------------------------------------
# Endpoint: List Metros
# ---------------------------------------------------------
@require_firebase_auth
@bp.get("/metros")
def list_metros():
    try:
        verify_and_get_user_id(request)
    except Exception as e:
        return jsonify({"error": str(e)}), 401

    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        cur.execute("""
            SELECT 
                m.id, 
                m.name, 
                m.state_names,
                c.intptlat, 
                c.intptlong
            FROM 
                metroplexes m
            LEFT JOIN
                metroplex_centers c ON m.id = c.metro_id
            ORDER BY m.name;
        """)

        rows = cur.fetchall()
        results = [{
            "id": r["id"],
            "name": r["name"],
            "state_names": r["state_names"],
            "latitude": r["intptlat"],
            "longitude": r["intptlong"]
        } for r in rows]

        return jsonify(results)

    except Exception as e:
        return jsonify({"error": str(e)}), 500

    finally:
        conn.close()

# ---------------------------------------------------------
# Endpoint: Get Single Metro by ID
# ---------------------------------------------------------
@require_firebase_auth
@bp.get("/metro/<string:metro_id>")
def get_metro_by_id(metro_id):
    """
    Returns a single metroplex record given its ID.
    """
    conn = None
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        cur.execute("""
            SELECT
                m.id,
                m.name AS name,
                m.state_names,
                c.intptlat AS latitude,
                c.intptlong AS longitude
            FROM
                metroplexes m
            LEFT JOIN
                metroplex_centers c ON m.id = c.metro_id
            WHERE
                m.id = %s;
        """, (metro_id,))

        row = cur.fetchone()

        if row is None:
            return jsonify({"error": f"Metro with ID {metro_id} not found."}), 404

        return jsonify(dict(row))

    except Exception as e:
        print(f"GET METRO ERROR: {e}")
        return jsonify({"error": str(e)}), 500

    finally:
        if conn:
            conn.close()

            
 # ---------------------------------------------------------
# Endpoint: Get My Metroplexes (IDs + Names)
# ---------------------------------------------------------
@require_firebase_auth
@bp.get("/my-metros")
def get_my_metros():
    """
    Returns a list of metroplexes (id, name) that the user has saved markets in.
    """
    try:
        # This returns the Firebase UID (e.g., 'Dn5j1...')
        user_id = verify_and_get_user_id(request) 
    except Exception as e:
        return jsonify({"error": str(e)}), 401

    conn = None
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        query = """
            WITH user_market_ids AS (
                SELECT DISTINCT market_element->>'metroplexId' AS metro_id
                FROM app_private.driver_settings_new d,
                     jsonb_array_elements(d.settings->'markets') AS market_element
                WHERE d.driver_id = %s
            )
            SELECT 
                m.id, 
                m.name
            FROM metroplexes m
            JOIN user_market_ids u ON m.id = u.metro_id
            ORDER BY m.name ASC;
        """

        cur.execute(query, (user_id,))
        rows = cur.fetchall()
        
        return jsonify(rows)

    except Exception as e:
        print(f"GET MY METROS ERROR: {e}")
        return jsonify({"error": str(e)}), 500

    finally:
        if conn:
            conn.close()

# ---------------------------------------------------------
# Endpoint: Create/Upsert Metroplex from Google Places
# ---------------------------------------------------------
@require_firebase_auth
@bp.route("/metro", methods=["POST"])
def create_metro():
    try:
        verify_and_get_user_id(request)
    except Exception as e:
        return jsonify({"error": str(e)}), 401

    data = request.json
    place_id = data.get("placeId")
    name = data.get("name")
    lat = data.get("lat")
    lng = data.get("lng")

    if not all([place_id, name, lat, lng]):
        return jsonify({"error": "Missing required fields: placeId, name, lat, lng"}), 400

    conn = None
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # Upsert metroplexes
        state_names = data.get("stateNames", []) or ["Unknown"]
        cur.execute("""
            INSERT INTO metroplexes (id, name, metropolitan, state_names)
            VALUES (%s, %s, true, %s::text[])
            ON CONFLICT (id) DO NOTHING
        """, (place_id, name, state_names))

        # Upsert metroplex_centers
        cur.execute("""
            INSERT INTO metroplex_centers (metro_id, name, intptlat, intptlong)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (metro_id) DO NOTHING
        """, (place_id, name, float(lat), float(lng)))

        conn.commit()

        return jsonify({"status": "success", "metro_id": place_id}), 201

    except Exception as e:
        print(f"CREATE METRO ERROR: {e}")
        if conn:
            conn.rollback()
        return jsonify({"error": str(e)}), 500

    finally:
        if conn:
            conn.close()            