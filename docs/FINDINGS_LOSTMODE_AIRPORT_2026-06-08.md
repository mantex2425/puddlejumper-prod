# Findings + Fix Plan — 2026-06-08 lost-mode 0/5 drive (cascade + airport-pickup gap)

**Status:** diagnosis complete, source-cited, two verifications passed. Fix NOT drafted yet
(this doc precedes code, per L-6). Mission anchor: **>95% pickup *location* accuracy** (within 100 m
of the manual tap). The 06-08 morning drive scored **0/5**.

This arc overturned several earlier conclusions; the honest record of *what was wrong and why* is
§6, so the reasoning is canon-adjacent rather than buried in chat.

---

## §1 The cascade root cause (why one phantom blocked the whole drive) — source-cited

1. **A phantom pins lost-mode.** `10550` ("Terminal D/E", NULL geocode, re-processed at app
   startup 04:36, decided *before the Uber app was on*) is `active` + unpicked, so it sits in
   `_get_alive_unpicked_offer_ids` (`driver_queue.py:446` — `LIVE_OFFER_PREDICATE_SQL` +
   `actual_pickup_at IS NULL`). `lost_mode = len(_get_alive_unpicked_offer_ids) > 0`
   (`driver_heartbeat.py:2107`). Because 10550 stayed `<4h` for the entire 05:39–08:05 drive, it held
   lost-mode **continuously** (736 `lost_mode_no_candidate` emissions).
2. **Lost-mode → blanket deferral.** `9acd504` (`decisions/logger.py:360`) defers every offer
   received in lost-mode by leaving the anchors NULL → status `deferred`. So 10629–10636 (the real
   pickups) all landed `deferred` with NULL `expected_pickup_distance`.
3. **Deferred → excluded from scoring.** `_assemble_per_offer_state` (`driver_heartbeat.py:1200`)
   does `if expected_pickup_distance is None: continue` — a guard written for *legacy pre-writer
   rows* now swallows every deferred offer → no `per_offer_state` entry.
4. **No state → TAD-fail → dispatch-skip.** `tad.py:466`: missing state → `TadVerdict(passed=False)`.
   `where_am_i.py:1817` (`_commits`): `passed=False` → "Step 5 skips dispatch … `per_target_outcomes`
   never contains them." So a deferred offer is **un-fireable at any confidence** — even 10629,
   parked 2 m away with a valid geocode.

**06-08 (0/5) vs 06-07 (3/8):** 06-07's lost-mode *flickered* — offers received in non-lost gaps kept
anchors → fireable. 06-08's phantom held lost-mode *continuously* → every offer deferred → all
skipped.

**Confirmed NOT the cause:** the 752-row unbounded backlog (`QUEUE_NO_REAP`) does **not** pin
lost-mode — lost-mode is 4h-bounded (uses `LIVE_OFFER_PREDICATE_SQL`); the bounded count was **0**.
The 752 is a monitor-display/hygiene issue (`driver_status.py` reads with null odometer), separate.

---

## §2 The airport-pickup gap (why a real terminal pickup is NULL) — the load-bearing find

`§XVII` (Semantic Anchor Engine, `poi_service.get_anchors_for_text` → Places `searchText` →
`poi_cache`) runs **at scoring time** (a matching signal in `_match_poi_class`,
`where_am_i.py:1399`), **NOT in the receipt geocode path** (which is plain
`bead_on_wire._google_geocode`). So:

- An airport terminal ("Terminal D/E, Departures: Zone 5E") → plain geocode → **NULL `pickup_lat`**
  at receipt. §XVII was never meant to populate the coordinate; it resolves venues *during matching*.
- **And it never even gets that chance on these:** `classify_address` (`bead_on_wire.py:327`) checks
  **intersection (line 358) before POI (line 385)**, and `INTERSECTION_REGEX` (`line 39`) includes
  `/`. "Terminal **D/E**" → the slash splits it into "Terminal D" / "E" → classed `intersection` →
  routed to the coord-requiring matcher → NULL coords → **"intersection skipped: NULL target coords"**
  (confirmed in 10471's 06-07 `pudo_decision_context`). `_match_poi_class`/§XVII is bypassed, even
  though `"terminal"` IS a POI token (`_EXTENDED_POI_TOKENS`).

So "we solved airport pickups" was half-right: §XVII solved airport *resolution as a scoring signal*
(IAH 0.824, `tests/test_semantic_anchor.py`), but it does not put a coordinate on the offer at
receipt, and the slash mis-classification bounces the offer before scoring. **Airport *pickups* were
never solved on the pickup leg.**

---

## §3 Why the "obvious" fixes were rejected (so we don't relitigate)

- **classify_address candidate (a) — "require both intersection split-parts to be road-like":**
  **KILLED empirically.** `_first_field_looks_like_road("Southbank")=False`, `("Main")=False`,
  `("5th")=False`, yet "Schevers St & Southbank" (offer 10630), "Main & 5th", "16th St &S Allen
  Genoa Rd" are all real intersections. The guard would reject real suffix-less intersections.
- **Reorder POI before intersection:** breaks "Terminal Road" (a real road) → mis-classed POI. The
  `"terminal"` token collides three ways: Terminal Road (road), Terminal Road & Main St
  (intersection), Terminal D/E (airport). No one-line classifier rule separates them.
- **Abandon-on-NULL-geocode (early framing):** KILLED — 10471 is a *real* airport pickup that is
  NULL-geocode; abandoning all NULL-geocode pickups concedes exactly the high-value class §XVII
  exists to catch.

**Conclusion:** chasing perfect classification is a rabbit hole. The right move is to *resolve* via
§XVII and *abandon only the genuinely-unresolvable residual*.

---

## §4 The fix set

**(F1) Move §XVII into the receipt geocode path — KILLED by the live test (2026-06-08).**
`get_anchors_for_text("Terminal D/E, Departures: Zone 5E")` returns **n=0 from a 45 km bias**, n=1
("George Bush Intercontinental Airport", `dist_m=0`) only from a bias AT IAH. "Terminal D/E" is
ambiguous without proximity, and the 50 km *soft* bias can't surface it from afar. So §XVII is
**inherently scoring-time** — it resolves the venue the driver is *at* (cluster bias), not a
not-yet-reached terminal at receipt. Receipt-time §XVII yields nothing. **Do not pursue F1.**

**(F1′) Airport fix, revised: classifier→POI + stay-in-normal-mode (no receipt §XVII).**
Airports are catchable via **scoring-time** §XVII, in **normal mode**:
  1. **Classifier routes "Terminal D/E" → `poi`** (not `intersection`), so `_match_poi_class`/§XVII
     runs. This is the hard part (the collision thicket — candidate-a dead, reorder breaks "Terminal
     Road"). Options: a targeted heuristic (a `/`-disjunction with single-letter parts → POI), or
     accept some airport pickups in the 5% via F2.
  2. **F2 keeps us in normal mode** (abandon the phantom → lost-mode clears → floor 0.40). The live
     test showed §XVII resolves the **airport centroid (~0.50** at a terminal ~500 m out), which
     clears the **normal** floor (0.40) but **not** the lost-mode floor (0.55). So normal-mode is
     required for the ~0.50 centroid to fire.
  3. Then scoring-time §XVII fires the airport pickup. No receipt coordinate needed.

**Airport mechanism is the GEOFENCE, not precise-terminal resolution (Andrew, 2026-06-08).**
`_match_poi_class` takes `geofence_score`/`geofence_witness` — a cluster-in-airport-polygon signal.
So an airport PUDO is identified by "you're inside the IAH geofence," NOT by resolving "Terminal D/E"
to a point. BUT the geofence lives *inside the POI matcher*, so it shares the same classifier gate:
on 10471 the offer was classed `intersection` → POI matcher (geofence + §XVII) never ran. So the
airport fix is the **light** version: get airport offers *to the POI matcher* (a small classifier
nudge so terminal addresses route to `poi`), then geofence + §XVII catch the at-airport PUDO.
**Decision: do NOT abandon every airport PUDO** (the geofence catches them once they reach the POI
matcher) and do NOT rabbit-hole precise-terminal classification. **95% accepts that some airport
pickups won't be perfect; it does not accept conceding the whole class.** Airports = a follow-up
track (geofence + classifier nudge), AFTER the lost-mode 0/5 fix ships.

**SHIP NOW (the actual 0/5 cure): F2 (abandon-the-unresolvable → cures the cascade) + F3 (A backstop).**
F1 dead, F1′/airports deferred to the follow-up track, F4 follow-up.

**(F2) Abandon-the-unresolvable. [cure for the cascade + honest miss]**
**Trigger — SEAM RESOLVED 2026-06-08 (do NOT abandon at receipt):** F1 is dead, so **§XVII does not
run at receipt** — at receipt only the plain geocode has executed. Abandoning a NULL-geocode offer at
receipt would kill airport offers *before* scoring-time §XVII + the geofence ever get their shot,
reopening conceding-airports. So F2 abandons a NULL-geocode offer **only after it has aged out
unmatched**: it stayed live (giving every scoring tick's §XVII + geofence a chance to match it at a
cluster), was never matched/resolved, and lingered past a staleness bound → only *then* mark
`expected_odometer_status='abandoned'` (§9.9 flag). An abandoned offer is excluded from
`_get_alive_unpicked_offer_ids` (`LIVE_OFFER_PREDICATE_SQL:265`,
`expected_odometer_status IS DISTINCT FROM 'abandoned'`), so once aged out it **can't pin lost-mode or
be a deferral peer** — the phantom 10550 stops the cascade. (No NULL-geocode-at-receipt criterion, no
airport conflation.) These abandons are **counted as misses** in the pickup-accuracy metric.
**Couplings to settle at draft time:** (i) the staleness bound must exceed the longest plausible
airport deadhead (don't abandon a real airport offer the driver is still driving to); (ii) NULL-geocode
*alone* cannot distinguish a phantom from a real airport offer — *that is exactly why* the trigger is
aged-out-unmatched, not receipt-NULL; (iii) F3 already lets deferred-but-*geocoded* offers fire during
any pin, so F2's bound only governs *how long* a phantom keeps us at the higher lost-mode floor (0.55)
rather than whether geocoded offers fire; (iv) actually *catching* airports (vs merely not abandoning
them prematurely) needs the follow-up classifier nudge so terminal offers reach the POI matcher +
geofence before they age out — until that ships, airports still age out → abandoned, which is the
accepted-5% case, not a regression.

**(F3) A — spatial firing for deferred-but-geocoded offers. [backstop]** A legitimately deferred
offer that *does* have a geocode should still fire on a strong cluster. Route it to the lost-mode
TAD verdict (`_build_lost_mode_verdict`, `tad.py:717` — confirmed NULL-anchor-safe, reads only
`state.actual_pickup_at`) → `passed=None` → fireable at `COMMIT_LOST_FLOOR` (0.55). Requires:
`_assemble_per_offer_state` builds a state for deferred offers; `OfferTadState` anchors `Optional`;
**hard None-anchor guard in `_evaluate_pickup_leg`/`_evaluate_dropoff_leg`** (lost-mode flickers
intra-drive — a None-anchor offer CAN reach the non-lost evaluator → None-math crash in the hot
path; this is a guard, not an assumption).

**(F4) Bound the lost-mode→deferral coupling. [follow-up]** Even a legitimate lost-mode shouldn't let
one peer blanket-defer every new offer. Lower priority once F2 removes phantom peers.

**Note on the lost-mode 0.55 floor vs §XVII:** the POI centroid-fallback scores ~0.50 (`_match_poi_class`
docstring) — below `COMMIT_LOST_FLOOR` (0.55). If F1 resolves a real sub-anchor (the 0.824 case),
that's moot. If terminals only ever hit centroid-fallback, the lost-mode floor for high-precision
semantic-anchor matches is a separate question — revisit only if F1's resolution lands ~0.50.

---

## §5 Mission framing (honest)

**95% = the residual that survives geocode AND §XVII still un-pointable.** Airports are **caught**
(F1), not conceded — the 5% budget is spent on genuinely un-resolvable addresses (garbage strings,
bad data), not on the high-value airport class we built tooling for. Abandons (F2) are **counted
misses**, visible in `./analysis/pickup_accuracy.sh`, not silent drops.

---

## §6 Conclusions overturned during this arc (the honest record)

- "Fix #3 (WAI single-road tuning)" — moot; never the issue.
- "Defer the dropoff-leg band reap" — retracted; Uber `trip_miles` is accurate at scale (66 rides,
  median 0.99); 10469 (+63%) was an outlier.
- "Candidate B (preserve the pickup anchor)" — declined on design grounds (spatial-first), not
  unviability (earlier "reverts 9acd504" framing overstated).
- "#1 = exclude NULL-geocode from alive_unpicked" — KILLED; conflated stale phantoms with real
  airport pickups (10471). Replaced by F2 (abandon-the-unresolvable).
- "classify_address candidate (a)" — KILLED empirically (breaks real suffix-less intersections).
- "Candidate A is the whole fix" — corrected; A is the backstop, not the cascade cure.
- "§XVII solved airport pickups" — corrected; §XVII is scoring-time, doesn't populate the receipt
  coordinate; airport *pickups* were never solved. **This (§2) is the load-bearing find.**

---

## §7 Open items before drafting
1. Live `searchText` test: does §XVII resolve "Terminal D/E, Departures: Zone 5E" from a far/metro
   bias (the receipt-time bias question)?
2. Receipt latency budget for a cache-miss Places call (sync vs async fallback).
3. Gemini review of this fix set (esp. F1 placement + the F3 None-anchor guard).
