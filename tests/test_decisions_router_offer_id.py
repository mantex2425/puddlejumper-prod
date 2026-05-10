"""
Sprint 1 (Identity Genesis, server-side) — unit tests for offerId
acceptance, validation, and persistence in decisions/router.py.

Producer side: Android APK 1.1.20+ mints UUIDv7 (RFC 9562 §5.7) at OCR
parse time and sends it on every POST /api/v1/decisions/. PR #1 on
mantex2425/Puddle_Jumper, branch feature/uuid-v7-validation, validated
end-to-end on real hardware (see docs/SERVER_SIDE_SPRINT_1_EVIDENCE_2026-05-09.md).

Consumer side (this file): server validates format and rejects with HTTP
400 on missing/invalid; valid UUIDs flow into decision_log.trace_data
JSONB via decisions/logger.py.

═══════════════════════════════════════════════════════════════════════
Test architecture
═══════════════════════════════════════════════════════════════════════

Two layers:

1. **Direct parse_request tests** (no Flask). parse_request is "never
   fails" per its docstring; tests confirm it extracts offerId without
   raising and round-trips garbage unchanged for the route handler to
   validate.

2. **Flask test_client tests** for the route handler validation block.
   This is the FIRST test_client usage in this codebase. We accept the
   precedent because Andrew's road-test time is too expensive to spend
   discovering 400-path regressions from the driver's seat.

Fixture design:
  - Session-scoped to amortize the app.py import cost across all tests.
  - app.py is light-boot per its module docstring (no DB/firebase init at
    module scope), but it does import 25+ blueprint modules. Session
    scope means we pay that once per pytest run, not per test.
  - Authentication is bypassed via patching verify_and_get_user_id at the
    decisions.router module boundary — not at the auth module boundary —
    because Flask resolves the imported name in the importer's namespace.
  - get_db is NOT patched. The 400-path tests we're running here all
    return BEFORE Stage 2's `conn = get_db()` call. If a future test in
    this file exercises the positive path (200), it MUST add get_db
    patching at that point.

Real production UUID fixtures from 2026-05-09 ~23:04 CDT, Andrew's device
(driver UjT1hE9eBXh2q95aSZYOkzDJ8lo1), OCR'd from real Uber offer cards,
verified RFC 9562 §5.7 compliant by the Android UuidCreator library.
"""
import json
import pytest

from decisions.router import parse_request


# ─────────────────────────────────────────────────────────────────────
# Real production v7 UUIDs from the 2026-05-09 evening device validation.
# Each minted at OCR parse time on Android, sent over the wire, captured
# in logcat. RFC 9562 §5.7 layout verified.
# ─────────────────────────────────────────────────────────────────────
VALID_V7_PRESENTATION_B = "019e100f-0c25-7c84-84e9-51ed3be48a72"  # $30.16
VALID_V7_PRESENTATION_C = "019e100f-567b-7cfb-a439-15a1800c0357"  # $22.45
VALID_V7_PRESENTATION_D = "019e100f-d0c8-7c81-89e1-01ff2eb23ffc"  # $18.63

# Wrong-version UUIDs for negative-path validation.
UUID_V4 = "550e8400-e29b-41d4-a716-446655440000"  # random — version 4
UUID_V1 = "c232ab00-9414-11ec-b3c8-9f6bdeced846"  # mac+timestamp — version 1


def _minimal_request_body(offer_id=VALID_V7_PRESENTATION_B):
    """Minimal request body matching APK 1.1.20 wire format.

    Includes only the fields parse_request reads and a few the route
    handler logs. offerId is the field under test; pass None to omit.
    """
    body = {
        "fare": 30.16,
        "tripMiles": 8.6,
        "tripMinutes": 20,
        "pickupMinutes": 5,
        "lat": 29.7099, "lng": -95.5455,
        "dropoffLat": 29.6284, "dropoffLng": -95.6509,
        "currentLat": 29.7100, "currentLng": -95.5400,
        "marketId": "6a35d28b-8e6c-4d60-94aa-2661e2650863",
        "isPuddleJumpMode": True,
        "pickupAddress": "Test pickup address",
        "dropoffAddress": "Test dropoff address",
    }
    if offer_id is not None:
        body["offerId"] = offer_id
    return body


# ═════════════════════════════════════════════════════════════════════
# Layer 1: parse_request direct tests (no Flask)
# ═════════════════════════════════════════════════════════════════════

def test_parse_request_extracts_valid_offer_id():
    """parse_request returns the raw offerId in params dict."""
    body = _minimal_request_body(offer_id=VALID_V7_PRESENTATION_B)
    params = parse_request(body, "test_driver")
    assert params["offer_id"] == VALID_V7_PRESENTATION_B


def test_parse_request_returns_none_when_offer_id_missing():
    """parse_request never raises; missing offerId yields None in params."""
    body = _minimal_request_body(offer_id=None)
    params = parse_request(body, "test_driver")
    assert params["offer_id"] is None


def test_parse_request_passes_through_invalid_offer_id_unchanged():
    """parse_request does not validate; route handler does. Garbage passes through."""
    body = _minimal_request_body(offer_id="not-a-uuid")
    params = parse_request(body, "test_driver")
    assert params["offer_id"] == "not-a-uuid"


# ═════════════════════════════════════════════════════════════════════
# Layer 2: Flask test_client tests for route handler validation
# ═════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="session")
def flask_app():
    """Session-scoped Flask app. Lazy-imports app.py once per pytest run.

    app.py is light-boot per its module docstring. If a future change
    introduces module-scope side effects that fail under test (DB
    connection, missing env var, etc.), patch at this layer with
    monkeypatch.setenv or similar BEFORE the import.
    """
    from app import app
    app.config["TESTING"] = True
    return app


# Test-harness bypass headers recognized by utils.verify_and_get_user_id
# at both the @app.before_request hook (app.py:89) and the route's own
# auth call. No Firebase RPC, no mocking.
BYPASS_HEADERS = {
    "X-Internal-Replay": "puddlejumper-replay-2026",
    "X-Driver-Id": "test_driver",
    "Content-Type": "application/json",
}


@pytest.fixture
def client(flask_app):
    """Function-scoped test client. Use _post_decision for bypass headers."""
    with flask_app.test_client() as c:
        yield c


def _post_decision(client, body):
    """POST to /api/v1/decisions/ with X-Internal-Replay bypass headers."""
    return client.post("/api/v1/decisions/", json=body, headers=BYPASS_HEADERS)


def test_route_returns_400_when_offer_id_missing(client):
    body = _minimal_request_body(offer_id=None)
    resp = _post_decision(client, body)
    assert resp.status_code == 400
    payload = resp.get_json()
    assert "offerId" in payload["error"]


def test_route_returns_400_when_offer_id_not_a_uuid(client):
    body = _minimal_request_body(offer_id="not-a-uuid")
    resp = _post_decision(client, body)
    assert resp.status_code == 400
    payload = resp.get_json()
    assert "valid UUID" in payload["error"]


def test_route_returns_400_when_offer_id_is_uuid_v4(client):
    """UUIDv4 parses cleanly but is the wrong version. Hard reject."""
    body = _minimal_request_body(offer_id=UUID_V4)
    resp = _post_decision(client, body)
    assert resp.status_code == 400
    payload = resp.get_json()
    assert "v7" in payload["error"]
    assert payload["got_version"] == 4


def test_route_returns_400_when_offer_id_is_uuid_v1(client):
    """UUIDv1 parses cleanly but is the wrong version. Hard reject."""
    body = _minimal_request_body(offer_id=UUID_V1)
    resp = _post_decision(client, body)
    assert resp.status_code == 400
    payload = resp.get_json()
    assert "v7" in payload["error"]
    assert payload["got_version"] == 1


def test_route_returns_400_when_offer_id_is_empty_string(client):
    """Empty string is not a valid UUID. uuid.UUID('') raises ValueError."""
    body = _minimal_request_body(offer_id="")
    resp = _post_decision(client, body)
    assert resp.status_code == 400
    payload = resp.get_json()
    # Empty string is caught by the missing-field branch (params.get
    # returns None for "" only if parse_request normalizes; otherwise
    # the unparseable branch fires). Either error message is acceptable
    # — both indicate rejection. The contract is "400 on bad offerId,"
    # not a specific error string for the empty-string case.
    assert "offerId" in payload["error"] or "UUID" in payload["error"]


# ═════════════════════════════════════════════════════════════════════
# Notes on coverage NOT included in Sprint 1
# ═════════════════════════════════════════════════════════════════════
#
# - Positive-path 200 (valid UUID accepted, persisted to trace_data):
#   would require mocking get_db, run_decision_engine, and log_decision.
#   That mock surface is large and brittle. Coverage instead lives in
#   the forward-compat verification gate — 5 real production drives
#   post-deploy, with the verification SQL from the brief checking
#   decision_log.trace_data->>'offer_id' against device logcat.
#
# - HTTP status code on the [OFFER_ID_REJECT] log path: tested above
#   (400 status code is asserted on every negative-path test). The log
#   message itself is not asserted — log assertion is brittle and the
#   business value is the rejection, not the log line.
#
# - Sprint 2 (canonical-column migration) will restructure this test
#   surface naturally — at that point offer_uuid is a typed column with
#   a UNIQUE constraint, and positive-path testing becomes a clean
#   schema-level assertion rather than a JSONB extract.
