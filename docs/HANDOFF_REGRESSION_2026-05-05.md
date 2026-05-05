# Handoff — Matcher Dispatch Regression

**Date:** 2026-05-05
**Status:** Production regression confirmed. Matcher hasn't fired today. Phase 2 work paused.
**Severity:** Silent — heartbeats process normally, clusters form normally, `pudo_decision_context` rows write normally. Only the WAI matcher is dark.

---

## The evidence

Same driver (`UjT1hE9eBXh2q95aSZYOkzDJ8lo1`), same `pudo_decision_context` query, two days:

```
       day        | rows | has_action | action_non_noop | has_conf | has_cluster | conf_with_cluster
------------------+------+------------+-----------------+----------+-------------+-------------------
 YESTERDAY (5-04) | 5424 |       5424 |            5424 |      113 |        4373 |               113
 TODAY (5-05)     | 9618 |       7689 |            7689 |        0 |        9361 |                 0
```

- `wai_confidence` populated yesterday (113 rows / 4,373 clusters ≈ 2.6% match rate)
- `wai_confidence` is NULL on every single today row (0 / 9,361 = 0%)
- Pattern is total absence, not threshold change. The matcher is not running.
- Topology fields (`wai_current_road`, `wai_current_road_class`) are populated today
  per Phase 1B, so the heartbeat handler IS reaching the topology probe — it's the
  matcher dispatch *after* topology that's gone dark.

Today's full McKeever drive forensic (104 heartbeats during 14:16-14:26 UTC,
40 cluster rows including a perfect 11-frame stationary cluster at the dentist
parking lot at 09:21-09:22 local): zero matcher firings. Offer 7712 (Sienna Pkwy
dropoff, manually queued) was in `current_offer_id` throughout. Matcher should
have evaluated against it on every cluster. It didn't.

---

## The bisect window

Two commits between yesterday's working state (`00589-whc`) and today's broken
state (`00591-vj7`):

- `574b53e` — phase 1a: transit-class adjacency gate
- `7fe4391` — phase 1b: forensic restoration of pudo_decision_context

Phase 1A is small and surgical (one signal function gated on road class). Phase 1B
rewrote `_log_decision_context` significantly, including a parameter rename from
`actions` → `executed_actions`. Per PHASE_1B_CLOSEOUT.md L-12 corollary: signature
changes must grep all invocation sites in `tests/` AND production. The closeout
doesn't explicitly confirm the production caller in `driver_heartbeat.py` was
updated to pass `executed_actions`. Worth that being the first place to look.

Bruno passed 17/17 against `00591-vj7`. Bruno doesn't exercise the matcher dispatch
path in a way that would expose this — Bruno simulates pickup/dropoff confirmation,
not arbitrary in-trip cluster evaluation. The integration suite (22/61 per
INDEX.md, with most failures pre-dating Phase C) likely doesn't cover this path
either, which is why pytest 250/250 → 257/257 didn't catch it.

---

## Why this matters for Phase 2

PHASE_2A_CLOSEOUT.md committed `poi_service.py` cache-only foundation. Phase 2b
was scoped to add Google Places integration. **Both are useless until the matcher
dispatches** — POI signals exist to flow into matcher confidence calculations.
Building Phase 2b on a system where confidence calculation isn't running would
have been hours of wasted authoring against an unobserved code path.

The drive forensic was originally pitched as a "baseline establishment for
Phase 2 A/B comparison." It became a regression detector.

---

## Recommended first move

Open `driver_heartbeat.py` and find the call site of `_log_decision_context`.
Confirm:

1. Is the orchestrator passing `executed_actions=...` correctly?
2. More fundamentally: where is the WAI matcher actually invoked? Find
   `WhereAmI.evaluate(...)` callers. Is the call still happening for clusters?
3. Is there an early-return path that skips matcher dispatch when topology
   succeeds but some other condition fails? (Yesterday: 113 firings out of 4,373
   clusters means there IS a gate normally; today's gate may have moved to
   "always closed.")

If `WhereAmI.evaluate` is still being called but returns empty matches, the
issue is inside the matcher. If it's not being called, the issue is upstream
in dispatch logic.

`git diff 00589-whc..00591-vj7 -- driver_heartbeat.py where_am_i.py dispatch.py`
or the SHA equivalents (`e61054c..7fe4391` for the two-commit window) will
narrow the surface fast.

---

## Repository state

- Branch: `demolition-2026-05-04` HEAD `d83b2a0` (PHASE_2A_CLOSEOUT.md)
- Live: `puddlejumper-api-00591-vj7`
- Pytest: 257/257 (matcher-dispatch path uncovered)
- Phase 2a shipped, Phase 2b NOT YET STARTED, paused until regression resolved

The `app_private.poi_cache` table is in production with 0 rows. `poi_service.py`
exists in code with no callers. None of this needs touching for the regression hunt.

---

## What to load in the next session

- `docs/INDEX.md`
- `docs/CANONICAL_RULES.md`, `docs/SESSION_PROTOCOL.md`
- `docs/PHASE_1B_CLOSEOUT.md` — particularly the `_log_decision_context` rewrite
  and L-12 corollary
- `docs/PHASE_2A_CLOSEOUT.md` — context for what's deployed
- This handoff
- After cracking it open: `driver_heartbeat.py`, `where_am_i.py`,
  `dispatch.py` source

Don't reload `OPERATION_STRIP_MALL_PROPOSAL.md` — Phase 2 is paused.

---

## What NOT to do in the next session

- Don't start with the live drive again. The forensic data is already captured;
  re-running burns time.
- Don't open Phase 2b. Phase 2b is fine. The thing under it is broken.
- Don't blame Phase 1A first. It's smaller and more surgical than 1B. Look at
  the bigger blast radius first.
- Don't rollback to `00589-whc` reflexively without understanding the regression.
  1B's forensic restoration is genuinely valuable infrastructure; we want to
  diagnose, not abandon.

---

## State of the queue

Driver `UjT1hE9eBXh2q95aSZYOkzDJ8lo1` has `current_offer_id = '7712'` queued
for the regression hunt. Leave it. If a fix lands and we want to validate live,
the queue is already primed for a McKeever-Sienna drive replay.