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

# The engine scores against a driver's own settings, and driver_settings_new has a foreign
# key to the auth users table, so an invented uid cannot have settings: it short-circuits
# to a 0.00/hr DECLINE, which would still pass a shape-only check while real scoring was
# broken. So borrow an existing account's settings for the dry run instead. Nothing is
# written -- the transaction is rolled back -- and no row is read that the driver could
# not read themselves.
_SETTINGS_OWNER_SQL = """
    SELECT driver_id FROM app_private.driver_settings_new
    WHERE settings ? 'dsi_threshold' AND settings ? 'cost_per_mile'
    ORDER BY driver_id LIMIT 1
"""

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
    # .strip() both sides: a secret created from a file keeps its trailing newline, while
    # $(gcloud secrets versions access) drops it, so the two never matched (2026-09-19).
    expected = (os.environ.get("SMOKE_TOKEN") or "").strip()
    supplied = (request.headers.get("X-Smoke-Token") or "").strip()
    if not expected or supplied != expected:
        return jsonify({"ok": False, "error": "forbidden"}), 403

    # Imported here for the same reason the bug existed: this must exercise the real
    # module graph at request time, not whatever was resolved at boot.
    from decisions.router import parse_request
    from decisions.engine import run_decision_engine

    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute(_SETTINGS_OWNER_SQL)
        row = cur.fetchone()
        if not row:
            return jsonify({"ok": False, "error": "no account with settings to score against"}), 500
        uid = row["driver_id"]

        params = parse_request(dict(CANNED_OFFER), uid)
        result, _trace, _ep = run_decision_engine(cur, conn, uid, params)
        conn.rollback()  # nothing this endpoint touches is kept

        verdict = (result or {}).get("verdict")
        rate = (result or {}).get("hourlyRate")
        # A rate of exactly 0 means the engine short-circuited before doing arithmetic --
        # the failure mode a shape-only assertion would wave through.
        ok = bool(verdict) and isinstance(rate, (int, float)) and rate > 0
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
