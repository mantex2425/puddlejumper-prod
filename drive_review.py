"""
drive_review.py — PuddleJumper Post-Drive Narrative Report

Reconstructs a complete chronological timeline of a drive session:
  - State transitions interleaved with offers
  - Pickup/dropoff addresses
  - Decline reasons
  - Nail It confirmations with error distance
  - Triangulation source and confidence tier

Usage:
  python3 drive_review.py                        # today's drive
  python3 drive_review.py 2026-04-01             # specific date
  python3 drive_review.py 2026-04-01 2026-04-02  # date range
"""

import psycopg2
import psycopg2.extras
import sys
from datetime import datetime, date

DB_CONFIG = {
    "host":     "10.128.0.2",
    "user":     "postgres",
    "dbname":   "puddlejumper",
    "cursor_factory": psycopg2.extras.RealDictCursor
}

DRIVER_ID = "UjT1hE9eBXh2q95aSZYOkzDJ8lo1"

STATE_EMOJI = {
    "UNCOMMITTED": "🟢",
    "ENROUTE":     "🟡",
    "IN_TRIP":     "🔵",
    "STACKED":     "🟠",
    None:          "⚪"
}

TRIGGER_LABEL = {
    "offer_accepted":           "Offer accepted",
    "offer_declined":           "Ride cancelled",
    "offer_cancelled_implicit": "Ride cancelled (implicit)",
    "pickup_confirmed":         "Nail It — pickup",
    "dropoff_confirmed":        "Nail It — dropoff",
    "gps_convergence":          "GPS convergence",
    "manual_reset":             "Manual reset",
    "watchdog_auto_reset":      "Watchdog auto-reset"
}

VERDICT_EMOJI = {"ACCEPT": "✅", "DECLINE": "❌"}
SOURCE_EMOJI  = {"nail_it": "🎯", "triangulated": "📍", "geocode": "🗺️", "unresolved": "❓"}

def fmt_coords(lat, lng):
    return f"{lat:.4f}, {lng:.4f}" if lat and lng else "—"

def fmt_ts(ts):
    if not ts: return "—"
    if hasattr(ts, 'strftime'): return ts.strftime("%H:%M:%S")
    return str(ts)

def get_date_range(args):
    today = date.today().isoformat()
    if len(args) == 1:   return today, today
    elif len(args) == 2: return args[1], args[1]
    else:                return args[1], args[2]

def run():
    date_from, date_to = get_date_range(sys.argv)

    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()

    print(f"\n{'='*70}")
    print(f"  🐸 PuddleJumper Drive Review")
    print(f"  Driver: {DRIVER_ID[:16]}...")
    print(f"  Period: {date_from} → {date_to}")
    print(f"{'='*70}\n")

    # ── State transitions ─────────────────────────────────────────────────
    cur.execute("""
        SELECT 'transition' AS event_type,
               logged_at AS event_time,
               from_state, to_state, trigger_event,
               pickup_lat, pickup_lng, dropoff_lat, dropoff_lng,
               current_offer_id,
               NULL::numeric AS fare,
               NULL::text AS app_verdict,
               NULL::text AS app_reason,
               NULL::numeric AS trip_miles,
               NULL::numeric AS pickup_miles,
               NULL::numeric AS effective_hourly_rate,
               NULL::text AS pickup_address,
               NULL::text AS dropoff_address,
               NULL::text AS offer_status,
               NULL::text AS data_source,
               NULL::text AS confidence_tier,
               NULL::numeric AS triangulation_error_m,
               NULL::numeric AS actual_pickup_lat,
               NULL::numeric AS actual_pickup_lng,
               NULL::timestamptz AS actual_pickup_at,
               NULL::boolean AS is_validated
        FROM app_private.driver_trip_state_log
        WHERE driver_id = %s
          AND logged_at AT TIME ZONE 'America/Chicago' >= %s::date
          AND logged_at AT TIME ZONE 'America/Chicago' <  %s::date + INTERVAL '1 day'
    """, (DRIVER_ID, date_from, date_to))
    transitions = cur.fetchall()

    # ── Offers ────────────────────────────────────────────────────────────
    cur.execute("""
        SELECT 'offer' AS event_type,
               oh.created_at AS event_time,
               NULL::text AS from_state,
               NULL::text AS to_state,
               NULL::text AS trigger_event,
               oh.pickup_lat, oh.pickup_lng,
               oh.dropoff_lat, oh.dropoff_lng,
               NULL::text AS current_offer_id,
               oh.fare,
               oh.app_verdict,
               oh.app_reason,
               oh.trip_miles,
               oh.pickup_miles,
               oh.effective_hourly_rate,
               oh.pickup_address,
               oh.dropoff_address,
               pms.offer_status,
               pms.data_source,
               dl.decision_result->>'confidenceTier' AS confidence_tier,
               pms.triangulation_error_m,
               pms.actual_pickup_lat,
               pms.actual_pickup_lng,
               pms.actual_pickup_at,
               pms.is_validated
        FROM app_private.offer_history oh
        JOIN app_private.decision_log dl ON dl.id = oh.decision_log_id
        LEFT JOIN app_private.pickup_market_signals pms ON pms.offer_id = oh.id
        WHERE dl.driver_id = %s
          AND oh.created_at AT TIME ZONE 'America/Chicago' >= %s::date
          AND oh.created_at AT TIME ZONE 'America/Chicago' <  %s::date + INTERVAL '1 day'
    """, (DRIVER_ID, date_from, date_to))
    offers = cur.fetchall()

    # ── Summary stats ─────────────────────────────────────────────────────
    cur.execute("""
        SELECT
            COUNT(*)                                                         AS total_offers,
            SUM(CASE WHEN oh.app_verdict = 'ACCEPT' THEN 1 ELSE 0 END)      AS accepted,
            SUM(CASE WHEN oh.app_verdict = 'DECLINE' THEN 1 ELSE 0 END)     AS declined,
            ROUND(AVG(CASE WHEN oh.app_verdict = 'ACCEPT'
                THEN oh.effective_hourly_rate END)::numeric, 2)              AS avg_accepted_rate,
            ROUND(AVG(CASE WHEN oh.app_verdict = 'DECLINE'
                THEN oh.effective_hourly_rate END)::numeric, 2)              AS avg_declined_rate,
            ROUND(SUM(CASE WHEN oh.app_verdict = 'ACCEPT'
                THEN oh.fare ELSE 0 END)::numeric, 2)                        AS total_fare,
            COUNT(pms.actual_pickup_at)                                      AS nail_it_count,
            ROUND(AVG(pms.triangulation_error_m)::numeric, 0)               AS avg_error_m,
            COUNT(CASE WHEN pms.data_source = 'nail_it' THEN 1 END)         AS nail_it_source_count
        FROM app_private.offer_history oh
        JOIN app_private.decision_log dl ON dl.id = oh.decision_log_id
        LEFT JOIN app_private.pickup_market_signals pms ON pms.offer_id = oh.id
        WHERE dl.driver_id = %s
          AND oh.created_at AT TIME ZONE 'America/Chicago' >= %s::date
          AND oh.created_at AT TIME ZONE 'America/Chicago' <  %s::date + INTERVAL '1 day'
    """, (DRIVER_ID, date_from, date_to))
    stats = cur.fetchone()
    conn.close()

    # ── Merge and sort chronologically ────────────────────────────────────
    all_events = list(transitions) + list(offers)
    all_events.sort(key=lambda e: e["event_time"] or datetime.min)

    # ── RENDER: Chronological Timeline ────────────────────────────────────
    total_events = len(all_events)
    print(f"── TIMELINE ({len(transitions)} transitions, {len(offers)} offers)")
    print(f"{'─'*70}")

    if not all_events:
        print("  No events recorded for this period.\n")
    else:
        for e in all_events:
            ts = fmt_ts(e["event_time"])

            if e["event_type"] == "transition":
                from_e  = STATE_EMOJI.get(e["from_state"], "⚪")
                to_e    = STATE_EMOJI.get(e["to_state"], "⚪")
                trigger = TRIGGER_LABEL.get(e["trigger_event"], e["trigger_event"] or "—")
                print(f"  {ts}  {from_e} {e['from_state'] or '—':12} → {to_e} {e['to_state']:12}  [{trigger}]")

            else:  # offer
                v_emoji  = VERDICT_EMOJI.get(e["app_verdict"], "❓")
                fare     = f"${e['fare']:.2f}" if e["fare"] else "$—"
                rate     = f"${e['effective_hourly_rate']:.2f}/hr" if e["effective_hourly_rate"] else "—/hr"
                miles    = f"{e['trip_miles']:.1f}mi" if e["trip_miles"] else "—"
                pickup_m = f"{e['pickup_miles']:.1f}mi away" if e["pickup_miles"] else "—"
                status   = e["offer_status"] or "pending"
                src      = SOURCE_EMOJI.get(e["data_source"], "—")
                tier     = e["confidence_tier"] or "—"
                validated = "✓" if e["is_validated"] else "✗"
                addr     = (e["pickup_address"] or "")[:40] or "—"
                reason   = f" — {e['app_reason']}" if e["app_verdict"] == "DECLINE" and e["app_reason"] else ""

                print(f"  {ts}  {v_emoji} {e['app_verdict']:7} {fare:8} {rate:12} {miles:8} pickup:{pickup_m}")
                print(f"           📍 {addr}")
                if e["app_verdict"] == "DECLINE":
                    print(f"           reason: {e['app_reason'] or '—'}")
                print(f"           status:{status:12} {src} {e['data_source'] or '—':12} tier:{tier} validated:{validated}")

                if e["actual_pickup_at"]:
                    err = f"{e['triangulation_error_m']:.0f}m error" if e["triangulation_error_m"] else "no baseline"
                    print(f"           🎯 Nail It at {fmt_ts(e['actual_pickup_at'])} — {err}")
            print()

    # ── RENDER: Summary ───────────────────────────────────────────────────
    print(f"── SUMMARY")
    print(f"{'─'*70}")
    if stats and stats["total_offers"]:
        accept_rate = int(stats['accepted']) / int(stats['total_offers']) * 100
        nail_pct    = int(stats['nail_it_count']) / max(int(stats['accepted']), 1) * 100
        print(f"  Total offers:      {stats['total_offers']}")
        print(f"  Accepted:          {stats['accepted']}  |  Declined: {stats['declined']}  ({accept_rate:.0f}% accept rate)")
        print(f"  Avg accepted rate: ${stats['avg_accepted_rate']}/hr")
        avg_dec = f"${stats['avg_declined_rate']}/hr" if stats['avg_declined_rate'] else "n/a"
        print(f"  Avg declined rate: {avg_dec}  (what you avoided)")
        print(f"  Total fare:        ${stats['total_fare']}")
        print(f"  Nail Its:          {stats['nail_it_count']} ({nail_pct:.0f}% of accepted)")
        if stats['avg_error_m']:
            print(f"  Avg tri. error:    {stats['avg_error_m']:.0f}m")
        print(f"  nail_it source:    {stats['nail_it_source_count']} confirmed via GPS")
    else:
        print("  No data for this period.")
    print(f"{'='*70}\n")

if __name__ == "__main__":
    run()
