#!/usr/bin/env python3
"""
replay_drive_stateful.py
Stateful replay harness — validates full pipeline responses against a real drive day.

Usage:
    python3 replay_drive_stateful.py 2026-04-03
    python3 replay_drive_stateful.py 2026-04-03 --verbose

Architecture:
    - Fetches decision_log and driver_trip_state_log for the day
    - Merges into a single chronological timeline
    - Walks the timeline maintaining correct driver state between offers
    - For each offer: runs pipeline stages 2, 4, 5 directly (no HTTP, no auth)
    - Checks every response for the 7 required "Space Age" keys
    - Reports FULL / TRUNCATED / CRASH per offer
    - Saves and restores driver state before/after replay

Key design choices:
    - Passes original decision_log.id to enrich_with_triangulation so the
      shadow table INSERT succeeds and update_driver_state commits correctly
    - Skips stages 3 and 6 (no new decision_log records written during replay)
    - Skips pipeline-triggered state changes (offer_accepted, offer_cancelled_implicit)
      because run_decision_engine + enrich_with_triangulation handle those themselves
"""

import sys, os, json, argparse
from datetime import datetime

# ── Path setup ────────────────────────────────────────────────────────
PROD = os.path.expanduser('~/puddlejumper-prod')
os.chdir(PROD)

# Add venv site-packages so firebase_admin and other prod deps resolve
import glob
_venv_site = glob.glob(os.path.join(PROD, 'venv/lib/python3*/site-packages'))
for _p in _venv_site:
    if _p not in sys.path:
        sys.path.insert(0, _p)

sys.path.insert(0, PROD)

import psycopg2
from psycopg2.extras import RealDictCursor

from decisions.router import parse_request

DB_CONFIG = {
    "host":   "10.128.0.2",
    "user":   "postgres",
    "dbname": "puddlejumper",
}

def get_db():
    return psycopg2.connect(**DB_CONFIG)
from decisions.engine import run_decision_engine
from decisions.state_enricher import enrich_with_state
from decisions.triangulation_enricher import enrich_with_triangulation
from nail_it_core import check_convergence

# ── Constants ─────────────────────────────────────────────────────────
DRIVER_ID = 'UjT1hE9eBXh2q95aSZYOkzDJ8lo1'

# Keys that MUST be present in every response (even if null)
REQUIRED_KEYS = [
    'verdict',
    'driverState',
    'confidenceTier',
    'triangulatedPickupLat',
    'triangulatedPickupLng',
    'odometerFloor',
    'odometerCeiling',
]

# State transitions that the pipeline handles — skip applying these manually
PIPELINE_TRIGGERS = {'offer_accepted', 'offer_cancelled_implicit'}

STATE_ICONS = {
    'UNCOMMITTED': '🟢',
    'ENROUTE':     '🟡',
    'IN_TRIP':     '🔵',
    'STACKED':     '🟠',
}

# ── DB helpers ────────────────────────────────────────────────────────
def get_current_state(cur, driver_id):
    cur.execute(
        "SELECT state FROM app_private.driver_trip_state WHERE driver_id = %s",
        (driver_id,)
    )
    row = cur.fetchone()
    return row['state'] if row else 'UNCOMMITTED'


def get_state_row(cur, driver_id):
    """Fetch full driver_trip_state row for convergence checks."""
    cur.execute("""
        SELECT state, pickup_lat, pickup_lng, dropoff_lat, dropoff_lng,
               nailed_pickup_error_m, nailed_dropoff_error_m, state_updated_at
        FROM app_private.driver_trip_state WHERE driver_id = %s
    """, (driver_id,))
    return cur.fetchone()


def save_state_snapshot(cur, driver_id):
    cur.execute(
        "SELECT * FROM app_private.driver_trip_state WHERE driver_id = %s",
        (driver_id,)
    )
    return cur.fetchone()


def restore_state_snapshot(cur, conn, driver_id, snap):
    """
    Walk the state machine to target state via valid intermediate transitions.
    Obeys DB trigger physics — no silent fallbacks.
    """
    if not snap:
        return
    try:
        conn.rollback()
    except Exception:
        pass

    target = snap['state']

    # Multi-hop walk paths: (from, to) -> [intermediate states]
    WALK = {
        ('UNCOMMITTED', 'ENROUTE'):     ['ENROUTE'],
        ('UNCOMMITTED', 'IN_TRIP'):     ['ENROUTE', 'IN_TRIP'],
        ('UNCOMMITTED', 'STACKED'):     ['ENROUTE', 'IN_TRIP', 'STACKED'],
        ('ENROUTE',     'IN_TRIP'):     ['IN_TRIP'],
        ('ENROUTE',     'STACKED'):     ['IN_TRIP', 'STACKED'],
        ('ENROUTE',     'UNCOMMITTED'): ['UNCOMMITTED'],
        ('IN_TRIP',     'STACKED'):     ['STACKED'],
        ('IN_TRIP',     'UNCOMMITTED'): ['UNCOMMITTED'],
        ('IN_TRIP',     'ENROUTE'):     ['UNCOMMITTED', 'ENROUTE'],
        ('STACKED',     'UNCOMMITTED'): ['UNCOMMITTED'],
        ('STACKED',     'ENROUTE'):     ['UNCOMMITTED', 'ENROUTE'],
        ('STACKED',     'IN_TRIP'):     ['UNCOMMITTED', 'ENROUTE', 'IN_TRIP'],
    }

    TRIGGER = {
        'ENROUTE':     'offer_accepted',
        'IN_TRIP':     'gps_convergence',
        'STACKED':     'offer_accepted',
        'UNCOMMITTED': 'manual_reset',
    }

    cur.execute(
        "SELECT state FROM app_private.driver_trip_state WHERE driver_id = %s",
        (driver_id,)
    )
    row = cur.fetchone()
    current = row['state'] if row else 'UNCOMMITTED'

    if current == target:
        steps = []
    else:
        steps = WALK.get((current, target), [target])

    for next_state in steps:
        try:
            cur.execute("SET LOCAL app.state_trigger = %s", (TRIGGER[next_state],))
            cur.execute("""
                UPDATE app_private.driver_trip_state
                SET state            = %s,
                    state_updated_at = NOW()
                WHERE driver_id = %s
            """, (next_state, driver_id))
            conn.commit()
            current = next_state
        except Exception as walk_err:
            conn.rollback()
            print(f"  [RESTORE WALK] {current} -> {next_state} rejected: {walk_err}")
            break

    if current != target:
        print(f"  [RESTORE] Reached {current}, could not walk to {target} — leaving {current}")
        return

    # Write metadata fields now that state is correct
    try:
        cur.execute("SET LOCAL app.state_trigger = 'replay_restore'")
        cur.execute("""
            UPDATE app_private.driver_trip_state
            SET current_offer_id       = %s,
                pickup_lat             = %s,
                pickup_lng             = %s,
                pickup_h3              = %s,
                nailed_pickup_lat      = %s,
                nailed_pickup_lng      = %s,
                nailed_pickup_error_m  = %s,
                nailed_dropoff_lat     = %s,
                nailed_dropoff_lng     = %s,
                nailed_dropoff_error_m = %s
            WHERE driver_id = %s
        """, (
            snap.get('current_offer_id'),
            snap.get('pickup_lat'),   snap.get('pickup_lng'),
            snap.get('pickup_h3'),
            snap.get('nailed_pickup_lat'),  snap.get('nailed_pickup_lng'),
            snap.get('nailed_pickup_error_m'),
            snap.get('nailed_dropoff_lat'), snap.get('nailed_dropoff_lng'),
            snap.get('nailed_dropoff_error_m'),
            driver_id,
        ))
        conn.commit()
    except Exception as meta_err:
        conn.rollback()
        print(f"  [RESTORE] Metadata write failed: {meta_err}")


def force_state(cur, conn, driver_id, to_state, trigger='replay_harness'):
    """Directly set driver state — used for non-pipeline transitions."""
    cur.execute("SET LOCAL app.state_trigger = %s", (trigger,))
    cur.execute("""
        UPDATE app_private.driver_trip_state
        SET state            = %s,
            state_updated_at = NOW(),
            current_offer_id       = CASE WHEN %s = 'UNCOMMITTED' THEN NULL ELSE current_offer_id END,
            pickup_lat             = CASE WHEN %s = 'UNCOMMITTED' THEN NULL ELSE pickup_lat END,
            pickup_lng             = CASE WHEN %s = 'UNCOMMITTED' THEN NULL ELSE pickup_lng END,
            pickup_h3              = CASE WHEN %s = 'UNCOMMITTED' THEN NULL ELSE pickup_h3 END,
            nailed_pickup_lat      = CASE WHEN %s = 'UNCOMMITTED' THEN NULL ELSE nailed_pickup_lat END,
            nailed_pickup_lng      = CASE WHEN %s = 'UNCOMMITTED' THEN NULL ELSE nailed_pickup_lng END,
            nailed_pickup_error_m  = CASE WHEN %s = 'UNCOMMITTED' THEN NULL ELSE nailed_pickup_error_m END,
            nailed_dropoff_lat     = CASE WHEN %s = 'UNCOMMITTED' THEN NULL ELSE nailed_dropoff_lat END,
            nailed_dropoff_lng     = CASE WHEN %s = 'UNCOMMITTED' THEN NULL ELSE nailed_dropoff_lng END,
            nailed_dropoff_error_m = CASE WHEN %s = 'UNCOMMITTED' THEN NULL ELSE nailed_dropoff_error_m END
        WHERE driver_id = %s
    """, (to_state,) + (to_state,) * 10 + (driver_id,))
    conn.commit()


# ── Timeline fetch ────────────────────────────────────────────────────
def get_timeline(cur, driver_id, replay_date):
    """Fetch offers + state changes for the day, sorted chronologically."""

    cur.execute("""
        SELECT
            id                                                    AS offer_id,
            created_at AT TIME ZONE 'America/Chicago'             AS ts,
            'offer'                                               AS kind,
            fare, pickup_minutes, trip_minutes,
            pickup_lat, pickup_lng, dropoff_lat, dropoff_lng,
            trip_miles, pickup_miles,
            market_id, mode_at_decision, market_name,
            current_lat, current_lng,
            towards_market_id, towards_target_lat, towards_target_lng,
            ocr_confidence,
            trace_data,
            decision_result->>'verdict'                           AS original_verdict
        FROM app_private.decision_log
        WHERE driver_id = %s
        AND DATE(created_at AT TIME ZONE 'America/Chicago') = %s
        ORDER BY created_at
    """, (driver_id, replay_date))
    offers = [dict(r) for r in cur.fetchall()]

    cur.execute("""
        SELECT
            logged_at AT TIME ZONE 'America/Chicago'  AS ts,
            'state_change'                             AS kind,
            from_state, to_state, trigger_event
        FROM app_private.driver_trip_state_log
        WHERE driver_id = %s
        AND DATE(logged_at AT TIME ZONE 'America/Chicago') = %s
        ORDER BY logged_at
    """, (driver_id, replay_date))
    changes = [dict(r) for r in cur.fetchall()]

    events = offers + changes
    events.sort(key=lambda e: e['ts'])
    return events, len(offers), len(changes)


# ── Request reconstruction ────────────────────────────────────────────
def build_p(row):
    """Reconstruct the HTTP request params dict from a decision_log row."""
    trace = row.get('trace_data') or {}
    if isinstance(trace, str):
        try:
            trace = json.loads(trace)
        except Exception:
            trace = {}
    arc = trace.get('arc_band', {}) if isinstance(trace, dict) else {}

    mode = row.get('mode_at_decision') or 'FREESTYLE'

    return {
        'fare':                      float(row['fare'] or 0),
        'tripMiles':                 float(row['trip_miles'] or 0),
        'tripMinutes':               float(row['trip_minutes'] or 0),
        'pickupMinutes':             float(row['pickup_minutes'] or 0),
        'pickupMiles':               float(row['pickup_miles'] or 0),
        'lat':                       row.get('pickup_lat'),
        'lng':                       row.get('pickup_lng'),
        'dropoffLat':                row.get('dropoff_lat'),
        'dropoffLng':                row.get('dropoff_lng'),
        'currentLat':                row.get('current_lat'),
        'currentLng':                row.get('current_lng'),
        'marketId':                  row.get('market_id'),
        'marketName':                row.get('market_name'),
        'isPuddleJumpMode':          mode == 'PUDDLE_JUMP',
        'towardsActive':             mode == 'TOWARDS',
        'towardsMarketId':           row.get('towards_market_id'),
        'towardsTargetLat':          row.get('towards_target_lat'),
        'towardsTargetLng':          row.get('towards_target_lng'),
        'towardsBacktrackTolerance': 3.0,
        'gpsAgeSec':                 trace.get('gps_age_sec') if isinstance(trace, dict) else None,
        'pickupAddress':             arc.get('pickup_address'),
        'dropoffAddress':            arc.get('dropoff_address'),
    }


# ── Result validation ─────────────────────────────────────────────────
def check_truncation(result):
    """Return list of missing required keys. Empty list = FULL response."""
    return [k for k in REQUIRED_KEYS if k not in result]


# ── Main ──────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description='Stateful replay harness — validates full pipeline responses'
    )
    parser.add_argument('date', help='Replay date YYYY-MM-DD')
    parser.add_argument('--verbose', '-v', action='store_true',
                        help='Show missing keys and errors inline')
    args = parser.parse_args()

    verbose = args.verbose

    print(f"""
{'='*70}
  🐸 PuddleJumper Stateful Replay Harness
  Driver:  {DRIVER_ID[:16]}...
  Date:    {args.date}
  Mode:    {'VERBOSE' if verbose else 'STANDARD'}
{'='*70}
""")

    conn = get_db()
    cur  = conn.cursor(cursor_factory=RealDictCursor)

    # Save current state
    snapshot = save_state_snapshot(cur, DRIVER_ID)
    saved_state = snapshot['state'] if snapshot else 'none'
    print(f"  Saved state snapshot:  {saved_state}")

    # Fetch timeline
    events, n_offers, n_changes = get_timeline(cur, DRIVER_ID, args.date)
    print(f"  Timeline loaded:       {n_offers} offers + {n_changes} state changes")

    # Reset to UNCOMMITTED
    force_state(cur, conn, DRIVER_ID, 'UNCOMMITTED', trigger='manual_reset')
    print(f"  Reset to:              UNCOMMITTED\n")
    print(f"{'─'*70}")

    # Stats
    total = passed = failed = 0
    failures = []
    seen_triggers = set()

    try:
        for event in events:

            # ── State change event ────────────────────────────────────
            if event['kind'] == 'state_change':
                trigger = event['trigger_event']

                # Deduplicate duplicate log entries for the same trigger
                dedup_key = f"{event['ts'].strftime('%H:%M:%S')}-{trigger}-{event['to_state']}"
                if dedup_key in seen_triggers:
                    continue
                seen_triggers.add(dedup_key)

                # Pipeline-triggered transitions are handled by the pipeline
                if trigger in PIPELINE_TRIGGERS:
                    continue

                before = get_current_state(cur, DRIVER_ID)
                bi = STATE_ICONS.get(before, '⚪')
                ai = STATE_ICONS.get(event['to_state'], '⚪')
                ts = event['ts'].strftime('%H:%M:%S')
                print(
                    f"  {ts}  {bi} {before:<12} → {ai} {event['to_state']:<12}"
                    f"  [{trigger}]"
                )
                try:
                    force_state(cur, conn, DRIVER_ID, event['to_state'], trigger=trigger)
                    actual = get_current_state(cur, DRIVER_ID)
                    if actual != event['to_state']:
                        print(f"    🔴 STATE MISMATCH: expected {event['to_state']}, got {actual}")
                except Exception as sc_err:
                    conn.rollback()
                    if verbose:
                        print(f"    [SKIP] State change rejected by DB trigger: {sc_err}")
                    else:
                        print(f"    [SKIP] Invalid transition {before} -> {event['to_state']} ({trigger}) — skipping")
                continue

            # ── Offer event ───────────────────────────────────────────
            # Session gap detection — reset if >30 min between offers
            offer_ts = event['ts']
            if last_offer_ts is not None:
                gap_minutes = (offer_ts - last_offer_ts).total_seconds() / 60
                if gap_minutes > 30:
                    print(f"
  ⏱  Session gap: {gap_minutes:.0f} min — resetting to UNCOMMITTED
")
                    force_state(cur, conn, DRIVER_ID, 'UNCOMMITTED', trigger='replay_session_gap')
            last_offer_ts = offer_ts

            total += 1
            offer_id       = event['offer_id']
            ts             = event['ts'].strftime('%H:%M:%S')
            orig_verdict   = event.get('original_verdict', '?')
            current_state  = get_current_state(cur, DRIVER_ID)
            si             = STATE_ICONS.get(current_state, '⚪')

            # Reconstruct request + parse
            p      = build_p(event)
            params = parse_request(p, DRIVER_ID)

            result = {}
            error  = None

            try:
                # Stage 2: decision engine
                result, _, ep = run_decision_engine(cur, conn, DRIVER_ID, params)

                # Stage 4: driver state + S04
                result, driver_state = enrich_with_state(cur, conn, DRIVER_ID, ep, result)

                # Stage 5: triangulation + shadow + state machine
                # Pass original offer_id so shadow INSERT succeeds and
                # update_driver_state commits → state machine stays accurate
                result = enrich_with_triangulation(
                    cur, conn, DRIVER_ID, ep, result, driver_state, offer_id
                )

            except Exception as e:
                error = f"{type(e).__name__}: {e}"

            # Truncation check
            missing = check_truncation(result) if not error else list(REQUIRED_KEYS)

            verdict      = result.get('verdict', '?')
            tier         = result.get('confidenceTier', '—')
            ds           = result.get('driverState', '—')
            vi           = '✅' if verdict == 'ACCEPT' else '❌'
            oi           = '✅' if orig_verdict == 'ACCEPT' else '❌'
            match        = '✓' if verdict == orig_verdict else '≠'

            if error:
                status = 'CRASH';     si2 = '💀'
                failed += 1
            elif missing:
                status = 'TRUNCATED'; si2 = '⚠️ '
                failed += 1
            else:
                status = 'FULL';      si2 = '✅'
                passed += 1

            # ── State machine correctness checks ──────────────────────
            state_checks = []
            if not error and verdict == 'ACCEPT':
                post_state = get_current_state(cur, DRIVER_ID)

                # Expected post-ACCEPT state depends on prior state
                # UNCOMMITTED/ENROUTE + ACCEPT -> ENROUTE
                # IN_TRIP + ACCEPT             -> STACKED
                # STACKED + ACCEPT             -> STACKED (already stacked)
                if current_state in ('UNCOMMITTED', 'ENROUTE'):
                    expected_post = 'ENROUTE'
                elif current_state == 'IN_TRIP':
                    expected_post = 'STACKED'
                else:
                    expected_post = current_state  # STACKED stays STACKED

                # Check 1: state advanced correctly after ACCEPT
                if post_state != expected_post:
                    state_checks.append(
                        f'ACCEPT state mismatch: expected {expected_post}, got {post_state}'
                    )
                elif expected_post == 'ENROUTE':
                    # Check 2: convergence engine can fire at pickup
                    # (only meaningful when transitioning to ENROUTE)
                    state_row = get_state_row(cur, DRIVER_ID)
                    if not state_row or not state_row['pickup_lat']:
                        state_checks.append('pickup_lat NULL — convergence engine blind')
                    else:
                        hb_verdict, _, hb_error_m = check_convergence(
                            DRIVER_ID,
                            float(state_row['pickup_lat']),
                            float(state_row['pickup_lng']),
                            0.0,   # simulate stopped at pickup
                            state_row, cur
                        )
                        if hb_verdict != 'INITIAL_NAIL':
                            state_checks.append(
                                f'convergence engine blocked at pickup (verdict={hb_verdict})'
                            )

                if state_checks and status == 'FULL':
                    status = 'STATE_MISMATCH'
                    si2 = '🔴'
                    passed -= 1
                    failed += 1

            if error or missing or state_checks:
                failures.append({
                    'offer_id':     offer_id,
                    'ts':           ts,
                    'state':        current_state,
                    'verdict':      verdict,
                    'status':       status,
                    'missing':      missing,
                    'error':        error,
                    'state_checks': state_checks,
                })

            print(
                f"  {ts}  {si} {current_state:<12}  "
                f"#{offer_id:<5}  {vi}{match}{oi}  "
                f"{si2} {status:<10}  "
                f"tier:{tier:<8} state:{ds}"
            )

            if verbose or state_checks:
                if missing:
                    print(f"           MISSING: {missing}")
                if error:
                    print(f"           ERROR:   {error}")
                for sc in state_checks:
                    print(f"           🔴 STATE: {sc}")

    except KeyboardInterrupt:
        print('\n  [Interrupted by user]')

    finally:
        # Always restore state
        restore_state_snapshot(cur, conn, DRIVER_ID, snapshot)
        restored = get_current_state(cur, DRIVER_ID)
        print(f"\n  State restored:        {restored}")

    # ── Summary ───────────────────────────────────────────────────────
    print(f"\n{'─'*70}")

    pass_pct = round(passed / total * 100) if total else 0
    verdict_icon = '✅' if failed == 0 else '❌'

    print(
        f"  {verdict_icon}  TOTAL: {total}  |  "
        f"PASSED: {passed} ({pass_pct}%)  |  "
        f"FAILED: {failed}"
    )

    if failures:
        print(f"\n  Failures:")
        print(f"  {'─'*60}")
        for f in failures:
            si = STATE_ICONS.get(f['state'], '⚪')
            print(
                f"  #{f['offer_id']:<5} @ {f['ts']}  "
                f"{si} {f['state']:<12}  {f['status']}"
            )
            if f['missing']:
                print(f"    Missing keys: {f['missing']}")
            if f['error']:
                print(f"    Error:        {f['error']}")

    print(f"\n{'='*70}\n")

    cur.close()
    conn.close()

    return 0 if failed == 0 else 1


if __name__ == '__main__':
    sys.exit(main())