"""POI cache lookup service — Operation Strip Mall Phase 2a.

Cache-only read path against ``app_private.poi_cache``. Phase 2b will add
the Google Places "Nearby Search" call on cache miss; Phase 2a returns
an empty list on miss.

Source of truth:
    OPERATION_STRIP_MALL_PROPOSAL.md REVISIONS R1 (radius-based GiST cache),
    R2 (distance-weighted Option C dedupe), R3 (80m radius locked).

Canonical Standards v2.0:
    §I  Recon-first — Cluster.median_lat/median_lng confirmed in
        cluster_detection.py:42-67. RealDictCursor pattern mirrored from
        driver_heartbeat.py.
    §II Coordinate functions — query passes (lat, lng) to
        app_private.coords_to_geography. No raw ST_MakePoint.
    §III UTC mandatory — TTL filter and last_hit_at write use
        (NOW() AT TIME ZONE 'UTC').
    §V  Binding integrity — last_hit_at column is updated on every cache
        hit (throttled to once-per-hour-per-row to avoid write storms).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Imported only for type hints to avoid circular imports at runtime.
    # cluster_detection.py imports nothing from poi_service.
    from cluster_detection import Cluster
    from psycopg2.extensions import cursor as _Cursor


# ─── DATA SHAPE ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class POI:
    """An immutable POI record returned to the matcher.

    Mirrors the shape Phase 2c's ``_signal_poi_match`` will consume:
    business_name is the primary fuzzy-match anchor (R4: 0.8 weight),
    place_id is the forensic anchor logged into pudo_decision_context.
    poi_top_names (Phase 1B column).

    Field semantics:
      place_id       -- Google Places place_id, stable across queries
      name           -- business name (Pepperoni's, Shell, Planet Fitness)
      business_type  -- primary Google Places type (restaurant, gas_station)
      dist_m         -- meters from cluster centroid to the cache row's
                        query point. Used by Option C dedupe to keep the
                        nearest occurrence of each unique business name.
    """

    place_id: str
    name: str
    business_type: str
    dist_m: float


# ─── CONSTANTS ───────────────────────────────────────────────────────────────


# 80m per OPERATION_STRIP_MALL_PROPOSAL R3. 50m too tight (border-cliff
# within a single mall); 100m pulls in adjacent businesses on neighboring
# parcels.
DEFAULT_RADIUS_M: int = 80


# Throttle for last_hit_at write-back. A heartbeat firing every ~5s would
# otherwise produce a write storm on hot strip-mall cache rows. 1 hour is
# coarse enough to suppress storms while still surfacing daily/weekly
# usage signal for analytics and future LRU eviction.
TOUCH_THROTTLE_INTERVAL: str = "1 hour"


# ─── CACHE LOOKUP ────────────────────────────────────────────────────────────


def get_pois_near_cluster(
    cluster: "Cluster",
    cur: "_Cursor",
    *,
    radius_m: int = DEFAULT_RADIUS_M,
) -> list[POI]:
    """Return cached POIs within ``radius_m`` of the cluster centroid.

    Cache-only in Phase 2a. Returns an empty list if no cache rows match
    (Phase 2b will add the Google Places call on miss). Returns an empty
    list if every matching row is a "negative cache" entry (cardinality
    zero across the parallel arrays — meaning Google was queried at that
    point and returned zero results).

    Side effect: matched rows have their ``last_hit_at`` updated to
    ``NOW() AT TIME ZONE 'UTC'``, throttled to at most once per hour per
    row. This satisfies Standard §V (binding integrity for the analytics
    column) without producing a write storm in the heartbeat loop.

    The returned list is Option C deduped: for each unique POI place_id
    appearing across all in-range cache rows, only the nearest occurrence
    survives. The result is sorted ascending by distance, so the matcher's
    fuzzy-match scan starts with the closest candidate.

    Args:
        cluster: a frozen Cluster from cluster_detection.detect_cluster.
        cur: a psycopg2 cursor with RealDictCursor factory. The function
             relies on dict-style row access (``row["id"]``); a vanilla
             tuple cursor will raise TypeError.
        radius_m: ST_DWithin radius in meters. Defaults to R3's locked 80m.

    Returns:
        list[POI] sorted by ascending distance. Empty on cache miss or
        when only negative-cache rows are in range.
    """
    cur.execute(
        """
        SELECT
            id,
            google_place_ids,
            business_names,
            business_types,
            ST_Distance(
                query_geog,
                app_private.coords_to_geography(%s, %s)
            ) AS dist_m
        FROM app_private.poi_cache
        WHERE expires_at > (NOW() AT TIME ZONE 'UTC')
          AND ST_DWithin(
              query_geog,
              app_private.coords_to_geography(%s, %s),
              %s
          )
        ORDER BY dist_m ASC
        """,
        (
            cluster.median_lat, cluster.median_lng,  # ST_Distance probe point
            cluster.median_lat, cluster.median_lng,  # ST_DWithin probe point
            radius_m,
        ),
    )

    rows = cur.fetchall()
    if not rows:
        return []

    # Side effect: touch matched rows' last_hit_at to record cache utility,
    # throttled to once-per-hour-per-row to avoid heartbeat write storms.
    # The WHERE clause guards against redundant writes within the throttle
    # window. Standard §V binding integrity for the analytics column.
    cur.execute(
        f"""
        UPDATE app_private.poi_cache
        SET last_hit_at = (NOW() AT TIME ZONE 'UTC')
        WHERE id = ANY(%s)
          AND (
              last_hit_at IS NULL
              OR last_hit_at < (NOW() AT TIME ZONE 'UTC')
                               - INTERVAL '{TOUCH_THROTTLE_INTERVAL}'
          )
        """,
        ([row["id"] for row in rows],),
    )

    # Option C dedupe: for each unique place_id across ALL rows in range,
    # keep the nearest occurrence. A strip mall hit by 5 different drivers
    # might produce 5 cache rows; this collapses them to one entry per
    # distinct business by closest distance.
    #
    # Keyed on place_id rather than name so two genuinely-different
    # businesses with the same name (e.g. two Shell stations on opposite
    # sides of a freeway, both within 80m of a cluster centroid at the
    # overpass) don't get collapsed. Name collisions across place_ids are
    # legitimately distinct entities.
    best: dict[str, tuple[float, str, str]] = {}
    for row in rows:
        place_ids = row["google_place_ids"]
        names = row["business_names"]
        types = row["business_types"]
        dist_m = float(row["dist_m"])

        # Schema CHECK constraint poi_cache_arrays_aligned guarantees the
        # three arrays have equal cardinality. Negative-cache rows (all
        # arrays empty) skip the loop body cleanly.
        for place_id, name, btype in zip(place_ids, names, types):
            existing = best.get(place_id)
            if existing is None or dist_m < existing[0]:
                best[place_id] = (dist_m, name, btype)

    # Sort by distance so the matcher's fuzzy scan starts with the closest
    # candidate. Stable sort preserves insertion order on ties, which
    # preserves the SQL's ORDER BY dist_m ASC ordering for equal distances.
    return sorted(
        (
            POI(place_id=pid, name=n, business_type=t, dist_m=d)
            for pid, (d, n, t) in best.items()
        ),
        key=lambda poi: poi.dist_m,
    )