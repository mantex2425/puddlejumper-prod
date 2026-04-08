# backend/driver_state_reset.py
# Emergency reset for driver trip state machine
# Use when state gets stuck due to cancellation or GPS miss

import logging
from flask import Blueprint, request, jsonify
from psycopg2.extras import RealDictCursor

from db import get_db
from utils import verify_and_get_user_id, require_firebase_auth
from state_machine import DriverStateMachine

driver_state_reset_bp = Blueprint('driver_state_reset', __name__)

# ======================================================================
# POST /api/v1/driver/reset-state
# Resets driver trip state machine back to UNCOMMITTED.
# Call when: going offline, after cancellations, state badge feels wrong.
# Driver ID extracted from Firebase token — no body needed.
# ======================================================================
@require_firebase_auth
@driver_state_reset_bp.route("/driver/reset-state", methods=["POST"])
def reset_driver_state():
    try:
        driver_id = verify_and_get_user_id(request)
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        DriverStateMachine.transition(driver_id, 'manual_reset', cur, conn,
            clear_coords=True,
        )

        logging.info(f"🔄 State reset to UNCOMMITTED for driver {driver_id}")

        return jsonify({
            "status": "reset",
            "message": "Driver state cleared. Ready for next ride.",
            "voice": "State reset. Ready for next ride."
        }), 200

    except Exception as e:
        logging.exception("driver state reset error")
        if 'conn' in locals():
            conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        if 'conn' in locals():
            conn.close()