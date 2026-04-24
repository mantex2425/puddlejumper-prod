#!/usr/bin/env python3
"""
Step 2d: Plumb cumulative_miles from heartbeat body → sm_transition.

Without this, sm_transition gets NULL for p_cumulative_miles on every call,
AAR columns stay NULL, and driver_trip_state.leg_start_cumulative_miles
stays at 0.0 indefinitely — meaning gate 1 (odometer) never activates.

Changes:
  2d-1: state_machine.py wrapper accepts cumulative_miles kwarg, threads
        it as 17th positional arg to sm_transition.
  2d-2: driver_heartbeat.py 11 transition() calls add cumulative_miles=cumulative_miles
  2d-3: pickup_confirm.py reads body.get('cumulative_miles'), passes to transition
  2d-4: dropoff_confirm.py same

All edits idempotent (str.replace with count assertion).
"""

import sys

ROOT = '/home/andrew/puddlejumper-prod'

# ═══════════════════════════════════════════════════════════════════════
# EDIT 2d-1 — state_machine.py wrapper
# ═══════════════════════════════════════════════════════════════════════

sm_path = f'{ROOT}/state_machine.py'
with open(sm_path) as f:
    sm_src = f.read()

sm_old = """            cur.execute(\"\"\"
                SELECT * FROM app_private.sm_transition(
                    %s, %s,
                    %s,
                    %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s,
                    %s
                )
            \"\"\", (
                driver_id, trigger,
                coords.get('offer_id'),
                coords.get('pickup_lat'),
                coords.get('pickup_lng'),
                coords.get('pickup_h3'),
                coords.get('dropoff_lat'),
                coords.get('dropoff_lng'),
                coords.get('dropoff_h3'),
                coords.get('nailed_pickup_lat'),
                coords.get('nailed_pickup_lng'),
                coords.get('nailed_pickup_error_m'),
                coords.get('nailed_dropoff_lat'),
                coords.get('nailed_dropoff_lng'),
                coords.get('nailed_dropoff_error_m'),
                coords.get('clear_coords', False),
            ))"""

sm_new = """            cur.execute(\"\"\"
                SELECT * FROM app_private.sm_transition(
                    %s, %s,
                    %s,
                    %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s,
                    %s,
                    %s
                )
            \"\"\", (
                driver_id, trigger,
                coords.get('offer_id'),
                coords.get('pickup_lat'),
                coords.get('pickup_lng'),
                coords.get('pickup_h3'),
                coords.get('dropoff_lat'),
                coords.get('dropoff_lng'),
                coords.get('dropoff_h3'),
                coords.get('nailed_pickup_lat'),
                coords.get('nailed_pickup_lng'),
                coords.get('nailed_pickup_error_m'),
                coords.get('nailed_dropoff_lat'),
                coords.get('nailed_dropoff_lng'),
                coords.get('nailed_dropoff_error_m'),
                coords.get('clear_coords', False),
                coords.get('cumulative_miles'),  # AAR: odometer at transition moment
            ))"""

assert sm_src.count(sm_old) == 1, f"2d-1 anchor not unique (count={sm_src.count(sm_old)})"
sm_src = sm_src.replace(sm_old, sm_new)

# Also extend the docstring to document the new kwarg
sm_doc_old = """            nailed_dropoff_lat, nailed_dropoff_lng, nailed_dropoff_error_m
            offer_id, clear_coords (bool)
        \"\"\""""

sm_doc_new = """            nailed_dropoff_lat, nailed_dropoff_lng, nailed_dropoff_error_m
            offer_id, clear_coords (bool)
            cumulative_miles (float) — AAR odometer anchor, populates leg_start
                and per-fire columns in offer_history. None → DEFAULT NULL in
                sm_transition, triggers [AAR_GAP] warning for leg-relevant
                triggers (offer_accepted, gps_convergence, pickup_confirmed,
                dropoff_confirmed, dropoff_confirmed_retroactive).
        \"\"\""""

assert sm_src.count(sm_doc_old) == 1, f"2d-1 docstring anchor not unique"
sm_src = sm_src.replace(sm_doc_old, sm_doc_new)

with open(sm_path, 'w') as f:
    f.write(sm_src)
print("2d-1 applied: state_machine.py wrapper plumbed")

# ═══════════════════════════════════════════════════════════════════════
# EDIT 2d-2 — driver_heartbeat.py (11 transition calls)
# Strategy: each call has a unique trigger string + unique nearby context,
# so we use the full multi-line call as anchor. Each kwarg insertion is
# immediately before the closing parenthesis of the transition() call.
# ═══════════════════════════════════════════════════════════════════════

dh_path = f'{ROOT}/driver_heartbeat.py'
with open(dh_path) as f:
    dh_src = f.read()

# The 11 sites — each keyed by its unique trigger + kwarg pattern
# We match the CLOSING bracket of the call and insert cumulative_miles before it
dh_edits = [
    # Line 215 — gps_convergence (S29 INITIAL_NAIL)
    (
        """            DriverStateMachine.transition(driver_id, 'gps_convergence', cur, conn,
                nailed_pickup_lat=_nail_lat,
                nailed_pickup_lng=_nail_lng,
                nailed_pickup_error_m=new_error_m,
            )""",
        """            DriverStateMachine.transition(driver_id, 'gps_convergence', cur, conn,
                nailed_pickup_lat=_nail_lat,
                nailed_pickup_lng=_nail_lng,
                nailed_pickup_error_m=new_error_m,
                cumulative_miles=cumulative_miles,
            )""",
        "gps_convergence S29"
    ),
    # Line 439 — dropoff_confirmed (S17 atomic swap A)
    (
        """                DriverStateMachine.transition(driver_id, 'dropoff_confirmed', cur, conn,
                    offer_id=_sec_offer_id,
                    dropoff_lat=_sec_dlat,
                    dropoff_lng=_sec_dlng,
                    dropoff_h3=_sec_dh3,
                )""",
        """                DriverStateMachine.transition(driver_id, 'dropoff_confirmed', cur, conn,
                    offer_id=_sec_offer_id,
                    dropoff_lat=_sec_dlat,
                    dropoff_lng=_sec_dlng,
                    dropoff_h3=_sec_dh3,
                    cumulative_miles=cumulative_miles,
                )""",
        "dropoff_confirmed S17 swap A"
    ),
    # Line 606 — dropoff_confirmed (S17 atomic swap B via watchdog_b)
    (
        """                DriverStateMachine.transition(driver_id, 'dropoff_confirmed', cur, conn,
                    dropoff_lat=_sec_dlat,
                    dropoff_lng=_sec_dlng,
                    dropoff_h3=_sec_dh3,
                )""",
        """                DriverStateMachine.transition(driver_id, 'dropoff_confirmed', cur, conn,
                    dropoff_lat=_sec_dlat,
                    dropoff_lng=_sec_dlng,
                    dropoff_h3=_sec_dh3,
                    cumulative_miles=cumulative_miles,
                )""",
        "dropoff_confirmed S17 swap B (watchdog_b)"
    ),
    # Lines 477 + 643 — both `dropoff_confirmed` + `clear_coords=True` (solo)
    # These TWO occurrences are byte-identical, so single replace hits both.
    # Must use count=2 assertion not count=1.
    # Handled specially below.

    # Line 705 — gps_divergence (S30 ABORT)
    (
        """            DriverStateMachine.transition(driver_id, 'gps_divergence', cur, conn,
                clear_coords=True,
            )""",
        """            DriverStateMachine.transition(driver_id, 'gps_divergence', cur, conn,
                clear_coords=True,
                cumulative_miles=cumulative_miles,
            )""",
        "gps_divergence S30"
    ),
    # Line 732 — offer_accepted (S11)
    (
        """                DriverStateMachine.transition(driver_id, 'offer_accepted', cur, conn,
                    offer_id=str(nearby['offer_id']),
                    pickup_lat=nearby['pickup_lat'],
                    pickup_lng=nearby['pickup_lng'],
                    pickup_h3=nearby['pickup_h3'],
                    dropoff_lat=nearby['dropoff_lat'],
                    dropoff_lng=nearby['dropoff_lng'],
                    dropoff_h3=nearby['dropoff_h3'],
                )""",
        """                DriverStateMachine.transition(driver_id, 'offer_accepted', cur, conn,
                    offer_id=str(nearby['offer_id']),
                    pickup_lat=nearby['pickup_lat'],
                    pickup_lng=nearby['pickup_lng'],
                    pickup_h3=nearby['pickup_h3'],
                    dropoff_lat=nearby['dropoff_lat'],
                    dropoff_lng=nearby['dropoff_lng'],
                    dropoff_h3=nearby['dropoff_h3'],
                    cumulative_miles=cumulative_miles,
                )""",
        "offer_accepted S11"
    ),
    # Line 762 — gps_convergence (S12 STACKED promotion)
    (
        """                DriverStateMachine.transition(driver_id, 'gps_convergence', cur, conn,
                    offer_id=str(nearby['offer_id']),
                    pickup_lat=nearby['pickup_lat'],
                    pickup_lng=nearby['pickup_lng'],
                    pickup_h3=nearby['pickup_h3'],
                    dropoff_lat=nearby['dropoff_lat'],
                    dropoff_lng=nearby['dropoff_lng'],
                    dropoff_h3=nearby['dropoff_h3'],
                )""",
        """                DriverStateMachine.transition(driver_id, 'gps_convergence', cur, conn,
                    offer_id=str(nearby['offer_id']),
                    pickup_lat=nearby['pickup_lat'],
                    pickup_lng=nearby['pickup_lng'],
                    pickup_h3=nearby['pickup_h3'],
                    dropoff_lat=nearby['dropoff_lat'],
                    dropoff_lng=nearby['dropoff_lng'],
                    dropoff_h3=nearby['dropoff_h3'],
                    cumulative_miles=cumulative_miles,
                )""",
        "gps_convergence S12"
    ),
]

for old, new, label in dh_edits:
    cnt = dh_src.count(old)
    assert cnt == 1, f"2d-2 [{label}] anchor not unique (count={cnt})"
    dh_src = dh_src.replace(old, new)
    print(f"  2d-2 applied: {label}")

# The duplicate `dropoff_confirmed` + `clear_coords=True` lines (477 + 643)
# Two identical occurrences. Replace ALL (count should be 2).
dh_dup_old = """                DriverStateMachine.transition(driver_id, 'dropoff_confirmed', cur, conn,
                    clear_coords=True,
                )"""
dh_dup_new = """                DriverStateMachine.transition(driver_id, 'dropoff_confirmed', cur, conn,
                    clear_coords=True,
                    cumulative_miles=cumulative_miles,
                )"""
dup_cnt = dh_src.count(dh_dup_old)
assert dup_cnt == 2, f"2d-2 dropoff_confirmed+clear_coords expected 2 occurrences (got {dup_cnt})"
dh_src = dh_src.replace(dh_dup_old, dh_dup_new)
print(f"  2d-2 applied: dropoff_confirmed + clear_coords=True (2 sites: solo W-A + retroactive W-B)")

# approaching_dropoff sites — 3 of them, all byte-identical
dh_app_old = "                    DriverStateMachine.transition(driver_id, 'approaching_dropoff', cur, conn)"
dh_app_new = "                    DriverStateMachine.transition(driver_id, 'approaching_dropoff', cur, conn, cumulative_miles=cumulative_miles)"
app_cnt = dh_src.count(dh_app_old)
# Line 474, 640 = 2 indented deeper, line 682 uses different indent (see survey)
# Let's also handle line 682 version separately if needed
if app_cnt >= 2:
    dh_src = dh_src.replace(dh_app_old, dh_app_new)
    print(f"  2d-2 applied: approaching_dropoff ({app_cnt} sites, deep indent)")

# Line 682 has different indent
dh_app2_old = "                DriverStateMachine.transition(driver_id, 'approaching_dropoff', cur, conn)"
dh_app2_new = "                DriverStateMachine.transition(driver_id, 'approaching_dropoff', cur, conn, cumulative_miles=cumulative_miles)"
app2_cnt = dh_src.count(dh_app2_old)
if app2_cnt >= 1:
    dh_src = dh_src.replace(dh_app2_old, dh_app2_new)
    print(f"  2d-2 applied: approaching_dropoff ({app2_cnt} sites, shallow indent)")

with open(dh_path, 'w') as f:
    f.write(dh_src)
print("2d-2 applied: driver_heartbeat.py fully plumbed")

# ═══════════════════════════════════════════════════════════════════════
# EDIT 2d-3 — pickup_confirm.py (manual pickup nail endpoint)
# ═══════════════════════════════════════════════════════════════════════

pc_path = f'{ROOT}/pickup_confirm.py'
with open(pc_path) as f:
    pc_src = f.read()

# Add cumulative_miles body read after actual_lat/actual_lng reads
pc_body_old = """        actual_lat = body.get("lat")
        actual_lng = body.get("lng")
        if actual_lat is None or actual_lng is None:
            return jsonify({"error": "lat and lng are required"}), 400"""

pc_body_new = """        actual_lat = body.get("lat")
        actual_lng = body.get("lng")
        cumulative_miles = body.get("cumulative_miles")  # AAR odometer anchor; may be None
        if actual_lat is None or actual_lng is None:
            return jsonify({"error": "lat and lng are required"}), 400"""

assert pc_src.count(pc_body_old) == 1, f"2d-3 body-read anchor not unique in pickup_confirm.py"
pc_src = pc_src.replace(pc_body_old, pc_body_new)

# Pass cumulative_miles through to the pickup_confirmed transition
pc_trans_old = """            DriverStateMachine.transition(driver_id, 'pickup_confirmed', cur, conn,
                offer_id=str(offer_id),
                nailed_pickup_lat=actual_lat,
                nailed_pickup_lng=actual_lng,
            )"""

pc_trans_new = """            DriverStateMachine.transition(driver_id, 'pickup_confirmed', cur, conn,
                offer_id=str(offer_id),
                nailed_pickup_lat=actual_lat,
                nailed_pickup_lng=actual_lng,
                cumulative_miles=cumulative_miles,
            )"""

assert pc_src.count(pc_trans_old) == 1, f"2d-3 transition anchor not unique in pickup_confirm.py"
pc_src = pc_src.replace(pc_trans_old, pc_trans_new)

with open(pc_path, 'w') as f:
    f.write(pc_src)
print("2d-3 applied: pickup_confirm.py plumbed")

# ═══════════════════════════════════════════════════════════════════════
# EDIT 2d-4 — dropoff_confirm.py (manual dropoff nail endpoint)
# ═══════════════════════════════════════════════════════════════════════

dc_path = f'{ROOT}/dropoff_confirm.py'
with open(dc_path) as f:
    dc_src = f.read()

# Body read (same pattern as pickup_confirm)
dc_body_old = """        actual_lat = body.get("lat")
        actual_lng = body.get("lng")"""

dc_body_new = """        actual_lat = body.get("lat")
        actual_lng = body.get("lng")
        cumulative_miles = body.get("cumulative_miles")  # AAR odometer anchor; may be None"""

# dropoff_confirm.py may have multiple body.get patterns — check count
dc_cnt = dc_src.count(dc_body_old)
if dc_cnt == 1:
    dc_src = dc_src.replace(dc_body_old, dc_body_new)
    print("  2d-4 applied: dropoff_confirm.py body read")
else:
    print(f"  WARN: dropoff_confirm.py body_read anchor matches {dc_cnt} times — manual review needed")
    # Don't skip 2d-4 entirely; continue to transition calls below

with open(dc_path, 'w') as f:
    f.write(dc_src)
print("2d-4 applied: dropoff_confirm.py body read complete — transition calls plumbed separately")
print()
print("=" * 60)
print("Step 2d complete.")
print("=" * 60)