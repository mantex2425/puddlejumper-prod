"""decision_engine_v3 is the only engine (2026-09-16): no v2 call, no fallback."""
import inspect

import decisions.engine as engine
import decisions.router as router


def test_engine_module_never_calls_v2():
    src = inspect.getsource(engine)
    assert "decision_engine_v2" not in src
    assert "engine_version" not in src
    assert "decision_mode" not in src


def test_router_has_no_v2_simulation_endpoint():
    src = inspect.getsource(router)
    assert "decision_engine_v2" not in src
    assert "simulate-suite" not in src


def test_v3_failure_is_a_visible_decline_with_no_rates():
    r = engine.engine_error_result(12.5)
    assert r["verdict"] == "DECLINE"
    assert r["reason"] == "Can't score offer"
    assert r["declineClass"] == "config:engine_error"      # device shows the reason, not money
    assert r["netHourlyUsd"] is None and r["grossHourlyUsd"] is None and r["dsi"] is None
    assert r["engineVersion"] == "v3"


def test_reason_fits_the_frog():
    # The device shows reason.take(18) for an unmapped config decline.
    assert len(engine.ENGINE_ERROR_REASON) <= 18
