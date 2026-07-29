"""
monitor.py — PuddleJumper Session Monitor
Posts real-time updates and health checks to Discord.
Run via Cloud Scheduler every 10 minutes.

2026-05-04 Commit 3f: state-machine demolished. Watchdog rewritten to
detect stale heartbeats during active rides (current_offer_id IS NOT
NULL AND heartbeat_at < NOW() - INTERVAL '5 minutes') and ALERT only
— no auto-reset, since the new architecture has no equivalent of
DriverStateMachine.transition('watchdog_auto_reset', ...). The 1-bit
memory model means change-detection collapses from a 5-state vocabulary
(FREE/IN_TRIP/STACKED/UNCOMMITTED/ENROUTE) to a 2-state vocabulary
(IDLE/ACTIVE), driven entirely by current_offer_id presence.

Preserved: Discord webhook plumbing, 4-hour stats query, change-detection
gate against monitor_last_report (column last_state continues to drive
the "skip Discord if nothing changed" semantic).
"""

import os
import json
import urllib.request
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, timezone

WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
DRIVER_ID = "UjT1hE9eBXh2q95aSZYOkzDJ8lo1"
DB_HOST = os.environ.get("DB_HOST", "10.128.0.3")
DB_NAME = os.environ.get("DB_NAME", "puddlejumper")
DB_USER = os.environ.get("DB_USER", "atjb")
DB_PASS = os.environ.get("DB_PASSWORD", os.environ.get("DB_PASS", ""))

# Stale-heartbeat threshold: an active ride with no heartbeat in this
# many seconds triggers a Discord alert. Tuned to be louder than the
# normal 3-second polling cadence but quieter than a true device-dead
# horizon (battery dies, app force-quit). 5 minutes catches real
# pathologies without alerting on transient cellular flakiness.
STALE_HEARTBEAT_SECONDS = 300


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


def detect_stale_heartbeat(cur):
    """ALERT-only watchdog: detect active ride with stale heartbeat.

    Per RIDE_LIFECYCLE.md §3 (post-demolition architecture), an "active
    ride" is the period between FirePickup (current_offer_id ← offer)
    and FireDropoff (current_offer_id ← NULL). During that window the
    iPhone polls every 3 seconds; if heartbeat_at falls more than
    STALE_HEARTBEAT_SECONDS behind NOW(), something is wrong:
      - device dead (battery / force-quit)
      - cellular dead and no failover
      - Auto Nail It missed the dropoff and the trip is wedged

    Returns: list of alert strings. Empty list = no alert.

    NB: this function is read-only. The runbook explicitly forbids
    state mutations from monitor.py post-demolition (no equivalent
    of the old DriverStateMachine.transition watchdog_auto_reset path).
    """
    cur.execute("""
        SELECT
            current_offer_id,
            heartbeat_at,
            EXTRACT(EPOCH FROM (NOW() - heartbeat_at))::integer AS hb_age_sec
        FROM app_private.driver_trip_state
        WHERE driver_id = %s
          AND current_offer_id IS NOT NULL
          AND heartbeat_at < NOW() - INTERVAL '5 minutes'
    """, (DRIVER_ID,))
    row = cur.fetchone()
    if not row:
        return []

    age_min = (row['hb_age_sec'] or 0) / 60.0
    return [
        f"⚠️ **Stale heartbeat during active ride**: offer={row['current_offer_id']} "
        f"last heartbeat {age_min:.0f} min ago (threshold: 5 min)"
    ]


def run_monitor():
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # ── Current 1-bit memory state (HINT post-Sub-commit 1c) ──
        # See driver_queue.py / L-19. This composite SELECT pulls
        # current_offer_id alongside heartbeat fields for the dashboard
        # render; treat the value as advisory. For authoritative queue
        # membership, route through DriverQueue.snapshot(). The IDLE/ACTIVE
        # display can briefly lag during an L-19-class self-heal but
        # converges within one heartbeat tick.
        cur.execute("""
            SELECT
                current_offer_id,
                heartbeat_at,
                EXTRACT(EPOCH FROM (NOW() - heartbeat_at))::integer AS hb_age_sec
            FROM app_private.driver_trip_state
            WHERE driver_id = %s
        """, (DRIVER_ID,))
        state_row = cur.fetchone()

        # IDLE = no active offer; ACTIVE = pickup fired, dropoff not yet.
        # Maps to FirePickup/FireDropoff in driver_heartbeat.py: FirePickup
        # binds current_offer_id (now via DriverQueue.bind), FireDropoff
        # unbinds (DriverQueue.unbind). The 1-bit memory model is the
        # entire state surface.
        current_state = "ACTIVE" if (state_row and state_row['current_offer_id']) else "IDLE"

        # ── Session stats (last 4 hours) — observability only ──
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

        # ── Stale-heartbeat watchdog (ALERT-only, no mutations) ────
        alerts = detect_stale_heartbeat(cur)

        # ── Triangulation health alerts ────────────────────────
        if stats and stats['total_offers'] and stats['total_offers'] > 3:
            if stats['avg_error_m'] and float(stats['avg_error_m']) > 800:
                alerts.append(f"⚠️ High triangulation error: {stats['avg_error_m']}m avg")
            nail_rate = (stats['nail_its'] or 0) / max(stats['accepted'] or 1, 1)
            if nail_rate < 0.4 and (stats['accepted'] or 0) > 10:
                alerts.append(f"⚠️ Low Nail It rate: {nail_rate:.0%} — Auto Nail It may not be firing")

        # ── Last reported state (change-detection gate) ────────
        cur.execute("SELECT last_state FROM app_private.monitor_last_report")
        last_report = cur.fetchone()
        last_state = last_report['last_state'] if last_report else None
        state_changed = current_state != last_state

        # ── Skip Discord if nothing changed and no alerts ──────
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

        # ── Build message ──────────────────────────────────────
        now = datetime.now(timezone.utc).strftime("%H:%M UTC")
        state_emoji = {"IDLE": "🟢", "ACTIVE": "🔵"}
        emoji = state_emoji.get(current_state, "⚪")

        msg = f"**🐸 PuddleJumper Monitor — {now}**\n"
        msg += f"{emoji} State: **{current_state}**"
        if current_state == "ACTIVE" and state_row and state_row.get('current_offer_id'):
            msg += f" (offer={state_row['current_offer_id']})"
        if state_row and state_row.get('hb_age_sec') is not None:
            hb_age = state_row['hb_age_sec']
            if hb_age < 60:
                msg += f" — last heartbeat {hb_age}s ago"
            else:
                msg += f" — last heartbeat {hb_age // 60}m ago"

        if stats:
            msg += (
                f"\n📊 Last 4hrs: {stats['total_offers']} offers"
                f" | {stats['accepted']} accepted"
                f" | {stats['nail_its']} Nail Its"
                f"\n🎯 Accuracy: {stats['avg_error_m'] or 'n/a'}m avg"
                f" | {stats['pct_on_target'] or 'n/a'}% on target"
            )

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