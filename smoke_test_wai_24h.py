#!/usr/bin/env python3
"""
smoke_test_wai_24h.py — read-only smoke test of WAI's matching against
24 hours of historical heartbeats.

PURPOSE
=======
Validates Assumption A1 from SIMPLIFIED_ARCHITECTURE.md §10:
  "WAI matching reliability >= 90% in production"

For each offer in the past 24 hours, replays the recorded heartbeats through
WAI in offline mode and records what WAI returns. Output is a truth table:
how many heartbeats produced at_current_pudo / at_unknown_pudo / not_at_pudo
/ exception, broken down by offer.

DESIGN
======
- Read-only. No writes to any table.
- Uses WAI's dependency-injection seam (_cluster_fn, _recent_clusters_fn)
  to inject the historical cluster data instead of querying live state.
- Wraps each WAI call in try/except per Option C — captures Bug 2
  (`'dict' object has no attribute 'lower'`) and any other latent crashes
  WITHOUT halting the test.
- Sample heartbeats: every Nth row per offer (default N=20) to keep
  runtime manageable. Adjust SAMPLE_EVERY_N if needed.

OUTPUT
======
Per-offer rows with: offer_id, address (pickup/dropoff), heartbeats_total,
heartbeats_sampled, matches, no_matches, crashes, top_exception.

Plus a global summary: total sampled, % matched, % crashed, top crash type.

USAGE
=====
  cd ~/puddlejumper-prod && venv/bin/python tmp/smoke_test_wai_24h.py

PRECONDITIONS
=============
- Run from ~/puddlejumper-prod (so where_am_i, pudo_types, etc. import)
- Run inside the venv (psycopg2, etc. installed there)
- DB credentials from environment ($PGPASSWORD, etc.)
"""
import os
import sys
import traceback
from collections import Counter, defaultdict
from datetime import datetime, timezone

# Sample every Nth heartbeat per offer to keep runtime bounded.
# 5,122 heartbeats / 20 = ~250 WAI calls. Each call ~50-200ms (DB-bound) ->
# ~30-60 seconds total runtime.
SAMPLE_EVERY_N = 20

# Connection details — match production tooling
PG_HOST = "10.128.0.2"
PG_USER = "postgres"
PG_DB = "puddlejumper"

# Driver in question — single-driver dev environment.
# If multi-driver in future, parameterize this.
DRIVER_ID = "UjT1hE9eBXh2q95aSZYOkzDJ8lo1"


def main() -> int:
    # Imports inside main so import errors are reported clearly with
    # context rather than at module-load.
    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor
    except ImportError as e:
        sys.exit(f"FATAL: psycopg2 not available. Run inside venv: {e}")

    try:
        from where_am_i import WhereAmI
        from pudo_types import Offer, TargetSpec
        from cluster_detection import Cluster
        from bead_on_wire import classify_address
    except ImportError as e:
        sys.exit(f"FATAL: import failed. Run from ~/puddlejumper-prod: {e}")

    pgpass = os.environ.get("PGPASSWORD")
    if not pgpass:
        sys.exit("FATAL: PGPASSWORD not set in environment.")

    print("=" * 78)
    print("WAI 24-hour smoke test")
    print("=" * 78)
    print(f"  Sample rate: every {SAMPLE_EVERY_N}th heartbeat per offer")
    print(f"  Driver: {DRIVER_ID}")
    print()

    conn = psycopg2.connect(
        host=PG_HOST,
        user=PG_USER,
        password=pgpass,
        dbname=PG_DB,
        cursor_factory=RealDictCursor,
    )
    conn.autocommit = True
    cur = conn.cursor()

    # ------------------------------------------------------------------
    # Step 1: Pull all distinct offers from the past 24 hours
    # ------------------------------------------------------------------
    cur.execute("""
        SELECT DISTINCT
            pdc.current_offer_id::int                AS offer_id,
            oh.pickup_address,
            oh.dropoff_address,
            dl.pickup_lat,
            dl.pickup_lng,
            dl.dropoff_lat,
            dl.dropoff_lng,
            MIN(pdc.created_at)                       AS first_heartbeat,
            MAX(pdc.created_at)                       AS last_heartbeat,
            COUNT(*)                                  AS heartbeat_count
        FROM app_private.pudo_decision_context pdc
        LEFT JOIN app_private.decision_log dl
               ON dl.id = pdc.current_offer_id::int
        LEFT JOIN app_private.offer_history oh
               ON oh.decision_log_id = dl.id
        WHERE pdc.created_at > NOW() - INTERVAL '24 hours'
          AND pdc.current_offer_id IS NOT NULL
        GROUP BY pdc.current_offer_id, oh.pickup_address, oh.dropoff_address,
                 dl.pickup_lat, dl.pickup_lng, dl.dropoff_lat, dl.dropoff_lng
        ORDER BY first_heartbeat
    """)
    offers = cur.fetchall()
    print(f"Found {len(offers)} distinct offers in past 24 hours.")
    print()

    # ------------------------------------------------------------------
    # Step 2: For each offer, sample heartbeats and replay through WAI
    # ------------------------------------------------------------------
    per_offer_results = []
    global_status_counter = Counter()
    global_exception_counter = Counter()

    for offer_row in offers:
        offer_id = offer_row["offer_id"]
        pickup_addr = offer_row["pickup_address"] or "(missing)"
        dropoff_addr = offer_row["dropoff_address"] or "(missing)"
        total_hb = offer_row["heartbeat_count"]
        first_hb = offer_row["first_heartbeat"]

        # Skip offers with missing address or coords — cannot build Offer
        if (offer_row["pickup_lat"] is None or offer_row["pickup_lng"] is None
            or offer_row["dropoff_lat"] is None or offer_row["dropoff_lng"] is None
            or not pickup_addr or pickup_addr == "(missing)"
            or not dropoff_addr or dropoff_addr == "(missing)"):
            print(f"[skip] Offer {offer_id}: missing pickup/dropoff data")
            per_offer_results.append({
                "offer_id": offer_id,
                "pickup": pickup_addr[:40],
                "dropoff": dropoff_addr[:40],
                "total_hb": total_hb,
                "sampled": 0,
                "matched": 0,
                "no_match": 0,
                "crashed": 0,
                "skipped": True,
                "top_exception": None,
                "match_addresses": Counter(),
            })
            continue

        # Build Offer object using same logic as _bucket_to_target_spec
        pickup_spec = _build_target_spec(
            classify_address, pickup_addr,
            offer_row["pickup_lat"], offer_row["pickup_lng"]
        )
        dropoff_spec = _build_target_spec(
            classify_address, dropoff_addr,
            offer_row["dropoff_lat"], offer_row["dropoff_lng"]
        )

        if pickup_spec is None or dropoff_spec is None:
            print(f"[skip] Offer {offer_id}: classify_address returned garbage for pickup or dropoff")
            per_offer_results.append({
                "offer_id": offer_id,
                "pickup": pickup_addr[:40],
                "dropoff": dropoff_addr[:40],
                "total_hb": total_hb,
                "sampled": 0,
                "matched": 0,
                "no_match": 0,
                "crashed": 0,
                "skipped": True,
                "top_exception": "garbage_classification",
                "match_addresses": Counter(),
            })
            continue

        offer = Offer(
            offer_id=str(offer_id),
            accepted_at=first_hb,
            pickup=pickup_spec,
            dropoff=dropoff_spec,
            secondary_dropoff=None,
        )

        # Sample heartbeats for this offer with cluster data
        cur.execute("""
            SELECT id, created_at, state_at_eval,
                   cluster_lat, cluster_lng, cluster_size, cluster_duration_s,
                   wai_status AS recorded_status, wai_reason AS recorded_reason
            FROM app_private.pudo_decision_context
            WHERE current_offer_id = %s
              AND created_at > NOW() - INTERVAL '24 hours'
              AND cluster_lat IS NOT NULL
              AND cluster_lng IS NOT NULL
            ORDER BY id
        """, (str(offer_id),))
        all_heartbeats = cur.fetchall()

        # Sample every Nth (always include first and last)
        if not all_heartbeats:
            sampled_heartbeats = []
        elif len(all_heartbeats) <= 3:
            sampled_heartbeats = all_heartbeats
        else:
            sampled_heartbeats = all_heartbeats[::SAMPLE_EVERY_N]
            # Ensure the last one is included
            if sampled_heartbeats[-1] != all_heartbeats[-1]:
                sampled_heartbeats.append(all_heartbeats[-1])

        # Reset per-offer counters
        matched = 0
        no_match = 0
        crashed = 0
        per_offer_exceptions = Counter()
        match_addresses = Counter()

        for hb in sampled_heartbeats:
            # Build a fake _cluster_fn that returns the historical cluster
            cluster = Cluster(
                n=hb["cluster_size"] or 0,
                median_lat=hb["cluster_lat"],
                median_lng=hb["cluster_lng"],
                spread_m=0.0,  # not stored; use 0 as benign default
                duration_s=hb["cluster_duration_s"] or 0.0,
                latest=hb["created_at"],
            )

            def fake_cluster_fn(_driver_id, _cur, _cluster=cluster):
                return _cluster

            def fake_recent_clusters_fn(_driver_id, _cur, _accepted_at, **kwargs):
                # Return empty list — historical revisit data not reconstructed
                # for this smoke test. This means cluster_revisit topology
                # signal is always False, which is acceptable for A1 validation
                # (the cluster_revisit signal is a refinement, not the primary
                # match signal).
                return []

            # Build a fresh WAI instance per heartbeat. WAI is stateless
            # post-construction, but each call needs a clean cur.
            wai = WhereAmI(
                cur,
                _cluster_fn=fake_cluster_fn,
                _recent_clusters_fn=fake_recent_clusters_fn,
            )

            try:
                result = wai.evaluate(
                    DRIVER_ID,
                    offer,
                    hb["state_at_eval"],
                )
                status = result.status
                global_status_counter[status] += 1
                if status in ("at_current_pudo", "at_previous_pudo"):
                    matched += 1
                    if result.target_address:
                        match_addresses[result.target_address] += 1
                else:
                    no_match += 1
            except Exception as exc:  # noqa: BLE001 — broad-except is intentional
                crashed += 1
                exc_key = f"{type(exc).__name__}: {str(exc)[:80]}"
                per_offer_exceptions[exc_key] += 1
                global_exception_counter[exc_key] += 1

        # Top exception for the per-offer summary
        top_exc = per_offer_exceptions.most_common(1)
        top_exc_str = top_exc[0][0] if top_exc else None

        per_offer_results.append({
            "offer_id": offer_id,
            "pickup": pickup_addr[:40],
            "dropoff": dropoff_addr[:40],
            "total_hb": total_hb,
            "sampled": len(sampled_heartbeats),
            "matched": matched,
            "no_match": no_match,
            "crashed": crashed,
            "skipped": False,
            "top_exception": top_exc_str,
            "match_addresses": match_addresses,
        })

        match_pct = (matched / len(sampled_heartbeats) * 100) if sampled_heartbeats else 0
        crash_pct = (crashed / len(sampled_heartbeats) * 100) if sampled_heartbeats else 0
        print(f"  Offer {offer_id}: {len(sampled_heartbeats):3d} sampled / {total_hb:4d} total | "
              f"matched={matched:3d} ({match_pct:5.1f}%) | "
              f"no_match={no_match:3d} | crashed={crashed:3d} ({crash_pct:5.1f}%) | "
              f"pickup={pickup_addr[:30]}")

    # ------------------------------------------------------------------
    # Step 3: Print summary
    # ------------------------------------------------------------------
    print()
    print("=" * 78)
    print("SUMMARY")
    print("=" * 78)

    total_sampled = sum(r["sampled"] for r in per_offer_results)
    total_matched = sum(r["matched"] for r in per_offer_results)
    total_no_match = sum(r["no_match"] for r in per_offer_results)
    total_crashed = sum(r["crashed"] for r in per_offer_results)
    skipped_offers = sum(1 for r in per_offer_results if r["skipped"])

    print(f"Offers in scope:    {len(offers)}")
    print(f"Offers skipped:     {skipped_offers}  (missing data or garbage classification)")
    print(f"Heartbeats sampled: {total_sampled}")
    print()
    if total_sampled:
        print(f"  matched:  {total_matched:5d}  ({total_matched/total_sampled*100:5.1f}%)")
        print(f"  no_match: {total_no_match:5d}  ({total_no_match/total_sampled*100:5.1f}%)")
        print(f"  crashed:  {total_crashed:5d}  ({total_crashed/total_sampled*100:5.1f}%)")
    print()
    print("Status distribution (successful WAI calls):")
    for status, count in global_status_counter.most_common():
        print(f"  {status:25s} {count:5d}")
    print()
    if global_exception_counter:
        print("Top exceptions encountered:")
        for exc, count in global_exception_counter.most_common(10):
            print(f"  {count:5d}  {exc}")
    print()
    print("A1 verdict (>=90% match rate threshold):")
    if total_sampled:
        match_rate = total_matched / total_sampled * 100
        if match_rate >= 90:
            print(f"  PASS — match rate {match_rate:.1f}% >= 90%")
        else:
            print(f"  FAIL — match rate {match_rate:.1f}% < 90%")
            print(f"  Need to either fix WAI bugs or relax A1 threshold.")
    print()
    print("Done.")
    return 0


def _build_target_spec(classify_address, address_text, lat, lng):
    """Mirror of driver_heartbeat._bucket_to_target_spec, inlined for clarity.

    Returns TargetSpec or None.
    """
    from pudo_types import TargetSpec
    if not address_text or lat is None or lng is None:
        return None
    classification = classify_address(address_text)
    bucket = classification.get("bucket")
    parts = classification.get("parts", {})

    if bucket == "intersection":
        return TargetSpec(
            lat=float(lat), lng=float(lng),
            address_class="intersection",
            named_roads=(parts.get("road_a", ""), parts.get("road_b", "")),
        )
    if bucket == "street_number":
        return TargetSpec(
            lat=float(lat), lng=float(lng),
            address_class="number_on_street",
            named_roads=(parts.get("road", ""),),
        )
    if bucket == "single_road":
        return TargetSpec(
            lat=float(lat), lng=float(lng),
            address_class="single_road",
            named_roads=(parts.get("road", ""),),
        )
    if bucket == "poi":
        return TargetSpec(
            lat=float(lat), lng=float(lng),
            address_class="poi",
            named_roads=(),
        )
    return None


if __name__ == "__main__":
    sys.exit(main())