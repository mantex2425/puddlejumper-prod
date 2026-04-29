"""test_adjacency.py — unit tests for the adjacency DIAGNOSE primitive.

Provenance (L-9):
  Test scenarios derived from:
  - 2026-04-27 Houston shift forensic (commit 773aa49): Planet Fitness
    canonical regression case (S32). Cluster centroid 29.8495463,-95.5037494
    expected to whitelist Hollister + Northwest Freeway frontage road
    plus internal lot segments (per Grok's verification query against
    production data on this date).
  - Coordinate-order rule from CANONICAL § I (lat, lng — never lng, lat).
  - Cursor-flavor robustness: production uses RealDictCursor;
    test_pudo_planner.py and test_where_am_i.py use injected fakes.
    These tests verify both shapes work.

L-9 corollary refinement (boundary fixtures):
  Boundary cases for the buffer (just-inside and just-outside 150m) are
  out of scope for unit tests — they require real production geometry
  and are integration-test territory. This file uses fakes; integration
  validation happens via the truth table during practice phase.

Style: pytest, fakes injected directly. No DB connection required.
"""

from __future__ import annotations

import pytest

from adjacency import (
    ADJACENCY_BUFFER_M,
    get_adjacent_roads,
    get_adjacent_roads_for_cluster,
)


# ============================================================================
# Fake cursor: captures the SQL and params, returns canned rows.
# ============================================================================

class _FakeCursor:
    """Minimal psycopg cursor stand-in for unit tests.

    Records the most recent execute() call so tests can assert on the
    SQL and params. fetchall() returns whatever was loaded via
    set_result(). Default cursor flavor returns tuple rows; flip
    use_dict=True to emulate RealDictCursor.
    """

    def __init__(self, use_dict: bool = False):
        self._result: list = []
        self._last_sql: str = ""
        self._last_params: tuple = ()
        self.use_dict = use_dict

    def set_result(self, names: list[str]) -> None:
        if self.use_dict:
            self._result = [{"name": n} for n in names]
        else:
            self._result = [(n,) for n in names]

    def execute(self, sql: str, params: tuple) -> None:
        self._last_sql = sql
        self._last_params = params

    def fetchall(self):
        return list(self._result)

    @property
    def last_sql(self) -> str:
        return self._last_sql

    @property
    def last_params(self) -> tuple:
        return self._last_params


# ============================================================================
# Fake cluster: minimal dataclass-shaped object with the two fields the
# wrapper reads (median_lat, median_lng).
# ============================================================================

class _FakeCluster:
    """Minimal Cluster stand-in. Only median_lat/median_lng are read by
    get_adjacent_roads_for_cluster.
    """

    def __init__(self, median_lat: float, median_lng: float):
        self.median_lat = median_lat
        self.median_lng = median_lng


# ============================================================================
# Tests
# ============================================================================

def test_returns_empty_tuple_when_lat_is_none():
    """Defensive: None lat short-circuits before SQL execution."""
    cur = _FakeCursor()
    cur.set_result(["Should Never Be Returned"])
    result = get_adjacent_roads(cur, None, -95.5)
    assert result == ()
    assert cur.last_sql == "", "SQL must not execute when lat is None"


def test_returns_empty_tuple_when_lng_is_none():
    """Defensive: None lng short-circuits before SQL execution."""
    cur = _FakeCursor()
    cur.set_result(["Should Never Be Returned"])
    result = get_adjacent_roads(cur, 29.8, None)
    assert result == ()
    assert cur.last_sql == ""


def test_returns_empty_tuple_when_no_rows():
    """Cluster in middle of nowhere — no roads in 150m. Empty tuple, not None."""
    cur = _FakeCursor()
    cur.set_result([])
    result = get_adjacent_roads(cur, 29.8495463, -95.5037494)
    assert result == ()
    assert isinstance(result, tuple)


def test_returns_alphabetized_tuple_of_names_tuple_cursor():
    """Default cursor flavor: row[0] indexing. Mimics ordered SQL output."""
    cur = _FakeCursor(use_dict=False)
    cur.set_result(["Hollister", "Northwest Freeway", "Player Bend"])
    result = get_adjacent_roads(cur, 29.8495463, -95.5037494)
    assert result == ("Hollister", "Northwest Freeway", "Player Bend")
    assert isinstance(result, tuple)


def test_returns_alphabetized_tuple_of_names_dict_cursor():
    """RealDictCursor flavor: row['name'] indexing. Production cursor type."""
    cur = _FakeCursor(use_dict=True)
    cur.set_result(["Hollister", "Northwest Freeway", "Player Bend"])
    result = get_adjacent_roads(cur, 29.8495463, -95.5037494)
    assert result == ("Hollister", "Northwest Freeway", "Player Bend")


def test_filters_out_null_or_empty_names():
    """SQL has a WHERE name IS NOT NULL guard, but be defensive: if a
    row somehow has name='' or None, drop it."""
    cur = _FakeCursor(use_dict=True)
    cur._result = [
        {"name": "Hollister"},
        {"name": None},
        {"name": ""},
        {"name": "Northwest Freeway"},
    ]
    result = get_adjacent_roads(cur, 29.8495463, -95.5037494)
    assert result == ("Hollister", "Northwest Freeway")


def test_uses_canonical_coords_to_geography_function():
    """Per CANONICAL § I: must call app_private.coords_to_geography,
    NEVER raw ST_MakePoint. The SQL string is the proof."""
    cur = _FakeCursor()
    cur.set_result([])
    get_adjacent_roads(cur, 29.8495463, -95.5037494)
    sql = cur.last_sql
    assert "app_private.coords_to_geography" in sql, (
        "Adjacency SQL must use canonical coords_to_geography wrapper"
    )
    assert "ST_MakePoint" not in sql, (
        "Adjacency SQL must NOT use raw ST_MakePoint (CANONICAL § I)"
    )


def test_lat_lng_argument_order_passed_correctly():
    """CANONICAL § I: arguments are always (lat, lng).

    The SQL placeholders are (%s, %s, %s) for (lat, lng, buffer_m).
    Verify the params tuple lands in that order.
    """
    cur = _FakeCursor()
    cur.set_result([])
    get_adjacent_roads(cur, 29.8495463, -95.5037494, buffer_m=150)
    assert cur.last_params == (29.8495463, -95.5037494, 150)


def test_default_buffer_is_adjacency_buffer_m():
    """Default buffer matches the module constant — test proves the
    constant is what gets passed when buffer_m is omitted."""
    cur = _FakeCursor()
    cur.set_result([])
    get_adjacent_roads(cur, 29.8, -95.5)
    assert cur.last_params[2] == ADJACENCY_BUFFER_M
    assert ADJACENCY_BUFFER_M == 150  # The Houston Standard, B.5 lock


def test_custom_buffer_is_honored():
    """Caller can override buffer; useful for testing wider/tighter
    captures during forensic investigation."""
    cur = _FakeCursor()
    cur.set_result([])
    get_adjacent_roads(cur, 29.8, -95.5, buffer_m=300)
    assert cur.last_params[2] == 300


def test_for_cluster_returns_empty_when_cluster_is_none():
    """Convenience wrapper: None cluster -> empty tuple, no SQL."""
    cur = _FakeCursor()
    cur.set_result(["Should Not Run"])
    result = get_adjacent_roads_for_cluster(cur, None)
    assert result == ()
    assert cur.last_sql == ""


def test_for_cluster_extracts_median_coords():
    """Wrapper passes cluster.median_lat / median_lng to the underlying
    function. Verifies the canonical-coord-order plumbing."""
    cur = _FakeCursor(use_dict=True)
    cur.set_result(["Hollister"])
    cluster = _FakeCluster(median_lat=29.8495463, median_lng=-95.5037494)
    result = get_adjacent_roads_for_cluster(cur, cluster)
    assert result == ("Hollister",)
    # And confirm the params were passed in (lat, lng) order, not flipped:
    assert cur.last_params == (29.8495463, -95.5037494, ADJACENCY_BUFFER_M)


def test_planet_fitness_forensic_case_shape():
    """L-9 provenance: from 2026-04-27 Houston shift forensic, Planet
    Fitness centroid 29.8495463,-95.5037494 should produce a whitelist
    that includes Hollister AND a Northwest Freeway frontage road
    (verified empirically by Grok's query pre-this-test).

    This test confirms the SHAPE of the function's behavior against that
    forensic case — not the production data itself (that's an
    integration concern). With fakes, we assert that a multi-road result
    includes both expected road categories.
    """
    cur = _FakeCursor(use_dict=True)
    # Mirror the empirical result from the forensic case:
    cur.set_result([
        "Hollister",
        "Northwest Freeway",
        "Northwest Freeway Frontage Road",
        "Player Bend",
        "Terramont",
    ])
    cluster = _FakeCluster(median_lat=29.8495463, median_lng=-95.5037494)
    result = get_adjacent_roads_for_cluster(cur, cluster)

    assert "Hollister" in result, "official Uber pickup road must be in whitelist"
    assert any("Northwest Freeway" in r for r in result), (
        "frontage road must be in whitelist — this is the Planet Fitness back-entrance signal"
    )
    assert len(result) >= 2, "real parking-lot footprints touch multiple named roads"
