# Stop Atlas — Locked Proposal

**Status:** Approved. Build scheduled for 2026-04-24 in a fresh chat.
**Goal:** BMOAR never fires a pickup nail at a traffic light or railroad crossing.
**Scope:** Internal use only. Purpose-built for PuddleJumper. Production-ready, one-month commercial launch timeline.

---

## 1. Problem

A 30-second zero-speed cluster on a named road near a pickup pin is GPS-indistinguishable from a 30-second zero-speed cluster at a red light or RR crossing. Without external context, BMOAR cannot disambiguate. Forum Park 7623 (2026-04-23) proved this empirically — auto-nail rate dropped to 20% because of traffic-signal false positives.

## 2. Solution

Pre-populate a database of every traffic signal and RR grade crossing in the Houston metro area. Before BMOAR fires a pickup nail, check: is there a known non-pickup stop reason within the per-row suppression radius of this location? If yes, suppress. If no, proceed to gates 1–3.

## 3. Schema (locked)

```sql
CREATE TABLE routing.stop_atlas (
    id                    bigserial PRIMARY KEY,
    lat                   double precision NOT NULL,
    lng                   double precision NOT NULL,
    geog                  geography(Point, 4326) NOT NULL,
    stop_type             text NOT NULL
                          CHECK (stop_type IN ('traffic_signal','railway_crossing')),
    source                text NOT NULL,
    source_id             text,
    suppression_radius_m  smallint NOT NULL DEFAULT 30,
    source_extra          jsonb,
    ingested_at           timestamptz NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_stop_atlas_geog ON routing.stop_atlas USING GIST (geog);
CREATE INDEX idx_stop_atlas_type ON routing.stop_atlas (stop_type);
CREATE UNIQUE INDEX uq_stop_atlas_source_id
    ON routing.stop_atlas (source, source_id)
    WHERE source_id IS NOT NULL;
```

### Schema rationale

- **`lat`/`lng` stored as separate columns AND `geog`.** `geog` is the only column queried on the hot path; `lat`/`lng` are there for debugging / forensics / human-readable `SELECT *`.
- **`stop_type` CHECK constraint** — two values today, enforce at DB layer rather than app layer.
- **`source_id` nullable** — OSM nodes sometimes lack stable IDs. Partial unique index handles this cleanly.
- **`suppression_radius_m` per-row (smallint, default 30)** — addresses Gemini's feeder-intersection problem structurally rather than reactively. Set to 45 on ingest for signals on `motorway`/`trunk`/`primary`/`motorway_link` edges (determined via JOIN against `routing.houston_ways` during ingestion). Everything else stays at 30.
- **`source_extra jsonb` nullable** — for forensic debugging three weeks from now when a weird suppression happens and we need the original source attributes without re-downloading the upstream ZIP. Unindexed, near-zero cost. Yes, we killed jsonb for the core domain columns; this one is explicitly allowed because its purpose is "stash the raw upstream row for debugging."

## 4. Hot-path suppression query (canonical)

Called once per heartbeat when a stop cluster is detected. Expected latency: sub-millisecond at 20–50k rows.

```sql
WITH candidates AS (
  SELECT stop_type, source, suppression_radius_m,
         ST_Distance(geog, app_private.coords_to_geography($1, $2)) AS d
  FROM routing.stop_atlas
  WHERE ST_DWithin(geog, app_private.coords_to_geography($1, $2), 60.0)
)
SELECT stop_type, source
FROM candidates
WHERE d <= suppression_radius_m
ORDER BY d ASC
LIMIT 1;
```

- `$1 = lat`, `$2 = lng` — canonical ordering, always.
- GIST prunes to the 60m neighborhood (empty set on the vast majority of heartbeats).
- The row-wise `d <= suppression_radius_m` filter runs on 0–3 rows.
- If row returned → suppress BMOAR fire. If no row → BMOAR proceeds to gates 1–3.
- **NEVER use `ST_MakePoint` or raw coordinate construction in this codebase.** All coord ops go through `app_private.coords_to_geography(lat, lng)` / `coords_to_point(lat, lng)` / `distance_miles(lat1, lng1, lat2, lng2)`.

## 5. Data sources

### Tier A — authoritative municipal/federal (ingest first)

| Source | Method | Est. rows | Endpoint notes |
|--------|--------|-----------|----------------|
| City of Houston Transtar Signals | Direct ZIP download | ~2,400 | `gisdata.houstontx.gov/gis_open_data/PUBLIC WORKS & ENGINEERING/Transtar_Signals.zip` |
| Harris County PID Traffic Signals | ArcGIS REST paginated query | ~500–1,000 | `geo-harriscounty.opendata.arcgis.com` |
| Fort Bend County signals | ArcGIS Hub | ~300–600 | `gis.fbctx.gov` — layer identification in the ingestion script |
| FRA Railroad Grade Crossings (TX filter) | REST API, filter `state=TX` on ingest | ~500–1,000 | `geodata.bts.gov` |

### Tier B — OSM fill (after Tier A)

Overpass API query for `highway=traffic_signals` and `railway=level_crossing` across the full H-GAC 13-county bbox. Ingested with `source='osm'`. Covers the counties Tier A doesn't (Galveston, Montgomery, Brazoria, Waller, Liberty, Chambers, Austin, Matagorda, Walker, Wharton, Colorado).

### Tier C — not in scope for v1

- Brazoria County email-gated request (OSM covers it acceptably)
- Contributing to OSM upstream
- Real-time signal status
- Stop signs / toll booths / school zones (potential v2 extensions; schema supports via new `stop_type` values)

## 6. Dedup policy — post-ingestion sweep

Authoritative wins. Runs once at the end of the weekly refresh cron, **not** inside each ingestion script (race-prone and couples scripts together).

```sql
DELETE FROM routing.stop_atlas osm
WHERE osm.source = 'osm'
  AND EXISTS (
    SELECT 1 FROM routing.stop_atlas auth
    WHERE auth.source <> 'osm'
      AND auth.stop_type = osm.stop_type
      AND ST_DWithin(auth.geog, osm.geog, 15.0)
  );
```

Deterministic, idempotent, auditable. If a Tier A and an OSM point exist within 15m of the same `stop_type`, keep Tier A, drop OSM.

## 7. Cron refresh

Weekly. Each ingestion script uses `INSERT ... ON CONFLICT (source, source_id) WHERE source_id IS NOT NULL DO UPDATE SET ingested_at = NOW(), lat = EXCLUDED.lat, lng = EXCLUDED.lng, geog = EXCLUDED.geog, source_extra = EXCLUDED.source_extra`. OSM rows without `source_id` are re-inserted fresh each run (truncate OSM rows at start of OSM ingestion script, re-populate, let dedup sweep clean up).

## 8. BMOAR integration

Single helper function `stop_atlas.is_known_stop(lat, lng) -> Optional[dict]` returns `None` if no known stop nearby, or `{"stop_type": ..., "source": ...}` if suppression should fire. Called from BMOAR fire path **before** gates 1–3.

Log prefix: `[ATLAS]`. Every suppression logs the returned `stop_type` and `source` for forensic analysis.

## 9. Build sequence (next chat)

1. DDL migration
2. Tier A ingestion scripts (COH Transtar, Harris County, Fort Bend, FRA) — four separate scripts in `scripts/stop_atlas_ingestion/`
3. Tier B ingestion (OSM)
4. Post-ingestion dedup sweep
5. `stop_atlas.py` helper module (`is_known_stop()`)
6. BMOAR integration — one-line call in fire path
7. Tests (unit + integration)
8. Weekly cron orchestrator
9. Deploy

Each step executed with verification per paired-programming protocol.

## 10. Review history

- **Claude (initial):** H3-indexed, jsonb-tagged, confidence-scored design. Rejected as over-engineered.
- **Claude (revised):** Minimal flat table, GIST-only, no confidence scoring. Approved in principle.
- **Gemini:** Flagged feeder-intersection problem (wide arterials stop further from signal than residential streets). Suggested post-first-drive tuning. Rejected — solved structurally via per-row `suppression_radius_m`.
- **Gemini:** Requested unique constraint on `(source, source_id)` to prevent cron double-loading. Accepted as partial unique index (handles nullable `source_id`).
- **Grok:** Requested `notes` column for debugging. Accepted as `source_extra jsonb`.
- **Grok:** Requested clearer table name. Accepted — `routing.stop_atlas` matches `routing.houston_ways` style and signals "curated geographic reference" rather than "generic infra table."
- **Grok:** Requested configurable radius. Subsumed by per-row column (strictly stronger than global config).
- **Claude (final):** Fixed canonical violation — `ST_MakePoint` replaced with `app_private.coords_to_geography` in hot path. Relocated dedup from per-script to post-ingestion sweep. Locked.

MARKDOWN_EOF