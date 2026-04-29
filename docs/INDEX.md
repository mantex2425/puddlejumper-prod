# PuddleJumper Document Index

**Status:** manifest of all active decision documents. Paste at the start of every chat session.
**Last updated:** 2026-04-29 evening (PUDO-FIRST ratified)
**Active docs:** 10 (post-audit cleanup)
**Archive:** `docs/archive/` (7 superseded docs preserved for git history)

---

## How to use this index

1. **Paste this file at the start of every chat session.** Claude reads it and knows what exists on the VM.
2. **When Claude needs context Claude doesn't have, Claude asks for the relevant doc by name.** You `cat` it from the VM, paste the content, Claude reads.
3. **When a new long-term decision is locked, document the outcome and update this index.** Build → document outcome → update INDEX → commit.
4. **Never guess at past decisions.** If it's not in this index or in the loaded docs, ask.

---

## Operational (load every session)

These three docs define how Claude operates and what rules apply. **Always loaded.**

- **CANONICAL_RULES.md** (228 lines) — Eternal product law: coordinate functions, UTC time, 4-box controller, ABORT guard, state levels (UNCOMMITTED/ENROUTE/IN_TRIP/STACKED), enforcement layers, Postgres/Python boundary. Source of truth for product architecture. Edits require explicit ratification.

- **SESSION_PROTOCOL.md** (149 lines) — How Claude and Andrew work together: paired-programming cycle (Claude proposes → Gemini reviews → consensus → execute), Claude behavior rules (CLI-only, push back when wrong, no preambles, Python heredocs over sed), output formatting, paste-safety rules (the 2026-04-27/28 hazards), pre-modification discipline, lessons reference (L-2 through L-11), infrastructure reference.

- **SPRINT_PLAN.md** (214 lines) — Current sprint structure: 2 sprints to launch (Sprint 1 = wire WAI live with logging; Sprint 2 = iterate based on production data). Includes the PUDO-FIRST DIRECTIVE (2026-04-29 evening ratification) — singular objective: prove 100% PUDO identification accuracy. Architectural shape locked: B-strict full replace, all 12 PlannerDecision actions wired, no feature gate. **This is the "what we're doing now" doc.**

---

## Active design references (load on demand)

Consult when working on the specific component.

- **WHERE_AM_I_PROPOSAL_v2.md** (58KB) — WAI RFC v2 with v2.6 amendment. The architectural specification for the trilogy (Memory–Signal–Latch). Load when modifying WAI logic or working on classification rules.

- **PHASE_E_STEP_6_DESIGN.md** (40KB, Apr 27) — Five Pillars taxonomy (S31-S35: Geometric, Structural, Temporal, Collapse, Inverse). Reference for understanding pillar-based test design. Mostly historical now post-pivot; load only if Five Pillars taxonomy is in play.

- **PHASE_E_KICKOFF.md** (6.7KB, Apr 26) — Phase E entry brief, original scope, non-goals. Load to understand the original Phase E framing and what was deliberately deferred.

---

## Forensic / empirical record (load when relevant)

Field-test findings and audit results that inform future design.

- **FORENSIC_2026_04_27_HOUSTON_SHIFT.md** (18KB, commit `773aa49`) — Five cases captured from 2026-04-27 Houston overnight shift: McDonald's corner-lot (S32), Planet Fitness canonical regression (S32), Target same-structure inversion proof, Sheraton Ghost Ride (TargetSpec Vacuum), 584m post-Sheraton geocode drift. L-9-class fixture provenance for future Phase F integration tests.

- **FORENSIC_2026_04_28_HOUSTON_WAYS_AUDIT.md** (12KB, commit `48297bf`) — `routing.houston_ways` audit: 3 mislabeled segments at Northwest Crossing parcel (Terramont/Player Bend gids 331487/259545/324551), 38 of 41 nearby segments correctly labeled. Validates B-27 Layer 4 (adjacency learning via observation) as load-bearing.

---

## Historical / reference only

Stable snapshots of past phases. Rarely loaded but kept for context.

- **PHASE_D_RETRO.md** (12KB, Apr 26) — Phase D lessons-learned (L-2 through L-8 source). Load if a lesson's origin needs verification.

- **PHASE_E_PROGRESS.md** (72KB, last touched commit `773aa49`) — Phase E sub-step tracking through 1c closeout. **Stale per 2026-04-29 pivot** (sub-steps 3-8 dropped). Load only for historical reference of what shipped pre-pivot.

---

## Archive (`docs/archive/`)

Superseded docs preserved for git history. **Do not load — they contain stale rules.**

- `ARCHITECTURE.md` — content merged into CANONICAL_RULES.md Sections X-XIII (state levels, ABORT guard, what-stays-in-Python)
- `CANONICAL_BRIEF.md` — 95% redundant with CANONICAL_RULES.md; 5% operational checklists deferred to post-launch
- `CONTEXT_FOR_CLAUDE.md` — first half redundant; behavior rules salvaged into SESSION_PROTOCOL.md
- `COORDINATE_RULES.md` — fully superseded by CANONICAL_RULES.md Section I (also contained Chicago-time bug)
- `DTF_DESIGN.md` — superseded by trilogy (Memory pillar makes retroactive resolution unnecessary)
- `NEXT_SESSION_STARTER_SUBSTEP_1C_CLOSEOUT.md` — bridge artifact for sub-step 1c, shipped at `72cf951`
- `SESSION_CONTEXT_APR17.md` — 12-day-old session context, fully obsolete

---

## Repository metadata

- **Repo:** `~/puddlejumper-prod/`
- **Active branch:** `patch-00566a-unified-refinement`
- **HEAD at index authoring:** `48297bf`
- **Test floor:** pytest 290/290, integration 22/61
- **Trilogy status:** SHIPPED in code (`72cf951`), DORMANT in production (not yet wired into `driver_heartbeat.py` — Sprint 1 today)

---

## Adding new docs to the index

When a new long-term decision is documented:

1. Author the doc as a focused outcome (not a design plan)
2. Add a one-line entry under the appropriate section above
3. Commit both the new doc and the updated INDEX in a single commit
4. Next session opens with the updated INDEX and Claude knows the new doc exists

When a doc becomes obsolete:

1. Move to `docs/archive/`
2. Add a one-line entry under "Archive" explaining what superseded it
3. Remove from the active sections above

The INDEX is the manifest. The docs are the cargo. Together they're the bridge between sessions.