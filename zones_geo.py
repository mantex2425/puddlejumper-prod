# --- PUDDLEJUMPER BACKEND v4.7 (zones_geo.py - SQL H3 Geometry) ---
# Endpoints for fetching polygon geometry & market zones

from flask import Blueprint, jsonify, request, g
from db import get_db_connection
from utils import (
    get_active_market_config,
    verify_and_get_user_id,
    ensure_user_exists,
    resolve_market_id,
    require_firebase_auth,   # attaches g.user
)

zones_geo_bp = Blueprint("zones_geo", __name__)


# ---------------------------------------------------------------------
# SHARED SQL: Convert hex → boundary using your working DB function
# ---------------------------------------------------------------------
HEX_TO_POLYGON_SQL = """
    SELECT
        hex_key,
        h3_boundary_to_json(hex_key) AS boundary
    FROM UNNEST(%s::text[]) AS hex_key;
"""

print("### LOADED zones_geo.py FROM:", __file__)

# ---------------------------------------------------------------------
# GET /api/v1/zones/geometry
# ---------------------------------------------------------------------
@require_firebase_auth
@zones_geo_bp.route('/zones/geometry', methods=['GET'])
def get_zones_geometry():
    uid = g.user.get("uid")
    if not uid:
        return jsonify({"error": "Unauthorized"}), 401

    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # Set per-request user-id for RLS / logging
        cur.execute("SELECT set_config('app.current_user_id', %s, false);", (uid,))
        ensure_user_exists(cur, uid)

        # resolve market
        market_id = resolve_market_id(cur, uid, request.args.get("market_id"))

        cur.execute("""
            SELECT settings
            FROM app_private.driver_settings_new
            WHERE driver_id = %s;
        """, (uid,))
        row = cur.fetchone()
        if not row:
            return jsonify({"error": "No driver settings found"}), 404

        settings = row[0]

        markets = settings.get("markets", [])
        active = next((m for m in markets if m.get("metroplexId") == market_id), None)
        if not active:
            return jsonify({"error": f"Market {market_id} not found"}), 404

        focal_lat = active.get("focalPoint", {}).get("lat")
        focal_lng = active.get("focalPoint", {}).get("lng")
        if focal_lat is None or focal_lng is None:
            return jsonify({"error": "Market focalPoint missing lat/lng"}), 500

        # Compute L7 market hex
        cur.execute("""
            SELECT h3_latlng_to_cell(POINT(%s, %s), 7) AS hex_l7;
        """, (focal_lng, focal_lat))
        row = cur.fetchone()
        market_hex_l7 = row[0]

        # Market boundary polygon
        cur.execute(HEX_TO_POLYGON_SQL, ([market_hex_l7],))
        hex_key, boundary_coords = cur.fetchone()

        market_boundary_feature = {
            "type": "Feature",
            "properties": {
                "hex_key": market_hex_l7,
                "market_id": market_id,
                "zone_type": "market_boundary"
            },
            "geometry": {
                "type": "Polygon",
                "coordinates": [boundary_coords]
            },
        }

        # -------------------------------------------------------------
        # RED ZONES (global)
        # -------------------------------------------------------------
        red_zones = settings.get("redZones", [])
        if not isinstance(red_zones, list):
            red_zones = []

        def build_collection(hex_list, zone_type, prefix):
            if not hex_list:
                return {"type": "FeatureCollection", "features": []}

            cur.execute(HEX_TO_POLYGON_SQL, (hex_list,))
            rows = cur.fetchall()

            features = []
            for idx, (hk, coords) in enumerate(rows):
                if coords is None:
                    continue
                features.append({
                    "type": "Feature",
                    "properties": {
                        "hex_key": hk,
                        "zone_type": zone_type,
                        "zone_id": f"{prefix}-{idx}",
                    },
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [coords],
                    },
                })
            return {"type": "FeatureCollection", "features": features}

        red_collection = build_collection(red_zones, "red", "R")

        return jsonify({
            "market_boundary": market_boundary_feature,
            "red_zones": red_collection,
        }), 200

    except Exception as e:
        print("GEOMETRY API ERROR:", e)
        return jsonify({"error": f"Failed to retrieve GeoJSON: {e}"}), 500

    finally:
        if conn:
            conn.close()


# ---------------------------------------------------------------------
# POST /api/v1/zones/hexes_to_polygons
# ---------------------------------------------------------------------
@require_firebase_auth
@zones_geo_bp.route('/zones/hexes_to_polygons', methods=['POST'])
def hexes_to_polygons():
    uid = verify_and_get_user_id(request)
    if not uid:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    hex_keys = data.get("hex_keys", [])

    # We now know we can remove the print statements that were fouling the output
    # print("### hexes_to_polygons() EXECUTED") 
    # print("### incoming hex_keys =", hex_keys)

    if not isinstance(hex_keys, list):
        return jsonify({"error": "Invalid input: 'hex_keys' must be a list"}), 400
    if not hex_keys:
        return jsonify({"hex_polygons": []}), 200
    if len(hex_keys) > 1000:
        return jsonify({"error": "Too many hex keys (limit = 1000)"}), 400

    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Note: We rely on conn.cursor() returning a RealDictCursor here.

        cur.execute("""
            SELECT
                hex_key,
                h3_boundary_to_json(hex_key) AS boundary
            FROM UNNEST(%s::text[]) AS hex_key;
        """, (hex_keys,))

        rows = cur.fetchall()

    except Exception as e:
        print(f"HEXES_TO_POLYGON ERROR: {e}")
        return jsonify({"error": f"Database failure: {e}"}), 500

    finally:
        if conn:
            conn.close()

    # ✅ FINAL FIX: Accessing columns by name, which the RealDictCursor supports.
    return jsonify({
        "hex_polygons": [
            {
                "hex_key": row["hex_key"],
                "boundary": [
                    # Reverse the list comprehension output: [coord[1], coord[0]]
                    [coord[1], coord[0]] for coord in row["boundary"]
                ]
            }
            for row in rows
        ]
    }), 200

# ---------------------------------------------------------------------
# GET /api/v1/zones/market_grid
# ---------------------------------------------------------------------
@require_firebase_auth
@zones_geo_bp.route('/zones/market_grid', methods=['GET'])
def get_market_grid():
    uid = g.user.get("uid")
    if not uid:
        return jsonify({"error": "Unauthorized"}), 401

    market_hex = request.args.get("market_hex")
    if not market_hex:
        return jsonify({"error": "Missing 'market_hex' parameter"}), 400

    GRID_DISK_RADIUS = 3

    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # Get grid L7 hexes
        cur.execute("""
            SELECT h3index_out(index) AS hex_key
            FROM h3_grid_disk_distances(%s::h3index, %s::integer);
        """, (market_hex, GRID_DISK_RADIUS))

        rows = cur.fetchall()
        hex_keys = [r[0] for r in rows if r and r[0]]

        if not hex_keys:
            return jsonify({
                "market_hex": market_hex,
                "hex_count": 0,
                "grid_hexes": [],
            }), 200

        # Now get polygons for all grid hexes
        cur.execute(HEX_TO_POLYGON_SQL, (hex_keys,))
        boundary_rows = cur.fetchall()

    except Exception as e:
        print("MARKET_GRID ERROR:", e)
        return jsonify({"error": f"Grid generation failed: {e}"}), 500

    finally:
        if conn:
            conn.close()

    out = [
        {"hex_key": hk, "boundary": boundary}
        for hk, boundary in boundary_rows
        if hk and boundary
    ]

    return jsonify({
        "market_hex": market_hex,
        "hex_count": len(out),
        "grid_hexes": out,
    }), 200
