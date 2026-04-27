#!/usr/bin/env python3
"""
apply_substep_1a.py — Phase E Step 6 Amendment 1 sub-step 1a.

Implements get_recent_clusters() per Gemini-ratified design:
  - Adds Cluster.latest field (datetime; the MAX(logged_at) already in SQL).
  - Updates detect_cluster() to populate latest.
  - Appends get_recent_clusters() — gaps-and-islands SQL primitive.
  - Updates 6 call sites (5 tests + 1 smoke script) to supply latest=.
  - Adds 8 new tests (T1-T8) for get_recent_clusters().
  - Updates test_cluster_field_count to expect 6 fields.

Idempotent: rerun is a noop if already applied.

Run from ~/puddlejumper-prod/:
    python apply_substep_1a.py
"""

import sys
from pathlib import Path

ROOT = Path.cwd()
if not (ROOT / "cluster_detection.py").exists():
    print(f"ERROR: must run from repo root (cluster_detection.py not in {ROOT})")
    sys.exit(1)


# =============================================================================
# Helpers
# =============================================================================

def patch_file(rel_path, edits, label):
    """Apply (old, new) tuples to file. Skips already-applied edits. Fails loud."""
    path = ROOT / rel_path
    text = path.read_text()
    original = text
    applied = 0
    skipped = 0
    for i, (old, new) in enumerate(edits, 1):
        if old in text:
            text = text.replace(old, new, 1)
            applied += 1
        elif new in text:
            skipped += 1
        else:
            print(f"  [FAIL] {label} edit {i}: neither old nor new found")
            print(f"    old snippet: {repr(old[:120])}")
            sys.exit(2)
    if text != original:
        path.write_text(text)
    status = f"{applied} applied"
    if skipped:
        status += f", {skipped} already present"
    print(f"  [ok] {rel_path}: {status}")


def ensure_imports(rel_path, needed_imports):
    """Add 'from datetime import datetime, timezone' if missing.
    needed_imports: list of (probe_substring, line_to_insert).

    KNOWN LIMITATIONS (sub-step 1a forensic record — three bugs found at apply-time):
      1. Probe is substring match — 'from datetime import timezone' (without datetime)
         is a false-positive that suppresses insertion. test_pudo_planner.py hit this;
         repaired by switching that file's call sites to 'datetime.datetime(...)'.
      2. Doesn't track open parens — if last 'from' in first 50 lines is the start of
         a multi-line 'from x import (' continuation, insertion lands inside the parens.
         test_where_am_i.py hit this; repaired by relocating the import.
      3. Doesn't detect 'import datetime' (module form) shadowing the class form.
         test_where_am_i.py line 940 had this; repaired by aliasing to _dt/_tz.

    Future patch scripts that insert imports: track paren depth, use anchored regex
    for probes, audit for module-vs-class shadowing."""
    path = ROOT / rel_path
    text = path.read_text()
    lines = text.splitlines(keepends=True)
    inserted_any = False
    for probe, line_to_insert in needed_imports:
        if probe in text:
            continue
        # Insert after the last top-level import/from line in the first 50 lines
        insert_at = 0
        for i, line in enumerate(lines[:50]):
            stripped = line.lstrip()
            if stripped.startswith("import ") or stripped.startswith("from "):
                insert_at = i + 1
        lines.insert(insert_at, line_to_insert + "\n")
        inserted_any = True
        print(f"  [ok] {rel_path}: added '{line_to_insert}'")
    if inserted_any:
        path.write_text("".join(lines))


# =============================================================================
# 1. cluster_detection.py
# =============================================================================

GET_RECENT_CLUSTERS_FN = '''
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
'''


CLUSTER_DETECTION_EDITS = [
    # 1a. Add datetime import.
    (
        "from dataclasses import dataclass\nfrom typing import Optional\nimport logging\n",
        "from dataclasses import dataclass\nfrom datetime import datetime\nfrom typing import Optional\nimport logging\n",
    ),
    # 1b. Add latest field to Cluster (after duration_s, before closing dataclass).
    (
        '''      duration_s  -- span (seconds) from earliest to latest heartbeat in the
                       cluster. NB: this is the WIDTH of the cluster, not how
                       long the driver has been stopped overall (because the
                       cluster only counts heartbeats in the most recent
                       uninterrupted low-speed run).
    """
    n: int
    median_lat: float
    median_lng: float
    spread_m: float
    duration_s: float
''',
        '''      duration_s  -- span (seconds) from earliest to latest heartbeat in the
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
''',
    ),
    # 1c. Update detect_cluster return statement to populate latest.
    (
        '''        return Cluster(
            n=int(row["n"]),
            median_lat=median_lat,
            median_lng=median_lng,
            spread_m=spread_m,
            duration_s=duration_s,
        )''',
        '''        return Cluster(
            n=int(row["n"]),
            median_lat=median_lat,
            median_lng=median_lng,
            spread_m=spread_m,
            duration_s=duration_s,
            latest=row["latest"],
        )''',
    ),
    # 1d. Append get_recent_clusters at end of file.
    (
        '''    except Exception as e:
        logging.warning(f"[CLUSTER] detect_cluster failed: {e}")
        return None''',
        '''    except Exception as e:
        logging.warning(f"[CLUSTER] detect_cluster failed: {e}")
        return None

''' + GET_RECENT_CLUSTERS_FN.lstrip("\n"),
    ),
]


# =============================================================================
# 2. tests/test_cluster_detection.py
# =============================================================================

NEW_TESTS_BLOCK = '''


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
'''


_LATEST_KW = 'latest=datetime(2026, 4, 23, 20, 52, 16, tzinfo=timezone.utc),'

TEST_DETECTION_EDITS = [
    # 2a. Update import to include get_recent_clusters.
    (
        "from cluster_detection import detect_cluster, Cluster, is_stable\n",
        "from cluster_detection import detect_cluster, Cluster, is_stable, get_recent_clusters\n",
    ),
    # 2b. Update test_cluster_field_count to expect 6 fields.
    (
        '''def test_cluster_field_count():
    """Cluster has exactly 5 fields. Adding a field is a contract change that
    should be deliberate; if this test fails after a Cluster edit, audit
    consumers (bead_on_wire, where_am_i) for compatibility."""
    import dataclasses
    field_names = {f.name for f in dataclasses.fields(Cluster)}
    assert field_names == {"n", "median_lat", "median_lng", "spread_m", "duration_s"}''',
        '''def test_cluster_field_count():
    """Cluster has exactly 6 fields. Adding a field is a contract change that
    should be deliberate; if this test fails after a Cluster edit, audit
    consumers (bead_on_wire, where_am_i) for compatibility.

    Phase E Step 6 sub-step 1a: added 'latest' field to expose MAX(logged_at)
    that the SQL was already aggregating, for offer-anchored lookback sorting."""
    import dataclasses
    field_names = {f.name for f in dataclasses.fields(Cluster)}
    assert field_names == {"n", "median_lat", "median_lng", "spread_m", "duration_s", "latest"}''',
    ),
    # 2c. test_cluster_is_frozen — add latest=.
    (
        '    c = Cluster(n=4, median_lat=29.6, median_lng=-95.5, spread_m=0.0, duration_s=16.5)\n    with pytest.raises(Exception):  # FrozenInstanceError in 3.11+, AttributeError in older\n        c.n = 99',
        f'    c = Cluster(n=4, median_lat=29.6, median_lng=-95.5, spread_m=0.0, duration_s=16.5, {_LATEST_KW.rstrip(",")})\n    with pytest.raises(Exception):  # FrozenInstanceError in 3.11+, AttributeError in older\n        c.n = 99',
    ),
    # 2d. is_stable T1.
    (
        '    c = Cluster(n=4, median_lat=29.6, median_lng=-95.5, spread_m=0.0, duration_s=15.0)\n    assert is_stable(c, threshold_s=15.0) is True',
        f'    c = Cluster(n=4, median_lat=29.6, median_lng=-95.5, spread_m=0.0, duration_s=15.0, {_LATEST_KW.rstrip(",")})\n    assert is_stable(c, threshold_s=15.0) is True',
    ),
    # 2e. is_stable T2.
    (
        '    c = Cluster(n=4, median_lat=29.6, median_lng=-95.5, spread_m=0.0, duration_s=14.99)\n    assert is_stable(c, threshold_s=15.0) is False',
        f'    c = Cluster(n=4, median_lat=29.6, median_lng=-95.5, spread_m=0.0, duration_s=14.99, {_LATEST_KW.rstrip(",")})\n    assert is_stable(c, threshold_s=15.0) is False',
    ),
    # 2f. is_stable T3.
    (
        '    c = Cluster(n=4, median_lat=29.6, median_lng=-95.5, spread_m=0.0, duration_s=300.0)\n    assert is_stable(c, threshold_s=15.0) is True',
        f'    c = Cluster(n=4, median_lat=29.6, median_lng=-95.5, spread_m=0.0, duration_s=300.0, {_LATEST_KW.rstrip(",")})\n    assert is_stable(c, threshold_s=15.0) is True',
    ),
    # 2g. Append T1-T8 block at end of file (anchor: last assertion).
    (
        '    assert meta["row_count"] == 17',
        '    assert meta["row_count"] == 17' + NEW_TESTS_BLOCK,
    ),
]


# =============================================================================
# 3-6. Other call sites — append latest= kwarg.
# =============================================================================

def append_latest_kwarg(old_call_block):
    """Insert latest= kwarg before the closing paren of a Cluster() call."""
    # Find the closing ')' that terminates the Cluster(...) call. It's the
    # last line, indented to match the function's body, with a closing paren.
    # We append latest= as a new line just before that closing paren.
    lines = old_call_block.splitlines(keepends=True)
    # Find the closing paren line (last line containing ')').
    close_idx = None
    for i in range(len(lines) - 1, -1, -1):
        if ')' in lines[i] and 'Cluster(' not in lines[i]:
            close_idx = i
            break
    if close_idx is None:
        raise ValueError(f"Could not find closing paren in:\n{old_call_block}")
    # Indentation of the closing paren = indentation of the new latest= line.
    close_line = lines[close_idx]
    # The kwarg should match indentation of the OTHER kwargs (the line above).
    # Reuse the indentation of lines[close_idx - 1].
    prev_line = lines[close_idx - 1]
    indent = prev_line[:len(prev_line) - len(prev_line.lstrip())]
    new_lines = lines[:close_idx] + [f"{indent}{_LATEST_KW}\n"] + lines[close_idx:]
    return "".join(new_lines)


# 3. tests/test_scenarios.py
SCENARIOS_OLD = '''    cluster = Cluster(
        n=stop["last_observed_row_index"] - stop["first_observed_row_index"] + 1,
        median_lat=stop["lat"],
        median_lng=stop["lng"],
        spread_m=15.0,
        duration_s=float(stop["duration_s"]),
    )'''
SCENARIOS_NEW = append_latest_kwarg(SCENARIOS_OLD)
TEST_SCENARIOS_EDITS = [(SCENARIOS_OLD, SCENARIOS_NEW)]


# 4. tests/test_where_am_i.py — factory function.
WHERE_AM_I_OLD = '''    return Cluster(
        n=n,
        median_lat=median_lat,
        median_lng=median_lng,
        spread_m=spread_m,
        duration_s=duration_s,
    )'''
WHERE_AM_I_NEW = append_latest_kwarg(WHERE_AM_I_OLD)
TEST_WHERE_AM_I_EDITS = [(WHERE_AM_I_OLD, WHERE_AM_I_NEW)]


# 5. tests/test_pudo_planner.py — 2 sites.
PUDO_OLD_1 = '''        cluster = Cluster(
            n=8,
            median_lat=corrected_lat if corrected_lat is not None else _DEFAULT_LAT,
            median_lng=corrected_lng if corrected_lng is not None else _DEFAULT_LNG,
            spread_m=15.0,
            duration_s=30.0,
        )'''
PUDO_NEW_1 = append_latest_kwarg(PUDO_OLD_1)

PUDO_OLD_2 = '''        cluster = Cluster(
            n=8,
            median_lat=29.6246,
            median_lng=-95.5102,
            spread_m=18.0,
            duration_s=42.0,
        )'''
PUDO_NEW_2 = append_latest_kwarg(PUDO_OLD_2)
TEST_PUDO_EDITS = [(PUDO_OLD_1, PUDO_NEW_1), (PUDO_OLD_2, PUDO_NEW_2)]


# 6. scripts/wai_smoke.py
SMOKE_OLD = '''    synthetic_cluster = Cluster(
        n=4,
        median_lat=args.pickup_lat,
        median_lng=args.pickup_lng,
        spread_m=15.0,
        duration_s=30.0,
    )'''
SMOKE_NEW = append_latest_kwarg(SMOKE_OLD)
SMOKE_EDITS = [(SMOKE_OLD, SMOKE_NEW)]


# =============================================================================
# Apply
# =============================================================================

print("=" * 64)
print("Phase E Step 6 sub-step 1a: get_recent_clusters() + Cluster.latest")
print("=" * 64)

print("\n[1/6] cluster_detection.py")
patch_file("cluster_detection.py", CLUSTER_DETECTION_EDITS, "cluster_detection")

print("\n[2/6] tests/test_cluster_detection.py")
# Imports already present (datetime, timezone, MagicMock) per Step 2 paste.
patch_file("tests/test_cluster_detection.py", TEST_DETECTION_EDITS, "test_cluster_detection")

print("\n[3/6] tests/test_scenarios.py")
ensure_imports("tests/test_scenarios.py", [
    ("from datetime import", "from datetime import datetime, timezone"),
])
patch_file("tests/test_scenarios.py", TEST_SCENARIOS_EDITS, "test_scenarios")

print("\n[4/6] tests/test_where_am_i.py")
ensure_imports("tests/test_where_am_i.py", [
    ("from datetime import", "from datetime import datetime, timezone"),
])
patch_file("tests/test_where_am_i.py", TEST_WHERE_AM_I_EDITS, "test_where_am_i")

print("\n[5/6] tests/test_pudo_planner.py")
ensure_imports("tests/test_pudo_planner.py", [
    ("from datetime import", "from datetime import datetime, timezone"),
])
patch_file("tests/test_pudo_planner.py", TEST_PUDO_EDITS, "test_pudo_planner")

print("\n[6/6] scripts/wai_smoke.py")
ensure_imports("scripts/wai_smoke.py", [
    ("from datetime import", "from datetime import datetime, timezone"),
])
patch_file("scripts/wai_smoke.py", SMOKE_EDITS, "wai_smoke")

print("\n" + "=" * 64)
print("DONE. Run: pytest -q  (target: 250 + 8 = 258 passed)")
print("=" * 64)