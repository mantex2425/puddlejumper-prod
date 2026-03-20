import logging
import traceback

from flask import Blueprint, request, jsonify
from psycopg2.extras import RealDictCursor

from db import get_db
from utils import verify_and_get_user_id, require_firebase_auth

referrals_bp = Blueprint('referrals', __name__)

# ======================================================================
# ROUTE: Apply a Referral Code
# ======================================================================

@require_firebase_auth
@referrals_bp.route('/apply', methods=['POST'])
def apply_referral():
    try:
        uid = verify_and_get_user_id(request)
        data = request.json
        code = data.get('code')

        if not code:
            return jsonify({"success": False, "message": "Missing referral code."}), 400

        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # 1. Find the code owner
        cur.execute(
            "SELECT user_id FROM referral_codes WHERE code = %s", (code,)
        )
        referrer = cur.fetchone()

        if not referrer:
            return jsonify({"success": False, "message": "Invalid referral code."}), 400

        referrer_id = referrer['user_id']

        # 2. Prevent self-referral
        if uid == referrer_id:
            return jsonify({"success": False, "message": "You cannot refer yourself."}), 400

        # 3. Check if already referred
        cur.execute(
            "SELECT id FROM referrals WHERE referee_id = %s", (uid,)
        )
        if cur.fetchone():
            return jsonify({"success": False, "message": "Referral code already applied."}), 409

        # 4. Link them
        cur.execute("""
            INSERT INTO referrals (referrer_id, referee_id, status)
            VALUES (%s, %s, 'PENDING_WORK')
        """, (referrer_id, uid))
        conn.commit()

        return jsonify({"success": True, "message": "Referral applied successfully!"})

    except Exception as e:
        logging.error(f"Referral apply failed: {e}")
        traceback.print_exc()
        return jsonify({"success": False, "message": "Something went wrong. Try again."}), 500
    finally:
        if 'conn' in locals():
            conn.close()