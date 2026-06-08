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


def test_geofence_containment_returns_commit_score():
    """§P18b (reshaped 2026-06-08): containment with no name match is the ground-truth
    LOCATION signal → GEOFENCE_CONTAINMENT_COMMIT_SCORE (0.60), no dwell/spread guard.
    Whether it FIRES is the dispatch's call (NULL-geocode offers only + the §XVI.C floor)."""
    rows = [
        {"id": 999, "name": None, "name_normalized": None,
         "category": "mall", "area_m2": 10000.0, "iata": None, "icao": None},
    ]
    cur = _build_cursor(rows)
    score, witness = _signal_geofence_membership(
        cur, cluster_lat=29.7, cluster_lng=-95.4,
        target_text="Some Random Office, Houston, Texas",
    )
    assert score == 0.60, f"containment must return the commit score (0.60); got {score}"
    assert "contained" in witness


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
