# Phase D Retrospective — `where_am_i.py` Continuous Awareness Primitive

**Status:** SHIPPED
**Branch:** `patch-00566a-unified-refinement`
**Commits:** `15b7a2f` (Phase D.0) → `5eed55c` (Step 5.7.2)
**Total work span:** April 2026
**Final pytest count:** 176/176
**Final integration baseline:** 22/61 (preserved exactly, all 39 failures pre-existing)

This document is the canonical record of Phase D's implementation. The
RFC `WHERE_AM_I_PROPOSAL_v2.md` captures the *reasoning at the time of
proposal*; this document captures *what actually shipped*. Future Phase E
planning conversations should start by reading both.

---

## Quick reference

| File | Lines | Purpose |
|---|---|---|
| `where_am_i.py` | 1141 | The WAI module: signals, plumbing, matchers, orchestrator |
| `pudo_types.py` | (extended) | `States` constants + `target_address` field on `WhereAmIResult` |
| `tests/test_where_am_i.py` | 1352 | 92 unit tests — signals, plumbing, matchers, orchestrator |
| `tests/test_scenarios.py` | 318 | 63 integration tests — TOML evidence locker harness + S31 forensic replay |
| `scripts/wai_smoke.py` | ~290 | Live-PG operational smoke (4 runs, manually invoked) |
| `scenarios/*.toml` | 31 files | TOML evidence locker, including S31 (Forum Park 7623) and S32 (implicit STACKED cancel, awaiting Phase E) |

---

## Architectural locks (Steps 1-4)

Twenty-eight Q-rulings made through paired-programming with Gemini. Condensed:

### Step 1 — Module structure
- **Q1** `on_target_road` removed from `_RoadTopology`; computed per-target in matchers
- **Q2** Class dispatch via `_CLASS_DISPATCH` table, not `if/elif`
- **Q3** STACKED tie-break: best confidence wins; primary breaks ties (stable `max()`)
- **Q4** POI stub returns `not_at_pudo` semantics with WARN log
- **Q5** Per-matcher debug logs gated behind `log.isEnabledFor(logging.DEBUG)`
- **Q6** `_cluster_fn` injection seam on `WhereAmI.__init__` for hermetic tests

### Step 2 — `_compute_road_topology` adapter
- **Q7** Single backward heartbeat scan via `pivot_context`; no separate snap cache
- **Q8** Ghost match radius 50m
- **Q9** `cluster_median_speed_mph` extra query at insert time deferred to Phase E
- **Q10** `WAI_SHADOW_ENABLED` flag deferred to Phase E (Phase D has no writes)

### Step 3 — Signal library
- Pure-function library + `_MatchOutcome` carrier + per-class orchestrators
- 6 `_signal_*` functions, all returning [0.0, 1.0]
- Linear proximity decay; triangle profile for cluster_duration; step functions for breadcrumb / on_target_road
- Haversine in Python (Section A.7) — Q ratified divergence from canonical SQL distance functions; only inside DIAGNOSE; writes still use canonical `app_private.*`

### Step 4 — `evaluate()` orchestrator + ghost cache
- **Q12** WAI is pure DIAGNOSE; no `suspected_pudos` writes; PLAN consumer (Phase E) owns all writes
- **Q1 (Step 4)** Tuple unpacking for ghost-cache SELECT — **REVERTED in 5.7.1** to dict access; production cursors use `RealDictCursor`
- **Q3 (Step 4)** `_MatchOutcome` enriched: `pudo_type`, `target_address`, `signals` dict
- **Q5 (Step 4)** `signals` dict on `_MatchOutcome` (internal); rendered into `reason` string for public exposure
- **Q6 (Step 4)** DB errors bubble; don't catch silently
- **Q7 (Step 4)** `States` constants in `pudo_types.py`; codebase sweep is backlog B-5
- **REFINE_DROPOFF** treated as IN_TRIP synonym; **REFINE_PICKUP** excluded (verdict label, not state)

### Locked constants
MIN_REPORT_THRESHOLD       = 0.4
STRONG_MATCH_CONFIDENCE    = 0.7
INTERSECTION_RADIUS_M      = 250.0
SINGLE_ROAD_RADIUS_M       = 500.0
NUMBER_ON_STREET_RADIUS_M  = 50.0
APARTMENT_RADIUS_M         = 300.0
GHOST_MATCH_RADIUS_M       = 50.0

`_CONFIDENCE_WEIGHTS` table: 4 address classes × 6 signals, every row sums to 1.0. Tunable post-shadow-mode.

---

## Implementation map
where_am_i.py (1141 lines)
├── Imports + module constants + _CONFIDENCE_WEIGHTS table
├── Section A — Signal library (~320 lines)
│   ├── _haversine_meters
│   ├── _signal_proximity
│   ├── _signal_breadcrumb_match
│   ├── _signal_cluster_tightness
│   ├── _signal_cluster_duration
│   ├── _signal_on_target_road
│   └── _signal_off_wire_pivot
├── Section B — Plumbing (~120 lines)
│   ├── _MatchOutcome (frozen dataclass, 8 fields)
│   ├── _weighted_confidence
│   ├── _render_reason
│   └── _validate_target (S27 fail-closed guard)
├── Section C — Matchers + dispatch (~250 lines)
│   ├── _RoadTopology (frozen dataclass)
│   ├── _compute_signals (helper)
│   ├── _build_outcome (helper)
│   ├── _match_intersection
│   ├── _match_single_road
│   ├── _match_number_on_street
│   ├── _match_apartment_complex
│   ├── _match_poi_stub (deferred to v1.1)
│   └── _CLASS_DISPATCH dict
└── Section D — Orchestrator (~450 lines)
├── _GHOST_CACHE_SQL constant
├── WhereAmI class
│   ├── init (cur, _cluster_fn, _pivot_fn injection seams)
│   ├── evaluate() — public entry point
│   ├── _compute_road_topology
│   ├── _match_current_pudo (Q3 STACKED tie-break)
│   ├── _targets_for_state
│   ├── _match_ghost_cache (read-only per Q12; dict access per 5.7.1)
│   ├── _not_at_pudo
│   ├── _at_unknown_pudo
│   └── _build_current_result

---

## Verification trail

| Test layer | Count | Status |
|---|---|---|
| Unit tests (signal library) | 37 | PASS |
| Unit tests (plumbing) | 14 | PASS |
| Unit tests (matchers + dispatch) | 17 | PASS |
| Unit tests (orchestrator) | 24 | PASS |
| Integration tests (scenario inventory, parameterized) | 62 | PASS |
| Integration tests (S31 forensic replay) | 1 | PASS |
| Integration baseline (test_integration.sh) | 22/61 | PRESERVED (39 known failures) |
| Live-PG smoke (4 runs) | 4/4 | PASS |

**The Vindication Test:** S31 (Forum Park 7623) — the canonical "BMOAR couldn't fire here" production failure case. Real captured driver session, 30-second stop at intersection 205m from geocoded pin. WAI produces:
status=at_current_pudo pudo_type=pickup confidence=0.786
reason: intersection conf=0.79 [breadcrumb_match=1.00, cluster_tightness=1.00,
cluster_duration=1.00, on_target_road=1.00, proximity=0.18, off_wire_pivot=0.00]

Confidence 0.786 clears the 0.7 STRONG_MATCH threshold that BMOAR's 200m proximity gate could never reach. The 4-Box DIAGNOSE primitive reproduces a successful diagnosis on the case that motivated its existence.

---

## Backlog (consolidated for Phase E reference)

| ID | Item | Phase E owns? |
|---|---|---|
| B-1 | Dynamic street-length proximity threshold for `single_road` | No (signal-library tuning) |
| B-2 | Multi-gate intersection convergence | No (signal-library tuning) |
| B-3 | (reserved) | — |
| B-4 | Q26 declined sweep | No (deferred constraint) |
| B-5 | Codebase sweep to use `States` constants (`pickup_confirm.py`, `decisions/`, `driver_status.py`, `monitor.py`) | No (refactor) |
| B-6 | Reorder S-scenarios by ID in canonical source | No (cosmetic) |
| B-7 | (consumed by TOML migration) | — |
| B-8 | Evaluate `REFINE_DROPOFF` deprecation post-shadow-mode | Yes |
| B-9 | Normalize TOML quote style across scenarios | No (cosmetic) |
| B-10 | Add `git status` clean-tree gate to phase closure protocol | No (process) |
| B-11 | Implicit STACKED cancellation detection (S32) | **Yes — primary Phase E concern** |

Plus: TargetSpec needs an `address` field (Phase F caller responsibility) so `WhereAmIResult.target_address` populates correctly. Currently always `None` because `getattr(target, "address", None)` always returns `None`.

---

## Lessons learned

Each lesson pairs with a protocol change for future paired-programming sessions.

### L-2 — Pre-commit paranoia gate
**Observation:** `git status` before staging catches scope drift, untracked files, and surprise modifications.
**Protocol change:** Every commit step includes a paranoia `git status` BEFORE `git add`, plus a confirming `git status` AFTER staging. Stop and surface anything unexpected.

### L-3 — Anchor-based patch scripts for surgical edits
**Observation:** Multi-line string anchors + sha-locked pre-conditions + `py_compile` post-conditions make in-place edits safe and predictable.
**Protocol change:** Any edit to a file >100 lines uses an anchor-based Python script with `--dry-run` default and `--apply` opt-in. SHA pre-checks refuse to run on unexpected baselines.

### L-5 — Trailing-newline guard
**Observation:** `cat >> file <<EOF` heredocs lose trailing newlines if the source ends without one. Idempotent fix: `[ -n "$(tail -c 1 FILE)" ] && echo >> FILE`.
**Protocol change:** Every file-writing step ends with the trailing-newline guard. POSIX text files end with a newline.

### L-6 — Inspect production artifacts before authoring assertions (NEW, Phase D Step 5.6)
**Observation:** Step 5.6's first attempt assumed S31.toml's `[fixture]` block had a key called `expected_signals`. Production schema actually used `expected_signals_minimum` with floor semantics. The smoke environment's reconstructed TOML masked the mismatch.
**Protocol change:** Before authoring tests/assertions against any real production artifact (TOML scenario, JSON fixture, DB row, etc.), `cat` the actual file and read its real shape. Reconstruction from memory is unreliable.

### L-7 — Cross-check architectural rulings against production conventions (NEW, Phase D Step 5.7.1)
**Observation:** Step 4's Q1 ruling chose tuple cursor unpacking "for performance." Audit during Step 5.7 revealed every production caller uses `psycopg2.extras.RealDictCursor`. The ruling was made without checking what production actually does. Bug would have crashed on first ghost match in shadow mode.
**Protocol change:** Before locking any architectural ruling about runtime types (cursor types, dataclass shapes, return-type conventions), run a 5-minute `grep` against production conventions. If the grep contradicts the proposed lock, surface for re-discussion BEFORE locking.

### L-8 — Live-PG smoke is non-negotiable for DB-coupled code (NEW, Phase D Step 5.7)
**Observation:** All 113 unit tests passed, but the cursor-type bug was invisible to them because `_FakeCursor` returned tuples. The bug only surfaced when WAI ran against real PG via the smoke script.
**Protocol change:** Every phase that ships DB-coupled code MUST include a live-PG smoke step before commit. Unit tests against fakes cannot exercise cursor-type, schema-name, or timezone assumptions; only real PG can.

---

## What remains before commercial launch

Phase D delivered the DIAGNOSE primitive. The full v1.0 commercial release requires:

### Phase E — PLAN consumer (`pudo_planner.py`)
- Subscribes to WAI's per-heartbeat output
- Owns `suspected_pudos` INSERT writes (ghost-cache population per Q12)
- Owns `sm_transition` calls (state machine writes)
- Implements temporal pattern detection ("stable for N heartbeats")
- Implements **B-11 implicit STACKED cancellation detection** (S32 forensic scenario)
- Decides which weak-confidence (0.4-0.7) matches to wait on vs. fire on

### Phase F — Shadow-mode integration (`driver_heartbeat.py`)
- Calls WAI in parallel with every real heartbeat
- Logs WAI output alongside BMOAR output for comparison
- Behind `WAI_PLANNER_ENABLED_DRIVERS` feature flag (sole gate per Gemini Apr 25 ruling)
- Powers the shadow-mode dashboard for tuning the `_CONFIDENCE_WEIGHTS` table
- Adds `address` field to `TargetSpec` so `WhereAmIResult.target_address` populates

### Phase G — BMOAR Path A/B deprecation
- Once shadow-mode data shows WAI matches or exceeds BMOAR's fire rate without false positives
- Deprecation of arc-banding-era detectors
- Eventual review of B-8 (REFINE_DROPOFF deprecation)

---

## Architectural closure

The 4-Box Controller (DIAGNOSE / PLAN / EXECUTE / OBSERVE) is no longer aspirational. The DIAGNOSE primitive is real, tested, version-controlled, and verified against the production database with the production cursor convention.

When Phase E begins, every Step 1-4 architectural lock is composed into working code with a test trail proving it. The S-scenario evidence locker (`scenarios/*.toml`) gives Phase E a forensic test bed already populated with cases — including S32, the canonical implicit-STACKED-cancel case that PLAN must handle.

Phase D shipped.
