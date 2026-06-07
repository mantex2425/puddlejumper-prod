# Canon rewrite plan — purge the dead state machine, consolidate to one source

**Status:** PROPOSAL for Gemini review (Rule I: Claude proposes → Gemini reviews → consensus →
execute). The line-by-line rewrite executes **after** this plan is ratified. **Authorized by
Andrew (2026-06-07):** "the state engine IS dead — rewrite now as its own loop."

**Premise (recon-confirmed):** the state machine is demolished. `driver_trip_state_log`
state-transition writes ceased **2026-05-04**; the **2026-04-30 DEPRECATION NOTICE** (CANONICAL_RULES
§ line 319) already flagged §§III, VIII, IX, X, XI, XII as state-machine-dead "to be rewritten
cleanly after Cut B3 lands." **Cut B3 landed; the cleanup never ran.** This loop runs it.

---

## Gemini review outcomes (2026-06-07) — REVISIONS to this plan

Gemini reviewed and pushed back; all corrections accepted (+ one refinement from Claude). **The full
canon rewrite is STOOD DOWN; only two prerequisite sections execute now (see Sequencing Gate).**

1. **Numbering RESOLVED by data (not preference).** `grep -rhoE "§…|Rule …" --include="*.py"` →
   736 `§` refs in code, all in `CANONICAL_RULES.md`'s scheme (§XVIII×69, §XVII×52, §XIV.I×39,
   §XVI.C×37, Rule XV×40, Rule XVI×17, §XIV.H/J, §0, §II, §V…). The v2.1 renumbering has **zero**
   code uptake. ⇒ **Keep the file's scheme; rewrite sections IN PLACE (numbers stable); tombstone
   cut sections (e.g. "§XI — retired, see §VI") rather than deleting numbers; renumber the v2.1
   doc to adapt.** `Rule XVI` (17 refs) is load-bearing (Arrest-Defined Truth) — accept/decline goes
   to Rule XV sub-clause or §XIX, never XVI.
2. **§XII — VERIFIED DEAD by grep (Andrew, 2026-06-07); CUT the machinery, fold the prohibition
   into §X.** Re-checked: zero code refs to §XII; zero live abort/divergence/cancellation mechanism
   (every `abort` hit is transaction-abort; every `diverg` hit is unrelated predicate/replay text);
   `nailed_pickup_*` survives only as a pickup-ANCHOR for dropoff refinement (`refinement_gates`,
   `nail_it_core`), NOT an abort guard. **Both Gemini's "keep it live" AND Claude's "live analog in
   band/TAD" were unverified — the grep overturns both.** Disposition: cut the §XII machinery
   (tombstone the section); fold the single forward-prohibition — *"once a pickup is physically
   confirmed, outward divergence is normal driving; never auto-abort or cancel tracking on it"* —
   into §X's "GPS is always the truth" as a one-line clause. Preserves the institutional-memory
   guard without implying a live firewall (Gemini's underlying concern honored, in the right place).
3. **§VIII EXECUTE = hard blocker; define the dual-lane NOW (before the ledger schema):**
   - **Authoritative / Gated lane:** inline, predicate-composed SQL state mutations that alter queue
     visibility, matching eligibility, or contract lifecycle. (Replaces the dead "all writes via
     `sm_transition`.")
   - **Passive / Open lane:** non-blocking, best-effort, append-only observability writes (the event
     ledger). Forbidden from altering application state or being **sourced for any business
     decision**.
   - **CLAUDE REFINEMENT (contradiction to resolve):** Gemini's "Passive lane never read by runtime
     loops" collides with the ledger's §6.2 lookback (the heartbeat reads the prior
     `queue_snapshot`/`matcher_snapshot` to compute the reap/inflection diff). Resolve one of two
     ways: **(a)** refine the rule to *"no runtime read for business state/decisions; observability-
     internal self-reads (the diff seed) allowed,"* or **(b)** honor the strict rule and move the
     lookback to `driver_trip_state` (Authoritative lane, already read each heartbeat). The dual-lane
     discipline argues for (b). **This fork must be decided as part of the §VIII rewrite.**
   - **RESOLVED (Andrew, 2026-06-07): Option 1 / (b).** The diff lookback reads `driver_trip_state`
     (Authoritative lane); the ledger stays a pure write-sink, **never read by runtime**. Verified
     feasible: `driver_trip_state` is PK-per-driver (one row), already read + `UPDATE…RETURNING`'d
     each heartbeat (arrest counter, `effective_last_move`), and already carries jsonb (`heartbeat`,
     `peak_sample`). Home the prior snapshot here — add `last_queue_snapshot`/`last_matcher_snapshot`
     jsonb (or fold into the existing `heartbeat` blob); likely **zero extra queries** (piggyback the
     read that already happens). Option 2 (ledger self-reads) **REJECTED** — it couples best-effort
     observability into the live decision path, the exact §VII violation the dual-lane exists to
     prevent.
4. **SEQUENCING GATE (decoupled phases):**
   - **Phase 1 — Prerequisite Gate (execute now):** rewrite ONLY the merged **§I Foundations
     (Space & Time)** and the **§VIII EXECUTE boundary** (dual-lane). These are the event-ledger's
     direct ancestors; they must be pristine before the schema is cut.
   - **Phase 2 — General Cleanup (deferred):** the dead-vocab purge of §III/§IX/§X/§XI/§XII/§XIII,
     parked to run *during* the ledger's shadow-validation period (low-coupling; must not block
     active tracking fixes).

The cut/fold map in §B below stands as the Phase-2 blueprint; §A + the new §VIII dual-lane are
Phase-1. Numbering questions in §G.1/§G are now resolved by item 1 above.

---

## A. FOUNDATION FIRST — coord + time (Andrew's emphasis: make-or-break for the event-ledger)

These are the rules every logging attempt inherits; get them wrong and the ledger is corrupt from
row one. Currently **split across §I, §II, and §XIV.G** — merge into ONE "Foundation" section.

- **Temporal (§II, current):** UTC always. `(NOW() AT TIME ZONE 'UTC')`, or `NOW()` on a
  `timestamptz` column. Engine is timezone-agnostic; "Texas Time" localization happens only at the
  UI edge, never in logic. → **Ledger binding:** `event_time timestamptz` written via `NOW()`; never
  naive. The `(driver_id, event_time DESC)` lookback (§6.2) and the delta/keyframe fold depend on
  correct UTC ordering — a naive or local timestamp silently breaks "latest row" and reconstruction.
- **Coordinate (§I, current):** canonical functions only — `coords_to_h3(lat,lng)`,
  `h3_to_lat/lng`, `coords_to_point(lat,lng)`, `coords_to_geography(lat,lng)`, `distance_miles(...)`.
  **Blacklist** raw `ST_MakePoint` (it's `(lng,lat)` — the swap trap), `h3_latlng_to_cell`,
  `h3_cell_to_latlng`. Ordering is **always `(lat, lng)`**. → **Ledger binding:** store `(lat,lng)`;
  derive any H3/geometry through the canonical funcs in `(lat,lng)` order; `queue_snapshot`/`payload`
  coords follow `(lat,lng)`; the writer never constructs raw geometry.
- **§XIV.G** today only cross-refs §I/§II ("when in doubt route through `coords_to_*` and
  `(NOW() AT TIME ZONE 'UTC')`; direct `ST_MakePoint`/local-time SQL is a review blocker"). Fold its
  enforcement teeth into the merged Foundation section; drop the duplication.

---

## B. Section disposition (cut / rewrite-keep-principle / keep)

**KEEP as current (deprecation notice confirms these are fully current):** §0, I, II, IV, V, VI,
VII, XIII (principle), XIV (+ subsections A–J), XV, XVI, XVII, XVIII.

**CUT entirely (pure dead state-machine, no surviving principle not already elsewhere):**
- **§IX Enforcement Layers** — `valid_state_transitions`, `enforce_state_transition_trigger`,
  `sm_transition`, `SET LOCAL app.state_trigger`: all dead.
- **§XI State Levels** — UNCOMMITTED/ENROUTE/IN_TRIP/STACKED: collapsed to the 1-bit
  `current_offer_id` already documented in **§VI** (its replacement already exists).
- **§XII Abort Guard** — **SETTLED-BY-GREP as dead machinery** (not a live firewall; see review
  outcome #2). Zero `§XII` code refs; zero live abort/divergence/cancellation path. **Phase 2: CUT
  the §XII section entirely, and fold ONLY the forward-prohibition** — *"once a pickup is physically
  confirmed, outward divergence is normal; never auto-abort/cancel tracking on it"* — **into §X's
  GPS-is-truth as a one-line clause.** Phase 2 must NOT inherit the "Post-Pickup Motion Firewall /
  keep-it-live" framing (that was Gemini's unverified reclassification; the grep overturned it).

**REWRITE, preserving the surviving principle:**
- **§III Logic Rules** — DROP "use the `check_convergence` state machine"; KEEP **"Distance over
  Geocode"** (physical distance is the primary constraint, geocode is a suggestion — aligns with §0
  and Rule XV, and the v2.1 "Truth Ordering" GPS > Cluster > WAI > Memory).
- **§VIII 4-Box Controller** — DROP the dead file assignments (`sm_transition`, `PudoPlanner`,
  `DriverStateMachine.transition`, `check_convergence` as the controller); KEEP the
  **Monitor/Diagnose/Plan/Execute separation-of-concerns frame** (the notice says the frame
  survives). Open: redefine "EXECUTE / the only write path" post-demolition (no `sm_transition`
  now — the write paths are the predicate-composed queries + `_log_decision_context`). Needs
  Gemini/Andrew confirmation of the new EXECUTE definition.
- **§X Implicit Cancellation** — DROP S04/S11/S12/ABORT/STACKED vocabulary; KEEP **"GPS is always
  the truth"** (already echoed in the v2.1 Truth Ordering and §4 Case D).
- **§XIII What Stays in Python** — fix dead names (`check_convergence()`, `PudoPlanner dispatch`);
  KEEP the principle (decision math / triangulation / YOLO / Discord / Firebase stay in Python).

---

## C. Accept/decline blindness (Andrew's "Rule XVI" ask) — elevate, don't duplicate

The principle is **already canon** — §XIV.I:671 ("the PUDO matching layer treats all queued offers
as equally valid observation candidates; branching on accept/decline conflates observation with
narrative, violating Rule XV") + Rule XV. And the **live engine already complies** (no `app_verdict`
read in `driver_heartbeat`/`driver_queue`/`where_am_i`/the matcher). So this is an *elevation*, not a
new behavior:
- State it as a clearly-named top-level rule: **all offers enter the queue; accept/decline is advice
  to the driver, with ZERO bearing on PUDO behavior or measurement.** `app_verdict` is legitimate
  only where it *is* the advice (decision engine produces it; analytics/forensic display it).
- **Placement:** fold into **Rule XV** (Observation Before Narrative) as a sub-clause, OR a new
  **§XIX** — **NOT "XVI"** (taken by Arrest-Defined Truth). Andrew/Gemini to pick.
- **Lock it:** add a test asserting the predicate/matcher never read `app_verdict` (blindness can't
  silently regress).

## D. Single source of truth (the deeper consolidation)

Three overlapping docs today: **`CANONICAL_RULES.md`** (1979 lines) + **`SIMPLIFIED_ARCHITECTURE.md`**
(named "authoritative on conflict" by the deprecation notice) + the **"v2.1 Standards"** (divergent
numbering — its I = Paired-Programming vs the file's I = Coordinate; *same numbers, different rules*).
Collapse to **ONE** canonical doc; demote the others to pointers or an archive. Resolve numbering to
a single scheme as part of the rewrite (the v2.1 renumbering and the file's numbering cannot both
stand).

## E. Out-of-band scripts: replay/harvest DELETED (2026-06-07)

`scripts/replay_pudo.py` and `scripts/harvest_ride.py` were unused (nothing imported them; both
stale) and are **deleted**. Their Rule-XVI measurement leak (`app_verdict=='ACCEPT'` expectation
gating) is therefore moot. Validation is the **live `00649` drive + the event-ledger**, not a replay
harness. Canon follow-up (Phase 2): §XIV.H's out-of-band exception clause still names "replay …
harvest" — drop those referents when §XIV is revised (drive_review/backtest stay if retained).

## F. Process & sequencing

- This is a **Propose → Gemini → execute** loop; ratify THIS plan (cut/fold map, numbering,
  accept/decline placement, single-source decision) before the line-by-line rewrite.
- Shares surface with the **event-ledger** build (both purge state-machine residue and both rest on
  the Foundation section). Sequence deliberately — the ledger should cite the rewritten Foundation
  for its time/coord invariants.

## G. Open decisions for Andrew / Gemini

1. **Numbering scheme** — keep the file's I–XVIII (+§0, §XIV.A–J) and renumber nothing else, or adopt
   a fresh scheme? (Recommend: keep existing numbers, only cut/rewrite in place — minimizes churn and
   broken `§` cross-references throughout the codebase comments.)
2. **Accept/decline rule placement** — Rule XV sub-clause vs new §XIX.
3. **`SIMPLIFIED_ARCHITECTURE.md`** — merge wholesale into the canon, or keep as a detail doc the
   canon points to?
4. **Merge §I+§II+§XIV.G** into one Foundation section, or leave §XIV.G as a Sprint-A pointer?
5. **New EXECUTE definition** for the rewritten §VIII (post-`sm_transition` write paths).
