#!/usr/bin/env python3
"""
apply_substep_1b1.py — Phase E Step 6 sub-step 1b.1 contract change.

The "heart transplant": atomic migration of two frozen dataclasses (Offer,
WhereAmIResult) plus all 9 production/test construction sites that build
them.

Architecture per the v2.6 amendment to WHERE_AM_I_PROPOSAL_v2.md
(commit aa8e667). Plumbing decision Option B ratified by Gemini 2026-04-27.

What this ships:
  pudo_types.py:
    - new import: from datetime import datetime
    - Offer: + accepted_at: datetime field (no default)
    - WhereAmIResult: + cluster_revisit: bool field (no default)

  where_am_i.py:
    - 4 WhereAmIResult(...) constructions get cluster_revisit=False
      (placeholder; sub-step 1b.2 wires the real computation)

  tests/test_pudo_planner.py:
    - 1 WhereAmIResult(...) construction gets cluster_revisit=False

  tests/test_scenarios.py:
    - 1 Offer(...) construction at the S31 forensic replay site gets
      accepted_at=datetime(2026, 4, 23, 20, 45, 0, tzinfo=timezone.utc)
      (literal, 7-min ENROUTE buffer before earliest 7623 heartbeat)
    - "Forensic Source" comment per Gemini 2026-04-27 follow-up

  tests/test_where_am_i.py:
    - new module-level constant DUMMY_ACCEPTED_AT (uses _dt/_tz aliases)
    - 1 Offer(...) factory construction gets accepted_at=DUMMY_ACCEPTED_AT

  scripts/wai_smoke.py:
    - new module-level constant DUMMY_ACCEPTED_AT
    - 2 Offer(...) constructions get accepted_at=DUMMY_ACCEPTED_AT

Total: 6 files modified, 9 construction sites patched, 2 new module
constants, 2 new dataclass fields, 1 new import.

Atomicity: all transformations applied in-memory and verified before any
file is written. Failure of any pre-condition or post-condition aborts
the entire patch with no disk writes.

Anchor strategy per L-3: each transformation has a multi-line anchor with
sufficient context to guarantee uniqueness. Idempotency guard: re-running
after success fails on the v2.6 marker check (cluster_revisit field
already present).

Predict-then-verify per L-2: every transformation asserts anchor count
== 1 before applying. Every file asserts post-condition checks (new
content present, expected count of new field).
"""

import sys
from pathlib import Path
from dataclasses import dataclass


@dataclass
class Patch:
    """One textual replacement with a description for error reporting."""
    description: str
    old: str
    new: str


@dataclass
class FileTransform:
    """All transformations for one file, applied atomically in-memory."""
    path: Path
    patches: list[Patch]
    expected_pre_marker_absent: str  # idempotency: this string must NOT exist
    expected_post_marker_present: str  # this string MUST exist after patches


# ============================================================================
# pudo_types.py — the contract change
# ============================================================================

PUDO_TYPES_IMPORT_OLD = """from typing import Literal, Optional

from cluster_detection import Cluster"""

PUDO_TYPES_IMPORT_NEW = """from datetime import datetime
from typing import Literal, Optional

from cluster_detection import Cluster"""


PUDO_TYPES_OFFER_OLD = """    Caller (driver_heartbeat.py / decisions.router) is responsible for
    assembling this struct correctly per state — WAI does not query
    offer_history.
    \"\"\"
    offer_id: str
    pickup: TargetSpec
    dropoff: TargetSpec
    secondary_dropoff: Optional[TargetSpec] = None"""

PUDO_TYPES_OFFER_NEW = """    Caller (driver_heartbeat.py / decisions.router) is responsible for
    assembling this struct correctly per state — WAI does not query
    offer_history.

    accepted_at (v2.6 amendment, sub-step 1b.1): the offer-acceptance
    timestamp from app_private.offer_history.accepted_at. Anchors the
    cluster-history lookback window in WAI's cluster_revisit topology
    check per Step 6 Amendment 1. UTC, timezone-aware.
    \"\"\"
    offer_id: str
    accepted_at: datetime
    pickup: TargetSpec
    dropoff: TargetSpec
    secondary_dropoff: Optional[TargetSpec] = None"""


PUDO_TYPES_WAIRESULT_OLD = """    # --- Underlying cluster snapshot (Q1) -----------------------------------
    # PLAN uses this directly for is_stable() across heartbeats rather than
    # re-running detect_cluster(). Keeping cluster math single-sourced is
    # the architectural reason Phase C extracted detect_cluster() into a
    # shared primitive in the first place.
    cluster: Optional[Cluster]


# ============================================================================
# DriverStateSnapshot — Input to PudoPlanner.consume()"""

PUDO_TYPES_WAIRESULT_NEW = """    # --- Underlying cluster snapshot (Q1) -----------------------------------
    # PLAN uses this directly for is_stable() across heartbeats rather than
    # re-running detect_cluster(). Keeping cluster math single-sourced is
    # the architectural reason Phase C extracted detect_cluster() into a
    # shared primitive in the first place.
    cluster: Optional[Cluster]

    # --- Cluster history topology (v2.6 amendment, sub-step 1b) -------------
    # True when WAI detects topological evidence of a round-trip (the
    # "Houston Loop"): a non-current cluster within CLUSTER_REVISIT_MIN_GAP_M
    # of the active cluster's centroid AND >=1 intermediate cluster
    # >= MIN_GAP_M from both. Sub-step 1b.1 ships the field as always-False
    # (placeholder); sub-step 1b.2 wires the real computation in evaluate();
    # sub-step 1c (B-26) is the same-address PLAN-side latch consumer.
    cluster_revisit: bool


# ============================================================================
# DriverStateSnapshot — Input to PudoPlanner.consume()"""


# ============================================================================
# where_am_i.py — 4 WhereAmIResult construction sites
# ============================================================================

# Site 1: _build_ghost_result (line 1037 area) — uniquely identified by the
# f-string reason and ghost_id=int(ghost_id).
WAI_SITE1_OLD = """            target_address=None,
            ghost_id=int(ghost_id),
            cluster=cluster,
        )"""

WAI_SITE1_NEW = """            target_address=None,
            ghost_id=int(ghost_id),
            cluster=cluster,
            cluster_revisit=False,
        )"""


# Site 2: _not_at_pudo (line 1061 area) — uniquely identified by cluster=None.
WAI_SITE2_OLD = """            target_address=None,
            ghost_id=None,
            cluster=None,
        )"""

WAI_SITE2_NEW = """            target_address=None,
            ghost_id=None,
            cluster=None,
            cluster_revisit=False,
        )"""


# Site 3: _at_unknown_pudo (line 1086 area) — uniquely identified by the
# specific reason string.
WAI_SITE3_OLD = """            reason="cluster detected but no offer or ghost match",
            target_address=None,
            ghost_id=None,
            cluster=cluster,
        )"""

WAI_SITE3_NEW = """            reason="cluster detected but no offer or ghost match",
            target_address=None,
            ghost_id=None,
            cluster=cluster,
            cluster_revisit=False,
        )"""


# Site 4: _build_current_result (line 1138 area) — uniquely identified by
# target_address=outcome.target_address.
WAI_SITE4_OLD = """            target_address=outcome.target_address,
            ghost_id=None,
            cluster=cluster,
        )"""

WAI_SITE4_NEW = """            target_address=outcome.target_address,
            ghost_id=None,
            cluster=cluster,
            cluster_revisit=False,
        )"""


# ============================================================================
# tests/test_pudo_planner.py — 1 WhereAmIResult site
# ============================================================================

# 4-space indented (test helper factory), unique anchor via ghost_id=ghost_id.
TPP_SITE_OLD = """        ghost_id=ghost_id,
        cluster=cluster,
    )"""

TPP_SITE_NEW = """        ghost_id=ghost_id,
        cluster=cluster,
        cluster_revisit=False,
    )"""


# ============================================================================
# tests/test_scenarios.py — 1 Offer site (S31 forensic replay)
# ============================================================================

# Literal accepted_at value chosen per Gemini ratification 2026-04-27:
# 7 minutes before earliest 7623 heartbeat (20:51:59.735) — realistic
# Houston ENROUTE leg duration, comfortably exceeds get_recent_clusters'
# 60-second preroll buffer.

TS_SITE_OLD = """    offer = Offer(
        offer_id=metadata["current_offer_id_text"],
        pickup=pickup,
        dropoff=dropoff,
    )"""

TS_SITE_NEW = """    # accepted_at: forensic anchor for 7623_heartbeats fixture (v2.6 amendment).
    # The earliest fixture heartbeat is at 2026-04-23T20:51:59.735Z; this
    # value is 7 minutes prior — a realistic Houston ENROUTE leg duration
    # that comfortably exceeds get_recent_clusters()'s 60-second preroll
    # buffer. See tests/fixtures/7623_heartbeats.json for the full record.
    offer = Offer(
        offer_id=metadata["current_offer_id_text"],
        accepted_at=datetime(2026, 4, 23, 20, 45, 0, tzinfo=timezone.utc),
        pickup=pickup,
        dropoff=dropoff,
    )"""


# ============================================================================
# tests/test_where_am_i.py — 1 Offer site + DUMMY_ACCEPTED_AT constant
# ============================================================================

# DUMMY_ACCEPTED_AT goes before the "Test fixture factory" section header.
# Uses _dt/_tz aliases per sub-step 1a's import convention in this file
# (line 28: from datetime import datetime as _dt, timezone as _tz).

TWAI_CONSTANT_OLD = """# =============================================================================
# Test fixture factory (Gemini Q1 ruling: factory over boilerplate)
# =============================================================================

def _cluster("""

TWAI_CONSTANT_NEW = '''# =============================================================================
# DUMMY_ACCEPTED_AT — synthetic anchor for Offer construction (v2.6 amendment)
# =============================================================================

DUMMY_ACCEPTED_AT = _dt(2026, 4, 27, 8, 0, tzinfo=_tz.utc)
"""
Synthetic anchor for tests that require an Offer construction but do not
depend on temporal lookback logic. L-9 fixture provenance: declared,
not borrowed.

WARNING: This is a placeholder of record. Tests exercising temporal logic
(e.g., Houston Loop topology, T75-T79) must use locally-coherent timestamps
relative to their heartbeat data, not this constant.
"""


# =============================================================================
# Test fixture factory (Gemini Q1 ruling: factory over boilerplate)
# =============================================================================

def _cluster('''


# Offer construction site at line 1014 — uniquely identified by the
# 4-field signature with secondary_dropoff.
TWAI_SITE_OLD = """    return Offer(
        offer_id=offer_id,
        pickup=pickup,
        dropoff=dropoff,
        secondary_dropoff=secondary_dropoff,
    )"""

TWAI_SITE_NEW = """    return Offer(
        offer_id=offer_id,
        accepted_at=DUMMY_ACCEPTED_AT,
        pickup=pickup,
        dropoff=dropoff,
        secondary_dropoff=secondary_dropoff,
    )"""


# ============================================================================
# scripts/wai_smoke.py — 2 Offer sites + DUMMY_ACCEPTED_AT constant
# ============================================================================

# DUMMY_ACCEPTED_AT goes just before the existing "Defaults pulled from
# canonical context" comment block. Uses bare datetime/timezone names
# per the file's clean import (line 47: from datetime import datetime, timezone).

WS_CONSTANT_OLD = """# Defaults pulled from canonical context (memories + S31 fixture).
DEFAULT_DRIVER = \"UjT1hE9eBXh2q95aSZYOkzDJ8lo1\""""

WS_CONSTANT_NEW = '''# Synthetic anchor for Offer construction (v2.6 amendment, sub-step 1b.1).
# wai_smoke is a smoke script, not a temporal-logic test; this dummy value
# satisfies Offer's now-required accepted_at field without affecting the
# script's read-only diagnostic intent.
DUMMY_ACCEPTED_AT = datetime(2026, 4, 27, 8, 0, tzinfo=timezone.utc)


# Defaults pulled from canonical context (memories + S31 fixture).
DEFAULT_DRIVER = "UjT1hE9eBXh2q95aSZYOkzDJ8lo1"'''


# Site 1 in wai_smoke.py (line 94 area) — uniquely identified by
# offer_id="smoke_test_offer".
WS_SITE1_OLD = """    return Offer(
        offer_id=\"smoke_test_offer\",
        pickup=pickup,
        dropoff=dropoff,
    )"""

WS_SITE1_NEW = """    return Offer(
        offer_id=\"smoke_test_offer\",
        accepted_at=DUMMY_ACCEPTED_AT,
        pickup=pickup,
        dropoff=dropoff,
    )"""


# Site 2 in wai_smoke.py (line 159 area) — uniquely identified by
# variable name bad_offer and offer_id="smoke_test_null".
WS_SITE2_OLD = """    bad_offer = Offer(
        offer_id=\"smoke_test_null\",
        pickup=bad_pickup,
        dropoff=dropoff,
    )"""

WS_SITE2_NEW = """    bad_offer = Offer(
        offer_id=\"smoke_test_null\",
        accepted_at=DUMMY_ACCEPTED_AT,
        pickup=bad_pickup,
        dropoff=dropoff,
    )"""


# ============================================================================
# Transform definitions
# ============================================================================

TRANSFORMS = [
    FileTransform(
        path=Path("pudo_types.py"),
        patches=[
            Patch("import datetime",       PUDO_TYPES_IMPORT_OLD,    PUDO_TYPES_IMPORT_NEW),
            Patch("Offer.accepted_at",     PUDO_TYPES_OFFER_OLD,     PUDO_TYPES_OFFER_NEW),
            Patch("WAIResult.cluster_revisit", PUDO_TYPES_WAIRESULT_OLD, PUDO_TYPES_WAIRESULT_NEW),
        ],
        # Idempotency: cluster_revisit field is the new-state marker.
        expected_pre_marker_absent="cluster_revisit: bool",
        expected_post_marker_present="cluster_revisit: bool",
    ),
    FileTransform(
        path=Path("where_am_i.py"),
        patches=[
            Patch("WAI site 1 (ghost result)",   WAI_SITE1_OLD, WAI_SITE1_NEW),
            Patch("WAI site 2 (not_at_pudo)",    WAI_SITE2_OLD, WAI_SITE2_NEW),
            Patch("WAI site 3 (at_unknown_pudo)",WAI_SITE3_OLD, WAI_SITE3_NEW),
            Patch("WAI site 4 (current_result)", WAI_SITE4_OLD, WAI_SITE4_NEW),
        ],
        expected_pre_marker_absent="cluster_revisit=False",
        expected_post_marker_present="cluster_revisit=False",
    ),
    FileTransform(
        path=Path("tests/test_pudo_planner.py"),
        patches=[
            Patch("test_pudo_planner WAIResult", TPP_SITE_OLD, TPP_SITE_NEW),
        ],
        expected_pre_marker_absent="cluster_revisit=False",
        expected_post_marker_present="cluster_revisit=False",
    ),
    FileTransform(
        path=Path("tests/test_scenarios.py"),
        patches=[
            Patch("S31 forensic Offer", TS_SITE_OLD, TS_SITE_NEW),
        ],
        expected_pre_marker_absent="accepted_at=datetime(2026, 4, 23, 20, 45",
        expected_post_marker_present="accepted_at=datetime(2026, 4, 23, 20, 45",
    ),
    FileTransform(
        path=Path("tests/test_where_am_i.py"),
        patches=[
            Patch("DUMMY_ACCEPTED_AT constant", TWAI_CONSTANT_OLD, TWAI_CONSTANT_NEW),
            Patch("Offer factory site",         TWAI_SITE_OLD,     TWAI_SITE_NEW),
        ],
        expected_pre_marker_absent="DUMMY_ACCEPTED_AT",
        expected_post_marker_present="DUMMY_ACCEPTED_AT = _dt(2026, 4, 27",
    ),
    FileTransform(
        path=Path("scripts/wai_smoke.py"),
        patches=[
            Patch("DUMMY_ACCEPTED_AT constant", WS_CONSTANT_OLD, WS_CONSTANT_NEW),
            Patch("smoke site 1 (default)",     WS_SITE1_OLD,    WS_SITE1_NEW),
            Patch("smoke site 2 (bad offer)",   WS_SITE2_OLD,    WS_SITE2_NEW),
        ],
        expected_pre_marker_absent="DUMMY_ACCEPTED_AT",
        expected_post_marker_present="DUMMY_ACCEPTED_AT = datetime(2026, 4, 27",
    ),
]


def fail(msg: str) -> None:
    print(f"FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def main() -> int:
    # ----- Phase 1: Load all files and verify pre-conditions ----------------
    print("=== Phase 1: Pre-condition verification ===")
    loaded: dict[Path, str] = {}
    for ft in TRANSFORMS:
        if not ft.path.exists():
            fail(f"{ft.path} not found in cwd. Run from puddlejumper-prod root.")
        original = ft.path.read_text()
        loaded[ft.path] = original

        # Idempotency: the new-state marker must not already exist.
        # Special-case: where_am_i.py uses the same marker as pudo_types.py
        # (cluster_revisit=False appears in test_pudo_planner.py too as the
        # post-marker), but for pre-check we want to ensure NO file has it.
        if ft.expected_pre_marker_absent in original:
            fail(f"{ft.path}: idempotency check failed — already contains "
                 f"{ft.expected_pre_marker_absent!r}. Has this script run already?")

        # Each patch's anchor must appear exactly once.
        for patch in ft.patches:
            count = original.count(patch.old)
            if count != 1:
                fail(f"{ft.path}: anchor for {patch.description!r} appears "
                     f"{count} times (expected 1).")

        print(f"  OK {ft.path} ({len(ft.patches)} anchors verified)")

    # ----- Phase 2: Apply transformations in-memory ------------------------
    print()
    print("=== Phase 2: In-memory transformation ===")
    transformed: dict[Path, str] = {}
    for ft in TRANSFORMS:
        content = loaded[ft.path]
        for patch in ft.patches:
            if content.count(patch.old) != 1:
                fail(f"{ft.path}: anchor for {patch.description!r} no longer "
                     f"unique mid-pipeline (something else changed it).")
            content = content.replace(patch.old, patch.new, 1)

        # Post-condition: marker must now be present at least once.
        if ft.expected_post_marker_present not in content:
            fail(f"{ft.path}: post-condition failed — {ft.expected_post_marker_present!r} "
                 f"not found in transformed content.")

        transformed[ft.path] = content
        delta = content.count("\n") - loaded[ft.path].count("\n")
        print(f"  OK {ft.path} (line delta: {delta:+d})")

    # ----- Phase 3: Write all files (commit phase) -------------------------
    print()
    print("=== Phase 3: Write to disk ===")
    for ft in TRANSFORMS:
        ft.path.write_text(transformed[ft.path])
        print(f"  WROTE {ft.path}")

    # ----- Summary ---------------------------------------------------------
    print()
    print("=== Summary ===")
    print(f"  Files modified:      {len(TRANSFORMS)}")
    total_patches = sum(len(ft.patches) for ft in TRANSFORMS)
    print(f"  Patches applied:     {total_patches}")
    print(f"  New imports:         1  (pudo_types.py: from datetime import datetime)")
    print(f"  New dataclass fields: 2 (Offer.accepted_at, WhereAmIResult.cluster_revisit)")
    print(f"  New module constants: 2 (DUMMY_ACCEPTED_AT in test_where_am_i.py and wai_smoke.py)")
    print()
    print("Verify next:")
    print("  pytest -q                                     # expect 258 passed")
    print("  git status --short                            # expect 6 modified files")
    print("  git diff --stat                               # expect ~50 insertions across 6 files")
    return 0


if __name__ == "__main__":
    sys.exit(main())