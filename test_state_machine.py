"""
test_state_machine.py — PuddleJumper State Machine Scenario Test Harness
v2.0 — Uses DriverStateMachine exclusively. No trip_state references.

Usage:
  python3 test_state_machine.py
"""

import psycopg2
import psycopg2.extras
import logging
import sys

logging.basicConfig(level=logging.ERROR)

DB_CONFIG = {
    "host":   "10.128.0.2",
    "user":   "postgres",
    "dbname": "puddlejumper",
}

TEST_DRIVER = "TEST_STATE_MACHINE_HARNESS_000"

# Geometry: real Houston coordinates
PICKUP_LAT  = 29.7097
PICKUP_LNG  = -95.4008
PICKUP_H3   = "88446ca839fffff"
DROPOFF_LAT = 29.7574
DROPOFF_LNG = -95.3947
DROPOFF_H3  = "88446ca8a5fffff"
DRIVER_LAT  = 29.6900
DRIVER_LNG  = -95.4500

# Secondary ride (stacked)
PICKUP2_LAT = 29.7200
PICKUP2_LNG = -95.3900
PICKUP2_H3  = "88446ca8b1fffff"
DROPOFF2_LAT = 29.7800
DROPOFF2_LNG = -95.3800
DROPOFF2_H3  = "88446ca8c3fffff"

PASS = "✅ PASS"
FAIL = "❌ FAIL"


# ── DB helpers ────────────────────────────────────────────────────────────────

def setup(cur, conn):
    """Reset test driver to UNCOMMITTED via replay_harness. Trigger intact."""
    cur.execute("""
        INSERT INTO app_private.driver_trip_state (driver_id, state, state_updated_at)
        VALUES (%s, 'UNCOMMITTED', NOW())
        ON CONFLICT (driver_id) DO NOTHING
    """, (TEST_DRIVER,))
    conn.commit()
    cur.execute(
        "SELECT * FROM app_private.sm_transition(%s, 'replay_harness', NULL, NULL, NULL, 'UNCOMMITTED', p_clear_coords := TRUE)",
        (TEST_DRIVER,)
    )
    conn.commit()


def force_state(cur, conn, state, offer_id=None,
                pickup_lat=None, pickup_lng=None, pickup_h3=None,
                dropoff_lat=None, dropoff_lng=None, dropoff_h3=None,
                nailed_pickup_lat=None, nailed_pickup_lng=None, nailed_pickup_error_m=None,
                nailed_dropoff_lat=None, nailed_dropoff_lng=None, nailed_dropoff_error_m=None,
                potential_cancellation=False):
    """
    Force arbitrary state via replay_harness. Trigger guard fully respected.
    p_pickup_h3 carries the target state name (sm_transition replay_harness design).
    """
    cur.execute("""
        SELECT * FROM app_private.sm_transition(
            %s, 'replay_harness',
            %s,
            %s, %s, %s,
            %s, %s, %s,
            %s, %s, %s,
            %s, %s, %s,
            %s
        )
    """, (
        TEST_DRIVER,
        offer_id,
        pickup_lat, pickup_lng, state,
        dropoff_lat, dropoff_lng, dropoff_h3,
        nailed_pickup_lat, nailed_pickup_lng, nailed_pickup_error_m,
        nailed_dropoff_lat, nailed_dropoff_lng, nailed_dropoff_error_m,
        False,
    ))
    conn.commit()
    if potential_cancellation:
        cur.execute(
            "UPDATE app_private.driver_trip_state SET potential_cancellation = TRUE WHERE driver_id = %s",
            (TEST_DRIVER,)
        )
        conn.commit()


def get_row(cur):
    cur.execute("""
        SELECT state, current_offer_id, potential_cancellation,
               pickup_lat, pickup_lng, pickup_h3,
               dropoff_lat, dropoff_lng, dropoff_h3,
               nailed_pickup_lat, nailed_pickup_lng, nailed_pickup_error_m,
               nailed_dropoff_lat, nailed_dropoff_lng, nailed_dropoff_error_m,
               state_updated_at,
               EXTRACT(EPOCH FROM (
                   NOW() - state_updated_at
               ))::integer AS state_seconds
        FROM app_private.driver_trip_state
        WHERE driver_id = %s
    """, (TEST_DRIVER,))
    return cur.fetchone()


def get_log_count(cur):
    """Count state log entries for test driver."""
    cur.execute("""
        SELECT COUNT(*) AS n FROM app_private.driver_trip_state_log
        WHERE driver_id = %s
    """, (TEST_DRIVER,))
    return cur.fetchone()["n"]


def get_sm_read(cur):
    cur.execute("SELECT * FROM app_private.sm_read(%s)", (TEST_DRIVER,))
    return cur.fetchone()


def seed_offer_history(cur, conn, offer_id, plat, plng, ph3, dlat, dlng, dh3, verdict=None):
    """Seed offer_history + decision_log for S11/S12 nearby scan tests."""
    verdict = verdict if verdict else 'DECLINE'
    cur.execute(f"""
        INSERT INTO app_private.decision_log
            (driver_id, fare, pickup_minutes, trip_minutes, pickup_lat, pickup_lng,
             dropoff_lat, dropoff_lng, decision_result, created_at)
        VALUES (%s, 10.0, 3, 15, %s, %s, %s, %s, '{{"verdict":"{verdict}"}}'::jsonb,
                NOW())
        RETURNING id
    """, (TEST_DRIVER, plat, plng, dlat, dlng))
    dl_id = cur.fetchone()["id"]
    cur.execute("""
        INSERT INTO app_private.offer_history
            (decision_log_id, pickup_lat, pickup_lng, pickup_h3,
             dropoff_lat, dropoff_lng, dropoff_h3, app_verdict, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                NOW())
    """, (dl_id, plat, plng, ph3, dlat, dlng, dh3, verdict))
    conn.commit()
    return dl_id


# ── Test runner ───────────────────────────────────────────────────────────────

def run_tests():
    from state_machine import DriverStateMachine as SM
    from nail_it_core import check_convergence

    conn = psycopg2.connect(**DB_CONFIG, cursor_factory=psycopg2.extras.RealDictCursor)
    conn.autocommit = False

    results = []

    def sp():
        """Reset to clean UNCOMMITTED before each scenario."""
        setup(cur, conn)
        conn.commit()

    def rsp():
        """Reset to clean UNCOMMITTED after each scenario."""
        setup(cur, conn)
        # Also clear offer_history/decision_log seeded during test
        cur.execute("DELETE FROM app_private.offer_history WHERE decision_log_id IN "
                    "(SELECT id FROM app_private.decision_log WHERE driver_id = %s)",
                    (TEST_DRIVER,))
        cur.execute("DELETE FROM app_private.decision_log WHERE driver_id = %s",
                    (TEST_DRIVER,))
        cur.execute("DELETE FROM app_private.driver_trip_state_log WHERE driver_id = %s",
                    (TEST_DRIVER,))
        conn.commit()

    def T(sid, desc, ok, detail=""):
        status = PASS if ok else FAIL
        results.append((sid, desc, ok, detail))
        print(f"  {status}  {sid:<5}  {desc}  {detail}")

    def trans(trigger, **coords):
        return SM.transition(TEST_DRIVER, trigger, cur, conn, **coords)

    def read():
        return SM.read(TEST_DRIVER, cur)

    try:
        cur = conn.cursor()
        setup(cur, conn)
        conn.commit()

        print(f"\n{'='*70}")
        print(f"  🐸 PuddleJumper State Machine Test Suite v2.0")
        print(f"  Driver: {TEST_DRIVER[:32]}...")
        print(f"{'='*70}\n")

        # ── SECTION 1: Core State Transitions ────────────────────────────────

        print("── Section 1: Core State Transitions ──────────────────────────────\n")

        # S01
        sp()
        row = get_row(cur)
        ok = row["state"] == "UNCOMMITTED" and not row["potential_cancellation"]
        T("S01", "Baseline UNCOMMITTED on start", ok, f"state={row['state']}")
        rsp()

        # S02
        sp()
        r = trans("offer_accepted",
            pickup_lat=PICKUP_LAT, pickup_lng=PICKUP_LNG, pickup_h3=PICKUP_H3,
            dropoff_lat=DROPOFF_LAT, dropoff_lng=DROPOFF_LNG, dropoff_h3=DROPOFF_H3)
        row = get_row(cur)
        ok = r["success"] and row["state"] == "ENROUTE"
        T("S02", "UNCOMMITTED + offer_accepted → ENROUTE", ok, f"state={row['state']}")
        rsp()

        # S03
        sp()
        force_state(cur, conn, "ENROUTE",
            pickup_lat=PICKUP_LAT, pickup_lng=PICKUP_LNG,
            dropoff_lat=DROPOFF_LAT, dropoff_lng=DROPOFF_LNG)
        r = trans("gps_convergence",
            nailed_pickup_lat=PICKUP_LAT, nailed_pickup_lng=PICKUP_LNG,
            nailed_pickup_error_m=50.0)
        row = get_row(cur)
        ok = r["success"] and row["state"] == "IN_TRIP"
        T("S03", "ENROUTE + gps_convergence → IN_TRIP", ok, f"state={row['state']}")
        rsp()

        # S04
        sp()
        force_state(cur, conn, "ENROUTE",
            pickup_lat=PICKUP_LAT, pickup_lng=PICKUP_LNG,
            dropoff_lat=DROPOFF_LAT, dropoff_lng=DROPOFF_LNG)
        r = trans("offer_cancelled_implicit", clear_coords=True)
        row = get_row(cur)
        ok = r["success"] and row["state"] == "UNCOMMITTED" and row["pickup_lat"] is None
        T("S04", "ENROUTE + offer_cancelled_implicit → UNCOMMITTED (coords cleared)", ok,
          f"state={row['state']} pickup_lat={row['pickup_lat']}")
        rsp()

        # S06
        sp()
        force_state(cur, conn, "STACKED",
            dropoff_lat=DROPOFF_LAT, dropoff_lng=DROPOFF_LNG,
            potential_cancellation=False)
        # STACKED + DECLINE = flag potential_cancellation (direct SQL, no state change)
        cur.execute("""
            UPDATE app_private.driver_trip_state
            SET potential_cancellation = TRUE,
                state_updated_at = NOW()
            WHERE driver_id = %s
        """, (TEST_DRIVER,))
        conn.commit()
        row = get_row(cur)
        ok = row["state"] == "STACKED" and row["potential_cancellation"]
        T("S06", "STACKED + 3rd offer → potential_cancellation flagged, state stays STACKED",
          ok, f"state={row['state']} cancel={row['potential_cancellation']}")
        rsp()

        # S07
        sp()
        force_state(cur, conn, "STACKED",
            dropoff_lat=DROPOFF2_LAT, dropoff_lng=DROPOFF2_LNG,
            potential_cancellation=True)
        r = trans("pickup_confirmed",
            nailed_pickup_lat=PICKUP2_LAT, nailed_pickup_lng=PICKUP2_LNG,
            nailed_pickup_error_m=30.0)
        row = get_row(cur)
        ok = r["success"] and row["state"] == "IN_TRIP"
        T("S07", "STACKED + pickup_confirmed → IN_TRIP", ok, f"state={row['state']}")
        rsp()

        # S08
        sp()
        force_state(cur, conn, "IN_TRIP",
            pickup_lat=PICKUP_LAT, pickup_lng=PICKUP_LNG,
            dropoff_lat=DROPOFF_LAT, dropoff_lng=DROPOFF_LNG,
            nailed_pickup_lat=PICKUP_LAT, nailed_pickup_lng=PICKUP_LNG,
            nailed_pickup_error_m=50.0)
        trans("approaching_dropoff")
        r = trans("dropoff_confirmed",
            nailed_dropoff_lat=DROPOFF_LAT, nailed_dropoff_lng=DROPOFF_LNG,
            nailed_dropoff_error_m=40.0,
            clear_coords=True)
        row = get_row(cur)
        ok = r["success"] and row["state"] == "UNCOMMITTED" and row["pickup_lat"] is None
        T("S08", "IN_TRIP→REFINE_DROPOFF→UNCOMMITTED (coords cleared)", ok,
          f"state={row['state']} pickup_lat={row['pickup_lat']}")
        rsp()

        # S09
        sp()
        force_state(cur, conn, "STACKED",
            pickup_lat=PICKUP2_LAT, pickup_lng=PICKUP2_LNG,
            dropoff_lat=DROPOFF_LAT, dropoff_lng=DROPOFF_LNG)
        r = trans("dropoff_confirmed",
            dropoff_lat=DROPOFF2_LAT, dropoff_lng=DROPOFF2_LNG,
            dropoff_h3=DROPOFF2_H3)
        row = get_row(cur)
        ok = r["success"] and row["state"] == "ENROUTE"
        T("S09", "STACKED + dropoff_confirmed → ENROUTE (buffer swap)", ok,
          f"state={row['state']}")
        rsp()

        # S30 — Secondary cancellation: STACKED + offer_declined → IN_TRIP
        # Verifies that when secondary is cancelled, primary coords are restored
        sp()
        force_state(cur, conn, "STACKED",
            pickup_lat=PICKUP_LAT, pickup_lng=PICKUP_LNG,
            dropoff_lat=DROPOFF_LAT, dropoff_lng=DROPOFF_LNG)
        # offer_declined = secondary cancelled, restore primary
        r = trans("offer_declined",
            offer_id=None,
            dropoff_lat=DROPOFF_LAT, dropoff_lng=DROPOFF_LNG,
            dropoff_h3=DROPOFF_H3)
        row = get_row(cur)
        ok = (r["success"] and 
              row["state"] == "IN_TRIP" and
              row["dropoff_lat"] == DROPOFF_LAT and
              row["dropoff_lng"] == DROPOFF_LNG)
        T("S30", "STACKED + offer_declined → IN_TRIP with primary coords restored", ok,
          f"state={row['state']} dropoff_lat={row['dropoff_lat']}")
        rsp()

        # S13
        sp()
        force_state(cur, conn, "IN_TRIP",
            pickup_lat=PICKUP_LAT, pickup_lng=PICKUP_LNG,
            dropoff_lat=DROPOFF_LAT, dropoff_lng=DROPOFF_LNG)
        r = trans("manual_reset", clear_coords=True)
        row = get_row(cur)
        ok = r["success"] and row["state"] == "UNCOMMITTED" and row["pickup_lat"] is None
        T("S13", "ANY + manual_reset → UNCOMMITTED (coords cleared)", ok,
          f"state={row['state']} pickup_lat={row['pickup_lat']}")
        rsp()

        # S14
        sp()
        force_state(cur, conn, "IN_TRIP",
            pickup_lat=PICKUP_LAT, pickup_lng=PICKUP_LNG,
            dropoff_lat=DROPOFF_LAT, dropoff_lng=DROPOFF_LNG)
        r = trans("watchdog_auto_reset", clear_coords=True)
        row = get_row(cur)
        ok = r["success"] and row["state"] == "UNCOMMITTED"
        T("S14", "IN_TRIP + watchdog_auto_reset → UNCOMMITTED", ok, f"state={row['state']}")
        rsp()

        # S14b STACKED watchdog
        sp()
        force_state(cur, conn, "STACKED",
            dropoff_lat=DROPOFF_LAT, dropoff_lng=DROPOFF_LNG)
        r = trans("watchdog_auto_reset", clear_coords=True)
        row = get_row(cur)
        ok = r["success"] and row["state"] == "UNCOMMITTED"
        T("S14b", "STACKED + watchdog_auto_reset → UNCOMMITTED", ok, f"state={row['state']}")
        rsp()

        # S16
        sp()
        force_state(cur, conn, "ENROUTE",
            pickup_lat=PICKUP_LAT, pickup_lng=PICKUP_LNG,
            dropoff_lat=DROPOFF_LAT, dropoff_lng=DROPOFF_LNG)
        r = trans("offer_cancelled_implicit", clear_coords=True)
        ok1 = r["success"] and read()["state"] == "UNCOMMITTED"
        r2 = trans("offer_accepted",
            pickup_lat=PICKUP2_LAT, pickup_lng=PICKUP2_LNG,
            dropoff_lat=DROPOFF2_LAT, dropoff_lng=DROPOFF2_LNG)
        row = get_row(cur)
        ok = ok1 and r2["success"] and row["state"] == "ENROUTE"
        T("S16", "ENROUTE + implicit cancel + new ACCEPT → UNCOMMITTED → ENROUTE", ok,
          f"state={row['state']}")
        rsp()

        # S23
        sp()
        force_state(cur, conn, "STACKED",
            pickup_lat=PICKUP2_LAT, pickup_lng=PICKUP2_LNG,
            dropoff_lat=DROPOFF_LAT, dropoff_lng=DROPOFF_LNG)
        r = trans("dropoff_confirmed",
            dropoff_lat=DROPOFF2_LAT, dropoff_lng=DROPOFF2_LNG,
            dropoff_h3=DROPOFF2_H3)
        row = get_row(cur)
        ok = r["success"] and row["state"] == "ENROUTE"
        T("S23", "STACKED + dropoff_confirmed → ENROUTE (Android buffer swap)", ok,
          f"state={row['state']}")
        rsp()

        # S24
        sp()
        force_state(cur, conn, "ENROUTE",
            pickup_lat=None, pickup_lng=None,  # triangulation failed
            dropoff_lat=DROPOFF_LAT, dropoff_lng=DROPOFF_LNG)
        r = trans("pickup_confirmed",
            nailed_pickup_lat=PICKUP_LAT, nailed_pickup_lng=PICKUP_LNG,
            nailed_pickup_error_m=25.0)
        row = get_row(cur)
        ok = r["success"] and row["state"] == "IN_TRIP"
        T("S24", "ENROUTE + pickup_confirmed with NULL pickup coords → IN_TRIP", ok,
          f"state={row['state']}")
        rsp()

        # S25
        sp()
        force_state(cur, conn, "IN_TRIP",
            pickup_lat=PICKUP_LAT, pickup_lng=PICKUP_LNG,
            dropoff_lat=None, dropoff_lng=None)  # triangulation failed
        r = trans("offer_accepted",
            pickup_lat=PICKUP2_LAT, pickup_lng=PICKUP2_LNG,
            pickup_h3=PICKUP2_H3)
        row = get_row(cur)
        ok = r["success"] and row["state"] == "STACKED"
        T("S25", "IN_TRIP + offer_accepted with NULL dropoff → STACKED (GPS fallback)", ok,
          f"state={row['state']}")
        rsp()

        # S28
        sp()
        force_state(cur, conn, "IN_TRIP",
            pickup_lat=PICKUP_LAT, pickup_lng=PICKUP_LNG,
            dropoff_lat=DROPOFF_LAT, dropoff_lng=DROPOFF_LNG)
        r = trans("offer_accepted",
            pickup_lat=PICKUP2_LAT, pickup_lng=PICKUP2_LNG)
        row = get_row(cur)
        ok = r["success"] and row["state"] == "STACKED"
        T("S28a", "IN_TRIP + offer_accepted → STACKED (stack accepted)", ok,
          f"state={row['state']}")
        rsp()

        # S28b — DECLINE while IN_TRIP stays IN_TRIP
        sp()
        force_state(cur, conn, "IN_TRIP",
            pickup_lat=PICKUP_LAT, pickup_lng=PICKUP_LNG,
            dropoff_lat=DROPOFF_LAT, dropoff_lng=DROPOFF_LNG)
        r = trans("offer_declined")
        ok = not r["success"]  # invalid transition — IN_TRIP+offer_declined not in table
        row = get_row(cur)
        ok = ok and row["state"] == "IN_TRIP"
        T("S28b", "IN_TRIP + offer_declined rejected → stays IN_TRIP", ok,
          f"state={row['state']} success={r['success']}")
        rsp()

        # ── SECTION 2: Heartbeat / GPS Convergence ────────────────────────────

        print("\n── Section 2: Heartbeat / GPS Convergence ──────────────────────────\n")

        # S29 — INITIAL_NAIL via heartbeat
        sp()
        force_state(cur, conn, "ENROUTE",
            pickup_lat=PICKUP_LAT, pickup_lng=PICKUP_LNG,
            dropoff_lat=DROPOFF_LAT, dropoff_lng=DROPOFF_LNG)
        state_row = get_row(cur)
        verdict, new_state, error_m = check_convergence(
            TEST_DRIVER, PICKUP_LAT, PICKUP_LNG, 1.0, state_row, cur
        )
        if verdict == "INITIAL_NAIL":
            r = trans("gps_convergence",
                nailed_pickup_lat=PICKUP_LAT, nailed_pickup_lng=PICKUP_LNG,
                nailed_pickup_error_m=error_m)
            row = get_row(cur)
            ok = r["success"] and row["state"] == "IN_TRIP" and row["nailed_pickup_lat"] is not None
        else:
            ok = False
        T("S29", f"ENROUTE + GPS at pickup → INITIAL_NAIL → IN_TRIP (verdict={verdict})", ok,
          f"state={get_row(cur)['state']} error_m={error_m}")
        rsp()

        # S29b — 800m from pickup → armed zone → REFINE_PICKUP, stays ENROUTE
        sp()
        force_state(cur, conn, "ENROUTE",
            pickup_lat=PICKUP_LAT, pickup_lng=PICKUP_LNG,
            dropoff_lat=DROPOFF_LAT, dropoff_lng=DROPOFF_LNG)
        state_row = get_row(cur)
        state_row = dict(state_row)  # make mutable
        state_row['pickup_address'] = "Main St & Texas Ave, Houston, Texas"  # precise → 200m confirm
        armed_lat = PICKUP_LAT + 0.0072  # ~800m north — inside armed zone, outside 200m confirm
        verdict29b, new_state29b, _ = check_convergence(
            TEST_DRIVER, armed_lat, PICKUP_LNG, 20.0, state_row, cur
        )
        ok = verdict29b == "REFINE_PICKUP" and get_row(cur)["state"] == "ENROUTE"
        T("S29b", f"ENROUTE + GPS 800m from pickup → REFINE_PICKUP, stays ENROUTE (verdict={verdict29b})", ok,
          f"state={get_row(cur)['state']} verdict={verdict29b}")
        rsp()

        # S29c — 150m + 10mph → inside confirm zone but fast → HOLD, stays ENROUTE
        sp()
        force_state(cur, conn, "ENROUTE",
            pickup_lat=PICKUP_LAT, pickup_lng=PICKUP_LNG,
            dropoff_lat=DROPOFF_LAT, dropoff_lng=DROPOFF_LNG)
        state_row = get_row(cur)
        confirm_lat = PICKUP_LAT + 0.00135  # ~150m north — inside confirm zone
        verdict29c, new_state29c, _ = check_convergence(
            TEST_DRIVER, confirm_lat, PICKUP_LNG, 10.0, state_row, cur
        )
        ok = verdict29c == "HOLD" and get_row(cur)["state"] == "ENROUTE"
        T("S29c", f"ENROUTE + GPS 150m at 10mph → HOLD (fast pass-through) (verdict={verdict29c})", ok,
          f"state={get_row(cur)['state']} verdict={verdict29c}")
        rsp()

        # S29d — 150m + 1mph → inside confirm zone + slow → INITIAL_NAIL
        sp()
        force_state(cur, conn, "ENROUTE",
            pickup_lat=PICKUP_LAT, pickup_lng=PICKUP_LNG,
            dropoff_lat=DROPOFF_LAT, dropoff_lng=DROPOFF_LNG)
        state_row = get_row(cur)
        verdict29d, new_state29d, error29d = check_convergence(
            TEST_DRIVER, confirm_lat, PICKUP_LNG, 1.0, state_row, cur
        )
        if verdict29d == "INITIAL_NAIL":
            r = trans("gps_convergence",
                nailed_pickup_lat=confirm_lat, nailed_pickup_lng=PICKUP_LNG,
                nailed_pickup_error_m=error29d)
            ok = r["success"] and get_row(cur)["state"] == "IN_TRIP"
        else:
            ok = False
        T("S29d", f"ENROUTE + GPS 150m at 1mph → INITIAL_NAIL → IN_TRIP (verdict={verdict29d})", ok,
          f"state={get_row(cur)['state']} verdict={verdict29d}")
        rsp()

        # S30 — ABORT removed: diverging at speed must stay ENROUTE
        sp()
        force_state(cur, conn, "ENROUTE",
            pickup_lat=PICKUP_LAT, pickup_lng=PICKUP_LNG,
            dropoff_lat=DROPOFF_LAT, dropoff_lng=DROPOFF_LNG)
        state_row = get_row(cur)
        far_lat = PICKUP_LAT + 0.015  # ~1.0 mile north
        verdict, _, _ = check_convergence(
            TEST_DRIVER, far_lat, PICKUP_LNG, 35.0, state_row, cur
        )
        ok = verdict != 'ABORT' and get_row(cur)["state"] == "ENROUTE"
        T("S30", f"ENROUTE + GPS diverging at speed → stays ENROUTE (no ABORT) (verdict={verdict})",
          ok, f"state={get_row(cur)['state']}")
        rsp()

        # S30b — ENROUTE + nailed pickup + diverging at speed → NO ABORT
        # Driver picked up passenger — diverging is normal driving toward dropoff
        sp()
        force_state(cur, conn, "ENROUTE",
            pickup_lat=PICKUP_LAT, pickup_lng=PICKUP_LNG,
            dropoff_lat=DROPOFF_LAT, dropoff_lng=DROPOFF_LNG,
            nailed_pickup_lat=PICKUP_LAT, nailed_pickup_lng=PICKUP_LNG,
            nailed_pickup_error_m=50.0)
        cur.execute("SET LOCAL app.skip_timestamp_trigger = 'true'")
        cur.execute("""
            UPDATE app_private.driver_trip_state
            SET state_updated_at = NOW() - INTERVAL '90 seconds'
            WHERE driver_id = %s
        """, (TEST_DRIVER,))
        conn.commit()
        state_row = get_row(cur)
        far_lat = PICKUP_LAT + 0.015  # ~1 mile away at speed
        verdict, _, _ = check_convergence(
            TEST_DRIVER, far_lat, PICKUP_LNG, 35.0, state_row, cur
        )
        ok = verdict != 'ABORT' and get_row(cur)["state"] == "ENROUTE"
        T("S30b", f"ENROUTE + nailed pickup + diverging at speed → NO ABORT (verdict={verdict})",
          ok, f"state={get_row(cur)['state']}")
        rsp()

        # S15 — GPS at dropoff → UNCOMMITTED
        sp()
        force_state(cur, conn, "IN_TRIP",
            pickup_lat=PICKUP_LAT, pickup_lng=PICKUP_LNG,
            dropoff_lat=DROPOFF_LAT, dropoff_lng=DROPOFF_LNG,
            nailed_pickup_lat=PICKUP_LAT, nailed_pickup_lng=PICKUP_LNG,
            nailed_pickup_error_m=50.0)
        state_row = get_row(cur)
        verdict, _, error_m = check_convergence(
            TEST_DRIVER, DROPOFF_LAT, DROPOFF_LNG, 1.0, state_row, cur
        )
        if verdict == "DROPOFF_NAIL":
            trans("approaching_dropoff")
            r = trans("dropoff_confirmed",
                nailed_dropoff_lat=DROPOFF_LAT, nailed_dropoff_lng=DROPOFF_LNG,
                nailed_dropoff_error_m=error_m,
                clear_coords=True)
            row = get_row(cur)
            ok = r["success"] and row["state"] == "UNCOMMITTED"
        else:
            ok = False
        T("S15", f"IN_TRIP→REFINE_DROPOFF→UNCOMMITTED via DROPOFF_NAIL (verdict={verdict})",
          ok, f"state={get_row(cur)['state']}")
        rsp()

        # S15a — IN_TRIP + GPS inside blast radius at HIGH speed → REFINE_DROPOFF armed
        sp()
        force_state(cur, conn, 'IN_TRIP',
            pickup_lat=PICKUP_LAT, pickup_lng=PICKUP_LNG,
            dropoff_lat=DROPOFF_LAT, dropoff_lng=DROPOFF_LNG,
            nailed_pickup_lat=PICKUP_LAT, nailed_pickup_lng=PICKUP_LNG,
            nailed_pickup_error_m=50.0)
        state_row = get_row(cur)
        # Simulate inside blast radius at 25mph — should arm regardless of speed
        verdict15a, _, error_m15a = check_convergence(
            TEST_DRIVER, DROPOFF_LAT, DROPOFF_LNG, 25.0, state_row, cur
        )
        ok = verdict15a == 'REFINE_DROPOFF'
        T('S15a', f'IN_TRIP + GPS at dropoff at 25mph → REFINE_DROPOFF armed (verdict={verdict15a})',
          ok, f'verdict={verdict15a}')
        rsp()

        # S15b — REFINE_DROPOFF + low speed at dropoff → DROPOFF_NAIL → UNCOMMITTED
        sp()
        force_state(cur, conn, 'REFINE_DROPOFF',
            pickup_lat=PICKUP_LAT, pickup_lng=PICKUP_LNG,
            dropoff_lat=DROPOFF_LAT, dropoff_lng=DROPOFF_LNG,
            nailed_pickup_lat=PICKUP_LAT, nailed_pickup_lng=PICKUP_LNG,
            nailed_pickup_error_m=50.0)
        state_row = get_row(cur)
        verdict15b, _, error_m15b = check_convergence(
            TEST_DRIVER, DROPOFF_LAT, DROPOFF_LNG, 1.0, state_row, cur
        )
        if verdict15b == 'DROPOFF_NAIL':
            r = trans('dropoff_confirmed',
                nailed_dropoff_lat=DROPOFF_LAT, nailed_dropoff_lng=DROPOFF_LNG,
                nailed_dropoff_error_m=error_m15b,
                clear_coords=True)
            row = get_row(cur)
            ok = r['success'] and row['state'] == 'UNCOMMITTED'
        else:
            ok = False
        T('S15b', f'REFINE_DROPOFF + GPS at dropoff at 1mph → DROPOFF_NAIL → UNCOMMITTED (verdict={verdict15b})',
          ok, f'state={get_row(cur)["state"]}')
        rsp()

        # S17 — STACKED + GPS at primary dropoff → ENROUTE
        sp()
        force_state(cur, conn, "STACKED",
            pickup_lat=PICKUP2_LAT, pickup_lng=PICKUP2_LNG,
            dropoff_lat=DROPOFF_LAT, dropoff_lng=DROPOFF_LNG)
        r = trans("gps_convergence",
            dropoff_lat=DROPOFF2_LAT, dropoff_lng=DROPOFF2_LNG,
            dropoff_h3=DROPOFF2_H3)
        row = get_row(cur)
        ok = r["success"] and row["state"] == "ENROUTE"
        T("S17", "STACKED + gps_convergence at primary dropoff → ENROUTE (buffer swap)", ok,
          f"state={row['state']}")
        rsp()

        # Heartbeat: no GPS — no transition
        sp()
        force_state(cur, conn, "ENROUTE",
            pickup_lat=PICKUP_LAT, pickup_lng=PICKUP_LNG)
        before = read()["state"]
        # No GPS means heartbeat returns early — state unchanged
        after = read()["state"]
        ok = before == after == "ENROUTE"
        T("HB01", "Heartbeat with no GPS — state unchanged", ok, f"state={after}")
        rsp()

        # Heartbeat: UNCOMMITTED, no nearby offer — no transition
        sp()
        before = read()["state"]
        cur.execute(
            "SELECT * FROM app_private.sm_find_nearby_offer(%s, %s, %s)",
            (TEST_DRIVER, DRIVER_LAT, DRIVER_LNG)
        )
        nearby = cur.fetchone()
        ok = before == "UNCOMMITTED" and nearby is None
        T("HB02", "Heartbeat UNCOMMITTED, no nearby offer — no transition", ok,
          f"nearby={nearby}")
        rsp()

        # Heartbeat: ENROUTE, GPS far from pickup — no transition yet
        sp()
        force_state(cur, conn, "ENROUTE",
            pickup_lat=PICKUP_LAT, pickup_lng=PICKUP_LNG,
            dropoff_lat=DROPOFF_LAT, dropoff_lng=DROPOFF_LNG)
        state_row = get_row(cur)
        verdict, _, _ = check_convergence(
            TEST_DRIVER, DRIVER_LAT, DRIVER_LNG, 25.0, state_row, cur
        )
        ok = verdict not in ("INITIAL_NAIL", "DROPOFF_NAIL") and get_row(cur)["state"] == "ENROUTE"
        T("HB03", f"Heartbeat ENROUTE, GPS far from pickup — no nail yet (verdict={verdict})",
          ok, f"state={get_row(cur)['state']}")
        rsp()

        # ── SECTION 3: S11/S12 Driver Override ───────────────────────────────

        print("\n── Section 3: S11/S12 Driver Override ──────────────────────────────\n")

        # S11 — GPS at declined offer pickup
        sp()
        seed_offer_history(cur, conn,
            "test_s11", PICKUP_LAT, PICKUP_LNG, PICKUP_H3,
            DROPOFF_LAT, DROPOFF_LNG, DROPOFF_H3)
        cur.execute(
            "SELECT * FROM app_private.sm_find_nearby_offer(%s, %s, %s)",
            (TEST_DRIVER, PICKUP_LAT, PICKUP_LNG)
        )
        nearby = cur.fetchone()
        if nearby:
            r = trans("offer_accepted",
                offer_id=str(nearby["offer_id"]),
                pickup_lat=nearby["pickup_lat"], pickup_lng=nearby["pickup_lng"],
                pickup_h3=nearby["pickup_h3"],
                dropoff_lat=nearby["dropoff_lat"], dropoff_lng=nearby["dropoff_lng"],
                dropoff_h3=nearby["dropoff_h3"])
            row = get_row(cur)
            ok = r["success"] and row["state"] == "ENROUTE"
        else:
            ok = False
        T("S11", "UNCOMMITTED + GPS near declined offer pickup → ENROUTE armed (not IN_TRIP)", ok,
          f"state={get_row(cur)['state']} nearby={'yes' if nearby else 'no'}")
        rsp()

        # S11a — Drive-by test: ACCEPT offer nearby should NOT trigger S11
        sp()
        seed_offer_history(cur, conn,
            "test_s11a", PICKUP_LAT, PICKUP_LNG, PICKUP_H3,
            DROPOFF_LAT, DROPOFF_LNG, DROPOFF_H3,
            verdict="ACCEPT")
        cur.execute(
            "SELECT * FROM app_private.sm_find_nearby_offer(%s, %s, %s)",
            (TEST_DRIVER, PICKUP_LAT, PICKUP_LNG)
        )
        nearby_accept = cur.fetchone()
        ok = nearby_accept is None
        T("S11a", "UNCOMMITTED + GPS near ACCEPTED offer pickup → no S11 trigger (stays UNCOMMITTED)",
          ok, f"nearby={'yes' if nearby_accept else 'no'}")
        rsp()

        # S11b — Missouri City drive-by: near declined pickup but should go ENROUTE not IN_TRIP
        sp()
        seed_offer_history(cur, conn,
            "test_s11b", PICKUP_LAT, PICKUP_LNG, PICKUP_H3,
            DROPOFF_LAT, DROPOFF_LNG, DROPOFF_H3,
            verdict="DECLINE")
        cur.execute(
            "SELECT * FROM app_private.sm_find_nearby_offer(%s, %s, %s)",
            (TEST_DRIVER, PICKUP_LAT, PICKUP_LNG)
        )
        nearby_decline = cur.fetchone()
        if nearby_decline:
            r = trans("offer_accepted",
                offer_id=str(nearby_decline["offer_id"]),
                pickup_lat=nearby_decline["pickup_lat"], pickup_lng=nearby_decline["pickup_lng"],
                pickup_h3=nearby_decline["pickup_h3"],
                dropoff_lat=nearby_decline["dropoff_lat"], dropoff_lng=nearby_decline["dropoff_lng"],
                dropoff_h3=nearby_decline["dropoff_h3"])
            row = get_row(cur)
            ok = r["success"] and row["state"] == "ENROUTE"
        else:
            ok = False
        T("S11b", "Missouri City drive-by: near declined pickup → ENROUTE armed (not IN_TRIP)",
          ok, f"state={get_row(cur)['state']}")
        rsp()

        # S12 — IN_TRIP + GPS at unexpected pickup
        sp()
        seed_offer_history(cur, conn,
            "test_s12", PICKUP2_LAT, PICKUP2_LNG, PICKUP2_H3,
            DROPOFF2_LAT, DROPOFF2_LNG, DROPOFF2_H3)
        force_state(cur, conn, "IN_TRIP",
            pickup_lat=PICKUP_LAT, pickup_lng=PICKUP_LNG,
            dropoff_lat=DROPOFF_LAT, dropoff_lng=DROPOFF_LNG)
        cur.execute(
            "SELECT * FROM app_private.sm_find_nearby_offer(%s, %s, %s)",
            (TEST_DRIVER, PICKUP2_LAT, PICKUP2_LNG)
        )
        nearby = cur.fetchone()
        if nearby:
            # S11 only fires when UNCOMMITTED — IN_TRIP must stay IN_TRIP
            # gps_convergence from IN_TRIP is now illegal (deleted from valid_state_transitions)
            r = trans("gps_convergence",
                offer_id=str(nearby["offer_id"]),
                pickup_lat=nearby["pickup_lat"], pickup_lng=nearby["pickup_lng"],
                pickup_h3=nearby["pickup_h3"],
                dropoff_lat=nearby["dropoff_lat"], dropoff_lng=nearby["dropoff_lng"],
                dropoff_h3=nearby["dropoff_h3"])
            row = get_row(cur)
            # IN_TRIP must stay IN_TRIP — S11 does not fire mid-trip
            ok = not r["success"] and row["state"] == "IN_TRIP"
        else:
            ok = False
        T("S12", "IN_TRIP + gps_convergence near declined pickup → stays IN_TRIP (S11 blocked mid-trip)", ok,
          f"state={get_row(cur)['state']} nearby={'yes' if nearby else 'no'}")
        rsp()

        # sm_find_nearby_offer — no match when far away
        sp()
        seed_offer_history(cur, conn,
            "test_far", PICKUP_LAT, PICKUP_LNG, PICKUP_H3,
            DROPOFF_LAT, DROPOFF_LNG, DROPOFF_H3)
        cur.execute(
            "SELECT * FROM app_private.sm_find_nearby_offer(%s, %s, %s)",
            (TEST_DRIVER, DRIVER_LAT, DRIVER_LNG)  # far from pickup
        )
        nearby = cur.fetchone()
        ok = nearby is None
        T("HB04", "sm_find_nearby_offer returns NULL when driver far from offer", ok,
          f"nearby={nearby}")
        rsp()

        # ── SECTION 4: sm_transition() Error Handling ─────────────────────────

        print("\n── Section 4: sm_transition() Error Handling ───────────────────────\n")

        # Invalid trigger
        sp()
        r = trans("totally_invalid_trigger")
        ok = not r["success"] and r["error"] is not None
        T("E01", "Invalid trigger → success=False, error set", ok,
          f"success={r['success']} error={r.get('error','')[:40]}")
        rsp()

        # Invalid transition (UNCOMMITTED → dropoff_confirmed)
        sp()
        r = trans("dropoff_confirmed")
        ok = not r["success"]
        T("E02", "Invalid transition UNCOMMITTED+dropoff_confirmed → rejected", ok,
          f"success={r['success']}")
        rsp()

        # Non-existent driver
        sp()
        cur.execute(
            "SELECT * FROM app_private.sm_transition(%s, %s)",
            ("DRIVER_DOES_NOT_EXIST_XYZ", "offer_accepted")
        )
        result = cur.fetchone()
        ok = not result["success"] and "not found" in (result.get("message") or result.get("error") or "").lower()
        T("E03", "Non-existent driver → success=False, Driver not found", ok,
          f"success={result['success']} error={result.get('error','')}")
        rsp()

        # sm_read on non-existent driver returns default
        sp()
        cur.execute("SELECT * FROM app_private.sm_read(%s)", ("DRIVER_DOES_NOT_EXIST_XYZ",))
        row = cur.fetchone()
        ok = row is None  # returns empty row set
        T("E04", "sm_read on non-existent driver → empty result (no crash)", ok,
          f"row={row}")
        rsp()

        # ── SECTION 5: sm_read() Computed Fields ──────────────────────────────

        print("\n── Section 5: sm_read() Computed Fields ────────────────────────────\n")

        # arc_center NULL when UNCOMMITTED
        sp()
        row = get_sm_read(cur)
        ok = row["state"] == "UNCOMMITTED" and row["arc_center_lat"] is None
        T("R01", "sm_read UNCOMMITTED → arc_center_lat is NULL", ok,
          f"arc_center_lat={row['arc_center_lat']}")
        rsp()

        # arc_center = nailed_pickup when ENROUTE + nailed
        sp()
        force_state(cur, conn, "ENROUTE",
            pickup_lat=PICKUP_LAT, pickup_lng=PICKUP_LNG,
            dropoff_lat=DROPOFF_LAT, dropoff_lng=DROPOFF_LNG,
            nailed_pickup_lat=PICKUP_LAT+0.001, nailed_pickup_lng=PICKUP_LNG+0.001)
        row = get_sm_read(cur)
        ok = (row["state"] == "ENROUTE" and
              abs(row["arc_center_lat"] - (PICKUP_LAT+0.001)) < 0.0001)
        T("R02", "sm_read ENROUTE + nailed → arc_center = nailed_pickup", ok,
          f"arc_center_lat={row['arc_center_lat']}")
        rsp()

        # arc_center = pickup when ENROUTE + not nailed
        sp()
        force_state(cur, conn, "ENROUTE",
            pickup_lat=PICKUP_LAT, pickup_lng=PICKUP_LNG,
            dropoff_lat=DROPOFF_LAT, dropoff_lng=DROPOFF_LNG)
        row = get_sm_read(cur)
        ok = (row["state"] == "ENROUTE" and
              abs(row["arc_center_lat"] - PICKUP_LAT) < 0.0001)
        T("R03", "sm_read ENROUTE + not nailed → arc_center = pickup_lat", ok,
          f"arc_center_lat={row['arc_center_lat']}")
        rsp()

        # arc_center = nailed_pickup when IN_TRIP
        sp()
        force_state(cur, conn, "IN_TRIP",
            nailed_pickup_lat=PICKUP_LAT, nailed_pickup_lng=PICKUP_LNG,
            dropoff_lat=DROPOFF_LAT, dropoff_lng=DROPOFF_LNG)
        row = get_sm_read(cur)
        ok = (row["state"] == "IN_TRIP" and
              abs(row["arc_center_lat"] - PICKUP_LAT) < 0.0001)
        T("R04", "sm_read IN_TRIP → arc_center = nailed_pickup", ok,
          f"arc_center_lat={row['arc_center_lat']}")
        rsp()

        # ── SECTION 6: State Log Integrity ───────────────────────────────────

        print("\n── Section 6: State Log Integrity ──────────────────────────────────\n")

        # Log entry written on transition
        sp()
        # Clear log entries written by setup/replay_harness
        cur.execute("DELETE FROM app_private.driver_trip_state_log WHERE driver_id = %s", (TEST_DRIVER,))
        conn.commit()
        before = get_log_count(cur)
        trans("offer_accepted",
            pickup_lat=PICKUP_LAT, pickup_lng=PICKUP_LNG,
            dropoff_lat=DROPOFF_LAT, dropoff_lng=DROPOFF_LNG)
        after = get_log_count(cur)
        ok = after == before + 1
        T("L01", "sm_transition writes exactly 1 log entry", ok,
          f"before={before} after={after}")
        rsp()

        # No log entry on rejected transition
        sp()
        cur.execute("DELETE FROM app_private.driver_trip_state_log WHERE driver_id = %s", (TEST_DRIVER,))
        conn.commit()
        before = get_log_count(cur)
        trans("dropoff_confirmed")  # invalid from UNCOMMITTED
        after = get_log_count(cur)
        ok = after == before
        T("L02", "Rejected transition writes 0 log entries", ok,
          f"before={before} after={after}")
        rsp()

        # Full solo trip: 3 transitions = 3 log entries
        sp()
        cur.execute("DELETE FROM app_private.driver_trip_state_log WHERE driver_id = %s", (TEST_DRIVER,))
        conn.commit()
        before = get_log_count(cur)
        trans("offer_accepted",
            pickup_lat=PICKUP_LAT, pickup_lng=PICKUP_LNG,
            dropoff_lat=DROPOFF_LAT, dropoff_lng=DROPOFF_LNG)
        trans("gps_convergence",
            nailed_pickup_lat=PICKUP_LAT, nailed_pickup_lng=PICKUP_LNG,
            nailed_pickup_error_m=50.0)
        trans("approaching_dropoff")
        trans("dropoff_confirmed",
            nailed_dropoff_lat=DROPOFF_LAT, nailed_dropoff_lng=DROPOFF_LNG,
            nailed_dropoff_error_m=40.0,
            clear_coords=True)
        after = get_log_count(cur)
        row = get_row(cur)
        ok = after == before + 4 and row["state"] == "UNCOMMITTED"
        T("L03", "Full solo trip: 4 transitions → 4 log entries → UNCOMMITTED", ok,
          f"log_delta={after-before} state={row['state']}")
        rsp()

        # ── SECTION 7: clear_coords Integrity ────────────────────────────────

        print("\n── Section 7: clear_coords Integrity ───────────────────────────────\n")

        # clear_coords=True NULLs all coord columns
        sp()
        force_state(cur, conn, "IN_TRIP",
            pickup_lat=PICKUP_LAT, pickup_lng=PICKUP_LNG, pickup_h3=PICKUP_H3,
            dropoff_lat=DROPOFF_LAT, dropoff_lng=DROPOFF_LNG, dropoff_h3=DROPOFF_H3,
            nailed_pickup_lat=PICKUP_LAT, nailed_pickup_lng=PICKUP_LNG,
            nailed_pickup_error_m=50.0)
        trans("approaching_dropoff")
        trans("dropoff_confirmed", clear_coords=True)
        row = get_row(cur)
        coord_cols = [
            row["pickup_lat"], row["pickup_lng"], row["pickup_h3"],
            row["dropoff_lat"], row["dropoff_lng"], row["dropoff_h3"],
            row["nailed_pickup_lat"], row["nailed_pickup_lng"],
        ]
        ok = all(v is None for v in coord_cols)
        T("C01", "clear_coords=True NULLs all coord columns", ok,
          f"pickup_lat={row['pickup_lat']} nailed_pickup_lat={row['nailed_pickup_lat']}")
        rsp()

        # clear_coords=False preserves coords
        sp()
        force_state(cur, conn, "ENROUTE",
            pickup_lat=PICKUP_LAT, pickup_lng=PICKUP_LNG,
            dropoff_lat=DROPOFF_LAT, dropoff_lng=DROPOFF_LNG)
        trans("gps_convergence",
            nailed_pickup_lat=PICKUP_LAT, nailed_pickup_lng=PICKUP_LNG,
            nailed_pickup_error_m=50.0)
        row = get_row(cur)
        ok = row["pickup_lat"] is not None and row["dropoff_lat"] is not None
        T("C02", "clear_coords=False preserves existing coords", ok,
          f"pickup_lat={row['pickup_lat']} dropoff_lat={row['dropoff_lat']}")
        rsp()

        # ── SECTION 8: Full Sequence Tests ───────────────────────────────────

        print("\n── Section 8: Full Sequence Tests ──────────────────────────────────\n")

        # Full stacked trip sequence
        sp()
        # UNCOMMITTED → ENROUTE
        r1 = trans("offer_accepted",
            pickup_lat=PICKUP_LAT, pickup_lng=PICKUP_LNG,
            dropoff_lat=DROPOFF_LAT, dropoff_lng=DROPOFF_LNG)
        # ENROUTE → IN_TRIP
        r2 = trans("gps_convergence",
            nailed_pickup_lat=PICKUP_LAT, nailed_pickup_lng=PICKUP_LNG,
            nailed_pickup_error_m=50.0)
        # IN_TRIP → STACKED
        r3 = trans("offer_accepted",
            pickup_lat=PICKUP2_LAT, pickup_lng=PICKUP2_LNG)
        # STACKED → IN_TRIP (pickup_confirmed on secondary)
        r4 = trans("pickup_confirmed",
            nailed_pickup_lat=PICKUP2_LAT, nailed_pickup_lng=PICKUP2_LNG,
            nailed_pickup_error_m=30.0)
        # IN_TRIP → REFINE_DROPOFF → UNCOMMITTED
        trans("approaching_dropoff")
        r5 = trans("dropoff_confirmed", clear_coords=True)
        row = get_row(cur)
        ok = all(r["success"] for r in [r1,r2,r3,r4,r5]) and row["state"] == "UNCOMMITTED"
        T("SEQ1", "Full stacked trip: UNCOMMITTED→ENROUTE→IN_TRIP→STACKED→IN_TRIP→REFINE_DROPOFF→UNCOMMITTED",
          ok, f"state={row['state']} all_success={ok}")
        rsp()

        # Watchdog recovery sequence
        sp()
        trans("offer_accepted",
            pickup_lat=PICKUP_LAT, pickup_lng=PICKUP_LNG,
            dropoff_lat=DROPOFF_LAT, dropoff_lng=DROPOFF_LNG)
        trans("gps_convergence",
            nailed_pickup_lat=PICKUP_LAT, nailed_pickup_lng=PICKUP_LNG,
            nailed_pickup_error_m=50.0)
        r_wd = trans("watchdog_auto_reset", clear_coords=True)
        r_new = trans("offer_accepted",
            pickup_lat=PICKUP2_LAT, pickup_lng=PICKUP2_LNG,
            dropoff_lat=DROPOFF2_LAT, dropoff_lng=DROPOFF2_LNG)
        row = get_row(cur)
        ok = r_wd["success"] and r_new["success"] and row["state"] == "ENROUTE"
        T("SEQ2", "Watchdog recovery: IN_TRIP→watchdog→UNCOMMITTED→new offer→ENROUTE",
          ok, f"state={row['state']}")
        rsp()

        # Manual reset from STACKED
        sp()
        trans("offer_accepted",
            pickup_lat=PICKUP_LAT, pickup_lng=PICKUP_LNG,
            dropoff_lat=DROPOFF_LAT, dropoff_lng=DROPOFF_LNG)
        trans("gps_convergence",
            nailed_pickup_lat=PICKUP_LAT, nailed_pickup_lng=PICKUP_LNG,
            nailed_pickup_error_m=50.0)
        trans("offer_accepted", pickup_lat=PICKUP2_LAT, pickup_lng=PICKUP2_LNG)
        r_reset = trans("manual_reset", clear_coords=True)
        row = get_row(cur)
        ok = r_reset["success"] and row["state"] == "UNCOMMITTED" and row["pickup_lat"] is None
        T("SEQ3", "Manual reset from STACKED → UNCOMMITTED (all coords cleared)", ok,
          f"state={row['state']} pickup_lat={row['pickup_lat']}")
        rsp()

        # S20 — manual Nail It after Auto Nail It (IN_TRIP stays IN_TRIP)
        sp()
        force_state(cur, conn, "IN_TRIP",
            pickup_lat=PICKUP_LAT, pickup_lng=PICKUP_LNG,
            nailed_pickup_lat=PICKUP_LAT, nailed_pickup_lng=PICKUP_LNG,
            nailed_pickup_error_m=200.0)  # wide first nail
        # Second nail with better accuracy — no state change, coords update
        cur.execute("""
            UPDATE app_private.driver_trip_state
            SET nailed_pickup_lat     = %s,
                nailed_pickup_lng     = %s,
                nailed_pickup_error_m = %s
            WHERE driver_id = %s
        """, (PICKUP_LAT+0.0001, PICKUP_LNG+0.0001, 25.0, TEST_DRIVER))
        conn.commit()
        row = get_row(cur)
        ok = row["state"] == "IN_TRIP" and row["nailed_pickup_error_m"] == 25.0
        T("S20", "Manual Nail It while IN_TRIP → coords updated, state stays IN_TRIP", ok,
          f"state={row['state']} error_m={row['nailed_pickup_error_m']}")
        rsp()

        # S21 — pickup_confirm while UNCOMMITTED (endpoint guard)
        sp()
        # UNCOMMITTED + pickup_confirmed is not in valid_state_transitions
        r = trans("pickup_confirmed",
            nailed_pickup_lat=PICKUP_LAT, nailed_pickup_lng=PICKUP_LNG,
            nailed_pickup_error_m=50.0)
        row = get_row(cur)
        ok = not r["success"] and row["state"] == "UNCOMMITTED"
        T("S21", "pickup_confirmed while UNCOMMITTED → rejected, stays UNCOMMITTED", ok,
          f"state={row['state']} success={r['success']}")
        rsp()

        # S22 — reset logs from_state correctly
        sp()
        force_state(cur, conn, "STACKED",
            pickup_lat=PICKUP_LAT, pickup_lng=PICKUP_LNG)
        cur.execute("DELETE FROM app_private.driver_trip_state_log WHERE driver_id = %s", (TEST_DRIVER,))
        conn.commit()
        before_count = get_log_count(cur)
        trans("manual_reset", clear_coords=True)
        after_count = get_log_count(cur)
        cur.execute("""
            SELECT from_state, to_state, trigger_event
            FROM app_private.driver_trip_state_log
            WHERE driver_id = %s
            ORDER BY logged_at DESC LIMIT 1
        """, (TEST_DRIVER,))
        log_row = cur.fetchone()
        ok = (after_count == before_count + 1 and
              log_row["from_state"] == "STACKED" and
              log_row["to_state"] == "UNCOMMITTED" and
              log_row["trigger_event"] == "manual_reset")
        T("S22", "manual_reset from STACKED logs correct from_state in state_log", ok,
          f"from={log_row['from_state']} to={log_row['to_state']} trigger={log_row['trigger_event']}")
        rsp()

    finally:
        conn.rollback()
        # Clean up test driver row
        try:
            cur.execute("DELETE FROM app_private.driver_trip_state WHERE driver_id = %s",
                        (TEST_DRIVER,))
            cur.execute("DELETE FROM app_private.driver_trip_state_log WHERE driver_id = %s",
                        (TEST_DRIVER,))
            cur.execute("DELETE FROM app_private.decision_log WHERE driver_id = %s",
                        (TEST_DRIVER,))
            conn.commit()
        except Exception:
            pass
        conn.close()

    # ── Results ───────────────────────────────────────────────────────────────
    passed = sum(1 for _, _, ok, _ in results if ok)
    failed = len(results) - passed

    print(f"\n{'─'*70}")
    if failed:
        print(f"\n  Failures:")
        for sid, desc, ok, detail in results:
            if not ok:
                print(f"  ❌  {sid:<5}  {desc}")
                print(f"         {detail}")

    print(f"\n{'─'*70}")
    print(f"  {passed}/{len(results)} passed  |  {failed} failed")
    print(f"{'='*70}\n")
    return failed == 0


if __name__ == "__main__":
    ok = run_tests()
    sys.exit(0 if ok else 1)
