# Phase E Forensic Field Report — Houston Overnight Shift, 2026-04-27

**Author:** Andrew Bruce (driver/observer)
**Doc structured:** 2026-04-27 (same session as Phase E sub-step 1c closeout doc refresh)
**Phase context:** Phase E sub-step 1c SHIPPED at commit `72cf951`; trilogy complete in code but DORMANT (`driver_heartbeat.py` does not call WAI/PudoPlanner today). All findings below are against the legacy BMOAR stack.

---

## Purpose & Provenance (L-9 framing)

This document captures field observations from a single overnight Houston driving shift in which the legacy BMOAR system surfaced multiple distinct failure modes against real production traffic. The observations are recorded here as **L-9-class fixture provenance** — future Phase F integration tests for B-12 (Reconcile dispatch) and B-27 (POI Adjacency Matrix) will cite this document as the ground-truth source for synthetic test fixtures derived from these cases.

Synthetic fixtures cannot generate corner-lot, geocode-drift, or bypass-after-decline scenarios with fidelity. The cases below are the kind of real-world ground truth that only emerges from live driving in a specific market.

**Methodology:** live-shift observation. System behavior cross-referenced against driver memory and Gemini field-support session transcripts captured during and immediately after the shift. Coordinates supplied where Gemini's session captured them at the time; coordinates marked **(approximate)** where the driver is reconstructing from memory.

**Coordinate system:** all coordinates EPSG:4326 (WGS-84 lat/lng) unless otherwise noted.

**Telemetry retrieval status:** raw GPS breadcrumbs, `offer_history` rows, and `community_offers` cluster medians for these specific rides have NOT yet been pulled from the production database. A follow-up session is scheduled to enrich this document with database-backed timing and cluster-shape data. Sections marked **[telemetry pending]** flag where database enrichment will land.

---

## Shift summary

| Field | Value |
|---|---|
| Driver | Andrew Bruce (driver_id `UjT1hE9eBXh2q95aSZYOkzDJ8lo1`) |
| Market | Houston (market_id `6a35d28b-8e6c-4d60-94aa-2661e2650863`) |
| Date | 2026-04-27 overnight |
| Stack active in production | Legacy BMOAR |
| Trilogy status | In code at `72cf951`, dormant — `driver_heartbeat.py` does not call WAI/PudoPlanner |
| Cases captured below | A, B, C, D, E |

---

## Case A — McDonald's at 13201 Northwest Fwy (S32 corner-lot failure)

| Field | Value |
|---|---|
| POI | McDonald's |
| Address | 13201 Northwest Fwy, Houston, TX 77040 |
| Coordinates | (approximate; telemetry pending) |
| Trip type | (telemetry pending — pickup or dropoff) |
| Uber-listed road | Northwest Freeway |
| Driver's actual approach | Back-road / side parking lot entrance to avoid frontage road congestion |
| Specific approach road | (telemetry pending — back-road name to be confirmed via OSM lookup against parcel polygon) |
| System behavior at arrival | Structural Pillar (S32) reported 0% road match. BMOAR did not register arrival until driver was physically at the building. |
| Failure classification | S32 corner-lot failure (B-27 candidate) |

**Significance:** First of three corner-lot cases in this shift. The driver's path never touched the named "Northwest Freeway" segment because access to the parcel was achieved via a side road. Today's S32 logic tests `current_road == target_road` (string equality), which fails when the driver legitimately accesses a multi-frontage parcel from a non-named edge.

---

## Case B — Planet Fitness at 5770 Hollister Rd (S32 + canonical regression case)

| Field | Value |
|---|---|
| POI | Planet Fitness |
| Address | 5770 Hollister Rd, Houston, TX 77040 |
| Centroid | `29.8490, -95.5035` |
| Hollister-side access | `29.8488, -95.5028` |
| NW Fwy frontage access | `29.8495, -95.5045` |
| Long-axis spread of access points | ~70m |
| Trip type | Dropoff |
| Uber-listed road | Hollister Rd |
| Driver's actual approach | Northwest Fwy frontage road, entirely |
| System behavior at arrival | Structural Pillar (S32) reported 0% road match. Auto-Nail failed to fire. |
| Driver intervention | Manual Nail required to confirm dropoff |
| Failure classification | S32 corner-lot failure (B-27 canonical regression case) |

**Significance — designated canonical B-27 regression case.** Coordinates were captured live during the shift via Gemini field support and are precise enough to drive a synthetic fixture. The Hollister-side and NW Fwy frontage access points are ~70m apart on a single parcel; the parcel adjoins both roads but the driver path traversed only one of them. This is the exact geometry B-27 (Adjacency Matrix) is designed to handle.

---

## Case C — Target at Northwest Crossing strip mall (S32 — same-parcel inversion proof)

| Field | Value |
|---|---|
| POI | Target |
| Location | Northwest Crossing Shopping Center, NW Fwy / Hollister area |
| Same building structure as | Case B (Planet Fitness) — confirmed via Google Maps satellite view, Apr 28 2026: [https://www.google.com/maps/@29.8495463,-95.5037494,17z](https://www.google.com/maps/@29.8495463,-95.5037494,17z) |
| Coordinates | (approximate; telemetry pending) |
| Trip type | Pickup |
| Uber-listed road | Northwest Freeway |
| Driver's actual approach | Hollister Rd, entirely |
| System behavior at arrival | Structural Pillar (S32) reported 0% road match (same failure mode as Case B) |
| Failure classification | S32 corner-lot failure (B-27 candidate) |

**Significance — this case is the killer finding for the B-27 inversion.** Cases B and C occurred in the **same shift**, in the **same connected building structure** (verified via satellite view at the URL above — Planet Fitness anchors the Hollister/W Tidwell end of an L-shaped retail building; Target anchors the NW Fwy end of the same connected structure), but with the Uber-listed named road and the driver's actual approach road **swapped** between them:

| | Case B (Planet Fitness, dropoff) | Case C (Target, pickup) |
|---|---|---|
| Uber-listed road | Hollister Rd | Northwest Fwy |
| Driver's actual road | NW Fwy frontage | Hollister Rd |
| S32 result | 0% match | 0% match |

This proves the **last-N-roads-driven heuristic** (a candidate fallback design that would track the driver's recent breadcrumb history and check membership against the target road) **provably fails on this structure in both directions.** No matter which road the driver approached on, neither trip's named road was in the look-back set. The structure is large enough (the Planet-Fitness-to-Target diagonal spans roughly 350m across the building footprint) that legitimate driver paths can use either road frontage and never touch the other.

The B-27 architectural inversion (compute the destination's Adjacency Set at offer-capture time; test `current_road IN adjacency_set` instead of strict equality) is the only proposed approach that handles both Case B and Case C with a single mechanism. Designs that rely on driver-history matching cannot.

**Cooperative-cache implication (B-27 Layer 3).** Because Planet Fitness and Target occupy the same connected building structure, their computed Adjacency Sets should converge to the same set: roughly `{Hollister St, W Tidwell Rd, Northwest Fwy frontage, Northwest Dr}`. Every offer-capture event for either store contributes to the same cached adjacency, and a fleet-wide cache reaches near-100% hit rate on this structure quickly. This is the cooperative-cache flywheel made concrete: each driver's encounter with this building (regardless of which store inside it) heals the Adjacency Set for every future driver. **Important nuance for B-27 design: the cache key cannot be the POI alone — it must be keyed on the building footprint or the place_id's parent geometry, so that semantically-related POIs in the same structure share a cached adjacency. This is a design question to lock during Phase F sub-step authoring.**

---

## Case D — Sheraton Houston West, 12621 Northwest Fwy (B-12 TargetSpec Vacuum)

| Field | Value |
|---|---|
| POI | Sheraton Houston West |
| Address | 12621 Northwest Fwy, Houston, TX |
| Coordinates | (approximate; telemetry pending) |
| Trip type | Pickup → dropoff cycle |
| Initial event | Uber offer presented; BMOAR auto-declined for low mileage rate |
| Driver action | Manually accepted the declined offer (bypass) |
| System state post-bypass | No `TargetSpec` exists internally because the offer was originally declined; state machine remained in IDLE |
| Pickup behavior | System "blind" — no internal contract for an active ride |
| Lag manifestation | System "woke up" and declared "En route to pickup" approximately **1 mile after the driver had already left the pickup with the passenger** |
| Dropoff behavior | System refused to confirm dropoff because it still believed the driver was en route to pickup |
| Driver intervention | (telemetry pending — exact intervention sequence to be reconstructed) |
| Failure classification | TargetSpec Vacuum / Ghost Ride (B-12 Reconcile candidate) |

**Significance.** This is the canonical B-12 (Reconcile dispatch) failure case. The legacy BMOAR contract assumes that a `TargetSpec` is created if and only if BMOAR itself accepted the offer. Driver bypass-acceptance breaks that assumption and leaves the state machine in a position where it cannot bind to physical reality even when the driver is mid-ride. The system's eventual "wake up" was reactive (catching up to physical events one mile late) rather than reconciliative.

B-12 (Reconcile dispatch, scheduled for Phase E Step 7, test slots T90-T99 reserved) is the architectural fix: when the state machine is IDLE but cluster-discovery indicates an active stop pattern consistent with a ride underway, synthesize a `TargetSpec` from the cluster and dispatch into the active-ride state machine.

---

## Case E — Post-Sheraton ride (584m geocode drift)

| Field | Value |
|---|---|
| POI | (telemetry pending — likely IHOP at 10928 Westheimer or the next offer immediately following the Sheraton trip) |
| Trip type | (telemetry pending) |
| Reported drift at arrival | 584m between driver position and BMOAR's pickup/dropoff pin |
| Driver's physical position | Across the street from the named address |
| Cause | Geocode pin placed at the geometric center of a large commercial block rather than at the street-side entrance |
| System behavior | (telemetry pending — completion sequence to be reconstructed) |
| Failure classification | S31 Geometric Pillar failure (geocode-pin-in-parcel-centroid anti-pattern) |
| Driver memory of incident | Driver noted at the time: *"Clearly, geocoding... is not working. Reinforce that we've got to fix the dwai."* |

**Significance.** The 584m drift is not a GPS error — it is a **geocode-pin error**. The pin landed in the geometric center of a large commercial parcel; the driver was at the parcel's street-frontage entrance, which is across-the-street-distance from the centroid pin. Today's S31 (Geometric Pillar) uses a strict distance threshold (`d < 50m`) which makes this scenario unrecoverable: no amount of correct GPS will get the driver "close enough" to a pin that's been placed in the wrong spot.

This case reinforces the general architectural principle that emerged from this shift: **in commercial corridors, the Memory Pillar (cluster discovery) must take precedence over the Geometric Pillar (distance-to-pin).** The driver stopped at the correct physical location; that stop is the truth, regardless of where the geocode pin landed.

---

## Cross-cutting findings

This shift surfaced four distinct architectural conclusions, each grounded in one or more of the cases above:

### Finding 1 — Distance is unreliable in commercial corridors (Case E)

Geocode pins for large commercial parcels routinely land at the parcel centroid rather than at the street-side entrance. Distance-based arrival detection (today's S31 threshold) cannot recover from a mis-placed pin. **The Memory Pillar (cluster proximity) must subordinate the Geometric Pillar in commercial corridors.** This principle is already implemented in the trilogy (1a/1b/1c shipped at `72cf951`); Case E is field-validation that the architectural choice was correct.

### Finding 2 — Road labels are unreliable for multi-access parcels (Cases A, B, C)

Three independent cases this shift involved a parcel with multiple road frontages where the Uber-listed "named" road did not match the driver's actual approach road. Today's S32 string-equality test fails this category of parcel by design. **B-27 (Adjacency Matrix) is the architectural fix: compute the destination's set of adjacent roads at offer-capture time, test membership instead of equality.**

### Finding 3 — Last-N-roads-driven heuristic provably fails on corner lots (Case C)

Case C is the regression proof. Cases B and C visited the **same parcel** in the same shift with **inverted named-road/driven-road pairings**. A driver-history look-back design (track the last N road segments, check the target road for membership) cannot succeed on Case C in either direction — neither named road is ever in the driver's look-back set when they're at this parcel. Only a destination-property-based design (Adjacency Set computed from the parcel's geometry, not the driver's history) handles both trips with a single mechanism.

### Finding 4 — TargetSpec must be reconcilable from cluster discovery, not just from accepted offers (Case D)

The legacy BMOAR contract assumes a `TargetSpec` exists if and only if BMOAR accepted the offer. Driver bypass-acceptance breaks this assumption and produces a "Ghost Ride" state where the state machine is blind to active physical events. **B-12 (Reconcile dispatch, Phase E Step 7) is the architectural fix: cluster-discovery synthesizes a `TargetSpec` when the state machine is IDLE but physical observation indicates an active ride.**

---

## Backlog cross-references

| Backlog item | Phase | Cases that motivate it |
|---|---|---|
| B-12 (Reconcile dispatch) | Phase E Step 7 | Case D (canonical), Case A (secondary — post-arrival lag) |
| B-27 (POI Adjacency Matrix + Cooperative Cache) | Phase F sub-step (TBD) | Case A, Case B (canonical), Case C (regression proof for inversion) |
| S31 review (Geometric Pillar threshold) | Phase F observability | Case E |

---

## Telemetry retrieval pending

The following enrichments are scheduled for a follow-up session to populate the **(telemetry pending)** fields above:

- `offer_history` rows for each of cases A–E, joined to driver_id `UjT1hE9eBXh2q95aSZYOkzDJ8lo1` and the shift's date range
- `community_offers` cluster medians for the actual arrival points
- Raw GPS breadcrumb sequences from the Android client telemetry stream
- OSM road-name lookups against each parcel polygon to confirm Adjacency Set candidates for cases A–C
- Exact driver-intervention sequences for cases D and E

Each enrichment can be authored as a separate forensic sub-document (e.g. `FORENSIC_2026_04_27_TELEMETRY_ENRICHMENT.md`) without modifying this document, preserving this artifact's status as the original observation record.

---

## Open questions for next session

### Q1 — `routing.houston_ways` data integrity audit

A diagnostic query during the shift returned road names from The Woodlands (~30 mi north of Houston) for coordinates in the Hollister area. The audit hierarchy:

1. **Most likely:** coordinate-order bug in the diagnostic query (`ST_MakePoint(lat, lng)` instead of `ST_MakePoint(lng, lat)`). The canonical-coordinate rule (memory note #2) exists to prevent this; ad-hoc field queries don't always route through `app_private.coords_to_*`.
2. **Possible:** localized name-mislabeling on a small number of OSM-imported segments.
3. **Unlikely (but flagged for completeness):** SRID projection mismatch at the table level.

**Recommended audit path:** run Grok's adjacent-roads template query (Apr 27 2026) against the Planet Fitness centroid `29.8490, -95.5035`. Expected output if the table is healthy: rows containing both "Hollister Rd" and "Northwest Fwy" (or frontage road equivalent), nothing from The Woodlands. Outcomes and their interpretations:

| Probe outcome | Interpretation |
|---|---|
| Hollister + NW Fwy returned, no Woodlands | Table is healthy; original Woodlands result was a coordinate-order bug in the ad-hoc query |
| Woodlands roads returned for Planet Fitness coords | Localized data integrity issue; targeted audit of houston_ways for affected segments |
| Wholly unexpected geographic results | Escalate to full SRID/projection audit |

The probe is a 5-minute psql experiment with no production impact. Strongly recommended as the next concrete action after the doc-refresh commit lands.

### Q2 — B-27 design ratification window

B-27 design lock is gated on (a) Q1 audit clearing, and (b) Phase F shadow-mode telemetry validating the strip-mall noise floor. The strip-mall edge case (Case C and similar future cases) is the highest-risk scenario: interior parking-lot drives may snap as roads via the Roads API and pollute the Adjacency Set. Phase F shadow telemetry must observe and quantify this before B-27 ships any layer beyond layer 1 (per-offer cache).

### Q3 — Case A / Case E telemetry reconstruction priority

Cases B, C, and D have sufficient detail in this document to drive synthetic-fixture authoring once the Phase F sub-steps for B-12 and B-27 begin. Cases A and E are partial. **Recommended next-session action:** before authoring any B-12 or B-27 design proposal, retrieve the `offer_history` rows for cases A and E so this document can be promoted to canonical fixture provenance for all five cases.

---

## Document status

**Status:** initial observation record (cases A–E captured at the level of driver memory + live Gemini transcripts).
**Promotion path:** telemetry enrichment → fixture-provenance canonical → cited from B-12 and B-27 test docstrings per L-9.
**Modification policy:** this document is the original observation record. Subsequent enrichments author separate sub-documents rather than amending this artifact, so the original observations remain auditable.