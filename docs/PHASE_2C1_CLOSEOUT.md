# Phase 2c.1 Closeout — bead_on_wire.detect_branded_token

**Date:** 2026-05-05
**Commit:** `99b105d`
**Branch:** `demolition-2026-05-04` (pushed to origin)
**Predecessor:** `d32fec5` (Phase 2b — Operation Strip Mall Google Places v1 + JSONB cache)
**Deployed:** No. poi_service.py + detect_branded_token both have zero production callers until Phase 2d.
**Test floor:** 302 → 352 (+50 net, all in tests/test_bead_on_wire.py NEW FILE).
**Bruno:** unchanged baseline (no production code path touched).
**Reviewer:** Gemini ratified through 4 rounds; final ratification withdrew earlier "Iron Fist" objection in favor of audit-driven decisions.
**Status:** ✅ Shipped, dormant.

## What shipped

### `bead_on_wire.py` (+145, -2)

1. **`_BRANDED_TOKEN_PATTERN`** — module-level compiled regex. Word-boundary alternation across `_EXTENDED_POI_TOKENS`, sorted longest-first so multi-word tokens ("southwest airlines", "hobby airport") match before any prefix. `re.IGNORECASE`. Single source of truth for both `_contains_poi_token` and `detect_branded_token`.

2. **`_contains_poi_token` rewritten** to use `_BRANDED_TOKEN_PATTERN.search()`. Eliminates the latent substring bug where "hou" in "Houston" or "park" in "Briarpark" matched falsely.

3. **`_HIGH_NOISE_TOKENS` rewritten** with audit-driven membership (2101 production offers, 2026-05-05):
   - **Validated high-noise (production-grounded):** `park` (104 hits, all road names), `airport` (18 hits, all road names), `alaska` (1 hit, road name)
   - **Theoretical high-noise (zero production hits, kept on common-word-collision reasoning):** `british`, `hilton`, `marquis`, `ritz`
   - **REMOVED from previous theoretical HIGH_NOISE based on production data:** `united` (20 real airline dropoffs), `delta` (4 of 5 real), `frontier` (3 real), `spirit` (1 real), `terminal` (25 real IAH terminals), `galleria` (3 real Galleria mall hits)

4. **`detect_branded_token(text)`** — new public function. Returns `(token, is_high_noise)` tuple or `None`. Uses `_BRANDED_TOKEN_PATTERN.search()`, returns canonical lowercase token from match, looks up high-noise flag.

### `tests/test_bead_on_wire.py` (+539, NEW FILE)

50 tests across 9 classes. Coverage: empty inputs, no-match, clean tokens, high-noise tokens, case insensitivity, Southwest-Fwy regression guard, apartment-name behavior, set integrity, substring regression guard with positive controls.

## Audit data (preserved for future reference)

Full table at production audit time (2026-05-05, 2101 offers, 4202 address-instances):

| Token | Pickup | Dropoff | Total | Verdict |
|---|---:|---:|---:|---|
| park | 47 | 57 | 104 | All road names → HIGH_NOISE |
| terminal | 15 | 10 | 25 | All real IAH terminals → clean |
| stadium | 18 | 2 | 20 | NRG Stadium → clean |
| united | 0 | 20 | 20 | Bare-airline dropoffs → clean |
| airport | 11 | 7 | 18 | All road names → HIGH_NOISE |
| southwest airlines | 0 | 14 | 14 | Real → clean |
| mall | 2 | 12 | 14 | Real → clean |
| george bush | 0 | 8 | 8 | IAH dropoffs → clean |
| marriott | 5 | 0 | 5 | Real → clean |
| delta | 0 | 5 | 5 | 4 real airline + 1 caught upstream → clean |
| hospital | 4 | 0 | 4 | Real → clean |
| frontier | 0 | 3 | 3 | Real → clean |
| galleria | 1 | 2 | 3 | Real Galleria mall → clean |

**Driver bias caveat:** Andrew (data-collecting driver) declines airport pickup queue offers, so `george bush` is dropoff-only and `terminal` skews pickup-light in this dataset. Commercial product users will see different directionality. Phase 2c matcher tests must cover both halves.

## Phase 2c.2 next-up scope

Per OPERATION_STRIP_MALL_PROPOSAL.md §2 Change 2 + Gemini Design D ratification (2026-05-05).

**Scope:** `where_am_i.py` integration of Design D 3-headed `_signal_poi_match`.

1. Add `_signal_poi_match(pois, target_address)` function:
   - Head 1: `rapidfuzz.fuzz.partial_ratio(poi.name, target_address) / 100.0` direct fuzzy
   - Head 2: branded co-reference using `bead_on_wire.detect_branded_token` on both target AND each poi.name; cap score at 0.5 if `is_high_noise=True` and no secondary anchor
   - Head 3: airport-type co-reference — if any poi has type "airport" AND target_address contains airline/airport token, score 0.9
   - Combined: `max(head1, head2_capped, head3) × 0.8 + street_number_present × 0.2`
   - Returns `(score: float, witness: Optional[str])` where witness is `"fuzzy:Excel Dental"` / `"branded:marriott"` / `"airport_type:hobby_airport"` / None

2. Update `_CONFIDENCE_WEIGHTS` per Gemini's ratified table:
   - intersection: poi_match 0.00 (unchanged row)
   - single_road: poi_match 0.30 (re-tune)
   - number_on_street: poi_match 0.30 (re-tune)
   - apartment_complex: poi_match 0.25 (re-tune; off_wire stays 0.40)
   - poi (NEW row): poi_match 0.45

3. Update `_compute_signals` — accept `pois` param, compute `poi_match` key

4. Thread `pois` through 5 matchers' signatures

5. Replace `_match_poi_stub` with real `_match_poi`; add `POI_RADIUS_M` constant

6. Add `MatchOutcome.poi_match: Optional[float]` and `MatchOutcome.poi_witness: Optional[str]` fields

7. Update `WhereAmI.evaluate` and `evaluate_with_diagnostics` — accept `pois` keyword-only param defaulting to None

8. Update `_evaluate` — thread pois through dispatch

**Test scope (Phase 2c.3 — separate sub-commit):**
- Spirit Airlines → Hobby Airport (Head 3 saves bare-airline dropoff)
- Excel Dental fuzzy match (Head 1, validated against Phase 2b smoke)
- Marriott both-sides branded co-reference (Head 2)
- Galleria-cap rule (high-noise capped without secondary anchor)
- Forum Park Dr (high-noise correctly handled)
- The Enclave at Sienna apartment-name fuzzy match
- All 5 matchers updated for new signature
- Apartment-complex matcher gets explicit POI test

**Estimated surface:**
- where_am_i.py: ~10 edit sites, ~80 lines added
- tests/test_where_am_i.py: ~12 sig fixes + 6 new behavior tests in 2c.3

**No deploy.** Phase 2d wires this into heartbeat. Phase 2e adds dispatch confidence floor. Phase 2f drives live.

## Files to load in 2c.2 fresh session

Mandatory paste at session-open:
- This closeout (`docs/PHASE_2C1_CLOSEOUT.md`)
- `docs/CANONICAL_RULES.md`
- `docs/SESSION_PROTOCOL.md`
- `docs/INDEX.md`
- `docs/OPERATION_STRIP_MALL_PROPOSAL.md`

Source files to recon (don't paste full, request ranges):
- `where_am_i.py` (1252 lines)
- `tests/test_where_am_i.py` (1903 lines)
- `bead_on_wire.py` (post-2c.1 state)
- `poi_service.py` (Phase 2b output, dataclass shape)

## Lessons logged this sprint

**L-26: Audit data trumps vocabulary intuition.** Theoretical "high-noise" classifications based on word ambiguity in the abstract were 5-of-11 wrong when measured against 2101 production offers. Running the audit before committing the membership decision saved 5 incorrect HIGH_NOISE classifications that would have broken 28+ real Houston offers (galleria=3, terminal=25 to be specific). Mitigation rule: any production-tunable parameter (HIGH_NOISE membership, weight table values, confidence thresholds) must be validated against actual production data before being treated as locked. Vocabulary intuition is hypothesis, not evidence.

**L-26 corollary: Driver bias in audit data.** Single-driver offer_history is biased toward that driver's preferences. The data-collecting driver's habit of declining airport pickup queue offers caused george_bush to appear dropoff-only and terminal pickup-light in the audit. The membership decisions still hold (road-name false positives don't change with directionality), but architectural assertions about pickup/dropoff patterns must be flagged as biased. Mitigation rule: when production data comes from a single user, prefer schema-bias-resistant metrics (token-as-substring vs token-as-word, real-vs-road) over directionality metrics that reflect user preference.

**L-27: TestSubstringRegressionGuard premise was wrong.** Tests written under "word-boundary regex eliminates ALL false positives in road names" assumption. Reality: word-boundary handles SUBSTRING collisions ("hou" in "Houston"); HIGH_NOISE flag handles STANDALONE-word collisions ("Park" in "Forum Park Dr"). The two mechanisms operate at different layers and are both necessary. Mitigation rule: when designing layered defense, write tests that exercise EACH layer independently and document what each layer is responsible for. Don't conflate "word X doesn't match" with "word X matches but is flagged for caution."
