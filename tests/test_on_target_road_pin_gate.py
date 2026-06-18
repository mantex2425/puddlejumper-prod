"""Phase 2: on_target_road transit-road pin gate (2026-06-18).

Gates the long-road false match (on a TRANSIT road km from the pin) WITHOUT
breaking the Path B.2 residential rescue (short road, bad pin, proximity=0).
"""
from where_am_i import _signal_on_target_road, ON_TARGET_ROAD_PIN_HORIZON_M


def test_transit_credits_near_pin():
    assert _signal_on_target_road("Gulf Fwy", ("Gulf Fwy",),
                                  current_road_class='transit', dist_to_pin_m=100.0) == 1.0


def test_transit_gated_far_from_pin():
    # the over-fire long-road false match: on the freeway km from the pickup
    assert _signal_on_target_road("Gulf Fwy", ("Gulf Fwy",),
                                  current_road_class='transit',
                                  dist_to_pin_m=ON_TARGET_ROAD_PIN_HORIZON_M + 1) == 0.0
    assert _signal_on_target_road("Gulf Fwy", ("Gulf Fwy",),
                                  current_road_class='transit', dist_to_pin_m=5300.0) == 0.0


def test_transit_boundary_credits_at_horizon():
    assert _signal_on_target_road("Gulf Fwy", ("Gulf Fwy",),
                                  current_road_class='transit',
                                  dist_to_pin_m=ON_TARGET_ROAD_PIN_HORIZON_M) == 1.0


def test_residential_NOT_gated_far_from_pin():
    # Path B.2 rescue: residential street, bad pin (far), proximity=0 -> STILL credits
    assert _signal_on_target_road("Watts Plantation Dr", ("Watts Plantation Dr",),
                                  current_road_class='residential', dist_to_pin_m=5300.0) == 1.0


def test_unknown_class_NOT_gated():
    # no road class / no pin -> back-compat, no gate
    assert _signal_on_target_road("Watts Plantation Dr", ("Watts Plantation Dr",),
                                  dist_to_pin_m=5300.0) == 1.0
    assert _signal_on_target_road("Gulf Fwy", ("Gulf Fwy",)) == 1.0


def test_no_name_match_still_zero():
    assert _signal_on_target_road("Some Other Rd", ("Gulf Fwy",),
                                  current_road_class='transit', dist_to_pin_m=10.0) == 0.0
