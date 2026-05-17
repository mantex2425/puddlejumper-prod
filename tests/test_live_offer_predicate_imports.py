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

    def test_detect_lost_mode_uses_horizon_predicate(self):
        """_detect_lost_mode body must reference the predicate and
        must NOT contain the old pickup-state-proxy or 2-hour-window
        clauses.
        """
        text = _module_text("driver_heartbeat.py")
        m = re.search(
            r"def _detect_lost_mode\(.*?(?=\n(?:def |class ))",
            text,
            re.DOTALL,
        )
        assert m is not None, "_detect_lost_mode function not found"
        body_with_docstring = m.group(0)
        # Strip the docstring before checking for old proxies — the
        # new docstring explains the old rule by name, which would
        # false-positive this test.
        body = re.sub(r'"""(?:.|\n)*?"""', '', body_with_docstring, count=1)

        assert "LIVE_OFFER_PREDICATE_SQL" in body, (
            "_detect_lost_mode must use LIVE_OFFER_PREDICATE_SQL"
        )
        assert "live_offer_predicate_params" in body, (
            "_detect_lost_mode must pass the params helper to splice "
            "the bind tuple"
        )
        assert "interval '2 hours'" not in body, (
            "_detect_lost_mode still has the old 2-hour wall-clock proxy"
        )
        # §XVIII bit-2 trigger (2026-05-16): the `actual_pickup_at IS NULL`
        # clause is back, with new semantics. It is no longer a proxy for
        # "narrative broken" — it now expresses "queue contains an offer
        # whose pickup has not been observed." See CANONICAL_RULES §XVIII.A.
        assert "actual_pickup_at IS NULL" in body, (
            "_detect_lost_mode missing §XVIII bit-2 trigger "
            "`actual_pickup_at IS NULL`"
        )

    def test_detect_lost_mode_signature(self):
        """_detect_lost_mode signature must include
        current_cumulative_miles=None for the distance-axis bind.
        """
        text = _module_text("driver_heartbeat.py")
        assert (
            "def _detect_lost_mode(cur, driver_id, queue_offer_ids, "
            "current_cumulative_miles, reference_time)" in text
        ), "_detect_lost_mode signature missing current_cumulative_miles or reference_time"

    def test_detect_lost_mode_caller_threads_miles(self):
        """The caller in driver_heartbeat.py must thread
        current_cumulative_miles into _detect_lost_mode.
        """
        text = _module_text("driver_heartbeat.py")
        assert (
            "_detect_lost_mode(cur, driver_id, queue_ids_int, "
            "cumulative_miles, _heartbeat_now)" in text
        ), "_detect_lost_mode caller missing cumulative_miles/_heartbeat_now thread"

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
        """The params helper signature must require reference_time
        as a positional argument (no default). Defaults invite the
        failure mode where production accidentally picks up
        wall-clock when the dev meant to inject a test clock.
        """
        text = _module_text("driver_queue.py")
        assert (
            "def live_offer_predicate_params(current_cumulative_miles, "
            "reference_time):" in text
        ), (
            "live_offer_predicate_params signature must take "
            "reference_time as a required positional argument"
        )
