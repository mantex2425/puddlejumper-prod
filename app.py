import os
import json
import logging
import sys
from flask import Flask, request, jsonify, Response

# --------------------------------------------------------------
# 1. Light Boot Strategy
# --------------------------------------------------------------
# We keep the global scope extremely light to ensure instant 
# container startup and fast health checks.
app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stdout,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# --------------------------------------------------------------
# 2. Deferred Blueprint Registration
# --------------------------------------------------------------
# Registering all blueprints at boot is fine, but we lazy-load 
# the logic inside them by using local imports in the blueprints themselves.
from markets import markets_bp
from unified_search import bp as unified_search_bp
from h3_utils import bp as h3_utils_bp
from geo import bp as geo_bp
from zones_geo import zones_geo_bp
from active_market import active_market_bp
from auth import auth_bp
from superpower_geo import superpower_geo_bp
from decisions import decisions_bp
from chat_ai import chat_ai_bp
from puddles_brain import puddles_bp

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
# 3. Global Auth Middleware (Optimized)
# --------------------------------------------------------------
@app.before_request
def require_auth_globally():
    public_paths = {
        "/",
        "/api/v1/health",
        "/.well-known/apple-app-site-association",
    }
    
    if request.path in public_paths or request.path.startswith("/h/"):
        return None

    # Lazy load auth logic only for secured requests
    from utils import verify_and_get_user_id
    try:
        verify_and_get_user_id(request)
    except Exception as e:
        return jsonify({"error": "Unauthorized", "details": str(e)}), 401

# --------------------------------------------------------------
# 4. Public Endpoints (Instant Response)
# --------------------------------------------------------------
@app.route("/")
def root():
    return jsonify({"status": "ok", "message": "PuddleJumper online"})

@app.route("/api/v1/health", methods=["GET"])
def health_check():
    # This route is now critical for Cloud Run's Startup CPU Boost
    return {"status": "ok", "database": os.getenv("DB_NAME", "unknown")}, 200

@app.route('/.well-known/apple-app-site-association')
def serve_aasa():
    aasa_content = {
        "appclips": {"apps": ["9D6CPTUV83.com.abruce.puddles.Clip"]},
        "applinks": {
            "details": [{"appIDs": ["9D6CPTUV83.com.abruce.puddles"], "components": [{"/": "/h/*"}]}]
        }
    }
    return Response(json.dumps(aasa_content), mimetype='application/json')

@app.route('/h/<handshake_id>')
def handshake_landing(handshake_id):
    return jsonify({"status": "active", "handshake_id": handshake_id, "protocol": "gevulot_v1"}), 200

# --------------------------------------------------------------
# 5. Data Routes (Lazy Loading Dependencies)
# --------------------------------------------------------------
@app.route("/api/v1/preferences", methods=["GET"])
def get_preferences():
    # Load heavy database and firebase modules only when this route is hit
    from firebase import get_firebase_app
    from db import get_db
    from psycopg2.extras import RealDictCursor
    from utils import verify_and_get_user_id, ensure_user_exists
    
    get_firebase_app()
    
    try:
        uid = verify_and_get_user_id(request)
        conn = get_db()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            ensure_user_exists(cur, uid)
            conn.commit()

            cur.execute("SELECT settings FROM app_private.driver_settings_new WHERE driver_id = %s", (uid,))
            row = cur.fetchone()

            if row is None:
                return jsonify({"driver_id": uid, "setup_completed": False, "car_type": "gas", "markets": [], "redZones": []})

            settings = row["settings"]
            settings.setdefault("driver_id", uid)
            return jsonify(settings)

    except Exception as e:
        logger.error("PREFERENCES GET ERROR: %s", str(e))
        return jsonify({"error": str(e)}), 500
    finally:
        if "conn" in locals(): conn.close()

@app.route("/api/v1/preferences", methods=["POST", "PUT"])
def save_preferences():
    # Standard light imports
    from db import get_db
    from psycopg2.extras import RealDictCursor
    from utils import verify_and_get_user_id
    
    try:
        uid = verify_and_get_user_id(request)
        data = request.json
        
        # Validation Logic (Inline to avoid extra import overhead)
        required = ["cost_per_mile", "deadhead_cost_per_hour", "min_net_profit_per_ride"]
        for key in required:
            if key not in data: raise ValueError(f"Missing {key}")
            
        data["driver_id"] = uid
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO app_private.driver_settings_new (driver_id, settings)
                VALUES (%s, %s::jsonb)
                ON CONFLICT (driver_id) DO UPDATE
                    SET settings = EXCLUDED.settings, last_updated = now()
            """, (uid, json.dumps(data)))
            conn.commit()
            
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("SELECT settings FROM app_private.driver_settings_new WHERE driver_id = %s", (uid,))
            post_write = cur.fetchone()
            return jsonify(post_write["settings"] if post_write else {})

    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if "conn" in locals(): conn.close()

# --------------------------------------------------------------
# 6. Admin / Debug (Secured & Lazy)
# --------------------------------------------------------------
@app.route('/api/v1/debug/routes', methods=['GET'])
def list_routes():
    from utils import require_firebase_auth
    @require_firebase_auth
    def internal_list():
        import urllib.parse
        output = []
        for rule in sorted(app.url_map.iter_rules(), key=lambda x: str(x)):
            if rule.endpoint == 'static': continue
            output.append({"endpoint": rule.endpoint, "methods": list(rule.methods), "url": urllib.parse.unquote(str(rule))})
        return jsonify({"total_routes": len(output), "routes": output})
    return internal_list()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5001))
    app.run(host="0.0.0.0", port=port)