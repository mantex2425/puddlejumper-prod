#!/usr/bin/env python3
"""ingest_harris_pid.py — Harris County PID Traffic Signals → routing.stop_atlas."""
import csv
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg2

REPO_ROOT = Path(__file__).resolve().parents[2]
CSV_PATH = REPO_ROOT / "data" / "houston_stoplights.csv"
LOG_DIR = REPO_ROOT / "scripts" / "stop_atlas_ingestion" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

SOURCE_NAME = "harris_pid"
SOURCE_FILE_LABEL = "houston_stoplights.csv (Harris County PID, downloaded 2026-04-24)"
DATASET_HUB_MODIFIED = "2020-12-04"
WIDE_ROAD_CLASSES = {"motorway", "trunk", "primary", "motorway_link"}
DEFAULT_RADIUS_M = 30
WIDE_RADIUS_M = 45
MAX_CLASSIFY_DIST_M = 50.0

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "10.128.0.3"),
    "database": os.getenv("DB_NAME", "puddlejumper"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD"),
}

log_file = LOG_DIR / f"harris_pid_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(log_file), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("ingest_harris_pid")


def parse_csv(path):
    total = 0
    kept = 0
    skipped_status = 0
    skipped_no_coords = 0

    with path.open(newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        for row in reader:
            total += 1
            status = (row.get("Status") or "").strip()
            if status != "Active":
                skipped_status += 1
                continue

            lat_s = (row.get("Latitude") or "").strip()
            lng_s = (row.get("Longitude") or "").strip()
            if not lat_s or not lng_s:
                skipped_no_coords += 1
                continue

            try:
                lat = float(lat_s)
                lng = float(lng_s)
            except ValueError:
                skipped_no_coords += 1
                continue

            if not (28.5 <= lat <= 31.0 and -97.0 <= lng <= -94.0):
                skipped_no_coords += 1
                continue

            signal_id = (row.get("SignalID") or "").strip()
            if not signal_id:
                skipped_no_coords += 1
                continue

            kept += 1
            yield {
                "signal_id": signal_id,
                "lat": lat,
                "lng": lng,
                "location_name": (row.get("Locaction_Name") or "").strip() or None,
                "turn_on_date": (row.get("Turn_on_Date") or "").strip() or None,
                "precinct": (row.get("Precinct") or "").strip() or None,
            }

    log.info(
        "CSV parse: %d total | %d kept | %d skipped_status | %d skipped_no_coords",
        total, kept, skipped_status, skipped_no_coords,
    )


def classify_road(cur, lat, lng):
    """
    Return suppression_radius_m for a signal.

    Decision rule: if ANY of the 5 nearest ways within MAX_CLASSIFY_DIST_M is
    tagged with a wide-road class (motorway/trunk/primary/motorway_link), return
    the wide radius. Otherwise default.

    Rationale: a signal at "Hardy Tollway @ Rankin Rd" exists because of the
    freeway; classifying it by whichever road happens to be nearest by
    bounding-box tiebreaker is non-deterministic and wrong. A signal within
    arm's reach of a motorway needs the wider radius regardless of which
    frontage road or cross-street is technically closer.

    Uses true ST_Distance (not the <-> KNN operator) to ensure deterministic
    ordering on ties. The 50m ST_DWithin prefilter keeps the query cheap despite
    not using the <-> index.
    """
    cur.execute(
        """
        SELECT c.tag_value
        FROM routing.houston_ways AS ways
        JOIN routing.configuration AS c
          ON c.tag_id = ways.tag_id AND c.tag_key = 'highway'
        WHERE ST_DWithin(
                  ways.the_geom::geography,
                  app_private.coords_to_geography(%s, %s),
                  %s
              )
        ORDER BY ST_Distance(
                    ways.the_geom::geography,
                    app_private.coords_to_geography(%s, %s)
                 ) ASC
        LIMIT 5
        """,
        (lat, lng, MAX_CLASSIFY_DIST_M, lat, lng),
    )
    rows = cur.fetchall()
    if not rows:
        return DEFAULT_RADIUS_M

    nearby_classes = {(r[0] or "").strip() for r in rows}
    if nearby_classes & WIDE_ROAD_CLASSES:
        return WIDE_RADIUS_M
    return DEFAULT_RADIUS_M


UPSERT_SQL = """
INSERT INTO routing.stop_atlas
    (lat, lng, geog, stop_type, source, source_id,
     suppression_radius_m, source_extra, ingested_at)
VALUES
    (%(lat)s, %(lng)s,
     app_private.coords_to_geography(%(lat)s, %(lng)s),
     'traffic_signal',
     %(source)s,
     %(source_id)s,
     %(radius)s,
     %(source_extra)s::jsonb,
     (NOW() AT TIME ZONE 'UTC'))
ON CONFLICT (source, source_id) WHERE source_id IS NOT NULL
DO UPDATE SET
    lat                  = EXCLUDED.lat,
    lng                  = EXCLUDED.lng,
    geog                 = EXCLUDED.geog,
    suppression_radius_m = EXCLUDED.suppression_radius_m,
    source_extra         = EXCLUDED.source_extra,
    ingested_at          = EXCLUDED.ingested_at
"""


def main():
    if not CSV_PATH.exists():
        log.error("CSV file not found: %s", CSV_PATH)
        sys.exit(1)

    log.info("Starting Harris County PID ingestion from %s", CSV_PATH)
    log.info("Log file: %s", log_file)

    try:
        conn = psycopg2.connect(**DB_CONFIG)
    except psycopg2.Error as e:
        log.error("DB connection failed: %s", e)
        sys.exit(2)

    inserted_or_updated = 0
    classify_failures = 0
    radius_distribution = {30: 0, 45: 0}

    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM routing.stop_atlas WHERE source = %s",
                (SOURCE_NAME,),
            )
            existing_count = cur.fetchone()[0]
            log.info("Existing rows with source='%s': %d", SOURCE_NAME, existing_count)

            for sig in parse_csv(CSV_PATH):
                try:
                    radius = classify_road(cur, sig["lat"], sig["lng"])
                except psycopg2.Error as e:
                    log.warning(
                        "Road classify failed for %s: %s — using default",
                        sig["signal_id"], e,
                    )
                    conn.rollback()
                    radius = DEFAULT_RADIUS_M
                    classify_failures += 1

                radius_distribution[radius] = radius_distribution.get(radius, 0) + 1

                source_extra = {
                    "location_name": sig["location_name"],
                    "turn_on_date": sig["turn_on_date"],
                    "status_at_ingest": "Active",
                    "precinct": sig["precinct"],
                    "ingest_source_file": SOURCE_FILE_LABEL,
                    "dataset_hub_modified": DATASET_HUB_MODIFIED,
                }

                try:
                    cur.execute(UPSERT_SQL, {
                        "lat": sig["lat"],
                        "lng": sig["lng"],
                        "source": SOURCE_NAME,
                        "source_id": sig["signal_id"],
                        "radius": radius,
                        "source_extra": json.dumps(source_extra),
                    })
                    inserted_or_updated += 1
                except psycopg2.Error as e:
                    log.error("UPSERT failed for %s: %s", sig["signal_id"], e)
                    conn.rollback()
                    continue

                if inserted_or_updated % 100 == 0:
                    conn.commit()
                    log.info("Committed batch at %d rows", inserted_or_updated)

            conn.commit()

            cur.execute(
                "SELECT COUNT(*) FROM routing.stop_atlas WHERE source = %s",
                (SOURCE_NAME,),
            )
            final_count = cur.fetchone()[0]
    finally:
        conn.close()

    log.info("=" * 60)
    log.info("INGESTION COMPLETE")
    log.info("=" * 60)
    log.info("Upsert operations:     %d", inserted_or_updated)
    log.info("Classification errors: %d (defaulted to %dm)",
             classify_failures, DEFAULT_RADIUS_M)
    log.info("Radius distribution:")
    for r, n in sorted(radius_distribution.items()):
        log.info("  %dm: %d signals", r, n)
    log.info("Source='%s' row count: %d (was %d, delta %+d)",
             SOURCE_NAME, final_count, existing_count,
             final_count - existing_count)


if __name__ == "__main__":
    main()
