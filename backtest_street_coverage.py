"""
backtest_street_coverage.py — Compare street name coverage between
app_private.street_network and routing.houston_ways

Tests how many real pickup addresses from offer_history can be 
matched in each street geometry source.

Usage:
  python3 backtest_street_coverage.py
"""

import psycopg2
import psycopg2.extras
import re
import sys

DB_CONFIG = {
    "host":   "10.128.0.2",
    "user":   "postgres",
    "dbname": "puddlejumper",
}

DRIVER_ID = "UjT1hE9eBXh2q95aSZYOkzDJ8lo1"

# Same abbreviation expander as clean_street_name() in triangulation.py
ABBREVS = {
    r'\bFwy\b': 'Freeway', r'\bPkwy\b': 'Parkway', r'\bBlvd\b': 'Boulevard',
    r'\bAve\b': 'Avenue',  r'\bDr\b':   'Drive',   r'\bSt\b':   'Street',
    r'\bRd\b':  'Road',    r'\bLn\b':   'Lane',    r'\bCt\b':   'Court',
    r'\bPl\b':  'Place',   r'\bCir\b':  'Circle',  r'\bTrl\b':  'Trail',
    r'\bTrce\b':'Trace',   r'\bXing\b': 'Crossing',r'\bHwy\b':  'Highway',
    r'\bExpy\b':'Expressway', r'\bFtg\b': 'Frontage',
}

def clean_street_name(address: str) -> list:
    """Extract and expand street names from address — same as triangulation.py"""
    if not address:
        return []
    
    # Split on & for intersections
    parts = [p.strip() for p in address.split('&')]
    results = []
    
    for part in parts:
        # Remove city/state suffix
        part = re.sub(r',.*$', '', part).strip()
        # Expand abbreviations
        for abbrev, full in ABBREVS.items():
            part = re.sub(abbrev, full, part, flags=re.IGNORECASE)
        # Extract just the street name (remove building numbers)
        part = re.sub(r'^\d+\s+', '', part).strip()
        if part:
            results.append(part)
    
    return results


def run():
    conn = psycopg2.connect(**DB_CONFIG,
                            cursor_factory=psycopg2.extras.RealDictCursor)
    cur = conn.cursor()

    # Fetch real pickup addresses from offer_history
    cur.execute("""
        SELECT DISTINCT pickup_address
        FROM app_private.offer_history oh
        JOIN app_private.decision_log dl ON dl.id = oh.decision_log_id
        WHERE dl.driver_id = %s
          AND oh.pickup_address IS NOT NULL
          AND oh.pickup_address != ''
        ORDER BY pickup_address
        LIMIT 100
    """, (DRIVER_ID,))
    addresses = [row['pickup_address'] for row in cur.fetchall()]

    print(f"\n{'='*70}")
    print(f"  🐸 Street Coverage Backtest")
    print(f"  Testing {len(addresses)} real pickup addresses")
    print(f"{'='*70}\n")

    street_network_hits = 0
    houston_ways_hits   = 0
    both_hits           = 0
    neither_hits        = 0
    houston_only        = 0

    misses = []

    for address in addresses:
        street_names = clean_street_name(address)
        if not street_names:
            continue

        sn_found = False
        hw_found = False

        for name in street_names:
            # Check app_private.street_network
            cur.execute("""
                SELECT COUNT(*) AS cnt
                FROM app_private.street_network
                WHERE street_name ILIKE %s
            """, (f'%{name}%',))
            if cur.fetchone()['cnt'] > 0:
                sn_found = True

            # Check routing.houston_ways
            cur.execute("""
                SELECT COUNT(*) AS cnt
                FROM routing.houston_ways
                WHERE name ILIKE %s
            """, (f'%{name}%',))
            if cur.fetchone()['cnt'] > 0:
                hw_found = True

        if sn_found:
            street_network_hits += 1
        if hw_found:
            houston_ways_hits += 1
        if sn_found and hw_found:
            both_hits += 1
        if hw_found and not sn_found:
            houston_only += 1
        if not sn_found and not hw_found:
            neither_hits += 1
            misses.append(address)

    total = len(addresses)
    print(f"  street_network hits:    {street_network_hits}/{total} ({100*street_network_hits/total:.1f}%)")
    print(f"  houston_ways hits:      {houston_ways_hits}/{total} ({100*houston_ways_hits/total:.1f}%)")
    print(f"  Both match:             {both_hits}/{total} ({100*both_hits/total:.1f}%)")
    print(f"  houston_ways only:      {houston_only}/{total} — addresses street_network misses")
    print(f"  Neither match:          {neither_hits}/{total} ({100*neither_hits/total:.1f}%)")

    if misses:
        print(f"\n── Addresses neither source matched:")
        for addr in misses[:20]:
            print(f"   {addr}")
        if len(misses) > 20:
            print(f"   ... and {len(misses)-20} more")

    print(f"\n{'='*70}")
    print(f"  Improvement if using houston_ways:")
    improvement = houston_ways_hits - street_network_hits
    print(f"  +{improvement} additional addresses matched ({100*improvement/total:.1f}% gain)")
    print(f"{'='*70}\n")

    conn.close()

if __name__ == "__main__":
    run()
