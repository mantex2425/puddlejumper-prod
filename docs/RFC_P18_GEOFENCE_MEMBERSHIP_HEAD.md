# RFC P18 — Geofence Membership Matcher Head for _match_poi_class

**Status:** Revision 2 — incorporates Gemini review 2026-05-20
**Author:** Andrew + Claude + Gemini
**Date:** 2026-05-20
**Sequence:** Follows P17 (geofence ingestion). Final piece of the geofence stack.

---

## 1. Motivation

Empirical findings from production drive 2026-05-20:

- Head 4 (poi_type_match): binary, proximity-blind, fires at 1.000 anywhere with broad POI types within 100m. Demoted to witness in P16.
- Head 5 (semantic_anchor): correct distance math but fails on real venues because Google geocoded centroid is often 500-1500m from venue perimeter. At Hobby curb, Head 5 score = 0.012.
- Head 1 (poi_match fuzzy): returns 0.444 for "American Airlines" vs "Braeburn Liquor" at the 8082 wrong-fire location due to broad token overlap. Fires above the 0.4 floor.

All three heads fail in different but architecturally related ways. P17 ingested OpenStreetMap polygon data into `routing.geofence_polygons` providing ground-truth venue boundaries. P18 introduces `_signal_geofence_membership` as the authoritative scorer when ground truth is available, with legacy heads as fallback only when no containment exists.

## 2. Where The New Head Lives

`_match_poi_class` in `where_am_i.py` currently composes confidence via `max(h5_score, h1_score)` post-P16. P18 changes this to a **ground-truth-priority pattern** (revised per Gemini ratification 2026-05-20):

```python
h6_score, h6_witness = _signal_geofence_membership(cur, cluster.median_lat, cluster.median_lng, target.address)
h5_score, h5_witness = _signal_semantic_anchor(anchors)
h1_score, h1_witness = _signal_poi_match(pois, target.address)
h4_score, h4_witness = _signal_poi_type_match(pois, target.address)  # witness only since P16

if h6_score > 0.0:
    # Ground truth available: geofence is authoritative.
    # Heads 1, 5 retained as witnesses but do NOT contribute to confidence.
    confidence = h6_score
    winning_head = "geofence"
else:
    # No polygon contains this cluster. Fall back to legacy ensemble.
    confidence = max(h5_score, h1_score)
    winning_head = "legacy_ensemble"
```

Function signature for the new head:

```python
def _signal_geofence_membership(
    cur,                       # psycopg2 cursor
    cluster_lat: float,
    cluster_lng: float,
    target_text: str,          # offer text used for name disambiguation
    name_match_floor: float = 0.6,
) -> tuple[float, Optional[str]]:
```

Returns `(score, witness)` matching the established head contract.

## 3. Scoring Logic

Two-phase: containment check, then name disambiguation.

**Phase 1 — Containment:**

```sql
SELECT id, name, name_normalized, category, area_m2, tags
FROM routing.geofence_polygons
WHERE ST_Contains(geom, ST_SetSRID(ST_MakePoint($lng, $lat), 4326))
ORDER BY area_m2 ASC
LIMIT 5;
```

If zero rows: return `(0.0, None)`. The caller falls through to the legacy ensemble.

If one or more rows: proceed to name disambiguation.

**Phase 2 — Name disambiguation:**

For each containing polygon (innermost first), compute name match score against `target_text`:

- **Tier 1 — IATA/ICAO code match (airports/terminals only).** Read code from `polygon.tags->>'iata'` or `polygon.tags->>'icao'`. If present, use word-boundary regex against normalized target text. Match → 1.0.
- **Tier 2 — Fuzzy name match.** `rapidfuzz.fuzz.partial_ratio(polygon.name_normalized, target_normalized) / 100.0`. Above `name_match_floor` (default 0.6) → 1.0.

**Score table per Gemini ratification 2026-05-20 revision:**

| Phase 1 | Phase 2 | h6_score | Effect | Witness |
|---|---|---|---|---|
| No containment | — | 0.0 | Caller falls back to legacy ensemble | None |
| Contained | Tier 1 or Tier 2 hit | 1.0 | Fires above floor | `geofence:{name}/{category} [match=iata/fuzzy]` |
| Contained | No name match | **0.30** | Records polygon hit but does NOT clear 0.4 fire floor | `geofence:contained-no-name-match in {name}` |

The 0.30 is below the WAI_CONFIDENCE_THRESHOLD (0.40). Per Gemini ratification: **unverified containment must not clear the fire gate.** Otherwise queued offers for OTHER venues of the same category (three hospital contracts, one Methodist visit) would simultaneously fire when the driver enters any hospital polygon.

When h6_score is 0.0 or 0.30, the caller still falls back to the legacy ensemble because 0.30 < 0.40. If legacy ensemble produces a stronger signal (e.g. Head 5 = 0.45 from a tight semantic match), it wins. The 0.30 witness is recorded for forensic visibility.

Actually — re-reading the section 2 logic, the 0.30 case STILL takes the geofence-authoritative branch because `h6_score > 0.0`. That's a bug in my own logic. Fix: change the branch condition.

**Corrected branching logic:**

```python
if h6_score >= 1.0:
    # Ground truth + name match: definitive
    confidence = h6_score
    winning_head = "geofence"
elif h6_score > 0.0:
    # Containment without name match: record witness, still fall back to legacy
    confidence = max(h5_score, h1_score)
    winning_head = "legacy_ensemble_after_geofence_partial"
    # h6_witness still recorded on MatchOutcome.geofence_witness
else:
    # No containment: pure legacy fallback
    confidence = max(h5_score, h1_score)
    winning_head = "legacy_ensemble"
```

## 4. IATA Code Source

OSM polygons sometimes carry IATA/ICAO codes in their tags. Verification query against P17 ingested data:

```sql
SELECT name, tags->>'iata' AS iata, tags->>'icao' AS icao
FROM routing.geofence_polygons
WHERE category = 'airport';
```

If `tags->>'iata'` is reliably populated (e.g. 'HOU' for Hobby, 'IAH' for Intercontinental), P18 uses that field. If sparsely populated, P18 ships with a hardcoded curated map (`{"HOU": polygon_id_for_hobby, "IAH": polygon_id_for_iah, ...}`) for the Houston market and grows the map per market.

This is determined at implementation time once we can query the actual ingested data.

## 5. Behavior at Specific Today's-Drive Locations

| Location | Cluster | Containment | Phase 2 | h6 | Final confidence | Fires? |
|---|---|---|---|---|---|---|
| 8082 wrong-fire (Bissonnet/Hillcroft) | 29.6713, -95.5284 | NONE | — | 0.0 | max(0.0, 0.444) = 0.444 | YES — REGRESSION (see below) |
| 8092 Hobby dropoff | 29.65479, -95.27736 | Hobby polygon | "HOU" tag matches "(HOU)" in offer text | 1.0 | 1.0 | YES |
| 8094 Hobby pickup (Main Terminal) | 29.6479, -95.2767 | Hobby polygon | "HOU" matches | 1.0 | 1.0 | YES |
| IAH pickup | (IAH coords) | IAH polygon | "IAH" matches | 1.0 | 1.0 | YES |

**The 8082 regression flag is real and must be addressed.** With h6=0.0 (no containment) the engine falls back to legacy ensemble, which produces 0.444 from Head 1 fuzzy match against Braeburn Liquor — the exact wrong fire we wanted to prevent.

Two paths to resolve:
- **Option A:** Demote Head 1 in addition to Head 4 (extends P16). `_match_poi_class` legacy fallback becomes `max(h5_score)` only.
- **Option B:** Raise the WAI_CONFIDENCE_THRESHOLD from 0.40 to 0.50, demoting Head 1's 0.444 score below the fire gate without removing it from the ensemble.

P18 ships with **Option A** because Head 1's fuzzy-match-on-broad-tokens is the same architectural pattern that broke Head 4. This is consistent with the P16 trajectory.

**Final post-P18 ensemble:**

```python
if h6_score >= 1.0:
    confidence = h6_score
elif h6_score > 0.0:
    # Containment without name match
    confidence = h5_score      # Head 1 also demoted; Head 5 is sole legacy survivor
else:
    confidence = h5_score      # No containment, no Head 1
```

This collapses to: confidence is max(geofence-with-name-match, semantic_anchor). Heads 1 and 4 are witness-only.

## 6. Test Plan

New test file: `tests/test_signal_geofence_membership.py`. Six cases (revised from 5):

1. `test_geofence_no_containment_returns_zero` — point outside any polygon → 0.0
2. `test_geofence_containment_with_iata_code_match_returns_one` — point inside Hobby + text contains "HOU" → 1.0
3. `test_geofence_containment_with_fuzzy_name_match_returns_one` — point inside NRG + text contains "NRG" → 1.0
4. `test_geofence_containment_without_name_match_returns_partial` — point inside polygon + text doesn't match any name → 0.30
5. `test_geofence_iata_code_word_boundary_prevents_substring_false_match` — "HOU" must not match "Houston Street" target text
6. `test_geofence_innermost_polygon_wins_on_overlapping_containment` — TMC has 11 overlapping hospital polygons; smallest area wins for name matching

Additional test for the new ensemble logic in a separate file: `tests/test_match_poi_class_geofence_priority.py`. Three cases:

7. `test_geofence_definitive_overrides_legacy` — h6=1.0 wins even if h5=0.0
8. `test_legacy_used_when_no_containment` — h6=0.0 → confidence = h5
9. `test_legacy_used_when_containment_without_name_match` — h6=0.30 → confidence = h5 (Head 1 NOT in ensemble)

Pytest floor moves: 604 → 613.

## 7. Migration / Deploy Impact

- Pure code change. No schema migration required (P17 already shipped the table).
- Cloud Run deploy via `bash deploy.sh`.
- Rollback: revert P18 commit; production returns to P16 behavior.

## 8. Risks

1. **Performance:** GIST index on `routing.geofence_polygons.geom` gives sub-ms `ST_Contains`. Each heartbeat tick adds ONE query per active offer.

2. **OSM polygon name vs offer text mismatch:** OSM names are sometimes formal/local ("William P. Hobby Airport") while offer text is informal ("Hobby"). Fuzzy match handles common cases. IATA tier handles airport codes when tags->>'iata' is populated. Edge cases fall to the 0.30 bucket — recorded as witness, doesn't fire on its own.

3. **Polygons with empty names:** Some OSM polygons (retail buildings) have NULL name. P18 records containment for forensic purposes but falls into 0.30 bucket.

4. **Coverage gaps:** P17 ingested Houston only. Outside Houston, h6=0.0 → legacy ensemble. Head 1 demotion means coverage outside Houston relies solely on Head 5 (semantic anchor). This is a known regression in non-Houston markets until they're ingested.

5. **Head 1 demotion drops fuzzy-match coverage for generic commercial POIs (clinics, restaurants):** Inside Houston with geofence coverage, these will fall to the 0.30 bucket if Head 5 doesn't find them either. Net effect: production becomes silent on commercial POI dropoffs unless they're in an OSM polygon OR have a strong semantic anchor. Trade-off for eliminating 0.444-class false fires.

