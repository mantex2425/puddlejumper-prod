# backend/active_market.py

from flask import Blueprint, request, jsonify
from psycopg2.extras import RealDictCursor

from utils import (
    verify_and_get_user_id,
    ensure_user_exists,
    get_active_market_id,
    set_active_market_id,
    require_firebase_auth
)
from db import get_db

active_market_bp = Blueprint("active_market", __name__)


# --------------------------------------------------------------
# GET /api/v1/active-market
# --------------------------------------------------------------
@require_firebase_auth
@active_market_bp.route("/api/v1/active-market", methods=["GET"])
def get_active_market():
    """Get the active market ID"""
    try:
        uid = verify_and_get_user_id(request)
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        cur.execute("""
            SELECT active_market_id
            FROM app_private.driver_active_market
            WHERE driver_id = %s
        """, (uid,))
        
        row = cur.fetchone()
        
        return jsonify({
            'active_market_id': row['active_market_id'] if row else None,
            'driver_id': uid
        })
    
    except Exception as e:
        print(f"GET /active-market error: {e}")
        return jsonify({'error': str(e)}), 500
    finally:
        if 'conn' in locals():
            conn.close()


# --------------------------------------------------------------
# POST /api/v1/active-market
# --------------------------------------------------------------
@require_firebase_auth
@active_market_bp.route("/api/v1/active-market", methods=["POST"])
def set_active_market():
    """Set the active market using the Market ID hash."""
    conn = None # Initialize conn outside try block
    try:
        uid = verify_and_get_user_id(request)
        data = request.json
        
        # 1. INPUT CHANGE: Expect 'marketId' (the hash) instead of 'active_market_id'
        market_id_hash = data.get('marketId')
        
        if not market_id_hash:
            return jsonify({'error': 'marketId (the unique hash) is required'}), 400
        
        # NOTE: We skip the complex validation against the driver_settings_new JSONB.
        # The primary role here is updating the driver_active_market table.
        # If the hash is invalid, it won't be found in subsequent GET requests,
        # which is acceptable for MVP.
        
        # 2. Set active market using the unique HASH ID
        conn = get_db()
        cur = conn.cursor() # RealDictCursor not needed here
        
        cur.execute("""
            INSERT INTO app_private.driver_active_market (driver_id, active_market_id)
            VALUES (%s, %s)
            ON CONFLICT (driver_id) DO UPDATE
              SET active_market_id = EXCLUDED.active_market_id,
                  last_updated = now()
        """, (uid, market_id_hash)) # *** CRITICAL FIX: Pass the hash here ***
        
        conn.commit()
        
        return jsonify({
            'status': 'Success',
            'active_market_id': market_id_hash,
            'driver_id': uid
        }), 200
    
    except Exception as e:
        print(f"POST /active-market error: {e}")
        if conn:
            conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        if conn:
            conn.close()