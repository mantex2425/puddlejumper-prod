# Stop Atlas Ingestion

**Status:** DEFERRED to v1.1 (or possibly never)
**Date deferred:** 2026-04-25
**Decided by:** Andrew Bruce

## What this is

Ingestion scripts for the Stop Atlas — a database of traffic signals,
RR crossings, and other stop features used to classify driver stop
clusters as "at_traffic_signal" vs "at_curb" etc.

`ingest_harris_pid.py` ingests Harris County's Public Improvement
District traffic signal dataset (the largest single source).

## Why it's not in v1.0

The `where_am_i()` continuous awareness primitive (Phase D of v1.0)
provides cluster-level diagnostic signals — `cluster_duration`,
`cluster_tightness`, `breadcrumb_match`, `on_target_road` — that
collectively encode most of what Stop Atlas would tell us, without
requiring a curated traffic-signal database.

In particular:
- A 30+ second stop with on_target_road=1.0 and breadcrumb_match=1.0
  is almost certainly a PUDO regardless of whether we know there's
  a traffic light nearby
- A 15-second stop on a busy road with low confidence on the other
  signals is probably a traffic light regardless of whether we have
  it cataloged

Stop Atlas would be a *third* signal to the diagnosis, useful primarily
as a tiebreaker in the ambiguous middle. Per RFC v2.1 §7, this was
deferred to v1.1 with `_stop_context()` returning `"unknown_stop"`
always in v1.

## Whether to revisit

After Phase D ships and shadow-mode data accumulates, evaluate:
- How often does WAI return ambiguous confidence (0.4-0.7) at stops
  that turn out to be traffic lights?
- Would Stop Atlas data resolve more of those cases than the
  cluster_duration triangle profile already does?

If Stop Atlas would meaningfully improve diagnosis, resume this work.
If WAI alone catches the vast majority of cases, leave deferred.

The maintenance weight of keeping Stop Atlas current (traffic signals
are added/removed/relocated, RR crossings change) is non-trivial and
would need ongoing investment.

## To revive this work

1. `data/houston_stoplights.csv` is the curated reference dataset
2. `ingest_harris_pid.py` is the ingestion script for Harris County PID
3. `routing.known_stops_config` schema is already in production
   (Phase A migration: 2026_04_25_phase_a_where_am_i_foundation.sql)
4. RFC original §7 in WHERE_AM_I_PROPOSAL_v2.md describes the
   integration pattern with `_stop_context()`
