from flask import Blueprint, request, jsonify
from db import get_db
from utils import require_firebase_auth, verify_and_get_user_id, ensure_user_exists

auth_bp = Blueprint("auth", __name__, url_prefix="/api/v1/auth")

@require_firebase_auth
@auth_bp.route("/init", methods=["POST"])
def initialize_user():
    """
    Endpoint run immediately after client Firebase sign-in.
    Ensures the Firebase UID exists in the 'auth.users' table.
    """
    conn = get_db()
    
    try:
        # Get the UID (the user ID is sufficient, as confirmed by schema review)
        uid = verify_and_get_user_id(request) 

        # Execute the upsert to guarantee the user exists in auth.users
        with conn.cursor() as cur:
            ensure_user_exists(cur, uid) 
            conn.commit()

        # Success! The user is now on the roster.
        return jsonify({"status": "User initialized and registered in DB", "user_id": uid}), 200

    except Exception as e:
        conn.rollback()
        # Return a generic 500 error for DB/internal issues
        return jsonify({"error": f"Failed to initialize user: {str(e)}"}), 500