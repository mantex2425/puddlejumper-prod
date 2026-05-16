# Decision: No Topology Rescue for Residential Sub-Stub Clusters

**Date:** 2026-05-16
**Status:** Decided — do not implement
**Investigation case:** Ride 7918 pickup miss (2026-05-15)
**Decision authority:** Andrew (ratified during 2026-05-16 morning session)

---

## The failure class

A driver arrests on a residential side-stub near, but not on, one of the
offer's named roads. The geocode pin is ~500m off from actual arrest.
Path B.2 (Google road-snap) honestly reports "not on the named road."
WAI confidence stays sub-floor (<0.40). PUDO misses.

Specifically for 7918:
- Offer pickup: `FM 2234 Rd & Quail Glen Dr, Missouri City, Texas`
- Geocode pin: `(29.5764944, -95.5162063)`
- Driver arrest: `(29.5813776, -95.5136462)` (557m from pin)
- Actual road at arrest: **Quail Crest Court** — a residential side-stub
- Result: `unmatched_reason = wai_below_floor`, no fire

## What we considered

The "topology rescue" hypothesis: when all five existing WAI signals
score zero, ask the road graph whether the cluster is **topologically
trapped** behind the named road. If removing the named road from the
graph disconnects the cluster from the rest of the city, the driver
must have arrived via the named road, and the signal fires.

Architecture proposed:
- `_signal_road_trapped` sixth signal head in `where_am_i.py`
- `pgr_drivingDistance` with edge-name filter excluding the named road
- Read canonical road names from extended `road_membership_cache`
  breadcrumb capture (proposed extension of `road_membership.py`
  route-extraction logic)
- Persistent cache table `routing.trapped_component_cache`

Validated empirically against 7918 via SQL on 2026-05-16:
- Cluster's nearest OSM node sits on Quail Crest Court (37.5m away)
- Quail Glen Drive exists as 17 segments / 953m total / 160m from cluster
- With Quail Glen as boundary, 2km ceiling: **410 reachable nodes,
  1989m traversed**
- Without boundary, same ceiling: **441 reachable nodes**
- Delta: **7%, not a trap**

The trapped component contained McHard Road, Texas Parkway, Cartwright
Road — real arterials. The cluster's neighborhood connects to multiple
through-roads, not exclusively to Quail Glen Drive. Houston subdivisions
in this part of Missouri City are part of the road grid, not enclosed
behind a single arterial.

## Why not pursue further

Even with test-tightening (closing the 8-edge boundary leak, dropping
ceiling to 500m), the topology rule would be force-fit to this case.
Alternatives considered:

- **Name-token overlap** (cluster on "Quail Crest Court" near offer's
  "Quail Glen Drive" — both contain "Quail"): fragile, fails when
  side-street has unrelated name, per-market maintenance.
- **Distance-based corridor**: rejected earlier in session as arbitrary.
- **Multi-signal hybrid**: accumulates technical debt.

The decisive observation: the failure class requires the **intersection
of three independent conditions** (bad geocode + side-stub arrest +
honest Path B.2 miss). Each uncommon; intersection rare. Engineering
cost to recover does not justify the marginal capture rate.

## The accepted cost

Missed PUDOs in this class are logged via §XVI Forensic Ladder
(`unmatched_reason = wai_below_floor`). The system is not silent —
forensics record the miss for later analysis. Pricing cache and
geographic cache forego one observation. Next ride proceeds normally.

§XVI is permissive about misses: "prefer a missed PUDO (recoverable)
to a wrong-narrative PUDO (corrupts forensic record)." This decision
applies that doctrine: the miss is the right outcome when the recovery
mechanism would be fragile.

## When to revisit

This decision should be reconsidered if:

1. Forensic analysis of historical `pudo_decision_context` rows shows
   that residential-sub-stub failures account for >10% of WAI sub-floor
   misses (currently unknown; would need a dedicated query against
   `unmatched_reason = wai_below_floor` rows).
2. International expansion produces a market with topology genuinely
   different from Houston (e.g., a market where subdivisions are
   commonly enclosed behind a single arterial — gated developments,
   walled compounds). In that case, topology rescue may be cheap and
   correct.
3. A different signal becomes available that solves this class cheaply.
   The §XVII semantic-anchor approach does not — residential
   intersections return non-route POIs from Google's searchText
   endpoint (verified 2026-05-16 against ride 7918's `poi_cache` row).

## Companions

- §V (Uber Data Reality) — coordinates come from geocoding messy text;
  imprecision is expected, not a system failure.
- §XV (Observation Before Narrative) — caches thrive on volume; a single
  miss does not corrupt the system.
- §XVI.D (Distance-to-geocode is never a gate) — the failure mode here
  is the geocode pin being off; the architecture already accepts that.
- §XVII.K (No more stubs) — this decision is the opposite of shipping
  a stub. We choose to not implement rather than ship a fragile rescue.

## Discipline

When a future change wants to propose topology-based or distance-based
recovery for residential sub-stub WAI failures, refer to this document
first. The work was done. The conclusion was: not worth the engineering.
Revisit only with new evidence per "When to revisit" above.
