# PuddleJumper Launch Sprint — 30 Days to Live

**Authored:** 2026-04-28 evening
**Updated:** 2026-04-29 morning — pivot per voice session
**Floor at start:** pytest 290/290, integration 22/61. HEAD `48297bf`.
**Mantra:** if it isn't code or a test to prove the code, we don't write it.

---

## Why this exists

BMOAR is broken. Houston shift 2026-04-27 proved it across 5 cases (corner-lot S32 failures, 584m geocode drift, TargetSpec Vacuum). The trilogy (Memory–Signal–Latch) ships in code at `72cf951` but is dormant — `driver_heartbeat.py` does not call it.

**The single critical question:** can the system identify a PUDO reliably?

Everything else — state machine, reconciliation, stacked rides, edge cases — is downstream of that one capability. We're cutting all synthetic-fixture work and validating PUDO identification against real fares with logging.

---

## Forensic artifacts we keep (already shipped, do not re-author)

- `PHASE_E_PROGRESS.md` (commit `773aa49`) — trilogy SHIPPED, lessons-learned, B-27 backlog
- `FORENSIC_2026_04_27_HOUSTON_SHIFT.md` (commit `773aa49`) — 5 cases, L-9 fixture provenance for future work
- `FORENSIC_2026_04_28_HOUSTON_WAYS_AUDIT.md` (commit `48297bf`) — `routing.houston_ways` audit, B-27 layer 4 validation

These are done. We cite them, we don't extend them.

---

## What changed in the 2026-04-29 pivot

The previous sprint plan had three sprints starting with T70-T74 (parachute check). Voice session this morning recognized that:

1. **Synthetic fixtures (T70-T74) don't validate against real PUDOs.** Real driving with logging does the same job better.
2. **WAI classification is the first filter** that determines which PUDO identification rule fires. Validating that classification against real fares is the actual test.
3. **A truth table built from production data** lets us find logic contradictions empirically, not theoretically.
4. **Adjacency logic must use dynamic street-segment lookup**, not hardcoded 200m radius.

T70-T74 sprint dropped. New sprint structure focuses on getting WAI live with logging today.

---

## The two sprints

### Sprint 1 — Wire WAI live with logging (TODAY)

**Goal:** WAI is the active decision engine. Every heartbeat is logged with full decision context. Drive real fares. Build the truth table empirically.

**Scope:**

1. **INDEX.md** at repo root — portable manifest of all decision documents. Pasteable at start of any chat session.

2. **Log table `pudo_decision_context`** — one row per heartbeat. Captures: timestamp, trip_id, driver GPS, WAI classification (straightroad/intersection/POI/not_at_pudo), rule_fired, segment data, identified_as_pudo, ground_truth fields filled post-ride.

3. **Wire `driver_heartbeat.py`:**
   - Replace BMOAR Path A/B detector calls with `WhereAmI.evaluate()` + `PudoPlanner.consume()`
   - Map all 12 `PlannerDecision` actions to existing dispatch handlers
   - **Reconciliation path:** when WAI returns `at_unknown_pudo` and state is IDLE, synthesize TargetSpec from the cluster WAI just produced. Uses cluster detection we already have — no 60-second timer.
   - Wrap WAI call in try/except: any exception falls back to safe inaction (log + no-fire). One bug must not brick the heartbeat handler.
   - Insert log row into `pudo_decision_context` on every evaluation.

4. **Drive single-trip rides.** Deliberately accept some declined offers (test bypass case). Turn system on/off mid-trip if curious.

5. **End-of-night query** the log table. See agreement rate, see contradictions.

**Done when:** `driver_heartbeat.py` calls WAI on every heartbeat, logs every decision, isolates errors. Existing pytest stays green. At least one shift driven with logging on.

**Estimated time:** 4-5 hours of focused work before driving. Then a shift.

---

### Sprint 2 — Iterate based on production data

**Goal:** turn the truth table into rule improvements.

**Scope:** depends entirely on what the log table shows after a few shifts. Likely candidates:

- **Adjacency logic (dynamic street-segment lookup)** if straightroad classification fires false-negatives on parking lots
- **WAI classification refinement** if `at_unknown_pudo` fires too often or too rarely
- **Reconciliation tuning** if the bypass-after-decline case shows logging gaps
- **3 mislabeled `routing.houston_ways` rows** cleanup (Terramont/Player Bend) if adjacency logic queries the table

**No pre-planning past this point.** The data tells us what to do.

---

## After Sprint 2

Iterate. Drive. Log. Query. Fix. Drive again.

If the truth table shows 95%+ agreement with reality, **launch.**

If it shows logic contradictions, fix those, drive again, requery.

The data is the gate, not a sub-step count.

---

## Discipline rules (kept)

- **L-2:** predict-then-verify on every gate
- **L-3:** anchor-based apply scripts when modifying files >200 lines
- **L-6 + corollary + SECOND STRIKE:** read production artifacts before authoring; `grep -rn` on any rename
- **L-9 + corollary refinement:** fixture provenance in docstrings; 80-iteration boundary probes
- **L-10:** gate threshold provenance categorized; paranoia not allowed
- **Memory note #2:** canonical `app_private.coords_to_*` functions; never raw `ST_MakePoint` in production
- **Memory note #9:** Auto Nail It only; no manual Nail It buttons in commercial product
- **Memory notes #17, #18:** never paste multi-line markdown to bash; scp from `/mnt/user-data/outputs/` only

---

## Discipline rules (NEW per 2026-04-29)

- **INDEX.md is pasted at start of every chat session.** Portable manifest of all decision documents. Without it, Claude operates blind.
- **System prompt directs Claude to consult INDEX before proposing changes.** Never guess at architecture; ask for the source document.
- **Read the code before modifying it.** Documents capture decisions; code holds the actual logic. Both required for safe changes.
- **Build outcomes, document outcomes.** No design documents before code ships. Markdown captures what was built and why, not what's planned.

---

## Discipline rules (DROPPED)

- **L-11 doc-currency gate** is now lightweight. Run pytest + git status at session-open, not the full doc-currency check.
- **PHASE_E_PROGRESS.md mid-sprint refreshes are dropped.** One closing refresh after launch, if at all.
- **No more multi-hundred-line forensic docs.** Bugs get a commit message. Findings get a one-paragraph note if they affect future work.
- **No synthetic-fixture sprints.** Real production data validates better than synthetic tests for PUDO identification.

---

## Status

**Sprint 1 (Wire WAI + logging):** ACTIVE TODAY
**Sprint 2 (Iterate based on data):** queued (opens after first shift)

LFG. 🎩🐸🏁