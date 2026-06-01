# FIX PROPOSAL — §XVIII cold-start bind: global gate → spatial-local competing set (2026-06-01)

**Status:** Claude proposal, pending Gemini review.
**Recon:** `docs/RECON_PICKUP_NOBIND_2026-06-01.md` (H-A confirmed),
`docs/RECON_COMMITS_NULL_HANDLING_2026-06-01.md` (this proposal records the
`_commits` null-handling verdict inline — see §2.3 — since that recon doc was
not separately written this session).
**Branch base:** `fix/three-recon-bugs-2026-05-30` @ `0dfc154`.
**Prod rev:** `puddlejumper-api-00638-wpg`.

---

## §0 Problem statement (one paragraph)

The §XVIII cold-start bind gate at `driver_heartbeat.py:608` is:

```python
if alive_unpicked_offer_ids == frozenset({str(action.offer_id)}):
    queue.bind(action.offer_id, cur)
```

`alive_unpicked_offer_ids` is a **global** set — every offer for this driver
that is live (passes `LIVE_OFFER_PREDICATE_SQL`) and unpicked, regardless of
where the driver physically is. On the 2026-06-01 AM drive this gate had its
precondition satisfied for exactly **1 of 7** pickup-class fires (14%); the
other six were correctly-by-the-code-but-wrongly-for-the-product withheld
because ≥2 offers were alive-unpicked — even though the other offers were
miles away and scored WAI ~0.19 (far below any floor) at the arrest. The
result was an ~80%-lost-mode shift: pickups fired as observations, narrative
never bound, downstream narrative optimization never engaged. H-A confirmed:
**no code defect; the gate's definition of "ambiguity" is wrong.** Ambiguity
is being measured globally (is there only one offer on the whole clipboard?)
when it should be measured locally (is there only one offer competing for
*this* piece of asphalt?).

---

## §1 Proposed change

### 1.1 The competing-set redefinition (the whole fix)

Replace the global alive-unpicked singleton test with a **leg-matched,
WAI-floor-cleared, alive-unpicked** singleton test. The "local competing set"
for a pickup bind is the set of offers that, at THIS heartbeat:

1. WAI evaluated on the **pickup leg**, AND
2. **cleared the commit floor** per the canonical `_commits` predicate
   (`where_am_i.py:1790-1835`), AND
3. are **alive AND unpicked** (intersect with `alive_unpicked_offer_ids`,
   which already filters `actual_pickup_at IS NULL` per §XVIII).

```python
# diagnostics + tad_verdicts threaded into _execute_action (see §1.2)
pickup_floor_clearers = frozenset(
    str(offer_id)
    for offer_id, leg, outcome in diagnostics.per_target_outcomes
    if leg == 'pickup'
    and _commits(outcome, tad_verdicts.get(offer_id))
)
# Intersect with alive-unpicked to retire already-fired pickups (§1.3).
local_competing = pickup_floor_clearers & alive_unpicked_offer_ids

if local_competing == frozenset({str(action.offer_id)}):
    queue.bind(action.offer_id, cur)
    log.info(
        "[§XVIII spatial-local bind] FirePickupObservation offer=%s "
        "narrative bound (sole pickup-floor-clearer among alive-unpicked: %s)",
        action.offer_id, sorted(local_competing),
    )
```

**Equality, not membership, is preserved.** The gate still binds only when
`action.offer_id` is the *sole* member of the local competing set. Two offers
genuinely clearing the pickup floor at the same curb (real shared-curb
ambiguity) → set size 2 → withhold, observation-only, §XIV.I §5.3 intact. The
§0.D.4 wrong-bind protection is unchanged. We have only stopped counting
offers that were never spatial candidates in the first place (cross-town
offers scoring below floor).

### 1.2 Threading `diagnostics` + `tad_verdicts` to the FPO handler

`_execute_action` already receives diagnostic-shaped kwargs
(`cluster=diagnostics.cluster`, `alive_unpicked_offer_ids=...`). Per the
2026-06-01 seam recon, `matches` and `diagnostics` are in local scope at the
invocation site (`driver_heartbeat.py:2036`). We thread two more values —
`per_target_outcomes` and `tad_verdicts` — following the exact established
pattern. To keep the handler contract minimal, pass the two projections rather
than the whole `DiagnosticContext`:

```python
# at the _execute_action call site (~line 2036):
_execute_action(
    action, cur, conn, driver_id, queue,
    cluster=diagnostics.cluster,
    ...,
    alive_unpicked_offer_ids=alive_unpicked_offer_ids,
    pickup_floor_clearers=pickup_floor_clearers,   # NEW — precomputed once, see below
)
```

**Design choice for Gemini (§4.1):** compute `pickup_floor_clearers` ONCE in
the heartbeat body (where `diagnostics` and `tad_verdicts` already live) and
pass the finished frozenset into `_execute_action`, rather than threading the
raw `per_target_outcomes` + `tad_verdicts` and recomputing inside the handler.
This mirrors how `alive_unpicked_offer_ids` is computed once at line 1812 and
threaded. It keeps `_execute_action`'s signature carrying finished sets, not
raw diagnostic structures, and avoids recomputation if multiple actions
execute in one heartbeat.

### 1.3 Leg-retirement folded in (no separate fix)

The recon's §4 surfaced an independent anomaly: the matcher kept emitting
`FirePickupObservation` on offer 8739's **pickup leg** across four heartbeats
*after* `actual_pickup_at` was already written (08:36/08:41/08:44/08:46). Under
a naive `pickup_floor_clearers` definition, an already-picked offer that keeps
clearing the pickup floor would remain in the set and could inflate it —
re-blocking a legitimately-solo bind on a *different* offer.

The `& alive_unpicked_offer_ids` intersection in §1.1 resolves this for the
bind gate: `alive_unpicked_offer_ids` already filters `actual_pickup_at IS
NULL`, so an already-picked offer is excluded from the competing set
regardless of whether WAI keeps scoring its pickup leg. This does NOT fix the
underlying matcher behavior (WAI still re-scoring a retired leg — that remains
a separate doctrine/matcher question per §5), but it prevents that behavior
from corrupting the bind decision.

### 1.4 Single-source-of-truth discipline

The floor check delegates to `_commits` verbatim — never a hardcoded `>= 0.40`
or `>= 0.55`. `_commits` is the canonical floor predicate; a future floor
change (or POI-lift change) flows to the bind gate automatically. This mirrors
the discipline that pinned `_get_alive_unpicked_offer_ids` as the single
source for the bit-2 predicate. A new test pins this delegation (§2.2).

---

## §2 Why this is correct (evidence + the `_commits` verdict)

### 2.1 Replaying 2026-06-01 fire #1 (08:36, 8739) through the new gate

At that arrest, `diagnostics.per_target_outcomes` pickup-leg scores were:

| offer | pickup-leg confidence | TAD verdict.passed | `_commits` floor | clears? |
|---|---|---|---|---|
| 8739 | 0.628 | None (lost) | 0.55 (LOST) | **YES** |
| 8737 | 0.186 | None (lost) | 0.55 (LOST) | no |
| 8736 | 0.186 | None (lost) | 0.55 (LOST) | no |

`pickup_floor_clearers = {8739}`. Intersect with alive_unpicked `{8736,8737,8739}`
→ `{8739}`. `== frozenset({8739})` → **bind fires.** Under the OLD gate this
was set-size 3 → withheld. The new gate binds correctly.

### 2.2 Replaying the airport/Cypress protection (10:45 cluster)

At the 10:45 fires, the entire 8742-8748 Cypress cohort was alive-unpicked. IF
multiple of them cleared the pickup floor at that single cluster (legitimate
stacked-offers-at-one-location case), `pickup_floor_clearers` would be size ≥2,
the equality test fails, and the system correctly stays observation-only. The
§0.D.4 false-positive shield is preserved by construction. (Whether they
actually multi-cleared is a forensic the live test will fixture both ways.)

### 2.3 The `_commits` null-handling verdict (GATING — confirmed GREEN)

`_commits` (`where_am_i.py:1790-1835`) has three commit branches plus a
defensive fall-through:

- `verdict is None` (no TAD wired / bridge) → `conf >= WAI_CONFIDENCE_THRESHOLD` (0.40)
- `verdict.passed is True` (Normal) → `conf >= COMMIT_NORMAL_FLOOR` (0.40)
- **`verdict.passed is None` (Lost Mode) → `conf >= COMMIT_LOST_FLOOR` (0.55)**
- `verdict.passed is False` → `return False` (Step 5 already skipped; defensive)

The 2026-06-01 lost-mode fires carried `verdict.passed = None` (a real verdict
object, lost branch), so the **0.55 lost floor** applies — NOT the 0.40 bridge
floor. 8739 at 0.628 clears 0.55; the cross-town offers at 0.186 do not.
**Delegating to `_commits` is correct and needs no TAD-blind variant.** The
0.55 lost floor is *more* conservative than a flat 0.40, which strengthens the
§0.D.4 wrong-bind protection (higher bar to enter the competing set).

---

## §3 Test plan

### 3.1 New live-PG regression test (REQUIRED, §XIV.J)

`tests/test_spatial_local_bind.py` (new), using the `db_cur` real-PG fixture +
`test_driver_id` + seed fixtures. This is the test that would have caught the
2026-06-01 shift and is mandatory per §XIV.J (no MagicMock — the gate depends
on real predicate + real row state).

- **Scenario A (the morning case — binds):** seed 3 alive-unpicked offers;
  fixture `per_target_outcomes` so one clears the pickup floor (lost-mode,
  conf 0.628 ≥ 0.55) and two do not (0.186). Assert the bind fires and
  `current_offer_id` binds to the clearer.
- **Scenario B (shared-curb ambiguity — withholds):** seed 2 alive-unpicked
  offers BOTH clearing the pickup floor at one cluster. Assert NO bind;
  `current_offer_id` stays NULL; both fire observation per §XIV.I §5.3.
- **Scenario C (leg-retirement — §1.3):** seed offer X already picked
  (`actual_pickup_at` set) but still clearing pickup floor in
  `per_target_outcomes`, plus offer Y alive-unpicked and solo-clearing.
  Assert the bind fires for Y (X excluded by the alive-unpicked intersection),
  proving an already-picked offer can't block a fresh bind.
- **Scenario D (normal-mode floor):** verify a `verdict.passed=True` offer uses
  the 0.40 normal floor via `_commits`, not the 0.55 lost floor — pins that the
  gate delegates to `_commits` rather than hardcoding lost-floor.

### 3.2 Single-source delegation pin

Assert the gate calls `_commits` (e.g. via a spy/patch on `_commits` confirming
it's consulted per candidate) rather than comparing confidence to a literal.
Mirrors `test_live_offer_predicate_imports.py`'s single-source enforcement.

### 3.3 Existing suite

Baseline is **683 passed / 1 skipped** (post-JOB-2, assuming JOB 2 committed
first; if JOB 2 not yet committed, baseline 680/1). New scenarios target +4.
Any non-new failure → stop and investigate. In particular, the existing
`§XVIII cold-start bind` tests (the `== frozenset({offer})` ones) must be
reviewed: some will need their assertions updated from "global singleton" to
"local-competing singleton." Inventory them as part of execution.

### 3.4 Live verification (post-deploy)

Next stacked-morning drive: confirm via PDC that pickups with a sole
pickup-floor-clearer bind (`current_offer_id` non-NULL, `match_signal`
narrative not `lost_mode_observation`), while genuine same-curb multi-clears
stay observation-only.

---

## §4 Open questions for Gemini

1. **Compute-once-and-thread vs thread-raw-and-recompute (§1.2).** Proposal
   computes `pickup_floor_clearers` once in the heartbeat body and threads the
   finished frozenset. Alternative: thread `per_target_outcomes` + `tad_verdicts`
   raw and compute inside the FPO handler. Compute-once mirrors
   `alive_unpicked_offer_ids` and avoids recompute across multiple actions in
   one heartbeat — but couples the heartbeat body to the bind's set logic. Which
   seam is cleaner?

2. **Should the dropoff/clear paths get a symmetric local-competing treatment?**
   This proposal only touches the pickup bind. Dropoff narrative-clear is a
   different mechanic, but is there a parallel "global vs local" bug lurking on
   the dropoff side worth a recon before we declare the bind subsystem fixed?

3. **The retired-leg matcher behavior (§1.3, §5).** This fix prevents an
   already-picked offer from corrupting the bind, but does NOT stop WAI from
   re-scoring a retired pickup leg (the 8739 4×-FPO behavior). Acceptable to
   defer that to a separate matcher-doctrine recon, or does it need to land
   with this fix?

4. **Canonical amendment timing.** §XVIII.A's binding-paths text currently
   describes the singleton gate in global terms ("alive-unpicked set is exactly
   {offer_id}"). Per Session Protocol (docs capture outcomes, not plans), the
   amendment lands AFTER this fix ships and validates on a drive — not in this
   PR. Confirm agreement, or argue for amending alongside.

---

## §5 What this proposal explicitly does NOT do

- No change to `_commits`, `LIVE_OFFER_PREDICATE_SQL`, or any floor constant.
- No fix to WAI's re-scoring of retired legs (§1.3 contains its *effect* on the
  bind; the matcher behavior itself is a separate recon — open question §4.3).
- No dropoff-side change (open question §4.2).
- No canonical-rules amendment in this PR (open question §4.4).
- No deploy in this loop. Proposal → Gemini → patch + test → pytest green →
  staged → gated deploy → live drive validation → THEN canon amendment.

---

## §6 Sequencing (if approved)

1. Confirm JOB 2 committed (separate, independent) so the baseline is clean.
2. Compute `pickup_floor_clearers` in the heartbeat body; thread into
   `_execute_action` per §1.2 (whichever seam Gemini picks in §4.1).
3. Replace the gate at `driver_heartbeat.py:608` per §1.1.
4. Inventory + update existing `§XVIII cold-start bind` tests (§3.3).
5. Add `tests/test_spatial_local_bind.py` (§3.1) + the delegation pin (§3.2).
6. pytest → expect baseline +4.
7. Single commit, push. Do NOT deploy.
8. Gated deploy decision → live stacked-morning drive → validate per §3.4.
9. ONLY after validation: amend `docs/CANONICAL_RULES.md` §XVIII.A.
