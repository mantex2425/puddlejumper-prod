"""
backtest_state_machine.py — PuddleJumper State Machine Backtest
Revision: puddlejumper-api-00442-hlr
Tests 13 scenarios against trip_state.py functions directly.
Run from: ~/puddlejumper-prod/
Usage:    python3 backtest_state_machine.py
"""

import psycopg2
import psycopg2.extras
import logging
import sys
from _DEPRECATED_trip_state import (
    update_driver_state,
    get_driver_state,
    confirm_pickup_arrival,
    confirm_dropoff_arrival,
    reset_state,
    CONVERGENCE_THRESHOLD_MILES
)

logging.basicConfig(level=logging.WARNING)  # suppress noise during backtest

DB_CONFIG = {
    "host":     "10.128.0.2",
    "user":     "postgres",
    "dbname":   "puddlejumper",
    "cursor_factory": psycopg2.extras.RealDictCursor
}

DRIVER_ID  = "BACKTEST_DRIVER_001"
OFFER_A    = "offer-primary-001"
OFFER_B    = "offer-secondary-002"
OFFER_C    = "offer-third-003"

# ── Real-ish Houston coordinates ──────────────────────────────────────────
DRIVER_HOME   = (29.6900, -95.4500)   # Rosharon area
PICKUP_A      = (29.7604, -95.3698)   # Downtown Houston
DROPOFF_A     = (29.7808, -95.3792)   # Midtown
PICKUP_B      = (29.7900, -95.3900)   # Montrose
DROPOFF_B     = (29.8100, -95.4100)   # Heights
PICKUP_C      = (29.8200, -95.4200)   # Washington Ave
NEARBY_OFFSET = 0.001                 # ~110m — within convergence threshold

results = []

def get_conn():
    return psycopg2.connect(**DB_CONFIG)

def check(scenario_id, description, actual, expected, note=""):
    passed = actual == expected
    status = "✅ PASS" if passed else "❌ FAIL"
    results.append({
        "id":     scenario_id,
        "desc":   description,
        "status": status,
        "actual": actual,
        "expect": expected,
        "note":   note
    })
    print(f"{status} | {scenario_id} | {description}")
    if not passed:
        print(f"       Expected: {expected}")
        print(f"       Actual:   {actual}")
    if note:
        print(f"       Note: {note}")

def seed_driver(conn, state, offer_id,
                pickup=PICKUP_A, dropoff=DROPOFF_A,
                potential_cancellation=False):
    """Force driver into a specific state for scenario setup.
    DELETE first so the INSERT is always fresh — avoids the
    enforce_state_transition trigger which only fires on UPDATE.
    """
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM app_private.driver_trip_state WHERE driver_id = %s",
            (DRIVER_ID,)
        )
        cur.execute("""
            INSERT INTO app_private.driver_trip_state (
                driver_id, current_offer_id, state,
                pickup_h3, pickup_lat, pickup_lng,
                dropoff_h3, dropoff_lat, dropoff_lng,
                potential_cancellation, state_updated_at
            ) VALUES (
                %s, %s, %s,
                app_private.coords_to_h3(%s, %s), %s, %s,
                app_private.coords_to_h3(%s, %s), %s, %s,
                %s, NOW() AT TIME ZONE 'America/Chicago'
            )
        """, (
            DRIVER_ID, offer_id, state,
            pickup[0], pickup[1], pickup[0], pickup[1],
            dropoff[0], dropoff[1], dropoff[0], dropoff[1],
            potential_cancellation
        ))
    conn.commit()

def get_state_row(conn):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT state, potential_cancellation, dropoff_lat, dropoff_lng
            FROM app_private.driver_trip_state
            WHERE driver_id = %s
        """, (DRIVER_ID,))
        row = cur.fetchone()
        return dict(row) if row else None

def seed_offer_history(conn, offer_id, pickup, dropoff, verdict="DECLINE"):
    """Seed a recent offer into offer_history via decision_log for S11/S12 scan."""
    with conn.cursor() as cur:
        # Insert decision_log row
        cur.execute("""
            INSERT INTO app_private.decision_log (
                driver_id, fare, pickup_minutes, trip_minutes,
                pickup_lat, pickup_lng, dropoff_lat, dropoff_lng,
                pickup_h3_index, dropoff_h3_index, ping_h3_index,
                created_at
            ) VALUES (
                %s, 12.50, 5, 15,
                %s, %s, %s, %s,
                app_private.coords_to_h3(%s, %s),
                app_private.coords_to_h3(%s, %s),
                app_private.coords_to_h3(%s, %s),
                NOW() AT TIME ZONE 'America/Chicago'
            ) RETURNING id
        """, (
            DRIVER_ID,
            pickup[0], pickup[1], dropoff[0], dropoff[1],
            pickup[0], pickup[1],
            dropoff[0], dropoff[1],
            pickup[0], pickup[1]
        ))
        dl_id = cur.fetchone()["id"]

        # Insert offer_history row
        cur.execute("""
            INSERT INTO app_private.offer_history (
                decision_log_id, pickup_lat, pickup_lng, pickup_h3,
                dropoff_lat, dropoff_lng, dropoff_h3,
                fare, pickup_miles, trip_miles,
                pickup_minutes, trip_minutes,
                app_verdict, created_at
            ) VALUES (
                %s, %s, %s, app_private.coords_to_h3(%s, %s),
                %s, %s, app_private.coords_to_h3(%s, %s),
                12.50, 2.5, 8.0, 5, 15,
                %s, NOW() AT TIME ZONE 'America/Chicago'
            )
        """, (
            dl_id,
            pickup[0], pickup[1], pickup[0], pickup[1],
            dropoff[0], dropoff[1], dropoff[0], dropoff[1],
            verdict
        ))
    conn.commit()

def wipe_driver(conn):
    with conn.cursor() as cur:
        cur.execute("DELETE FROM app_private.driver_trip_state WHERE driver_id = %s", (DRIVER_ID,))
        cur.execute("""
            DELETE FROM app_private.offer_history
            WHERE decision_log_id IN (
                SELECT id FROM app_private.decision_log WHERE driver_id = %s
            )
        """, (DRIVER_ID,))
        cur.execute("DELETE FROM app_private.decision_log WHERE driver_id = %s", (DRIVER_ID,))
        cur.execute("DELETE FROM app_private.driver_trip_state_log WHERE driver_id = %s", (DRIVER_ID,))
    conn.commit()

# ═════════════════════════════════════════════════════════════════════════════
print("\n🧪 PuddleJumper State Machine Backtest")
print("=" * 60)

conn = get_conn()
wipe_driver(conn)

# ── S01: Start of shift ───────────────────────────────────────────────────
print("\n── S01")
with conn.cursor() as cur:
    result = get_driver_state(DRIVER_ID, *DRIVER_HOME, cur)
conn.commit()
check("S01", "Fresh driver → UNCOMMITTED", result["state"], "UNCOMMITTED")
check("S01", "Arc center = driver GPS", 
      (result["arc_center_lat"], result["arc_center_lng"]), DRIVER_HOME)

# ── S02: UNCOMMITTED + DECLINE ────────────────────────────────────────────
print("\n── S02")
wipe_driver(conn)
with conn.cursor() as cur:
    update_driver_state(
        DRIVER_ID, OFFER_A, "DECLINE",
        "h3_pickup", PICKUP_A[0], PICKUP_A[1],
        "h3_drop",   DROPOFF_A[0], DROPOFF_A[1],
        "UNCOMMITTED", cur
    )
conn.commit()
row = get_state_row(conn)
check("S02", "UNCOMMITTED + DECLINE → UNCOMMITTED", row["state"], "UNCOMMITTED")
check("S02", "potential_cancellation = FALSE", row["potential_cancellation"], False)

# ── S03: UNCOMMITTED + ACCEPT → ENROUTE ──────────────────────────────────
print("\n── S03")
wipe_driver(conn)
with conn.cursor() as cur:
    update_driver_state(
        DRIVER_ID, OFFER_A, "ACCEPT",
        "h3_pickup", PICKUP_A[0], PICKUP_A[1],
        "h3_drop",   DROPOFF_A[0], DROPOFF_A[1],
        "UNCOMMITTED", cur
    )
conn.commit()
row = get_state_row(conn)
check("S03", "UNCOMMITTED + ACCEPT → ENROUTE", row["state"], "ENROUTE")
check("S03", "Arc center = dropoff (not pickup)",
      (row["dropoff_lat"], row["dropoff_lng"]), DROPOFF_A)
check("S03", "potential_cancellation = FALSE", row["potential_cancellation"], False)

# ── S04: ENROUTE + new offer → UNCOMMITTED ───────────────────────────────
print("\n── S04")
seed_driver(conn, "ENROUTE", OFFER_A, PICKUP_A, DROPOFF_A)
with conn.cursor() as cur:
    update_driver_state(
        DRIVER_ID, OFFER_B, "DECLINE",
        "h3_pickup", PICKUP_B[0], PICKUP_B[1],
        "h3_drop",   DROPOFF_B[0], DROPOFF_B[1],
        "ENROUTE", cur
    )
conn.commit()
row = get_state_row(conn)
check("S04", "ENROUTE + new offer → UNCOMMITTED", row["state"], "UNCOMMITTED")
check("S04", "potential_cancellation = FALSE", row["potential_cancellation"], False)

# ── S06: STACKED + 3rd offer → stay STACKED + flag ───────────────────────
print("\n── S06")
seed_driver(conn, "STACKED", OFFER_B, PICKUP_B, DROPOFF_A, False)
with conn.cursor() as cur:
    update_driver_state(
        DRIVER_ID, OFFER_C, "DECLINE",
        "h3_pickup", PICKUP_C[0], PICKUP_C[1],
        "h3_drop",   DROPOFF_B[0], DROPOFF_B[1],
        "STACKED", cur
    )
conn.commit()
row = get_state_row(conn)
check("S06", "STACKED + 3rd offer → stays STACKED", row["state"], "STACKED")
check("S06", "potential_cancellation = TRUE", row["potential_cancellation"], True)

# ── S07: STACKED + Nail It pickup → IN_TRIP + flag cleared ───────────────
print("\n── S07")
seed_driver(conn, "STACKED", OFFER_B, PICKUP_B, DROPOFF_A, True)
with conn.cursor() as cur:
    confirm_pickup_arrival(DRIVER_ID, OFFER_B, PICKUP_B[0], PICKUP_B[1], cur)
conn.commit()
row = get_state_row(conn)
check("S07", "STACKED + Nail It → IN_TRIP", row["state"], "IN_TRIP")
check("S07", "potential_cancellation cleared = FALSE", row["potential_cancellation"], False)

# ── S08: IN_TRIP solo + dropoff Nail It → UNCOMMITTED ────────────────────
print("\n── S08")
seed_driver(conn, "IN_TRIP", OFFER_A, PICKUP_A, DROPOFF_A)
with conn.cursor() as cur:
    confirm_dropoff_arrival(DRIVER_ID, DROPOFF_A[0], DROPOFF_A[1], cur)
conn.commit()
row = get_state_row(conn)
check("S08", "IN_TRIP solo dropoff → UNCOMMITTED", row["state"], "UNCOMMITTED")
check("S08", "potential_cancellation = FALSE", row["potential_cancellation"], False)

# ── S09: STACKED + dropoff Nail It → ENROUTE (buffer swap) ───────────────
print("\n── S09")
seed_driver(conn, "STACKED", OFFER_B, PICKUP_B, DROPOFF_A)
seed_offer_history(conn, OFFER_B, PICKUP_B, DROPOFF_B, verdict="ACCEPT")
with conn.cursor() as cur:
    confirm_dropoff_arrival(DRIVER_ID, DROPOFF_A[0], DROPOFF_A[1], cur)
conn.commit()
row = get_state_row(conn)
check("S09", "STACKED dropoff → ENROUTE", row["state"], "ENROUTE")
check("S09", "Arc center = secondary dropoff",
      (row["dropoff_lat"], row["dropoff_lng"]), DROPOFF_B,
      note="Buffer swap: secondary ride dropoff fetched from offer_history")

# ── S11: UNCOMMITTED + GPS at declined offer pickup → IN_TRIP ─────────────
print("\n── S11")
wipe_driver(conn)
seed_driver(conn, "UNCOMMITTED", None, DRIVER_HOME, DRIVER_HOME)
seed_offer_history(conn, OFFER_A, PICKUP_A, DROPOFF_A, verdict="DECLINE")
# Driver GPS arrives at PICKUP_A (the declined offer)
gps = (PICKUP_A[0] + NEARBY_OFFSET, PICKUP_A[1] + NEARBY_OFFSET)
with conn.cursor() as cur:
    result = get_driver_state(DRIVER_ID, gps[0], gps[1], cur)
conn.commit()
check("S11", "UNCOMMITTED + GPS at declined pickup → IN_TRIP",
      result["state"], "IN_TRIP",
      note="_find_nearby_recent_offer scan")
check("S11", "Arc center = declined offer dropoff",
      (result["arc_center_lat"], result["arc_center_lng"]), DROPOFF_A)

# ── S12: IN_TRIP + GPS at unexpected pickup → IN_TRIP (promoted) ──────────
print("\n── S12")
seed_driver(conn, "IN_TRIP", OFFER_A, PICKUP_A, DROPOFF_A)
seed_offer_history(conn, OFFER_B, PICKUP_B, DROPOFF_B, verdict="DECLINE")
# Driver GPS arrives at PICKUP_B (unexpected secondary pickup)
gps = (PICKUP_B[0] + NEARBY_OFFSET, PICKUP_B[1] + NEARBY_OFFSET)
with conn.cursor() as cur:
    result = get_driver_state(DRIVER_ID, gps[0], gps[1], cur)
conn.commit()
row = get_state_row(conn)
check("S12", "IN_TRIP + GPS at unexpected pickup → IN_TRIP",
      row["state"], "IN_TRIP",
      note="Primary cancelled — secondary promoted")
check("S12", "Arc center = secondary dropoff",
      (result["arc_center_lat"], result["arc_center_lng"]), DROPOFF_B)

# ── S13: Manual reset from any state ─────────────────────────────────────
print("\n── S13")
seed_driver(conn, "STACKED", OFFER_B, PICKUP_B, DROPOFF_A)
with conn.cursor() as cur:
    reset_state(DRIVER_ID, cur, "manual_reset")
conn.commit()
row = get_state_row(conn)
check("S13", "Manual reset → UNCOMMITTED", row["state"], "UNCOMMITTED")
check("S13", "potential_cancellation = FALSE", row["potential_cancellation"], False)

# ── S14: Watchdog auto-reset ──────────────────────────────────────────────
print("\n── S14")
seed_driver(conn, "IN_TRIP", OFFER_A, PICKUP_A, DROPOFF_A)
with conn.cursor() as cur:
    reset_state(DRIVER_ID, cur, "watchdog_auto_reset")
conn.commit()
row = get_state_row(conn)
check("S14", "Watchdog reset → UNCOMMITTED", row["state"], "UNCOMMITTED")

# ── S15: IN_TRIP + GPS at dropoff → UNCOMMITTED (no Nail It) ─────────────
print("\n── S15")
seed_driver(conn, "IN_TRIP", OFFER_A, PICKUP_A, DROPOFF_A)
gps = (DROPOFF_A[0] + NEARBY_OFFSET, DROPOFF_A[1] + NEARBY_OFFSET)
with conn.cursor() as cur:
    result = get_driver_state(DRIVER_ID, gps[0], gps[1], cur)
conn.commit()
check("S15", "IN_TRIP + GPS at dropoff → UNCOMMITTED",
      result["state"], "UNCOMMITTED",
      note="GPS convergence without Nail It tap")

# ─────────────────────────────────────────────────────────────────────────────

# ── S16: Duplicate offer detection ───────────────────────────────────────
print("\n── S16")
wipe_driver(conn)
seed_driver(conn, "UNCOMMITTED", None, DRIVER_HOME, DRIVER_HOME)
# First offer — should be accepted and state set to ENROUTE
with conn.cursor() as cur:
    update_driver_state(
        DRIVER_ID, OFFER_A, "ACCEPT",
        "h3_pickup", PICKUP_A[0], PICKUP_A[1],
        "h3_drop",   DROPOFF_A[0], DROPOFF_A[1],
        "UNCOMMITTED", cur
    )
conn.commit()
# Second offer — same pickup location within 30 seconds, should be skipped
with conn.cursor() as cur:
    update_driver_state(
        DRIVER_ID, OFFER_B, "ACCEPT",
        "h3_pickup", PICKUP_A[0] + 0.0005, PICKUP_A[1] + 0.0005,
        "h3_drop",   DROPOFF_B[0], DROPOFF_B[1],
        "ENROUTE", cur
    )
conn.commit()
row = get_state_row(conn)
check("S16", "Duplicate offer skipped — state stays ENROUTE", row["state"], "ENROUTE",
      note="Second offer within 160m of first pickup — silently dropped")

# ── S17: STACKED + GPS at primary dropoff → ENROUTE (no Nail It) ─────────
print("\n── S17")
seed_driver(conn, "STACKED", OFFER_B, PICKUP_B, DROPOFF_A)
seed_offer_history(conn, OFFER_B, PICKUP_B, DROPOFF_B, verdict="ACCEPT")
gps = (DROPOFF_A[0] + NEARBY_OFFSET, DROPOFF_A[1] + NEARBY_OFFSET)
with conn.cursor() as cur:
    result = get_driver_state(DRIVER_ID, gps[0], gps[1], cur)
conn.commit()
row = get_state_row(conn)
check("S17", "STACKED + GPS at primary dropoff → ENROUTE", row["state"], "ENROUTE",
      note="GPS convergence without Nail It — pivots to secondary ride")
check("S17", "Arc center = secondary dropoff",
      (result["arc_center_lat"], result["arc_center_lng"]), DROPOFF_B)

# ── S18: ENROUTE + GPS at pickup → IN_TRIP (no Nail It) ──────────────────
print("\n── S18")
seed_driver(conn, "ENROUTE", OFFER_A, PICKUP_A, DROPOFF_A)
gps = (PICKUP_A[0] + NEARBY_OFFSET, PICKUP_A[1] + NEARBY_OFFSET)
with conn.cursor() as cur:
    result = get_driver_state(DRIVER_ID, gps[0], gps[1], cur)
conn.commit()
check("S18", "ENROUTE + GPS at pickup → IN_TRIP", result["state"], "IN_TRIP",
      note="GPS convergence path vs pickup_confirmed path")
check("S18", "Arc center = dropoff after convergence",
      (result["arc_center_lat"], result["arc_center_lng"]), DROPOFF_A)

# ── S20: NULL pickup coords — GPS convergence must not fire ───────────────
print("\n── S20")
wipe_driver(conn)
# Seed ENROUTE with NULL pickup coords (hallucinated geocode scenario)
with conn.cursor() as cur:
    cur.execute("""
        INSERT INTO app_private.driver_trip_state (
            driver_id, current_offer_id, state,
            pickup_h3, pickup_lat, pickup_lng,
            dropoff_h3, dropoff_lat, dropoff_lng,
            potential_cancellation, state_updated_at
        ) VALUES (
            %s, %s, 'ENROUTE',
            NULL, NULL, NULL,
            NULL, NULL, NULL,
            FALSE, NOW() AT TIME ZONE 'America/Chicago'
        )
    """, (DRIVER_ID, OFFER_A))
conn.commit()
# GPS arrives at what would be pickup — should NOT converge (no coords to check against)
gps = (PICKUP_A[0] + NEARBY_OFFSET, PICKUP_A[1] + NEARBY_OFFSET)
with conn.cursor() as cur:
    result = get_driver_state(DRIVER_ID, gps[0], gps[1], cur)
conn.commit()
check("S20", "NULL coords — GPS convergence does not fire", result["state"], "ENROUTE",
      note="Hallucinated geocode — stays ENROUTE until manual reset or watchdog")

# ── S22: Manual reset from UNCOMMITTED → UNCOMMITTED (no-op) ─────────────
print("\n── S22")
seed_driver(conn, "UNCOMMITTED", None, DRIVER_HOME, DRIVER_HOME)
with conn.cursor() as cur:
    reset_state(DRIVER_ID, cur, "manual_reset")
conn.commit()
row = get_state_row(conn)
check("S22", "Reset from UNCOMMITTED → UNCOMMITTED (no-op)", row["state"], "UNCOMMITTED",
      note="Must not throw — valid transition exists in DB")

# ── S23: Two different offers with nearby pickups — second must process ───
print("\n── S23")
wipe_driver(conn)
seed_driver(conn, "UNCOMMITTED", None, DRIVER_HOME, DRIVER_HOME)
# First offer accepted
with conn.cursor() as cur:
    update_driver_state(
        DRIVER_ID, OFFER_A, "ACCEPT",
        "h3_pickup", PICKUP_A[0], PICKUP_A[1],
        "h3_drop",   DROPOFF_A[0], DROPOFF_A[1],
        "UNCOMMITTED", cur
    )
conn.commit()
# Simulate 35 seconds passing by backdating state_updated_at
with conn.cursor() as cur:
    cur.execute("""
        UPDATE app_private.driver_trip_state
        SET state_updated_at = NOW() AT TIME ZONE 'America/Chicago' - INTERVAL '35 seconds'
        WHERE driver_id = %s
    """, (DRIVER_ID,))
conn.commit()
# Second offer — nearby pickup but different offer, outside 30-second window
PICKUP_A_NEARBY = (PICKUP_A[0] + 0.0005, PICKUP_A[1] + 0.0005)  # ~80m away
with conn.cursor() as cur:
    update_driver_state(
        DRIVER_ID, OFFER_B, "ACCEPT",
        "h3_pickup", PICKUP_A_NEARBY[0], PICKUP_A_NEARBY[1],
        "h3_drop",   DROPOFF_B[0], DROPOFF_B[1],
        "ENROUTE", cur
    )
conn.commit()
row = get_state_row(conn)
check("S23", "Second nearby offer outside 30s window — ENROUTE+ACCEPT blocked by design", row["state"], "ENROUTE",
      note="Duplicate guard passed (35s elapsed) but ENROUTE+ACCEPT is intentionally blocked — Uber can't stack from ENROUTE")

# ── S24: Nail It fires after watchdog already reset ───────────────────────
print("\n── S24")
seed_driver(conn, "UNCOMMITTED", None, DRIVER_HOME, DRIVER_HOME)
# Watchdog already fired — state is UNCOMMITTED
# Now Nail It tries pickup_confirmed — not a valid transition from UNCOMMITTED
try:
    with conn.cursor() as cur:
        confirm_pickup_arrival(DRIVER_ID, OFFER_A, PICKUP_A[0], PICKUP_A[1], cur)
    conn.commit()
    row = get_state_row(conn)
    check("S24", "Nail It after watchdog reset — graceful failure",
          row["state"], "UNCOMMITTED",
          note="pickup_confirmed from UNCOMMITTED must fail gracefully — state unchanged")
except Exception as e:
    conn.rollback()
    check("S24", "Nail It after watchdog reset — graceful failure",
          "exception", "UNCOMMITTED",
          note=f"Unexpected unhandled exception: {e}")

# Cleanup
wipe_driver(conn)
conn.close()

# ── Summary ───────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
passed = sum(1 for r in results if "PASS" in r["status"])
failed = sum(1 for r in results if "FAIL" in r["status"])
print(f"Results: {passed} PASS / {failed} FAIL / {len(results)} total checks")
if failed > 0:
    print("\nFailed scenarios:")
    for r in results:
        if "FAIL" in r["status"]:
            print(f"  {r['id']} — {r['desc']}")
            print(f"    Expected: {r['expect']}")
            print(f"    Actual:   {r['actual']}")
print()
sys.exit(0 if failed == 0 else 1)
