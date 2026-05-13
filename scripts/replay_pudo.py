#!/usr/bin/env python3
"""
replay_pudo.py — forensic replay of historical rides through the live
PUDO matcher pipeline.

Usage:
    python3 scripts/replay_pudo.py <offer_id>
    python3 scripts/replay_pudo.py <offer_id> --time-window-utc "2026-05-01 18:55:00,2026-05-01 19:10:00"
    python3 scripts/replay_pudo.py <offer_id> --backfill-heartbeat-log

What it does
------------
For an offer in offer_history, this harness:

1. Loads the offer (geocoded pickup/dropoff coords, address text, classifications)
2. Loads historical GPS frames for that driver in the offer's window
3. For each frame, in chronological order:
   a. Inserts a synthetic heartbeat_log row at that frame's timestamp (so the
      matchers' internal heartbeat_log queries see the real data)
   b. Constructs the offer queue (Offer dataclass with TargetSpec for pickup
      and dropoff) — same shape _project_queue produces
   c. Calls cluster_detection.detect_cluster() to see what cluster forms
   d. Calls WhereAmI._evaluate() against the cluster + queue
   e. Calls dispatch() against the matches + queue
   f. Records the outcome to a per-frame timeline

Output: a human-readable timeline showing exactly what the matchers saw and
decided at each frame. Useful for:
  - Diagnosing why a real ride didn't fire (e.g., yesterday's two rides during
    the heartbeat_log regression, where the system was blind)
  - Testing matcher tuning hypothetically ("if I lower the cluster threshold,
    does this curbside-walkup ride fire?") without deploying
  - Generating ground-truth expectations for Bruno regression tests

Important constraints
---------------------
- Runs on the VM (where puddlejumper imports + DB live)
- Uses a SAVEPOINT-style transaction discipline: every synthetic INSERT is
  rolled back at the end, so this is a non-destructive read-mostly tool. No
  pollution of production tables.
- Only inserts into a TEMP table that mirrors heartbeat_log's schema, then
  monkey-patches the matcher's heartbeat_log reads to use the temp table
  during replay. (Cleaner alternatives like passing a fixture-cur exist but
  require more refactoring; this gets us to forensic-replay quickly.)

Limitations
-----------
- Some matchers may use other tables (driver_trip_state, etc.) that aren't
  part of this replay. Output may diverge slightly from a true production
  replay where those side-effects matter. Worth knowing for interpretation.
- Reverse-geocoding (Google Places API) is called for real if a matcher
  invokes it. This will use real API quota. Watch for costs.
- The 'cluster' object returned by detect_cluster comes from the temp table;
  WAI's downstream lookups (recent_clusters, pivot_context) may also touch
  heartbeat_log and require the same temp-table redirection. We patch all
  three call sites.
"""
import argparse
import datetime
import os
import sys
import textwrap

# Add the puddlejumper-prod root to sys.path so we can import the matchers
# (this script lives in puddlejumper-prod/scripts/ so go up one)
HERE = os.path.dirname(os.path.abspath(__file__))
PROD_ROOT = os.path.dirname(HERE)
sys.path.insert(0, PROD_ROOT)

import psycopg2
import psycopg2.extras

# Now import the production matchers
from where_am_i import WhereAmI  # noqa: E402
from cluster_detection import detect_cluster, get_recent_clusters, Cluster  # noqa: E402
from dispatch import dispatch  # noqa: E402
from pudo_types import Offer, OfferMeta, TargetSpec  # noqa: E402
from bead_on_wire import classify_address  # noqa: E402

import math


def _haversine_m(lat1, lng1, lat2, lng2):
    R_M = 6371000.0
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlng / 2) ** 2)
    return 2 * R_M * math.asin(math.sqrt(a))


def make_replay_cluster_fn(frames, current_idx_holder):
    """Returns a (driver_id, cur) -> Optional[Cluster] function that
    operates on an in-memory frame list.

    Mirrors cluster_detection.detect_cluster verbatim:
      - Look back window_sec=60 from the current frame's timestamp
      - Find the most recent uninterrupted run of low-speed (<10 mph) frames
      - Require >=3 samples
      - Compute median lat/lng, spread, duration
      - Reject if spread > 25m
    """
    WINDOW_SEC = 60
    MIN_SAMPLES = 3
    MAX_SPEED_MPH = 10.0
    MAX_SPREAD_M = 25.0

    def fn(driver_id, cur):
        idx = current_idx_holder[0]
        if idx is None or idx >= len(frames):
            return None
        now_ts = frames[idx]['ts']
        cutoff = now_ts - datetime.timedelta(seconds=WINDOW_SEC)

        # Frames in the window, current and older, newest first
        in_window = [f for f in frames[:idx + 1] if f['ts'] >= cutoff]
        in_window.sort(key=lambda f: f['ts'], reverse=True)

        # Walk newest-first; the "current run" stops at the first sample
        # whose speed >= MAX_SPEED_MPH (the breaks_before guard)
        current_run = []
        for f in in_window:
            speed = f.get('speed_mph')
            if speed is None:
                speed = 0.0
            if speed >= MAX_SPEED_MPH:
                break
            current_run.append(f)

        if len(current_run) < MIN_SAMPLES:
            return None

        # Median lat/lng (sorted, middle element)
        lats = sorted(float(f['lat']) for f in current_run)
        lngs = sorted(float(f['lng']) for f in current_run)
        n = len(current_run)
        if n % 2 == 1:
            median_lat = lats[n // 2]
            median_lng = lngs[n // 2]
        else:
            median_lat = (lats[n // 2 - 1] + lats[n // 2]) / 2
            median_lng = (lngs[n // 2 - 1] + lngs[n // 2]) / 2

        # Spread (max distance from median, in meters)
        spread_m = max(
            _haversine_m(float(f['lat']), float(f['lng']), median_lat, median_lng)
            for f in current_run
        )
        if spread_m > MAX_SPREAD_M:
            return None

        # Duration: earliest to latest in the current run
        timestamps = [f['ts'] for f in current_run]
        earliest = min(timestamps)
        latest = max(timestamps)
        duration_s = (latest - earliest).total_seconds()

        return Cluster(
            n=n,
            median_lat=median_lat,
            median_lng=median_lng,
            spread_m=spread_m,
            duration_s=duration_s,
            latest=latest,
        )

    return fn


def make_replay_recent_clusters_fn():
    """Returns an empty-list recent-clusters function. The Memory Eye
    cluster-history lookback isn't relevant for forensic replay of a single
    ride."""
    def fn(driver_id, cur, accepted_at_anchor=None):
        return []
    return fn


DB_HOST = "10.128.0.2"
DB_USER = "postgres"
DB_NAME = "puddlejumper"


# ─── DB connection ────────────────────────────────────────────────────────────

def connect():
    return psycopg2.connect(
        host=DB_HOST, user=DB_USER, dbname=DB_NAME,
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


# ─── Offer loading ────────────────────────────────────────────────────────────

def load_offer(cur, offer_id):
    cur.execute("""
        SELECT o.id, o.created_at, d.driver_id,
               o.pickup_address, o.pickup_lat, o.pickup_lng, o.pickup_minutes,
               o.dropoff_address, o.dropoff_lat, o.dropoff_lng, o.trip_minutes,
               o.fare, o.app_verdict,
               o.pickup_classification, o.dropoff_classification
        FROM app_private.offer_history o
        JOIN app_private.decision_log d ON d.id = o.decision_log_id
        WHERE o.id = %s
    """, (offer_id,))
    row = cur.fetchone()
    if not row:
        sys.exit(f"FATAL: no offer with id={offer_id}")
    return row


def build_target_spec(address_text, lat, lng):
    """Lift of driver_heartbeat.py:_bucket_to_target_spec.

    Classifies the address via bead_on_wire and constructs the appropriate
    TargetSpec (address_class + named_roads) per the §5.1 matching rule.
    Returns None for unrecognized address shapes ("garbage" bucket).
    """
    if not address_text or lat is None or lng is None:
        return None
    try:
        classification = classify_address(address_text)
    except Exception as e:
        print(f"  WARNING: classify_address failed for {address_text!r}: {e}")
        return None
    if not classification:
        return None

    bucket = classification.get("bucket")
    parts = classification.get("parts", {})

    if bucket == "intersection":
        return TargetSpec(
            lat=float(lat), lng=float(lng),
            address_class="intersection",
            named_roads=(parts.get("road_a", ""), parts.get("road_b", "")),
        )
    if bucket == "street_number":
        return TargetSpec(
            lat=float(lat), lng=float(lng),
            address_class="number_on_street",
            named_roads=(parts.get("road", ""),),
        )
    if bucket == "single_road":
        return TargetSpec(
            lat=float(lat), lng=float(lng),
            address_class="single_road",
            named_roads=(parts.get("road", ""),),
        )
    if bucket == "poi":
        return TargetSpec(
            lat=float(lat), lng=float(lng),
            address_class="poi",
            named_roads=(),
        )
    # "garbage" or any unrecognized bucket -> unevaluatable
    return None


def build_offer_queue(offer_row):
    """Build an Offer dataclass list with the offer in it."""
    pickup = build_target_spec(
        offer_row['pickup_address'], offer_row['pickup_lat'], offer_row['pickup_lng']
    )
    dropoff = build_target_spec(
        offer_row['dropoff_address'], offer_row['dropoff_lat'], offer_row['dropoff_lng']
    )
    if pickup is None or dropoff is None:
        sys.exit("FATAL: could not build target specs (pickup or dropoff is unbuildable)")
    return [Offer(
        offer_id=str(offer_row['id']),
        accepted_at=offer_row['created_at'],
        pickup=pickup,
        dropoff=dropoff,
    )]


# ─── GPS source detection (same as harvest_ride.py) ───────────────────────────

def detect_gps_source(cur, driver_id, window_start, window_end):
    cur.execute("""
        SELECT COUNT(*) AS n FROM app_private.heartbeat_log
        WHERE driver_id = %s AND logged_at BETWEEN %s AND %s
    """, (driver_id, window_start, window_end))
    if cur.fetchone()['n'] > 0:
        return 'heartbeat_log'

    cur.execute("""
        SELECT COUNT(*) AS n FROM app_private.pudo_decision_context
        WHERE driver_id = %s AND created_at BETWEEN %s AND %s
          AND lat IS NOT NULL AND lng IS NOT NULL
    """, (driver_id, window_start, window_end))
    if cur.fetchone()['n'] > 0:
        return 'pudo_decision_context'

    return None


def fetch_frames(cur, driver_id, window_start, window_end, source):
    if source == 'heartbeat_log':
        cur.execute("""
            SELECT logged_at AS ts, lat, lng, speed_mph, gps_accuracy_m
            FROM app_private.heartbeat_log
            WHERE driver_id = %s AND logged_at BETWEEN %s AND %s
              AND lat IS NOT NULL AND lng IS NOT NULL
            ORDER BY logged_at ASC
        """, (driver_id, window_start, window_end))
    elif source == 'pudo_decision_context':
        cur.execute("""
            SELECT created_at AS ts, lat, lng, speed_mph, gps_accuracy_m
            FROM app_private.pudo_decision_context
            WHERE driver_id = %s AND created_at BETWEEN %s AND %s
              AND lat IS NOT NULL AND lng IS NOT NULL
            ORDER BY created_at ASC
        """, (driver_id, window_start, window_end))
    else:
        return []
    return list(cur.fetchall())


# ─── Backfill discipline ──────────────────────────────────────────────────────
# To run cluster_detection against historical data, the data must exist in
# heartbeat_log. We backfill rows from pudo_decision_context into a SAVEPOINT
# that we roll back at the end. This is non-destructive and safe.

def backfill_heartbeat_log(cur, driver_id, frames):
    """Insert frames into heartbeat_log within the current transaction.

    Returns the count inserted. Caller must arrange to ROLLBACK or use a
    SAVEPOINT to discard these inserts after replay.
    """
    cur.execute("SAVEPOINT replay_backfill")
    inserted = 0
    for f in frames:
        speed = f.get('speed_mph')
        if speed is None:
            speed = 0.0  # safe default for cluster_detection's speed filter
        accuracy = f.get('gps_accuracy_m')
        if accuracy is None:
            accuracy = 5.0
        cur.execute("""
            INSERT INTO app_private.heartbeat_log
              (driver_id, logged_at, lat, lng, speed_mph, gps_accuracy_m,
               state, current_offer_id)
            VALUES (%s, %s, %s, %s, %s, %s, 'UNCOMMITTED', NULL)
        """, (driver_id, f['ts'], f['lat'], f['lng'], speed, accuracy))
        inserted += 1
    return inserted


def rollback_backfill(cur):
    cur.execute("ROLLBACK TO SAVEPOINT replay_backfill")


# ─── Main replay loop ─────────────────────────────────────────────────────────

def replay(offer_id, time_window_utc=None, backfill=False):
    conn = connect()
    cur = conn.cursor()

    offer = load_offer(cur, offer_id)
    print(f"  Offer {offer['id']}: {offer['app_verdict']} from {offer['driver_id']}")
    print(f"    Pickup:  {offer['pickup_address']}  ({offer['pickup_lat']}, {offer['pickup_lng']})")
    print(f"    Dropoff: {offer['dropoff_address']}  ({offer['dropoff_lat']}, {offer['dropoff_lng']})")
    print(f"    Created: {offer['created_at']}")
    print()

    # Window
    if time_window_utc:
        start_str, end_str = time_window_utc.split(',')
        window_start = datetime.datetime.strptime(start_str.strip(), '%Y-%m-%d %H:%M:%S').replace(tzinfo=datetime.timezone.utc)
        window_end = datetime.datetime.strptime(end_str.strip(), '%Y-%m-%d %H:%M:%S').replace(tzinfo=datetime.timezone.utc)
    else:
        window_start = offer['created_at'] - datetime.timedelta(minutes=10)
        window_end = offer['created_at'] + datetime.timedelta(hours=2)
    print(f"  Window: {window_start}  ->  {window_end}")

    source = detect_gps_source(cur, offer['driver_id'], window_start, window_end)
    if source is None:
        sys.exit(f"FATAL: no GPS data in window")
    print(f"  GPS source: {source}")

    frames = fetch_frames(cur, offer['driver_id'], window_start, window_end, source)
    print(f"  Frames: {len(frames)}")
    if not frames:
        sys.exit("FATAL: zero frames")
    print()

    # Build the queue (single offer)
    queue = build_offer_queue(offer)

    # Walk frames
    print(f"  {'='*78}")
    print(f"  Replay timeline (PUDO matcher outcomes per frame)")
    print(f"  {'='*78}")
    print()

    # Inject in-memory cluster + recent-clusters functions so WAI doesn't
    # call cluster_detection.detect_cluster (which uses NOW()-anchored SQL
    # invisible to historical data).
    current_idx_holder = [None]  # mutable cell so closure can read it
    cluster_fn = make_replay_cluster_fn(frames, current_idx_holder)
    recent_clusters_fn = make_replay_recent_clusters_fn()
    wai = WhereAmI(cur, _cluster_fn=cluster_fn, _recent_clusters_fn=recent_clusters_fn)

    last_planner_action = None
    fire_pickup_seen_at = None

    for i, frame in enumerate(frames):
        # Tell the in-memory cluster function which frame is "now"
        current_idx_holder[0] = i

        ts_ct = frame['ts'].astimezone(datetime.timezone(datetime.timedelta(hours=-5)))
        ts_label = ts_ct.strftime('%H:%M:%S')

        # In-memory cluster detection (does NOT touch the DB)
        try:
            cluster = cluster_fn(offer['driver_id'], cur)
        except Exception as e:
            print(f"  {ts_label}  CLUSTER ERROR: {e}")
            continue

        if cluster is None:
            cluster_summary = "(no cluster)"
        else:
            cluster_summary = (
                f"n={cluster.n} dur={cluster.duration_s:.0f}s spread={cluster.spread_m:.1f}m "
                f"@ ({cluster.median_lat:.5f}, {cluster.median_lng:.5f})"
            )

        # WAI evaluate
        try:
            matches, diagnostics = wai.evaluate_with_diagnostics(offer['driver_id'], queue)
        except Exception as e:
            print(f"  {ts_label}  WAI ERROR: {e}")
            continue

        if matches:
            match_summary = ", ".join(
                f"{m.location_type}@{m.confidence:.2f}" for m in matches
            )
        else:
            match_summary = "(no matches)"

        # Dispatch
        try:
            actions = dispatch(
                matches,
                current_offer_id=None,
                queue_metadata={str(offer['id']): OfferMeta(created_at=offer['created_at'])},
            )
        except Exception as e:
            print(f"  {ts_label}  DISPATCH ERROR: {e}")
            continue

        action_summary = ", ".join(type(a).__name__ for a in actions) if actions else "(no actions)"
        primary_action = type(actions[0]).__name__ if actions else None

        # Print only when something changes (cluster forms/grows, action changes)
        # to keep the output readable for long rides.
        line = f"  {ts_label}  ({frame['lat']:.5f}, {frame['lng']:.5f})  cluster={cluster_summary}  matches={match_summary}  action={action_summary}"
        if primary_action != last_planner_action or i == 0 or i == len(frames) - 1:
            print(line)
            last_planner_action = primary_action

        if primary_action == "FirePickup" and fire_pickup_seen_at is None:
            fire_pickup_seen_at = ts_label

    print()
    print(f"  {'='*78}")
    print(f"  Summary")
    print(f"  {'='*78}")
    if fire_pickup_seen_at:
        print(f"  ✓ FirePickup fired at {fire_pickup_seen_at}")
    else:
        print(f"  ✗ FirePickup never fired")
    print()

    # No backfill, nothing to roll back.
    conn.rollback()  # discard the read-only transaction
    conn.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('offer_id', type=int)
    parser.add_argument('--time-window-utc', default=None,
                        help='UTC window: "YYYY-MM-DD HH:MM:SS,YYYY-MM-DD HH:MM:SS"')
    parser.add_argument('--backfill-heartbeat-log', action='store_true',
                        help="Force backfill (default: only when GPS source is pudo_decision_context)")
    args = parser.parse_args()

    replay(args.offer_id, args.time_window_utc, args.backfill_heartbeat_log)


if __name__ == "__main__":
    main()