# backend/h3_utils.py

from flask import Blueprint, request, jsonify
from utils import require_firebase_auth
import h3

bp = Blueprint("h3_utils", __name__)


@require_firebase_auth
@bp.get("/cluster_from_latlng")
def cluster_from_latlng():
    """
    Returns H3 cluster for a lat/lng with optional ring size.
    Example:
      /api/v1/h3/cluster_from_latlng?lat=29.76&lng=-95.36&ring=1
    """
    try:
        lat = float(request.args.get("lat"))
        lng = float(request.args.get("lng"))
        ring = int(request.args.get("ring", 1))

        # Resolution used in your other tables
        res = 8

        center_h3 = h3.latlng_to_cell(lat, lng, res)
        cluster = list(h3.grid_ring(center_h3, ring))
        cluster.append(center_h3)

        return jsonify({
            "center_h3": center_h3,
            "cluster": cluster,
            "ring": ring,
            "resolution": res
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@require_firebase_auth
@bp.get("/cluster_polygons")
def cluster_polygons():
    """
    Unified endpoint to return the 7 H3 keys in a cluster and immediately convert 
    all H3 keys into a JSON list of their Polygon boundaries.
    Input: ?lat=[double]&lng=[double]&ring=1
    Output: JSON object containing 'hex_polygons' list.
    """
    # Remove debug line in final code
    # print(f"DEBUG: Cluster Polygon Request Args: {request.args}") 
    
    try:
        # 1. Read Input
        lat = float(request.args.get("lat"))
        lng = float(request.args.get("lng"))
        ring = int(request.args.get("ring", 1)) 

        # Resolution used in your other tables (Level 8)
        res = 8

        # 2. H3 Cluster Generation
        center_h3 = h3.latlng_to_cell(lat, lng, res)
        cluster_set = set(h3.grid_ring(center_h3, ring))
        cluster_set.add(center_h3)
        
        final_hex_polygons = []

        # 3. Polygon Conversion & 4. Format Output
        for hex_key in cluster_set:
            # h3.cell_to_boundary returns a list of points in [LAT, LON] order (e.g., [[lat1, lon1], ...])
            boundary = h3.cell_to_boundary(hex_key)
            
            final_hex_polygons.append({
                "hex_key": hex_key, 
                # ⚠️ CRITICAL FIX: Swap order to GeoJSON standard [LON, LAT]
                "boundary": [
                    [point[1], point[0]] for point in boundary
                ]
            })

        # 5. Return JSON
        return jsonify({
            "success": True,
            "center_key": center_h3,
            "h3_resolution": res,
            "hex_polygons": final_hex_polygons
        })

    except ValueError as e:
        return jsonify({
            "success": False, 
            "error": "The provided 'lat', 'lng', or 'ring' values are not valid numbers."
        }), 400
    
    except Exception as e:
        print(f"Cluster Polygon General Error: {e}")
        return jsonify({"success": False, "error": f"Internal server error: {str(e)}"}), 500

@bp.get("/starter_market")
@require_firebase_auth
def starter_market():
    """
    Generate a filled hex disk for a new user's starter market.
    /api/v1/h3/starter_market?lat=29.58&lng=-95.53&ring=3
    Returns 37 contiguous hexes (ring=3) as a filled hexagon.
    """
    lat = float(request.args.get("lat"))
    lng = float(request.args.get("lng"))
    ring = int(request.args.get("ring", 3))
    
    center_h3 = h3.latlng_to_cell(lat, lng, 8)
    filled = list(h3.grid_disk(center_h3, ring))
    
    return jsonify({
        "center_h3": center_h3,
        "hexes": filled,
        "count": len(filled),
        "ring": ring,
        "resolution": 8
    })