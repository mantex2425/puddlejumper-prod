"""
monitor.py — PuddleJumper Session Monitor
Posts real-time updates and health checks to Discord.
Run via Cloud Scheduler every 10 minutes.
"""

import os
import json
import urllib.request
import psycopg2
from psycopg2.extras import RealDictCursor
from state_machine import DriverStateMachine
from datetime import datetime, timezone

WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
DRIVER_ID = "UjT1hE9eBXh2q95aSZYOkzDJ8lo1"
DB_HOST = os.environ.get("DB_HOST", "10.128.0.2")
DB_NAME = os.environ.get("DB_NAME", "puddlejumper")
DB_USER = os.environ.get("DB_USER", "atjb")
DB_PASS = os.environ.get("DB_PASSWORD", os.environ.get("DB_PASS", ""))


def send_discord(message: str):
    """Send a message to Discord webhook."""
    if not WEBHOOK_URL:
        print("No webhook URL configured")
        return
    data = json.dumps({"content": message}).encode()
    req = urllib.request.Request(
        WEBHOOK_URL,
        data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "PuddleJumper-Monitor/1.0 (GCP-CloudRun)"
        },
        method="POST"
    )
    try:
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        print(f"Discord send failed: {e}")


def get_db():
    return psycopg2.connect(
        host=DB_HOST, dbname=DB_NAME, user=DB_USER, password=DB_PASS
    )


# INSERT THIS FUNCTION before run_monitor() in monitor.py

def run_watchdog(conn, cur):
    """
    Self-healing watchdog for driver state machine.
    Auto-resets stuck states and logs the intervention to driver_trip_state_log.
    Called at the start of every monitor run.

    Stuck state rules:
    - IN_TRIP or STACKED for > 90 minutes with no state change = stuck
    - Auto-reset to UNCOMMITTED
    - Log the intervention so we can track frequency

    Returns: list of alert strings to include in Discord message
    """
    alerts = []

    cur.execute("""
        SELECT 
            driver_id,
            state,
            state_updated_at,
            GREATEST(0, EXTRACT(EPOCH FROM (
                NOW() - state_updated_at
            ))/60) AS age_min
        FROM app_private.driver_trip_state
        WHERE driver_id = %s
          AND (
            (state = 'STACKED'  AND state_updated_at < NOW() - INTERVAL '45 minutes')
            OR
            (state = 'IN_TRIP'  AND state_updated_at < NOW() - INTERVAL '90 minutes')
          )
    """, (DRIVER_ID,))

    stuck = cur.fetchone()

    if stuck:
        age = float(stuck['age_min'] or 0)
        old_state = stuck['state']
        threshold = 45 if old_state == 'STACKED' else 90

        # Auto-reset to UNCOMMITTED via state machine
        DriverStateMachine.transition(DRIVER_ID, 'watchdog_auto_reset', cur, conn,
            clear_coords=True,
        )

        alerts.append(
            f"🔧 **Watchdog auto-reset**: {old_state} stuck for {age:.0f} min "
            f"(threshold: {threshold} min) → UNCOMMITTED"
        )
        print(f"Watchdog: auto-reset {old_state} after {age:.0f} min (threshold {threshold} min)")

    return alerts

def run_monitor():
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # ── Current state ──────────────────────────────────────
        cur.execute("""
            SELECT state, state_updated_at,
                   GREATEST(0, EXTRACT(EPOCH FROM (NOW() - state_updated_at))/60) AS age_min
            FROM app_private.driver_trip_state
            WHERE driver_id = %s
        """, (DRIVER_ID,))
        state_row = cur.fetchone()

        # ── Session stats (last 4 hours) ───────────────────────
        cur.execute("""
            SELECT
                COUNT(*) AS total_offers,
                COUNT(*) FILTER (WHERE pms.is_accepted = true) AS accepted,
                COUNT(*) FILTER (WHERE pms.actual_pickup_at IS NOT NULL) AS nail_its,
                round(AVG(pms.triangulation_error_m) FILTER (
                    WHERE pms.actual_pickup_at IS NOT NULL
                )::numeric, 0) AS avg_error_m,
                round(100.0 * COUNT(*) FILTER (
                    WHERE pms.triangulation_error_m <= 800
                    AND pms.actual_pickup_at IS NOT NULL
                ) / NULLIF(COUNT(*) FILTER (
                    WHERE pms.actual_pickup_at IS NOT NULL), 0), 1) AS pct_on_target
            FROM app_private.pickup_market_signals pms
            JOIN app_private.decision_log dl ON dl.id = pms.offer_id
            WHERE dl.driver_id = %s
              AND dl.created_at > NOW() - INTERVAL '4 hours'
        """, (DRIVER_ID,))
        stats = cur.fetchone()

        # ── State machine health ───────────────────────────────
        cur.execute("""
            SELECT COUNT(*) AS transitions
            FROM app_private.driver_trip_state_log
            WHERE driver_id = %s
              AND logged_at > NOW() - INTERVAL '4 hours'
        """, (DRIVER_ID,))
        state_stats = cur.fetchone()

        # ── Watchdog: auto-reset stuck states ─────────────────────
        watchdog_alerts = run_watchdog(conn, cur)

        # ── Last reported state ────────────────────────────────
        cur.execute("SELECT last_state FROM app_private.monitor_last_report")
        last_report = cur.fetchone()
        last_state = last_report['last_state'] if last_report else None
        current_state = state_row['state'] if state_row else 'UNCOMMITTED'
        state_changed = current_state != last_state

        # ── Anomaly detection ──────────────────────────────────
        alerts = list(watchdog_alerts)

        if state_row:
            age = float(state_row['age_min'] or 0)
            if state_row['state'] == 'IN_TRIP' and age > 90:
                alerts.append(f"⚠️ Stuck IN_TRIP for {age:.0f} min — consider reset!")
            if state_row['state'] == 'STACKED' and age > 90:
                alerts.append(f"⚠️ Stuck STACKED for {age:.0f} min — consider reset!")

        if stats and stats['total_offers'] and stats['total_offers'] > 3:
            if stats['avg_error_m'] and float(stats['avg_error_m']) > 800:
                alerts.append(f"⚠️ High triangulation error: {stats['avg_error_m']}m avg")
            nail_rate = (stats['nail_its'] or 0) / max(stats['accepted'] or 1, 1)
            if nail_rate < 0.4 and (stats['accepted'] or 0) > 10:
                alerts.append(f"⚠️ Low Nail It rate: {nail_rate:.0%} — Auto Nail It may not be firing")

        # ── Build message ──────────────────────────────────────
        now = datetime.now(timezone.utc).strftime("%H:%M UTC")

        # ── Skip Discord if nothing changed and no alerts ────────
        if not state_changed and not alerts:
            print(f"Monitor: no state change ({current_state}) — skipping Discord")
            conn.close()
            return

        # ── Update last reported state ─────────────────────────
        cur.execute("""
            UPDATE app_private.monitor_last_report
            SET last_state = %s,
                last_reported_at = NOW()
        """, (current_state,))
        conn.commit()

        state_emoji = {"FREE": "🟢", "IN_TRIP": "🔵", "STACKED": "🟠", "UNCOMMITTED": "🟢"}
        emoji = state_emoji.get(current_state, "⚪")

        msg = f"""**🐸 PuddleJumper Monitor — {now}**
{emoji} State: **{current_state}**"""

        if state_row:
            msg += f" ({state_row['age_min']:.0f} min)"

        if stats:
            msg += f"""
📊 Last 4hrs: {stats['total_offers']} offers | {stats['accepted']} accepted | {stats['nail_its']} Nail Its
🎯 Accuracy: {stats['avg_error_m'] or 'n/a'}m avg | {stats['pct_on_target'] or 'n/a'}% on target
🔄 State transitions: {state_stats['transitions']}"""

        if alerts:
            msg += "\n\n" + "\n".join(alerts)
        else:
            msg += "\n✅ All systems normal"

        send_discord(msg)
        print(f"Monitor sent: {msg}")
        conn.close()

    except Exception as e:
        send_discord(f"❌ Monitor error: {e}")
        print(f"Monitor failed: {e}")


if __name__ == "__main__":
    run_monitor()
