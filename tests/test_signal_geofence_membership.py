"""P18 sentinel tests: _signal_geofence_membership."""
from __future__ import annotations

from unittest.mock import MagicMock

from where_am_i import _signal_geofence_membership


def _build_cursor(rows):
    cur = MagicMock()
    cur.fetchall.return_value = rows
    return cur


def test_geofence_no_containment_returns_zero():
    cur = _build_cursor([])
    score, witness = _signal_geofence_membership(
        cur, cluster_lat=29.6713, cluster_lng=-95.5284,
        target_text="American Airlines, Houston, Texas",
    )
    assert score == 0.0
    assert witness is None


def test_geofence_iata_word_boundary_hit_returns_one():
    rows = [
        {"id": 268, "name": None, "name_normalized": None, "category": "terminal",
         "area_m2": 43687.0, "iata": None, "icao": None},
        {"id": 5, "name": "William P. Hobby Airport",
         "name_normalized": "william p hobby airport",
         "category": "airport", "area_m2": 5277277.0, "iata": "HOU", "icao": "KHOU"},
    ]
    cur = _build_cursor(rows)
    score, witness = _signal_geofence_membership(
        cur, cluster_lat=29.65479, cluster_lng=-95.27736,
        target_text="William P. Hobby Airport (HOU), Houston, Texas",
    )
    assert score == 1.0, f"got {score} witness={witness}"
    assert "HOU" in witness


def test_geofence_iata_word_boundary_substring_rejected():
    """HOU must NOT match 'Houston Street' — \\b prevents substring."""
    rows = [
        {"id": 5, "name": "William P. Hobby Airport",
         "name_normalized": "william p hobby airport",
         "category": "airport", "area_m2": 5277277.0, "iata": "HOU", "icao": None},
    ]
    cur = _build_cursor(rows)
    # Hypothetical: target text mentions "Houston Street" only
    score, witness = _signal_geofence_membership(
        cur, cluster_lat=29.65479, cluster_lng=-95.27736,
        target_text="1234 Houston Street, Sugar Land, Texas",
    )
    # IATA won\'t match (Houston has no word-boundary HOU).
    # Fuzzy might match the airport name slightly but should not clear 0.6.
    assert score < 1.0, f"got {score} witness={witness} — HOU should not substring-match Houston"


def test_geofence_fuzzy_name_match_returns_one():
    rows = [
        {"id": 99, "name": "NRG Stadium", "name_normalized": "nrg stadium",
         "category": "stadium", "area_m2": 56094.0, "iata": None, "icao": None},
    ]
    cur = _build_cursor(rows)
    score, witness = _signal_geofence_membership(
        cur, cluster_lat=29.6847, cluster_lng=-95.4107,
        target_text="NRG Stadium, Houston, Texas",
    )
    assert score == 1.0
    assert "NRG" in witness or "Stadium" in witness


def test_geofence_terminal_iata_inheritance_via_outer_polygon():
    """Innermost terminal lacks IATA, outer airport has it.

    Empirical case from 2026-05-20 ingest: Hobby has two containing polygons.
    The terminal (id=268) has no name and no IATA. The airport (id=5) has both.
    """
    rows = [
        {"id": 268, "name": None, "name_normalized": None, "category": "terminal",
         "area_m2": 43687.0, "iata": None, "icao": None},
        {"id": 5, "name": "William P. Hobby Airport",
         "name_normalized": "william p hobby airport",
         "category": "airport", "area_m2": 5277277.0, "iata": "HOU", "icao": "KHOU"},
    ]
    cur = _build_cursor(rows)
    score, witness = _signal_geofence_membership(
        cur, cluster_lat=29.65479, cluster_lng=-95.27736,
        target_text="Main Terminal, Hobby Airport (HOU)",
    )
    assert score == 1.0, f"outer airport IATA must rescue terminal hit; got {score}"
    assert "HOU" in witness


def test_geofence_containment_no_metadata_returns_thirty():
    """Containment exists but neither IATA nor fuzzy name match (no cluster shape)."""
    rows = [
        {"id": 999, "name": None, "name_normalized": None,
         "category": "mall", "area_m2": 10000.0, "iata": None, "icao": None},
    ]
    cur = _build_cursor(rows)
    score, witness = _signal_geofence_membership(
        cur, cluster_lat=29.7, cluster_lng=-95.4,
        target_text="Some Random Office, Houston, Texas",
    )
    assert score == 0.30, f"contained-no-name-match must score 0.30; got {score}"
    assert "contained-no-name-match" in witness


# --- §P18b venue track (2026-06-08): containment-fire on a genuine stop ---------

_MALL_ROW = [
    {"id": 999, "name": None, "name_normalized": None,
     "category": "mall", "area_m2": 10000.0, "iata": None, "icao": None},
]


def test_geofence_containment_genuine_stop_fires():
    """§P18b: containment + a real STOP (long dwell, tight spread) → commit score."""
    cur = _build_cursor(list(_MALL_ROW))
    score, witness = _signal_geofence_membership(
        cur, cluster_lat=29.7, cluster_lng=-95.4,
        target_text="Some Random Office, Houston, Texas",
        cluster_duration_s=60.0, cluster_spread_m=30.0,
    )
    assert score == 0.60, f"genuine stop inside polygon must fire (0.60); got {score}"
    assert "contained-genuine-stop" in witness


def test_geofence_containment_drivethrough_short_dwell_stays_thirty():
    """A pass-through (dwell below the floor) must NOT fire — stays 0.30."""
    cur = _build_cursor(list(_MALL_ROW))
    score, _ = _signal_geofence_membership(
        cur, cluster_lat=29.7, cluster_lng=-95.4,
        target_text="Some Random Office, Houston, Texas",
        cluster_duration_s=10.0, cluster_spread_m=30.0,
    )
    assert score == 0.30, f"short-dwell pass-through must stay 0.30; got {score}"


def test_geofence_containment_loose_spread_stays_thirty():
    """A loose cluster (not a real stop) must NOT fire — stays 0.30."""
    cur = _build_cursor(list(_MALL_ROW))
    score, _ = _signal_geofence_membership(
        cur, cluster_lat=29.7, cluster_lng=-95.4,
        target_text="Some Random Office, Houston, Texas",
        cluster_duration_s=120.0, cluster_spread_m=300.0,
    )
    assert score == 0.30, f"loose-spread cluster must stay 0.30; got {score}"


def test_geofence_fuzzy_floor_tightened_rejects_loose_match():
    """§P18b: fuzzy floor 0.6→0.8. The real FP (GBIA address vs an 'Alvin Airpark'
    polygon scored 0.70) must no longer reach 1.0 once the floor is 0.8."""
    rows = [
        {"id": 7, "name": "Alvin Airpark", "name_normalized": "alvin airpark",
         "category": "airport", "area_m2": 108577.0, "iata": None, "icao": None},
    ]
    cur = _build_cursor(rows)
    score, witness = _signal_geofence_membership(
        cur, cluster_lat=29.4153, cluster_lng=-95.2889,
        target_text="George Bush Intercontinental Airport",
    )
    assert score < 1.0, f"loose fuzzy (≈0.70) must not name-match at floor 0.8; got {score} {witness}"
