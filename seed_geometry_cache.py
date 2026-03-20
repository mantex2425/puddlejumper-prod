#!/usr/bin/env python3
"""
Pre-seed street_geometry_cache for known POIs that won't match by street name.
Run once: python3 seed_geometry_cache.py
"""
import json
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db import get_db
from arc_band import _get_region_h3, _store_in_cache

import argparse

def parse_args():
    parser = argparse.ArgumentParser(description='PuddleJumper POI Cache Seeder')
    parser.add_argument('--city', type=str, default='Houston', help='City to seed POI cache for')
    return parser.parse_args()

CITY_POIS = {
    'Houston': [
        {
            "lat": 29.9902, "lng": -95.3414,
            "variants": [
                "terminal a, george bush intercontinental airport (iah), houston, tx, us",
                "terminal b, george bush intercontinental airport (iah), houston, tx, us",
                "terminal c, george bush intercontinental airport (iah), houston, tx, us",
                "terminal d, george bush intercontinental airport (iah), houston, tx, us",
                "terminal e, george bush intercontinental airport (iah), houston, tx, us",
                "george bush intercontinental airport (iah), houston, texas",
                "united, houston, texas",
            ]
        },
        {
            "lat": 29.6454, "lng": -95.2789,
            "variants": [
                "southwest airlines, houston, texas",
                "william p. hobby airport, houston, texas",
                "hobby airport, houston, texas",
            ]
        },
        {
            "lat": 29.6850, "lng": -95.4104,
            "variants": [
                "nrg park, houston, texas",
                "nrg stadium, houston, texas",
            ]
        },
    ],
    'Corpus Christi': [
        {
            "lat": 27.7704, "lng": -97.5011,
            "variants": [
                "corpus christi international airport, corpus christi, texas",
                "crp airport, corpus christi, texas",
                "corpus christi international airport (crp), corpus christi, tx, us",
            ]
        },
        {
            "lat": 27.8006, "lng": -97.3964,
            "variants": [
                "american bank center, corpus christi, texas",
                "whataburger field, corpus christi, texas",
            ]
        },
    ],
}

def get_proximity_segments(cur, lat, lng):
    cur.execute("""
        SELECT ST_AsGeoJSON(geometry)
        FROM app_private.street_network
        WHERE ST_DWithin(geometry,
                         ST_SetSRID(ST_MakePoint(%s, %s), 4326),
                         0.018)
        AND ST_GeometryType(geometry) = 'ST_LineString'
        AND highway_type IN ('motorway', 'trunk', 'primary', 'secondary',
                             'tertiary', 'residential', 'unclassified')
        ORDER BY geometry <-> ST_SetSRID(ST_MakePoint(%s, %s), 4326)
        LIMIT 50
    """, (lng, lat, lng, lat))
    rows = cur.fetchall()
    if not rows:
        return None
    return [
        [[coord[1], coord[0]] for coord in json.loads(row[0] if isinstance(row, tuple) else row['st_asgeojson'])['coordinates']]
        for row in rows
    ]

def main():
    args = parse_args()
    city = args.city
    pois = CITY_POIS.get(city)
    if not pois:
        print(f"No POIs configured for '{city}'. Add entries to CITY_POIS in this script.")
        print(f"Available cities: {list(CITY_POIS.keys())}")
        return

    print(f"Seeding POI cache for: {city}")
    conn = get_db()
    cur = conn.cursor()

    for poi in pois:
        lat, lng = poi['lat'], poi['lng']
        print(f"\n📍 Processing POI at ({lat}, {lng})")

        region_h3 = _get_region_h3(lat, lng, cur)
        print(f"   region_h3: {region_h3}")

        segments = get_proximity_segments(cur, lat, lng)
        if not segments:
            print(f"   ⚠️ No segments found — skipping")
            continue
        print(f"   Found {len(segments)} segments")

        for variant in poi['variants']:
            _store_in_cache(variant, region_h3, segments, [], cur, conn)
            print(f"   ✅ Cached: {variant}")

    cur.close()
    conn.close()
    print("\n✅ Cache seeding complete")

if __name__ == '__main__':
    main()
