from flask import Blueprint, request, jsonify
from db import get_db
from utils import require_firebase_auth, verify_and_get_user_id

crash_reports_bp = Blueprint("crash_reports", __name__, url_prefix="/api/v1")


@require_firebase_auth
@crash_reports_bp.route("/crash-report", methods=["POST"])
def receive_crash_report():
    conn = get_db()
    try:
        uid = verify_and_get_user_id(request)
        data = request.get_json()
        crash_log = data.get("crashLog", "")
        device_info = data.get("deviceInfo", "")

        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO app_private.crash_reports (driver_id, crash_log, device_info)
                   VALUES (%s, %s, %s)""",
                (uid, crash_log, device_info)
            )
        conn.commit()
        return jsonify({"status": "received"}), 200
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
