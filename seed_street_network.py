#!/usr/bin/env python3
"""
PuddleJumper Street & POI Network Pre-Seeder
Enhanced to handle Airports, Stadiums, and Malls in addition to Streets.
Resumable - skips names already in the table.
"""

import os
import time
import logging
import requests
import psycopg2

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
logger = logging.getLogger(__name__)

DB_CONFIG = {
    'host': '10.128.0.2',
    'dbname': 'puddlejumper',
    'user': 'postgres',
    'port': 5432,
}

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
HOUSTON_BBOX = (29.20, -95.90, 30.20, -94.80)
DELAY_BETWEEN = 2
RETRY_DELAY = 60

def get_conn():
    return psycopg2.connect(**DB_CONFIG)

def get_cached_names(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT street_name FROM app_private.street_network")
        return {row[0] for row in cur.fetchall()}

def fetch_osm_data(name, bbox):
    s, w, n, e = bbox
    name_safe = name.replace('"', '\\"')
    query = f"""[out:json][timeout:50];
(
  nwr["name"="{name_safe}"]({s},{w},{n},{e});
  nwr["official_name"="{name_safe}"]({s},{w},{n},{e});
  nwr["aeroway"~"terminal|gate"]["name"~"{name_safe}"]({s},{w},{n},{e});
);
out geom;"""
    for attempt in range(3):
        try:
            resp = requests.post(OVERPASS_URL, data=query, timeout=55)
            if resp.status_code == 200:
                return resp.json().get('elements', [])
            elif resp.status_code == 429:
                logger.warning(f"Rate limited on '{name}', waiting {RETRY_DELAY}s...")
                time.sleep(RETRY_DELAY)
            else:
                logger.error(f"HTTP {resp.status_code} for '{name}'")
                time.sleep(5)
        except Exception as e:
            logger.error(f"Exception for '{name}': {e}")
            time.sleep(10)
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

def insert_elements(conn, search_name, elements):
    inserted = 0
    with conn.cursor() as cur:
        for el in elements:
            osm_id = el.get('id')
            tags = el.get('tags', {})
            highway = tags.get('highway')
            aeroway = tags.get('aeroway')
            amenity = tags.get('amenity')
            if highway in ('footway', 'cycleway', 'path', 'steps', 'track'):
                continue
            wkt = element_to_wkt(el)
            if not wkt:
                continue
            alt_names = []
            for t in ['alt_name', 'official_name', 'short_name', 'ref', 'iata']:
                val = tags.get(t)
                if val and val != search_name:
                    alt_names.append(val)
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
                        'Houston'
                    )
                    ON CONFLICT (osm_id) DO UPDATE SET
                        street_name = EXCLUDED.street_name,
                        alt_names = EXCLUDED.alt_names,
                        updated_at = now();
                """, (osm_id, search_name, alt_names or None,
                      highway or aeroway or amenity,
                      wkt, wkt, wkt))
                inserted += 1
            except Exception as e:
                logger.error(f"DB Error on {search_name}: {e}")
                conn.rollback()
                return inserted
    conn.commit()
    return inserted

def main():
    source_file = '/home/andrew/houston_streets_full.txt'
    with open(source_file) as f:
        names = [l.strip().strip('"') for l in f if l.strip()]

    conn = get_conn()
    cached = get_cached_names(conn)
    todo = [n for n in names if n not in cached]
    logger.info(f"Total: {len(names)} | Cached: {len(cached)} | Remaining: {len(todo)}")

    for i, name in enumerate(todo):
        elements = fetch_osm_data(name, HOUSTON_BBOX)
        if elements:
            count = insert_elements(conn, name, elements)
            logger.info(f"[{i+1}/{len(todo)}] '{name}': {count} elements")
        else:
            logger.info(f"[{i+1}/{len(todo)}] '{name}': not found")
        time.sleep(DELAY_BETWEEN)
        if (i+1) % 100 == 0:
            logger.info(f"=== {i+1}/{len(todo)} done ===")

    conn.close()
    logger.info("Seeding complete.")

if __name__ == '__main__':
    main()
