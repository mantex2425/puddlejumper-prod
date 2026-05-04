#!/usr/bin/env python3
"""
harvest_ride.py — convert a historical ride into a replayable Bruno test folder.

Usage:
    python3 harvest_ride.py <offer_id>
    python3 harvest_ride.py <offer_id> --output-dir /tmp/bruno_replays
    python3 harvest_ride.py <offer_id> --topology "intersection_residential"

Output: a Bruno folder at /tmp/bruno_replays/<folder_name>/ ready to scp to
your Mac and drop into the Bruno collection.

Design
------
GPS source auto-selection:
  1. heartbeat_log if it has rows in the offer's window (the canonical source)
  2. pudo_decision_context as fallback (lower-fidelity but contains lat/lng
     for rides during the heartbeat_log regression blackout)
  3. exit non-zero if neither has data

Drive window bounding:
  Offer timestamps don't reliably bound the drive (driver may see offer 20+
  min before pickup, may take detours, etc.). The actual drive window is:
    start = first GPS sample within 0.05mi of pickup pin (or offer.created_at,
            whichever is later — pickup observation is the floor)
    end   = last GPS sample within 0.05mi of dropoff pin (or
            offer.created_at + 1 hour, whichever is earlier — sane upper bound)
  If pickup or dropoff was never approached, falls back to a generous default
  window from the offer timestamps.

Asymmetric subsampling:
  - DWELL_ZONE = within 80m of pickup or dropoff: keep EVERY frame
  - CRUISE = mid-trip motion: keep one frame per 30 seconds
  - Cap total at MAX_FRAMES_PER_REPLAY (60). If still over, increase the
    cruise stride.

Speed derivation:
  When speed_mph is NULL (e.g., pudo_decision_context source), compute
  haversine distance / time delta between successive frames. Express in mph.
  Edge frames (no successor) inherit the previous frame's speed.

Output:
  /tmp/bruno_replays/<folder_name>/
    01 Reset (before).bru
    02 Seed Offer.bru
    03..NN HB t=<elapsed>.bru
    NN+1 Verify FirePickup.bru
    NN+2 Verify FireDropoff.bru          (only if dropoff approached)
    99 Reset (after).bru
    README.md                             (offer metadata + harvest notes)
"""

import argparse
import datetime
import math
import os
import sys
import textwrap

import psycopg2
import psycopg2.extras

# ─── Configuration ────────────────────────────────────────────────────────────

DB_HOST = "10.128.0.2"
DB_USER = "postgres"
DB_NAME = "puddlejumper"

# Test driver UID — only this driver is allowed in test_endpoints allowlist
TEST_DRIVER_ID = "C7FRKHbnvFcDPZ0RcXXjtPwGXgw2"

# Asymmetric subsampling parameters
DWELL_RADIUS_MILES = 0.05            # ~80m — frames within this are "at the pin"
CRUISE_STRIDE_SECONDS = 30.0         # mid-trip frame stride
MAX_FRAMES_PER_REPLAY = 60           # cap on heartbeats per Bruno folder

# Drive window bounding
# (Removed in favor of duration-scaled bounding in bound_drive_window)

# Default GPS accuracy if missing
DEFAULT_GPS_ACCURACY_M = 5.0


# ─── Math helpers ─────────────────────────────────────────────────────────────

def haversine_miles(lat1, lng1, lat2, lng2):
    """Great-circle distance in miles between two (lat, lng) pairs."""
    R_MILES = 3958.7613
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlng / 2) ** 2)
    c = 2 * math.asin(math.sqrt(a))
    return R_MILES * c


# ─── DB helpers ───────────────────────────────────────────────────────────────

def connect():
    return psycopg2.connect(
        host=DB_HOST, user=DB_USER, dbname=DB_NAME,
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


def fetch_offer(cur, offer_id):
    cur.execute("""
        SELECT o.id, o.created_at, d.driver_id,
               o.pickup_address, o.pickup_lat, o.pickup_lng, o.pickup_minutes,
               o.dropoff_address, o.dropoff_lat, o.dropoff_lng, o.trip_minutes,
               o.actual_pickup_at, o.actual_pickup_lat, o.actual_pickup_lng,
               o.actual_dropoff_at, o.actual_dropoff_lat, o.actual_dropoff_lng,
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


def detect_gps_source(cur, driver_id, window_start, window_end):
    """Return 'heartbeat_log' or 'pudo_decision_context' or None."""
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


def fetch_gps_frames(cur, driver_id, window_start, window_end, source):
    """Return list of dicts: {ts, lat, lng, speed_mph, gps_accuracy_m}."""
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


# ─── Window bounding & subsampling ────────────────────────────────────────────

def bound_drive_window(offer):
    """Return (window_start, window_end) for fetching GPS.

    Uses actual_dropoff_at if known (real dropoff observed). Otherwise
    falls back to predicted duration * 3.0x (Houston-tax ceiling for
    rain/traffic/detour cases). We tighten via GPS proximity after fetch.
    """
    start = offer['created_at']
    if offer.get('actual_dropoff_at'):
        end = offer['actual_dropoff_at'] + datetime.timedelta(minutes=5)
    else:
        predicted_minutes = (offer.get('pickup_minutes') or 5) + (offer.get('trip_minutes') or 30)
        houston_tax_mult = 3.0
        end = start + datetime.timedelta(minutes=predicted_minutes * houston_tax_mult)
    return start, end


def tighten_window_by_gps(frames, offer):
    """Tighten the window using GPS proximity to pickup/dropoff pins.

    Returns (frames_subset, pickup_first_idx, dropoff_last_idx). If pickup
    or dropoff was never approached, the corresponding idx is None.
    """
    pickup_lat = float(offer['pickup_lat'])
    pickup_lng = float(offer['pickup_lng'])
    dropoff_lat = float(offer['dropoff_lat']) if offer['dropoff_lat'] else None
    dropoff_lng = float(offer['dropoff_lng']) if offer['dropoff_lng'] else None

    pickup_first_idx = None
    dropoff_last_idx = None

    for i, f in enumerate(frames):
        if pickup_first_idx is None:
            d = haversine_miles(f['lat'], f['lng'], pickup_lat, pickup_lng)
            if d <= DWELL_RADIUS_MILES:
                pickup_first_idx = i
        if dropoff_lat is not None:
            d = haversine_miles(f['lat'], f['lng'], dropoff_lat, dropoff_lng)
            if d <= DWELL_RADIUS_MILES:
                dropoff_last_idx = i  # keep updating — want the LAST one

    # Determine slice bounds
    # Start: 60s before pickup_first if pickup was approached, else frame 0
    # End: 60s after dropoff_last if dropoff was approached, else last frame
    if pickup_first_idx is not None:
        approach_start_ts = frames[pickup_first_idx]['ts'] - datetime.timedelta(seconds=60)
        slice_start = next(
            (i for i, f in enumerate(frames) if f['ts'] >= approach_start_ts), 0
        )
    else:
        slice_start = 0

    if dropoff_last_idx is not None:
        depart_end_ts = frames[dropoff_last_idx]['ts'] + datetime.timedelta(seconds=60)
        slice_end_iter = (i for i, f in enumerate(frames) if f['ts'] > depart_end_ts)
        slice_end = next(slice_end_iter, len(frames))
    else:
        slice_end = len(frames)

    sliced = frames[slice_start:slice_end]
    # Re-index pickup_first_idx and dropoff_last_idx into the sliced array
    new_pickup_idx = (pickup_first_idx - slice_start) if pickup_first_idx is not None else None
    new_dropoff_idx = (dropoff_last_idx - slice_start) if dropoff_last_idx is not None else None
    return sliced, new_pickup_idx, new_dropoff_idx


def subsample(frames, offer):
    """Asymmetric subsampling.

    Returns list of frames preserved for replay.
    """
    pickup_lat = float(offer['pickup_lat'])
    pickup_lng = float(offer['pickup_lng'])
    dropoff_lat = float(offer['dropoff_lat']) if offer['dropoff_lat'] else None
    dropoff_lng = float(offer['dropoff_lng']) if offer['dropoff_lng'] else None

    def in_dwell(f):
        if haversine_miles(f['lat'], f['lng'], pickup_lat, pickup_lng) <= DWELL_RADIUS_MILES:
            return True
        if dropoff_lat is not None and haversine_miles(
            f['lat'], f['lng'], dropoff_lat, dropoff_lng
        ) <= DWELL_RADIUS_MILES:
            return True
        return False

    kept = []
    last_cruise_ts = None
    stride = CRUISE_STRIDE_SECONDS
    for f in frames:
        if in_dwell(f):
            kept.append(f)
            last_cruise_ts = f['ts']
        else:
            if last_cruise_ts is None or (f['ts'] - last_cruise_ts).total_seconds() >= stride:
                kept.append(f)
                last_cruise_ts = f['ts']

    # If still over the cap, increase stride and redo
    while len(kept) > MAX_FRAMES_PER_REPLAY:
        stride *= 1.5
        kept = []
        last_cruise_ts = None
        for f in frames:
            if in_dwell(f):
                kept.append(f)
                last_cruise_ts = f['ts']
            else:
                if last_cruise_ts is None or (f['ts'] - last_cruise_ts).total_seconds() >= stride:
                    kept.append(f)
                    last_cruise_ts = f['ts']

    return kept


def derive_speeds(frames):
    """Fill in speed_mph where missing using lat/lng deltas."""
    if not frames:
        return frames
    for i, f in enumerate(frames):
        if f.get('speed_mph') is not None:
            continue
        # Try forward delta first
        if i + 1 < len(frames):
            ahead = frames[i + 1]
            dt = (ahead['ts'] - f['ts']).total_seconds()
            if dt > 0:
                d = haversine_miles(f['lat'], f['lng'], ahead['lat'], ahead['lng'])
                f['speed_mph'] = d / (dt / 3600.0)
                continue
        # Fall back to backward delta
        if i > 0:
            prev = frames[i - 1]
            dt = (f['ts'] - prev['ts']).total_seconds()
            if dt > 0:
                d = haversine_miles(prev['lat'], prev['lng'], f['lat'], f['lng'])
                f['speed_mph'] = d / (dt / 3600.0)
                continue
        f['speed_mph'] = 0.0  # singleton frame, can't derive
    # Same fallback for accuracy
    for f in frames:
        if f.get('gps_accuracy_m') is None:
            f['gps_accuracy_m'] = DEFAULT_GPS_ACCURACY_M
    return frames


# ─── Bruno folder writer ──────────────────────────────────────────────────────

def safe_folder_name(offer):
    """Produce a filesystem-safe folder name."""
    date_str = offer['created_at'].astimezone(datetime.timezone.utc).strftime('%Y-%m-%d')
    pickup = (offer['pickup_address'] or 'unknown_pickup')[:40]
    pickup = ''.join(c if c.isalnum() or c in ' -.,' else '_' for c in pickup).strip()
    classification = offer['pickup_classification'] or 'unclassified'
    return f"PUDO Replay - {date_str} - {pickup} ({classification})"


def write_reset(folder, name, seq, when):
    """Write a Reset request file."""
    content = textwrap.dedent(f"""\
        meta {{
          name: {name}
          type: http
          seq: {seq}
        }}

        post {{
          url: {{{{BASE_URL}}}}/api/v1/test/reset_driver
          body: none
          auth: bearer
        }}

        auth:bearer {{
          token: {{{{JWT_TOKEN}}}}
        }}

        settings {{
          encodeUrl: true
          timeout: 0
        }}
        """)
    with open(os.path.join(folder, f"{seq:02d} Reset ({when}).bru"), 'w') as f:
        f.write(content)


def write_seed(folder, seq, offer):
    """Write the seed_offer request that recreates the historical offer."""
    body = textwrap.dedent(f"""\
        {{
          "pickup_lat":      {offer['pickup_lat']},
          "pickup_lng":      {offer['pickup_lng']},
          "pickup_address":  "{offer['pickup_address'] or ''}",
          "pickup_miles":    1.2,
          "pickup_minutes":  {offer['pickup_minutes'] or 5},
          "dropoff_lat":     {offer['dropoff_lat']},
          "dropoff_lng":     {offer['dropoff_lng']},
          "dropoff_address": "{offer['dropoff_address'] or ''}",
          "trip_miles":      10.5,
          "trip_minutes":    {offer['trip_minutes'] or 15},
          "fare":            {offer['fare'] or 0},
          "app_verdict":     "{offer['app_verdict'] or 'ACCEPT'}",
          "set_active":      false
        }}""")
    content = textwrap.dedent(f"""\
        meta {{
          name: {seq:02d} Seed Offer (replay of {offer['id']})
          type: http
          seq: {seq}
        }}

        post {{
          url: {{{{BASE_URL}}}}/api/v1/test/seed_offer
          body: json
          auth: bearer
        }}

        auth:bearer {{
          token: {{{{JWT_TOKEN}}}}
        }}

        body:json {{
        {body}
        }}

        script:post-response {{
          bru.setVar("seeded_offer_id", String(res.getBody().offer_id));
          bru.setVar("dwell_zone_fired", false);
        }}

        tests {{
          test("status 201", function() {{
            expect(res.getStatus()).to.equal(201);
          }});
          test("offer_id returned", function() {{
            expect(res.getBody().offer_id).to.be.a("number");
          }});
        }}

        settings {{
          encodeUrl: true
          timeout: 0
        }}
        """)
    with open(os.path.join(folder, f"{seq:02d} Seed Offer.bru"), 'w') as f:
        f.write(content)


def write_heartbeat(folder, seq, frame, t0, cumulative_miles, capture):
    """Write one heartbeat .bru file."""
    elapsed = (frame['ts'] - t0).total_seconds()
    capture_block = (
        '  if (res.getBody().voice === "Pickup confirmed") {\n'
        '    bru.setVar("dwell_zone_fired", true);\n'
        '  }'
        if capture else ''
    )
    speed = round(frame['speed_mph'] or 0.0, 2)
    accuracy = round(frame['gps_accuracy_m'] or DEFAULT_GPS_ACCURACY_M, 2)
    content = textwrap.dedent(f"""\
        meta {{
          name: {seq:02d} HB t={elapsed:.0f}s
          type: http
          seq: {seq}
        }}

        post {{
          url: {{{{BASE_URL}}}}/api/v1/driver/heartbeat
          body: json
          auth: bearer
        }}

        auth:bearer {{
          token: {{{{JWT_TOKEN}}}}
        }}

        body:json {{
          {{
            "lat":              {frame['lat']:.6f},
            "lng":              {frame['lng']:.6f},
            "speed_mph":        {speed},
            "gps_accuracy_m":   {accuracy},
            "cumulative_miles": {cumulative_miles:.2f}
          }}
        }}

        script:post-response {{
        {capture_block}
        }}

        tests {{
          test("status 200", function() {{
            expect(res.getStatus()).to.equal(200);
          }});
        }}

        settings {{
          encodeUrl: true
          timeout: 0
        }}
        """)
    with open(os.path.join(folder, f"{seq:02d} HB t={elapsed:.0f}s.bru"), 'w') as f:
        f.write(content)


def write_verify_pickup(folder, seq):
    content = textwrap.dedent(f"""\
        meta {{
          name: {seq:02d} Verify FirePickup
          type: http
          seq: {seq}
        }}

        get {{
          url: {{{{BASE_URL}}}}/api/v1/driver/status
          body: none
          auth: bearer
        }}

        auth:bearer {{
          token: {{{{JWT_TOKEN}}}}
        }}

        tests {{
          test("status 200", function() {{
            expect(res.getStatus()).to.equal(200);
          }});
          test("FirePickup fired sometime during replay", function() {{
            expect(bru.getVar("dwell_zone_fired")).to.equal(true);
          }});
          test("FirePickup with dispatch_executed in last 3 actions", function() {{
            var actions = res.getBody().last_3_dispatch_actions;
            expect(actions).to.be.an("array");
            var fired = actions.find(function(a) {{
              return a.planner_action === "FirePickup"
                && a.dispatch_executed === true;
            }});
            expect(fired, "no FirePickup with dispatch_executed=true").to.not.be.undefined;
          }});
        }}

        settings {{
          encodeUrl: true
          timeout: 0
        }}
        """)
    with open(os.path.join(folder, f"{seq:02d} Verify FirePickup.bru"), 'w') as f:
        f.write(content)


def write_folder_meta(folder, seq):
    """Bruno folder.bru with auth inheritance."""
    content = textwrap.dedent(f"""\
        meta {{
          name: {os.path.basename(folder)}
          seq: {seq}
        }}

        auth {{
          mode: inherit
        }}
        """)
    with open(os.path.join(folder, "folder.bru"), 'w') as f:
        f.write(content)


def write_readme(folder, offer, frames, source, slice_meta):
    pickup_first, dropoff_last, total_orig = slice_meta
    pickup_approached = pickup_first is not None
    dropoff_approached = dropoff_last is not None
    expected_pickup = offer['app_verdict'] == 'ACCEPT' and pickup_approached
    yolo_total_min = (offer['pickup_minutes'] or 0) + (offer['trip_minutes'] or 0)
    if frames:
        actual_min = (frames[-1]['ts'] - frames[0]['ts']).total_seconds() / 60.0
    else:
        actual_min = 0.0
    rain_mult = (actual_min / yolo_total_min) if yolo_total_min else 0.0

    content = textwrap.dedent(f"""\
        # PUDO Replay — Offer {offer['id']}

        ## Original ride
        - **Created** (UTC): `{offer['created_at']}`
        - **Driver:** `{offer['driver_id']}` (replay uses test driver `{TEST_DRIVER_ID}`)
        - **Verdict:** `{offer['app_verdict']}`
        - **Fare:** ${offer['fare']}
        - **Pickup:** {offer['pickup_address']}  → ({offer['pickup_lat']}, {offer['pickup_lng']})
        - **Dropoff:** {offer['dropoff_address']}  → ({offer['dropoff_lat']}, {offer['dropoff_lng']})
        - **Pickup classification:** `{offer['pickup_classification'] or 'unclassified — TODO classify'}`
        - **Dropoff classification:** `{offer['dropoff_classification'] or 'unclassified — TODO classify'}`

        ## Replay metadata
        - **GPS source:** `{source}`
        - **Original frames in window:** {total_orig}
        - **Subsampled frames in replay:** {len(frames)}
        - **Pickup pin approached:** {pickup_approached}
        - **Dropoff pin approached:** {dropoff_approached}
        - **Yolo expected duration:** {yolo_total_min} min (pickup+trip)
        - **Actual replay span:** {actual_min:.1f} min
        - **Houston-tax multiplier:** {rain_mult:.2f}× (>1.0 = real ride was slower than Yolo predicted)

        ## Expected outcome
        - FirePickup expected: `{expected_pickup}`
        - This replay is the canonical regression test for this PUDO topology.

        ## Notes (hand-edit as needed)
        - <!-- Add ride-specific notes here: weather, detours, passenger behavior, anything that explains the GPS pattern -->
        """)
    with open(os.path.join(folder, "README.md"), 'w') as f:
        f.write(content)


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('offer_id', type=int)
    parser.add_argument('--output-dir', default='/tmp/bruno_replays')
    parser.add_argument('--folder-seq', type=int, default=100,
                        help="Bruno folder.bru seq number (sort order in GUI)")
    args = parser.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)

    conn = connect()
    cur = conn.cursor()

    offer = fetch_offer(cur, args.offer_id)
    print(f"  Offer {offer['id']}: {offer['app_verdict']} from {offer['driver_id']}")
    print(f"    Pickup:  {offer['pickup_address']}")
    print(f"    Dropoff: {offer['dropoff_address']}")

    window_start, window_end = bound_drive_window(offer)
    source = detect_gps_source(cur, offer['driver_id'], window_start, window_end)
    if source is None:
        sys.exit(
            f"FATAL: no GPS data for driver {offer['driver_id']} in window "
            f"[{window_start} → {window_end}]. Ride is unrecoverable."
        )
    print(f"  GPS source: {source}")

    frames = fetch_gps_frames(cur, offer['driver_id'], window_start, window_end, source)
    total_orig = len(frames)
    print(f"  Frames in window: {total_orig}")
    if total_orig == 0:
        sys.exit("FATAL: zero GPS frames retrieved.")

    sliced, pickup_first, dropoff_last = tighten_window_by_gps(frames, offer)
    print(f"  Frames after window-tightening: {len(sliced)} "
          f"(pickup_approached={pickup_first is not None}, "
          f"dropoff_approached={dropoff_last is not None})")

    sampled = subsample(sliced, offer)
    print(f"  Frames after subsampling: {len(sampled)}")

    sampled = derive_speeds(sampled)

    # Build the folder
    folder_name = safe_folder_name(offer)
    folder = os.path.join(args.output_dir, folder_name)
    os.makedirs(folder, exist_ok=True)
    print(f"  Writing folder: {folder}")

    write_folder_meta(folder, args.folder_seq)

    seq = 1
    write_reset(folder, "01 Reset (before)", seq, "before")
    seq = 2
    write_seed(folder, seq, offer)

    if not sampled:
        sys.exit("FATAL: no frames left after subsampling.")
    t0 = sampled[0]['ts']
    cumulative_miles = 0.0
    last_frame = None
    for frame in sampled:
        seq += 1
        if last_frame:
            cumulative_miles += haversine_miles(
                last_frame['lat'], last_frame['lng'], frame['lat'], frame['lng']
            )
        # Capture script on every HB (the dwell_zone_fired var only ever gets
        # set to true; non-pickup frames are no-ops)
        write_heartbeat(folder, seq, frame, t0, cumulative_miles, capture=True)
        last_frame = frame

    seq += 1
    write_verify_pickup(folder, seq)

    seq = 99
    write_reset(folder, "99 Reset (after)", seq, "after")

    write_readme(folder, offer, sampled, source, (pickup_first, dropoff_last, total_orig))

    print()
    print(f"  Done. To use this replay:")
    print(f"    1. scp -r {folder} <mac>:/path/to/Bruno/PuddleJumper\\ Backend/")
    print(f"    2. Reload the Bruno collection in the GUI")
    print(f"    3. bru run \"{folder_name}\" --env \"PuddleJumper Dev\" \\")
    print(f"       --env-var TEST_EMAIL=... --env-var TEST_PASSWORD=... --delay 5500")
    return 0


if __name__ == "__main__":
    sys.exit(main())