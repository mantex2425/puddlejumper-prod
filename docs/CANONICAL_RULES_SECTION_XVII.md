# Canonical Rules Amendment — §V Hardening + §XVII (Semantic Anchor)

**Status:** Draft for Gemini ratification, then append to `docs/CANONICAL_RULES.md`.
**Ratified design:** 2026-05-14, after the IAH ride-1 diagnostic that exposed
the venue-class dropoff failure mode (40 venue offers, 0 fires).
**Authorship loop:** Claude proposes → Gemini reviews → Andrew ratifies → ship.
**Revision history:**
  - v1 (2026-05-14): initial draft with parallel `semantic_anchor_cache` table
  - v2 (2026-05-14): revised §E to extend existing `app_private.poi_cache`
    instead of creating a parallel table. Same SQL surface, one table to operate.

---

## V. UBER DATA REALITY (HARDENING APPEND — 2026-05-14)

The existing §V text is correct and complete. This append addresses a
specific failure mode observed in chat: Claude (and humans) drifting into
phrasings like "the geocode Uber returned" or "Uber gave us coordinates."

**Required phrasings.** When describing where coordinates come from,
always attribute correctly:

- ✓ "Google geocoded the address text Uber provided."
- ✓ "The driver's GPS heartbeat at position (lat, lng)."
- ✗ "Uber gave us the geocode (lat, lng)." — WRONG. Uber gave text.
- ✗ "The coords Uber returned for this address." — WRONG. Google returned them.

The distinction matters because any design that internalizes "Uber as a
coord source" loses sight of the geocoder error budget and proposes
gates against Uber-coords that don't exist. The IAH ride-1 case is the
canonical example: Uber's `dropoff_address` was "United, Houston, Texas".
Google's geocoder reduced that text to `(29.99311, -95.34163)` — the
airport polygon centroid, ~4 miles from the actual passenger drop curb.
That `(29.99311, -95.34163)` is the **geocoder's output**, not Uber's.

---

## XVII. SEMANTIC ANCHOR — THE OFFER IS THE QUERY

**Ratified:** 2026-05-14
**Companions:** §V (Uber Data Reality), §XV (Observation Before Narrative),
§XVI (Arrest-Defined Truth)
**Replaces in practice:** the token-driven POI matching strategy that
required maintaining `_EXTENDED_POI_TOKENS` / `_AIRLINE_AIRPORT_TOKENS` /
`CLASS_TO_TYPE_MAP` as Houston-specific data structures. Those structures
remain useful for §XVI's noise-gate and witness signals, but they no longer
gate destination matching.

### The principle

The offer's destination text is a Google Places query. Resolve it via
Places Text Search to a set of **semantic anchors** — real-world POIs
that the destination string refers to. The PUDO fires when the driver
arrests within the venue horizon of any anchor.

This inverts the previous design. The previous matcher asked: *"What
POIs are near the car? Does any of their name/type match the offer?"*
That question fails for airport-class destinations because Phase 3's
50m searchNearby at the arrest coordinates returns POIs like "Female
Bathroom" — accurate for what's nearest the car, useless for confirming
identity against the offer.

The new matcher asks: *"Where does the offer say we're going? Is the
car there?"* For "United, Houston, Texas" Google's Text Search returns
the United terminals, the United Club, the United Bag Drop, the IAH
polygon centroid, and the United Houston Corporate Support Center —
the actual venue footprint. The car at `(29.9869, -95.3350)` is 88m
from one of those anchors. Match.

### A. The text query is the offer's destination text, unmodified

`places:searchText` is called with the offer's `dropoff_address` (or
`pickup_address` when in pickup leg) **as-is, no preprocessing**, no
tokenization, no lexicon lookup, no canonicalization. Google's text
search is robust to messy address strings.

The only structured parameter is `locationBias.circle`:

- center: configurable per market (Houston: `(29.7604, -95.3698)`).
  This is a soft hint, not a hard restriction.
- radius: 50,000 meters. Wide enough to catch all metro destinations
  even when the offer text matches a national chain ("Hilton, Houston,
  Texas" finds Houston Hiltons, not the New York Hilton).

`maxResultCount: 20` — captures enough anchors for high-density venues
like airports without inflating cost.

### B. The arrest gate runs first; §XVII is the matcher inside Phase 2b

§XVII does NOT change the §XVI Forensic Ladder. It plugs in as the
matcher consulted at Phase 2b:

1. **Phase 1** (passive): TAD odometer flags destination zone.
2. **Phase 2** (active): 6s of `speed_mph = 0` accumulates.
3. **Phase 2b** (the new entry point): §XVII fires `places:searchText`
   for each TAD-passing offer's current-leg address. Computes anchor
   distances. Matcher score = `max(0, 1.0 − dist_m / horizon_m)` per
   anchor. The score that crosses WAI's 0.40 floor (and the §XVI
   commit logic above it) triggers Phase 3.
4. **Phase 3-5** (notarize): unchanged.

This means highway-speed drive-bys never reach §XVII. The arrest gate
already filters them. §XVII's "false positive" envelope is bounded by
"places where someone could have arrested for 6s+", which excludes the
vast majority of false positives by construction.

### C. Per-anchor-type horizons (the matcher's only classification step)

The horizon for each returned anchor is derived from the anchor's own
`types` array — NOT from the offer text, NOT from any lexicon we
maintain.

| Anchor type contains            | Horizon |
|---------------------------------|---------|
| `airport`, `international_airport` | 800m |
| `stadium`, `tourist_attraction` | 600m |
| `university`, `shopping_mall`   | 500m |
| `hospital`, `medical_clinic`    | 150m |
| `lodging`                       | 150m |
| (anything else)                 | 500m default |

Rationale: airports and stadiums are huge polygons with sprawling
adjacent infrastructure; passenger-drop happens anywhere in the
complex. Hospitals are tight footprints; the driver must reach the
specific building. The horizons reflect real-world venue geometry.

**This is the only category step in §XVII**, and it operates on
Google's classifications, not ours. Adding a new market doesn't require
extending any lexicon — Google's `airport` taxonomy already covers JFK,
LAX, Heathrow, etc.

When multiple anchors return, each uses its own type-based horizon.
The winning anchor is the one with the highest score (linear-decay
weighted by its own horizon), not the closest in raw meters.

### D. Linear decay confidence weighting

For each returned anchor `a` with horizon `h_a`:

```
score_a = max(0, 1.0 − dist(cluster, a) / h_a)
```

The §XVII signal score for the offer = `max(score_a for a in anchors)`.
The winning anchor's name and type are recorded as witness.

At 0m → score 1.0. At horizon → score 0.0. Outside horizon → no
contribution. This produces graceful degradation when the driver is
near but not at the venue, and lets the §XVI commit logic (weighted
confidence ≥ 0.40 floor, or higher with `poi_type_match`) use the
score in its existing arithmetic without special-casing.

### E. The cache — extension of existing `poi_cache`, not a parallel table

§XVII reuses `app_private.poi_cache`. The table is extended with two
nullable columns:

- `text_query text` — the searchText query string. NULL for legacy
  searchNearby rows; non-NULL for §XVII searchText rows.
- `bias_radius_m double precision` — the locationBias circle radius
  used in the API call.

Dual access patterns share one table:

| `text_query` | Access pattern | Cache key | TTL |
|---|---|---|---|
| `NULL` | searchNearby (legacy) | `(query_lat, query_lng)` via GIST | 30 days |
| `NOT NULL` | searchText (§XVII) | `(text_query, query_lat, query_lng, bias_radius_m)` via partial UNIQUE | 365 days |

**TTL rationale.** SearchNearby cache entries reflect transient
business presence near a point — Starbucks opens, the strip mall
tenants rotate. 30 days is the existing setting and remains correct.
SearchText cache entries reflect venue identity — "United Airlines"
terminals at IAH do not relocate quarterly. 365 days is conservative
even for venue cache; could go longer.

**`last_hit_at` semantics.** Cache hits on either mode touch
`last_hit_at`, throttled to once per hour per row (matches existing
`_read_cache` behavior in `poi_service.py`). Forensic value: lets us
detect cold cache entries for pruning, and confirms which text queries
are most active in production.

**Idempotent dedupe.** New searchText writes use
`INSERT ... ON CONFLICT (text_query, query_lat, query_lng, bias_radius_m)
WHERE text_query IS NOT NULL DO NOTHING`. The partial UNIQUE index
guarantees one row per `(query, bias)` tuple. Legacy searchNearby rows
are unaffected by the partial index.

**Migration.** Idempotent DDL script `tmp/migrate_poi_cache_xvii.sql`
adds the columns + indexes. Safe to run any number of times. Required
prerequisite before §XVII production code lands or before the Tier A
backtest runs.

### F. Witness format

The `pudo_decision_context.poi_top_names` column extends to carry
semantic-anchor witnesses. Format:

```
semantic_anchor:{name}/{primary_type} ({dist_m}m)
```

Example: `semantic_anchor:United/transportation_service (88m)`.

The primary_type is the first element of the anchor's `types` array
(Google sorts these by relevance). For forensic queries, the full
`places` blob is in `poi_cache.places` and joinable via
`text_query = dropoff_address`.

### G. Composition with existing matcher signals

§XVII becomes Head 5 of `_signal_*` in `where_am_i.py`. The existing
heads (Head 1 fuzzy, Head 2 branded, Head 3 airport-type, Head 4
poi_type_match) remain in place. The final signal score is:

```
final_score = max(head1, head2, head3, head4, head5_semantic_anchor)
```

When Head 5 wins, the witness reflects it. When an existing head
wins (e.g. an apartment-complex match via Head 1 fuzzy at a residential
address with no anchor structure), that head's witness wins instead.

Existing token-driven heads continue to work for cases where Google
Text Search returns nothing useful (residential intersections,
single-road dropoffs in suburbs with no nearby commercial venues).
§XVII covers the venue-class gap they were never designed for.

### H. What this does NOT do

- **Does not deprecate `_EXTENDED_POI_TOKENS`.** That set remains in
  use for Head 2 (branded co-reference) and `_signal_poi_type_match`.
  Its scope tightens: it's a *secondary* signal at addresses that
  already passed Head 5 or that Head 5 missed. The two-tier system
  is intentional — anchor primary, tokens secondary.
- **Does not require offer-text-to-category derivation.** No
  `CATEGORY_LEXICON` in the §XVII path. The lexicon-keyed approach
  was discussed and rejected on 2026-05-14 because it reintroduces
  the maintenance trap §XVII is built to eliminate.
- **Does not change the §XVI Phase economy.** Phase 1 still runs TAD
  cheap; Phase 2 still requires 6s arrest; the API call is still
  cache-first and per-arrest-event, not per-heartbeat.
- **Does not create a parallel cache.** §XVII extends `poi_cache`,
  the table already in production. One table to operate.

### I. Forensic record

`pudo_decision_context` rows during §XVII evaluations gain:

- `poi_lookup_source` extends from `{cache_hit, api_call, api_error}`
  to also include `{semantic_cache_hit, semantic_api_call,
  semantic_api_error}` to disambiguate which endpoint sourced the data.
- `poi_top_names` carries anchor witnesses prefixed `semantic_anchor:`.
- `poi_match_score` carries the linear-decay score of the winning
  anchor when Head 5 wins.
- The `poi_cache.places` blob preserves the full anchor list (name,
  type, lat/lng) for offline analysis. JOIN by
  `text_query = offer.dropoff_address`.

### J. Out-of-band scripts

Backtest scripts (`tmp/backtest_xvii_*.py`) MAY write production cache
rows from historical data. This is a deliberate exception, not a
violation: the cache rows are correct and useful regardless of which
process wrote them. Per §XIV.H Out-of-band Exceptions, each such
script documents its exception in
`docs/out_of_band_offer_history_queries.md`.

### K. The discipline

- When a future change wants to add an "airport-specific arming"
  branch in the matcher, refer to this rule and refuse. The arrest
  gate fires for airports the same way it fires for hospitals.
- When a future change wants to extend a per-market token lexicon
  with new airline names or hospital chains, refer to this rule and
  refuse. The anchors come from Google Text Search; no maintenance
  burden.
- When a future change wants to call `places:searchNearby` again for
  primary destination matching, refer to this rule and refuse. The
  inversion is the point.
- When a future change wants to create a parallel cache table for
  "semantic anchors specifically," refer to this rule and refuse.
  Extension of `poi_cache` is the canonical pattern.

> The map is not the territory. The arrest is the pin. But to know
> which arrest matters — ask the offer where it was going.

---

## §XVI cross-reference update

§XVI Phase 2b ("The Arrest") section currently reads:

> Check: WAI confidence on the best-matching offer.
> Gate: Is WAI confidence > 0.40 for any offer that also has TAD passed?

After §XVII ratification, this remains semantically correct — WAI's
"best-matching offer" determination now includes the Head 5 semantic
anchor signal in its `max()` composition. No textual change to §XVI
is required, but readers of §XVI should understand that the WAI
confidence number is now §XVII-aware.

---

## Notes for future Claude sessions

- §XVII is the architectural answer to "venue-class dropoffs missing."
  The pre-§XVII matcher has a documented 0% fire rate on the 40
  venue-style offers in production history (`United/Southwest/Delta/
  IAH/NRG/Galleria` text matches in `offer_history.dropoff_address`).
- §XVII does NOT solve Bug 1 (silent NULL writer to `current_offer_id`).
  Those are orthogonal: §XVII fires Phase 5 cleanly; Bug 1 nulls the
  narrative downstream. Both must be fixed for productive drives.
- Production rollout sequence:
  1. Apply `tmp/migrate_poi_cache_xvii.sql` (idempotent).
  2. Backtest Tier A (104 both-fired accepts, hard truth) — measures
     §XVII's precision against known correct dropoffs. Bootstraps
     the searchText cache as a side benefit.
  3. Backtest Tier C (40 venue-class accepts, cluster-proxy truth) —
     measures §XVII's recovery rate on the segment that's currently
     dark.
  4. Apply patch: new helper in `poi_service.py` for searchText,
     wire Head 5 into `_signal_*` family in `where_am_i.py`.
  5. Real-PG `db_cur` regression test for "United, Houston, Texas" →
     anchors include "United" name at IAH coordinates.
  6. Deploy. Andrew drives. PDC rows show `poi_top_names` carrying
     `semantic_anchor:*` witnesses on venue-class arrests.
