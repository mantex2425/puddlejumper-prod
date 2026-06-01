# Recon: Does Address Class Already Scale Geocode Trust?

**Date:** 2026-05-30
**Deployed revision:** `puddlejumper-api-00634-gq8` (branch `phase-2c-2-geocode-signal`)
**Investigator:** Claude (server-side recon, read-only)
**Status:** Read-only investigation complete. No code edited. Output for Gemini review.

---

## VERDICT

**(A) Address class ALREADY scales geocode trust — via TWO independent mechanisms.**

1. **Per-class proximity radius (`threshold_m`).** Each address class has its own `_RADIUS_M` constant fed into `_signal_proximity`. For low-precision classes the radius is 10× wider than for high-precision: `single_road` = 500m, `number_on_street` = 50m. The matcher will consider the geocoded coord "near enough" within whichever radius the class permits.

2. **Per-class proximity weight in `_CONFIDENCE_WEIGHTS`.** Each address class has its own weights row. The `proximity` signal contributes 6× more to overall confidence for high-precision than low-precision: `number_on_street` weights proximity at 0.30; `single_road` weights it at 0.05.

For `single_road` specifically, the matcher uses both knobs: it accepts the geocode-to-cluster match within a wide 500m envelope **and** then almost-ignores that signal in the final confidence (5% weight). The trust budget is reallocated to non-geocode signals that ARE reliable for single-road addresses: `breadcrumb_match` (0.35 — "did the driver actually drive on this road?") and `on_target_road` (0.15 — "is the cluster on this road right now?"). The architecture is exactly the "scale trust by precision" shape the recon was checking for, and it's been there since at least the §XVII work.

The proximity-radius and proximity-weight numbers below are the only knobs the code currently uses. There's no per-class confidence penalty, no per-class hard rejection, no per-class downstream filter — just these two.

---

## 1. THE CLASSIFIER (`bead_on_wire.py:327`)

```python
def classify_address(text: str) -> dict:
    """
    Classify a YOLO'd address string into a bucket.

    Returns: {"bucket": str, "parts": dict}
    Buckets: "intersection", "street_number", "single_road", "poi", "garbage"
    """
```

### 1.1 Full label set (verbatim from docstring + return statements)

**Five buckets** returned by `classify_address`:

| Bucket | Decision rule |
|---|---|
| `garbage` | Empty / whitespace-only input (line 334-335) |
| `street_number` | First field matches `^(\d+)\s+(.+)$` AND first field is NOT a highway pattern (line 343-355). E.g. `"2727 Allen Pkwy"`, `"401 Franklin St"`. Guard: `"I-10"` and `"Hwy 6"` are NOT street numbers. |
| `intersection` | First field contains `&`, ` and `, `@`, or `+` per `INTERSECTION_REGEX`, AND splits into two non-empty road names (line 357-370) |
| `single_road` | `_first_field_looks_like_road(first_field)` returns True (line 372-382). E.g. `"S Post Oak"`, `"Fondren Rd"` |
| `poi` | `_contains_poi_token(first_field)` returns True — matches a known venue/brand/airline token (line 384-389) |
| `garbage` (fallback) | None of the above match (line 391-395) |

### 1.2 Bucket → address_class mapping (`driver_heartbeat.py:131-167`)

The bucket is then mapped to a stable `address_class` string on the `TargetSpec`:

```
bucket           → address_class
─────────────────────────────────
intersection     → "intersection"
street_number    → "number_on_street"
single_road      → "single_road"
poi              → "poi"
garbage          → "garbage"      (§PR-A: admitted with explicit class, not rejected)
```

There's also a **5th** address_class that classify_address never emits but the matchers handle separately: `"apartment_complex"`. This class is assigned elsewhere (the recon did not chase its assignment site — it's not produced by `classify_address`).

---

## 2. WHERE THE CLASS IS CONSUMED

### 2.1 Per-class matcher dispatch (`where_am_i.py:1526-1538`)

```python
_CLASS_DISPATCH = {
    "intersection":      _match_intersection,
    "single_road":       _match_single_road,
    "number_on_street":  _match_number_on_street,
    "apartment_complex": _match_apartment_complex,
    "poi":               _match_poi_class,
    # §PR-A: "garbage" routes to _match_poi_class so OCR-shredded text
    "garbage":           _match_poi_class,
}
```

Resolved at `where_am_i.py:2184`:

```python
matcher = _CLASS_DISPATCH.get(target.address_class)
```

Each `address_class` routes to a class-specific matcher function. This is the primary consumer.

### 2.2 Per-class confidence weights (`where_am_i.py:196-233`)

```python
_CONFIDENCE_WEIGHTS = {
    "intersection": {
        "proximity":            0.10,
        "breadcrumb_match":     0.30,
        "cluster_tightness":    0.15,
        "cluster_duration":     0.10,
        "on_target_road":       0.20,
        "off_wire_pivot":       0.05,
        "adjacent_road_match":  0.10,
    },
    "single_road": {
        "proximity":            0.05,
        "breadcrumb_match":     0.35,
        "cluster_tightness":    0.15,
        "cluster_duration":     0.15,
        "on_target_road":       0.15,
        "off_wire_pivot":       0.05,
        "adjacent_road_match":  0.10,
    },
    "number_on_street": {
        "proximity":            0.30,
        "breadcrumb_match":     0.20,
        "cluster_tightness":    0.15,
        "cluster_duration":     0.10,
        "on_target_road":       0.15,
        "off_wire_pivot":       0.00,
        "adjacent_road_match":  0.10,
    },
    "apartment_complex": {
        "proximity":            0.10,
        "breadcrumb_match":     0.10,
        "cluster_tightness":    0.20,
        "cluster_duration":     0.15,
        "on_target_road":       0.05,
        "off_wire_pivot":       0.40,
        "adjacent_road_match":  0.00,
    },
}
```

Notice: no entry for `"poi"` or `"garbage"` — those go to `_match_poi_class`, which uses a different scoring path (POI/anchor signals) rather than `_compute_signals` + `_weighted_confidence`. POI matching is its own architecture and was not the focus of this recon.

Applied at the end of each class-specific matcher, e.g.:

- `where_am_i.py:1225`: `confidence = _weighted_confidence(signals, _CONFIDENCE_WEIGHTS["intersection"])`
- `where_am_i.py:1269`: `confidence = _weighted_confidence(signals, _CONFIDENCE_WEIGHTS["single_road"])`
- `where_am_i.py:1317`: `confidence = _weighted_confidence(signals, _CONFIDENCE_WEIGHTS["number_on_street"])`
- `where_am_i.py:1367`: `confidence = _weighted_confidence(signals, _CONFIDENCE_WEIGHTS["apartment_complex"])`

### 2.3 POI type-match witness (`where_am_i.py:690-724`)

```python
def _signal_poi_type_match(
    ...
    address_class: str,
    ...
):
    """... this matches POI TYPE against address_class. Both are witness signals
    outside _CONFIDENCE_WEIGHTS per Bible Rule 2 -- they corroborate the
    other signals but don't enter the weighted sum directly.
    ...
    accepted_types = CLASS_TO_TYPE_MAP.get(address_class)
```

Class is used to look up an accepted-POI-types set for cross-referencing. This is a **witness** signal — does NOT enter `_CONFIDENCE_WEIGHTS`. (`Bible Rule 2` per the comment.)

### 2.4 Class-gated witness (`where_am_i.py:1199`)

```python
if target.address_class not in ("single_road", "intersection"):
    ...
```

One predicate gates a specific signal (`_apply_road_membership_override` per context) to road-flavored classes only. The override seeds road-adjacency signals when the cluster is on a target named road. Not a trust-scaling decision per se — just "this signal only makes sense for road-class addresses."

---

## 3. THE CRUX: Yes, geocode trust IS scaled by class — via radius + weight

### 3.1 Per-class proximity radius constants (`where_am_i.py:57-69`)

```python
INTERSECTION_RADIUS_M     = 250.0
SINGLE_ROAD_RADIUS_M      = 500.0
NUMBER_ON_STREET_RADIUS_M = 50.0
APARTMENT_RADIUS_M        = 300.0
GHOST_MATCH_RADIUS_M      = 50.0
POI_RADIUS_M              = 100.0
```

The radius is passed to `_signal_proximity` as `threshold_m`, which produces a normalized score:

```python
# where_am_i.py:344-366
"""Distance from cluster median to target, normalized by threshold.
   ...
   - At target (0m):       1.0
   - At threshold:         0.0
   - Beyond threshold:     0.0 (does not go negative)
   - Inside threshold:     1.0 - (distance / threshold)
"""
if distance_m >= threshold_m:
    return 0.0
return 1.0 - (distance_m / threshold_m)
```

So `single_road` accepts the geocoded coord as a "near enough" match for a cluster up to **500 meters** from the geocoded point — a 10× wider envelope than `number_on_street`'s 50m. The score linearly decays from 1.0 (exact match) to 0.0 (at the threshold), so being 250m from a single_road geocode still produces a proximity signal of 0.50, while being 250m from a number_on_street geocode produces 0.0.

Wired into each per-class matcher:

- `where_am_i.py:1224`: `_compute_signals(cluster, topo, target, INTERSECTION_RADIUS_M)` for `_match_intersection`
- `where_am_i.py:1268`: `_compute_signals(cluster, topo, target, SINGLE_ROAD_RADIUS_M)` for `_match_single_road`
- `where_am_i.py:1316`: `_compute_signals(cluster, topo, target, NUMBER_ON_STREET_RADIUS_M)` for `_match_number_on_street`

The comment on `_match_number_on_street` at `where_am_i.py:1308-1312` makes the intent explicit:

```python
def _match_number_on_street(...):
    """Match a number_on_street target ("1234 Main St").

    Tightest proximity threshold of any class (50m) because Google's
    house-number geocode is precise to within a few meters typically.
    """
```

### 3.2 Per-class proximity weight (`_CONFIDENCE_WEIGHTS` again)

Even after the per-class radius scales WHEN the geocode counts as a match, the per-class WEIGHT scales HOW MUCH that match matters to overall confidence:

| Address class | Proximity radius | Proximity weight | Combined effect |
|---|---|---|---|
| `number_on_street` | 50m (tight) | 0.30 (high) | Geocode is precise → demand a tight match, then weight it heavily |
| `intersection` | 250m | 0.10 | Moderate precision → moderate envelope, low weight |
| `single_road` | **500m (widest)** | **0.05 (lowest)** | Geocode is imprecise → wide envelope, almost-ignore the signal |
| `apartment_complex` | 300m | 0.10 | Moderate envelope; weight is moved to `off_wire_pivot` (0.40) |

For `single_road` the matcher effectively says: "If you happen to be within half a kilometer of the geocode, fine, mark that signal as positive — but I'm only going to let it contribute 5% to my final confidence. The real questions for this class are: did you drive on the road (breadcrumb, 35%) and are you on it now (on_target_road, 15%)?"

### 3.3 Compensating signals — where the trust budget goes instead

For low-precision classes the unused proximity weight is reallocated to signals that don't depend on the geocoded coord:

- `single_road`: `breadcrumb_match` 0.35 + `on_target_road` 0.15 + `cluster_tightness/duration` 0.30 = **80%** of the budget goes to road-driving evidence and cluster shape.
- `apartment_complex`: `off_wire_pivot` 0.40 (driver left main road, dwelling on apartment-complex driveway) + `cluster_tightness` 0.20 = **60%** on pivot-and-stop evidence.
- `number_on_street`: proximity 0.30 dominates because the geocode IS the reliable signal for this class.

---

## Summary

- **`classify_address` produces 5 buckets** (`intersection`, `street_number`, `single_road`, `poi`, `garbage`); mapped to **5 address_class strings** on `TargetSpec` (`intersection`, `number_on_street`, `single_road`, `poi`, `garbage`). A 6th class (`apartment_complex`) is assigned elsewhere and handled by its own matcher + weights row.

- **Class drives per-class matcher dispatch** at `where_am_i.py:1526-1538` (`_CLASS_DISPATCH`). Each matcher applies a **per-class proximity radius** AND a **per-class confidence-weights row**.

- **Geocode trust IS scaled by class via two mechanisms acting in concert:**
  1. **Radius:** `single_road` accepts a match up to 500m; `number_on_street` demands 50m. (10× spread.)
  2. **Weight:** `single_road` weights proximity at 0.05; `number_on_street` at 0.30. (6× spread.)
  - Combined, the matcher for `single_road` reallocates ~95% of the confidence budget away from the geocode and onto road-driving signals.

- **No other code path scales geocode trust by class.** No per-class `ST_DWithin` radius in SQL, no per-class confidence penalty, no per-class hard rejection. The TWO mechanisms above are the entire surface.

- **POI/witness signals are class-aware but separate** from the geocode-trust question. They corroborate via `CLASS_TO_TYPE_MAP` but are explicitly Bible-Rule-2 witnesses outside `_CONFIDENCE_WEIGHTS`.

Output for Gemini review. No fixes proposed.
