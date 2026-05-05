# Phase 2c.2 fresh-chat handoff (Patches 2 + 3)

**Date:** 2026-05-05
**Predecessor commit:** Patch 1 (Phase 2c.2.0 vocabulary expansion) on `demolition-2026-05-04`
**Predecessor test floor:** 356/356 pytest, 17/17 Bruno
**Status:** Patches 2 and 3 not yet authored. All design ratifications complete.

---

## Mission

Land Phase 2c.2 — Design D 3-headed `_signal_poi_match` integration into
`where_am_i.py` per `OPERATION_STRIP_MALL_PROPOSAL.md` §2 Change 2 +
Gemini Design D ratification (2026-05-05) + Gemini R4-redesign
ratification (2026-05-05, this same date).

Deferred from previous chat to manage context window during signature-
change apply cycles. Patch 1 (vocabulary expansion) shipped clean in
the previous chat; this chat picks up at Patch 2.

---

## Patch 2 scope: `where_am_i.py` core integration

### 2.1 — Add POI lookup at cluster level (NEW step in `_evaluate`)

Insert between Step 2 (topology) and Step 3 (stop context). POI lookup
is cluster-keyed, not target-keyed, so it runs once per `_evaluate`
invocation:

```python
# Step 2.5: POI lookup (Phase 2c.2 — cluster-keyed, runs once per heartbeat)
from poi_service import get_pois_near_cluster
poi_result = get_pois_near_cluster(cluster, self.cur)
pois = poi_result.pois
poi_lookup_source = poi_result.source
```

`POILookupResult.source` is one of: `'cache_hit'`, `'api_call'`,
`'api_error'`, `'skipped'`. Phase 1B already restored
`pudo_decision_context.poi_lookup_source` column binding; this just
populates it.

### 2.2 — `DiagnosticContext` gains `poi_lookup_source` field

Surface for forensic logging. Phase 2d's heartbeat handler will read
this for the `pudo_decision_context` insert.

### 2.3 — Add `POI_RADIUS_M` constant

Module-level. Caller-internal threshold (separate from
`poi_service.DEFAULT_RADIUS_M = 80` which governs cache-lookup radius).
Recommended value: 100m. Filter: `_signal_poi_match` ignores any POI in
`pois` with `dist_m > POI_RADIUS_M`.

### 2.4 — `MatchOutcome` gains 2 fields

```python
poi_match: Optional[float]      # head score 0.0-1.0, None if no pois passed
poi_witness: Optional[str]       # "fuzzy:Excel Dental" / "branded:marriott"
                                  # / "airport_type:hobby_airport" / None
```

Place in the "Metadata" group alongside `signals`.

### 2.5 — `_signal_poi_match` per Gemini Option B (NEW)

```python
def _signal_poi_match(
    pois: list[POI],
    target_address: str,
) -> tuple[float, Optional[str]]:
    """3-headed POI co-reference signal with Option B noise-gate logic.

    Heads:
      1. Direct fuzzy: rapidfuzz.fuzz.partial_ratio(poi.name, target) / 100
      2. Branded co-reference: detect_branded_token on both target and
         each poi.name; match if both produce same token
      3. Airport type co-reference: any poi with 'airport' in poi.types
         AND target contains airline/airport branded token

    Option B noise-gate (replaces R4 multiplier):
      - If matched token is CLEAN: return signal_score, witness
      - If matched token is HIGH_NOISE:
          - if street_number_present in target: return signal_score (number disambiguates)
          - else: return min(signal_score, 0.5), witness  (Galleria cap)
      - If no match: return 0.0, None

    Provenance: production audit 2026-05-05 demolished R4 multiplier
    (96.5% of branded matches lack street number; multiplier locked
    out R7 floor relaxation). Option B uses street_number as
    noise-gate key only, where data shows it actually disambiguates.
    """
```

`detect_branded_token` returns `(token, is_high_noise)`. Use that
directly for the gate logic — no separate HIGH_NOISE lookup needed.

`street_number_present` regex: `re.compile(r"^\s*(\d+)\b")`.

### 2.6 — `_compute_signals` accepts `pois` parameter

```python
def _compute_signals(
    cluster: Cluster,
    topo: RoadTopology,
    target,
    threshold_m: float,
    pois: Optional[list[POI]] = None,  # NEW, default None
) -> dict[str, float]:
    ...
    poi_score, poi_witness = (
        _signal_poi_match(pois, target.address)
        if pois is not None
        else (0.0, None)
    )
    return {
        ...existing 7 signals...,
        "poi_match": poi_score,
        "_poi_witness": poi_witness,  # leading underscore = exclude from weighted sum
    }
```

Witness is stashed in dict with leading underscore so `_weighted_confidence`'s
sum naturally skips it. Alternative: return tuple, refactor callers.
Underscore convention is lighter touch.

### 2.7 — `_build_outcome` populates `poi_witness`

Pass `signals` through; pop `_poi_witness` for the dedicated field.

### 2.8 — 5 matchers thread `pois` through

Signature change: `(cluster, topo, target)` → `(cluster, topo, target, pois)`.
Each matcher passes `pois` to `_compute_signals`.

`_match_poi_stub` becomes real `_match_poi` — uses apartment_complex
template (POI_RADIUS_M proximity threshold, off_wire_pivot relevant,
poi_match dominates).

### 2.9 — `_CLASS_DISPATCH` updated

```python
"poi": _match_poi,  # was _match_poi_stub
```

### 2.10 — `_CONFIDENCE_WEIGHTS` rewrite per Gemini ratification

Each row sums to exactly 1.00.

```python
_CONFIDENCE_WEIGHTS = {
    "intersection": {  # unchanged - no poi_match for road-anchored class
        "proximity":            0.10,
        "breadcrumb_match":     0.30,
        "cluster_tightness":    0.15,
        "cluster_duration":     0.10,
        "on_target_road":       0.20,
        "off_wire_pivot":       0.05,
        "adjacent_road_match":  0.10,
        "poi_match":            0.00,  # explicit 0 for uniform schema
    },
    "single_road": {
        "proximity":            0.05,
        "breadcrumb_match":     0.20,  # was 0.35 (-0.15)
        "cluster_tightness":    0.15,
        "cluster_duration":     0.10,  # was 0.15 (-0.05)
        "on_target_road":       0.10,  # was 0.15 (-0.05)
        "off_wire_pivot":       0.05,
        "adjacent_road_match":  0.05,  # was 0.10 (-0.05)
        "poi_match":            0.30,  # NEW
    },
    "number_on_street": {
        "proximity":            0.20,  # was 0.30 (-0.10)
        "breadcrumb_match":     0.10,  # was 0.20 (-0.10)
        "cluster_tightness":    0.15,
        "cluster_duration":     0.10,
        "on_target_road":       0.10,  # was 0.15 (-0.05)
        "off_wire_pivot":       0.00,
        "adjacent_road_match":  0.05,  # was 0.10 (-0.05)
        "poi_match":            0.30,  # NEW
    },
    "apartment_complex": {
        "proximity":            0.10,
        "breadcrumb_match":     0.05,  # was 0.10 (-0.05)
        "cluster_tightness":    0.15,  # was 0.20 (-0.05)
        "cluster_duration":     0.15,
        "on_target_road":       0.05,
        "off_wire_pivot":       0.30,  # was 0.40 (-0.10)
        "adjacent_road_match":  0.00,
        "poi_match":            0.25,  # NEW
    },
    "poi": {  # NEW class
        "proximity":            0.15,
        "breadcrumb_match":     0.05,
        "cluster_tightness":    0.10,
        "cluster_duration":     0.10,
        "on_target_road":       0.05,
        "off_wire_pivot":       0.10,
        "adjacent_road_match":  0.00,
        "poi_match":            0.45,
    },
}
```

`_weighted_confidence` may need to skip the `_poi_witness` key (or
the leading-underscore convention handles it via dict comprehension
exclusion in the sum step).

### 2.11 — `evaluate` / `evaluate_with_diagnostics` unchanged signatures

POI lookup happens internally inside `_evaluate`. Public API stays
`(driver_id, queue)` — backward compatible with all `tmp/`, `apply_*`,
and test callers per L-6 inventory below.

### 2.12 — `_evaluate` matcher invocation

```python
outcome = matcher(cluster, topo, target, pois)  # was matcher(cluster, topo, target)
```

Single dispatch site at line 1170 in current source.

---

## Patch 3 scope: tests

### 3.1 — Update existing matcher call sites for new signature

12 call sites in `tests/test_where_am_i.py` (per L-6 inventory):
lines 808, 835, 852, 859, 884, 900, 923, 937, 968, 991, 1008, 1020.

Pattern: add `pois=None` (or `pois=[]`) as 4th positional arg.

### 3.2 — Update `_compute_signals` call sites

2 sites: lines 757, 771. Same pattern.

### 3.3 — New behavior tests (estimated 6-8 tests)

Per closeout Phase 2c.3 scope, but folded into Patch 3 here:

- `test_signal_poi_match_head1_direct_fuzzy` — Excel Dental fuzzy match
- `test_signal_poi_match_head2_branded_both_sides` — Marriott both-sides
- `test_signal_poi_match_head3_airport_type` — Spirit → Hobby Airport save
- `test_signal_poi_match_high_noise_capped_without_anchor` — Galleria cap
- `test_signal_poi_match_high_noise_uncapped_with_street_number` — number disambiguates
- `test_signal_poi_match_no_match_returns_zero_none` — empty case
- `test_match_poi_apartment_style` — replace _match_poi_stub coverage
- `test_confidence_weights_all_rows_sum_to_one` — sanity check on the 5 rows

### 3.4 — DiagnosticContext field test

- `test_diagnostic_context_carries_poi_lookup_source`

---

## L-6 SECOND STRIKE inventory (do not skip)

All matcher / `_compute_signals` / `evaluate*` callers identified in
previous chat's recon. Confirmed surface:

**Matcher direct callers:** `where_am_i.py` definitions + `_CLASS_DISPATCH`
(both auto-handled by Patch 2). `tests/test_where_am_i.py` 12 explicit call
sites + 1 import block. **No production callers outside these files.**

**`_compute_signals` callers:** 4 inside `where_am_i.py` (the 4 non-stub
matchers, all auto-handled by Patch 2 since `pois` is optional and
defaulted). 2 in `tests/test_where_am_i.py` (lines 757, 771).

**`evaluate` / `evaluate_with_diagnostics` callers:** `tmp/` files
(historical/staged), `apply_substep_1b3.py` (historical apply script).
**No production callers yet** — Phase 2d wires heartbeat. Public API
unchanged means zero risk to these.

L-6 conclusion: Patch 2 surface is `where_am_i.py` only. Patch 3 surface
is `tests/test_where_am_i.py` only. No other files touched.

---

## Open architectural decisions (none — all ratified)

Gemini ratified all 3 design questions on 2026-05-05:

1. **R4 redesign:** Option B (street_number as noise-gate key only).
   Locked. See §2.5 above.

2. **Weight redistribution:** Proposed reductions ratified as-is. Locked.
   See §2.10 above.

3. **New `poi` class weight row:** Ratified including `adjacent_road_match
   = 0.00`. Gemini explicitly defended that one. Locked. See §2.10.

**Gemini's outstanding question:** "do you want to add 'Amazon' to the
Clean Token list now, or wait?" — Decided against during Patch 1: Amazon
fired only 1 time in 2,101 offers (below noise floor). Revisit after
next 500 rides per Gemini's wait-and-see option.

---

## Recon assets (re-paste at start of fresh chat)

These were verified in previous chat and form the ground truth:

### `_EXTENDED_POI_TOKENS` post-Patch 1 (33 tokens)

Located `bead_on_wire.py` lines 134-152. After Patch 1, includes `nrg`
and `houston methodist` in the "Other venues" stanza.

### `MatchOutcome` field list

Lines 424-458 in `where_am_i.py`. Existing fields:
`matched, confidence, corrected_lat, corrected_lng, reason, pudo_type,
target_address, signals`. Patch 2 adds `poi_match` and `poi_witness`.

### `_compute_signals` current signature

Line 581: `(cluster, topo, target, threshold_m) -> dict[str, float]`.
Returns 7 signal keys. Patch 2 adds `pois=None` parameter and
`poi_match` + `_poi_witness` return keys.

### `_evaluate` matcher invocation

Line 1170: `outcome = matcher(cluster, topo, target)`. Single dispatch
point. Patch 2 changes to 4-arg call.

### `POI` dataclass

`poi_service.py:46-67`. Frozen, fields: `place_id, name, types: list[str],
lat, lng, dist_m`. Head 3 predicate is `"airport" in poi.types`.

### `POILookupResult`

`poi_service.py:70-95`. Fields: `pois: list[POI], source: str`.
`source` ∈ `{'cache_hit', 'api_call', 'api_error', 'skipped'}`.

---

## Apply script structure recommendation

Patch 2 is too large for a single L-3-envelope script. Split into:

- **2a:** `MatchOutcome` field additions + `POI_RADIUS_M` constant +
  `_CONFIDENCE_WEIGHTS` rewrite. Pure data-shape changes, low risk.
- **2b:** `_signal_poi_match` function definition. New code, no
  signature-change blast radius.
- **2c:** `_compute_signals` + `_build_outcome` updates to thread
  `pois` and witness through. Signature change with internal scope.
- **2d:** 5 matcher signature changes + `_CLASS_DISPATCH` update +
  `_match_poi_stub` → `_match_poi` rewrite.
- **2e:** `_evaluate` Step 2.5 POI lookup insertion +
  `DiagnosticContext.poi_lookup_source` field.

Each sub-patch ships with its own L-3 envelope. After 2a-2e all land,
run pytest. **Existing tests will fail** because they call matchers
with 3-arg signatures — that's expected and Patch 3 fixes them.

Patch 3 lands after Patch 2e. Single L-3 envelope script:

- **3a:** Update 14 existing call sites for new signatures.
- **3b:** Append 8-ish new behavior tests in
  `TestPhase2c2SignalPoiMatch` and related classes.

Final pytest after Patch 3: 356 + 8 ≈ 364 passing.

---

## Standing rules (apply throughout)

- **L-3 envelope** mandatory on all apply scripts (Phase 1 verify +
  idempotency / Phase 2 transform + delta gates / Phase 3 atomic write
  + read-back).
- **Set-difference gates** for L-2 (compare actual-before to
  actual-after, not against snapshot constants — see Patch 1 v2
  rationale).
- **Apply scripts to `~/puddlejumper-prod/tmp/`**, recon ephemera
  to `/tmp/`.
- **Paste safety:** all multi-line content via `create_file` →
  `present_files` → `scp`. No heredocs to bash for code.
- **Document conventions:** when chat displays filename-like strings,
  bash receives clean bytes via code-fence copy. Read past `[name.py
  ](http://name.py)` rendering artifacts in pasted output (L-22).
- **Gemini-loop discipline:** any new design question gets the
  audit-driven treatment, not parking. (See userMemories directive
  2026-05-05.)
- **No verbose preambles, no MVP framing, full production code,
  Andrew sets the pace.**

---

## Predecessor commit handoff

Verify at fresh chat start:

```bash
cd ~/puddlejumper-prod
git log --oneline -3
# expect: Patch 1 commit at HEAD, with message starting
# "Phase 2c.2.0: add nrg + houston methodist to _EXTENDED_POI_TOKENS"
git status
# expect: clean working tree, on demolition-2026-05-04
python3 -m pytest tests/ -q
# expect: 356 passed
```

If any of those don't match, stop and reconcile before authoring
Patches 2 or 3.