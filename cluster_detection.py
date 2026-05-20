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
      spread_m      -- max distance (meters) from any cluster point to centroid.
                       Informational only post-P19; no longer a rejection gate.
      duration_s  -- span (seconds) from earliest to latest heartbeat in the
                       cluster. NB: this is the WIDTH of the cluster, not how
                       long the driver has been stopped overall (because the
                       cluster only counts heartbeats in the most recent
                       uninterrupted stillness run).
      latest        -- MAX(logged_at) of the cluster's heartbeats. Exposes
                       data already aggregated by the SQL.
      started_at    -- (P19) MIN(logged_at) of the cluster. Stable anchor for
                       planner-side dedup across the departure_grace window.
                       Optional for backward compat with stub-Cluster tests.
    """
    n: int
    median_lat: float
    median_lng: float
    spread_m: float
    duration_s: float
    latest: datetime
    started_at: Optional[datetime] = None


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
                   max_spread_m: float = 25.0,
                   min_duration_s: float = 10.0,
                   departure_grace_s: float = 5.0) -> Optional[Cluster]:
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
        # P19 — stillness gating. Find the most recent CONTIGUOUS RUN of
        # speed_mph = 0.0 heartbeats. Gaps-and-islands SQL groups stillness
        # samples into runs; we select the run with the latest ended_at that
        # also ended within `departure_grace_s` of NOW() (covers the
        # pull-away race window where motion resumes between heartbeats).
        #
        # No spread_m gate. By construction, contiguous speed=0 samples are
        # at a single physical location modulo GPS noise (typically <5m).
        # spread_m is still computed and returned for forensic visibility.
        #
        # max_speed_mph parameter retained for backward compat but no longer
        # affects gating (stillness is speed=0 exactly).
        cur.execute("""
            WITH samples AS (
                SELECT lat, lng, speed_mph, logged_at,
                       (speed_mph > 0) AS is_moving
                FROM app_private.heartbeat_log
                WHERE driver_id = %s
                  AND logged_at >= NOW() - make_interval(secs => %s)
                ORDER BY logged_at
            ),
            labeled AS (
                SELECT *,
                       SUM(CASE WHEN is_moving THEN 1 ELSE 0 END)
                         OVER (ORDER BY logged_at
                               ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
                         AS run_id
                FROM samples
            ),
            stillness_runs AS (
                SELECT run_id,
                       COUNT(*)::int AS n,
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY lat)  AS median_lat,
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY lng)  AS median_lng,
                       MIN(logged_at) AS started_at,
                       MAX(logged_at) AS latest,
                       EXTRACT(EPOCH FROM (MAX(logged_at) - MIN(logged_at)))::real
                         AS duration_s
                FROM labeled
                WHERE NOT is_moving
                GROUP BY run_id
                HAVING COUNT(*) >= %s
                   AND EXTRACT(EPOCH FROM (MAX(logged_at) - MIN(logged_at))) >= %s
                   AND MAX(logged_at) >= NOW() - make_interval(secs => %s)
            )
            SELECT run_id, n, median_lat, median_lng, started_at, latest, duration_s
            FROM stillness_runs
            ORDER BY latest DESC
            LIMIT 1
        """, (driver_id, window_sec, min_samples, min_duration_s, departure_grace_s))
        row = cur.fetchone()
        if not row or row["n"] is None:
            return None

        median_lat = float(row["median_lat"])
        median_lng = float(row["median_lng"])

        # Compute spread_m forensically (no gating). This is a second query;
        # we accept the cost because spread_m is observable in forensic logs
        # and we want it accurate when GPS noise is non-trivial.
        cur.execute("""
            WITH samples AS (
                SELECT lat, lng, speed_mph, logged_at
                FROM app_private.heartbeat_log
                WHERE driver_id = %s
                  AND logged_at >= %s
                  AND logged_at <= %s
                  AND speed_mph = 0.0
            )
            SELECT COALESCE(MAX(
                app_private.distance_miles(lat, lng, %s, %s) * 1609.34
            ), 0.0) AS spread_m
            FROM samples
        """, (driver_id, row["started_at"], row["latest"], median_lat, median_lng))
        sr = cur.fetchone()
        spread_m = float(sr["spread_m"]) if sr and sr["spread_m"] is not None else 0.0

        return Cluster(
            n=int(row["n"]),
            median_lat=median_lat,
            median_lng=median_lng,
            spread_m=spread_m,
            duration_s=float(row["duration_s"]),
            latest=row["latest"],
            started_at=row["started_at"],
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
                        max_spread_m: float = 25.0,
                        min_duration_s: float = 5.0) -> list:
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
        # P19 stillness-gating refactor. Identifies contiguous speed=0 runs
        # (islands) within the offer-anchored lookback window, returns all
        # qualifying islands oldest-first. min_duration_s defaults to 5s
        # (lower than detect_cluster's 10s) to give the matcher visibility
        # into micro-stillness islands for stop-creep-stop disambiguation
        # at airport curbs and apartment complexes.
        #
        # max_speed_mph and max_spread_m parameters retained for backward
        # compat but no longer affect gating.
        cur.execute("""
            WITH samples AS (
                SELECT lat, lng, speed_mph, logged_at,
                       (speed_mph > 0) AS is_moving
                FROM app_private.heartbeat_log
                WHERE driver_id = %s
                  AND logged_at >= %s - make_interval(secs => %s)
                  AND logged_at <= NOW()
                ORDER BY logged_at
            ),
            labeled AS (
                SELECT *,
                       SUM(CASE WHEN is_moving THEN 1 ELSE 0 END)
                         OVER (ORDER BY logged_at
                               ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
                         AS run_id
                FROM samples
            ),
            stillness_runs AS (
                SELECT run_id,
                       COUNT(*)::int AS n,
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY lat) AS median_lat,
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY lng) AS median_lng,
                       MIN(logged_at) AS started_at,
                       MAX(logged_at) AS latest,
                       EXTRACT(EPOCH FROM (MAX(logged_at) - MIN(logged_at)))::real
                         AS duration_s
                FROM labeled
                WHERE NOT is_moving
                GROUP BY run_id
                HAVING COUNT(*) >= %s
                   AND EXTRACT(EPOCH FROM (MAX(logged_at) - MIN(logged_at))) >= %s
            )
            SELECT
                s.run_id, s.n, s.median_lat, s.median_lng,
                s.started_at, s.latest, s.duration_s,
                COALESCE((
                    SELECT MAX(app_private.distance_miles(
                        l.lat, l.lng, s.median_lat, s.median_lng) * 1609.34)
                    FROM labeled l
                    WHERE l.run_id = s.run_id AND NOT l.is_moving
                ), 0.0) AS spread_m
            FROM stillness_runs s
            ORDER BY s.latest ASC
        """, (driver_id, accepted_at_anchor, preroll_sec, min_samples, min_duration_s))
        rows = cur.fetchall()

        clusters = []
        for row in rows:
            clusters.append(Cluster(
                n=int(row["n"]),
                median_lat=float(row["median_lat"]),
                median_lng=float(row["median_lng"]),
                spread_m=float(row["spread_m"]),
                duration_s=float(row["duration_s"]),
                latest=row["latest"],
                started_at=row["started_at"],
            ))
        return clusters
    except Exception as e:
        logging.warning(f"[CLUSTER] get_recent_clusters failed: {e}")
        return []
