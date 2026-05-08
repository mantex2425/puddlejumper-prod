# Phase 2c.2 Item 3 Handoff — Resume Brief

**For:** the next architecture-chat session continuing this sprint
**From:** the chat that just shipped Item 2 (commit `9c326fc`)
**Date:** 2026-05-08
**Branch:** `phase-2c-2-tad-exit-4tools` off `f3f5dc9` on `demolition-2026-05-04`

---

## Read first (paste at session start)

Documents to load before any work:

1. **`docs/PHASE_2C_2_SPRINT_BIBLE.md`** — operative spec for this sprint. **READ END TO END.** Item 3 is the work it calls "the riskiest surgery in the sprint."

2. **`docs/SESSION_PROTOCOL.md`** — paired-programming workflow, paste-safety rules, lessons L-1 through L-22.

3. **`CANONICAL_RULES.md`** — eternal product law (coordinate functions, UTC, etc.). PuddleJumper Canonical Standards v2.1 covers Section III (UTC-mandatory), Section IV (TAD as hard gate), Section V (forensic JSONB), Section VI (Auto Nail It only).

After loading docs, run a quick git recon:

```bash
cd ~/puddlejumper-prod && git log --oneline f3f5dc9..HEAD
```

You should see eight commits ahead of base:

```
9c326fc phase 2c.2 brain: head 4 + patch 2c (poi witness signals fully wired)
1d84248 docs: phase 2c.2 sprint bible amendment for lost mode + inferred dropoff
a77c211 docs: phase 2c.2 sprint handoff for next session
de2ba5d phase 2c.2 brain: tad.py Part 2 + Lost Mode (evaluate_tad_gate)
61094d0 phase 2c.2 brain: tad.py Part 1 (compute_offer_expectations)
386ba3f phase 2c.2 prereq: extend Offer with pickup_minutes/trip_minutes
7a236a3 schema: phase 2c.2 migration applied to prod
5be275e docs: phase 2c.2 sprint bible (operative spec, supersedes kickoff)
```

Pytest baseline: **492/492 passed** (was 466 pre-Item-2; Item 2 added ~26 tests).

---

## What's landed since the last handoff

### Item 1 (commit `1d84248`): Bible amendments

Six amendments to the Sprint Bible covering Lost Mode and inferred-dropoff:

- **Rule 3 transformation** (3a Normal Mode + 3b Lost Mode subsections, colocated)
- **Rule 7 (NEW)**: Lost Mode triggers and mechanics — `narrative_blindness` (caller-driven) + `narrative_violation` (data-driven, >115% odometer overshoot), the 85%/115% distance window, time-doesn't-trigger note
- **Rule 8 (NEW)**: Inferred-dropoff anchor recovery — trigger conditions, inferred values, GPS fallback for no-intermediate-cluster case, persistence requirement (mechanism deferred to Item 3)
- **Rule 9 (excluded)**: Price Radar weighting for no-show pickups — Phase 2g scope, captured as paragraph in "What this sprint does NOT include"
- **Step 4.5 line update**: tristate verdict expansion (`True`=Normal pass, `False`=skip, `None`=Lost Mode)
- **Step 6 line update**: dual commit rule reference

### Item 2 (commit `9c326fc`): Head 4 + Patch 2c wiring

Production code (`where_am_i.py`):
- `CLASS_TO_TYPE_MAP` constant — Houston-validated, three classes share `_STREET_ADDRESS_DESTINATIONS` (number_on_street + single_road + intersection); apartment_complex narrow; poi destination-typed
- `_signal_poi_type_match()` function — Head 4 boolean witness, sibling to `_signal_poi_match`, NOT in `_CONFIDENCE_WEIGHTS`
- `MatchOutcome` extended with `poi_type_match: Optional[bool]` + `poi_type_witness: Optional[str]` (10 → 12 fields)
- `_build_outcome` signature extended with 4 optional witness kwargs (5 → 9 params)
- 4 real per-class matchers gain `pois: Optional[list] = None` parameter and call both witness signals
- `_evaluate()` Step 3.6 inserted: cluster-scoped POI fetch via `poi_service.get_pois_near_cluster`, mantra-aligned error handling (empty list on any exception)
- `_match_poi_stub` left untouched (out of sprint scope)

Tests:
- `TestSignalPoiTypeMatch` (12 tests in `test_bead_on_wire.py`) — covers all 4 audit cases (Excel Dental dentist, Pappasito's restaurant, tire shop car_repair, U of H university), apartment_complex narrow set positive + negative, first-match-wins, radius filter, dispatch-key consistency
- `TestSignalPoiMatch` (8 tests in `test_bead_on_wire.py`) — closes patch 2a/2b coverage gap, covers all 3 heads
- `TestWitnessWiring` (6 tests in `test_where_am_i.py`) — proves all 4 real matchers thread `pois` through correctly, witnesses populate via `_build_outcome`, MatchOutcome arithmetic gate

Test floor: 466 → 492.

---

## Findings to act on (READ BEFORE AUTHORING)

### Finding 1: Bible recon evidence has drift, do not trust it without re-verification

The Bible's "Recon evidence" section describes some post-Item-3 state, not post-Phase-1 state. Specifically:
- **`Cluster` dataclass had 6 fields, Bible said 5** (missing `latest: datetime`). Caused a Script 2 test failure during Item 2.
- **`TargetSpec` has 4 fields, no `address` field** — see Finding 3.
- **Step 4.5 (TAD bouncer) is NOT in `_evaluate()` body** — Bible recon implied it was. Item 3 lands it.

**Discipline for Item 3:** every dataclass and function signature you touch gets `grep -A 30 "^class X:"` or equivalent against production code BEFORE you author code that constructs it. The Bible's recon is reference material with known drift; production code is the only source of truth. Pre-measure all transforms locally before submitting an apply script (lesson from Item 2 calibration round-trips).

### Finding 2: Step 4.5 (TAD bouncer) lands in Item 3, not Phase 1

The Bible recon claimed Step 4.5 was Phase 1 work landed at `de2ba5d`. It wasn't. Phase 1 shipped `tad.py` (the module) and the schema migration, NOT the `evaluate()` integration. Current `_evaluate()` body has Steps 1, 2, 3, 3.5, 4, 5, 6 — no 4.5.

Item 2 inserted **Step 3.6 (POI fetch)** between 3.5 (Memory Eye) and 4 (Map). This was deliberate to leave runway for Step 4.5. Item 3's TAD bouncer goes between Step 4 (Map: candidate generation) and Step 5 (Evaluate: per-class dispatch), as the Bible specifies.

### Finding 3: Patch 2c name-witness signal is DORMANT until TargetSpec gains an `address` field

This is the most architecturally significant finding for Item 3.

**Production reality:** `TargetSpec` has 4 fields: `lat, lng, address_class, named_roads`. There is no `address` field. The 7 sites in production code that read it use `getattr(target, "address", None)` defensively, anticipating future plumbing.

**Consequence for Patch 2c:** Item 2's wiring threads `getattr(target, "address", "") or ""` into `_signal_poi_match`. In production, this evaluates to empty string. The function short-circuits at line `if not pois or not target_address: return 0.0, None`. Patch 2c is shipped but **dormant** — `MatchOutcome.poi_match` is always `0.0` and `poi_witness` is always `None` in production.

**Consequence for Head 4:** Head 4 (`_signal_poi_type_match`) uses `address_class` which IS on `TargetSpec`. Head 4 is **fully live** in production.

**Consequence for Item 3's Rule 3b (Lost Mode commit rule):**
- Lost Mode commit rule: `weighted >= 0.85 AND poi_type_match TRUE`
- The mandatory POI requirement is `poi_type_match` (Head 4), which is live.
- Patch 2c name-witness contributes to Normal Mode's elevator condition (`weighted >= 0.80 AND poi_type_match`) — but it's dormant, so it never lifts the floor in production.
- **Net: Lost Mode is fully functional with Head 4 alone. Normal Mode's elevator is functional with Head 4 alone. Patch 2c will activate automatically when `TargetSpec.address` lands.**

**For Phase 2c.3+ (NOT this sprint):** Add `address: str` field to `TargetSpec`, update 4 production constructors (`driver_heartbeat.py:99-117`, `scripts/replay_pudo.py`, `apply_patch_replay_target_spec.py`). Patch 2c activates automatically. No `where_am_i.py` change needed (defensive `getattr` already in place).

The dormancy is documented in `tests/test_where_am_i.py::TestWitnessWiring`'s class docstring, in case Item 3 asserts on `poi_match` somewhere.

### Finding 4: Pytest baseline is 492, not 466

Item 3's acceptance criteria need to track against 492 + new Item 3 tests, not 466. The Bible's "Pytest green: target ~485-490" range was authored against the 466 baseline; with Item 2's +26, the target shifts to ~510-515 + Item 3's additions.

---

## What's left — Phase 1 (architecture-chat, this session)

The Bible's Item 3 spec stands. Three deliverables:

### Item 3a: Step 4.5 TAD bouncer integration

In `_evaluate()` body of `where_am_i.py`. After Step 4 (Map: candidate generation), call `tad.evaluate_tad_gate(...)`. The caller assembles `OfferTadState` per offer from `offer_history` rows fetched alongside the queue.

`tad.evaluate_tad_gate()` returns tristate verdicts per offer:
- `True` → Normal Mode pass (driver in 85%–115% window) → apply Rule 3a
- `False` → skip candidate (driver hasn't arrived)
- `None` → Lost Mode (narrative blindness or violation, see Bible Rule 7) → apply Rule 3b

### Item 3b: Lost Mode detection (caller side)

The caller decides when to set `lost_mode=True` on `evaluate_tad_gate()`:
- No prior anchor available (queue empty / GC'd / stacked offer with no prev confirmation)
- Inferred-dropoff fallback (see Item 3d)

When `lost_mode=True`, all offers in the queue receive `passed=None`.

### Item 3c: Per-leg dispatch + dual commit rule

In Step 5 (Evaluate), only run spatial scoring for offers where `verdict.passed in (True, None)`. Skip offers where `verdict.passed is False`.

In Step 6 (Reduce), apply commit rule per offer:
- `verdict.passed is True` → Normal Mode: commit if `weighted >= 0.90 OR (weighted >= 0.80 AND poi_type_match)`
- `verdict.passed is None` → Lost Mode: commit if `weighted >= 0.85 AND poi_type_match TRUE`

### Item 3d: Inferred-dropoff logic

When pickup B's cluster confirms (during Step 6 commit, for a pickup target), look back at ride A. If ride A has `actual_pickup_at` set but `actual_dropoff_at IS NULL`, AND current UTC time ≥ `ride_A.expected_dropoff_arrival_time`, AND current UTC time ≥ `ride_B.expected_pickup_arrival_time`, infer that ride A's dropoff happened.

Persist:
- Inferred dropoff time = current UTC timestamp (per v2.1 Section III)
- Inferred dropoff location, in priority order:
  1. Last known cluster centroid before pickup B's cluster
  2. **Fallback:** last GPS coordinate received prior to cluster B formation (no accuracy threshold)

The persistence mechanism (column on `offer_history` vs flag in forensic blob vs separate event table) was deferred from the Bible amendment — locks at this Item 3 implementation time. Recommend reading Bible Rule 8's "Persistence requirement" paragraph and proposing a mechanism through Gemini before authoring.

### Item 3e: Forensic blob assembly

Build the `tad_decision_context` JSONB per the schema-documented structure, including TAD verdicts, spatial scores per signal, weighted_confidence, poi_type_match boolean, elevator_triggered or lost_mode flags, final_verdict ("COMMIT" | "SKIP").

---

## What's left — Phase 2 (Claude Code, separate session)

Per the Bible's existing scope. Unchanged by Item 1 / Item 2.

---

## Acceptance criteria for Item 3 + sprint completion

### Tests
- Pytest green: target ~510-525 (492 baseline + ~20 Item 3 integration tests + ~10-15 Phase 2 tests)
- Item 3 specifically tests: Lost Mode rule fires (mock TAD verdict with `passed=None`, verify stricter commit rule), inferred-dropoff logic with both centroid and GPS-fallback paths, forensic blob structure

### Deploy + forensics
Bible's existing acceptance criteria stand.

---

## Operational protocol reminder

- Paired programming: Claude proposes → Gemini reviews → consensus → execute
- Apply scripts in `~/puddlejumper-prod/tmp/` (gitignored)
- Migrations in `~/puddlejumper-prod/migrations/` (committed) with `YYYY-MM-DD_descriptive_name.sql` naming
- Recon files in `/tmp/` (Linux ephemeral)
- Pytest via venv: `source ~/puddlejumper-prod/venv/bin/activate` then `python3 -m pytest -x --tb=short`
- File transfers: download from chat UI, scp from laptop. Verify md5 BEFORE running any apply script.
- L-3 envelope: arithmetic gates (Phase 2), AST parse (Phase 3), subprocess smoke import via `REPO/venv/bin/python3` (Phase 3)
- Pre-measure all transform deltas locally before submitting apply scripts (lesson from Item 2)

---

End of handoff.