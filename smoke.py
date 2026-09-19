"""Post-deploy smoke test: score one canned offer through the real decision path.

Why this exists. On 2026-09-19 a TAD cleanup deleted four modules that a static grep
said nothing imported. decisions/engine.py imports one of them INSIDE the function, on
every decision, so the deploy succeeded, the container started, /api/v1/health answered,
and every scoring call returned {"error": "No module named 'bead_on_wire'"}. It was found
on the road, on three real offers, by a driver watching a frog that never lit up.

A health check that only proves the process is up cannot catch that. This runs the actual
pipeline -- parse_request then run_decision_engine, the same calls the router makes -- so
a missing import, a broken SQL function or a renamed setting fails here instead of on a
windscreen. It writes nothing: the transaction is rolled back, and the driver id is one no
person can own.
"""
import logging
import os

from flask import Blueprint, jsonify, request
from psycopg2.extras import RealDictCursor

from db import get_db

smoke_bp = Blueprint("smoke", __name__)

# Not a Firebase uid, so it can never collide with a real driver.
SMOKE_UID = "pj-smoke-test-not-a-driver"

# A dull, unambiguous offer: $18.50, 6.2 miles, 21 minutes, 2 miles of pickup. Any engine
# that works at all returns a verdict for it. We assert the SHAPE, not the verdict, so a
# threshold change never turns a deploy red.
CANNED_OFFER = {
    "fare": 18.5,
    "tripMiles": 6.2,
    "tripMinutes": 21,
    "pickupMiles": 2.0,
    "pickupMinutes": 7,
    "lat": 29.7604,
    "lng": -95.3698,
    "dropoffLat": 29.7899,
    "dropoffLng": -95.4194,
    "isPuddleJumpMode": False,
}


@smoke_bp.route("/internal/smoke/decision", methods=["GET"])
def smoke_decision():
    expected = os.environ.get("SMOKE_TOKEN")
    if not expected or request.headers.get("X-Smoke-Token") != expected:
        return jsonify({"ok": False, "error": "forbidden"}), 403

    # Imported here for the same reason the bug existed: this must exercise the real
    # module graph at request time, not whatever was resolved at boot.
    from decisions.router import parse_request
    from decisions.engine import run_decision_engine

    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        params = parse_request(dict(CANNED_OFFER), SMOKE_UID)
        result, _trace, _ep = run_decision_engine(cur, conn, SMOKE_UID, params)
        conn.rollback()  # nothing this endpoint touches is kept

        verdict = (result or {}).get("verdict")
        rate = (result or {}).get("hourlyRate")
        ok = bool(verdict) and rate is not None
        payload = {
            "ok": ok,
            "verdict": verdict,
            "hourlyRate": rate,
            "engineVersion": (result or {}).get("engineVersion"),
        }
        if not ok:
            logging.error(f"[SMOKE] engine returned no verdict: {result}")
        return jsonify(payload), (200 if ok else 500)
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logging.exception("[SMOKE] decision path failed")
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500
    finally:
        conn.close()
