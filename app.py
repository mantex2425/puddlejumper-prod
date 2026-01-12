import os
import json
import re
import logging
import sys
from flask import Flask, request, jsonify, Response
from psycopg2.extras import RealDictCursor

app = Flask(__name__)

# --------------------------------------------------------------
# Imports and Blueprints
# --------------------------------------------------------------
from markets import markets_bp
from unified_search import bp as unified_search_bp
from db import get_db
from h3_utils import bp as h3_utils_bp
from geo import bp as geo_bp
from zones_geo import zones_geo_bp
from active_market import active_market_bp
from auth import auth_bp
from superpower_geo import superpower_geo_bp
from decisions import decisions_bp
from chat_ai import chat_ai_bp
from puddles_brain import puddles_bp

# Firebase init
from firebase import get_firebase_app
get_firebase_app()

# Auth helpers
from utils import (
    require_firebase_auth,
    verify_and_get_user_id,
    ensure_user_exists
)

# --------------------------------------------------------------
# Logging configuration (must come after import logging)
# --------------------------------------------------------------
logging.basicConfig(
    level=logging.DEBUG,
    stream=sys.stdout,
    format='%(asctime)s - %(levelname)s - %(name)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Debug info at startup (useful for Cloud Run / Docker debugging)
logger.debug("Python version: %s", sys.version)
logger.debug("Current working dir: %s", os.getcwd())
logger.debug("PYTHONPATH: %s", os.environ.get("PYTHONPATH", "not set"))
logger.debug("PORT env: %s", os.environ.get("PORT", "not set!!!"))
logger.debug("All env vars: %s", dict(os.environ))

# --------------------------------------------------------------
# Register Blueprints
# --------------------------------------------------------------
app.register_blueprint(markets_bp)
app.register_blueprint(unified_search_bp, url_prefix="/api/v1")
app.register_blueprint(h3_utils_bp, url_prefix="/api/v1")
app.register_blueprint(geo_bp, url_prefix="/api/v1")
app.register_blueprint(zones_geo_bp, url_prefix="/api/v1")
app.register_blueprint(active_market_bp, url_prefix="")
app.register_blueprint(auth_bp)
app.register_blueprint(chat_ai_bp, url_prefix='/api/v1')
app.register_blueprint(decisions_bp, url_prefix='/api/v1/decisions')
app.register_blueprint(superpower_geo_bp, url_prefix="/api/v1")
app.register_blueprint(puddles_bp, url_prefix="/api/v1")

# --------------------------------------------------------------
# Global Auth Middleware (skip public routes)
# --------------------------------------------------------------
@app.before_request
def require_auth_globally():
    # UPDATED: Added Gevulot Protocol public paths
    public_paths = {
        "/",
        "/api/v1/health",
        "/.well-known/apple-app-site-association",
    }
    
    # Check for exact matches or prefix matches (for handshake URLs)
    if request.path in public_paths or request.path.startswith("/h/"):
        return None

    try:
        verify_and_get_user_id(request)
    except Exception as e:
        return jsonify({"error": "Unauthorized", "details": str(e)}), 401

# --------------------------------------------------------------
# Gevulot Protocol / App Clip Support
# --------------------------------------------------------------
@app.route('/.well-known/apple-app-site-association')
def serve_aasa():
    """
    Mandatory handshake file for iOS App Clips.
    Must be served with application/json mimetype.
    """
    aasa_content = {
        "appclips": {
            "apps": ["9D6CPTUV83.com.abruce.puddles.Clip"]
        },
        "applinks": {
            "details": [
                {
                    "appIDs": ["9D6CPTUV83.com.abruce.puddles"],
                    "components": [{"/": "/h/*"}]
                }
            ]
        }
    }
    return Response(
        json.dumps(aasa_content), 
        mimetype='application/json'
    )

@app.route('/h/<handshake_id>')
def handshake_landing(handshake_id):
    """
    The landing point for the Gevulot Handshake.
    Richard's phone hits this when scanning the QR.
    """
    return jsonify({
        "status": "active",
        "handshake_id": handshake_id,
        "protocol": "gevulot_v1"
    }), 200

# --------------------------------------------------------------
# Root (public)
# --------------------------------------------------------------
@app.route("/")
def root():
    return jsonify({"status": "ok", "message": "PuddleJumper backend running"})

@app.route('/api/v1/debug/routes', methods=['GET'])
@require_firebase_auth
def list_routes():
    import urllib.parse
    output = []
    
    # Sort rules by URL path for easier reading
    rules = sorted(app.url_map.iter_rules(), key=lambda x: str(x))
    
    for rule in rules:
        # Ignore the default static route and internal Flask routes
        if rule.endpoint == 'static':
            continue
            
        methods = [m for m in rule.methods if m not in ['OPTIONS', 'HEAD']]
        url = urllib.parse.unquote(str(rule))
        
        output.append({
            "endpoint": rule.endpoint,
            "methods": methods,
            "url": url
        })
        
    return jsonify({
        "total_routes": len(output),
        "status": "authenticated",
        "routes": output
    })

# --------------------------------------------------------------
# GET /api/v1/preferences (secured)
# --------------------------------------------------------------
@app.route("/api/v1/preferences", methods=["GET"])
@require_firebase_auth
def get_preferences():
    try:
        uid = verify_and_get_user_id(request)
    except Exception as e:
        return jsonify({"error": str(e)}), 403

    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        ensure_user_exists(cur, uid)
        conn.commit()

        cur.execute("""
            SELECT settings
            FROM app_private.driver_settings_new
            WHERE driver_id = %s
        """, (uid,))
        row = cur.fetchone()

        if row is None:
            # Default response if no settings found
            return jsonify({
                "driver_id": uid,
                "setup_completed": False,
                "car_type": "gas",
                "weekly_rental_cost": None,
                "weekly_insurance_cost": None,
                "weekly_toll_fees": 0.0,
                "estimated_weekly_hours": 40.0,
                "cost_per_mile": 0.67,
                "deadhead_cost_per_hour": 17.5,
                "min_net_profit_per_ride": 8.0,
                "min_effective_dollar_per_mile": 1.8,
                "min_effective_hourly_rate": 22.0,
                "auto_accept_hourly_rate": 30.0,
                "max_pickup_miles": 4.0,
                "max_pickup_miles_surge": 5.0,
                "surge_multiplier_threshold": 1.5,
                "opportunity_multiplier": 1.0,
                "redZones": [],
                "markets": []
            })

        settings = row["settings"]
        # Ensure driver_id and default fields are present
        settings.setdefault("driver_id", uid)
        settings.setdefault("setup_completed", False)
        settings.setdefault("car_type", "gas")
        
        return jsonify(settings)

    except Exception as e:
        print("PREFERENCES GET ERROR:", str(e))
        return jsonify({"error": str(e)}), 500
    finally:
        if "conn" in locals():
            conn.close()

# --------------------------------------------------------------
# Validation helper
# --------------------------------------------------------------
def validate_preferences_schema(data):
    if not isinstance(data, dict):
        raise ValueError("Root must be an object")

    required_top_level = [
        "cost_per_mile", "deadhead_cost_per_hour", "min_net_profit_per_ride",
        "min_effective_dollar_per_mile", "min_effective_hourly_rate",
        "auto_accept_hourly_rate", "max_pickup_miles", "max_pickup_miles_surge",
        "surge_multiplier_threshold", "opportunity_multiplier"
    ]
    for key in required_top_level:
        if key not in data or not isinstance(data[key], (int, float)):
            raise ValueError(f"{key} must be a number")

    if "markets" not in data or not isinstance(data["markets"], list):
        raise ValueError("markets must be a list")

    if "redZones" not in data or not isinstance(data["redZones"], list):
        raise ValueError("redZones must be a list")

    return True

# --------------------------------------------------------------
# POST/PUT /api/v1/preferences (secured)
# --------------------------------------------------------------
@app.route("/api/v1/preferences", methods=["POST", "PUT"])
@require_firebase_auth
def save_preferences():
    try:
        uid = verify_and_get_user_id(request)
    except Exception as e:
        return jsonify({"error": str(e)}), 403

    try:
        data = request.json
        validate_preferences_schema(data)
        data["driver_id"] = uid

        conn = get_db()
        cur = conn.cursor()

        cur.execute("""
            INSERT INTO app_private.driver_settings_new (driver_id, settings)
            VALUES (%s, %s::jsonb)
            ON CONFLICT (driver_id) DO UPDATE
                SET settings = EXCLUDED.settings,
                    last_updated = now()
        """, (uid, json.dumps(data)))

        conn.commit()

        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT settings FROM app_private.driver_settings_new WHERE driver_id = %s", (uid,))
        post_write = cur.fetchone()

        return jsonify(post_write["settings"] if post_write else {})

    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if "conn" in locals():
            conn.close()

# --------------------------------------------------------------
# Other routes
# --------------------------------------------------------------
@app.route("/api/v1/health", methods=["GET"])
def health_check():
    return {"status": "ok", "database": os.getenv("DB_NAME", "unknown")}, 200

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=True)