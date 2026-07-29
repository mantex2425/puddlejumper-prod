#!/usr/bin/env python3
"""
PuddleJumper Street Network Turbo Seeder
Spatial tile approach - queries entire grid squares instead of individual streets.
~4 hours instead of ~13 days.
"""
import time
import requests
import psycopg2
import logging
import argparse

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
logger = logging.getLogger(__name__)

def parse_args():
    parser = argparse.ArgumentParser(description='PuddleJumper Street Network Seeder')
    parser.add_argument('--city', type=str, default='Houston', help='City name (stored in street_network.city)')
    parser.add_argument('--bbox', type=str, default='29.40,-95.90,30.20,-94.80',
                        help='Bounding box: south,west,north,east (e.g. 27.60,-97.55,27.90,-97.10)')
    return parser.parse_args()

ARGS = parse_args()
_bbox = [float(x) for x in ARGS.bbox.split(',')]
CITY = ARGS.city

DB_CONFIG = {
    'host': '10.128.0.3',
    'dbname': 'puddlejumper',
    'user': 'postgres',
    'port': 5432,
}

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
LAT_START, LAT_END = _bbox[0], _bbox[2]
LON_START, LON_END = _bbox[1], _bbox[3]
STEP = 0.05
DELAY = 20

def get_conn():
    return psycopg2.connect(**DB_CONFIG)

def fetch_tile(s, w, n, e):
    query = f"""[out:json][timeout:180];
(
  way["highway"~"motorway|trunk|primary|secondary|tertiary|residential|unclassified"]({s},{w},{n},{e});
  nwr["amenity"~"hospital|stadium|university|hotel|mall|cinema|theatre"]({s},{w},{n},{e});
  nwr["aeroway"~"terminal|gate|aerodrome"]({s},{w},{n},{e});
  nwr["leisure"~"stadium|arena|sports_centre"]({s},{w},{n},{e});
  nwr["tourism"~"hotel|attraction|museum"]({s},{w},{n},{e});
);
out geom;"""
    for attempt in range(3):
        try:
            resp = requests.post(OVERPASS_URL, data=query, timeout=190)
            if resp.status_code == 200:
                return resp.json().get('elements', [])
            elif resp.status_code == 429:
                logger.warning("Rate limited, waiting 90s...")
                time.sleep(90)
            elif resp.status_code == 504:
                logger.warning("504, waiting 30s...")
                time.sleep(30)
            else:
                logger.error(f"HTTP {resp.status_code}")
                return []
        except Exception as e:
            logger.error(f"Tile error: {e}")
            time.sleep(15)
    return []

def element_to_wkt(el):
    etype = el.get('type')
    if etype == 'node':
        return f"POINT({el['lon']} {el['lat']})"
    geom = el.get('geometry', [])
    if not geom or len(geom) < 2:
        return None
    coords = ', '.join(f"{pt['lon']} {pt['lat']}" for pt in geom)
    if etype == 'way' and geom[0] == geom[-1] and len(geom) > 3:
        return f"POLYGON(({coords}))"
    return f"LINESTRING({coords})"

def insert_elements(conn, elements):
    inserted = 0
    skipped = 0
    with conn.cursor() as cur:
        for el in elements:
            osm_id = el.get('id')
            tags = el.get('tags', {})
            name = tags.get('name') or tags.get('official_name')
            if not name:
                skipped += 1
                continue
            highway = tags.get('highway')
            aeroway = tags.get('aeroway')
            amenity = tags.get('amenity')
            leisure = tags.get('leisure')
            tourism = tags.get('tourism')
            if highway in ('footway', 'cycleway', 'path', 'steps', 'track', 'service'):
                skipped += 1
                continue
            wkt = element_to_wkt(el)
            if not wkt:
                skipped += 1
                continue
            alt_names = []
            for t in ['alt_name', 'official_name', 'short_name', 'ref', 'iata']:
                val = tags.get(t)
                if val and val != name:
                    for v in val.split(';'):
                        v = v.strip()
                        if v:
                            alt_names.append(v)
            highway_type = highway or aeroway or amenity or leisure or tourism
            try:
                cur.execute("""
                    INSERT INTO app_private.street_network
                        (osm_id, street_name, alt_names, highway_type, geometry, city)
                    VALUES (
                        %s, %s, %s, %s,
                        CASE
                            WHEN %s LIKE 'POLYGON%%' THEN ST_Centroid(ST_GeomFromText(%s, 4326))
                            ELSE ST_GeomFromText(%s, 4326)
                        END,
                        %s
                    )
                    ON CONFLICT (osm_id) DO UPDATE SET
                        street_name = EXCLUDED.street_name,
                        alt_names = EXCLUDED.alt_names,
                        updated_at = now()
                """, (osm_id, name, alt_names or None, highway_type,
                      wkt, wkt, wkt, CITY))
                inserted += 1
            except Exception as e:
                logger.error(f"Insert error {osm_id}: {e}")
                conn.rollback()
    conn.commit()
    return inserted, skipped

def get_completed_tiles(conn):
    """Track which tiles are done using a simple log table."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS app_private.street_network_tiles (
                tile_key text PRIMARY KEY,
                completed_at timestamptz DEFAULT now()
            )
        """)
        conn.commit()
        cur.execute("SELECT tile_key FROM app_private.street_network_tiles")
        return {row[0] for row in cur.fetchall()}

def mark_tile_done(conn, tile_key):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO app_private.street_network_tiles (tile_key)
            VALUES (%s) ON CONFLICT DO NOTHING
        """, (tile_key,))
    conn.commit()

def main():
    conn = get_conn()
    completed = get_completed_tiles(conn)

    # Calculate total tiles
    lats = []
    lat = LAT_START
    while lat < LAT_END:
        lats.append(round(lat, 3))
        lat += STEP
    lons = []
    lon = LON_START
    while lon < LON_END:
        lons.append(round(lon, 3))
        lon += STEP

    total_tiles = len(lats) * len(lons)
    done_tiles = len(completed)
    logger.info(f"Total tiles: {total_tiles} | Already done: {done_tiles} | Remaining: {total_tiles - done_tiles}")

    tile_num = 0
    total_inserted = 0

    for lat in lats:
        for lon in lons:
            tile_key = f"{lat},{lon}"
            if tile_key in completed:
                tile_num += 1
                continue

            s, w, n, e = lat, lon, round(lat + STEP, 3), round(lon + STEP, 3)
            tile_num += 1

            elements = fetch_tile(s, w, n, e)
            if elements:
                inserted, skipped = insert_elements(conn, elements)
                total_inserted += inserted
                logger.info(f"[{tile_num}/{total_tiles}] Tile ({s},{w}): {len(elements)} elements, {inserted} inserted, {skipped} skipped (total: {total_inserted})")
            else:
                logger.info(f"[{tile_num}/{total_tiles}] Tile ({s},{w}): empty")

            mark_tile_done(conn, tile_key)
            time.sleep(DELAY)

    conn.close()
    logger.info(f"Done! {total_inserted} total rows in street_network")

if __name__ == '__main__':
    main()
