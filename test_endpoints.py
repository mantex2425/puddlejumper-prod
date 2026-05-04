# backend/test_endpoints.py
#
# Sprint A test harness — Bruno-driven simulation endpoints.
#
# Allowlist-gated: only responds for known test driver IDs. Real drivers
# get 403. The blueprint is registered unconditionally in app.py; the
# allowlist check inside each handler is the security boundary.
#
# Endpoints:
#   POST /api/v1/test/seed_offer    — write offer_history + driver_trip_state
#                                     fixture so WAI has something to match
#   POST /api/v1/test/reset_driver  — clear driver_trip_state +
#                                     test-fixture rows for clean re-run
#
# Why allowlist over env var: env vars get forgotten in production deploys.
# A driver_id allowlist is encoded in source, reviewable in PRs, and
# impossible to enable accidentally without a code change.

import logging
from flask import Blueprint, request, jsonify
from psycopg2.extras import RealDictCursor

from db import get_db
from utils import verify_and_get_user_id, require_firebase_auth

log = logging.getLogger(__name__)

test_endpoints_bp = Blueprint('test_endpoints', __name__)

# ─── Allowlist ────────────────────────────────────────────────────────────────
# Only these driver_ids may invoke /api/v1/test/* endpoints.
# Add new entries by code change + PR review only — never via runtime config.
TEST_ALLOWLIST = frozenset({
    "C7FRKHbnvFcDPZ0RcXXjtPwGXgw2",  # Bruno test driver (Andrew's Bruno auth)
})


def _require_test_driver(driver_id):
    """Return None if allowed, or a (response, status) tuple to abort with."""
    if driver_id not in TEST_ALLOWLIST:
        log.warning("[test] denied: driver_id=%s not in TEST_ALLOWLIST", driver_id)
        return jsonify({"error": "test endpoints not enabled for this driver"}), 403
    return None


# ─── POST /api/v1/test/seed_offer ─────────────────────────────────────────────
@test_endpoints_bp.route("/test/seed_offer", methods=["POST"])
@require_firebase_auth
def seed_offer():
    """Seed a synthetic offer for the test driver.

    Writes:
      - app_private.decision_log row (parent FK)
      - app_private.offer_history row (so _project_queue can see it)
      - app_private.driver_trip_state row updated with current_offer_id
        IF body['set_active'] is true (default false — leave NULL so
        Case B can fire from empty current_offer_id when WAI matches).

    Body schema (all required unless noted):
      pickup_lat, pickup_lng, pickup_address, pickup_miles, pickup_minutes
      dropoff_lat, dropoff_lng, dropoff_address, trip_miles, trip_minutes
      fare, app_verdict (e.g. "ACCEPT" or "DECLINE")
      set_active (optional bool, default false)

    Returns:
      201 { "offer_id": <int>, "decision_log_id": <int>, "active": <bool> }
    """
    driver_id = verify_and_get_user_id(request)
    deny = _require_test_driver(driver_id)
    if deny:
        return deny

    body = request.get_json() or {}
    required = [
        "pickup_lat", "pickup_lng", "pickup_address", "pickup_miles", "pickup_minutes",
        "dropoff_lat", "dropoff_lng", "dropoff_address", "trip_miles", "trip_minutes",
        "fare", "app_verdict",
    ]
    missing = [k for k in required if k not in body]
    if missing:
        return jsonify({"error": "missing required fields", "missing": missing}), 400

    set_active = bool(body.get("set_active", False))

    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        # 1. Parent decision_log row
        cur.execute("""
            INSERT INTO app_private.decision_log (
                driver_id, fare,
                pickup_minutes, trip_minutes,
                pickup_lat, pickup_lng,
                dropoff_lat, dropoff_lng,
                pickup_miles, trip_miles,
                audit_source
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'test_harness'
            )
            RETURNING id
        """, (
            driver_id, body["fare"],
            body["pickup_minutes"], body["trip_minutes"],
            body["pickup_lat"], body["pickup_lng"],
            body["dropoff_lat"], body["dropoff_lng"],
            body["pickup_miles"], body["trip_miles"],
        ))
        decision_log_id = cur.fetchone()["id"]

        # 2. offer_history row
        cur.execute("""
            INSERT INTO app_private.offer_history (
                pickup_lat, pickup_lng, pickup_address, pickup_miles, pickup_minutes,
                dropoff_lat, dropoff_lng, dropoff_address, trip_miles, trip_minutes,
                fare, app_verdict, decision_log_id, is_validated
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, true
            )
            RETURNING id
        """, (
            body["pickup_lat"], body["pickup_lng"], body["pickup_address"],
            body["pickup_miles"], body["pickup_minutes"],
            body["dropoff_lat"], body["dropoff_lng"], body["dropoff_address"],
            body["trip_miles"], body["trip_minutes"],
            body["fare"], body["app_verdict"], decision_log_id,
        ))
        offer_id = cur.fetchone()["id"]

        # 3. Optionally bind as active in driver_trip_state.
        if set_active:
            cur.execute("""
                INSERT INTO app_private.driver_trip_state (driver_id, current_offer_id)
                VALUES (%s, %s)
                ON CONFLICT (driver_id) DO UPDATE
                SET current_offer_id = EXCLUDED.current_offer_id
            """, (driver_id, str(offer_id)))

        conn.commit()
        log.info("[test] seeded offer_id=%s for driver=%s active=%s",
                 offer_id, driver_id, set_active)
        return jsonify({
            "offer_id": offer_id,
            "decision_log_id": decision_log_id,
            "active": set_active,
        }), 201

    except Exception as e:
        conn.rollback()
        log.exception("[test] seed_offer failed")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


# ─── POST /api/v1/test/reset_driver ───────────────────────────────────────────
@test_endpoints_bp.route("/test/reset_driver", methods=["POST"])
@require_firebase_auth
def reset_driver():
    """Reset all state for the test driver.

    Deletes:
      - pudo_decision_context rows for this driver
      - offer_history rows tied to this driver's test_harness decision_logs
      - decision_log rows (audit_source='test_harness' ONLY — production
        rows preserved as a safety boundary even within the allowlist)
      - driver_trip_state row cleared (current_offer_id NULL)

    Returns:
      200 { "deleted": {...} }
    """
    driver_id = verify_and_get_user_id(request)
    deny = _require_test_driver(driver_id)
    if deny:
        return deny

    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    counts = {}
    try:
        # 1. pudo_decision_context — driver-keyed, all rows fair game
        cur.execute("""
            DELETE FROM app_private.pudo_decision_context WHERE driver_id = %s
        """, (driver_id,))
        counts["pudo_decision_context"] = cur.rowcount

        # 2. offer_history — only rows tied to test_harness decision_logs.
        cur.execute("""
            DELETE FROM app_private.offer_history
            WHERE decision_log_id IN (
                SELECT id FROM app_private.decision_log
                WHERE driver_id = %s AND audit_source = 'test_harness'
            )
        """, (driver_id,))
        counts["offer_history"] = cur.rowcount

        # 3. decision_log — only test_harness rows. Production rows preserved.
        cur.execute("""
            DELETE FROM app_private.decision_log
            WHERE driver_id = %s AND audit_source = 'test_harness'
        """, (driver_id,))
        counts["decision_log"] = cur.rowcount

        # 4. driver_trip_state — clear current_offer_id + heartbeat
        #    (don't delete the row; the upserted shell is fine for the
        #    next test cycle and clearing avoids ON CONFLICT churn).
        cur.execute("""
            UPDATE app_private.driver_trip_state
            SET current_offer_id = NULL,
                heartbeat = NULL,
                heartbeat_at = NULL
            WHERE driver_id = %s
        """, (driver_id,))
        counts["driver_trip_state_cleared"] = cur.rowcount > 0

        conn.commit()
        log.info("[test] reset driver=%s counts=%s", driver_id, counts)
        return jsonify({"deleted": counts}), 200

    except Exception as e:
        conn.rollback()
        log.exception("[test] reset_driver failed")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


# ─── GET /api/v1/test/inspect ─────────────────────────────────────────────────
@test_endpoints_bp.route("/test/inspect", methods=["GET"])
@require_firebase_auth
def inspect_test_state():
    """Return row counts for the test driver — verifies seed/reset side effects.

    Designed for Bruno post-reset assertions (pudo_rows == 0, etc.).
    No body. Returns JSON with row counts and a couple of sanity samples.

    Returns:
      200 {
        "driver_id": "<id>",
        "pudo_rows":           <int>,
        "offer_history_rows":  <int>  // test_harness only
        "decision_log_rows":   <int>  // test_harness only
        "current_offer_id":    <str|null>,
        "heartbeat_age_sec":   <int|null>
      }
    """
    driver_id = verify_and_get_user_id(request)
    deny = _require_test_driver(driver_id)
    if deny:
        return deny

    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""
            SELECT COUNT(*) AS n
            FROM app_private.pudo_decision_context
            WHERE driver_id = %s
        """, (driver_id,))
        pudo_rows = cur.fetchone()["n"]

        cur.execute("""
            SELECT COUNT(*) AS n
            FROM app_private.offer_history
            WHERE decision_log_id IN (
                SELECT id FROM app_private.decision_log
                WHERE driver_id = %s AND audit_source = 'test_harness'
            )
        """, (driver_id,))
        offer_rows = cur.fetchone()["n"]

        cur.execute("""
            SELECT COUNT(*) AS n
            FROM app_private.decision_log
            WHERE driver_id = %s AND audit_source = 'test_harness'
        """, (driver_id,))
        decision_rows = cur.fetchone()["n"]

        cur.execute("""
            SELECT current_offer_id, heartbeat_at,
                   EXTRACT(EPOCH FROM (NOW() - heartbeat_at))::integer AS hb_age
            FROM app_private.driver_trip_state
            WHERE driver_id = %s
        """, (driver_id,))
        dts = cur.fetchone()

        return jsonify({
            "driver_id": driver_id,
            "pudo_rows": pudo_rows,
            "offer_history_rows": offer_rows,
            "decision_log_rows": decision_rows,
            "current_offer_id": (
                str(dts["current_offer_id"])
                if dts and dts["current_offer_id"] else None
            ),
            "heartbeat_age_sec": dts["hb_age"] if dts else None,
        }), 200

    except Exception as e:
        log.exception("[test] inspect failed")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()
