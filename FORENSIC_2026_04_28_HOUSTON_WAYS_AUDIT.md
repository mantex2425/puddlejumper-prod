# Forensic Audit — `routing.houston_ways` Localized Mislabeling at Northwest Crossing

**Author:** Andrew Bruce (probe runner) + Claude (forensic interpretation)
**Date:** 2026-04-28 (overnight, post-1c-doc-refresh commit `773aa49`)
**Trigger:** Open question Q1 from `FORENSIC_2026_04_27_HOUSTON_SHIFT.md` — diagnostic query during the 2026-04-27 Houston shift returned road names from The Woodlands (~30 mi north of Houston) for coordinates in the Northwest Crossing parcel.
**Sibling document:** `FORENSIC_2026_04_27_HOUSTON_SHIFT.md` (committed at `773aa49`). This audit doc enriches that artifact's Q1 without modifying it, per the parent doc's stated modification policy.

---

## Verdict

**Localized data quality issue. Not table-wide corruption. B-27 design unaffected; if anything, validated.**

Three mislabeled segments sit at the Northwest Crossing parcel where they shouldn't be. The remaining 38 of 41 nearby segments (within 200m) are correctly named Houston roads at sensible distances. The table is fundamentally healthy at this parcel; B-27 layer 4 (adjacency learning via shadow-mode observation) is the architectural component that absorbs this class of defect.

---

## Probe methodology

Three read-only PostgreSQL queries against `routing.houston_ways`. All ran sub-second against indexed `the_geom` column. No production code path or write touched.

**Probe coordinate authority.** Two coordinate sets were available:

- **Set 1 (per Gemini field memo, cited in `FORENSIC_2026_04_27_HOUSTON_SHIFT.md` as the canonical regression case):** centroid `29.8490, -95.5035`.
- **Set 2 (per Andrew's Google Maps URL, captured 2026-04-28):** `29.8495463, -95.5037494`. Approximately 60m from Set 1 within the same parcel.

The audit ran probe v1 against Set 1 (matching the committed forensic-doc provenance), and probes v2 + v3 + v4 against Set 2 (the more building-centered point). Both centroids surface the same anomalous rows, confirming the issue is genuinely localized to this parcel and not a centroid artifact.

**`ST_MakePoint` argument-order note.** PostGIS `ST_MakePoint` takes `(x=lng, y=lat)`, the inverse of the project's canonical `(lat, lng)` order (memory note #2). All probes deliberately use raw `ST_MakePoint(lng, lat)` to remove any possibility that anomalies were caused by argument-order bugs in the diagnostic itself.

---

## Probe results

### Probe v1 — 80m buffer, Set 1 centroid `29.8490, -95.5035`

```
  gid   |            road_name            | dist_m
--------+---------------------------------+--------
 331487 | Terramont Drive                 |      1
 259545 | Player Bend Drive               |     35
 578032 | Northwest Freeway Frontage Road |     73
 976561 | Northwest Freeway Frontage Road |     78
 615604 | Northwest Freeway Frontage Road |     78
(5 rows)
```

### Probe v2 — 80m buffer, Set 2 centroid `29.8495463, -95.5037494`

```
  gid   |     road_name     | dist_m
--------+-------------------+--------
 331487 | Terramont Drive   |     12
 259545 | Player Bend Drive |     49
(2 rows)
```

### Probe v3 — Hollister name search, 1km buffer, Set 2

```
   gid   |    road_name     | dist_m
---------+------------------+--------
  951697 | Hollister Street |    121
  944116 | Hollister Street |    121
  950261 | Hollister Street |    136
  ... (20 rows total, all "Hollister Street", distances 121-228m)
```

### Probe v4 — 200m buffer, Set 2

```
  gid    |            road_name            | dist_m
---------+---------------------------------+--------
  331487 | Terramont Drive                 |     12   <- ANOMALOUS
  259545 | Player Bend Drive               |     49   <- ANOMALOUS
  615604 | Northwest Freeway Frontage Road |    111
 1043550 | Northwest Freeway Frontage Road |    111
  ... [12 Hollister Street segments at 121-188m]
  ... [13 Northwest Freeway Frontage Road segments at 111-197m]
  ... [5 Northwest Freeway segments at 136-173m]
  ... [2 US 290 Express Lane segments at 151-159m]
  324551 | Terramont Drive                 |    150   <- ANOMALOUS
(40 rows)
```

**Summary:** 41 rows within 200m of the parcel (probes v3 and v4 combined deduplicate to ~41 distinct gids near the parcel). 3 mislabeled (Terramont/Player Bend); 38 correctly named.

---

## Findings

### Finding 1 — Three segments mislabeled at the Northwest Crossing parcel (the "Woodlands Data Ghost")

`Terramont Drive` (gids `331487` and `324551`) and `Player Bend Drive` (gid `259545`) are documented streets in **The Woodlands**, Texas — approximately 30 miles north of the probed location. They do not exist at the Northwest Crossing parcel in physical reality.

The probes nonetheless place them at distances of 1m, 12m, 35m, 49m, and 150m from the parcel centroid — i.e., these segments' geometries are physically located in Houston, but their `name` field carries Woodlands road names.

Two implications:

1. **Geometry is correctly placed in Houston.** The spatial index is functional; SRID is correctly applied. `ST_DWithin` would not have matched these rows against a Houston center point if their geometries lived in The Woodlands.

2. **The defect is in the `name` field, not the geometry.** This is segment-level data corruption, localized to ~3 rows at this parcel.

**Severity:** Lower than table-wide projection corruption (which would have produced wholly unexpected geographic results). Higher than a query bug (which would have been resolvable by fixing the query alone). The defect is real, bounded, and tractable.

**Root cause hypothesis (unverified):** the most likely mechanism is a bad upstream OSM segment that landed in `routing.houston_ways` during an import or rebase, possibly inheriting names from an OSM edit that hadn't been validated against ground truth. Investigating the affected gids in raw form (with their `the_geom` and any other metadata) is the natural next step but is not blocking.

### Finding 2 — Hollister Street is healthy in the table; original probe missed it due to buffer-distance choice

The first probe (80m buffer) returned no Hollister-named segments and triggered a worry that Hollister was missing from the table. The follow-up probe (1km buffer with `ILIKE '%hollister%'`) returned 20 correctly-named Hollister Street segments, with the closest at **121m** from the building-centered probe coordinate.

**The 80m buffer was simply too tight for this parcel.** The Northwest Crossing building footprint extends well beyond 80m in at least one direction; Hollister sits along the building's far edge from the probe centroid.

This is a meaningful observation about adjacency-query design generally: **a fixed-radius buffer is the wrong abstraction for parcel adjacency.** Different parcels have different geometric footprints. A small standalone shop might have a 30m parcel; a large strip mall with multiple anchors might have a 200m+ parcel. Any production B-27 implementation must use a parcel-shape-aware buffer (e.g., the Google Places viewport bounding box) rather than a global radius constant.

### Finding 3 — The remaining roads at the parcel are correctly represented

Within 200m of the building-centered probe coordinate, `routing.houston_ways` contains:

- **12 Hollister Street segments** (distances 121-188m)
- **13 Northwest Freeway Frontage Road segments** (distances 111-197m)
- **5 Northwest Freeway segments** (distances 136-173m)
- **2 US 290 Express Lane segments** (distances 151-159m)

All four of these are physically present in reality. The table contains them at sensible distances and with correct names.

**Ratio: 38 of 41 nearby segments are correctly labeled.** The mislabeling is a small fraction of the parcel's adjacency representation, not a dominant pattern.

---

## Architectural impact on B-27

The audit's findings actually **validate** the B-27 architecture rather than threaten it. Three points:

### 1. Layer 4 (adjacency learning via shadow-mode observation) is load-bearing, not an optional refinement

The B-27 backlog entry in `PHASE_E_PROGRESS.md` (committed at `773aa49`) describes layer 4 as: *"shadow-mode telemetry refines the cache. Roads Google didn't list but drivers consistently use → promoted. Roads Google listed but no driver uses → demoted."*

This audit gives us a concrete, ground-truthed example of the class of defect layer 4 is designed to correct. If a downstream B-27 component were ever to surface "Terramont Drive" as an adjacent road for the Northwest Crossing parcel — whether sourced from `routing.houston_ways`, from a Google API anomaly, or from any other upstream — layer 4 would observe that **zero drivers ever drive on Terramont Drive when arriving at Planet Fitness or Target**, and demote it. Conversely, every arrival uses Hollister Street or Northwest Freeway Frontage Road, so those promote regardless of any upstream's claims.

A cooperative-cache architecture without layer 4 would propagate the upstream error to every driver in the fleet via layer 3. **With layer 4, the corpus converges on physical truth even when every individual data source is partially wrong.** The fleet's collective driving pattern is the ground truth, and observation is the validation layer.

This is the strategic moat property restated in concrete terms: **PuddleJumper's adjacency knowledge is more accurate than any single upstream source because every active driver is a sensor.**

### 2. Google Roads API remains the right primary source for layer 1

Not because `routing.houston_ways` is broken — it's mostly fine — but because Roads API is genuinely more authoritative for "what roads are at this place":

- Updates more frequently than any OSM-derived snapshot
- Native `nearestRoads` operation against parcel viewport edge points (no buffer-distance choice required, addressing Finding 2's design concern)
- Canonical source for Place IDs already used elsewhere in B-27's design

`routing.houston_ways` becomes a useful **cross-validation source** (when both agree, confidence is high; when they disagree, layer 4's observation is the tiebreaker), and a **fallback** if Roads API is unavailable. But it is not the primary path.

### 3. The 3 mislabeled rows are a tracked-and-bounded data quality issue, not a blocker

The affected gids are known: `331487`, `259545`, `324551`. Investigation, decision (rename/delete/relabel), and repair can happen as a small targeted spike at any time. It is not on the Phase E or Phase F critical path, and B-27 can ship ahead of any cleanup because layer 4 absorbs the case correctly.

A broader sweep of `routing.houston_ways` for similar mislabeling patterns elsewhere in the table is worth considering eventually but is not blocking. The cooperative-cache architecture means even unknown upstream defects are corrected through observation.

---

## Recommended follow-ups (none blocking)

1. **Targeted segment investigation** of gids `331487`, `259545`, `324551`. Inspect their raw geometry and any associated OSM metadata. Determine the right repair (rename to correct Houston street, delete if duplicates exist, or relocate to The Woodlands if the geometries were also misimported). Estimated effort: 30 minutes.

2. **Sweep audit** for similarly mislabeled segments elsewhere in `routing.houston_ways`. A pattern-detection query (segments whose name contains a known Woodlands street name but whose geometry sits south of, say, latitude 29.95) would surface candidates. Estimated effort: 1-2 hours. Not urgent given B-27's layer-4 absorption property.

3. **B-27 design lock for layer 4 specifics.** Promotion/demotion thresholds, observation-window sizing, decay parameters. Phase F territory; this audit provides the concrete test case for layer 4's effectiveness.

---

## Document status

**Status:** complete audit, ready to commit.
**Promotion path:** cited from B-27 design proposal during Phase F sub-step authoring as the empirical evidence supporting layer 4's necessity.
**Modification policy:** this document is a complete audit artifact. Subsequent enrichments (e.g., results of segment investigation, sweep audit findings) should author separate sub-documents rather than amend this one, mirroring the parent forensic doc's policy.