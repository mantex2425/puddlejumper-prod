# HANDOFF — §5.5 Sentinel: deployed, witnessed, but trigger mismatched (2026-06-06 AM)

## TL;DR (read this first)
The §5.5 deferred sentinel is BUILT, UNIT-TESTED, DEPLOYED (rev 00648-lwx), and its
recompute PLUMBING is production-witnessed. BUT its deferral TRIGGER is mismatched:
it defers on "client omitted odometer," not on §XVIII lost-mode — and the current
client always sends the odometer, so the trigger fires 0% in production. In real
lost-mode the code instead FABRICATES the wrong-but-plausible idle anchor the
sentinel exists to prevent. **The lost-mode PUDO miss problem is NOT solved.** The
next workstream is the trigger fix. This is a clean, well-scoped handoff.

## Current production state
- Deployed: `puddlejumper-api-00648-lwx`, serving 100%, from branch
  `fix/restore-fire-error-metric-2026-06-02` @ `f88b8c7` (now `681eccb`/`f7f58a9`
  after docs commits — code identical, docs-only commits after the deploy).
- Floor: 770 passed / 1 skipped / 0 failed. Index guard at 38.
- Deploy discipline: ONLY deploy this branch to puddlejumper-api. A parallel-branch
  deploy dropped a fix earlier this week; don't repeat.

## What was accomplished this session (real, keep)
1. **§5.5 sentinel built + deployed.** Migration (expected_odometer +
   expected_odometer_status columns), status constants, persist-on-None at the
   offer_history INSERT (positionally verified at 38), Class A pickup-fire
   resolution, Class B window-scoped dropoff recompute (`_resolve_deferred_at_dropoff`),
   §XIV.J live-PG test (3/3), Call A guard-test (2/2). Commits bd5175b → f88b8c7.
2. **Design fully documented.** FINDING §5.5 + §9.1–9.8 + `docs/SENTINEL_DESIGN_SPEC.md`
   (distilled intent + 7 invariants). Gemini ratified 00648 (Call A guard-test,
   Call B full-backfill). Nothing design-level lives only in chat.
3. **Recompute plumbing production-witnessed.** Bruno, synthetic driver: offer 10202
   deferred via real receipt path (omitted cumulativeMiles), resurrected at a real
   dropoff fire (POI/semantic-anchor at cached IAH Terminal C coords), expected_odometer
   flipped to 15.700 (chained anchor), `[deferred-sentinel] recomputed offer=10202`
   log line confirmed. Reusable Bruno "PUDO Simulation — Deferred" suite created.

## THE FINDING (production-confirmed, not hypothesis) — the reason for the next workstream
**The §5.5 deferral trigger does not coincide with §XVIII lost-mode, and fires 0% in
production.**
- Spec trigger (FINDING §5.5 / INV-1): `current_offer_id IS NULL` AND alive-unpicked
  offer exists.
- Implemented trigger: `cumulative_miles is None` at receipt (decisions/logger.py
  ~150/250/348). No lost-mode awareness anywhere in the receipt anchor path;
  log_decision reads no current_offer_id / driver_trip_state.
- decisions/router.py:214 reads the odometer from the CLIENT payload
  (`p.get("cumulativeMiles")`); nothing derives it server-side at receipt.
  (The server-side primitive bead_on_wire.odometer_miles_since exists but is only
  used by BEAD's motion gate, never by decisions/.)
- Android client (ScreenshotMonitorService.kt:843-845, HeartbeatSender.kt:116/321)
  sets cumulativeMiles UNCONDITIONALLY, monotonic across the session, no lost-mode
  branch. Production data: 0/364 receipts missing odometer since 2026-05-11 (the
  62% historical rate was a pre-2026-05-11 legacy artifact). The 9132 lost-mode
  drive carried an odometer on 43/43 receipts.
- **Consequence (the active harm, not just inaction):** in real lost-mode the offer
  carries an odometer → takes the IDLE branch in compute_offer_expectations →
  `expected_pickup_distance = current_odometer + pickup_miles` → lands `active` with
  a confident-WRONG anchor (assumes idle when the driver is on an unobserved trip
  whose remaining distance is the unknowable bridge). This is precisely the
  "certain and wrong" failure §5.5 was built to prevent. The sentinel never engages.
- This is the definitive answer to "why deferred_count=0": not lost-mode rarity —
  the trigger keys on a condition (missing odometer) that never occurs.

### One residual caveat (honest)
The strongest lost-mode evidence (9132, 43/43 odometer) PREDATES the deploy
(06-04 < 06-06), so "those would have landed active" is a tightly-grounded
COUNTERFACTUAL, not a direct post-deploy observation. Post-deploy lost-mode traffic
is sparse (6 offers, all active/with-odometer). The one empirical thing left: a real
post-06-06 lost-mode drive (or a Bruno scenario that sets current_offer_id NULL with
an alive-unpicked offer AND sends an odometer) to directly witness an offer landing
active when it should defer. Code path makes this confirmation, not doubt.

## THE NEXT WORKSTREAM (scoped, not designed — for recon→design→ratify→deploy)
**Design question (stated, not inferred):** should the deferral trigger key on the
§XVIII signal (`current_offer_id IS NULL` + alive-unpicked, available server-side in
driver_trip_state at receipt) instead of (or in addition to) odometer presence?

Key facts the design inherits:
- The lost-mode signal IS available server-side at receipt (driver_trip_state /
  current_offer_id) — log_decision simply doesn't consult it. The fix is plausibly
  "make log_decision read the lost-mode condition and defer on it."
- The §5.5 plumbing (defer → recompute → resurrect) is correct and deployed — only
  the TRIGGER needs rewiring. The fix connects existing plumbing to the right signal.
- Once the trigger is fixed, a real lost-mode drive becomes the true witness (the
  thing the counterfactual above is standing in for).
- Discipline: this is an architectural change (deferral now consults driver-state) —
  full recon→design→Gemini-ratify→deploy, do not rush. Verify the receipt path's
  current_offer_id availability and the idle-vs-lost-mode branch in
  compute_offer_expectations before proposing the wiring.

## Other open threads (lower priority, distinct workstreams)
1. **Out-of-window deferred sweep — RESOLVED (§9.9, shipped `23dabb1`).** The open
   question (hard sweep = dropoff-handler-as-reaper vs soft sweep = re-flag to a new
   status) was resolved in favor of the SOFT sweep: out-of-window deferred offers are
   flipped to `expected_odometer_status = 'abandoned'` at the dropoff handler, and the
   live predicate excludes them via `expected_odometer_status IS DISTINCT FROM
   'abandoned'` — gone immediately, no 4h linger. NOT a second killing authority: the
   predicate stays the single liveness owner; the dropoff handler only writes a status
   it consumes (same pattern as the pickup-fire deferred→active writer), so no Gemini
   re-ratify of single-killing-authority was needed. The suspicion that the GC is
   structurally unable to reap never-picked-up deferred offers (its SELECT requires
   `actual_pickup_at IS NOT NULL`) was CONFIRMED — which is exactly why "leave for the
   GC" was wrong. See FINDING §9.9 + `docs/SENTINEL_DESIGN_SPEC.md` INV-4; tests in
   `tests/test_deferred_sentinel_recompute.py` + `tests/test_abandoned_excluded_from_predicate.py`.
2. **Matcher transit-road accuracy — triply-confirmed, real, degrades live drives.**
   The matcher fires reliably only via POI (airports, conf 1.00) or residential-road
   + driven-approach (Forum Park, 0.85). NO reliable fire path on transit-class roads:
   last night's drive bound PUDOs 0.3-0.5mi off on Sam Houston Pkwy (on_target_road
   over-confident on multi-mile roads; proximity=0.00 contributing nothing). Evidence:
   offers 10197-10201 (real drive), CC's synthetic block (0.19 dwell-only), the
   firing-examples contrast. Likely lever: road-class-gated confidence weighting
   (proximity should dominate, on_target_road should be near-worthless on transit
   roads). Recon where_am_i.py `_CONFIDENCE_WEIGHTS` + the proximity computation
   before proposing. Define ground-truth tolerance with Andrew first (what should the
   system do when you arrest near-but-not-at a pickup you can't physically stop at?).
3. **Step 7 cohorted replay** (does the band keep 9132-class offers live at arrest,
   against deployed rev) — still owed per FINDING §7; separate from all the above.

## Standing context
- VM: andrew@puddle-jumper. DB: psql -h 10.128.0.2 -U postgres -d puddlejumper.
  Repo: ~/puddlejumper-prod. Cloud Run: puddlejumper-api us-central1
  project puddle-jumper-477316.
- Real driver: UjT1hE9eBXh2q95aSZYOkzDJ8lo1. Synthetic test driver (Bruno/allowlist):
  C7FRKHbnvFcDPZ0RcXXjtPwGXgw2.
- pudo_decision_context is the forensic goldmine (WAI scores, cluster arrests,
  unmatched_reason, wai_per_offer_scores, matched_offer_id). heartbeat_log has GPS +
  cumulative_miles + current_offer_id. contest_labels = Andrew's physical button taps
  (ground truth). FORENSIC GOTCHA: taps are CT, DB is UTC sub-second — use ranges,
  never timestamp equality.
- FORENSIC INTERPRETATION GOTCHA: `wai_below_floor` with EMPTY matcher_candidates is
  a MISLABEL of lock-suppressed/empty-set post-fire re-eval — NOT a real below-floor
  rejection. Check matcher_candidates before concluding the matcher rejected anything.
- CC WIP safe in stashes on fix/three-recon-bugs-2026-05-30 (stash@{0} untracked
  docs/tests, stash@{1} tracked WIP). CC currently working tree clean on that branch.
- Key docs: FINDING_ODOMETER_GC_SPEC_RECONCILIATION_2026-06-05.md (§5.5 + §9.1-9.9),
  SENTINEL_DESIGN_SPEC.md, CANONICAL_RULES.md, SIMPLIFIED_ARCHITECTURE.md, plus the
  trigger-mismatch recon doc CC is writing now.

## The honest one-line summary for whoever picks this up
We built and proved the sentinel's machinery; it is wired to the wrong trigger and
does not fire on the lost-mode condition it exists for; the fix is to make the
deferral trigger consult the lost-mode signal that is already available server-side —
a small, well-scoped change that finally connects the plumbing to its purpose.
