"""
adjacency.py — DIAGNOSE primitive for parking-lot / off-wire road awareness.

Top-level peer of cluster_detection.py, pivot_context.py, and where_am_i.py.

Purpose
=======
When a driver stops in a parking lot, strip-mall back-entrance, apartment
complex, or hospital footprint, the GPS-snapped current_road is often a
private driveway, an internal lot road, or null. The standard
on_target_road signal returns 0 in these cases even when the cluster is
physically adjacent to the offer's named road.

This primitive answers the complementary question: "what NAMED roads
border the cluster?" Output is a tuple of road names that touch a 150m
buffer around the cluster centroid. Matchers consume this via the
adjacent_road_match signal (see _signal_adjacent_road_match in
where_am_i.py) when their target's named_roads don't appear in
current_road but DO appear in this whitelist.

Architectural placement
=======================
DIAGNOSE box per CANONICAL § VIII. Pure read against routing.houston_ways.
No state writes, no business logic. Returns data; matchers reason about
it. Caller (where_am_i._compute_road_topology) injects the cursor.

Canonical-rules compliance
==========================
- Uses app_private.coords_to_geography(lat, lng) per § I (NEVER raw
  ST_MakePoint).
- Argument order is (lat, lng) per § I ordering rule.
- Query plan: hits idx_houston_ways_geog_gist (partial GIST on
  the_geom::geography WHERE the_geom IS NOT NULL). At 150m buffer in
  Houston this returns ~3-12 rows in single-digit milliseconds.

Sprint 1 design lock (2026-04-29)
=================================
- Buffer: 150m (B.5 ratification — Houston Standard, wide enough for
  frontage roads, tight enough to exclude parallel residential).
- Returns tuple[str, ...] of distinct, non-null road names.
- Empty tuple when cluster is None (caller convention).
- DEDUPLICATED: the same name appearing on multiple segments collapses
  to one entry. Matchers do exact-string equality, so duplication is
  noise.
- ORDERING: alphabetical for determinism (test stability and forensic
  log readability). Not used for prioritization.
"""

from __future__ import annotations

from typing import Optional

from cluster_detection import Cluster


# Buffer distance for road adjacency lookup. 150m per B.5 ratification.
# Smaller misses frontage-road cases (Planet Fitness back entrance).
# Larger pulls in too many parallel roads in dense Houston grids.
ADJACENCY_BUFFER_M = 150


_ADJACENCY_SQL = """
SELECT DISTINCT name
FROM routing.houston_ways
WHERE name IS NOT NULL
  AND ST_DWithin(
        the_geom::geography,
        app_private.coords_to_geography(%s, %s),
        %s
      )
ORDER BY name;
"""


def get_adjacent_roads(
    cur,
    lat: float,
    lng: float,
    buffer_m: int = ADJACENCY_BUFFER_M,
) -> tuple[str, ...]:
    """Return the alphabetized tuple of distinct named roads within
    `buffer_m` meters of (lat, lng).

    Args:
      cur: psycopg cursor (caller-injected, request-scoped per
           where_am_i.WhereAmI lifecycle).
      lat: latitude of the query point. Typically cluster.median_lat.
      lng: longitude of the query point. Typically cluster.median_lng.
      buffer_m: search radius in meters. Default ADJACENCY_BUFFER_M (150).

    Returns:
      Tuple of unique road names within the buffer, alphabetized. Empty
      tuple if no roads match or if lat/lng are None.

    Performance:
      Hits idx_houston_ways_geog_gist (partial GIST on the_geom::geography
      WHERE the_geom IS NOT NULL). Single-digit milliseconds at 150m in
      Houston.

    Canonical-rules compliance: uses app_private.coords_to_geography(lat, lng)
    per § I; never raw ST_MakePoint.
    """
    if cur is None or lat is None or lng is None:
        return ()

    cur.execute(_ADJACENCY_SQL, (float(lat), float(lng), int(buffer_m)))
    rows = cur.fetchall()

    # Cursor may be RealDictCursor (returns dict-like rows) or default
    # tuple-cursor. Handle both robustly so tests don't have to mock a
    # specific cursor flavor.
    out: list[str] = []
    for row in rows:
        if isinstance(row, dict):
            name = row.get("name")
        else:
            name = row[0]
        if name:
            out.append(name)
    return tuple(out)


def get_adjacent_roads_for_cluster(
    cur,
    cluster: Optional[Cluster],
    buffer_m: int = ADJACENCY_BUFFER_M,
) -> tuple[str, ...]:
    """Convenience wrapper over get_adjacent_roads that takes a Cluster.

    Returns empty tuple when cluster is None — caller convention so
    _compute_road_topology never has to None-check the result.
    """
    if cluster is None:
        return ()
    return get_adjacent_roads(cur, cluster.median_lat, cluster.median_lng, buffer_m)
