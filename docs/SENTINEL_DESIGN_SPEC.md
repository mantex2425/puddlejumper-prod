# §5.5 Deferred Sentinel — Design Intent & Required Behavior

Purpose of this doc: give CC (or any implementer/tester) the *intent and required
behavior* of the deferred sentinel, distilled from the forensic FINDING. The
authoritative source remains `docs/FINDING_ODOMETER_GC_SPEC_RECONCILIATION_
2026-06-05.md` §5.5 + §9.1–9.8; this is the readable specification of what it must
do and why. If this doc and the FINDING ever disagree, the FINDING wins.

Deployed in rev `puddlejumper-api-00648-lwx`, branch
`fix/restore-fire-error-metric-2026-06-02` @ HEAD `681eccb`.

**2026-06-06 update (fix #2 + §9.9, this branch).** Two changes land the design's
actual intent, correcting two places where the deployed code diverged from it:

1. **Trigger wired to the §XVIII lost-mode condition (fix #2).** The deployed
   trigger fired only on a *missing odometer* — which the current client never
   sends, so it fired 0% in production (`RECON_DEFERRED_SENTINEL_TRIGGER_MISMATCH`).
   Fix #2 makes the receipt path defer on the real lost-mode condition (INV-1
   below), keeping the odometer-absent case as a union safety net.
2. **Out-of-window disposition changed deferred→`abandoned` (§9.9).** INV-4 below
   was rewritten: out-of-window deferred offers are no longer "left for the GC"
   (the GC is structurally blind to them); the dropoff handler marks them
   `abandoned` and the live predicate excludes them immediately.

---

## 1. The problem the sentinel solves (intent)

PuddleJumper detects pickups/dropoffs (PUDOs) by comparing the driver's cumulative
odometer against an *expected odometer* for each offer's leg. "Expected" needs an
anchor — where the driver was when the relevant trip began.

- **Idle receipt:** anchor = current odometer. Clean.
- **Mid-trip receipt (stacked):** anchor = previous offer's expected dropoff
  distance, chained forward. Clean — the chaining already folds in the "finish the
  current trip first" distance (this is the *only* bridge; there is no separate
  additive bridge term).

There is one case where the anchor is **genuinely unknowable**: the driver is in
**lost mode** — `current_offer_id IS NULL` AND an alive unpicked offer exists (the
§XVIII condition). The system does not know it is on a trip, so it cannot compute
how far the current (unobserved) trip will carry the driver before the new offer's
leg begins. That offset is unknown at receipt.

The hazard is GUESSING. A fabricated anchor (or a magic sentinel like -1) produces
a wrong-but-plausible expected odometer, and the downstream band arithmetic will
confidently compare against it and emit a garbage verdict — matching or reaping an
offer on a number that was never real. The worst failure mode in this system is
not "we don't know," it is "we are certain and wrong."

**The sentinel's intent: refuse to fabricate the unknowable anchor. Mark the offer
inert on the odometer axis. Wait for a real-world event (a dropoff) to supply the
missing anchor. Never guess.**

---

## 2. Required behavior (the invariants — a test MUST verify these)

### INV-1 — Deferral trigger (lost-mode ∪ odometer-absent)
At receipt the system MUST set `expected_odometer = NULL` and
`expected_odometer_status = 'deferred'` when EITHER condition holds (union, not
replacement — two distinct failure classes, both warranting defer):

- **Lost-mode (fix #2, the class that fires in the wild).** There is no chaining
  anchor — `prev_offer is None` after the Horizon GC — AND a pre-existing
  alive-unpicked peer offer exists (the §XVIII condition). The idle anchor
  (`current_odometer + pickup_miles`) would be a *confident guess* at an unknowable
  bridge term (§XVIII.B); defer rather than fabricate.
- **Odometer-absent (the original safety net).** `cumulative_miles` is absent, so no
  anchor is computable at all.

NULL is the sentinel — never a magic number. (A number in a miles column silently
corrupts any consumer that forgets to guard it; NULL forces every consumer to branch
or fail loud.)

Ratified signal choices (fix #2):
- **bit-1 = `prev_offer is None`**, NOT the literal `current_offer_id IS NULL` (R1).
  It keys on whether a real chaining anchor exists and is immune to the L-19
  stale-pointer problem; the two diverge only when `current_offer_id` is a stale
  non-NULL pointer — exactly the case where it is the wrong signal.
- **bit-2** is evaluated against the SAME alive-unpicked set the heartbeat lost-mode
  detector sees (R3 identical-set parity): the canonical bit-2 body
  `_get_alive_unpicked_offer_ids` fed `compute_effective_last_move` (the staleness
  anchor the detector uses — NOT raw `last_odometer_move_at`, which under-defers
  while the driver is moving, i.e. the lost-mode case). Both predicate bodies live
  in `driver_queue.py` (single definition, three consumers).
- **R2 (deferral volume) is resolved on philosophy, not a measured rate.** When a
  live unpicked peer exists the idle anchor is a guess, so defer-don't-fabricate is
  the honest call regardless of how often it fires. The single-driver / weeks-old
  dataset is too young to generalize; a band-aware historical replay would sharpen a
  non-generalizable number (false precision). The true rate is measured at the
  post-deploy lost-mode drive — no replay is built.

**Detection-error policy (fail-closed, honest about both branches).** The bit-2
detection is two DB reads. On a recon-confirmed expected failure
(`psycopg2.Error`/`DataError` — infra or a bad `::numeric` cast) the trigger NEVER
fabricates the idle anchor; it suppresses the anchor. Two outcomes:
- *recoverable error, healthy connection* → the offer_history INSERT persists the
  row `'deferred'` (the honest "unknowable" state). Pinned by the error-path test.
- *connection-loss* → the INSERT fails into the outer handler: a loud
  `[ERROR] … insert failed` with NO row written (fabrication-free; covered by
  propagation, not by a deferred write).

Any non-psycopg2 exception is a programming defect and propagates to the outer
handler (no row, loud) rather than silently deferring.

### INV-2 — Deferred offers are alive but inert on the odometer axis
A deferred offer MUST NOT be reaped by the band (no anchor → no band → cannot be
distance-reaped) and MUST NOT be matched by the band. It is parked: neither killed
nor acted on by odometer logic, until resolved.

### INV-3 — The dropoff supplies the anchor, WINDOW-SCOPED
When a dropoff fires for some offer X, X's trip is now identified; its window is
`[COALESCE(X.actual_pickup_at, X.created_at), NOW()]` (use receipt_time as the
lower bound when X's own pickup was missed). For every deferred offer D whose
`created_at` falls INSIDE X's window: recompute D's anchor (D was received during
the now-identified trip, so X supplies the missing bridge) and flip D to 'active'.
The recompute re-invokes the EXISTING expectation function with X's dropoff anchors
— the chaining IS the bridge; no new formula. D's full anchor set is backfilled so
a resurrected offer is indistinguishable from one active at receipt.

### INV-4 — Out-of-window deferred offers are marked `abandoned` (§9.9)
Deferred offers OUTSIDE X's window belong to no resolved ride. The dropoff handler
MUST NOT recompute them (no bridge), and MUST NOT leave them lingering `deferred`:
it sets `expected_odometer_status = 'abandoned'`, and the live predicate's
`expected_odometer_status IS DISTINCT FROM 'abandoned'` clause (§9.9.2) excludes
them from the live set immediately — no 4h linger.

This replaces the original "LEFT for the Horizon GC" disposition, which the GC
structurally cannot honor: its SELECT requires `actual_pickup_at IS NOT NULL`, so a
never-picked-up deferred offer is invisible to it. This is NOT a second killing
authority: the predicate remains the single liveness authority; the dropoff handler
only WRITES a status the predicate consumes (the same pattern as the pickup-fire
deferred→active writer). The abandonment runs even when X carries no chaining anchor
(§9.9.6) — it keys only on window membership. See FINDING §9.9.

### INV-5 — Window-scoping is the load-bearing guardrail
Recomputing a deferred offer against a trip it did NOT belong to would produce a
confident-wrong anchor — the exact failure the sentinel exists to prevent, by a
different door. The window confines recompute to the trip the offer actually
belonged to. This is the single most important correctness property.

### INV-6 — Verdict-blindness
Deferral, recompute, and reap MUST key only on receipt-time, window membership,
and the live-offer predicate. They MUST NEVER consult `app_verdict` (ACCEPT/
DECLINE). The car's physical position is the sole sensor of driver intent; a
driven-declined offer is observed identically to a driven-accepted one.

### INV-7 — Best-effort, never breaks the dropoff
A recompute failure MUST log and return without failing the dropoff fire. Liveness
of the dropoff over completeness of the resurrection.

---

## 3. Where each resolution executes (the ratified split)

- **Class A (dropoff-leg deferred — pickup fired late/lost):** resolves at the
  pickup-fire UPDATEs. The post-pickup odometer is the real anchor; set
  expected_odometer + status='active' on the existing UPDATE. No new authority.

- **Class B (receipt-deferred — pure lost-mode arrival):** resolves at the DROPOFF
  handler via INV-3's window-scoped recompute. Reap stays at the GC (INV-4).

Why recompute lives at the dropoff handler and NOT the GC: the GC's offer SELECT
requires `actual_pickup_at IS NOT NULL`, so it is structurally blind to receipt-
deferred (never-picked-up) offers — it cannot own the recompute without distorting
the single-authority contract. The dropoff handler is where the disambiguating
event, the window bounds, and the fire odometer are all in scope. Reap stays with
the GC because the GC is the single killing authority.

---

## 4. Observable signals (what a witness/test reads)

- `offer_history.expected_odometer_status`: NULL (legacy/pre-sentinel) | 'deferred'
  | 'active'.
- `offer_history.expected_odometer`: the band center (== expected_pickup_distance),
  or NULL while deferred.
- On a successful Class B recompute the deployed service logs:
  `[deferred-sentinel] recomputed offer=<D> in window of X=<X> (expected_odometer=...)`.
- A genuine witness MUST observe (a) the DB flip deferred→active with the chained
  anchor value, AND (b) the log line — proving the deployed dropoff HANDLER invoked
  the recompute, not merely that the DB ended up correct.

---

## 5. What "deferred-ness" must NOT be faked

A deferred offer's status MUST arise from the real receipt path computing a NULL
anchor under lost-mode conditions — NEVER from a direct UPDATE setting
status='deferred'. A test that hand-paints the precondition proves nothing about
whether the deployed code produces deferred state correctly. Any witness must drive
a genuine lost-mode receipt through the real API.

---

## 6. Current proof status (honest)

- Unit-proven (resurrection + abandonment, §9.2/§9.9):
  `tests/test_deferred_sentinel_recompute.py` (in-window→active, out-of-window→
  abandoned, no-anchor-still-abandons) + `tests/test_abandoned_excluded_from_
  predicate.py` (predicate excludes abandoned, incl. NULL-band independence) +
  guard-test `tests/test_chaining_ignores_prev_offer_attrs.py`. §9.9 floor at
  `23dabb1`: 773/1/0.
- Unit-proven (trigger, fix #2): `tests/test_lost_mode_deferral_trigger.py` — the
  four-way decision tree (lost-mode→deferred, idle→active, stacked→active,
  odometer-absent→deferred), the set-identity regression against the heartbeat
  detector in the MOVING case (the assertion that distinguishes effective_last_move
  parity from the raw-timestamp subset bug), the INV-A genuine-idle guard, and the
  fail-closed error-path test (detection error → 'deferred', never a fabricated
  'active' anchor).
- Deployed: rev 00648-lwx serving the PRE-fix trigger (missing-odometer only).
- Why `deferred_count = 0` in the wild pre-fix: the deployed trigger keyed on a
  missing odometer, which the current Android client never sends — so it never fired
  on the lost-mode condition it was designed for (the trigger mismatch fix #2
  corrects). Post-fix it fires on real lost-mode.
- STILL NOT production-witnessed: a genuine lost-mode receipt landing `deferred`
  through the live API (and a real lost-mode drive) remains the open empirical task —
  the fix→test→**drive** step. No replay substitutes for it (R2).

---

## 7. Deferred follow-ups

- **SQL/Python effective_last_move agreement test — DEFERRED from fix #2.** The
  Python helper `driver_queue.compute_effective_last_move` and the SQL `CASE` that
  persists `last_odometer_move_at` (inline in the heartbeat's `driver_trip_state`
  UPDATE, `driver_heartbeat.py`) encode the same "now-if-moved-else-stored" rule —
  "equivalent by construction," but currently *unenforced* by any test. A true
  runtime cross-check (run both on identical inputs, assert equal) requires
  extracting that UPDATE's SQL to a module constant so a test can execute the real
  CASE — a heartbeat-UPDATE refactor out of scope for the trigger commit. Deferred
  deliberately: fix #2 did NOT touch the CASE and *consolidated* the Python side
  into one helper (replacing the prior inline copy), so it lowered drift risk rather
  than raising it. A source-text-only pin was considered and rejected (weak proof;
  would need rework when the real test lands). Pick this up as: extract
  `DRIVER_TRIP_STATE_HEARTBEAT_UPDATE_SQL` + a live-PG agreement test.
