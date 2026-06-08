# Gemini Review Brief — 2026-06-08: F3 + Venue Track (§P18b) + §XVI.C fix

**Context.** The 2026-06-08 morning drive scored **0/5** on pickup-LOCATION accuracy (the mission
is >95% within 100 m). Root cause + full arc: `docs/FINDINGS_LOSTMODE_AIRPORT_2026-06-08.md`.
Three changes came out of it. **F3 is deployed (rev 00655); the venue track and the §XVI.C fix are
committed but NOT deployed — they need this review before any deploy.** All tests green (807 passed,
1 skipped) at HEAD `98116c2` on branch `fix/restore-fire-error-metric-2026-06-02`.

Mission frame (§0): **pickups are the product** (the pricing cache); a wrong-location fire poisons
it. Canon touchpoints: §0.D.4 (arrest-defined truth), §XVI.C (TAD advisory, not a gate),
§XVII (semantic anchor), §XVIII (lost mode).

---

## Piece 1 — F3: §5.5-deferred offers fire on spatial  [DEPLOYED, rev 00655]
Commits `471457d` (fix) + `5aecc03` (test fixtures).

**Bug.** A lost-mode-deferred offer (NULL `expected_pickup_distance`, from `9acd504`) was excluded
by `_assemble_per_offer_state`'s legacy-row guard (`driver_heartbeat.py:1200`) → no `per_offer_state`
→ TAD `passed=False` → dispatch-skip. So real geocoded offers 2 m away (10629–10636) could not fire.

**Fix.**
- `OfferTadState.expected_pickup_{distance,arrival_time}` → `Optional` (deferred offers carry NULL).
- `_assemble_per_offer_state`: for `expected_odometer_status='deferred'`, **build** the state with
  None anchors instead of excluding; legacy pre-writer rows still skipped.
- `_evaluate_pickup_leg` (`tad.py`): None-anchor guard **before** the odometer math → returns
  `passed=None` (`deferred_no_anchor`) so the offer commits at `COMMIT_LOST_FLOOR` on spatial rather
  than crashing on `None - float`. A guard, not an assert, because lost-mode flickers intra-drive.

**Review asks:** (1) Is the None-anchor guard placement complete — any other `OfferTadState` deref
that can see a deferred offer? (Blast-radius check found only `tad.py:536/575`, both in
`_evaluate_pickup_leg`, both behind the guard.) (2) Is `passed=None` the right verdict for a
deferred offer in *normal* mode (lost-mode flicker off)?

---

## Piece 2 — Venue track §P18b: geofence as ground-truth  [COMMITTED `c2cbfdc`, NOT deployed; BEING RESHAPED]
**Motive.** `routing.geofence_polygons` holds **2,959 venue polygons** (2,104 malls + airports/
terminals/arenas/hospitals/universities/train_stations). `_signal_geofence_membership`
(`where_am_i.py:815`) computes ground-truth `ST_Contains` every tick — but it was (1) **POI-gated**
(only `_match_poi_class` consumed `geo_score`; a mis-classed "Terminal D/E"→intersection discarded
it) and (2) containment-only scored **0.30** (< 0.40 floor); the 1.0 name-match almost never fires
(empirically even a "Terminal E" polygon didn't match "Terminal D/E"; malls unnamed; the rare 1.0 is
a loose fuzzy FP — GBIA→"Alvin Airpark" at 0.70).

**What shipped in `c2cbfdc`:** containment-fire on a genuine stop (dwell≥45s AND spread≤75m → 0.60),
a global dispatch lift (apply `geo_score` to all classes, not just `_match_poi_class`), fuzzy floor
0.6→0.8.

**RESHAPE PENDING (Andrew + Claude review, post-commit) — do NOT review `c2cbfdc` as final:**
- **Andrew's frame:** the geofence is *just another WAI head* (a ground-truth *location* signal);
  the **proven PUDO logic — arrest physics (§0.D.4) + commit — detects the EVENT.** The geofence
  identifies "we're at venue X," not "a PUDO happened."
- So **the dwell/spread guard is being REMOVED** — it re-implemented arrest detection inside the
  geofence (redundant) and was mis-calibrated (a 60–120s Houston light clears 45s at ~0 spread → FP
  at the mall centroid; and it would reject fast curbside pickups that **horny mode** —
  `driver_heartbeat.py:2389`, 1 Hz when WAI≥0.40 & speed<5 mph — exists to catch).
- **Open design question for Gemini (the crux):** with the dwell guard gone, containment-only is
  0.30 (< floor) so it won't fire; raising it re-exposes the light FP, because **containment is
  coarser than geocode-proximity** — a light *inside* a venue polygon looks like "at the venue,"
  and in lost-mode there's no odometer band to filter it. What is the right **light-discriminator a
  light can't fake**? Candidates: off-wire / left-the-road-into-the-venue; the offer-attribution
  requirement (only fires if a venue offer is in the queue); polygon-size weighting. **This is the
  one unresolved design fork.**

---

## Piece 3 — §XVI.C fix: TAD is advisory, not a gate  [COMMITTED `d92a9e6`+`98116c2`, NOT deployed]
**This is the highest-stakes change — it alters commit behavior for every offer.**

**Canon.** §XVI.C (CANONICAL_RULES, amended 2026-05-22): *"WAI confidence ≥ 0.40 is the canonical
match signal. TAD is input to WAI, not a separate gate"* (`:1044`); *"when [TAD and ground truth]
disagree, ground truth wins… TAD is advisory"* (`:1055`); *"all heartbeats with live offers advance
to candidate evaluation regardless of TAD verdict"* (`:1089`). The named bug (`:1046`): *"an offer
driven out-of-order from what TAD predicted… had its WAI confidence cleared but its TAD gate blocked,
causing the observation to be missed."* The code still ran the **pre-amendment gate-era doctrine.**

**Two gate sites (the only TAD-as-gate enforcement) changed:**
1. `where_am_i.evaluate()` Step 5: **removed the "TAD bouncer"** (`if verdict.passed is False:
   continue`) that skipped scoring entirely.
2. `_commits()`: `passed=False` is **no longer a hard veto** — it commits at `COMMIT_LOST_FLOOR`
   (0.55), same as lost-mode (narrative untrustworthy, but ground truth ≥ 0.55 wins).
   `classify_commit_rule` labels it `narrative_disagree_floor[_with_poi_lift]`.

**Two flagged canon-vs-canon / open calls for Gemini (do not assume my defaults):**
- **WALLET (Bible Rule 1).** The bouncer also cited "no Google API spend on TAD-failed offers."
  Removing it scores `passed=False` offers too. I argued the marginal spend is bounded (the §XVII
  semantic lookup is cache-first, 365-day TTL), but the wallet-gate-vs-§XVI.C tension is a real call
  to ratify. **Is bounded-cache spend acceptable, or should `passed=False` score on cheap signals
  only (geofence containment is a DB query; gate the Google call)?**
- **UNIFORM FLOOR.** §XVI.C (`:1057`, `:1106`) says lost-mode commits at **WAI ≥ 0.40 (uniform)**,
  not 0.55. I retained the 0.55 `COMMIT_LOST_FLOOR` for `passed=None`/`passed=False` as conservative
  pre-existing strictness. **Should the floor collapse to a uniform 0.40 per canon, or is the 0.55
  lost-mode strictness a deliberate post-amendment refinement to keep?** (Over-fire risk to weigh.)

**Review asks:** (3) Is removing the bouncer + the veto the correct/complete §XVI.C restoration, or
is there a third gate site? (4) Over-fire risk: `passed=False` band-overshoot offers with strong
spatial WAI (≥0.55) now commit — is the 0.55 floor a sufficient guard, or does this re-open a
false-positive class? (5) The two open calls above.

---

## Claude's adversarial flags (prior round) and disposition
1. **`_commits` TAD gate kills venue fire in lost-mode** → CONFIRMED as a §XVI.C regression; *fixed*
   in Piece 3. (For the drive itself it still fired because lost-mode = `passed=None`, not False.)
2. **dwell≥45s doesn't exclude a long light + misses fast pickups** → CONFIRMED; the dwell guard is
   being removed (Piece 2 reshape); horny mode covers fast pickups; the light-discriminator is the
   open fork.
3. **fuzzy 0.6→0.8 kills the Alvin FP but the TP band underneath is unverified** → OPEN: forensic
   not yet run (what real venue matches scored 0.6–0.8 on recent drives). Listed below.

---

## Outstanding tasks
- [ ] **Gemini review** of Piece 3 (§XVI.C) + the two open calls (wallet, uniform floor) → then deploy.
- [ ] **Geofence-head reshape** (Piece 2): remove the dwell guard; decide the light-discriminator
      (off-wire / offer-attribution / polygon-size). Blocked on the design fork above.
- [ ] **Fuzzy-band forensic** (Flag 3): what fired in the 0.6–0.8 geofence-fuzzy band recently.
- [ ] **F2** (abandon-at-source): **DROPPED** — conflicts with keeping venue offers live, and F3 +
      geofence + §XVI.C neutralize the phantom; the 4h ceiling reaps it. (Recorded for the trail.)
- [ ] **Validate on a drive** (`./analysis/pickup_accuracy.sh`) after the next run — F3's effect
      should already show on rev 00655.

## Commits (branch `fix/restore-fire-error-metric-2026-06-02`)
`471457d`/`5aecc03` F3 · `c2cbfdc` venue §P18b · `d92a9e6`/`98116c2` §XVI.C · `e1e1378` findings §8.
