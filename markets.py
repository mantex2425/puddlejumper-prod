import json
import uuid
from flask import Blueprint, request, jsonify
from typing import Dict, Any, List, Tuple, Optional
from psycopg2.extras import RealDictCursor
# REMOVED: import h3 

from utils import verify_and_get_user_id, require_firebase_auth
from db import get_db_connection as get_db

markets_bp = Blueprint("markets", __name__, url_prefix="/api/v1")


# ----------------------------------------------------------------------
# Internal helpers
# ----------------------------------------------------------------------

def _get_default_settings(uid: str) -> Dict[str, Any]:
    """Provides a guaranteed full dictionary structure for a new driver."""
    return {
        "driver_id": uid,
        "redZones": [],
        "markets": [],
        "costPerMile": 0.15,
        "opportunityMultiplier": 1.0 
    }

def _load_settings(conn, uid: str) -> Dict[str, Any]:
    """Loads settings from JSONB column, applying defaults for missing keys."""
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        """
        SELECT settings 
        FROM app_private.driver_settings_new 
        WHERE driver_id = %s
        """,
        (uid,),
    )
    row = cur.fetchone()
    cur.close()

    if not row:
        return _get_default_settings(uid)

    settings_data = row.get("settings") 
    
    # Handle both stringified JSON and direct dict (psycopg2 adapter behavior)
    if isinstance(settings_data, str):
        settings = json.loads(settings_data)
    else:
        settings = settings_data

    # Ensure mandatory keys are present using the defaults
    defaults = _get_default_settings(uid)
    for key, default_value in defaults.items():
        settings.setdefault(key, default_value)

    return settings


def _save_settings(conn, uid: str, settings: Dict[str, Any]) -> None:
    """Saves the settings dictionary back to the JSONB column."""
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO app_private.driver_settings_new (driver_id, settings, last_updated)
        VALUES (%s, %s::jsonb, now())
        ON CONFLICT (driver_id) DO UPDATE
            SET settings = EXCLUDED.settings,
                last_updated = now();
        """,
        (uid, json.dumps(settings)),
    )
    conn.commit()
    cur.close()


def _find_market_by_id(markets: List[Dict[str, Any]], target_id: str) -> Tuple[Optional[int], Optional[Dict[str, Any]]]:
    """Returns the index and market object by matching UUID string."""
    for idx, m in enumerate(markets):
        if str(m.get("id")) == str(target_id):
            return idx, m
    return None, None


# ----------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------

@require_firebase_auth
@markets_bp.route("/markets-with-active", methods=["GET"])
def get_markets_with_active():
    """
    Gets all markets (which now have stored UUIDs) and identifies 
    the active market by matching the ID in driver_active_market.
    """
    try:
        uid = verify_and_get_user_id(request)
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # Efficient SQL: Builds the response object inside the DB
        # Compares the stored JSON 'id' against the 'active_market_id' column
        cur.execute("""
            SELECT jsonb_build_object(
                'activeMarket',
                    (
                        SELECT elem
                        FROM jsonb_array_elements(ds.settings->'markets') elem
                        WHERE (elem->>'id') = am.active_market_id 
                    ),
                'markets', ds.settings->'markets',
                'globalRedZones', ds.settings->'redZones'
            ) AS result
            FROM app_private.driver_settings_new ds
            LEFT JOIN app_private.driver_active_market am
                ON am.driver_id = ds.driver_id
            WHERE ds.driver_id = %s
        """, (uid,))

        row = cur.fetchone()
        
        # If no settings exist yet, return clean defaults
        if row is None or row.get("result") is None:
            defaults = _get_default_settings(uid)
            return jsonify({
                "activeMarket": None,
                "markets": [],
                "globalRedZones": defaults.get('redZones', [])
            })
            
        return jsonify(row["result"])

    except Exception as e:
        print(f"GET /markets-with-active error: {e}")
        return jsonify({'error': str(e)}), 500
    finally:
        if 'conn' in locals():
            conn.close()


@require_firebase_auth
@markets_bp.route("/markets", methods=["POST"])
def create_market():
    """Create a new market, assign a persistent UUID, and set as active."""
    conn = get_db()
    try:
        uid = verify_and_get_user_id(request)
        data = request.json
        
        if 'name' not in data:
            return jsonify({'error': 'name is required'}), 400
        
        if 'metroplexId' not in data:
            return jsonify({"error": "'metroplexId' is required."}), 400

        settings = _load_settings(conn, uid)

        # Check if market name already exists (optional but good UX)
        existing_names = [m.get('name') for m in settings.get('markets', [])]
        if data['name'] in existing_names:
            return jsonify({'error': 'Market with this name already exists'}), 400
        
        # Generate a permanent UUID
        new_id = str(uuid.uuid4())

        new_market = {
            'id': new_id,
            'name': data['name'],
            'metroplexId': data.get('metroplexId'),
            'focalPoint': data.get('focalPoint', {'lat': None, 'lng': None}),
            'greenZones': data.get('greenZones', []),
        }
        
        settings['markets'].append(new_market)
        _save_settings(conn, uid, settings)
        
        # Set as Active Market using the UUID
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO app_private.driver_active_market (driver_id, active_market_id)
            VALUES (%s, %s)
            ON CONFLICT (driver_id) DO UPDATE
              SET active_market_id = EXCLUDED.active_market_id,
                  last_updated = now()
        """, (uid, new_id))
        
        conn.commit()
        
        return jsonify(new_market), 201
    
    except Exception as e:
        print(f"POST /markets error: {e}")
        return jsonify({'error': str(e)}), 500
    finally:
        if 'conn' in locals():
            conn.close()


@require_firebase_auth
@markets_bp.route("/markets/<string:market_id>/rename", methods=["PUT"])
def rename_market(market_id):
    """Renames a market identified by its UUID."""
    uid = verify_and_get_user_id(request)
    data = request.get_json()
    new_name = data.get('name')

    if not new_name:
        return jsonify({'error': 'New name is required'}), 400

    conn = get_db()
    try:
        settings = _load_settings(conn, uid)
        markets = settings.get("markets", [])
        
        # Find market by UUID
        idx, existing = _find_market_by_id(markets, market_id)
        
        if existing is None:
            return jsonify({'error': 'Market not found'}), 404

        # Validate name uniqueness (excluding self)
        for m in markets:
            if m.get('name', '').lower() == new_name.lower() and str(m.get('id')) != str(market_id):
                 return jsonify({'error': 'A market with this name already exists'}), 400

        # Update the name
        markets[idx]['name'] = new_name
        settings['markets'] = markets
        
        _save_settings(conn, uid, settings)

        # No need to update active_market_id table because ID didn't change!
        
        return jsonify({
            "success": True, 
            "id": market_id, 
            "newName": new_name
        })

    except Exception as e:
        print(f"PUT /markets/rename error: {e}")
        return jsonify({'error': str(e)}), 500
    finally:
        if 'conn' in locals():
            conn.close()


@require_firebase_auth
@markets_bp.route("/markets/<string:market_id>", methods=["DELETE"])
def delete_market(market_id):
    """Delete an existing market by UUID."""
    uid = verify_and_get_user_id(request)
    conn = get_db()
    try:
        settings = _load_settings(conn, uid)
        markets = settings.get("markets", [])

        idx, existing = _find_market_by_id(markets, market_id)
        if existing is None:
            return jsonify({"error": "not_found"}), 404

        # 1. Remove from the JSON list in settings
        markets.pop(idx)
        settings["markets"] = markets
        _save_settings(conn, uid, settings)

        # 2. Fix the Active Market Pointer
        # If the deleted market was the active one, we must DELETE the row entirely.
        # (We cannot set it to NULL because of the table constraint)
        cur = conn.cursor()
        cur.execute("""
            DELETE FROM app_private.driver_active_market 
            WHERE driver_id = %s AND active_market_id = %s
        """, (uid, market_id))
        conn.commit()
        cur.close()

        return jsonify({"status": "deleted"}), 200
    except Exception as e:
        print(f"DELETE /markets error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        if 'conn' in locals():
            conn.close()


@require_firebase_auth
@markets_bp.route("/markets/<string:market_id>", methods=["PUT"])
def update_market_details(market_id):
    """Update market details (focalPoint, greenZones) by UUID."""
    uid = verify_and_get_user_id(request)
    payload = request.get_json(silent=True)

    if not payload:
        return jsonify({"error": "invalid body"}), 400

    conn = get_db()
    try:
        settings = _load_settings(conn, uid)
        markets = settings.get("markets", [])

        idx, existing = _find_market_by_id(markets, market_id)
        if existing is None:
            return jsonify({"error": "not_found"}), 404

        # Update fields if provided
        existing["focalPoint"] = payload.get("focalPoint", existing.get("focalPoint"))
        existing["greenZones"] = payload.get("greenZones", existing.get("greenZones", []))
        
        # NOTE: This endpoint deliberately does NOT update 'name' to separate concerns
        
        markets[idx] = existing
        settings["markets"] = markets

        _save_settings(conn, uid, settings)

        return jsonify(existing), 200
    except Exception as e:
        print(f"PUT /markets/update error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        if 'conn' in locals():
            conn.close()

# ----------------------------------------------------------------------
# Legacy Route Support
# ----------------------------------------------------------------------

@require_firebase_auth
@markets_bp.route("/markets", methods=["GET"])
def get_markets_legacy():
    """Legacy endpoint: Get all markets."""
    conn = get_db()
    try:
        uid = verify_and_get_user_id(request)
        settings = _load_settings(conn, uid)
        return jsonify({'markets': settings.get('markets', [])})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        if 'conn' in locals(): conn.close()

@require_firebase_auth
@markets_bp.route("/proximity-check", methods=["POST"])
def check_proximity():
      try:
          uid = verify_and_get_user_id(request)
          data = request.json

          lat = data.get('lat')
          lng = data.get('lng')

          if lat is None or lng is None:
              return jsonify({'error': 'lat and lng are required'}), 400

          conn = get_db()
          cur = conn.cursor(cursor_factory=RealDictCursor)

          # This query uses the Postgres H3 extension, NOT Python H3
          query = """
          WITH driver_data AS (
              SELECT am.active_market_id, ds.settings
              FROM app_private.driver_active_market am
              JOIN app_private.driver_settings_new ds ON am.driver_id = ds.driver_id
              WHERE am.driver_id = %s
          ),
          active_market_obj AS (
              SELECT elem FROM driver_data, jsonb_array_elements(settings->'markets') elem
              WHERE elem->>'id' = active_market_id
          ),
          active_green_hexes AS (
              SELECT jsonb_array_elements_text(elem->'greenZones')::public.h3index as hex
              FROM active_market_obj
          ),
          user_location AS (
              SELECT h3_latlng_to_cell(point(%s, %s), 8) as current_cell
          ),
          nearest_green AS (
              SELECT 
                  hex,
                  h3_grid_distance((SELECT current_cell FROM user_location), hex) as dist
              FROM active_green_hexes
              ORDER BY dist ASC
              LIMIT 1
          )
          SELECT 
              EXISTS (
                  SELECT 1 FROM active_green_hexes 
                  WHERE hex = (SELECT current_cell FROM user_location)
              ) as is_in_active_green,
              COALESCE((SELECT dist FROM nearest_green), 999) as hex_distance,
              (SELECT (h3_cell_to_latlng(hex))[1] FROM nearest_green) as nearest_green_lat,
              (SELECT (h3_cell_to_latlng(hex))[0] FROM nearest_green) as nearest_green_lng
          """

          cur.execute(query, (uid, float(lng), float(lat)))
          result = cur.fetchone()

          return jsonify({
              "isInActiveGreen": result["is_in_active_green"],
              "hexDistance": result["hex_distance"],
              "nearestGreenLat": result["nearest_green_lat"],
              "nearestGreenLng": result["nearest_green_lng"]
          })

      except Exception as e:
          print(f"POST /proximity-check error: {e}")
          return jsonify({'error': str(e)}), 500
      finally:
          if 'conn' in locals():
              conn.close()