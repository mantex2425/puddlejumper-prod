"""
Live-DB test infrastructure for PuddleJumper.

Provides pytest fixtures that connect to the production database (with
SAVEPOINT/ROLLBACK transactional isolation) for tests that exercise
real SQL semantics — tests that mocks cannot validate (e.g., the
LIVE_OFFER_PREDICATE_SQL time + distance horizon math, FK ON DELETE
behavior, real query planner decisions).

Until Sub-step B adds commit-suppression infrastructure, these fixtures
are usable only for tests where the function under test does NOT call
conn.commit(). Read-only and pre-commit-only tests are fine; tests
that exercise log_decision() will need additional fixture work in B.

Credential resolution
---------------------
DB_PASSWORD is read from os.environ first; if absent, fetched from
Google Secret Manager (projects/puddle-jumper-477316/secrets/
DB_PASSWORD/versions/latest). All other DB connection parameters
follow db.py's env-var convention with sensible defaults for the
puddle-jumper VM.

Sentinel discipline
-------------------
All test rows are tagged with a driver_id matching the pattern:

    TEST_GC_<YYYYMMDD_HHMMSS>_<uuid12>

The session-stamped prefix groups all rows from one pytest run for
production-log archaeology. The uuid12 suffix ensures per-test
uniqueness even within a single session.

Janitor
-------
Session-scoped autouse fixture. At session teardown, deletes any rows
matching the TEST_GC_ prefix in offer_history (first, due to FK NO
ACTION) then decision_log. Under normal operation this finds nothing —
ROLLBACK in db_cur teardown handles per-test cleanup. The janitor is
a safety net for tests that crash hard enough to skip teardown
(SIGKILL, OOM, segfault).

Tech debt
---------
Tests run against the production database. Acceptable while Andrew is
the sole driver. P1 for post-launch: dedicated test database/schema,
separate connection string, CI integration.
"""

from __future__ import annotations

import datetime
import os
import uuid

import psycopg2
import pytest
from psycopg2.extras import RealDictCursor


# =============================================================================
# Credential resolution
# =============================================================================

def _load_db_password() -> str:
    """Resolve DB_PASSWORD from env first, Secret Manager as fallback.

    Order:
      1. os.environ['DB_PASSWORD'] if set
      2. GCP Secret Manager
         (projects/puddle-jumper-477316/secrets/DB_PASSWORD/versions/latest)

    Raises RuntimeError with a clear message if neither path resolves.
    """
    pw = os.environ.get("DB_PASSWORD")
    if pw:
        return pw
    try:
        from google.cloud import secretmanager
    except ImportError as e:
        raise RuntimeError(
            "DB_PASSWORD not in env, and google-cloud-secret-manager "
            "is not installed. Either export DB_PASSWORD or "
            "pip install google-cloud-secret-manager."
        ) from e
    try:
        client = secretmanager.SecretManagerServiceClient()
        name = (
            "projects/puddle-jumper-477316/secrets/DB_PASSWORD/"
            "versions/latest"
        )
        response = client.access_secret_version(request={"name": name})
        return response.payload.data.decode("UTF-8")
    except Exception as e:
        raise RuntimeError(
            f"DB_PASSWORD not in env, and Secret Manager fetch failed: "
            f"{type(e).__name__}: {e}. Either export DB_PASSWORD or "
            f"verify gcloud auth application-default login + the IAM "
            f"role roles/secretmanager.secretAccessor."
        ) from e


def _load_db_credentials() -> dict:
    """Return kwargs suitable for psycopg2.connect(...)."""
    return {
        "host": os.environ.get("DB_HOST", "10.128.0.3"),
        "user": os.environ.get("DB_USER", "atjb"),
        "dbname": os.environ.get("DB_NAME", "puddlejumper"),
        "port": os.environ.get("DB_PORT", "5432"),
        "password": _load_db_password(),
        "cursor_factory": RealDictCursor,
        "connect_timeout": 10,
        "sslmode": "disable",
    }


# =============================================================================
# Connection fixture (session-scoped — one connection per pytest run)
# =============================================================================

@pytest.fixture(scope="session")
def pg_conn():
    """Session-scoped psycopg2 connection.

    Lifetime: opened at first request, closed at session teardown.
    autocommit=False (psycopg2 default). Tests use SAVEPOINT/ROLLBACK
    in the db_cur fixture; the outer transaction is discarded at
    session end.
    """
    conn = psycopg2.connect(**_load_db_credentials())
    yield conn
    try:
        conn.rollback()
    except psycopg2.Error:
        pass  # connection may already be closed/broken
    conn.close()


# =============================================================================
# Session sentinel — datestamped prefix for grouping test rows
# =============================================================================

@pytest.fixture(scope="session")
def test_session_prefix():
    """Datestamped prefix for all test driver_ids in this session."""
    return f"TEST_GC_{datetime.datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"


@pytest.fixture
def test_driver_id(test_session_prefix):
    """Unique sentinel driver_id, per test.

    Format: TEST_GC_YYYYMMDD_HHMMSS_<uuid12>
    Globally unique within a session. The session prefix groups all
    rows from one pytest run for production-log archaeology.
    """
    return f"{test_session_prefix}_{uuid.uuid4().hex[:12]}"


# =============================================================================
# Cursor fixture (function-scoped — SAVEPOINT/ROLLBACK per test)
# =============================================================================

@pytest.fixture
def db_cur(pg_conn):
    """Yield a cursor inside a SAVEPOINT. Roll back at teardown.

    Each test runs inside its own SAVEPOINT. On teardown, ROLLBACK TO
    SAVEPOINT discards everything the test did. Tests can INSERT,
    UPDATE, DELETE freely; nothing persists.

    LIMITATION (Sub-step A): if the function under test calls
    conn.commit(), the savepoint chain is destroyed and data persists
    permanently in production. Tests exercising such code paths must
    wait for Sub-step B's commit-suppression infrastructure.

    The first execute() on the cursor causes psycopg2 to issue an
    implicit BEGIN before our SAVEPOINT, so we don't need an explicit
    BEGIN. Subsequent tests in the same session re-create the savepoint
    (Postgres allows SAVEPOINT name to overwrite an existing savepoint
    of the same name within the same transaction).
    """
    cur = pg_conn.cursor()
    cur.execute("SAVEPOINT test_savepoint")
    try:
        yield cur
    finally:
        try:
            cur.execute("ROLLBACK TO SAVEPOINT test_savepoint")
        except psycopg2.Error:
            # Savepoint may have been destroyed by an unexpected commit
            # in the function under test. Fall through to a full
            # rollback; the janitor is the final cleanup line.
            try:
                pg_conn.rollback()
            except psycopg2.Error:
                pass
        cur.close()


# =============================================================================
# Seeding helpers — fixture-callables, take db_cur as dependency
# =============================================================================

@pytest.fixture
def seed_decision_log(db_cur):
    """Helper fixture returning a callable: seed_decision_log(driver_id)
    inserts a minimal decision_log row and returns its id.
    """
    def _seed(driver_id):
        db_cur.execute(
            """
            INSERT INTO app_private.decision_log
                (driver_id, decision_result)
            VALUES (%s, %s::jsonb)
            RETURNING id
            """,
            (driver_id, '{"verdict": "ACCEPT"}'),
        )
        return db_cur.fetchone()["id"]
    return _seed


@pytest.fixture
def seed_offer_history(db_cur):
    """Helper fixture returning a callable:
    seed_offer_history(decision_log_id, **overrides) inserts an
    offer_history row with sensible defaults and returns its id.

    Defaults match a typical Houston ride. Override any field via
    keyword args. Returns the row's id.

    Common overrides (for GC predicate tests):
        created_at:               tz-aware UTC datetime; drives time axis
        miles_at_offer_receipt:   float; drives distance axis
        actual_pickup_at,
        actual_dropoff_at:        tz-aware UTC datetime; anchor confirmations
        pickup_miles, trip_miles: numeric; horizon distance inputs
        pickup_minutes,
        trip_minutes:             smallint; horizon time inputs
    """
    def _seed(decision_log_id, **overrides):
        defaults = {
            "decision_log_id":        decision_log_id,
            "created_at":             datetime.datetime.now(datetime.timezone.utc),
            "pickup_miles":           2.5,
            "trip_miles":             8.0,
            "pickup_minutes":         8,
            "trip_minutes":           15,
            "miles_at_offer_receipt": 100.0,
            "actual_pickup_at":       None,
            "actual_dropoff_at":      None,
        }
        defaults.update(overrides)

        cols = list(defaults.keys())
        vals = [defaults[k] for k in cols]
        placeholders = ", ".join(["%s"] * len(cols))
        col_list = ", ".join(cols)
        db_cur.execute(
            f"""
            INSERT INTO app_private.offer_history ({col_list})
            VALUES ({placeholders})
            RETURNING id
            """,
            vals,
        )
        return db_cur.fetchone()["id"]
    return _seed


# =============================================================================
# Janitor — session-scoped autouse safety net
# =============================================================================

@pytest.fixture(scope="session", autouse=True)
def _janitor():
    """Session-end cleanup of any TEST_GC_ rows that escaped rollback.

    Under normal operation finds nothing — ROLLBACK in db_cur handles
    per-test cleanup, and pg_conn's session-end rollback discards the
    outer transaction. The janitor catches edge cases where a test
    crashed hard enough to skip teardown (segfault, OOM, kill -9).

    Delete order respects the FK constraint
    offer_history.decision_log_id → decision_log.id (NO ACTION):
    offer_history first, then decision_log.

    The janitor opens its own short-lived connection to avoid
    contamination from any in-flight transaction state on pg_conn.
    """
    yield  # run all tests first

    try:
        conn = psycopg2.connect(**_load_db_credentials())
    except Exception as e:
        # Session never opened a real connection (e.g., all tests were
        # skipped or DB credentials unresolvable). Nothing to clean.
        print(f"[janitor] connect failed at teardown: {e}")
        return

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM app_private.offer_history
                WHERE decision_log_id IN (
                    SELECT id FROM app_private.decision_log
                    WHERE driver_id LIKE 'TEST_GC_%%'
                )
                """
            )
            n_oh = cur.rowcount
            cur.execute(
                """
                DELETE FROM app_private.decision_log
                WHERE driver_id LIKE 'TEST_GC_%%'
                """
            )
            n_dl = cur.rowcount
        conn.commit()
        if n_oh or n_dl:
            print(
                f"[janitor] cleaned {n_oh} offer_history + {n_dl} "
                f"decision_log rows (rollback escaped)"
            )
    finally:
        conn.close()
