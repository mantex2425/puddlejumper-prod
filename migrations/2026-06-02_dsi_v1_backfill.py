#!/usr/bin/env python3
"""DSI v1 historical backfill (STEP 2 of the DSI build).

Computes dsi_v1 for EXISTING rows in app_private.offer_history and
public.community_offers, using dsi.compute_dsi_v1() — the SAME helper the live
ingest writer uses. No inline formula (anti-divergence: HARD RULE 1).

Guarantees:
  - NULL-strict: only touches rows with BOTH effective_hourly_rate AND
    dollars_per_mile present; rows missing either stay NULL (HARD RULE 2).
  - Idempotent: WHERE dsi_v1 IS NULL on both SELECT and UPDATE; a second run
    is a no-op (HARD RULE 3).
  - One transaction overall, row-level try/except -> rollback the ENTIRE
    backfill on any error; commit only if fully clean (HARD RULE 4).
  - Batched writes (executemany, page=BATCH) INSIDE the single rollback
    boundary — batching is for speed, never partial-commit.

Connects as postgres via ~/.pgpass (the DDL/DML owner; app role atjb lacks it).

Usage:
  2026-06-02_dsi_v1_backfill.py dryrun   (default) compute + count, then ROLLBACK
  2026-06-02_dsi_v1_backfill.py parity   show 5 rows: inputs -> computed (eyeball)
  2026-06-02_dsi_v1_backfill.py commit   compute + write, COMMIT if clean
"""
import sys
import os

# Make dsi.py (repo/worktree root, one level up from migrations/) importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg2
from psycopg2.extras import RealDictCursor
from dsi import compute_dsi_v1, IRS_RATE_PER_MILE, DSI_MILE_WEIGHT

MODE = sys.argv[1] if len(sys.argv) > 1 else "dryrun"
assert MODE in ("dryrun", "parity", "commit"), f"bad mode {MODE!r}"
BATCH = 100

TABLES = [
    ("app_private.offer_history", "id"),
    ("public.community_offers", "id"),
]
SELECT_SQL = """
    SELECT {pk} AS id,
           effective_hourly_rate AS hr,
           dollars_per_mile      AS dpm
    FROM {tbl}
    WHERE dsi_v1 IS NULL
      AND effective_hourly_rate IS NOT NULL
      AND dollars_per_mile      IS NOT NULL
"""


def _conn():
    return psycopg2.connect(
        host="10.128.0.2", dbname="puddlejumper", user="postgres",
        port="5432", cursor_factory=RealDictCursor,
    )


def parity():
    """STEP 4: show 5 rows, compute_dsi_v1() vs hand-applied formula."""
    conn = _conn(); cur = conn.cursor()
    print(f"IRS_RATE_PER_MILE={IRS_RATE_PER_MILE}  DSI_MILE_WEIGHT={DSI_MILE_WEIGHT}")
    for tbl, pk in TABLES[:1]:  # offer_history sample is sufficient to eyeball
        cur.execute(SELECT_SQL.format(pk=pk, tbl=tbl) + " LIMIT 5")
        for r in cur.fetchall():
            hr, dpm = float(r["hr"]), float(r["dpm"])
            via_helper = compute_dsi_v1(hr, dpm)
            hand = hr + DSI_MILE_WEIGHT * (dpm - IRS_RATE_PER_MILE)
            match = "OK" if via_helper == hand else "MISMATCH!"
            print(f"  id={r['id']}  hr={hr}  dpm={dpm}  "
                  f"compute_dsi_v1={via_helper:.6f}  hand={hand:.6f}  [{match}]")
            if via_helper != hand:
                conn.close(); sys.exit("STEP 4 PARITY FAILED")
    conn.close()


def run(commit):
    conn = _conn(); cur = conn.cursor()
    results = {}
    try:
        for tbl, pk in TABLES:
            cur.execute(SELECT_SQL.format(pk=pk, tbl=tbl))
            rows = cur.fetchall()
            pairs = []
            for r in rows:
                val = compute_dsi_v1(float(r["hr"]), float(r["dpm"]))
                if val is None:
                    # defensive: WHERE filter guarantees both inputs present,
                    # so this is unreachable. Skip rather than write a partial.
                    continue
                pairs.append((val, r["id"]))
            upd = f"UPDATE {tbl} SET dsi_v1 = %s WHERE {pk} = %s AND dsi_v1 IS NULL"
            updated = 0
            for i in range(0, len(pairs), BATCH):
                cur.executemany(upd, pairs[i:i + BATCH])
                if cur.rowcount is not None and cur.rowcount >= 0:
                    updated += cur.rowcount
            results[tbl] = {"selected": len(rows), "computed": len(pairs), "updated": updated}
        if commit:
            conn.commit()
            print("BACKFILL COMMITTED.")
        else:
            conn.rollback()
            print("DRY-RUN (rolled back; nothing persisted).")
        for tbl, c in results.items():
            print(f"  {tbl}: selected={c['selected']} computed={c['computed']} updated_rowcount={c['updated']}")
    except Exception as e:
        conn.rollback()
        print("BACKFILL FAILED -- ENTIRE TXN ROLLED BACK:", type(e).__name__, str(e)[:300])
        conn.close()
        sys.exit(1)
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    if MODE == "parity":
        parity()
    else:
        run(commit=(MODE == "commit"))
