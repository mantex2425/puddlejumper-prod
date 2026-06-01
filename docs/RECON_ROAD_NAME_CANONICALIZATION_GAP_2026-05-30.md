# Recon: Directional-Prefix Canonicalization Gap in `_road_names_match`

**Date:** 2026-05-30
**Deployed revision:** `puddlejumper-api-00634-gq8` (branch `phase-2c-2-geocode-signal`)
**Investigator:** Claude (server-side recon, read-only)
**Status:** Read-only investigation complete. No code edited. Output for Gemini review.

**Related recons (load together):**
- `docs/RECON_MATCHER_EMPTY_CANDIDATES_2026-05-30.md` — bind-drift on `current_cumulative_miles` (verdict 1)
- `docs/RECON_ADDRESS_CLASS_GEOCODE_TRUST_2026-05-30.md` — confirmed address-class scales geocode trust via per-class radius + per-class proximity weight

---

## ⚠ CORRECTION ADDED AFTER DEEPER SCOPING

**The user's original recon brief framed today's drive as "5 of 5 pickups starved." Subsequent investigation found this is incorrect — 2 of 5 pickups starved, 3 of 5 fired correctly.** §4 of this doc still shows the original scoping framing; treat the table there as superseded by this addendum.

**Actual breakdown across the 5 pickup arrest windows:**

| # | pickup CT | snapped road | matcher_candidates | unmatched_reason | starved? | cause |
|---|---|---|---|---|---|---|
| 1 | 12:22:38 | South Post Oak Road | `{}` (8 hbs) | `lost_mode_no_candidate` | **YES** | **canonicalization bug — this doc** |
| 2 | 12:40:49 | Concourse Drive | `{8580, 8578}` | NULL | NO | fired correctly |
| 3 | 12:56–12:57 | South Gessner Road then NULL | `{8582, 8581, 8580, 8578}` across both arrest windows | NULL | NO | fired correctly (see below) |
| 4 | 13:47:55 | NULL | `{8584, 8583}` at the fire, then `wai_below_floor` on subsequent ticks (normal post-fire abstention) | NULL→post-fire | NO | fired correctly |
| 5 | 13:59:31+ | Imperial Valley Drive | `{}` for 97s (17 hbs) | `wai_below_floor` | **YES** | **mystery — see §4.2 below** |

**Definitive count: 1 of 5 explained by canonicalization bug. 1 of 5 has an unexplained mystery (pickup 5). 3 of 5 fired correctly.**

### Why pickup 3 (`S Gessner Rd`) didn't trigger the canonicalization gap as predicted

The static trace I documented in §1 says `_road_names_match("South Gessner Road", "S Gessner Rd")` should return False — same substring problem as S Post Oak. Yet the matcher fired `{8582, ...}` for the `S Gessner Rd & Westpark Dr` offer. Two possible explanations (neither verified in this recon):

1. **`target.named_roads` doesn't carry the raw OCR token verbatim.** The geocoder may have populated `named_roads` with a normalized form that includes `"South Gessner Rd"` or both spellings. My trace assumed `named_roads = ("S Gessner Rd", "Westpark Dr")` exactly; the actual contents need verification — I did NOT trace where `named_roads` is populated from the geocoded offer payload.
2. **Other signals carried confidence over the floor.** The second road token `"Westpark Dr"` may have matched the breadcrumb (driver may have come from Westpark), and intersection-class proximity weight (0.10) plus cluster signals may have been enough.

The S Post Oak canonicalization bug is real and reproduced verbatim in §1's trace. But scope-at-population claims need a separate recon into how `target.named_roads` is populated from offer geocoding — the bug fires deterministically for pickup 1 but not for the S Gessner case despite the identical substring-check failure.

### §4.2 Pickup 5 mystery (`wai_below_floor` on Imperial Valley Drive)

Pickup 5 IS genuinely starved but the canonicalization bug does NOT explain it:

- `wai_current_road = "Imperial Valley Drive"` every heartbeat
- Offer 8585 = `"Imperial Valley Dr & Parramatta Ln, Houston, Texas"` (intersection class)
- `_road_names_match("Imperial Valley Drive", "Imperial Valley Dr")`:
  - `canonicalize_address("Imperial Valley Drive")` → `imperial valley dr` (Drive → Dr suffix-canonical)
  - `canonicalize_address("Imperial Valley Dr")` → `imperial valley dr` (already abbreviated)
  - `"imperial valley dr" in "imperial valley dr"` → **True**
- `on_target_road` should fire (1.0 × 0.20 = 0.20)
- `breadcrumb_match` should fire if breadcrumb includes Imperial Valley Drive (1.0 × 0.30 = 0.30)
- Combined road signals alone: **0.50 — should clear the 0.40 floor**

Driver was at cluster (30.0277983, -95.4203267); offer geocode (30.0289303, -95.4208767); distance ~136m — well within intersection's 250m proximity threshold. Proximity contribution ≈ 0.046.

**Predicted total confidence ≈ 0.55+ — should have CLEARED the floor.** But it didn't, for 17 consecutive heartbeats while arrest grew from 11s to 97s and cluster grew from 3 to 12 anchors.

**Root cause unknown from PDC data.** `wai_*` columns only populate for the WAI winner; per-offer scoring isn't serialized to `tad_decision_context` (TAD verdict captures distance-gate state, not WAI's per-signal breakdown).

**Plausible hypotheses (none verifiable from current data):**
1. `target.named_roads` for offer 8585 doesn't actually contain `"Imperial Valley Dr"` — the geocoder may have populated it differently
2. Breadcrumb at 13:59:31 didn't include Imperial Valley Drive — driver may have approached from a side street and snap-to-road only caught Imperial Valley at the moment of arrest, leaving breadcrumb empty for it
3. Some intersection-matcher quirk specific to the `Parramatta Ln` second road token
4. A WAI calibration / signal-aggregation issue specific to this offer's geometry

**To diagnose pickup 5 properly, the codebase needs forward-looking instrumentation** — add `wai_per_offer_scores jsonb` to `pudo_decision_context` capturing per-offer signal breakdowns. The next drive that hits this pattern would self-diagnose. Without it, pickup 5 is a forensic dead end — the data needed to characterize the failure isn't being captured.

**Pickup 5 is likely the legitimate "destructive dilution" case** the user originally flagged in `[[project-pj-open-diagnostics-2026-05-27]]` item 4 — but the present PDC schema doesn't capture enough to nail it.

---

## VERDICT

**`address_utils.canonicalize_address` does NOT normalize directional prefixes (`S` ↔ `South`, `N` ↔ `North`, etc.).** As a consequence, `pivot_context._road_names_match` returns FALSE for any case where the Uber-text address uses the directional abbreviation and OSM stores the spelled-out form (or vice versa) — e.g. `"S Post Oak"` (Uber) vs `"South Post Oak Road"` (OSM).

When this canonicalization fails for a `single_road` offer, the matcher loses **all three of its road-evidence signals at once** — `breadcrumb_match` (weight 0.35), `on_target_road` (weight 0.15), `adjacent_road_match` (weight 0.10). Combined with `single_road`'s deliberately-tiny `proximity` weight (0.05), this collapses ~95% of the confidence budget to zero. The offer cannot clear the 0.40 floor and silently abstains, even though the driver IS demonstrably on the correct road.

**This is the "destructive dilution" pattern.** The architecture's defense against bad geocodes (downweight proximity, demand road evidence) is itself defeated by a road-name comparison defect that silently zeroes the road-evidence signals.

**Runtime-proven for offer 8576 at the 12:22:38 starved pickup. Suspected for at least one additional pickup today (12:57:17 — `S Gessner Rd`). Three of five starved pickups today have a different starvation mechanism (canonicalization not the cause).**

---

## 1. The trace through `_road_names_match`

**File:** `pivot_context.py:282-295`

```python
def _road_names_match(snapped_name: str, address_road: str) -> bool:
    """Fuzzy match between an OSM road name and a YOLO'd address road name.

    Both get lowercased and abbreviation-normalized via canonicalize_address.
    Match if either contains the other (to absorb OSM adding directional or
    county qualifiers that Uber's text doesn't include, and vice versa).
    """
    if not snapped_name or not address_road:
        return False
    s = canonicalize_address(snapped_name).strip()
    a = canonicalize_address(address_road).strip()
    if not s or not a:
        return False
    return s in a or a in s
```

The docstring claims to absorb "OSM adding directional or county qualifiers that Uber's text doesn't include." This works for SUFFIX additions (Road / Rd) but not for PREFIX expansions (S / South).

**File:** `address_utils.py:26-58`

```python
SUFFIX_CANONICAL = [
    (r"\bstreet\b",    "st"),
    (r"\bavenue\b",    "ave"),
    (r"\bboulevard\b", "blvd"),
    (r"\bdrive\b",     "dr"),
    (r"\broad\b",      "rd"),
    (r"\blane\b",      "ln"),
    (r"\bcourt\b",     "ct"),
    (r"\bplace\b",     "pl"),
    (r"\bparkway\b",   "pkwy"),
    (r"\bfreeway\b",   "fwy"),
    (r"\bhighway\b",   "hwy"),
    (r"\btrail\b",     "trl"),
    (r"\bcircle\b",    "cir"),
    (r"\bterrace\b",   "ter"),
    (r"\btrace\b",     "trce"),
    (r"\bcrossing\b",  "xing"),
    (r"\bpoint\b",     "pt"),
]


def canonicalize_address(addr: str) -> str:
    """Normalize an address for cache keying.

    Lowercases, collapses whitespace, and abbreviates spelled-out road
    suffixes. "4th Street & Orchard Street, Missouri City" and
    "4th St & Orchard St, Missouri City" produce identical canonical forms.
    """
    s = addr.lower().strip()
    s = re.sub(r"\s+", " ", s)
    for pattern, repl in SUFFIX_CANONICAL:
        s = re.sub(pattern, repl, s)
    return s
```

**Zero directional-prefix handling.** The list is suffix-only.

### 1.1 Verbatim trace for `_road_names_match("South Post Oak Road", "S Post Oak")`

| step | operation | output |
|---|---|---|
| 1 | `canonicalize_address("South Post Oak Road")` lowercase + ws | `south post oak road` |
| 2 | apply SUFFIX_CANONICAL `\broad\b → rd` | `south post oak rd` |
| 3 | `canonicalize_address("S Post Oak")` lowercase + ws | `s post oak` |
| 4 | apply SUFFIX_CANONICAL — no patterns match | `s post oak` |
| 5 | `s in a` → `"south post oak rd" in "s post oak"` | **False** |
| 6 | `a in s` → `"s post oak" in "south post oak rd"` | **False** |
| 7 | return | **`False`** |

The substring check at step 5/6 fails because `"s "` (s + space) is not a substring of `"south "` (s + outh + space) — the directional token's expanded form intercalates between the leading `s` and the next word boundary.

---

## 2. Signal collapse for offer 8576 at 12:22:38

Offer 8576 — pickup_address `"S Post Oak, Houston, Texas"`. Bucket per `classify_address`: **`single_road`** (bare road name, no number, no intersection).

`_CONFIDENCE_WEIGHTS["single_road"]`:

| signal | weight | actual value at 12:22:42 | contribution | reason |
|---|---|---|---|---|
| `proximity` | 0.05 | **0.0** | 0.0 | cluster (29.6258, -95.4656) is ~820m from geocoded pickup (29.6330, -95.4637), beyond single_road's 500m threshold |
| `breadcrumb_match` | 0.35 | **0.0** | 0.0 | `_road_names_match("South Post Oak Road", "S Post Oak")` → False (canonicalization gap) |
| `on_target_road` | 0.15 | **0.0** | 0.0 | same canonicalization gap on `wai_current_road = "South Post Oak Road"` |
| `cluster_tightness` | 0.15 | ~0.5 partial | ~0.08 | 4-anchor cluster, moderate spread |
| `cluster_duration` | 0.15 | ~0.3 partial | ~0.05 | 16s of arrest, ramping |
| `off_wire_pivot` | 0.05 | likely 0 | 0.0 | driver was on-wire on South Post Oak throughout |
| `adjacent_road_match` | 0.10 | likely 0 | 0.0 | same canonicalization barrier |

**Max achievable confidence ≈ 0.13**, well below the **0.40 floor**. WAI correctly returned no candidate **given the signals it computed** — but three of its strongest signals (combined weight 0.60) were structurally suppressed by the canonicalization defect.

---

## 3. Runtime evidence at 12:22:38

### 3.1 Driver IS on S Post Oak (snap-to-road confirms)

Pulled `wai_current_road` from `pudo_decision_context` for the arrest window:

```
   id   |           hb_ct            |  wai_current_road   | cluster_size
--------+----------------------------+---------------------+-------------
 336151 | 2026-05-30 12:22:36.720313 | South Post Oak Road |            3
 336152 | 2026-05-30 12:22:42.493363 | South Post Oak Road |            4
 336153 | 2026-05-30 12:22:48.174034 | South Post Oak Road |            5
 336154 | 2026-05-30 12:22:53.880307 | South Post Oak Road |            6
 336155 | 2026-05-30 12:23:00.27135  | South Post Oak Road |            7
 336156 | 2026-05-30 12:23:06.03257  | South Post Oak Road |            8
 336157 | 2026-05-30 12:23:11.780339 | South Post Oak Road |            9
 336158 | 2026-05-30 12:23:17.455215 | South Post Oak Road |           10
```

**8 consecutive heartbeats with `wai_current_road = "South Post Oak Road"`.** Snap-to-road IS working. Breadcrumb capture is NOT broken. The driver IS on S Post Oak. The matcher knows it.

### 3.2 Pickup approach is textbook

`heartbeat_log` for the preceding ~3 minutes shows classic pickup-arrival shape:

| time CT | speed_mph | notes |
|---|---|---|
| 12:19:34 — 12:20:08 | 0 mph | stationary (prior fare dropoff) |
| 12:20:13 — 12:20:46 | 23 → 44 → 36 mph | acceleration onto fast road, eastward |
| 12:20:51 — 12:21:13 | 19 → 10 → 14 mph | slowing, approaching turn |
| 12:21:19 — 12:21:41 | 11 → 6 → 4 mph | turn south, decelerating onto S Post Oak |
| 12:21:46 — onward | 0 mph | stopped at (29.6258823, -95.4654856), 22+ seconds stationary at pickup |

Driver IS at a real pickup. No question.

### 3.3 The geocode is the bad data

| field | value |
|---|---|
| Offer 8576 pickup_address | `"S Post Oak, Houston, Texas"` |
| Offer 8576 geocoded pickup | (29.6329959, -95.4636588) |
| Driver's actual arrest location | (29.6258823, -95.4654856) |
| Distance | **~820 meters** |

This is exactly the "geocode can be 800m+ off the curb" pattern the address-class recon documented. Google's geocode of a bare-road query lands somewhere on the road but not necessarily near the real pickup. The matcher's defense (single_road proximity threshold 500m, proximity weight 0.05) is correct in design. Both knobs fired correctly here — proximity was beyond threshold (signal 0.0) AND weighted at 0.05 anyway.

**The geocode imprecision alone would NOT have starved the matcher** — `breadcrumb_match` and `on_target_road` together carry weight 0.50, which IS enough to clear the 0.40 floor when they fire. The starvation only happens because the canonicalization gap **silently zeroes the road-evidence signals at the moment they would have rescued the match.**

---

## 4. Scoping: how many of today's 5 starved pickups does this explain?

Cross-referenced `wai_current_road` at each starved pickup window against the likely-target offer's pickup_address. Manual canonicalization walk:

| # | pickup CT | snapped road | likely offer | offer road token(s) | canonical match? | starvation cause |
|---|---|---|---|---|---|---|
| 1 | 12:22:38 | `South Post Oak Road` → `south post oak rd` | **8576** `S Post Oak` → `s post oak` | ✗ MISMATCH | **canonicalization bug (proven)** |
| 2 | 12:40:49 | `Concourse Drive` → `concourse dr` | 8580 `Concourse Dr & Finchwood Ln` → `concourse dr` / `finchwood ln` | ✓ matches | **NOT canonicalization** (different mechanism) |
| 3 | 12:57:17 | NULL (snap missing) | 8582 `S Gessner Rd & Westpark Dr` → `s gessner rd` / `westpark dr` | likely ✗ if snapped = `South Gessner Road` | **suspected same bug** (cannot fully confirm without snap data) |
| 4 | 13:47:43 | NULL (snap missing) | 8583 `Cypress Station Dr` / 8584 `Hollow Tree Ln` | neither token has a directional | **NOT canonicalization** (insufficient data on actual mechanism) |
| 5 | 13:59:26 | `Imperial Valley Drive` → `imperial valley dr` | 8585 `Imperial Valley Dr & Parramatta Ln` → `imperial valley dr` / `parramatta ln` | ✓ matches | **NOT canonicalization** (different mechanism) |

**Definitive bug count: 1/5. Suspected (S Gessner): 2/5. Remaining 3/5 starvations have a different cause** — pickups 2 and 5 had road-name matches that should have fired `on_target_road` and `breadcrumb_match` cleanly; their starvations need separate investigation (possibly bind-drift from verdict-1 recon, or matcher behavior on intersection-class offers when only ONE of the two road tokens matches the snapped road, or something else entirely).

---

## 5. Scope assessment in Houston

The canonicalization gap is triggered by any address whose Uber-text uses a directional abbreviation (`N`, `S`, `E`, `W`, `NE`, `SW`, etc.) AND whose OSM road name uses the spelled-out form (or the reverse). Houston has dozens of directional-prefix grid streets — S Post Oak, N Shepherd, W Gray, S Wayside, S Main, N Main, E Crosstimbers, W 11th, W Alabama, S Voss, NW Freeway, etc. Plus the inverse pattern is possible: an offer text saying `"North Main"` against an OSM road named `"N Main St"`.

Likely affected today: pickups 1 and 3 (S Post Oak, S Gessner) = **40% of today's starved pickups attributable or suspected attributable to this single canonicalization defect**.

Likely affected at population scale: any pickup whose pickup address starts with a directional abbreviation, AND whose offer reaches the `single_road` or `intersection` matcher branches that depend on `_road_names_match` for breadcrumb / on_target_road / adjacent_road_match signals. For POI/apartment classes the bug is masked because their matchers don't use these road-evidence signals as the primary signal — but for any other class, this defect can silently zero the matcher's three strongest road-evidence signals.

---

## 6. Connections to other open items

- **`docs/RECON_MATCHER_EMPTY_CANDIDATES_2026-05-30.md`** (verdict 1, bind drift): explains why offer 8575 (intersection-class, `Parks Edge Blvd & River Trace Ct`) didn't reach the matcher at all — distance-gate bind drift filtered it before WAI even saw it. Bind-drift and canonicalization-gap are INDEPENDENT bugs operating on independent failure surfaces; fixing one does not affect the other.
- **`docs/RECON_ADDRESS_CLASS_GEOCODE_TRUST_2026-05-30.md`** (verdict A, address-class trust scaling): documents the per-class proximity weight + radius. The canonicalization-gap bug doesn't change that design — it just exposes a hidden dependency: the per-class trust shift to road-evidence signals only works if the road-evidence signals can fire. They can't fire when canonicalization is broken.
- **`[[project-pj-open-diagnostics-2026-05-27]]` item 4 — WAI doctrinal review / destructive dilution.** This recon answers the "destructive dilution" question concretely for at least one pickup: the dilution is not a calibration mis-tune; it's a road-name comparison defect that silently zeroes three signals at once. The WAI weights are correct; their input is incomplete.

---

## What I did NOT do (per spec)

- No code edits.
- No fix proposals. (For Gemini review: the fix surface is `SUFFIX_CANONICAL` extended with a directional-prefix list, OR a separate `DIRECTIONAL_CANONICAL` list with a parallel substitution pass, OR a preprocessing step in `_road_names_match` that strips leading directional tokens before substring comparison. Tradeoffs differ; not my call.)
- Did not chase pickups 2, 4, 5 starvation mechanism. Those need separate investigation if priority allows — they may be additional canonicalization gaps on the second road token of an intersection, or bind-drift residuals, or something else.
- Did not verify whether `_match_intersection` correctly fires when ONLY ONE of the two road tokens matches (pickup 2 had Concourse Dr match but Finchwood Ln may not have been in any breadcrumb). Worth a follow-up.

Output for Gemini review. No fixes proposed.
