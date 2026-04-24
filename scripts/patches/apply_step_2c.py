#!/usr/bin/env python3
"""
Step 2c: Three surgical edits to nail_it_core.py

1. Gate 1 (odometer) added to BMOAR blind_man fire — mandatory per
   UNIFIED_INTENT spec §4. Without it, BMOAR could fire on a pivot
   match mid-trip before odometer threshold reached.

2. Pickup Path B (proximity-enable) — replaces deleted-legacy placeholder.
   Fires when BMOAR didn't but driver is close + stopped + odometer passed.

3. Dropoff Path B — symmetric to pickup.

Idempotent: uses str.replace with count-assertion. Re-running is safe
(str_count will be 0 on second run, assert fails fast, no damage).
"""

import sys

PATH = '/home/andrew/puddlejumper-prod/nail_it_core.py'
with open(PATH) as f:
    src = f.read()

# ─────────────────────────────────────────────────────────────────────
# EDIT 1 — Add gate 1 odometer check to BMOAR blind_man fire block.
# The check goes INSIDE the `if _src.startswith('blind_man'):` branch,
# BEFORE the state-check-and-fire logic.
# ─────────────────────────────────────────────────────────────────────

edit1_old = """                        _src = _bead_result.get('source', '')
                        if _src.startswith('blind_man'):
                            # Compute distance at the override coord for the
                            # returned dist_m (downstream audit consistency).
                            cur.execute(
                                "SELECT app_private.distance_miles(%s, %s, %s, %s) * 1609.34 AS dist_m",
                                (current_lat, current_lng, target_lat, target_lng)
                            )
                            _fire_dist_m = float(cur.fetchone()['dist_m'])"""

edit1_new = """                        _src = _bead_result.get('source', '')
                        if _src.startswith('blind_man'):
                            # ────────────────────────────────────────────
                            # GATE 1: Odometer floor (MANDATORY per spec §4)
                            # miles_since_leg_start >= 0.9 * expected_miles
                            # Prevents mid-trip false-fires where pivot+cluster
                            # accidentally match at a red light near the pin.
                            # ────────────────────────────────────────────
                            _leg_start = state_row.get('leg_start_cumulative_miles')
                            _leg_start_f = float(_leg_start) if _leg_start is not None else 0.0
                            _cm = float(cumulative_miles) if cumulative_miles is not None else 0.0
                            _miles_since_leg_start = _cm - _leg_start_f
                            _gate1_floor = 0.9 * float(_bead_miles)
                            if _miles_since_leg_start < _gate1_floor:
                                logging.info(
                                    f"[BEAD] gate 1 FAIL — blind_man fire blocked: "
                                    f"miles_since_leg_start={_miles_since_leg_start:.2f} "
                                    f"< floor={_gate1_floor:.2f} "
                                    f"(expected_miles={_bead_miles:.2f}, "
                                    f"leg_start={_leg_start_f:.2f}, "
                                    f"cumulative={_cm:.2f})"
                                )
                                # Do not fire via blind_man. Fall through to
                                # Path B (proximity) or Watchdog A.
                                raise StopIteration("gate_1_failed")

                            # Compute distance at the override coord for the
                            # returned dist_m (downstream audit consistency).
                            cur.execute(
                                "SELECT app_private.distance_miles(%s, %s, %s, %s) * 1609.34 AS dist_m",
                                (current_lat, current_lng, target_lat, target_lng)
                            )
                            _fire_dist_m = float(cur.fetchone()['dist_m'])"""

assert src.count(edit1_old) == 1, \
    f"EDIT 1 anchor not unique (count={src.count(edit1_old)})"
src = src.replace(edit1_old, edit1_new)
print("EDIT 1 applied: gate 1 added to BMOAR fire")

# ─────────────────────────────────────────────────────────────────────
# Adjust the outer try/except to catch StopIteration from gate 1 fail.
# The existing `except Exception` won't catch StopIteration. We add an
# explicit except BEFORE the existing one.
# ─────────────────────────────────────────────────────────────────────

edit1b_old = """    except Exception as _bead_err:
        logging.warning(f"[BEAD] compute_target integration failed: {_bead_err}")"""

edit1b_new = """    except StopIteration:
        # Gate 1 failed — blind_man fire blocked. Falls through to Path B.
        pass
    except Exception as _bead_err:
        logging.warning(f"[BEAD] compute_target integration failed: {_bead_err}")"""

assert src.count(edit1b_old) == 1, \
    f"EDIT 1b anchor not unique (count={src.count(edit1b_old)})"
src = src.replace(edit1b_old, edit1b_new)
print("EDIT 1b applied: StopIteration catch added")

# ─────────────────────────────────────────────────────────────────────
# EDIT 2 — Pickup Path B (proximity-enable unified predicate).
# Replaces the deleted-legacy comment placeholder at line 1330-ish.
# ─────────────────────────────────────────────────────────────────────

edit2_old = """        # Legacy pickup inner-confirm removed — BMOAR owns pickup fires.
        # Pickup Watchdog A below is the mercy-kill for BMOAR misses.
        if dist_m < ARMED_RADIUS_M:"""

edit2_new = """        # ════════════════════════════════════════════════════════════════
        # PATH B — Pickup unified predicate (proximity-enable)
        # Fires when BMOAR (Path A) didn't fire but cluster forms within
        # 200m of target + gate 1 (odometer) passes.
        # Per UNIFIED_INTENT spec §4: fire requires
        #   (gate 1 odometer) AND cluster AND (proximity OR pivot-match)
        # Path A (BMOAR above) covers the pivot-match enable.
        # Path B (this block) covers the proximity enable.
        # Both fire at cluster median (never current position).
        # ════════════════════════════════════════════════════════════════
        try:
            from bead_on_wire import detect_cluster as _detect_cluster_b
            from bead_on_wire import get_pivot_context as _get_pivot_context_b

            _b_offer_id = state_row.get('current_offer_id')
            _b_pickup_miles = None
            if _b_offer_id:
                cur.execute(
                    "SELECT pickup_miles FROM app_private.offer_history "
                    "WHERE decision_log_id = %s::integer LIMIT 1",
                    (_b_offer_id,)
                )
                _b_row = cur.fetchone()
                if _b_row and _b_row.get('pickup_miles') is not None:
                    _b_pickup_miles = float(_b_row['pickup_miles'])

            _b_leg_start = state_row.get('leg_start_cumulative_miles')
            _b_leg_start_f = float(_b_leg_start) if _b_leg_start is not None else 0.0
            _b_cm = float(cumulative_miles) if cumulative_miles is not None else 0.0
            _b_miles_since_leg_start = _b_cm - _b_leg_start_f

            _b_gate1 = (_b_pickup_miles is not None
                        and _b_miles_since_leg_start >= 0.9 * _b_pickup_miles)
            _b_proximity = dist_m < 200.0

            if _b_gate1 and _b_proximity:
                _b_pivot = _get_pivot_context_b(driver_id, cur, anchor_time=None)
                _b_on_wire = bool(_b_pivot.get('on_wire')) if _b_pivot else False
                _b_spread = 25.0 if _b_on_wire else 70.0
                _b_cluster = _detect_cluster_b(driver_id, cur, max_spread_m=_b_spread)

                if _b_cluster:
                    _b_nail_lat = float(_b_cluster['median_lat'])
                    _b_nail_lng = float(_b_cluster['median_lng'])
                    logging.info(
                        f"[PATH_B] ✅ INITIAL_NAIL (pickup proximity) — "
                        f"dist_m={dist_m:.0f} miles_since_leg={_b_miles_since_leg_start:.2f}/"
                        f"{_b_pickup_miles:.2f} "
                        f"cluster_n={_b_cluster.get('n')} spread={_b_cluster.get('spread_m', 0):.0f}m "
                        f"on_wire={_b_on_wire} "
                        f"nail_at=({_b_nail_lat:.5f},{_b_nail_lng:.5f})"
                    )
                    _log_nail_instrument(driver_id, 'path_b_pickup',
                                         _b_nail_lat, _b_nail_lng,
                                         target_lat, target_lng)
                    return ('INITIAL_NAIL', 'IN_TRIP', dist_m, (_b_nail_lat, _b_nail_lng))
        except Exception as _path_b_err:
            logging.warning(f"[PATH_B] pickup predicate failed (non-fatal): {_path_b_err}")

        if dist_m < ARMED_RADIUS_M:"""

assert src.count(edit2_old) == 1, \
    f"EDIT 2 anchor not unique (count={src.count(edit2_old)})"
src = src.replace(edit2_old, edit2_new)
print("EDIT 2 applied: pickup Path B unified predicate installed")

# ─────────────────────────────────────────────────────────────────────
# EDIT 3 — Dropoff Path B (symmetric to pickup).
# ─────────────────────────────────────────────────────────────────────

edit3_old = """        # ── Dual-Watchdog Logic (NEW) ───────────────────────────────────────
        if dist_m < armed_radius:
            # Legacy dropoff inner-confirm removed — BMOAR owns dropoff fires.
            # Watchdog A (below) + Watchdog B departure are the mercy-kills."""

edit3_new = """        # ════════════════════════════════════════════════════════════════
        # PATH B — Dropoff unified predicate (proximity-enable)
        # Symmetric to pickup Path B above. Fires when BMOAR didn't but
        # proximity + cluster + gate 1 all pass.
        # ════════════════════════════════════════════════════════════════
        try:
            from bead_on_wire import detect_cluster as _detect_cluster_d
            from bead_on_wire import get_pivot_context as _get_pivot_context_d

            _d_offer_id = state_row.get('current_offer_id')
            _d_trip_miles = None
            if _d_offer_id:
                cur.execute(
                    "SELECT trip_miles FROM app_private.offer_history "
                    "WHERE decision_log_id = %s::integer LIMIT 1",
                    (_d_offer_id,)
                )
                _d_row = cur.fetchone()
                if _d_row and _d_row.get('trip_miles') is not None:
                    _d_trip_miles = float(_d_row['trip_miles'])

            _d_leg_start = state_row.get('leg_start_cumulative_miles')
            _d_leg_start_f = float(_d_leg_start) if _d_leg_start is not None else 0.0
            _d_cm = float(cumulative_miles) if cumulative_miles is not None else 0.0
            _d_miles_since_leg_start = _d_cm - _d_leg_start_f

            _d_gate1 = (_d_trip_miles is not None
                        and _d_miles_since_leg_start >= 0.9 * _d_trip_miles)
            _d_proximity = dist_m < 200.0

            if _d_gate1 and _d_proximity:
                _d_pivot = _get_pivot_context_d(driver_id, cur, anchor_time=None)
                _d_on_wire = bool(_d_pivot.get('on_wire')) if _d_pivot else False
                _d_spread = 25.0 if _d_on_wire else 70.0
                _d_cluster = _detect_cluster_d(driver_id, cur, max_spread_m=_d_spread)

                if _d_cluster:
                    _d_nail_lat = float(_d_cluster['median_lat'])
                    _d_nail_lng = float(_d_cluster['median_lng'])
                    logging.info(
                        f"[PATH_B] ✅ DROPOFF_NAIL (dropoff proximity) — "
                        f"dist_m={dist_m:.0f} miles_since_leg={_d_miles_since_leg_start:.2f}/"
                        f"{_d_trip_miles:.2f} "
                        f"cluster_n={_d_cluster.get('n')} spread={_d_cluster.get('spread_m', 0):.0f}m "
                        f"on_wire={_d_on_wire} "
                        f"nail_at=({_d_nail_lat:.5f},{_d_nail_lng:.5f})"
                    )
                    _log_nail_instrument(driver_id, 'path_b_dropoff',
                                         _d_nail_lat, _d_nail_lng,
                                         target_lat, target_lng)
                    return ('DROPOFF_NAIL', 'UNCOMMITTED', dist_m, (_d_nail_lat, _d_nail_lng))
        except Exception as _path_b_err:
            logging.warning(f"[PATH_B] dropoff predicate failed (non-fatal): {_path_b_err}")

        # ── Dual-Watchdog Logic (NEW) ───────────────────────────────────────
        if dist_m < armed_radius:
            # Legacy dropoff inner-confirm removed — BMOAR owns dropoff fires.
            # Watchdog A (below) + Watchdog B departure are the mercy-kills."""

assert src.count(edit3_old) == 1, \
    f"EDIT 3 anchor not unique (count={src.count(edit3_old)})"
src = src.replace(edit3_old, edit3_new)
print("EDIT 3 applied: dropoff Path B unified predicate installed")

# ─────────────────────────────────────────────────────────────────────

with open(PATH, 'w') as f:
    f.write(src)
print("File written successfully")