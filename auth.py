from flask import Blueprint, request, jsonify
from db import get_db
from utils import require_firebase_auth, verify_and_get_user_id, ensure_user_exists
import json as _json

auth_bp = Blueprint("auth", __name__, url_prefix="/api/v1/auth")

# Smart defaults for new drivers
#
# PRODUCT DEFAULTS, approved by Andrew 2026-09-15 -- deliberate, not one driver's tuning:
#   dsi_formula   net_hourly: DSI is net dollars per hour; the weighted index is retired.
#   dsi_threshold 12.0 $/hr: the peak of net per online hour when a month of real
#                 Houston offers is replayed at $0.23/mi (65% of offers pass).
#   cost_per_mile 0.23: AAA Your Driving Costs 2025, medium sedan operating cost
#                 (fuel 11.13c + maintenance, repair & tires 11.91c). Marginal cost only;
#                 the setup wizard replaces it with the driver's own fuel figure.
# Was cost_per_mile 0.67 (roughly the IRS fully-loaded rate), which declines most offers.
_NEW_DRIVER_DEFAULTS = {
    "setup_completed": False,
    "car_type": "gas",
    "calculation_method": "both",
    "engine_version": "v3",
    "dsi_formula": "net_hourly",
    "dsi_threshold": 12.0,
    # Minimum on-screen gross $/hr, checked after DSI (2026-09-16). 0 turns it off.
    "min_gross_hourly": 18.0,
    "cost_per_mile": 0.23,
    "cost_per_hour": 12.0,
    "revenue_per_hour": 10.0,
    "revenue_per_mile": 1.00,
    "min_effective_hourly_rate": 15.0,
    "min_effective_dollar_per_mile": 0.70,
    "deadhead_percent": 1.0,
    "deadhead_basis": "hourly",
    "max_pickup_miles": 25.0,
    "timezone": "America/Chicago",
    "freestyleStrategy": "ai",
    "autoOptimizeEnabled": True,
    "markets": [],
    "redZones": [],
    "shifts": [],
}

@auth_bp.route("/init", methods=["POST"])
@require_firebase_auth
def initialize_user():
    """
    Endpoint run immediately after client Firebase sign-in.
    Ensures the Firebase UID exists in auth.users AND
    creates driver_settings_new row with smart defaults if missing.
    """
    conn = get_db()

    try:
        uid = verify_and_get_user_id(request)

        with conn.cursor() as cur:
            # 1. Ensure auth.users row
            ensure_user_exists(cur, uid)

            # 2. Create driver_settings_new with smart defaults if not exists
            cur.execute(
                "SELECT 1 FROM app_private.driver_settings_new WHERE driver_id = %s",
                (uid,)
            )
            is_new = cur.fetchone() is None

            if is_new:
                defaults = dict(_NEW_DRIVER_DEFAULTS)
                defaults["driver_id"] = uid
                cur.execute(
                    """INSERT INTO app_private.driver_settings_new (driver_id, settings)
                       VALUES (%s, %s::jsonb)
                       ON CONFLICT (driver_id) DO NOTHING""",
                    (uid, _json.dumps(defaults))
                )

            conn.commit()

        status = "created" if is_new else "exists"
        return jsonify({"status": status, "user_id": uid}), 200

    except Exception as e:
        conn.rollback()
        return jsonify({"error": f"Failed to initialize user: {str(e)}"}), 500
