"""
cluster_detection.py — shared geospatial primitive

Detects "stopped clusters" in a driver's recent heartbeat trail. A cluster is
a tight, low-speed run of consecutive heartbeats where the driver has been
stationary or near-stationary.

This is a pure DIAGNOSE primitive (per PuddleJumper's 4-box controller):
  - Reads from app_private.heartbeat_log
  - No writes, no state mutation
  - Returns a Cluster snapshot, or None if the driver isn't currently stopped

Consumers:
  - bead_on_wire.compute_target() -- BMOAR Path A (blind man) and Path B (proximity)
  - where_am_i.WhereAmI() -- WAI continuous awareness primitive (Phase D)

Both consumers MUST go through this module. Inline cluster math is forbidden;
having two implementations means two definitions of "where the driver is",
which is precisely the architectural smell where_am_i() exists to eliminate.

RELOCATION HISTORY (Phase C, 2026-04-25):
This function was relocated verbatim from bead_on_wire.py PRIMITIVE 6.
Changes from the original:
  1. Return type: dict -> Cluster (frozen dataclass).
  2. Log prefix: [BEAD] -> [CLUSTER] (since the function no longer lives
     in BMOAR specifically).
The two SQL queries inside detect_cluster() are BYTE-IDENTICAL to the
original. Bit-identical relocation guarantee verified by the equivalence
tests in tests/test_cluster_detection.py.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional
import logging


# ============================================================================
# Cluster -- immutable snapshot returned by detect_cluster()
# ============================================================================

@dataclass(frozen=True)
class Cluster:
    """An immutable snapshot of a stopped-driver cluster.

    Frozen because clusters are diagnostic snapshots of physical reality at a
    specific moment. Nothing downstream should be mutating them; if a consumer
    wants to derive a different value, it should construct a new object.

    Field semantics:
      n             -- number of consecutive low-speed heartbeats in the cluster
      median_lat    -- cluster centroid latitude (PERCENTILE_CONT(0.5))
      median_lng    -- cluster centroid longitude
      spread_m      -- max distance (meters) from any cluster point to centroid
      duration_s  -- span (seconds) from earliest to latest heartbeat in the
                       cluster. NB: this is the WIDTH of the cluster, not how
                       long the driver has been stopped overall (because the
                       cluster only counts heartbeats in the most recent
                       uninterrupted low-speed run).
      latest        -- MAX(logged_at) of the cluster's heartbeats. Exposes
                       data already aggregated by the SQL. Used by
                       get_recent_clusters() consumers to sort and to anchor
                       Amendment 1's cluster_revisit topology check.
    """
    n: int
    median_lat: float
    median_lng: float
    spread_m: float
    duration_s: float
    latest: datetime


# ============================================================================
# is_stable -- policy predicate, kept OUT of the Cluster dataclass
# ============================================================================

def is_stable(cluster: Cluster, threshold_s: float) -> bool:
    """True if the cluster has persisted at least threshold_s seconds.

    Kept as a free function (not a Cluster method) because the threshold is a
    POLICY decision, not a property of the cluster itself. Different consumers
    apply different thresholds:
      - WAI PUDO planner v1: 15s for residential pickups
      - Future: longer for apartment complex pivots, shorter for "definitely
        parked" verification, etc.

    The v1 default lives in routing.known_stops_config.min_cluster_duration_s
    and should be loaded at the consumer level, not bound to Cluster.
    """
    return cluster.duration_s >= threshold_s


# ============================================================================
# detect_cluster -- the primitive
# ============================================================================

def detect_cluster(driver_id: str, cur,
                   window_sec: int = 60,
                   min_samples: int = 3,
                   max_speed_mph: float = 10.0,
                   max_spread_m: float = 25.0) -> Optional[Cluster]:
    """Check if driver has a tight low-speed cluster in the recent past.

    Cluster criteria (all must hold):
      - >= min_samples heartbeats in the last window_sec seconds
      - all at speed < max_speed_mph
      - all within max_spread_m of the cluster median
      - all in the most recent uninterrupted low-speed run

    The "most recent uninterrupted run" rule (the breaks_before = 0 SQL
    trick) is the critical anti-conflation guard: it prevents a red-light
    stop 45s ago from being mixed into a curb stop happening now. Without
    this rule, a driver who paused at a light then continued to the curb
    would yield a cluster median halfway between the two events.

    Returns: Cluster snapshot, or None if no qualifying cluster exists.

    None means driver isn't in a stopped cluster -- caller should hold.
    """
    if not driver_id:
        return None

    try:
        # Find the most recent CONSECUTIVE run of low-speed heartbeats.
        # This is the "current stopped state" -- not "everything slow in the
        # last minute" which can conflate a red light earlier with a curb
        # stop now.
        cur.execute("""
            WITH recent AS (
                SELECT lat, lng, speed_mph, logged_at
                FROM app_private.heartbeat_log
                WHERE driver_id = %s
                  AND logged_at >= NOW() - make_interval(secs => %s)
                ORDER BY logged_at DESC
            ),
            tagged AS (
                SELECT *,
                       SUM(CASE WHEN speed_mph >= %s THEN 1 ELSE 0 END)
                         OVER (ORDER BY logged_at DESC
                               ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
                         AS breaks_before
                FROM recent
            ),
            current_run AS (
                SELECT lat, lng, speed_mph, logged_at
                FROM tagged
                WHERE breaks_before = 0    -- zero high-speed samples between
                                            -- this row and the most recent one
                  AND speed_mph < %s
            )
            SELECT
                COUNT(*)::int AS n,
                percentile_cont(0.5) WITHIN GROUP (ORDER BY lat)  AS median_lat,
                percentile_cont(0.5) WITHIN GROUP (ORDER BY lng)  AS median_lng,
                MIN(logged_at) AS earliest,
                MAX(logged_at) AS latest
            FROM current_run;
        """, (driver_id, window_sec, max_speed_mph, max_speed_mph))
        row = cur.fetchone()
        if not row or row["n"] is None or row["n"] < min_samples:
            return None

        median_lat = float(row["median_lat"])
        median_lng = float(row["median_lng"])

        # Spread check -- spread of CURRENT low-speed run only
        cur.execute("""
            WITH recent AS (
                SELECT lat, lng, speed_mph, logged_at
                FROM app_private.heartbeat_log
                WHERE driver_id = %s
                  AND logged_at >= NOW() - make_interval(secs => %s)
                ORDER BY logged_at DESC
            ),
            tagged AS (
                SELECT *,
                       SUM(CASE WHEN speed_mph >= %s THEN 1 ELSE 0 END)
                         OVER (ORDER BY logged_at DESC
                               ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
                         AS breaks_before
                FROM recent
            )
            SELECT MAX(
                app_private.distance_miles(lat, lng, %s, %s) * 1609.34
            ) AS max_dist_m
            FROM tagged
            WHERE breaks_before = 0 AND speed_mph < %s;
        """, (driver_id, window_sec, max_speed_mph,
              median_lat, median_lng, max_speed_mph))
        sr = cur.fetchone()
        if not sr or sr["max_dist_m"] is None:
            return None
        spread_m = float(sr["max_dist_m"])
        if spread_m > max_spread_m:
            return None

        duration_s = (row["latest"] - row["earliest"]).total_seconds()
        return Cluster(
            n=int(row["n"]),
            median_lat=median_lat,
            median_lng=median_lng,
            spread_m=spread_m,
            duration_s=duration_s,
            latest=row["latest"],
        )
    except Exception as e:
        logging.warning(f"[CLUSTER] detect_cluster failed: {e}")
        return None

# ============================================================================
# get_recent_clusters -- offer-anchored history primitive
# ============================================================================
#
# Phase E Step 6 Amendment 1 (sub-step 1a, 2026-04-27)
#
# Gaps-and-islands extension of detect_cluster()'s breaks_before pattern.
# Where detect_cluster() returns the most-recent island only (filter
# breaks_before = 0), this returns ALL qualifying islands in an
# offer-anchored window, oldest-first, by GROUPing on breaks_before.

def get_recent_clusters(driver_id: str, cur,
                        accepted_at_anchor: datetime,
                        preroll_sec: int = 60,
                        min_samples: int = 3,
                        max_speed_mph: float = 10.0,
                        max_spread_m: float = 25.0) -> list:
    """Find all qualifying stopped clusters in the offer-anchored lookback window.

    Window: [accepted_at_anchor - preroll_sec, NOW()].

    Each cluster is a contiguous low-speed island in heartbeat_log; islands
    are separated by >= 1 high-speed sample (gaps-and-islands extension of
    detect_cluster's breaks_before pattern). Returns oldest-first.

    Differences from detect_cluster():
      - Returns ALL qualifying islands, not just the most recent.
      - Spread filter drops only the offending island; other islands survive.
      - Empty list (not None) when no islands qualify.

    Pure DIAGNOSE primitive. No writes, no state mutation.

    Consumers:
      - where_am_i.WhereAmI() -- Amendment 1 cluster_revisit topology check
        (sub-step 1b) and B-26 same-address PLAN-side latch (sub-step 1c).
    """
    if not driver_id:
        return []

    try:
        cur.execute("""
            WITH window_hb AS (
                SELECT lat, lng, speed_mph, logged_at
                FROM app_private.heartbeat_log
                WHERE driver_id = %s
                  AND logged_at >= %s - make_interval(secs => %s)
                  AND logged_at <= NOW()
                ORDER BY logged_at DESC
            ),
            tagged AS (
                SELECT *,
                       SUM(CASE WHEN speed_mph >= %s THEN 1 ELSE 0 END)
                         OVER (ORDER BY logged_at DESC
                               ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
                         AS breaks_before
                FROM window_hb
            ),
            low_speed AS (
                SELECT lat, lng, logged_at, breaks_before AS island_id
                FROM tagged
                WHERE speed_mph < %s
            ),
            per_island AS (
                SELECT island_id,
                       COUNT(*)::int AS n,
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY lat) AS median_lat,
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY lng) AS median_lng,
                       MIN(logged_at) AS earliest,
                       MAX(logged_at) AS latest
                FROM low_speed
                GROUP BY island_id
                HAVING COUNT(*) >= %s
            ),
            per_island_spread AS (
                SELECT pi.island_id, pi.n, pi.median_lat, pi.median_lng,
                       pi.earliest, pi.latest,
                       MAX(app_private.distance_miles(ls.lat, ls.lng,
                                                      pi.median_lat, pi.median_lng) * 1609.34)
                         AS spread_m
                FROM per_island pi
                JOIN low_speed ls ON ls.island_id = pi.island_id
                GROUP BY pi.island_id, pi.n, pi.median_lat, pi.median_lng,
                         pi.earliest, pi.latest
            )
            SELECT n, median_lat, median_lng, spread_m, earliest, latest
            FROM per_island_spread
            WHERE spread_m <= %s
            ORDER BY latest ASC;
        """, (driver_id, accepted_at_anchor, preroll_sec,
              max_speed_mph, max_speed_mph, min_samples, max_spread_m))

        rows = cur.fetchall()
        clusters = []
        for row in rows:
            duration_s = (row["latest"] - row["earliest"]).total_seconds()
            clusters.append(Cluster(
                n=int(row["n"]),
                median_lat=float(row["median_lat"]),
                median_lng=float(row["median_lng"]),
                spread_m=float(row["spread_m"]),
                duration_s=duration_s,
                latest=row["latest"],
            ))
        return clusters
    except Exception as e:
        logging.warning(f"[CLUSTER] get_recent_clusters failed: {e}")
        return []
