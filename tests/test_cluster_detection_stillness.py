"""P19 sentinel tests — cluster stillness refactor.

Validates the refactored detect_cluster + get_recent_clusters behavior
ratified Andrew + Gemini + Claude 2026-05-20.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

from cluster_detection import Cluster, detect_cluster, get_recent_clusters


def _build_cursor_row(n=4, median_lat=29.6, median_lng=-95.5,
                     duration_s=15.0,
                     started_at=None, latest=None,
                     spread_m=2.0):
    if started_at is None:
        started_at = datetime(2026, 5, 20, 14, 0, 0, tzinfo=timezone.utc)
    if latest is None:
        latest = datetime(2026, 5, 20, 14, 0, 15, tzinfo=timezone.utc)
    return {
        "run_id": 0,
        "n": n,
        "median_lat": median_lat,
        "median_lng": median_lng,
        "started_at": started_at,
        "latest": latest,
        "duration_s": duration_s,
        "spread_m": spread_m,
    }


def test_detect_cluster_returns_none_if_no_driver_id():
    cur = MagicMock()
    assert detect_cluster("", cur) is None


def test_detect_cluster_returns_none_on_empty_result():
    cur = MagicMock()
    cur.fetchone.return_value = None
    assert detect_cluster("driver", cur) is None


def test_detect_cluster_returns_cluster_with_started_at():
    """Healthy stillness run returns Cluster with started_at populated."""
    cur = MagicMock()
    cur.fetchone.side_effect = [
        _build_cursor_row(n=5, duration_s=15.0),
        {"spread_m": 1.2},
    ]
    result = detect_cluster("driver", cur)
    assert result is not None
    assert result.n == 5
    assert result.duration_s == 15.0
    assert result.started_at == datetime(2026, 5, 20, 14, 0, 0, tzinfo=timezone.utc)
    assert result.latest == datetime(2026, 5, 20, 14, 0, 15, tzinfo=timezone.utc)
    assert result.spread_m == 1.2


def test_detect_cluster_started_at_distinct_from_latest():
    """started_at should be the min, latest should be the max."""
    started = datetime(2026, 5, 20, 14, 0, 0, tzinfo=timezone.utc)
    latest = datetime(2026, 5, 20, 14, 0, 22, tzinfo=timezone.utc)
    cur = MagicMock()
    cur.fetchone.side_effect = [
        _build_cursor_row(n=5, duration_s=22.0,
                          started_at=started, latest=latest),
        {"spread_m": 1.0},
    ]
    result = detect_cluster("driver", cur)
    assert result.started_at == started
    assert result.latest == latest
    assert result.started_at < result.latest


def test_detect_cluster_handles_sql_exception():
    cur = MagicMock()
    cur.execute.side_effect = RuntimeError("simulated db failure")
    assert detect_cluster("driver", cur) is None


def test_get_recent_clusters_empty_driver_returns_empty_list():
    cur = MagicMock()
    assert get_recent_clusters("", cur, datetime.now(timezone.utc)) == []


def test_get_recent_clusters_returns_list_of_clusters():
    """Multiple stillness runs in the lookback window produce one Cluster each."""
    cur = MagicMock()
    row1 = _build_cursor_row(n=4, duration_s=8.0,
                              started_at=datetime(2026, 5, 20, 14, 0, 0, tzinfo=timezone.utc),
                              latest=datetime(2026, 5, 20, 14, 0, 8, tzinfo=timezone.utc))
    row2 = _build_cursor_row(n=6, duration_s=15.0,
                              started_at=datetime(2026, 5, 20, 14, 5, 0, tzinfo=timezone.utc),
                              latest=datetime(2026, 5, 20, 14, 5, 15, tzinfo=timezone.utc))
    cur.fetchall.return_value = [row1, row2]
    cur.fetchone.side_effect = [{"spread_m": 0.5}, {"spread_m": 1.1}]
    result = get_recent_clusters("driver", cur,
                                  datetime(2026, 5, 20, 14, 0, 0, tzinfo=timezone.utc))
    assert len(result) == 2
    assert all(isinstance(c, Cluster) for c in result)
    assert result[0].duration_s == 8.0
    assert result[1].duration_s == 15.0
    assert result[0].started_at < result[1].started_at


def test_get_recent_clusters_handles_sql_exception():
    cur = MagicMock()
    cur.execute.side_effect = RuntimeError("simulated db failure")
    result = get_recent_clusters("driver", cur, datetime.now(timezone.utc))
    assert result == []


def test_cluster_dataclass_supports_optional_started_at():
    """Existing test patterns (started_at omitted) still work."""
    c = Cluster(
        n=3, median_lat=29.6, median_lng=-95.5,
        spread_m=0.0, duration_s=15.0,
        latest=datetime(2026, 5, 20, 14, 0, 15, tzinfo=timezone.utc),
    )
    assert c.started_at is None


def test_cluster_dataclass_carries_started_at_when_provided():
    started = datetime(2026, 5, 20, 14, 0, 0, tzinfo=timezone.utc)
    c = Cluster(
        n=3, median_lat=29.6, median_lng=-95.5,
        spread_m=0.0, duration_s=15.0,
        latest=datetime(2026, 5, 20, 14, 0, 15, tzinfo=timezone.utc),
        started_at=started,
    )
    assert c.started_at == started


def _haversine_m(lat1, lng1, lat2, lng2):
    import math
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def test_density_peak_lands_at_knot_while_median_smears(db_cur):
    """§P18b db_cur integration (§XIV.J — real cursor, real GROUP BY coords_to_h3,
    NOT a mock). A synthetic ELONGATED stillness run: a sparse creep-in string
    (8 samples ~18 m apart, speed 0.3 → in the run) plus a dense knot at the curb
    (6 samples within ~2 m, speed 0.0). The density-peak (densest H3 cell centroid)
    must land at the KNOT; the median (PERCENTILE_CONT) is dragged back along the
    creep. Asserts committed-anchor (peak) ≈ knot, median off it, peak closer."""
    drv = "test-density-peak-driver-p18b"
    knot_lat, knot_lng = 29.99100, -95.33680

    rows = []
    # Creep-in string: 8 samples, ~18 m apart, heading toward the curb, slow-creep
    # (0.3 mph < 0.5 → is_moving False → in the stillness run). secs-ago 14..7.
    creep_offsets = [0.00130, 0.00114, 0.00097, 0.00081, 0.00065, 0.00049, 0.00032, 0.00016]
    for i, off in enumerate(creep_offsets):
        rows.append((knot_lat - off, knot_lng, 0.3, 14 - i))
    # Dense knot: 6 samples within ~2 m (one H3 cell), stopped. secs-ago 6..1.
    knot_jit = [0.00002, -0.00002, 0.00001, -0.00001, 0.0, 0.00002]
    for j, jit in enumerate(knot_jit):
        rows.append((knot_lat + jit, knot_lng + jit, 0.0, 6 - j))

    for lat, lng, spd, ago in rows:
        db_cur.execute(
            """INSERT INTO app_private.heartbeat_log (driver_id, lat, lng, speed_mph, logged_at)
               VALUES (%s, %s, %s, %s, NOW() - make_interval(secs => %s))""",
            (drv, lat, lng, spd, ago),
        )

    c = detect_cluster(drv, db_cur)
    assert c is not None, "expected a stillness cluster from the synthetic run"
    assert c.peak_lat is not None, "density-peak must be populated"
    peak_d = _haversine_m(c.peak_lat, c.peak_lng, knot_lat, knot_lng)
    med_d = _haversine_m(c.median_lat, c.median_lng, knot_lat, knot_lng)
    assert peak_d < 10, f"density-peak should land at the knot; got {peak_d:.1f} m off"
    assert med_d > 20, f"median should smear back along the creep; got only {med_d:.1f} m off"
    assert peak_d < med_d, f"peak ({peak_d:.1f} m) must beat median ({med_d:.1f} m) to the knot"
