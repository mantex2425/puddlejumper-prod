# PuddleJumper Document Index

**Status:** manifest of all active decision documents. Paste at the start of every chat session.
**Last updated:** 2026-04-30 (Sprint A architectural pivot)
**Active docs:** 11 (added SIMPLIFIED_ARCHITECTURE.md)
**Archive:** `docs/archive/` (7 superseded docs preserved for git history)
**Architectural pivot in flight:** Sprint A — see SIMPLIFIED_ARCHITECTURE.md and SPRINT_PLAN.md

---

## How to use this index

1. **Paste this file at the start of every chat session.** Claude reads it and knows what exists on the VM.
2. **When Claude needs context Claude doesn't have, Claude asks for the relevant doc by name.** You `cat` it from the VM, paste the content, Claude reads.
3. **When a new long-term decision is locked, document the outcome and update this index.** Build → document outcome → update INDEX → commit.
4. **Never guess at past decisions.** If it's not in this index or in the loaded docs, ask.

---

## Operational (load every session)

These three docs define how Claude operates and what rules apply. **Always loaded.**

- **CANONICAL_RULES.md** (228 lines) — Eternal product law: coordinate functions (Section I), UTC time (Section II), 4-box controller, ABORT guard, enforcement layers, Postgres/Python boundary. **Note:** State levels sections (UNCOMMITTED/ENROUTE/IN_TRIP/STACKED, Sections XI-XIII) are being superseded by SIMPLIFIED_ARCHITECTURE.md when Sprint A ships. Until then, both apply: legacy state machine in production (00575-mch), simplified architecture in design. Edits to non-superseded sections require explicit ratification.

- **SESSION_PROTOCOL.md** (149 lines) — How Claude and Andrew work together: paired-programming cycle (Claude proposes → Gemini reviews → consensus → execute), Claude behavior rules (CLI-only, push back when wrong, no preambles, Python heredocs over sed), output formatting, paste-safety rules (the 2026-04-27/28 hazards), pre-modification discipline, lessons reference (L-2 through L-11), infrastructure reference.

- **SPRINT_PLAN.md** (377 lines) — Current sprint structure with Sprint A architectural pivot at top. **Sprint A (2026-04-30):** WAI as single source of truth, state machine dissolved. **Sprint B:** validation against real shift. Below the pivot section, historical record of Sprint 1 (B-strict trilogy wiring, SHIPPED 00574-gw9) and Sprint 2 (adjacency lifeboat, SHIPPED 00575-mch) preserved verbatim, plus PUDO-FIRST DIRECTIVE (2026-04-29 evening) and B-NEW-1 through B-NEW-12 backlog. **This is the "what we're doing now" doc.**

- **SIMPLIFIED_ARCHITECTURE.md** (407 lines) — Architectural specification for the simplified ride identification system. Ratified 2026-04-30 morning by paired-programming (Andrew + Claude + Gemini). 12 sections: core principle (WAI as source of truth), data flow with Map-Reduce evaluation contract, match resolution (5 cases including implicit-cancel Ghost Ride recovery), disambiguation rules (3 multi-match scenarios), Queue Synchronization (Postgres canonical), Motion Gate (transition prerequisite), Triangulation Filter (pricing context), dispatch surface (3 actions), 8 documented assumptions (A1-A8) with validation paths and fallback strategies, implementation scope (12-15 hours estimated). **This is Product Law for Sprint A.** Edits to core sections (§2, §4, §5, §7, §8, §10) require paired ratification.

---

## Active design references (load on demand)

Consult when working on the specific component.

- **WHERE_AM_I_PROPOSAL_v2.md** (58KB) — WAI RFC v2 with v2.6 amendment. The architectural specification for the trilogy (Memory–Signal–Latch). Load when modifying WAI logic or working on classification rules.

- **PHASE_E_STEP_6_DESIGN.md** (40KB, Apr 27) — Five Pillars taxonomy (S31-S35: Geometric, Structural, Temporal, Collapse, Inverse). Reference for understanding pillar-based test design. Mostly historical now post-pivot; load only if Five Pillars taxonomy is in play.

- **PHASE_E_KICKOFF.md** (6.7KB, Apr 26) — Phase E entry brief, original scope, non-goals. Load to understand the original Phase E framing and what was deliberately deferred.

---

## Identity Genesis (Sprint 1 server-side SHIPPED 2026-05-10)

UUID v7 as canonical offer identity, edge-generated on Android. Replaces
the legacy dual-integer-sequence identity model that caused the X3
incident. Producer side (Android APK 1.1.20) shipped via PR #1 on
mantex2425/Puddle_Jumper. Server-side Sprint 1 (forward-compat: accept,
validate, persist into trace_data JSONB) shipped on commits a5618e6
through 0bfc166 on phase-2c-2-tad-exit-4tools. Sprint 2 (canonical-column
schema migration) is the next major sprint, blocked on the 5-real-offer
verification gate completing.

- **IDENTITY_GENESIS_DESIGN_2026-05-09.md** — The architectural decision,
  ratified by Gemini. UUID v7 / RFC 9562 §5.7 / edge-generated. Full
  schema-migration plan in §5, sprint plan in §8. **Note §11.4's
  forward-compat column proposal was superseded by the JSONB-only
  approach in the Sprint 1 brief — the brief is authoritative.**

- **SERVER_SIDE_SPRINT_1_BRIEF_2026-05-09.md** — Authoritative Sprint 1
  scope: accept offerId, validate UUIDv7, persist into trace_data JSONB.
  Forward-compat only — no schema migration. Includes verification SQL
  for the 5-real-offer gate.

- **SERVER_SIDE_SPRINT_1_EVIDENCE_2026-05-09.md** — Empirical evidence
  from 2026-05-09 evening device validation. Three real production v7
  UUIDs on Andrew's phone, OCR'd from real Uber offer cards. Used as
  test fixtures in tests/test_decisions_router_offer_id.py.

- **X3_FINDINGS_2026-05-09.md** — Root-cause forensic from the X3
  incident: dual-integer-sequence identity debt, α-fix translation
  subqueries, zero actual_pickup_at writes across 11 ACCEPTed offers.
  The reason Identity Genesis exists.

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
- **HEAD at index update:** post-`df8fc5b` (doc cleanup), pending Sprint A architecture commit
- **Test floor:** pytest 309/309, integration 22/61
- **Trilogy status:** SHIPPED and WIRED in code (Sprint 2 closeout, deployed 00575-mch). Dormant per architecture pivot — Sprint A rewrites the heartbeat handler around WAI-as-source-of-truth.
- **Production traffic:** `puddlejumper-api-00575-mch` (rolled back from `00576-f4p` on 2026-04-30 after Bug 2 surfaced). Bug 1 (`_just_nailed_pickup` undefined) may still trigger in IN_TRIP/STACKED branches; acceptable risk while Sprint A proceeds.
- **Sprint A status:** SCOPED, ratified, awaiting first implementation session.

---

## Odometer/GC
# INDEX.md additions — odometer / GC subsystem (2026-06-05)

Paste these into INDEX.md. The odometer/GC subsystem is currently UNREFERENCED in INDEX.md
(confirmed 2026-06-05); these entries begin closing that gap.

---

## Add under a new or existing "Odometer / GC / Liveness" heading:

- **FINDING_ODOMETER_GC_SPEC_RECONCILIATION_2026-06-05.md** — Canonical odometer/GC spec
  (proposed, awaiting ratification). Reconciles the three-way drift between RIDE_LIFECYCLE.md
  (stale time gate), PHASE_2C_2_DYNAMIC_ODOMETER_SNAPSHOT.md (unratified ±15% signal), and the
  running distance-gate code (unsourced 1.25× / [2,50] clamp). Defines: expected-odometer with
  remaining-current-trip bridge term; two-sided ±15% band; emergent reaping (no time ceiling, no
  distance cap, 4-hour abandonment backstop only); update-only-for-offers-received-during-current-
  ride invariant; lost-mode NULL+`deferred` sentinel with dropoff-disambiguation recompute/reap;
  verdict-blindness; odometer encapsulation + persistence to pudo_decision_context. Origin: the
  2026-06-04 drive review (9132 candidate-set eviction, ~63% PUDO rate).

## Stale-doc reconciliation flags (action items, not new docs):

- **RIDE_LIFECYCLE.md §3 step 3** — describes a TIME-based queue projection
  (`LEAST(GREATEST((pu_min+trip_min)*1.5,15),240)` minutes) that was replaced by the distance
  staleness gate on 2026-05-19. Marked for correction once the FINDING above is ratified. Until
  then, RIDE_LIFECYCLE.md §3 is NOT current on GC.

- **PHASE_2C_2_DYNAMIC_ODOMETER_SNAPSHOT.md** — status "not yet ratified for implementation";
  describes the ±15% expected-odometer concept as a *matcher scoring signal* (never built). The
  FINDING above adopts the ±15% concept as the *GC liveness band* instead. Cross-reference, do
  not treat as current spec.


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