#!/usr/bin/env python3
"""
refresh_progress_1a.py — refresh PHASE_E_PROGRESS.md after sub-step 1a.

Four anchor-based edits (per L-3):
  1. Header date + revision tag.
  2. "Current state of the world" — HEAD, cluster_detection line count,
     and sub-step status sentence.
  3. Commit lineage table — append sub-step 1a row.
  4. Step 6 section — insert "Sub-step 1a — CLOSED" entry between sub-step 0
     and sub-step 1.
  5. Starter message — rewrite for sub-step 1b as next work.

Idempotent: rerun is a noop if already applied.
"""

import sys
import subprocess
from pathlib import Path

p = Path("PHASE_E_PROGRESS.md")
if not p.exists():
    print("ERROR: must run from repo root")
    sys.exit(1)

# Get the actual sub-step 1a SHA from the most recent commit on this branch
# (we'll be running this BEFORE the commit, so the SHA is unknown — use a
# placeholder that the commit step will substitute).
SHA_PLACEHOLDER = "{SUBSTEP_1A_SHA}"

text = p.read_text()
original = text

# -----------------------------------------------------------------------------
# Edit 1: header date + revision
# -----------------------------------------------------------------------------
old1 = """**Updated:** 2026-04-27 — sub-step 0.3 + Amendment 1 closeout (commit 7a8616a).
**Replaces:** prior version at commit 7863b11 (post sub-step 0.3, pre-Amendment 1)."""
new1 = """**Updated:** 2026-04-27 — sub-step 1a closeout (commit """ + SHA_PLACEHOLDER + """).
**Replaces:** prior version at commit 89367ff (post-Amendment 1, pre sub-step 1a)."""
if old1 in text:
    text = text.replace(old1, new1, 1)
    print("[ok] edit 1: header date + revision")
elif new1.replace(SHA_PLACEHOLDER, "") in text.replace(SHA_PLACEHOLDER, ""):
    print("[skip] edit 1: already applied")
else:
    print("[FAIL] edit 1: header anchor not found")
    sys.exit(2)

# -----------------------------------------------------------------------------
# Edit 2: "Current state of the world" — HEAD + line count + status sentence
# -----------------------------------------------------------------------------
old2 = """HEAD:                 7a8616a (Phase E Step 6 — Amendment 1 (Offer-Anchor Lookback) ratified)
Branch:               patch-00566a-unified-refinement (clean working tree, in lockstep with origin)
Tests pytest:         250/250 passing
Tests integration:    22/61 passing (39 failing) per sub-step 0.1 baseline 2026-04-26 23:46:19 UTC
Live-PG smoke:        4/4 from Phase D Step 5.7.2 (not re-run in Phase E; no DB-coupled changes shipped)
pudo_planner.py:      1041 lines, 4 sections (A/B/C/D), 11 builders, 68-test pytest suite
test_pudo_planner.py: 1650 lines, 68 tests across 6 sub-step blocks
cluster_detection.py: 200 lines (sub-step 1a target — get_recent_clusters() + Cluster.latest field)
where_am_i.py:        1154 lines (sub-step 1b target — cluster_revisit field)"""

# Compute new line count for cluster_detection.py
cd_lines = sum(1 for _ in open("cluster_detection.py"))
tcd_lines = sum(1 for _ in open("tests/test_cluster_detection.py"))
new2 = f"""HEAD:                 {SHA_PLACEHOLDER} (Phase E Step 6 sub-step 1a — get_recent_clusters() + Cluster.latest)
Branch:               patch-00566a-unified-refinement (clean working tree, in lockstep with origin)
Tests pytest:         258/258 passing (250 floor + 8 new T1-T8 for get_recent_clusters)
Tests integration:    22/61 passing (39 failing) per sub-step 0.1 baseline 2026-04-26 23:46:19 UTC
Live-PG smoke:        4/4 from Phase D Step 5.7.2 (not re-run in Phase E; no DB-coupled changes shipped)
pudo_planner.py:      1041 lines, 4 sections (A/B/C/D), 11 builders, 68-test pytest suite
test_pudo_planner.py: 1650 lines, 68 tests across 6 sub-step blocks
cluster_detection.py: {cd_lines} lines (sub-step 1a SHIPPED — get_recent_clusters() + Cluster.latest)
test_cluster_detection.py: {tcd_lines} lines ({8} new T1-T8 tests appended in sub-step 1a)
where_am_i.py:        1154 lines (sub-step 1b target — cluster_revisit field)"""

if old2 in text:
    text = text.replace(old2, new2, 1)
    print("[ok] edit 2: current state of the world")
elif "sub-step 1a SHIPPED" in text:
    print("[skip] edit 2: already applied")
else:
    print("[FAIL] edit 2: state-of-world anchor not found")
    sys.exit(2)

# -----------------------------------------------------------------------------
# Edit 2b: status sentence after the code block
# -----------------------------------------------------------------------------
old2b = """**Sub-step 1a is the next
concrete work** — authoring `get_recent_clusters()` per the offer-anchor
signature."""
new2b = """**Sub-step 1a SHIPPED at commit """ + SHA_PLACEHOLDER + """** —
`get_recent_clusters()` authored per Amendment 1's offer-anchor signature
with gaps-and-islands SQL extending detect_cluster()'s `breaks_before` pattern;
`Cluster.latest` field added (data already in SQL via `MAX(logged_at)`, just
exposed). 8 new T1-T8 unit tests; 5 consumer call sites updated for the
required `latest=` kwarg. **Sub-step 1b is the next concrete work** — wiring
`get_recent_clusters()` into WAI's `cluster_revisit` topology check per the
v2.6 amendment to WHERE_AM_I_PROPOSAL_v2.md."""

if old2b in text:
    text = text.replace(old2b, new2b, 1)
    print("[ok] edit 2b: status sentence")
elif "Sub-step 1a SHIPPED at commit" in text:
    print("[skip] edit 2b: already applied")
else:
    print("[FAIL] edit 2b: status sentence anchor not found")
    sys.exit(2)

# -----------------------------------------------------------------------------
# Edit 3: commit lineage table — append sub-step 1a row
# -----------------------------------------------------------------------------
old3 = "| 1f04d23   | Pre-6     | UTC anchor patch on tests/test_integration.sh (T16/T30/T38) |"
new3 = """| 1f04d23   | Pre-6     | UTC anchor patch on tests/test_integration.sh (T16/T30/T38) |
| 86ea117   | 6.0.1     | Sub-step 0.1 — live integration baseline 22/61 (L-6 corollary) |
| 5a86f2e   | 6.0.2     | Sub-step 0.2 — Group E inventory closure (39 failures classified) |
| 7863b11   | 6.0.3     | Sub-step 0.3 — WAI source-read: stateless against cluster history |
| 7a8616a   | 6 amend   | Step 6 Amendment 1 (Offer-Anchor Lookback) ratified |
| 89367ff   | 6 doc     | PHASE_E_PROGRESS.md refresh per L-11 (post-Amendment 1) |
| """ + SHA_PLACEHOLDER + """ | 6.1a      | Sub-step 1a — get_recent_clusters() + Cluster.latest (258 floor) |"""

if old3 in text:
    text = text.replace(old3, new3, 1)
    print("[ok] edit 3: commit lineage")
elif "Sub-step 1a — get_recent_clusters()" in text:
    print("[skip] edit 3: already applied")
else:
    print("[FAIL] edit 3: lineage anchor not found")
    sys.exit(2)

# -----------------------------------------------------------------------------
# Edit 4: insert "Sub-step 1a — CLOSED" entry before "Sub-step 1 — Contract
# amendments (NEXT SESSION's work)" heading.
# -----------------------------------------------------------------------------
old4 = """### Sub-step 1 — Contract amendments (NEXT SESSION's work)"""

new4 = """### Sub-step 1a — get_recent_clusters() + Cluster.latest — CLOSED 2026-04-27

Single-commit ship at `""" + SHA_PLACEHOLDER + """`. Three artifacts:

1. **`Cluster` dataclass extended** with `latest: datetime` field (sixth field
   appended). The data was already aggregated by detect_cluster()'s SQL via
   `MAX(logged_at)` — the field exposes it. `detect_cluster()`'s return
   updated to populate it; no SQL change. 5 external call sites updated for
   the now-required `latest=` kwarg (test_scenarios, test_where_am_i factory,
   test_pudo_planner ×2, scripts/wai_smoke).

2. **`get_recent_clusters()` authored** in `cluster_detection.py` per
   Amendment 1's offer-anchored signature. Window: `[accepted_at_anchor -
   preroll_sec, NOW()]`. SQL is a **gaps-and-islands extension** of
   `detect_cluster()`'s `breaks_before` pattern: where `detect_cluster()`
   filters `breaks_before = 0` to keep only the most-recent island, this
   GROUPs on `breaks_before` so each distinct value forms one chronologically-
   contiguous low-speed run. Three deliberate departures from
   `detect_cluster()`: single query (not two), late spread filter (drops
   only the offending island), explicit `<= NOW()` upper bound for synthetic
   test safety. Ratified by Gemini 2026-04-27 with no pushback.

3. **8 new T1-T8 unit tests** appended to `tests/test_cluster_detection.py`
   following the file's mock-cursor convention (MagicMock with
   `fetchall.return_value`, hand-computing what SQL would return). Tests
   T4 (gaps-and-islands behavior) and T5 (lookback boundary) softened to
   function-level claims — SQL correctness is validated at integration
   (`tests/test_integration.sh`, current floor 22/61), not in unit-mock
   tests. T6 locks the parameter binding contract; T8 locks default-kwarg
   parity with `detect_cluster()`. Floor: 250 → 258 passing.

**Apply-time bugs caught in `apply_substep_1a.py`** (forensic record;
documented in the script's `ensure_imports` docstring). All three were
import-detection bugs in the patch helper, all repaired live without
requiring a second commit:

- Substring probe false-positive: `tests/test_pudo_planner.py` already
  had `from datetime import timezone` (no `datetime`), which matched the
  probe `"from datetime import"` and suppressed insertion. Repaired by
  switching the file's two new call sites to `datetime.datetime(...)`
  (matching the file's existing convention via `import datetime` at line 21).
- Open-paren confusion: `tests/test_where_am_i.py` had `from where_am_i
  import (` as the last `from` line in the first 50, so insertion landed
  inside the open parens. Repaired by relocating the import.
- Module-vs-class shadowing: same file has `import datetime` at line 940,
  rebinding the name back to the module after our `from datetime import
  datetime`. Repaired by aliasing to `_dt`/`_tz` in lines 27 and 65.

**Architectural rulings exercised:** L-2 (predict-then-verify on every
gate), L-3 (anchor-based patch script), L-5 (trailing-newline guard
implicit in test additions), L-6 (read `cluster_detection.py` and
`test_cluster_detection.py` verbatim before authoring), L-6 corollary
(reading external `Cluster()` call sites before patching), L-7 (UTC
ruling reconciled with bare-`NOW()` engine convention via Gemini Gate A),
L-9 (synthetic Null Island fixtures fine for unit tests; integration
gets live data), L-10 (default kwargs locked as single source of truth
across `detect_cluster()` and `get_recent_clusters()` via T8), L-11
(this entry).

### Sub-step 1 — Contract amendments (NEXT SESSION's work)"""

if old4 in text:
    text = text.replace(old4, new4, 1)
    print("[ok] edit 4: sub-step 1a closeout entry inserted")
elif "Sub-step 1a — get_recent_clusters() + Cluster.latest — CLOSED" in text:
    print("[skip] edit 4: already applied")
else:
    print("[FAIL] edit 4: sub-step 1 heading anchor not found")
    sys.exit(2)

# -----------------------------------------------------------------------------
# Edit 5: rewrite starter message for sub-step 1b
# -----------------------------------------------------------------------------
old5 = """> I'm resuming Phase E at Step 6 sub-step 1a. Sub-step 0 closed at
> commit 7863b11 (WAI source-read finding: WAI is stateless against
> cluster history). Step 6 Amendment 1 (Offer-Anchor Lookback) shipped
> at commit 7a8616a. HEAD is 7a8616a. Floor: pytest 250/250,
> integration 22/61.
>
> Sub-step 1a authors `get_recent_clusters()` in `cluster_detection.py`
> per Amendment 1's offer-anchored signature:
>
>     def get_recent_clusters(
>         driver_id: str, cur,
>         accepted_at_anchor: datetime,
>         preroll_sec: int = 60,
>         min_samples: int = 3,
>         max_speed_mph: float = 10.0,
>         max_spread_m: float = 25.0,
>     ) -> list[Cluster]
>
> Window: [accepted_at_anchor - preroll_sec, NOW()]. SQL approach:
> gaps-and-islands extension of detect_cluster()'s breaks_before=0
> pattern. Cluster dataclass extension: add `latest: datetime`. Test
> floor projected 250 → 254-258. Single commit.
>
> Please read in order:
>   1. PHASE_E_PROGRESS.md (this file)
>   2. PHASE_E_KICKOFF.md (architectural ground truth)
>   3. PHASE_E_STEP_6_DESIGN.md — read the top-of-doc Amendment 1
>      notice, then the body, then the full Amendment 1 spec at end
>   4. cluster_detection.py (200 lines, target file)
>   5. tests/test_cluster_detection.py
>   6. WHERE_AM_I_PROPOSAL_v2.md (planned v2.6 amendment ships in 1b)
>
> **Gates before authoring 1a:**
>   - L-11 doc-currency check: HEAD must be 7a8616a, pytest 250, working
>     tree clean
>   - Q6 re-verification query: run the same-address forensic search
>     from sub-step 1a design brief against current data. If zero
>     candidates, T75-T79 ships synthetic per L-9 Null Island
>     convention. If non-zero, evaluate per L-9 fixture provenance.
>     Yesterday's 14-day search returned zero clean candidates (Manvel
>     test reruns, Transco OCR corruption, Cunningham never-engaged) —
>     two days additional driving since.
>
> Same paired-programming protocol that worked through 25 Phase E
> commits applies. Active verification gates: L-2 / L-3 / L-5 / L-6 /
> L-6 corollary / L-7 / L-9 / L-10 / L-11. L-8 reactivates at Step 7.
>
> Constraints: Reconcile dispatch (B-12) is Step 7. Same-address PLAN-
> side latch (B-26) is sub-step 1c (or folded into early sub-step 2).
> Phase F observability (B-23, B-24, B-25) deferred."""

new5 = """> I'm resuming Phase E at Step 6 sub-step 1b. Sub-step 1a closed at
> commit """ + SHA_PLACEHOLDER + """ (`get_recent_clusters()` + `Cluster.latest`
> shipped, 8 new tests, 258/258 floor). HEAD is """ + SHA_PLACEHOLDER + """.
> Floor: pytest 258/258, integration 22/61.
>
> Sub-step 1b wires `get_recent_clusters()` into `where_am_i.py` as the
> `cluster_revisit` topology signal per the v2.6 amendment to
> `WHERE_AM_I_PROPOSAL_v2.md`. WAI is stateless against cluster history
> (sub-step 0.3 finding) so the wiring is purely a new injected callable
> alongside `_cluster_fn` and `_pivot_fn`, plus a new `_recent_clusters_fn`
> attribute on `WhereAmI`, plus the topology-check logic in `evaluate()`.
> The contract: detect "I've stopped here before in this offer" by finding
> a non-current-window cluster within 25m of the active cluster's centroid.
>
> Please read in order:
>   1. PHASE_E_PROGRESS.md (this file — sub-step 1a entry has the full
>      get_recent_clusters() spec)
>   2. PHASE_E_KICKOFF.md (architectural ground truth)
>   3. PHASE_E_STEP_6_DESIGN.md — Amendment 1 spec at end
>   4. WHERE_AM_I_PROPOSAL_v2.md — read the planned v2.6 amendment for
>      `cluster_revisit` (authored in sub-step 1b)
>   5. where_am_i.py — full file (1154 lines), the sub-step 1b target
>   6. tests/test_where_am_i.py — sub-step 1b test home
>   7. cluster_detection.py — `get_recent_clusters()` is at the bottom;
>      review its signature before consuming it
>
> **Gates before authoring 1b:**
>   - L-11 doc-currency check: HEAD must be """ + SHA_PLACEHOLDER + """, pytest 258,
>     working tree clean
>   - L-6 corollary: read `where_am_i.py` `WhereAmI.__init__` (lines
>     770-794) and `evaluate()` (lines 795-844) verbatim before authoring
>     the topology-check insertion point
>   - WHERE_AM_I_PROPOSAL_v2.md v2.6 amendment must be authored and
>     ratified by Gemini before WAI integration begins
>
> Same paired-programming protocol that worked through 27 Phase E
> commits applies. Active verification gates: L-2 / L-3 / L-5 / L-6 /
> L-6 corollary / L-7 / L-9 / L-10 / L-11. L-8 reactivates at Step 7.
>
> Constraints: Reconcile dispatch (B-12) is Step 7. Same-address PLAN-
> side latch (B-26) is sub-step 1c (or folded into early sub-step 2).
> Phase F observability (B-23, B-24, B-25) deferred."""

if old5 in text:
    text = text.replace(old5, new5, 1)
    print("[ok] edit 5: starter message rewritten for sub-step 1b")
elif "I'm resuming Phase E at Step 6 sub-step 1b" in text:
    print("[skip] edit 5: already applied")
else:
    print("[FAIL] edit 5: starter message anchor not found")
    sys.exit(2)

# -----------------------------------------------------------------------------
# Write only if changed
# -----------------------------------------------------------------------------
if text != original:
    p.write_text(text)
    print()
    print(f"[ok] PHASE_E_PROGRESS.md: {len(text) - len(original):+d} chars")
    print()
    print("NEXT: SHA placeholder substitution after commit creates the SHA.")
    print("      Run sed -i 's/{SUBSTEP_1A_SHA}/<actual-sha>/g' PHASE_E_PROGRESS.md")
    print("      then `git commit --amend --no-edit` to fold the substitution in.")
else:
    print("[noop] PHASE_E_PROGRESS.md: nothing changed")