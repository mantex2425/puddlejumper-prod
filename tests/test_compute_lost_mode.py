"""Unit tests for _compute_lost_mode — §XVIII.A two-bit lost-mode.

Regression for the 2026-06-16 bit-1 restoration: lost_mode must be FALSE once
current_offer_id binds, EVEN WITH other alive-unpicked offers in the queue.
That is the fix for permanent lost-mode + the 2026-06-15 over-fire (binding
never exited lost-mode under the prior bit-2-only derivation).
"""
from driver_heartbeat import _compute_lost_mode


def test_unbound_with_alive_unpicked_is_lost():
    assert _compute_lost_mode(None, ["11401", "11402"]) is True


def test_bound_is_not_lost_even_with_other_alive_unpicked():
    # THE fix: binding exits lost-mode regardless of remaining alive-unpicked
    assert _compute_lost_mode("11401", ["11402", "11403"]) is False


def test_unbound_empty_queue_is_not_lost():
    assert _compute_lost_mode(None, []) is False


def test_bound_empty_queue_is_not_lost():
    assert _compute_lost_mode("11401", []) is False
