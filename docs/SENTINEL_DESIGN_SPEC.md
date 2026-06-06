# §5.5 Deferred Sentinel — Design Intent & Required Behavior

Purpose of this doc: give CC (or any implementer/tester) the *intent and required
behavior* of the deferred sentinel, distilled from the forensic FINDING. The
authoritative source remains `docs/FINDING_ODOMETER_GC_SPEC_RECONCILIATION_
2026-06-05.md` §5.5 + §9.1–9.8; this is the readable specification of what it must
do and why. If this doc and the FINDING ever disagree, the FINDING wins.

Deployed in rev `puddlejumper-api-00648-lwx`, branch
`fix/restore-fire-error-metric-2026-06-02` @ HEAD `681eccb`.

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

### INV-1 — Deferral on lost-mode receipt
When an offer is received in lost mode (no computable anchor), the system MUST set
`expected_odometer = NULL` and `expected_odometer_status = 'deferred'`. NULL is the
sentinel — never a magic number. (A number in a miles column silently corrupts any
consumer that forgets to guard it; NULL forces every consumer to branch or fail
loud.)

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

### INV-4 — Out-of-window deferred offers are LEFT for the GC (no second reaper)
Deferred offers OUTSIDE X's window belong to an earlier, unresolved segment. The
dropoff handler MUST NOT reap or recompute them. They are left for the upstream
Horizon Budget GC to sweep on its own authority + 4h abandonment ceiling. There is
exactly ONE killing authority (the GC). The sentinel only ever WRITES anchors
(receipt, pickup-fire, dropoff-recompute); it never sets liveness false.

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

- Unit-proven: live-PG test `tests/test_deferred_sentinel_recompute.py` (3/3) +
  guard-test `tests/test_chaining_ignores_prev_offer_attrs.py` (2/2). Floor 770/1/0.
- Deployed: rev 00648-lwx serving.
- NOT yet production-witnessed: `deferred_count=0` since deploy; the deferred path
  has never fired in the wild. Witnessing it (driving a genuine lost-mode receipt
  through the live API) is the open task.
