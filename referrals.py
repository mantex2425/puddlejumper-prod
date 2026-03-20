import logging
import traceback
import secrets
import string

from flask import Blueprint, request, jsonify
from psycopg2.extras import RealDictCursor

from db import get_db
from utils import verify_and_get_user_id, require_firebase_auth

referrals_bp = Blueprint('referrals', __name__)


def generate_referral_code(length=8):
    """Generate a random alphanumeric referral code."""
    alphabet = string.ascii_uppercase + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))


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


# ======================================================================
# ROUTE: Get or Create My Referral Code
# ======================================================================

@require_firebase_auth
@referrals_bp.route('/my-code', methods=['GET'])
def get_my_code():
    try:
        uid = verify_and_get_user_id(request)

        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # Check if user already has a code
        cur.execute(
            "SELECT code, created_at FROM referral_codes WHERE user_id = %s", (uid,)
        )
        existing = cur.fetchone()

        if existing:
            return jsonify({
                "success": True,
                "code": existing['code'],
                "created_at": existing['created_at'].isoformat()
            })

        # Generate a new unique code
        for _ in range(10):
            new_code = generate_referral_code()
            cur.execute("SELECT id FROM referral_codes WHERE code = %s", (new_code,))
            if not cur.fetchone():
                break
        else:
            return jsonify({"success": False, "message": "Could not generate unique code"}), 500

        # Insert the new code
        cur.execute("""
            INSERT INTO referral_codes (user_id, code)
            VALUES (%s, %s)
            RETURNING code, created_at
        """, (uid, new_code))
        row = cur.fetchone()
        conn.commit()

        return jsonify({
            "success": True,
            "code": row['code'],
            "created_at": row['created_at'].isoformat(),
            "new": True
        }), 201

    except Exception as e:
        logging.error(f"Get my code failed: {e}")
        traceback.print_exc()
        return jsonify({"success": False, "message": "Something went wrong"}), 500
    finally:
        if 'conn' in locals():
            conn.close()

# ======================================================================
# ROUTE: Get My Referral Status / Stats
# ======================================================================

@require_firebase_auth
@referrals_bp.route('/status', methods=['GET'])
def get_referral_status():
    try:
        uid = verify_and_get_user_id(request)

        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # Get user's referral code
        cur.execute("SELECT code FROM referral_codes WHERE user_id = %s", (uid,))
        code_row = cur.fetchone()
        my_code = code_row['code'] if code_row else None

        # Get all referrals where this user is the referrer
        cur.execute("""
            SELECT 
                referee_id,
                offer_count,
                status,
                created_at,
                updated_at
            FROM referrals 
            WHERE referrer_id = %s
            ORDER BY created_at DESC
        """, (uid,))
        referrals = cur.fetchall()

        # Calculate stats
        total_referrals = len(referrals)
        pending_work = sum(1 for r in referrals if r['status'] == 'PENDING_WORK')
        pending_payment = sum(1 for r in referrals if r['status'] == 'PENDING_PAYMENT')
        payable = sum(1 for r in referrals if r['status'] == 'PAYABLE')
        paid = sum(1 for r in referrals if r['status'] == 'PAID')
        
        # $15 per paid referral
        total_earned = paid * 15
        total_pending = (payable + pending_payment) * 15

        # Format referrals for response
        referral_list = [{
            "referee_id": r['referee_id'][:8] + "...",  # Truncate for privacy
            "offer_count": r['offer_count'],
            "status": r['status'],
            "created_at": r['created_at'].isoformat(),
            "updated_at": r['updated_at'].isoformat() if r['updated_at'] else None
        } for r in referrals]

        return jsonify({
            "success": True,
            "my_code": my_code,
            "stats": {
                "total_referrals": total_referrals,
                "pending_work": pending_work,
                "pending_payment": pending_payment,
                "payable": payable,
                "paid": paid,
                "total_earned": total_earned,
                "total_pending": total_pending
            },
            "referrals": referral_list
        })

    except Exception as e:
        logging.error(f"Get referral status failed: {e}")
        traceback.print_exc()
        return jsonify({"success": False, "message": "Something went wrong"}), 500
    finally:
        if 'conn' in locals():
            conn.close()