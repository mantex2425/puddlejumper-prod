I'm resuming Phase E at the sub-step 1c closeout doc refresh, then proceeding to sub-step 2 (T70-T74 Five Pillars synthetic-heartbeat block). Sub-step 1c closed at commit `72cf951` (Memory–Signal–Latch trilogy complete; B-26 PLAN-side latch shipped; 5 files changed, +388/-19; 277 → 290 floor; integration unchanged at 22/61). HEAD is `72cf951`.

**Floor:** pytest 290/290, integration 22/61 (preserved through sub-step 1c).

**What this session ships.** Two commits, in order:

1. **PHASE_E_PROGRESS.md doc refresh for sub-step 1c closeout** — single doc commit, mirroring the precedent set by `840ae49` after sub-step 1b.3.
2. **Phase E Step 6 sub-step 2: Five Pillars synthetic-heartbeat block (T70-T74)** — five integration tests, one per pillar (S31-S35), each feeding a synthetic heartbeat sequence to assembled `WAI.evaluate() → PudoPlanner.consume()` and asserting the planner emits the expected PlannerDecision. Per PHASE_E_PROGRESS.md sub-step 2 spec.

**Critical context: Phase F is NOT next.** Per the disciplined phased rollout, Phase F (heartbeat-loop integration into `driver_heartbeat.py`) cannot ship until Phase E sub-steps 2-8 + Step 7 are complete. The trilogy is in the codebase but dormant — `driver_heartbeat.py` does not call WAI or PudoPlanner today. Sub-step 2's T70-T74 are the FIRST tests that exercise the assembled WAI→PLAN dispatch chain at the integration level. They must ship before Phase F surgery on the production heartbeat endpoint.

---

## Doc refresh patch design (Commit 1)

Mirrors the structure of `refresh_progress_1b3.py` (the prior closeout refresh, commit `840ae49`). Single doc commit, ~9 patches.

### Patches required

1. **Header status block (lines 3-7).** Bump "sub-step 1b.3 closeout" → "sub-step 1c closeout". Update commit reference `a68447b` → `72cf951`. "Replaces" line: post sub-step 1c, pre sub-step 2.

2. **Read-these-in-order section (11-49).** References to sub-step 1c being NEXT → sub-step 2 is NEXT. Update line counts:
   - `pudo_types.py` 325 → 326
   - `where_am_i.py` 1275 → 1286
   - `pudo_planner.py` 1041 → 1159
   - `tests/test_pudo_planner.py` 1653 → 1892
   - Add `tests/test_where_am_i.py` (got the haversine_meters rename touches)

3. **Current state HEAD/floor block (lines 53-85).** HEAD `72cf951`. pytest 277 → 290. Integration 22/61 unchanged. Untracked file list expanded with `apply_substep_1c.py`, `apply_substep_1c_fix.py`. Narrative paragraph rewrites for "sub-step 2 is next" + Memory–Signal–Latch trilogy SHIPPED.

4. **Commit lineage table.** Append rows after `a68447b`:
   - `| 840ae49 | 6.1b.3 doc | PHASE_E_PROGRESS.md refresh for sub-step 1b.3 closeout per L-11 |`
   - `| 72cf951 | 6.1c | B-26 PLAN-side latch — Memory-Signal-Latch trilogy complete (290 floor) |`

5. **NEW Sub-step 1c SHIPPED section.** Insert before the existing "Sub-step 1c — NEXT" section at line 484. Mirrors structure of sub-step 1a/1b SHIPPED sections. Captures:
   - The trilogy completion narrative (Memory + Signal + Latch all shipped)
   - Path A coordinate-only design (B-15 unshipped, address comparison defer)
   - `PUDO_COLOCATION_THRESHOLD_M = 30.0` with L-10 cat-2 provenance + B-24 telemetry contract reference
   - `_is_same_pudo_colocation` pure helper
   - `_build_noop_same_pudo_no_revisit` builder (12th action variant)
   - Latch guard placement (dispatch-level, not builder parameter — sidesteps L-6 corollary)
   - 13 new tests (TestIsSamePudoColocation 7 + TestSamePudoColocationLatch 6)
   - REPL-probed boundary triple at 29.5/30.0/30.5m per L-9 corollary
   - Sharp-cutoff verification at integration level
   - Pickup-branch-unaffected safety property
   - Floor 277 → 290 (+13)
   - apply_substep_1c.py 14-patch L-3 anchor script + apply_substep_1c_fix.py recovery

6. **Reframe stale "Sub-step 1c — NEXT" section.** Title becomes `### Sub-step 2 — Five Pillars synthetic-heartbeat block (T70-T74) — NEXT`. Body replaced with sub-step 2's spec (currently lives at lines 508-526).

7. **L-9 corollary extension subsection — REPL probe convergence requirement.** Captures the boundary-precision recovery from sub-step 1c apply: 50-iteration probe gave +2e-6 over threshold; floating-point precision noise. 80-iteration probe gave -1.6e-10. Protocol: REPL probes for boundary fixtures must converge to ~1e-10 or smaller. Append after existing L-9 corollary section.

8. **L-6 corollary extension SECOND STRIKE notation.** The 1c authoring caught the rename inventory miss in `tests/test_where_am_i.py` AT APPLY TIME (pytest collection error: ImportError). Promotes to L-3 anchor-script authoring CHECKLIST item: any rename or signature change must `grep -r` the entire repo for invocation sites, not just intra-module. Update existing L-6 corollary extension subsection with this generalization.

9. **Lineage table duplicate cleanup.** Pre-existing crud at lines 124-128 (`86ea117`, `5a86f2e`, `7863b11`, `7a8616a` each appear twice). Flagged at the prior 1b.3 doc refresh, deferred. Now in scope. Delete the duplicate rows.

10. **Concrete first-message-of-new-chat starter (end of file).** Rewrite for next session opening at sub-step 3 (T75-T79 round-trip block), or whatever sub-step 2 closeout points to.

### Expected line delta

Per the prior 1b.3 doc refresh (+48 lines), this refresh likely lands +60 to +90 lines (slightly bigger because the trilogy SHIPPED section is rich, plus new L-9 corollary subsection, plus removing duplicate rows nets only -5). Envelope per file: pre-line-count via `wc -l PHASE_E_PROGRESS.md` at session-open; widen apply-script envelope appropriately based on actual prediction.

---

## Sub-step 2 design (Commit 2)

Per PHASE_E_PROGRESS.md lines 508-526:

> Five integration tests, one per pillar. Each test feeds a synthetic heartbeat sequence to assembled `WAI.evaluate() → PudoPlanner.consume()` and asserts the planner emits the expected PlannerDecision.
>
> | Test | Pillar | Asserts |
> |------|--------|---------|
> | T70  | S31    | Geometric — Forum Park 7623 fixture replay → fire_pickup    |
> | T71  | S32    | Structural — synth secondary pickup → fire_stacked_swap     |
> | T72  | S33    | Temporal — long stop then departure → fire_retroactive      |
> | T73  | S34    | Collapse — single-heartbeat fire (B-13 deferred)            |
> | T74  | S35    | Inverse — synth primary pickup in STACKED → fire_stacked_revert |
>
> T70 uses the existing `tests/fixtures/7623_heartbeats.json`. T71-T74 require new synthetic fixtures (no production capture available; Phase F shadow-mode adds forensic replay later). All fixtures declare provenance per L-9.

### Forensic reads required at session-open (L-6)

Before any patch authoring, read in this order:

1. **`PHASE_E_PROGRESS.md`** at HEAD (post-doc-refresh) — full sub-step 2 spec.
2. **`tests/fixtures/7623_heartbeats.json`** — does it exist? structure? T70 reuses this; the others mirror its shape.
3. **`tests/test_pudo_planner.py`** existing fixture conventions — `_FakeClock`, `_wai`, `_snapshot`, `_state` factories. Latch tests at end of file (TestSamePudoColocationLatch) are the most-recent precedent for assembled-dispatch testing.
4. **`tests/test_where_am_i.py`** existing fixture conventions — synthetic heartbeats fed to `WhereAmI.evaluate()`. Particularly the `TestEvaluate` class and `_FakeCursor` for cluster-history mocking.
5. **WAI's `evaluate()` interface** in `where_am_i.py` — exact call signature, what state needs to be assembled (driver_id, current_offer, GPS sample, etc.).
6. **PudoPlanner.consume() and _decide()** in `pudo_planner.py` — already read in this session; refresh as needed for the integration test layer.

### Design questions for sub-step 2 session-open (resolve before authoring)

- **Q1: Single fixture file vs per-test fixtures?** T70 has a real production fixture (`7623_heartbeats.json`). T71-T74 are synthetic. Best practice per L-9: per-test docstrings declare provenance. Should each synthetic fixture live as a separate JSON file in `tests/fixtures/`, or inline as Python data structures in the test class? Inline is more discoverable; separate JSONs match the precedent set by 7623.

- **Q2: Per-test heartbeat sequence length.** Real production heartbeats arrive every ~5 seconds. The Forum Park fixture has some number of heartbeats covering some time window. Synthetic fixtures need parity — long enough to exercise the full N_HEARTBEATS_TO_FIRE=3 stable-match logic plus surrounding context. Probably 8-12 heartbeats per fixture. Confirm at session-open against the 7623 fixture's actual length.

- **Q3: Where do TestFivePillars tests land in the file?** End of file (after TestSamePudoColocationLatch from sub-step 1c) is the natural spot. Or: dedicated section header `# Sub-step 2 — Five Pillars synthetic-heartbeat block`. Prefer the section header for discoverability.

- **Q4: Does `WAI.evaluate()` in tests need real cluster-history mock data?** The latch tests mocked clusters via `_FakeCursor` returning empty lists. T72 (Temporal — long stop then departure) and T75-T79 (round-trip) inherently need cluster history. T70-T71-T73-T74 might not. Each test's fixture should declare what cluster history (if any) it injects.

- **Q5: Test naming convention.** PHASE_E_PROGRESS.md uses T70-T74 as identifiers. Within Python: `test_t70_s31_forum_park_fixture_fires_pickup` or `test_geometric_pillar_fires_pickup`? Convention should be checkable from `pytest -k` filtering. Recommendation: descriptive names with comment annotation citing the T-number for cross-reference.

### Floor target

277 + 13 (sub-step 1c) = 290 baseline. Sub-step 2 adds 5 tests → **295 target**. Sharp prediction; envelope per file pre-authoring once forensic reads complete.

---

## Carry-over from sub-step 1c

### Lessons captured today (ship in commit 1's L-9 corollary + L-6 corollary extensions)

- **L-9 corollary refinement:** REPL probes for boundary fixtures must converge below floating-point precision noise. Empirical guidance: 80 binary-search iterations; target `|haversine_meters - threshold| < 1e-10`. The 50-iteration probe used in 1c authoring gave +2e-6 above threshold, which failed `<=` semantics.

- **L-6 corollary extension second strike:** Cross-file rename inventory. Sub-step 1c renamed `_haversine_meters → haversine_meters` in `where_am_i.py` and inventoried 4 internal call sites correctly, but missed 4 sites in `tests/test_where_am_i.py` (1 import + 3 test calls). Caught at apply-time via pytest ImportError. Generalization: any patch that renames a public/private symbol or modifies a method signature must `grep -rn "<symbol>" --include="*.py"` against the entire repo before applying, not just within the modifying module. Promote to L-3 anchor-script authoring checklist.

### Backlog notes

- `apply_substep_1c.py` and `apply_substep_1c_fix.py` are forensic-record artifacts in the working tree as untracked files. Per L-3 they can be added in-tree at next refresh OR left untracked indefinitely. Decision deferred; both options are valid per protocol.

---

## Gates before authoring (L-11, every session-open)

1. **L-11 doc-currency check:** HEAD must be `72cf951`. pytest 290. Integration 22/61. Working tree clean except untracked patch scripts (`apply_substep_1b2_test_fix.py`, `apply_substep_1b3.py`, `apply_substep_1b3_fix.py`, `apply_substep_1c.py`, `apply_substep_1c_fix.py`, `apply_v26_amendment.py`, `refresh_progress_1b2.py`, `refresh_progress_1b3.py`, `tmp/`).

2. **Read PHASE_E_PROGRESS.md verbatim.** Specifically the unchanged "Sub-step 1c — NEXT" section (still says NEXT pre-doc-refresh; this session's doc refresh fixes it) and the sub-step 2 spec at lines 508-526.

3. **Confirm sub-step 2 design questions Q1-Q5 with Andrew.** No code authored until decisions locked.

4. **Confirm doc-refresh patch list (1-10 above) with Gemini before authoring `refresh_progress_1c.py`.** Same proposal-as-file → ratification protocol used twice this session (1b.3 doc refresh, 1c apply script).

---

## Constraints unchanged

- **Phase F is NOT next.** Heartbeat-loop integration is gated on Phase E sub-steps 2-8 + Step 7 complete. The trilogy is dormant in code; `driver_heartbeat.py` does not call WAI/PudoPlanner today. Premature integration risks shipping untested-at-integration code into a production-critical endpoint.
- **Reconcile dispatch (B-12) is Step 7.** Sub-step 5 (T90-T99) reserves test slots only.
- **Phase F observability (B-23, B-24, B-25) deferred** until Phase F itself.
- **L-8 (live-PG smoke) reactivates at Step 7.** Phase E sub-step 2 is database-blind per R2.

---

## Same paired-programming protocol

Active gates: L-2 / L-3 / L-5 / L-6 / L-6 corollary / L-6 corollary extension (now CHECKLIST item per second-strike) / L-7 / L-9 / L-9 corollary (now with convergence-iteration guidance) / L-10 / L-11.

Cycle: Claude proposes → Gemini reviews (Grok occasionally) → consensus → Claude provides execution instructions → Andrew runs gates → commit + push immediately, never bundle.

Output formatting per Andrew's preferences:
- Wrap SQL in psql for CLI execution
- Long outputs go to `/tmp/<descriptor>.txt` then `cat` (avoid heredoc size issues)
- Apply scripts use anchor-based L-3 envelope: Phase 1 verify + idempotency / Phase 2 in-memory transform + per-file delta gate / Phase 3 atomic disk write + read-back + sentinel sweep
- File transfers via `/mnt/user-data/outputs` + present_files + scp when scripts exceed ~200 lines

---

## First concrete actions (this session opening)

1. L-11 doc-currency check on session-open per protocol.
2. Read PHASE_E_PROGRESS.md verbatim — specifically lines 484-526 (sub-step 1c spec + sub-step 2 spec).
3. Read `tests/fixtures/` directory — confirm `7623_heartbeats.json` exists and inspect structure.
4. Decide Q1-Q5 with Andrew.
5. Propose `refresh_progress_1c.py` patch script for Gemini ratification (proposal-as-file, present_files, scp to VM).
6. Author and ship Commit 1 (doc refresh).
7. Forensic read for sub-step 2: `WAI.evaluate()` interface, existing test idioms, fixture conventions.
8. Propose sub-step 2 patch design for Gemini ratification.
9. Author and ship Commit 2 (T70-T74).

Estimated session length: 3-4 hours of paired-programming flow. Two atomic commits target.

---

## Trilogy status reminder

The Memory–Signal–Latch trilogy is complete:
- **Memory** (1a, `6fe454a`): `get_recent_clusters()` + `Cluster.latest`
- **Signal** (1b, `6e1d60f`/`6522ba5`/`a68447b`): `cluster_revisit` topology in WAI
- **Latch** (1c, `72cf951`): B-26 PLAN-side latch + 12th action `noop_same_pudo_no_revisit`

Sub-step 2 is the FIRST integration test of these three primitives composing correctly. T70-T74 are not just "tests" — they are the proof-of-correctness for the contract assembly that Phase F will eventually wire into production. Treat them as load-bearing.