# Phase E Kickoff Brief

**For:** A fresh Claude conversation starting Phase E (PLAN consumer / pudo_planner.py).
**Author of brief:** Phase D's closing Claude session (commit 60ed517).
**Date authored:** 2026-04-26.

---

## Read these in order before doing anything else

1. **`PHASE_D_RETRO.md`** at the repo root — the implementation record from Phase D. Five-minute read. Pay attention to lessons L-6, L-7, L-8 — those are protocol changes that affect how Phase E should be run.

2. **`WHERE_AM_I_PROPOSAL_v2.md`** at the repo root — the design RFC. Note the v2.5 amendment header at the top: the RFC below it is **historical context**, not the current state. The current state is `PHASE_D_RETRO.md`.

3. **The last 13 commits** — `git log --oneline 15b7a2f^..HEAD` — that's the entire Phase D arc.

4. **`scenarios/S32_implicit_stacked_cancel.toml`** — the only scenario in the locker explicitly marked `AWAITING_PHASE_E`. This is the canonical motivating case for one of Phase E's primary deliverables.

---

## Current state of the world (at Phase E entry)
HEAD: 60ed517 (Phase D Step 5.8 — closure)
Branch: patch-00566a-unified-refinement (mergeable, clean working tree)
Tests: 176/176 pytest passing
Integration: 22/61 (39 known pre-existing failures, documented in tests/TEST_SUITE_STATUS.md)
Live PG smoke: 4/4 passing (run via python3 scripts/wai_smoke.py)

WAI (`where_am_i.py`) is the DIAGNOSE primitive in the 4-Box Controller. It REPORTS the truth about driver location. It does not act on that truth — it has no writes, no state mutation, no `sm_transition` calls. Per Q12, all writes are PLAN/EXECUTE's responsibility.

**Phase E is the PLAN consumer.** Its job is to subscribe to WAI's output and act on it: write to `app_private.suspected_pudos`, drive `app_private.sm_transition` calls, decide whether to fire on weak-confidence matches, detect contradictions like the S32 implicit-cancel pattern.

---

## What Phase E owns (high-level scope)

From `PHASE_D_RETRO.md` and the v2.4 RFC's deferred section:

1. **Subscribes to WAI's per-heartbeat output.** Phase F (shadow-mode integration in driver_heartbeat.py) is what actually invokes WAI on every heartbeat; Phase E is the consumer of those results.

2. **Owns `suspected_pudos` INSERT writes.** Per Q12, WAI does not write. When WAI returns `at_unknown_pudo`, PLAN decides whether to persist a suspect for ghost-cache purposes.

3. **Owns `sm_transition` calls.** When WAI's confidence + temporal pattern + state-machine rules say "fire," PLAN drives the state transition.

4. **Implements temporal pattern detection.** "Stable for N heartbeats" — WAI is point-in-time; PLAN watches it over time.

5. **Implements B-11: implicit STACKED cancellation detection (S32).** When WAI in STACKED state reports `at_current_pudo` with `target_address` matching the secondary's pickup (not the primary's dropoff), PLAN must detect this contradiction and drive `sm_transition` to close the primary and promote the secondary.

6. **Decides which weak-confidence matches (0.4 ≤ conf < 0.7) to wait on vs. fire on.** WAI reports the truth; PLAN decides whether the truth is decisive yet.

---

## What Phase E does NOT own

- **WAI's diagnostic logic.** Don't reopen Phase D's Q-rulings. The 28 architectural locks are stable. If something seems wrong, surface as a B-* backlog item, don't litigate inline.
- **The actual heartbeat loop.** That's Phase F. Phase E builds the *consumer* assuming the loop will call it.
- **The shadow-mode dashboard / metrics surface.** Also Phase F.
- **BMOAR Path A/B deprecation.** That's Phase G — only happens after shadow-mode data shows WAI matches or exceeds BMOAR's fire rate.

---

## Backlog items relevant to Phase E (at entry)

From `PHASE_D_RETRO.md`:

- **B-8** — Evaluate `REFINE_DROPOFF` deprecation post-shadow-mode. Phase E may have an opinion once it's working.
- **B-11** — Implicit STACKED cancellation detection (S32). **Primary Phase E concern.**
- Phase F TODO: TargetSpec needs an `address` field. Currently `WhereAmIResult.target_address` is always None because `getattr(target, "address", None)` returns None. Phase E may want to coordinate with Phase F on this since the field will materially affect S32 detection logic.

---

## Protocol — the paired-programming pattern that worked

This is what made Phase D ship cleanly. Phase E should use the same pattern.

### The cycle
1. **Claude proposes** an architectural step (one step at a time)
2. **Andrew runs the proposal past Gemini** for review
3. **Gemini ratifies or pushes back** — paste the response
4. **If ratified, Claude implements** with verification gates
5. **Andrew runs the gates** — paste output
6. **Commit + push immediately**, never bundle multiple steps into one commit

### Critical rules
- **One step at a time.** Each step has its own commit. L-2 paranoia gate: `git status` before staging, after staging, after commit.
- **Verification gates per step.** Before any commit, demonstrate: pytest count preserved or increased, integration baseline preserved (currently 22/61), and the new functionality works.
- **Anchor-based patch scripts for files >100 lines.** L-3: SHA-locked pre-condition + py_compile post-condition + `--dry-run` default.
- **Trailing-newline guard.** L-5: `[ -n "$(tail -c 1 FILE)" ] && echo >> FILE` after any heredoc-write.
- **Inspect production artifacts before authoring assertions.** L-6: `cat` the actual file, don't reconstruct from memory.
- **Cross-check architectural rulings against production conventions.** L-7: 5-minute grep against the codebase before locking type-shaped decisions (cursor types, dataclass shapes, return-type conventions).
- **Live-PG smoke before declaring done.** L-8: every DB-coupled phase ends with a smoke run against `10.128.0.2`.

### What Andrew expects from Claude
- Treat Andrew as a 1995-era programmer learning modern tools. Core logic he gets; modern terminology benefits from clear explanation.
- One step at a time. Wait for explicit confirmation before proceeding.
- Every step needs a specific testable verification command.
- Push back when something feels wrong, even if Andrew says "proceed."
- Don't blow smoke up his ass. He values honesty over agreement.

---

## Note on this file's authoring

This file was reconstructed at end of Phase E session-12 (post commit 10eb654) from the original brief Andrew pasted at the start of that session. The original heredoc command meant to author it was never executed before work began; we worked through Step 2 -> Step 4 + handoff treating it as existent. The content here is the original brief verbatim — what Phase E was *supposed* to start with.

For the current state of Phase E (post-Step-4), see `PHASE_E_PROGRESS.md`.
