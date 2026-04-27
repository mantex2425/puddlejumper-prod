#!/usr/bin/env python3
"""
apply_substep_1b2.py — Phase E Step 6 sub-step 1b.2 WAI wiring.

Wires get_recent_clusters() into WhereAmI.evaluate() as the cluster_revisit
topology signal per the v2.6 amendment to WHERE_AM_I_PROPOSAL_v2.md
(commit aa8e667). Replaces the always-False placeholders shipped in
sub-step 1b.1 (commit 6e1d60f) with the real verdict at three of four
return paths.

Design ratified by Gemini 2026-04-27 via the Q1/Q2/Q3 round:
  Q1 — _not_at_pudo (no active cluster):      cluster_revisit=False permanently
                                              (no call to get_recent_clusters)
  Q2 — current_offer is None:                 cluster_revisit=False permanently
                                              (no fallback anchor; no call)
  Q3 — ghost-cache hit + no offer:            cluster_revisit=False permanently
                                              (per Q2; "at_previous_pudo"
                                              status already signals revisit
                                              of an old PUDO, conceptually
                                              distinct from same-ride loop)

Topology (Gemini ratified): cluster_revisit=True iff there exists a
non-active cluster within CLUSTER_REVISIT_MIN_GAP_M of the active centroid
AND >=1 intermediate cluster >= MIN_GAP_M from BOTH (the active cluster
AND the prior PUDO). The "from both" framing protects against GPS
multipath shuffling at one address being mistaken for a true round-trip.
No minimum duration floor on the intermediate (L-10: paranoia thresholds
do not ship; min_samples=3, max_speed_mph=10 inside get_recent_clusters
already constitute the structural time floor).

Scope of this patch (where_am_i.py only — 8 changes):

  1. Import: add get_recent_clusters to from cluster_detection import line.

  2. Constant: add CLUSTER_REVISIT_MIN_GAP_M = 200 module constant.
     L-10 category 1 provenance comment: production-data-grounded
     structural noise floor, Houston GPS multipath wobble.

  3. Helper: add _compute_cluster_revisit() pure function before class
     WhereAmI. Uses _haversine_meters for distance. Returns False on
     trivial inputs (empty/single-element history); returns True iff a
     valid (prior_pudo, intermediate) pair exists in the history.

  4. __init__ signature: add _recent_clusters_fn=get_recent_clusters as a
     third keyword-only injected callable, mirroring _cluster_fn / _pivot_fn.

  5. __init__ body: bind self._recent_clusters_fn = _recent_clusters_fn.

  6. evaluate() body: insert cluster-history topology computation between
     Step 3 (stop_context) and Step 4 (current-ride PUDO matching). Only
     calls _recent_clusters_fn when current_offer is not None
     (Q2 ratification). Variable cluster_revisit is bound for use in
     subsequent return paths.

  7. Builder signatures: add cluster_revisit: bool parameter to:
       - _match_ghost_cache (called via Step 5 of evaluate)
       - _at_unknown_pudo   (called via Step 6 of evaluate)
       - _build_current_result (called via Step 4 of evaluate)
     _not_at_pudo is unchanged — it stays False permanently per Q1.

  8. Builder call sites in evaluate(): plumb cluster_revisit=cluster_revisit
     into the three call sites; AND replace the three "cluster_revisit=False"
     placeholders inside the builder bodies with "cluster_revisit=cluster_revisit".
     The _not_at_pudo call site and body stay unchanged.

Floor target: 258 -> 258 (wiring; tests come in 1b.3).

Atomicity: all transformations applied in-memory and verified before any
disk write. Per L-3, anchor strings are multi-line with sufficient context
to guarantee uniqueness.

Idempotency: the new module constant CLUSTER_REVISIT_MIN_GAP_M is the
new-state marker; rerunning after success fails on its presence.
"""

import sys
from pathlib import Path
from dataclasses import dataclass


@dataclass
class Patch:
    description: str
    old: str
    new: str


# ============================================================================
# 1. Import: add get_recent_clusters
# ============================================================================

IMPORT_OLD = "from cluster_detection import detect_cluster"

IMPORT_NEW = "from cluster_detection import detect_cluster, get_recent_clusters"


# ============================================================================
# 2. Module constant: CLUSTER_REVISIT_MIN_GAP_M
# ============================================================================

# Place after GHOST_MATCH_RADIUS_M block and before the # Cluster tightness
# thresholds block.

CONSTANT_OLD = """# Ghost-cache match radius (Step 1 Q8)
GHOST_MATCH_RADIUS_M = 50.0

# Cluster tightness thresholds (Step 3 section A.3)"""

CONSTANT_NEW = """# Ghost-cache match radius (Step 1 Q8)
GHOST_MATCH_RADIUS_M = 50.0

# Cluster history topology — Houston Loop revisit gate (v2.6 amendment, sub-step 1b.2)
# L-10 category 1 provenance: production-data-grounded structural noise floor.
# Houston GPS multipath wobble in the rideshare heartbeat stream produces
# centroid drift at a single address; 200m forces evidence of a genuinely
# different spatial context (driver actually departed and returned) before
# the round-trip latch (B-26) can fire. Not a policy threshold — adjusting
# requires physics-of-the-environment justification, not behavioral preference.
CLUSTER_REVISIT_MIN_GAP_M = 200.0

# Cluster tightness thresholds (Step 3 section A.3)"""


# ============================================================================
# 3. Pure helper: _compute_cluster_revisit
# ============================================================================

# Insert immediately before "class WhereAmI:" definition (line 756).
# We anchor on a stable preceding section. The cleanest option is to find
# the last module-level def or constant before class WhereAmI, but we don't
# know what that is precisely. Use class WhereAmI: as the anchor itself.

HELPER_OLD = """class WhereAmI:"""

HELPER_NEW = '''def _compute_cluster_revisit(
    active_cluster: "Cluster",
    recent_clusters: list,
) -> bool:
    """Houston Loop topology check (v2.6 amendment, sub-step 1b.2).

    Returns True iff `recent_clusters` contains topological evidence that
    the driver has departed and returned to the active cluster's location
    within the offer-anchored lookback window. The signal is structural,
    not temporal — the intermediate cluster's existence IS the proof of
    departure-and-return.

    Args:
      active_cluster: the cluster currently under evaluation. Always the
        last element of recent_clusters when called from evaluate(), but
        passed separately for clarity and to keep this helper testable
        without ordering assumptions on recent_clusters.
      recent_clusters: oldest-first list of all clusters in the offer-
        anchored lookback window, as returned by get_recent_clusters().

    Algorithm:
      1. History = recent_clusters minus active_cluster (matched by identity
         on (median_lat, median_lng, latest) — the natural unique key).
      2. If |history| < 2: return False (no room for prior_pudo + intermediate).
      3. For each candidate prior_pudo in history (chronological order):
         If prior_pudo within MIN_GAP_M of active centroid:
           For each later cluster mid in history (after prior_pudo):
             If mid >= MIN_GAP_M from BOTH active AND prior_pudo:
               Return True.
      4. Return False.

    The "from both" framing (Gemini Q3 ratification) protects against GPS
    multipath drift at a single address being mistaken for a true revisit:
    if the driver simply shuffled around the pickup, the intermediate would
    be near the prior_pudo (and thus also near active, since prior_pudo is
    near active). Requiring distance from both proves a genuinely different
    spatial context.

    Performance: O(n^2) in cluster count. n is bounded by the offer-
    anchored window (typically <30 clusters even for long fares); the
    nested loop terminates on first valid pair, so worst case is rare.
    """
    # Identify the active cluster within recent_clusters by its tuple key.
    # Equality-by-content is safer than `is` since callers may construct
    # the active_cluster freshly.
    active_key = (active_cluster.median_lat, active_cluster.median_lng,
                  active_cluster.latest)
    history = [
        c for c in recent_clusters
        if (c.median_lat, c.median_lng, c.latest) != active_key
    ]

    if len(history) < 2:
        return False

    # history is oldest-first (per get_recent_clusters' ORDER BY latest ASC).
    # Iterate prior_pudo candidates in chronological order.
    for i, prior_pudo in enumerate(history):
        d_prior_to_active = _haversine_meters(
            prior_pudo.median_lat, prior_pudo.median_lng,
            active_cluster.median_lat, active_cluster.median_lng,
        )
        if d_prior_to_active >= CLUSTER_REVISIT_MIN_GAP_M:
            continue  # not a prior_pudo candidate

        # Look for an intermediate AFTER prior_pudo (chronologically).
        for mid in history[i + 1:]:
            d_mid_to_active = _haversine_meters(
                mid.median_lat, mid.median_lng,
                active_cluster.median_lat, active_cluster.median_lng,
            )
            d_mid_to_prior = _haversine_meters(
                mid.median_lat, mid.median_lng,
                prior_pudo.median_lat, prior_pudo.median_lng,
            )
            if (d_mid_to_active >= CLUSTER_REVISIT_MIN_GAP_M and
                    d_mid_to_prior >= CLUSTER_REVISIT_MIN_GAP_M):
                return True

    return False


class WhereAmI:'''


# ============================================================================
# 4 & 5. __init__ signature and body
# ============================================================================

INIT_OLD = """    def __init__(
        self,
        cur,
        *,
        _cluster_fn=detect_cluster,
        _pivot_fn=get_pivot_context,
    ):
        \"\"\"
        cur: psycopg cursor for ghost-cache SELECT.
        _cluster_fn: callable(driver_id, cur) -> Optional[Cluster].
            Default: cluster_detection.detect_cluster. Tests inject fakes.
        _pivot_fn: callable(driver_id, cur, anchor_time=None) -> dict.
            Default: pivot_context.get_pivot_context. Tests inject fakes.

        Keyword-only args via `*` so production callers never accidentally
        pass test doubles positionally.
        \"\"\"
        self.cur = cur
        self._cluster_fn = _cluster_fn
        self._pivot_fn = _pivot_fn"""

INIT_NEW = """    def __init__(
        self,
        cur,
        *,
        _cluster_fn=detect_cluster,
        _pivot_fn=get_pivot_context,
        _recent_clusters_fn=get_recent_clusters,
    ):
        \"\"\"
        cur: psycopg cursor for ghost-cache SELECT.
        _cluster_fn: callable(driver_id, cur) -> Optional[Cluster].
            Default: cluster_detection.detect_cluster. Tests inject fakes.
        _pivot_fn: callable(driver_id, cur, anchor_time=None) -> dict.
            Default: pivot_context.get_pivot_context. Tests inject fakes.
        _recent_clusters_fn: callable(driver_id, cur, accepted_at_anchor, ...)
            -> list[Cluster]. Default: cluster_detection.get_recent_clusters.
            Used by evaluate()'s cluster_revisit topology check (v2.6
            amendment, sub-step 1b.2). Tests inject fakes.

        Keyword-only args via `*` so production callers never accidentally
        pass test doubles positionally.
        \"\"\"
        self.cur = cur
        self._cluster_fn = _cluster_fn
        self._pivot_fn = _pivot_fn
        self._recent_clusters_fn = _recent_clusters_fn"""


# ============================================================================
# 6. evaluate() body: cluster_revisit computation + plumbing into builders
# ============================================================================

EVALUATE_OLD = '''        # --- Step 1: Cluster check -----------------------------------------
        cluster = self._cluster_fn(driver_id, self.cur)
        if cluster is None:
            return self._not_at_pudo(reason="no cluster detected")

        # --- Step 2: Topology (single pivot_context call, reused below) ----
        topo = self._compute_road_topology(driver_id)

        # --- Step 3: Stop context — STUB for v1.0 (Stop Atlas v1.1) -------
        stop_context = "unknown_stop"

        # --- Step 4: Current-ride PUDO matching ---------------------------
        if current_offer is not None:
            current_outcome = self._match_current_pudo(
                cluster, topo, current_offer, state,
            )
            if current_outcome is not None and current_outcome.matched:
                return self._build_current_result(
                    outcome=current_outcome,
                    topo=topo,
                    stop_context=stop_context,
                    cluster=cluster,
                    offer=current_offer,
                    state=state,
                )

        # --- Step 5: Ghost cache READ (Q12 lock: read-only) ---------------
        ghost_result = self._match_ghost_cache(driver_id, cluster, topo, stop_context)
        if ghost_result is not None:
            return ghost_result

        # --- Step 6: Cluster exists, no offer or ghost explains it --------
        # Per Q12: WAI does NOT INSERT here. PLAN consumer (Phase E)
        # decides whether to persist a suspect into suspected_pudos.
        return self._at_unknown_pudo(cluster, topo, stop_context)'''

EVALUATE_NEW = '''        # --- Step 1: Cluster check -----------------------------------------
        cluster = self._cluster_fn(driver_id, self.cur)
        if cluster is None:
            return self._not_at_pudo(reason="no cluster detected")

        # --- Step 2: Topology (single pivot_context call, reused below) ----
        topo = self._compute_road_topology(driver_id)

        # --- Step 3: Stop context — STUB for v1.0 (Stop Atlas v1.1) -------
        stop_context = "unknown_stop"

        # --- Step 3.5: Cluster history topology (v2.6 amendment) ----------
        # Compute cluster_revisit only when an active offer anchors the
        # lookback window (Q2 ratification). Without an offer there's no
        # "current ride" for round-trip semantics; cluster_revisit is
        # ride-scoped, not driver-scoped.
        if current_offer is not None:
            recent_clusters = self._recent_clusters_fn(
                driver_id, self.cur,
                accepted_at_anchor=current_offer.accepted_at,
            )
            cluster_revisit = _compute_cluster_revisit(cluster, recent_clusters)
        else:
            cluster_revisit = False

        # --- Step 4: Current-ride PUDO matching ---------------------------
        if current_offer is not None:
            current_outcome = self._match_current_pudo(
                cluster, topo, current_offer, state,
            )
            if current_outcome is not None and current_outcome.matched:
                return self._build_current_result(
                    outcome=current_outcome,
                    topo=topo,
                    stop_context=stop_context,
                    cluster=cluster,
                    offer=current_offer,
                    state=state,
                    cluster_revisit=cluster_revisit,
                )

        # --- Step 5: Ghost cache READ (Q12 lock: read-only) ---------------
        ghost_result = self._match_ghost_cache(
            driver_id, cluster, topo, stop_context, cluster_revisit,
        )
        if ghost_result is not None:
            return ghost_result

        # --- Step 6: Cluster exists, no offer or ghost explains it --------
        # Per Q12: WAI does NOT INSERT here. PLAN consumer (Phase E)
        # decides whether to persist a suspect into suspected_pudos.
        return self._at_unknown_pudo(cluster, topo, stop_context, cluster_revisit)'''


# ============================================================================
# 7. Builder signatures: add cluster_revisit: bool parameter
# ============================================================================

# 7a. _match_ghost_cache signature
GHOST_SIG_OLD = """    def _match_ghost_cache(
        self,
        driver_id: str,
        cluster: Cluster,
        topo: _RoadTopology,
        stop_context: str,
    ) -> Optional[WhereAmIResult]:"""

GHOST_SIG_NEW = """    def _match_ghost_cache(
        self,
        driver_id: str,
        cluster: Cluster,
        topo: _RoadTopology,
        stop_context: str,
        cluster_revisit: bool,
    ) -> Optional[WhereAmIResult]:"""


# 7b. _at_unknown_pudo signature
UNK_SIG_OLD = """    def _at_unknown_pudo(
        self,
        cluster: Cluster,
        topo: _RoadTopology,
        stop_context: str,
    ) -> WhereAmIResult:"""

UNK_SIG_NEW = """    def _at_unknown_pudo(
        self,
        cluster: Cluster,
        topo: _RoadTopology,
        stop_context: str,
        cluster_revisit: bool,
    ) -> WhereAmIResult:"""


# 7c. _build_current_result signature
CURRENT_SIG_OLD = """    def _build_current_result(
        self,
        outcome: _MatchOutcome,
        topo: _RoadTopology,
        stop_context: str,
        cluster: Cluster,
        offer,
        state: str,
    ) -> WhereAmIResult:"""

CURRENT_SIG_NEW = """    def _build_current_result(
        self,
        outcome: _MatchOutcome,
        topo: _RoadTopology,
        stop_context: str,
        cluster: Cluster,
        offer,
        state: str,
        cluster_revisit: bool,
    ) -> WhereAmIResult:"""


# ============================================================================
# 8. Replace cluster_revisit=False placeholders inside three builder bodies
# ============================================================================

# Site 1: _match_ghost_cache body (line ~1053). Anchor: ghost_id=int(ghost_id).
SITE1_OLD = """            ghost_id=int(ghost_id),
            cluster=cluster,
            cluster_revisit=False,
        )"""

SITE1_NEW = """            ghost_id=int(ghost_id),
            cluster=cluster,
            cluster_revisit=cluster_revisit,
        )"""


# Site 3: _at_unknown_pudo body. Anchor: the unique reason string.
SITE3_OLD = '''            reason="cluster detected but no offer or ghost match",
            target_address=None,
            ghost_id=None,
            cluster=cluster,
            cluster_revisit=False,
        )'''

SITE3_NEW = '''            reason="cluster detected but no offer or ghost match",
            target_address=None,
            ghost_id=None,
            cluster=cluster,
            cluster_revisit=cluster_revisit,
        )'''


# Site 4: _build_current_result body. Anchor: target_address=outcome.target_address.
SITE4_OLD = """            target_address=outcome.target_address,
            ghost_id=None,
            cluster=cluster,
            cluster_revisit=False,
        )"""

SITE4_NEW = """            target_address=outcome.target_address,
            ghost_id=None,
            cluster=cluster,
            cluster_revisit=cluster_revisit,
        )"""


# Site 2: _not_at_pudo body — DO NOT CHANGE. Stays cluster_revisit=False
# permanently per Q1 ratification (no active cluster -> no revisit possible).


# ============================================================================
# Transform definition (single file)
# ============================================================================

PATCHES = [
    Patch("1. import get_recent_clusters",      IMPORT_OLD,      IMPORT_NEW),
    Patch("2. CLUSTER_REVISIT_MIN_GAP_M const", CONSTANT_OLD,    CONSTANT_NEW),
    Patch("3. _compute_cluster_revisit helper", HELPER_OLD,      HELPER_NEW),
    Patch("4&5. WhereAmI.__init__",             INIT_OLD,        INIT_NEW),
    Patch("6. evaluate() body",                 EVALUATE_OLD,    EVALUATE_NEW),
    Patch("7a. _match_ghost_cache signature",   GHOST_SIG_OLD,   GHOST_SIG_NEW),
    Patch("7b. _at_unknown_pudo signature",     UNK_SIG_OLD,     UNK_SIG_NEW),
    Patch("7c. _build_current_result signature",CURRENT_SIG_OLD, CURRENT_SIG_NEW),
    Patch("8a. site 1 (ghost) cluster_revisit",       SITE1_OLD, SITE1_NEW),
    Patch("8b. site 3 (unknown) cluster_revisit",     SITE3_OLD, SITE3_NEW),
    Patch("8c. site 4 (current) cluster_revisit",     SITE4_OLD, SITE4_NEW),
]


PATH = Path("where_am_i.py")
PRE_MARKER_ABSENT = "CLUSTER_REVISIT_MIN_GAP_M"  # idempotency guard
POST_MARKER_PRESENT = "CLUSTER_REVISIT_MIN_GAP_M = 200.0"


def fail(msg: str) -> None:
    print(f"FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def main() -> int:
    # ----- Phase 1: Pre-condition verification -----------------------------
    print("=== Phase 1: Pre-condition verification ===")
    if not PATH.exists():
        fail(f"{PATH} not found in cwd. Run from puddlejumper-prod root.")

    original = PATH.read_text()

    if PRE_MARKER_ABSENT in original:
        fail(f"{PATH}: idempotency check failed — already contains "
             f"{PRE_MARKER_ABSENT!r}. Has this script run already?")

    for patch in PATCHES:
        count = original.count(patch.old)
        if count != 1:
            fail(f"anchor for {patch.description!r} appears {count} times "
                 f"(expected 1).")

    print(f"  OK {PATH} ({len(PATCHES)} anchors verified)")

    # ----- Phase 2: In-memory transformation -------------------------------
    print()
    print("=== Phase 2: In-memory transformation ===")
    content = original
    for patch in PATCHES:
        if content.count(patch.old) != 1:
            fail(f"anchor for {patch.description!r} no longer unique mid-pipeline.")
        content = content.replace(patch.old, patch.new, 1)
        print(f"  applied: {patch.description}")

    if POST_MARKER_PRESENT not in content:
        fail(f"post-condition failed — {POST_MARKER_PRESENT!r} not in "
             f"transformed content.")

    delta = content.count("\n") - original.count("\n")
    print(f"  Line delta: {delta:+d}")

    # ----- Phase 3: Write to disk -----------------------------------------
    print()
    print("=== Phase 3: Write to disk ===")
    PATH.write_text(content)
    print(f"  WROTE {PATH}")

    # ----- Summary --------------------------------------------------------
    print()
    print("=== Summary ===")
    print(f"  File:                {PATH}")
    print(f"  Patches applied:     {len(PATCHES)}")
    print(f"  Line delta:          {delta:+d}")
    print(f"  New module constant: CLUSTER_REVISIT_MIN_GAP_M = 200.0")
    print(f"  New helper:          _compute_cluster_revisit()")
    print(f"  New injection:       _recent_clusters_fn (WhereAmI.__init__)")
    print()
    print("Verify next:")
    print("  python3 -c 'import where_am_i'         # syntax + import sanity")
    print("  pytest -q                              # expect 258 passed")
    print("  grep -c 'cluster_revisit=cluster_revisit' where_am_i.py  # expect 3")
    print("  grep -c 'cluster_revisit=False' where_am_i.py            # expect 2")
    print("    (one in _not_at_pudo body, one in evaluate's else branch)")
    return 0


if __name__ == "__main__":
    sys.exit(main())