# backend/utils.py
import os
from functools import wraps
from flask import request, jsonify

from firebase_admin import auth as firebase_auth

from firebase import firebase_app        # <-- canonical init path
from db import get_db


# -----------------------------------------------------------
# Decorator: require_firebase_auth
# -----------------------------------------------------------

def require_firebase_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):

        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return jsonify({"error": "Missing or malformed Authorization header"}), 401

        token = auth_header.split(" ", 1)[1]

        try:
            decoded = firebase_auth.verify_id_token(token)
        except Exception:
            return jsonify({"error": "Invalid or expired Firebase token"}), 401

        request.user = decoded
        return f(*args, **kwargs)

    return wrapper


# -----------------------------------------------------------
# verify_and_get_user_id helper
# -----------------------------------------------------------

def verify_and_get_user_id(req: request):
    auth_header = req.headers.get("Authorization", "")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise Exception("Authorization token missing or malformed")

    token = auth_header.split(" ", 1)[1].strip()
    if not token:
        raise Exception("Authorization token empty")

    try:
        decoded = firebase_auth.verify_id_token(token)
    except Exception as e:
        raise Exception(f"Firebase token verification failed: {e}")

    uid = decoded.get("uid") or decoded.get("sub")
    if not uid:
        raise Exception("Token missing uid/sub claim")

    return str(uid).strip()


# -----------------------------------------------------------
# DB helpers
# -----------------------------------------------------------

def ensure_user_exists(cursor, user_id):
    cursor.execute(
        "INSERT INTO auth.users (id) VALUES (%s) ON CONFLICT (id) DO NOTHING;",
        (user_id,)
    )


# -----------------------------------------------------------
# Market helpers
# -----------------------------------------------------------

def get_active_market_config(cursor, auth_uid):
    cursor.execute(
        "SELECT settings FROM app_private.driver_settings_new WHERE driver_id = %s;",
        (auth_uid,)
    )
    row = cursor.fetchone()
    if not row or not row["settings"]:
        return None

    settings = row["settings"]

    global_red = settings.get("redZones", []) or settings.get("red_zones", [])
    markets = settings.get("markets", []) or settings.get("Markets", [])

    for market in markets:
        if market.get("isActive") or market.get("is_active"):
            return {
                "hex_key_l7": market.get("hexKey") or market.get("hex_key"),
                "city_name": market.get("cityName") or market.get("city_name"),
                "custom_cpm": market.get("customCPM") or market.get("custom_cpm"),
                "opportunity_multiplier": (
                    market.get("opportunityMultiplier")
                    or market.get("opportunity_multiplier")
                ),
                "green_zones": market.get("greenZones", []) or market.get("green_zones", []),
                "red_zones": global_red,
            }

    return None


def get_active_market_id(cursor, driver_id):
    cursor.execute(
        """
        SELECT active_market_id
        FROM app_private.driver_active_market
        WHERE driver_id = %s;
        """,
        (driver_id,)
    )
    row = cursor.fetchone()
    return row["active_market_id"] if row and "active_market_id" in row else None


def set_active_market_id(cursor, driver_id, market_id):
    cursor.execute(
        """
        INSERT INTO app_private.driver_active_market (driver_id, active_market_id, updated_at)
        VALUES (%s, %s, NOW())
        ON CONFLICT (driver_id)
        DO UPDATE SET
            active_market_id = EXCLUDED.active_market_id,
            updated_at = NOW();
        """,
        (driver_id, market_id)
    )


def resolve_market_id(cursor, driver_id, requested_market_id=None):
    if requested_market_id:
        return requested_market_id

    cursor.execute(
        """
        SELECT active_market_id
        FROM app_private.driver_active_market
        WHERE driver_id = %s;
        """,
        (driver_id,)
    )
    row = cursor.fetchone()
    return row["active_market_id"] if row and row.get("active_market_id") else None