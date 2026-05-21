"""PR-A sentinel tests — queue admission geocode signal refactor.

Validates the updated behavior of _bucket_to_target_spec and the _CLASS_DISPATCH
routing matrix under the 'Geocode as Signal' specification.
"""
from __future__ import annotations

import pytest
from unittest.mock import patch
from pudo_types import TargetSpec
from driver_heartbeat import _bucket_to_target_spec
from where_am_i import _CLASS_DISPATCH, _match_poi_class


def test_bucket_to_target_spec_admits_poi_null_coords():
    """POI-classed text with missing/null coordinates must be admitted with empty named_roads."""
    mock_classification = {"bucket": "poi", "parts": {"name": "Hobby Airport"}}

    with patch("driver_heartbeat.classify_address") as mock_classify:
        mock_classify.return_value = mock_classification

        result = _bucket_to_target_spec("Main Terminal, Arrivals", None, None)

        assert result is not None
        assert isinstance(result, TargetSpec)
        assert result.address_class == "poi"
        assert result.named_roads == ()
        assert result.lat is None
        assert result.lng is None
        assert result.address == "Main Terminal, Arrivals"


def test_bucket_to_target_spec_admits_intersection_null_coords():
    """Intersection text with null coords must be admitted carrying its extracted named roads."""
    mock_classification = {
        "bucket": "intersection",
        "parts": {"road_a": "Coltwood", "road_b": "Navajo"},
    }

    with patch("driver_heartbeat.classify_address") as mock_classify:
        mock_classify.return_value = mock_classification

        result = _bucket_to_target_spec("Coltwood Dr & Navajo Trail Dr", None, None)

        assert result is not None
        assert result.address_class == "intersection"
        assert result.named_roads == ("Coltwood", "Navajo")
        assert result.lat is None


def test_bucket_to_target_spec_admits_garbage_bucket():
    """Mangled OCR text hitting the garbage bucket must return a 'garbage' TargetSpec with empty roads."""
    mock_classification = {"bucket": "garbage", "parts": {}}

    with patch("driver_heartbeat.classify_address") as mock_classify:
        mock_classify.return_value = mock_classification

        result = _bucket_to_target_spec(
            "Main Terminal, Arrivals (Baggage, Texas", None, None
        )

        assert result is not None
        assert result.address_class == "garbage"
        assert result.named_roads == ()
        assert result.lat is None
        assert result.address == "Main Terminal, Arrivals (Baggage, Texas"


def test_bucket_to_target_spec_excludes_truly_empty_text():
    """Genuinely empty strings (zero content) must still fail closed.

    §PR-A contract: _bucket_to_target_spec returns None ONLY when there
    is no text to classify. Empty string and None fall into this category.
    Whitespace-only text is classifiable (via the strip+garbage path)
    and is admitted — see test_bucket_to_target_spec_admits_whitespace_as_garbage.
    """
    assert _bucket_to_target_spec("", 29.7604, -95.3698) is None
    assert _bucket_to_target_spec(None, 29.7604, -95.3698) is None


def test_bucket_to_target_spec_admits_whitespace_as_garbage():
    """Whitespace-only text classifies as garbage and is admitted.

    §PR-A: classify_address strips text and returns {"bucket": "garbage"}
    for whitespace-only input. Per the geocode-as-signal architecture,
    garbage offers are admitted so geofence/anchor heads can rescue them
    if the driver arrests inside a known polygon. WAI signal floor
    correctly skips these when no heads score (no false fire risk).
    """
    result = _bucket_to_target_spec("   ", 29.7604, -95.3698)
    assert result is not None
    assert result.address_class == "garbage"
    assert result.lat == 29.7604
    assert result.lng == -95.3698
    assert result.named_roads == ()
    assert result.address == "   "


def test_class_dispatch_routes_garbage_to_poi_matcher():
    """The garbage class enum string must map explicitly to the POI matching logic."""
    assert "garbage" in _CLASS_DISPATCH
    assert _CLASS_DISPATCH["garbage"] == _match_poi_class
