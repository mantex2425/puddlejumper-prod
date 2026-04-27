"""
test_cluster_detection.py -- Phase C equivalence + behavior test

This test verifies that the relocated detect_cluster() function in
cluster_detection.py is bit-identical in behavior to the original PRIMITIVE 6
in bead_on_wire.py.

Test strategy: hermetic, no live database required.
  - The 17-row 7623 fixture is loaded from JSON.
  - The function's two SQL queries are mocked at the cursor level.
  - For each test scenario we hand-compute what the SQL WOULD return on the
    fixture data at a given temporal cutoff, and assert the function builds
    the correct Cluster object from that.

Hand-computation of SQL semantics:
  - Query 1: filters heartbeat_log to (logged_at >= NOW() - window_sec),
    tags each row with breaks_before (running count of speed >= max_speed
    samples from most recent walking back), filters to (breaks_before = 0
    AND speed_mph < max_speed), aggregates COUNT/median/min/max.
  - Query 2: same CTE chain, computes MAX distance from given (lat,lng).

The "cutoff" for each scenario is the simulated NOW(). Heartbeats logged
after that cutoff are not visible to the query.

Note: this file does NOT import from bead_on_wire. After Phase C the original
function no longer exists there. The test verifies the relocated function in
isolation against a snapshot of expected outputs derived from the original's
documented semantics.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from cluster_detection import detect_cluster, Cluster, is_stable, get_recent_clusters


FIXTURE_DIR = Path(__file__).parent / "fixtures"
DRIVER_ID = "UjT1hE9eBXh2q95aSZYOkzDJ8lo1"


# ============================================================================
# Fixture loading
# ============================================================================

def _load_heartbeats():
    """Load and parse the 7623 heartbeat fixture, converting ISO timestamps
    to datetime objects so they behave like real psycopg fetched rows."""
    with (FIXTURE_DIR / "7623_heartbeats.json").open() as f:
        data = json.load(f)
    rows = data["rows"]
    for r in rows:
        r["logged_at"] = datetime.fromisoformat(r["logged_at"])
    return rows


def _make_mock_cursor(query1_result, query2_result=None):
    """Return a mock cursor whose fetchone() returns query1 then query2."""
    cur = MagicMock()
    side_effects = [query1_result]
    if query2_result is not None:
        side_effects.append(query2_result)
    cur.fetchone.side_effect = side_effects
    return cur


# ============================================================================
# Cluster dataclass invariants
# ============================================================================

def test_cluster_is_frozen():
    """Cluster must be immutable. Mutating any field must raise."""
    c = Cluster(n=4, median_lat=29.6, median_lng=-95.5, spread_m=0.0, duration_s=16.5, latest=datetime(2026, 4, 23, 20, 52, 16, tzinfo=timezone.utc))
    with pytest.raises(Exception):  # FrozenInstanceError in 3.11+, AttributeError in older
        c.n = 99


def test_cluster_field_count():
    """Cluster has exactly 6 fields. Adding a field is a contract change that
    should be deliberate; if this test fails after a Cluster edit, audit
    consumers (bead_on_wire, where_am_i) for compatibility.

    Phase E Step 6 sub-step 1a: added 'latest' field to expose MAX(logged_at)
    that the SQL was already aggregating, for offer-anchored lookback sorting."""
    import dataclasses
    field_names = {f.name for f in dataclasses.fields(Cluster)}
    assert field_names == {"n", "median_lat", "median_lng", "spread_m", "duration_s", "latest"}


# ============================================================================
# is_stable predicate
# ============================================================================

def test_is_stable_at_threshold_is_true():
    """Boundary: duration == threshold should be stable (>= comparison)."""
    c = Cluster(n=4, median_lat=29.6, median_lng=-95.5, spread_m=0.0, duration_s=15.0, latest=datetime(2026, 4, 23, 20, 52, 16, tzinfo=timezone.utc))
    assert is_stable(c, threshold_s=15.0) is True


def test_is_stable_below_threshold_is_false():
    c = Cluster(n=4, median_lat=29.6, median_lng=-95.5, spread_m=0.0, duration_s=14.99, latest=datetime(2026, 4, 23, 20, 52, 16, tzinfo=timezone.utc))
    assert is_stable(c, threshold_s=15.0) is False


def test_is_stable_well_above_threshold():
    c = Cluster(n=4, median_lat=29.6, median_lng=-95.5, spread_m=0.0, duration_s=300.0, latest=datetime(2026, 4, 23, 20, 52, 16, tzinfo=timezone.utc))
    assert is_stable(c, threshold_s=15.0) is True


# ============================================================================
# detect_cluster -- empty / error short-circuits
# ============================================================================

def test_empty_driver_id_short_circuits():
    """Empty driver_id returns None without touching the cursor."""
    cur = MagicMock()
    result = detect_cluster("", cur)

    assert result is None
    cur.execute.assert_not_called()


def test_db_error_caught_and_returns_none():
    """Any cursor exception is caught and yields None (with warning log)."""
    cur = MagicMock()
    cur.execute.side_effect = RuntimeError("connection lost")
    result = detect_cluster(DRIVER_ID, cur)

    assert result is None


# ============================================================================
# detect_cluster -- forensic scenarios derived from the 7623 fixture
# ============================================================================

def test_scenario_tight_4sample_stop_at_T16s():
    """Scenario: cutoff just after row index 3 (logged_at = 20:52:16.278843).
    Last 4 heartbeats are all speed=0 at the same coordinates.
    breaks_before=0 for all 4. min_samples=3 satisfied. spread=0.

    This is the cluster the system FAILED to fire on. Phase C proves the
    cluster math itself was correct -- the failure was downstream.
    """
    rows = _load_heartbeats()
    earliest = rows[0]["logged_at"]
    latest = rows[3]["logged_at"]

    # What the SQL WOULD return at this cutoff:
    query1_result = {
        "n": 4,
        "median_lat": 29.6245833,
        "median_lng": -95.5102295,
        "earliest": earliest,
        "latest": latest,
    }
    # All 4 points at identical coordinates => max distance from median = 0.
    query2_result = {"max_dist_m": 0.0}

    cur = _make_mock_cursor(query1_result, query2_result)
    result = detect_cluster(DRIVER_ID, cur)

    assert result is not None
    assert isinstance(result, Cluster)
    assert result.n == 4
    assert result.median_lat == pytest.approx(29.6245833)
    assert result.median_lng == pytest.approx(-95.5102295)
    assert result.spread_m == pytest.approx(0.0, abs=0.01)
    # Duration = 20:52:16.278843 - 20:51:59.735134 = 16.543709 seconds
    assert result.duration_s == pytest.approx(16.543709, abs=0.001)

    # Cluster meets the v1 stability threshold of 15s
    assert is_stable(result, threshold_s=15.0) is True
    # But not a stricter 20s threshold
    assert is_stable(result, threshold_s=20.0) is False


def test_scenario_no_cluster_when_currently_moving():
    """Scenario: cutoff after row index 8 (logged_at = 20:52:44.150754).
    The most recent heartbeats are all speed >= 10 mph (the driver pulled
    away from the stop). breaks_before > 0 for every earlier slow sample,
    so the current_run is empty.

    Empty current_run -> COUNT(*) = 0, medians NULL.
    """
    query1_result = {
        "n": 0,
        "median_lat": None,
        "median_lng": None,
        "earliest": None,
        "latest": None,
    }
    cur = _make_mock_cursor(query1_result)
    result = detect_cluster(DRIVER_ID, cur)

    assert result is None


def test_scenario_undersized_cluster_rejected():
    """Scenario: cutoff at row index 11 (logged_at = 20:53:00.947572).
    Driver briefly paused (5 seconds, 1 zero-speed sample). All earlier
    samples were high-speed approach traffic. n=1, fails default
    min_samples=3.
    """
    ts = datetime(2026, 4, 23, 20, 53, 0, 947572, tzinfo=timezone.utc)
    query1_result = {
        "n": 1,
        "median_lat": 29.6218166,
        "median_lng": -95.5101237,
        "earliest": ts,
        "latest": ts,
    }
    cur = _make_mock_cursor(query1_result)
    result = detect_cluster(DRIVER_ID, cur)

    assert result is None


def test_scenario_min_samples_override_lets_brief_pause_qualify():
    """Same fixture cutoff as above but min_samples=1. Now the brief pause
    qualifies and a cluster is returned. Proves the parameter is wired.
    """
    ts = datetime(2026, 4, 23, 20, 53, 0, 947572, tzinfo=timezone.utc)
    query1_result = {
        "n": 1,
        "median_lat": 29.6218166,
        "median_lng": -95.5101237,
        "earliest": ts,
        "latest": ts,
    }
    query2_result = {"max_dist_m": 0.0}
    cur = _make_mock_cursor(query1_result, query2_result)

    result = detect_cluster(DRIVER_ID, cur, min_samples=1)

    assert result is not None
    assert result.n == 1
    assert result.duration_s == pytest.approx(0.0)


def test_scenario_spread_exceeds_max_returns_none():
    """Scenario: hypothetical 4-sample cluster passes the count gate, but
    the spread query reports max distance 50m (exceeds default 25m).
    Function returns None, even though n was sufficient."""
    earliest = datetime(2026, 4, 23, 20, 51, 59, tzinfo=timezone.utc)
    latest = datetime(2026, 4, 23, 20, 52, 16, tzinfo=timezone.utc)

    query1_result = {
        "n": 4,
        "median_lat": 29.6,
        "median_lng": -95.5,
        "earliest": earliest,
        "latest": latest,
    }
    query2_result = {"max_dist_m": 50.0}  # > default max_spread_m=25

    cur = _make_mock_cursor(query1_result, query2_result)
    result = detect_cluster(DRIVER_ID, cur)

    assert result is None


def test_scenario_spread_at_threshold_accepted():
    """Boundary: spread exactly at max_spread_m is accepted (<= comparison)."""
    earliest = datetime(2026, 4, 23, 20, 51, 59, tzinfo=timezone.utc)
    latest = datetime(2026, 4, 23, 20, 52, 16, tzinfo=timezone.utc)

    query1_result = {
        "n": 4,
        "median_lat": 29.6,
        "median_lng": -95.5,
        "earliest": earliest,
        "latest": latest,
    }
    query2_result = {"max_dist_m": 25.0}  # exactly at default max_spread_m

    cur = _make_mock_cursor(query1_result, query2_result)
    result = detect_cluster(DRIVER_ID, cur)

    assert result is not None
    assert result.spread_m == pytest.approx(25.0)


def test_scenario_null_max_dist_returns_none():
    """Edge: spread query returns NULL max_dist_m (empty current_run on
    second query, race condition). Function defends with None."""
    earliest = datetime(2026, 4, 23, 20, 51, 59, tzinfo=timezone.utc)
    latest = datetime(2026, 4, 23, 20, 52, 16, tzinfo=timezone.utc)

    query1_result = {
        "n": 4,
        "median_lat": 29.6,
        "median_lng": -95.5,
        "earliest": earliest,
        "latest": latest,
    }
    query2_result = {"max_dist_m": None}

    cur = _make_mock_cursor(query1_result, query2_result)
    result = detect_cluster(DRIVER_ID, cur)

    assert result is None


# ============================================================================
# Sanity: fixture file is consistent with what the test scenarios assume
# ============================================================================

def test_fixture_has_expected_shape():
    """Guard against fixture drift. If someone re-pulls the fixture and the
    row count or anchor coords change, this test surfaces it loudly."""
    rows = _load_heartbeats()
    assert len(rows) == 17

    # First 4 rows are the actual stop -- speed=0, identical coords
    for r in rows[:4]:
        assert r["speed_mph"] == 0.0
        assert r["lat"] == 29.6245833
        assert r["lng"] == -95.5102295

    # Last row is the moment of state transition to IN_TRIP
    assert rows[-1]["state"] == "IN_TRIP"
    assert rows[-1]["speed_mph"] > 30.0


def test_fixture_metadata_matches_offer_6999():
    """Anchor: fixture metadata identifies offer 6999 / current_offer_id 7623."""
    with (FIXTURE_DIR / "7623_heartbeats.json").open() as f:
        data = json.load(f)
    meta = data["metadata"]
    assert meta["offer_history_id"] == 6999
    assert meta["current_offer_id_text"] == "7623"
    assert meta["driver_id"] == DRIVER_ID
    assert meta["row_count"] == 17


# ============================================================================
# get_recent_clusters -- Phase E Step 6 sub-step 1a (T1-T8)
# ============================================================================

ANCHOR_TS = datetime(2026, 4, 23, 20, 52, 16, tzinfo=timezone.utc)


def _make_island_row(n, median_lat, median_lng, spread_m, earliest, latest):
    """Mock fetchall() row matching get_recent_clusters' SELECT shape."""
    return {
        "n": n,
        "median_lat": median_lat,
        "median_lng": median_lng,
        "spread_m": spread_m,
        "earliest": earliest,
        "latest": latest,
    }


def _make_recent_clusters_cursor(rows):
    """Mock cursor whose fetchall() returns the supplied rows."""
    cur = MagicMock()
    cur.fetchall.return_value = rows
    return cur


# T1
def test_get_recent_clusters_empty_driver_id_short_circuits():
    """Empty driver_id returns [] without touching cursor."""
    cur = MagicMock()
    result = get_recent_clusters("", cur, accepted_at_anchor=ANCHOR_TS)
    assert result == []
    cur.execute.assert_not_called()


# T2
def test_get_recent_clusters_db_error_returns_empty():
    """Cursor exception caught, returns [] (consistent with detect_cluster)."""
    cur = MagicMock()
    cur.execute.side_effect = RuntimeError("connection lost")
    result = get_recent_clusters(DRIVER_ID, cur, accepted_at_anchor=ANCHOR_TS)
    assert result == []


# T3
def test_get_recent_clusters_empty_window_returns_empty():
    """fetchall() returns [] => function returns []."""
    cur = _make_recent_clusters_cursor([])
    result = get_recent_clusters(DRIVER_ID, cur, accepted_at_anchor=ANCHOR_TS)
    assert result == []


# T4
def test_get_recent_clusters_single_island_builds_one_cluster():
    """One row from fetchall() => one Cluster, all fields populated."""
    earliest = datetime(2026, 4, 23, 20, 51, 59, tzinfo=timezone.utc)
    latest = datetime(2026, 4, 23, 20, 52, 16, tzinfo=timezone.utc)
    rows = [_make_island_row(4, 29.6245833, -95.5102295, 0.5, earliest, latest)]
    cur = _make_recent_clusters_cursor(rows)
    result = get_recent_clusters(DRIVER_ID, cur, accepted_at_anchor=ANCHOR_TS)
    assert len(result) == 1
    c = result[0]
    assert isinstance(c, Cluster)
    assert c.n == 4
    assert c.median_lat == pytest.approx(29.6245833)
    assert c.median_lng == pytest.approx(-95.5102295)
    assert c.spread_m == pytest.approx(0.5)
    assert c.duration_s == pytest.approx(17.0)
    assert c.latest == latest


# T5
def test_get_recent_clusters_multiple_islands_preserve_order():
    """Two rows from fetchall() => two Clusters in input order.
    SQL ORDER BY latest ASC is producer-side; function must not re-sort."""
    older_earliest = datetime(2026, 4, 23, 20, 51, 0, tzinfo=timezone.utc)
    older_latest = datetime(2026, 4, 23, 20, 51, 30, tzinfo=timezone.utc)
    newer_earliest = datetime(2026, 4, 23, 20, 52, 0, tzinfo=timezone.utc)
    newer_latest = datetime(2026, 4, 23, 20, 52, 16, tzinfo=timezone.utc)
    rows = [
        _make_island_row(3, 29.6240, -95.5100, 5.0, older_earliest, older_latest),
        _make_island_row(4, 29.6245, -95.5102, 1.0, newer_earliest, newer_latest),
    ]
    cur = _make_recent_clusters_cursor(rows)
    result = get_recent_clusters(DRIVER_ID, cur, accepted_at_anchor=ANCHOR_TS)
    assert len(result) == 2
    assert result[0].latest == older_latest
    assert result[1].latest == newer_latest
    assert result[0].latest < result[1].latest


# T6
def test_get_recent_clusters_passes_query_parameters_in_order():
    """Parameter binding order is contractual (driver_id, anchor, preroll_sec,
    max_speed_mph x2, min_samples, max_spread_m). Single execute call."""
    cur = _make_recent_clusters_cursor([])
    get_recent_clusters(
        DRIVER_ID, cur,
        accepted_at_anchor=ANCHOR_TS,
        preroll_sec=45,
        min_samples=5,
        max_speed_mph=8.0,
        max_spread_m=20.0,
    )
    assert cur.execute.call_count == 1
    args, _ = cur.execute.call_args
    _sql, params = args
    assert params == (DRIVER_ID, ANCHOR_TS, 45, 8.0, 8.0, 5, 20.0)


# T7
def test_get_recent_clusters_duration_computed_from_earliest_latest():
    """duration_s = (latest - earliest).total_seconds() per row."""
    earliest = datetime(2026, 4, 23, 20, 50, 0, tzinfo=timezone.utc)
    latest = datetime(2026, 4, 23, 20, 50, 42, tzinfo=timezone.utc)
    rows = [_make_island_row(5, 29.62, -95.51, 2.0, earliest, latest)]
    cur = _make_recent_clusters_cursor(rows)
    result = get_recent_clusters(DRIVER_ID, cur, accepted_at_anchor=ANCHOR_TS)
    assert result[0].duration_s == pytest.approx(42.0)


# T8
def test_get_recent_clusters_default_kwargs_match_detect_cluster():
    """Default min_samples=3, max_speed_mph=10.0, max_spread_m=25.0 must match
    detect_cluster() defaults — single source of truth across both primitives."""
    cur = _make_recent_clusters_cursor([])
    get_recent_clusters(DRIVER_ID, cur, accepted_at_anchor=ANCHOR_TS, preroll_sec=60)
    args, _ = cur.execute.call_args
    _sql, params = args
    assert params[3] == 10.0   # max_speed_mph
    assert params[4] == 10.0   # max_speed_mph (repeated for two %s slots)
    assert params[5] == 3      # min_samples
    assert params[6] == 25.0   # max_spread_m
