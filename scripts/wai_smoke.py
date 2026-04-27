#!/usr/bin/env python3
"""
Phase D Step 5.7 — WAI functional smoke against live PG.

Operational verification that WhereAmI.evaluate() works end-to-end against
the real Postgres instance and the production-shaped get_pivot_context /
detect_cluster outputs.

NOT a pytest test (depends on live PG; brittle in CI). Run manually on the
VM after deploys to confirm WAI's read path is healthy.

Three smoke runs:
  1. bare-evaluate    — synthetic Offer at canonical intersection, ENROUTE
  2. ghost-reach      — UNCOMMITTED, no offer; exercises ghost-cache SELECT
  3. null-coords      — S27 case (NULL target.lat); proves fail-closed

For each: status, confidence, reason, wall-clock time. Pure DIAGNOSE per
Q12 — no writes anywhere in WAI's path.

Usage:
  python3 scripts/wai_smoke.py
  python3 scripts/wai_smoke.py --driver-id <uid>
  python3 scripts/wai_smoke.py --pickup-lat 29.6246 --pickup-lng -95.5102
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
import traceback
from pathlib import Path

# Self-bootstrap: this script lives in scripts/, but where_am_i.py and
# pudo_types.py live at the project root. Insert the root into sys.path
# so the imports below resolve regardless of CWD when invoked.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import psycopg2
from psycopg2.extras import RealDictCursor

from where_am_i import WhereAmI
from pudo_types import Offer, States, TargetSpec
from cluster_detection import Cluster
from datetime import datetime, timezone


# Synthetic anchor for Offer construction (v2.6 amendment, sub-step 1b.1).
# wai_smoke is a smoke script, not a temporal-logic test; this dummy value
# satisfies Offer's now-required accepted_at field without affecting the
# script's read-only diagnostic intent.
DUMMY_ACCEPTED_AT = datetime(2026, 4, 27, 8, 0, tzinfo=timezone.utc)


# Defaults pulled from canonical context (memories + S31 fixture).
DEFAULT_DRIVER = "UjT1hE9eBXh2q95aSZYOkzDJ8lo1"
DEFAULT_PICKUP_LAT = 29.6246      # Forum Park intersection
DEFAULT_PICKUP_LNG = -95.5102
DEFAULT_DROPOFF_LAT = 29.7000     # Placeholder
DEFAULT_DROPOFF_LNG = -95.4000

DEFAULT_DB_HOST = "10.128.0.2"
DEFAULT_DB_NAME = "puddlejumper"
DEFAULT_DB_USER = "postgres"


def _format_result(result, elapsed_ms: float) -> str:
    """One-liner summary of a WhereAmIResult for human eyes."""
    return (
        f"  status={result.status} "
        f"pudo_type={result.pudo_type} "
        f"conf={result.confidence:.3f} "
        f"on_target_road={result.on_target_road} "
        f"on_wire={result.on_wire} "
        f"current_road={result.current_road!r} "
        f"off_wire_s={result.off_wire_duration_s} "
        f"elapsed={elapsed_ms:.1f}ms\n"
        f"  reason: {result.reason}"
    )


def _build_default_offer(args) -> Offer:
    """Synthetic Offer with intersection-class pickup at the canonical
    Forum Park geocoded location (or wherever args override to).
    """
    pickup = TargetSpec(
        lat=args.pickup_lat,
        lng=args.pickup_lng,
        address_class="intersection",
        named_roads=("Settemont Rd", "Joan St"),
    )
    dropoff = TargetSpec(
        lat=args.dropoff_lat,
        lng=args.dropoff_lng,
        address_class="number_on_street",
        named_roads=("Placeholder St",),
    )
    return Offer(
        offer_id="smoke_test_offer",
        accepted_at=DUMMY_ACCEPTED_AT,
        pickup=pickup,
        dropoff=dropoff,
    )


def _smoke_1_bare_evaluate(wai, args) -> bool:
    """Smoke 1: bare evaluate() with synthetic ENROUTE offer."""
    print("--- Smoke 1: bare evaluate() (state=ENROUTE) ---")
    offer = _build_default_offer(args)
    try:
        t0 = time.perf_counter()
        result = wai.evaluate(args.driver_id, offer, States.ENROUTE)
        elapsed = (time.perf_counter() - t0) * 1000
        print(_format_result(result, elapsed))
        print("  PASS (no exception)")
        return True
    except Exception as e:
        print(f"  FAIL: {type(e).__name__}: {e}")
        traceback.print_exc()
        return False


def _smoke_2_ghost_reach(wai, args) -> bool:
    """Smoke 2: UNCOMMITTED + no offer → ghost-cache SELECT runs."""
    print("\n--- Smoke 2: ghost-cache reachability (state=UNCOMMITTED) ---")
    try:
        t0 = time.perf_counter()
        result = wai.evaluate(args.driver_id, None, States.UNCOMMITTED)
        elapsed = (time.perf_counter() - t0) * 1000
        print(_format_result(result, elapsed))
        # Any of the three statuses is acceptable here — what matters is
        # the SELECT didn't raise. status will be one of:
        #   not_at_pudo       (no cluster — most common in idle-driver state)
        #   at_unknown_pudo   (cluster, no ghost match)
        #   at_previous_pudo  (cluster + ghost match)
        if result.status not in (
            "not_at_pudo", "at_unknown_pudo", "at_previous_pudo",
        ):
            print(f"  FAIL: unexpected status={result.status!r}")
            return False
        print("  PASS (SELECT executed without error)")
        return True
    except Exception as e:
        print(f"  FAIL: {type(e).__name__}: {e}")
        traceback.print_exc()
        return False


def _smoke_3_null_coords(wai, args) -> bool:
    """Smoke 3: S27 case — TargetSpec with NULL lat → fail-closed."""
    print("\n--- Smoke 3: NULL target.lat fail-closed (S27) ---")
    bad_pickup = TargetSpec(
        lat=None,
        lng=args.pickup_lng,
        address_class="intersection",
        named_roads=("Bad Geocode Rd",),
    )
    dropoff = TargetSpec(
        lat=args.dropoff_lat,
        lng=args.dropoff_lng,
        address_class="number_on_street",
        named_roads=("Placeholder St",),
    )
    bad_offer = Offer(
        offer_id="smoke_test_null",
        accepted_at=DUMMY_ACCEPTED_AT,
        pickup=bad_pickup,
        dropoff=dropoff,
    )
    try:
        t0 = time.perf_counter()
        result = wai.evaluate(args.driver_id, bad_offer, States.ENROUTE)
        elapsed = (time.perf_counter() - t0) * 1000
        print(_format_result(result, elapsed))
        # No specific status assertion — what matters is that NULL lat
        # didn't crash on math.radians(None) inside _haversine. If we
        # got a result at all, _validate_target did its job.
        print("  PASS (NULL coords didn't crash)")
        return True
    except TypeError as e:
        # The specific failure mode we're guarding against is
        # math.radians(None) raising TypeError — fail loudly if so.
        print(f"  FAIL: NULL coords reached the math layer — _validate_target gap")
        print(f"  Exception: {e}")
        traceback.print_exc()
        return False
    except Exception as e:
        print(f"  FAIL: {type(e).__name__}: {e}")
        traceback.print_exc()
        return False


def _smoke_4_injected_cluster(args, conn) -> bool:
    """Smoke 4: inject a synthetic cluster, exercise topology + ghost cache.

    The first three smokes only verify "no crash" — but if the driver has
    no recent heartbeats, detect_cluster returns None and evaluate() exits
    at the cluster check, never reaching _compute_road_topology or
    _match_ghost_cache. This smoke forces those paths to execute against
    the real PG cursor by injecting a known-good Cluster via the
    _cluster_fn seam (the same seam unit tests use).

    Catches:
      - pivot_context.get_pivot_context() returning a dict shape that
        _compute_road_topology doesn't expect
      - _GHOST_CACHE_SQL referencing a column name that doesn't exist
        in the real Phase A schema
      - timezone arithmetic crashes when pivot_time is a real PG
        timestamptz value (rather than the test fixtures' fake datetime)
    """
    print("\n--- Smoke 4: injected cluster (exercises topology + ghost cache) ---")

    synthetic_cluster = Cluster(
        n=4,
        median_lat=args.pickup_lat,
        median_lng=args.pickup_lng,
        spread_m=15.0,
        duration_s=30.0,
        latest=datetime(2026, 4, 23, 20, 52, 16, tzinfo=timezone.utc),
    )

    def fake_cluster_fn(driver_id, cur):
        return synthetic_cluster

    # Fresh cursor for this run — the parent cursor was used by smokes 1-3
    cur = conn.cursor(cursor_factory=RealDictCursor)
    wai = WhereAmI(cur, _cluster_fn=fake_cluster_fn)

    offer = _build_default_offer(args)
    try:
        t0 = time.perf_counter()
        result = wai.evaluate(args.driver_id, offer, States.ENROUTE)
        elapsed = (time.perf_counter() - t0) * 1000
        print(_format_result(result, elapsed))
        # Outcome can be either at_current_pudo (matched our synthetic
        # offer at the cluster), at_unknown_pudo (cluster but offer didn't
        # match), or at_previous_pudo (cluster + ghost row in PG). All
        # three are real successful executions of the deeper paths.
        valid_statuses = {"at_current_pudo", "at_unknown_pudo", "at_previous_pudo"}
        if result.status not in valid_statuses:
            print(f"  FAIL: unexpected status={result.status!r}")
            return False
        print(f"  PASS (deep path executed: status={result.status})")
        return True
    except Exception as e:
        print(f"  FAIL: {type(e).__name__}: {e}")
        traceback.print_exc()
        return False


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--driver-id", default=DEFAULT_DRIVER,
                   help=f"Driver ID to query (default: {DEFAULT_DRIVER[:12]}...)")
    p.add_argument("--pickup-lat", type=float, default=DEFAULT_PICKUP_LAT,
                   help=f"Pickup latitude (default: {DEFAULT_PICKUP_LAT})")
    p.add_argument("--pickup-lng", type=float, default=DEFAULT_PICKUP_LNG,
                   help=f"Pickup longitude (default: {DEFAULT_PICKUP_LNG})")
    p.add_argument("--dropoff-lat", type=float, default=DEFAULT_DROPOFF_LAT,
                   help=f"Dropoff latitude (default: {DEFAULT_DROPOFF_LAT})")
    p.add_argument("--dropoff-lng", type=float, default=DEFAULT_DROPOFF_LNG,
                   help=f"Dropoff longitude (default: {DEFAULT_DROPOFF_LNG})")
    p.add_argument("--db-host", default=DEFAULT_DB_HOST,
                   help=f"PG host (default: {DEFAULT_DB_HOST})")
    p.add_argument("--db-name", default=DEFAULT_DB_NAME,
                   help=f"PG database (default: {DEFAULT_DB_NAME})")
    p.add_argument("--db-user", default=DEFAULT_DB_USER,
                   help=f"PG user (default: {DEFAULT_DB_USER})")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="Enable DEBUG logging from where_am_i module")
    args = p.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)

    print("=" * 70)
    print(f"WAI live-PG smoke — driver_id={args.driver_id}")
    print(f"  pickup=({args.pickup_lat}, {args.pickup_lng})")
    print(f"  PG: {args.db_user}@{args.db_host}/{args.db_name}")
    print("=" * 70)

    conn_str = f"host={args.db_host} dbname={args.db_name} user={args.db_user}"

    # Production convention (audit per Step 5.7.1): every WAI dependency
    # caller uses psycopg2.extras.RealDictCursor. Smoke must match or it
    # exercises a different code path than production. cluster_detection
    # accesses row["n"] etc.; tuple cursor would crash inside the cluster
    # detector, not WAI itself.
    conn = None
    try:
        conn = psycopg2.connect(conn_str)
        cur = conn.cursor(cursor_factory=RealDictCursor)
        wai = WhereAmI(cur)

        results = []
        results.append(_smoke_1_bare_evaluate(wai, args))
        results.append(_smoke_2_ghost_reach(wai, args))
        results.append(_smoke_3_null_coords(wai, args))
        results.append(_smoke_4_injected_cluster(args, conn))
    except psycopg2.OperationalError as e:
        print(f"\nFATAL: could not connect to PG: {e}")
        return 2
    finally:
        if conn is not None:
            conn.close()

    print("\n" + "=" * 70)
    passed = sum(results)
    total = len(results)
    if passed == total:
        print(f"ALL {total} SMOKE RUNS PASSED")
        return 0
    else:
        print(f"FAILED: {passed}/{total} smoke runs passed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
