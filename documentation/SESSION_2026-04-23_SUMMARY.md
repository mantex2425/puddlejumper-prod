# Session Summary — 2026-04-23

## What was accomplished

- Diagnosed why auto-nail rate dropped to 20% tonight. Root cause: BMOAR cannot disambiguate a pickup stop from a traffic-light stop using GPS alone. Forum Park 7623 was the forensic anchor case.
- Designed the Stop Atlas: an external geographic reference of known traffic signals and railroad grade crossings, queried by BMOAR before firing a pickup nail.
- Three-reviewer consensus (Claude / Gemini / Grok) reached on final schema and integration.
- Locked proposal saved to `documentation/STOPS_DB_PROPOSAL.md`.

## Key architectural decisions (locked)

- Table name: `routing.stop_atlas` (matches `routing.houston_ways` naming; signals curated reference rather than generic infra table).
- Per-row `suppression_radius_m smallint DEFAULT 30`, set to 45 on ingest for signals on `motorway`/`trunk`/`primary`/`motorway_link` edges. Solves the feeder-intersection problem structurally.
- Nullable `source_extra jsonb` for forensic debugging — explicitly allowed despite the general rule against jsonb, because its purpose is stashing raw upstream rows.
- Partial unique index on `(source, source_id) WHERE source_id IS NOT NULL` — handles OSM nodes without stable IDs.
- Hot path uses `app_private.coords_to_geography(lat, lng)` with a two-stage GIST-prune-then-row-filter query. No `ST_MakePoint` anywhere.
- Dedup sweep runs post-ingestion, not inside each script.

## Forum Park 7623 forensic anchor

The empirical ground-truth case that motivated the Stop Atlas build. If radius tuning or over-suppression debugging comes up later, this is the reference datapoint.

**Needs to be filled in from tonight's session logs before they're lost:**
- Pin location (lat, lng):
- Actual pickup location (lat, lng):
- Distance from pin to stop cluster (meters):
- Stop cluster duration (seconds):
- Nearest traffic signal distance (meters):
- Nearest signal source (COH Transtar / Harris County / FRA / OSM):

## Next session scope

Build the Stop Atlas capability. Production-ready, one-month commercial launch timeline. Solo developer — full implementation on every step, no MVP phasing.

Build sequence:
1. DDL migration (`routing.stop_atlas` + indexes)
2. Four Tier A ingestion scripts (COH Transtar, Harris County PID, Fort Bend, FRA grade crossings)
3. Tier B OSM fallback ingestion
4. Post-ingestion dedup sweep
5. `stop_atlas.py` helper module with `is_known_stop(lat, lng)`
6. BMOAR integration — one-line call in the fire path before gates 1–3
7. Tests (unit + integration)
8. Weekly cron orchestrator
9. Deploy

Paired-programming protocol applies. Each step verified before the next starts.

## Reference files

- `documentation/STOPS_DB_PROPOSAL.md` — locked proposal, canonical source of truth for tomorrow's build
- `documentation/SESSION_2026-04-23_SUMMARY.md` — this file

