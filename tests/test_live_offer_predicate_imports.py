"""Structural contract: LIVE_OFFER_PREDICATE_SQL has exactly one home.

Per Canonical Rule §H, the live-offer predicate is defined once in
driver_queue.py and imported by all consumers. This test enforces
the structural contract.

Replaces the failure mode where two textually-identical SQL strings
drift apart in the same commit (caught by the old textual-drift gate
in test_driver_queue.py, which still exists and still works). The
new test catches the case where someone defines the predicate in a
THIRD location — which the textual-drift gate misses.
"""
import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent


def _module_text(name):
    path = REPO_ROOT / name
    return path.read_text() if path.exists() else ""


class TestPredicateStructuralContract:

    def test_driver_queue_defines_the_predicate(self):
        """driver_queue.py is the canonical home."""
        text = _module_text("driver_queue.py")
        assert "LIVE_OFFER_PREDICATE_SQL = " in text
        assert "def live_offer_predicate_params" in text

    def test_driver_heartbeat_imports_not_defines(self):
        """driver_heartbeat.py must IMPORT the predicate."""
        text = _module_text("driver_heartbeat.py")
        assert "LIVE_OFFER_PREDICATE_SQL" in text
        assert "from driver_queue import" in text or "import driver_queue" in text
        # Negative: it must not redefine
        assert not re.search(
            r"^LIVE_OFFER_PREDICATE_SQL\s*=", text, re.MULTILINE
        ), "driver_heartbeat.py must not redefine the predicate"

    def test_no_third_definition_anywhere(self):
        """No production module outside driver_queue.py may define
        LIVE_OFFER_PREDICATE_SQL. Test files are exempt (they may
        intentionally reference the constant string).
        """
        pattern = re.compile(r"^LIVE_OFFER_PREDICATE_SQL\s*=", re.MULTILINE)
        offenders = []
        for py_file in REPO_ROOT.glob("*.py"):
            if py_file.name == "driver_queue.py":
                continue
            if pattern.search(py_file.read_text()):
                offenders.append(py_file.name)
        assert not offenders, (
            "LIVE_OFFER_PREDICATE_SQL defined outside driver_queue.py: "
            + ", ".join(offenders)
        )

    def test_get_alive_unpicked_offer_ids_uses_horizon_predicate(self):
        """_get_alive_unpicked_offer_ids body must reference the canonical
        predicate and must NOT contain the old pickup-state-proxy or
        2-hour-window clauses.

        Updated 2026-05-31 (§XVIII cold-start bind sprint): the predicate
        body was extracted from _detect_lost_mode into a new helper
        _get_alive_unpicked_offer_ids that returns the set of alive-
        unpicked offer IDs (so the FirePickupObservation cold-start bind
        can consume the same set). _detect_lost_mode now delegates to
        this helper. Single source of truth — the body must live exactly
        here. test_detect_lost_mode_delegates_to_helper enforces the
        delegation; this test enforces the body's content.

        Updated 2026-06-06 (§5.5 receipt-path trigger): the helper was
        RELOCATED from driver_heartbeat.py to driver_queue.py (the shared
        leaf beside LIVE_OFFER_PREDICATE_SQL) so decisions/logger.py can
        import it without the driver_heartbeat import cycle. The body now
        lives in driver_queue.py; this test reads it there. One body, three
        consumers (detector, cold-start bind, receipt trigger).
        """
        text = _module_text("driver_queue.py")
        m = re.search(
            r"def _get_alive_unpicked_offer_ids\(.*?(?=\n(?:def |class ))",
            text,
            re.DOTALL,
        )
        assert m is not None, "_get_alive_unpicked_offer_ids function not found"
        body_with_docstring = m.group(0)
        # Strip the docstring before checking for old proxies — the
        # new docstring explains the old rule by name, which would
        # false-positive this test.
        body = re.sub(r'"""(?:.|\n)*?"""', '', body_with_docstring, count=1)

        assert "LIVE_OFFER_PREDICATE_SQL" in body, (
            "_get_alive_unpicked_offer_ids must use LIVE_OFFER_PREDICATE_SQL"
        )
        assert "live_offer_predicate_params" in body, (
            "_get_alive_unpicked_offer_ids must pass the params helper to "
            "splice the bind tuple"
        )
        assert "interval '2 hours'" not in body, (
            "_get_alive_unpicked_offer_ids still has the old 2-hour wall-clock proxy"
        )
        # §XVIII bit-2 trigger (2026-05-16, recast 2026-05-31): the
        # `actual_pickup_at IS NULL` clause expresses "queue contains an
        # offer whose pickup has not been observed." See CANONICAL_RULES §XVIII.A.
        assert "actual_pickup_at IS NULL" in body, (
            "_get_alive_unpicked_offer_ids missing §XVIII bit-2 trigger "
            "`actual_pickup_at IS NULL`"
        )

    def test_detect_lost_mode_delegates_to_helper(self):
        """_detect_lost_mode must delegate to _get_alive_unpicked_offer_ids,
        not duplicate the query body.

        Added 2026-05-31 alongside the helper extraction. Without this
        test, a future commit could re-inline the query into
        _detect_lost_mode (well-intentioned consolidation) and silently
        recreate the parallel-function drift surface the helper was
        created to eliminate.
        """
        text = _module_text("driver_heartbeat.py")
        m = re.search(
            r"def _detect_lost_mode\(.*?(?=\n(?:def |class ))",
            text,
            re.DOTALL,
        )
        assert m is not None, "_detect_lost_mode function not found"
        body_with_docstring = m.group(0)
        body = re.sub(r'"""(?:.|\n)*?"""', '', body_with_docstring, count=1)

        assert "_get_alive_unpicked_offer_ids" in body, (
            "_detect_lost_mode must delegate to _get_alive_unpicked_offer_ids "
            "(single-source-of-truth contract per 2026-05-31 cold-start fix)"
        )
        # Negative: the query body must NOT live here anymore.
        assert "LIVE_OFFER_PREDICATE_SQL" not in body, (
            "_detect_lost_mode must NOT reference LIVE_OFFER_PREDICATE_SQL "
            "directly — the predicate lives in _get_alive_unpicked_offer_ids. "
            "Delegate, don't duplicate."
        )
        assert "actual_pickup_at IS NULL" not in body, (
            "_detect_lost_mode must NOT reference the bit-2 SQL clause "
            "directly — that lives in _get_alive_unpicked_offer_ids."
        )

    def test_detect_lost_mode_signature(self):
        """_detect_lost_mode signature must include current_cumulative_miles
        and reference_time as required positionals (for distance + clock
        binding), plus last_odometer_move_at=None as the staleness-gate
        parameter (defaulted; permissive on NULL).

        Updated 2026-05-19 (P10/P11 staleness gate): the signature grew
        from 5 to 6 positional parameters. reference_time remains required
        (no default) per Rule VII; last_odometer_move_at is defaulted to
        None because the staleness clause short-circuits to permissive
        when the timestamp is NULL.
        """
        text = _module_text("driver_heartbeat.py")
        assert (
            "def _detect_lost_mode(cur, driver_id, queue_offer_ids, "
            "current_cumulative_miles, reference_time, "
            "last_odometer_move_at=None)" in text
        ), (
            "_detect_lost_mode signature drift: must accept "
            "(cur, driver_id, queue_offer_ids, current_cumulative_miles, "
            "reference_time, last_odometer_move_at=None)"
        )

    def test_predicate_caller_threads_miles(self):
        """The heartbeat caller must thread cumulative_miles, the
        captured _heartbeat_now reference time, AND the
        effective_last_move staleness timestamp computed by the
        pre-fetch+effective-compute block.

        Updated 2026-05-19 (P10/P11): caller now passes 6 arguments
        instead of 5. The 6th, effective_last_move, is computed in the
        heartbeat handler from prior driver_trip_state row + current
        cumulative_miles per the §XIV.H pre-fetch pattern.

        Renamed 2026-05-31: the call target changed from _detect_lost_mode
        to _get_alive_unpicked_offer_ids when the predicate body was
        extracted into the new helper (the bind on FirePickupObservation
        needs the SET of alive-unpicked IDs, not just the bool from the
        old helper). _detect_lost_mode now delegates and is no longer
        called by the heartbeat path. The thread-the-right-params
        property is enforced against the new symbol, but the intent
        (heartbeat caller must pass cumulative_miles + _heartbeat_now +
        effective_last_move to the predicate evaluator) is unchanged.
        """
        text = _module_text("driver_heartbeat.py")
        assert (
            "_get_alive_unpicked_offer_ids(\n"
            "        cur, driver_id, cumulative_miles, _heartbeat_now, effective_last_move,\n"
            "    )" in text
        ), (
            "Predicate caller signature drift: heartbeat must thread "
            "cumulative_miles, _heartbeat_now, and effective_last_move "
            "to _get_alive_unpicked_offer_ids"
        )

    def test_predicate_does_not_use_server_clock(self):
        """Rule VII final form: LIVE_OFFER_PREDICATE_SQL must NOT
        reference NOW() server-side. The clock is explicit data
        passed via live_offer_predicate_params(.., reference_time).

        Why: a physics engine that depends on the server clock as
        a side effect is non-deterministic. Replay harnesses
        (Houston Playback) require the ability to anchor evaluation
        at any historical moment.
        """
        text = _module_text("driver_queue.py")
        # Extract the LIVE_OFFER_PREDICATE_SQL constant string body
        m = re.search(
            r'LIVE_OFFER_PREDICATE_SQL\s*=\s*"""(.*?)"""',
            text,
            re.DOTALL,
        )
        assert m is not None, "LIVE_OFFER_PREDICATE_SQL constant not found"
        predicate_body = m.group(1)
        assert "NOW()" not in predicate_body, (
            "LIVE_OFFER_PREDICATE_SQL still references NOW() — clock "
            "is not fully parametrized. Rule VII violation."
        )
        # Affirmative: must use %s::timestamptz for the reference clock
        assert "%s::timestamptz" in predicate_body, (
            "LIVE_OFFER_PREDICATE_SQL missing %s::timestamptz placeholder "
            "for the reference clock parameter"
        )

    def test_predicate_has_causality_guard(self):
        """Causality Guard (2026-05-12): LIVE_OFFER_PREDICATE_SQL must
        include the `oh.created_at <= %s::timestamptz` clause that
        prevents future offers from leaking into the live set during
        replay.

        Why: a replay harness that anchors at a historical reference_time
        would otherwise return offers created AFTER reference_time, which
        violates causality (offer cannot be live before it exists).
        Production NOW() was silently safe; replay was not.
        """
        text = _module_text("driver_queue.py")
        m = re.search(
            r'LIVE_OFFER_PREDICATE_SQL\s*=\s*"""(.*?)"""',
            text,
            re.DOTALL,
        )
        assert m is not None, "LIVE_OFFER_PREDICATE_SQL constant not found"
        predicate_body = m.group(1)
        # Tolerant whitespace match — the clause may be split across lines
        # or formatted with extra indent, but must contain the operator.
        assert "oh.created_at <= %s::timestamptz" in predicate_body, (
            "LIVE_OFFER_PREDICATE_SQL missing Causality Guard clause "
            "`oh.created_at <= %s::timestamptz`. Future offers would "
            "leak into the live set during replay. See Houston Playback."
        )

    def test_params_helper_requires_reference_time(self):
        """The params helper signature must require reference_time as
        a positional argument (no default), per Rule VII deterministic-
        clock requirement. last_odometer_move_at MAY default to None
        because the staleness gate's NULL-guard sub-clause provides
        graceful degradation — but reference_time has no such fallback
        and must be explicit.

        Updated 2026-05-19 (P10/P11): helper now takes 3 positionals
        (current_cumulative_miles, reference_time, last_odometer_move_at).
        The last has a default; the first two do not.
        """
        text = _module_text("driver_queue.py")
        assert (
            "def live_offer_predicate_params(current_cumulative_miles, "
            "reference_time, last_odometer_move_at=None):" in text
        ), (
            "live_offer_predicate_params signature must be "
            "(current_cumulative_miles, reference_time, "
            "last_odometer_move_at=None) — reference_time required, "
            "last_odometer_move_at defaulted"
        )

        # Defensive: explicitly assert reference_time has no default.
        # If someone ever adds `reference_time=None`, this catches it.
        import re as _re
        sig_match = _re.search(
            r"def live_offer_predicate_params\(([^)]*)\):", text
        )
        assert sig_match is not None, "helper signature regex failed to match"
        params_str = sig_match.group(1)
        # Find the reference_time parameter and verify no `=` follows it
        # before the next comma or end-of-string.
        rt_match = _re.search(r"reference_time(\s*=\s*[^,]+)?", params_str)
        assert rt_match is not None, "reference_time missing from signature"
        assert rt_match.group(1) is None, (
            "reference_time has a default value — Rule VII violation. "
            "The clock must always be explicit; defaults invite the "
            "failure mode where production accidentally uses wall-clock."
        )
