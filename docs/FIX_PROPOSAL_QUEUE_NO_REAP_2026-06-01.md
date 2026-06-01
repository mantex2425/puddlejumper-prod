# FIX PROPOSAL — Monitor queue surfaces phantom offers (2026-06-01)

**Status:** Claude proposal, pending Gemini review.
**Recon:** `docs/RECON_QUEUE_NO_REAP_2026-06-01.md`
**Branch base:** `fix/three-recon-bugs-2026-05-30` @ `0dfc154`

---

## §0 Problem statement (one paragraph)

`driver_status.py:187-189` invokes `DriverQueue(driver_id).offer_ids_only(cur)`
with no `current_cumulative_miles` and no `last_odometer_move_at`. Both
default to `None`, which causes `LIVE_OFFER_PREDICATE_SQL`'s distance gate
to short-circuit (NULL guard) and its odometer-staleness gate to behave
permissively. The predicate then reaps only via causality and the 4h
abandonment ceiling. On 2026-06-01 this surfaced 6 phantom offers on the
monitor at +7h post-drive while the heartbeat handler — which threads the
real per-tick state — correctly considered the queue empty (recon §3).
Only one production caller is affected; only one surface (the monitor /
`planner_queue` field of `GET /api/v1/driver/status`) is wrong.

---

## §1 Proposed change

### 1.1 Thread odometer state at the call site (primary fix)

`driver_status.py` already SELECTs `driver_trip_state.heartbeat`
(line 39); `cumulative_miles` is already in the local `hb` dict
(line 140 reads `hb.get("cumulative_miles")` for the API response).
We add `last_odometer_move_at` to the existing SELECT and thread both
signals into `offer_ids_only`.

**Diff sketch (driver_status.py):**

```python
# Lines 33-43 — add last_odometer_move_at to the SELECT
cur.execute("""
    SELECT
        current_offer_id,
        pickup_lat, pickup_lng,
        dropoff_lat, dropoff_lng,
        potential_cancellation,
        heartbeat,
        heartbeat_at,
        last_odometer_move_at          -- NEW
    FROM app_private.driver_trip_state
    WHERE driver_id = %s
""", (driver_id,))
state_row = cur.fetchone()
hb = state_row["heartbeat"] if state_row and state_row["heartbeat"] else {}

# Lines 187-189 — thread real per-tick state through the predicate
"planner_queue": list(
    DriverQueue(driver_id).offer_ids_only(
        cur,
        current_cumulative_miles=hb.get("cumulative_miles"),
        last_odometer_move_at=state_row["last_odometer_move_at"] if state_row else None,
    )
),
```

That's the whole proximate fix. Two-line SELECT extension and one
kwarg-bearing call rewrite.

**Why this is safe:**
- `hb.get("cumulative_miles")` may still return `None` on first heartbeat
  or in a driver-not-yet-started state. The predicate handles NULL via
  the same NULL-guard that currently fires for every monitor request —
  no regression risk for those moments. The change only adds correctness
  to the case where the data is present.
- `last_odometer_move_at` may be NULL for a brand-new driver_trip_state
  row. Same NULL-tolerance argument.
- No new transaction shape, no new query. The data is one column-add
  away on a SELECT we already run.

### 1.2 Signature hardening (recommended companion change)

`offer_ids_only(self, cur, current_cumulative_miles=None, last_odometer_move_at=None)`
is now and forever a footgun: defaults silently degrade the predicate to
"causality + 4h ceiling only." Today there is exactly one production caller
(this one), so removing the defaults is cheap:

```python
def offer_ids_only(
    self,
    cur,
    *,
    current_cumulative_miles,
    last_odometer_move_at,
) -> tuple[str, ...]:
```

Making them keyword-only and required forces every future caller to
think about what they pass. Tests update mechanically (most pass
sentinels or `None` explicitly already — see §2). This is the kind of
small-tax-now-big-trust-later refactor §XIV.H's "predicate is canonical"
spirit favors.

**Rejected alternative considered:** keep defaults but warn-log when
both are NULL. That treats every monitor poll (3 sec cadence) as a
warn-loggable event and amounts to noise; the type system can prevent
this cleanly instead.

### 1.3 Spatial gate (out of scope; record for the next loop)

The recon §4 distance-from-driver column shows the 6 phantom offers
sit 29-35 miles from the driver's last known position. A future
**spatial gate** in `LIVE_OFFER_PREDICATE_SQL` — "reap if straight-line
distance from current heartbeat exceeds N miles" — would catch the
case where odometer freezes legitimately (driver stops to pee, but
the offers are still physically distant). That is defense-in-depth,
not the proximate fix. Hold for a separate proposal.

---

## §2 Test plan

### 2.1 Regression test (new)

`tests/test_driver_status_planner_queue.py` (new file or extend
existing `tests/test_driver_status.py` if present):

- Scenario A: `driver_trip_state.last_odometer_move_at` is fresh AND
  `heartbeat.cumulative_miles` is past every offer's envelope ⇒
  `planner_queue` should be `[]` (heartbeat-path predicate output).
- Scenario B: `driver_trip_state.last_odometer_move_at` is NULL (brand
  new driver) ⇒ `planner_queue` falls back to time-only reap (current
  behavior preserved for the degenerate case).
- Scenario C: `heartbeat.cumulative_miles` < envelope cap for one offer
  but > envelope cap for others ⇒ `planner_queue` contains only the
  surviving offer.

Each scenario fixtures the offer_history rows + driver_trip_state row
and asserts on the JSON payload. The 2026-06-01 production case maps
to Scenario A directly.

### 2.2 Predicate-shape test (existing — verify still green)

`tests/test_driver_queue.py:161` —
`test_offer_ids_only_query_uses_canonical_gc_constants` already
pins the canonical predicate text. Signature change in §1.2 must not
break that test; only the function arguments shift.

### 2.3 Callers smoke (existing — verify still green)

Pytest baseline is **680 passed / 1 skipped** (recon-time confirmation).
After the fix, target is **683 passed / 1 skipped** (three new
scenarios). If any test fails that isn't the three new ones, stop and
investigate.

### 2.4 Live verification

After deploy, against the same driver:
1. Run the §4 monitor-path query from the recon doc — expect empty set
   (no longer mirrors the NULL/NULL call).
2. Hit `/api/v1/driver/status` and assert `planner_queue == []`.
3. Trigger a new offer; assert it appears in `planner_queue` until the
   driver physically departs the envelope, then disappears on the next
   poll.

---

## §3 Blast radius

| Layer | Change | Risk |
|---|---|---|
| `driver_status.py` SELECT | +1 column (`last_odometer_move_at`) | None — table already has the column; no schema work. |
| `driver_status.py` call | Thread two kwargs into `offer_ids_only` | None — the kwargs already exist on the function. |
| `driver_queue.py` signature | (Optional, §1.2) Remove defaults | Touches 1 prod call site (the fix above) + N test sites. Test refactor is mechanical. |
| Data | None | Read-only, no migrations. |
| API contract | `planner_queue` may shrink for some drivers | **Intentional** — that's the fix. UI consumers see at most a length change; no field shape change. |

---

## §4 Open questions for Gemini

1. **Signature hardening: in or out of this PR?** Keep this PR minimal
   (call-site only) and split the keyword-only refactor into a follow-up,
   or bundle? Argument for bundling: only-one-caller-today makes the
   refactor trivial *now* and expensive *later*. Argument for splitting:
   minimal blast for a forensic-driven fix.

2. **`snapshot()` and `offers()` parity.** Both already require the
   kwargs at call sites that matter (`driver_heartbeat.py:1659` passes
   them); they're nominally optional in signature but every prod caller
   threads real values. Should §1.2's "keyword-only + required" apply
   to all three queue read entry points uniformly? Symmetry argument
   says yes.

3. **Phantom-queue forensics retention.** Should we add a one-line
   `log.info` on the `driver_status.py` queue read when the threaded
   set differs from a NULL-NULL set, just for the first week post-deploy?
   This would give us positive evidence that the fix did the work we
   expect, beyond ad-hoc monitor observation. Removed after one week.

4. **§XIV.H amendment.** The canonical-rules §XIV.H text (line 647)
   says "production hot-path code MUST go through the predicate." It
   doesn't say "must thread the predicate's *parameters*." Worth a one-
   sentence amendment: "callers in production hot paths must supply
   real per-tick `current_cumulative_miles` and `last_odometer_move_at`
   when available; NULL is reserved for replay/forensic use."

---

## §5 What this proposal explicitly does NOT do

- No spatial gate (deferred — §1.3).
- No change to `LIVE_OFFER_PREDICATE_SQL` text or constants.
- No change to the heartbeat handler — that path is already correct.
- No new endpoint or schema work.
- No deploy in this loop. Proposal first, Gemini review, then patch +
  test + deploy as a separate step.

---

## §6 Sequencing (if approved)

1. Edit `driver_status.py` per §1.1 (proximate fix).
2. Add `tests/test_driver_status_planner_queue.py` per §2.1.
3. Run pytest → expect 683/1.
4. (If §1.2 in scope this PR) edit `driver_queue.py` signature, update
   existing tests that pass positional args, rerun pytest.
5. (If §4.4 in scope) one-paragraph amendment to `docs/CANONICAL_RULES.md`
   §XIV.H.
6. Single commit (or stacked PR if §1.2 splits), push, deploy via the
   standard Cloud Run path.
7. Post-deploy: run §2.4 live verification.
