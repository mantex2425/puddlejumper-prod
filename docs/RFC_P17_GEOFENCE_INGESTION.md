# RFC P17 — OSM Geofence Ingestion for PuddleJumper Matcher

**Status:** Ratified (Andrew + Gemini + Claude) 2026-05-20
**Sequence:** Follows P16 (Head 4 demotion). Precedes P18 (geofence membership head).
**Revision 2:** incorporates Gemini review 2026-05-20 (geometry not geography; drop parent_polygon_id; drop GIN index; tile Overpass queries).

---

## 1. Motivation

Per production drive 2026-05-20 forensic, `_match_poi_class` cannot reliably identify when the driver is physically inside a named venue (airport, stadium, hospital, university, mall, theme park, train station).

Google Places `searchNearby` at the driver's arrest centroid returns local commercial POIs (coffee shops, bathrooms, kiosks) but NOT the parent venue itself. Even at the Hobby airport curb, no POI of type `airport` is in the 1000m response set.

`searchText` (Head 5) returns the parent venue only when offer text is specific. Generic terms ("Main Terminal, Arrivals (Baggage Claim level) Zone 5") return zero anchors.

We need a ground-truth membership test: **is the driver's cluster centroid inside a named polygon?** OpenStreetMap provides this via global, free, frequently-updated polygon data.

Empirical validation 2026-05-20: probe across 15 globally diverse POIs returned 15/15 usable polygons via the Overpass API.

## 2. The Polygon Table

New table: `routing.geofence_polygons`

```sql
CREATE TABLE routing.geofence_polygons (
    id              bigserial PRIMARY KEY,
    osm_id          bigint NOT NULL,
    osm_type        text NOT NULL CHECK (osm_type IN ('way', 'relation')),
    category        text NOT NULL,
    subcategory     text,
    name            text,
    name_normalized text,
    tags            jsonb NOT NULL,
    geom            geometry(MULTIPOLYGON, 4326) NOT NULL,
    area_m2         double precision NOT NULL,
    centroid_lat    double precision NOT NULL,
    centroid_lng    double precision NOT NULL,
    market          text NOT NULL,
    ingested_at     timestamptz NOT NULL DEFAULT (NOW() AT TIME ZONE 'UTC'),
    source_revision text NOT NULL,
    UNIQUE (osm_type, osm_id)
);

CREATE INDEX idx_geofence_polygons_geom ON routing.geofence_polygons USING GIST (geom);
CREATE INDEX idx_geofence_polygons_category_market ON routing.geofence_polygons (category, market);
```

Schema decisions:
- `geom` is `geometry` not `geography`. Planar math is sub-millisecond cheaper per call. Accuracy loss across Houston metro is negligible (~0.05% at 30°N). Per Gemini ratification 2026-05-20.
- No `parent_polygon_id`. Innermost-polygon resolution at query time via `ORDER BY area_m2 ASC LIMIT 1` over the spatially-filtered result set. Avoids state liability across partial market refreshes. Per Gemini ratification 2026-05-20.
- No GIN index on `name_normalized`. Name matching happens in Python application tier after spatial filter narrows to ≤5 polygons. GIN would be write-only overhead. Per Gemini ratification 2026-05-20.
- `tags` jsonb stores all OSM tags for forensic audit and future tag-based queries.
- `market` is e.g. 'houston', 'la', 'london' — lets us refresh per market without rebuilding the whole table.

## 3. Category Mapping

Empirically validated 2026-05-20 against 15 global POIs.

| Category | OSM tag fallback chain | Min area (m²) |
|---|---|---|
| airport | aeroway=aerodrome | 50,000 |
| terminal | aeroway=terminal | 1,000 |
| train_station | railway=station, public_transport=station, building=train_station | 500 |
| stadium | leisure=stadium, leisure=sports_centre, building=stadium | 5,000 |
| arena | leisure=stadium, leisure=sports_centre, building=sports_hall, amenity=events_venue | 3,000 |
| hospital | amenity=hospital, healthcare=hospital | 1,000 |
| mall | shop=mall, building=retail, landuse=retail | 5,000 |
| themepark | tourism=theme_park, leisure=park | 50,000 |
| university | amenity=university, amenity=college, landuse=education | 10,000 |

Tag fallbacks: ingest tries each tag chain per category; a polygon may surface under whichever chain hits first. If the same OSM feature appears under two chains it deduplicates on `(osm_type, osm_id)`.

## 4. Polygon Hierarchy

No stored parent relations. At query time:

```sql
SELECT id, name, category, subcategory, area_m2
FROM routing.geofence_polygons
WHERE ST_Contains(geom, ST_SetSRID(ST_MakePoint($lng, $lat), 4326))
ORDER BY area_m2 ASC
LIMIT 5;
```

Returns innermost polygon first. The matcher (P18) chooses the most specific containment based on offer-text affinity, falling back to next-innermost if name mismatches.

## 5. Name Matching (P18 territory; documented here for completeness)

The matcher answers: "the car is inside polygon 'George Bush Intercontinental Airport - Houston'. Does this match offer text 'IAH, Houston, Texas'?"

Two-tier matching planned for P18:
- **Airport code tier:** existing `_AIRLINE_AIRPORT_TOKENS` and `detect_branded_token` machinery from `bead_on_wire.py`. Exact substring match for IATA/ICAO codes (HOU, IAH, LAX, JFK, ORD, etc.). Match → high name confidence.
- **Fuzzy tier:** `rapidfuzz.fuzz.partial_ratio` against `name_normalized`. Threshold 0.60. Subordinated to airport code tier to prevent "HOU" → "Houston Street" false positives.

**Name match is corroboration, not gate.** Polygon containment is the primary signal. Container without name match still observed but reduced confidence. Container with name match → high confidence.

## 6. Per-Market Scope and Overpass Tiling

Initial ingest: Houston market only. Outer bbox:
- North: 30.10 N, South: 29.40 N, East: -95.00 W, West: -95.85 W
- ~7,500 km², covers Houston metro including IAH, Hobby, NRG, TMC, downtown, ship channel, Sugar Land, Pearland, Katy.

**Per Gemini ratification 2026-05-20:** Single-bbox queries for dense categories (hospitals, retail, stadiums) will timeout against the public Overpass API. Ingest must tile.

Tiling strategy:
- Sub-divide Houston bbox into a 4×4 grid (16 tiles, ~1,900 km² each)
- For each (category, tile) pair: run separate Overpass query
- For dense categories (hospital, mall, train_station) all 16 tiles always run
- For sparse categories (airport, themepark) the full bbox can be queried in one shot since results count is small

Network resilience (reused from probe_osm_v2):
- 3 Overpass endpoints rotated: overpass-api.de, kumi.systems, openstreetmap.fr
- 3 retries per endpoint, exponential backoff (3s, 6s, 12s) on 429/504/502/503
- 5s sleep between queries
- 180s Overpass query timeout, 200s HTTP timeout

Total Houston ingest expected: 30-90 minutes wall clock for the first run.

Refresh: monthly cron, market-by-market.

## 7. Acceptance Criteria

P17 ingest is successful when:
1. `routing.geofence_polygons` exists with the revised schema
2. ≥ 200 Houston polygons ingested across 9 categories
3. Specific named entities present (sentinel set): 'William P. Hobby Airport', 'George Bush Intercontinental Airport - Houston', 'NRG Stadium', 'Houston Methodist Hospital', 'University of Houston', 'Galleria'
4. For each sentinel, `ST_Contains(geom, ST_Point(lng, lat))` returns the polygon for a known-interior coordinate
5. Spot check: 8092 dropoff cluster (29.65479, -95.27736) → returns Hobby airport polygon
6. Spot check: 8082 wrong-fire location (29.6713, -95.5284) → returns NOTHING (not inside any polygon)
7. Indexes built and ANALYZE'd, ST_Contains query p95 < 5ms

## 8. Scope Boundary

P17 is data ingestion only. The new matcher head is P18.

- No changes to `where_am_i.py` or `poi_service.py` in P17.
- No global expansion beyond Houston.
- No Overture Maps or commercial source integration.
- No "live" Overpass calls from production — only the ingest cron uses Overpass; production reads only the local `routing.geofence_polygons` table.

## 9. Risks

1. **Tag schema variance across regions:** Heathrow as multipolygon, NRG as closed_way. Handled by the same code path that probe_osm_v2 already validated.
2. **Polygon name quality:** Some OSM polygons have empty `name` tags. We accept polygons without names — they still provide containment.
3. **Centroid offset surprises:** IAH centroid was 1539m from the geocoded "address" centroid. Integration tests must use KNOWN INTERIOR POINTS, not geocoded address points.
4. **Multi-overlapping polygons:** TMC has 11 overlapping hospital polygons. ST_Contains may match several. `ORDER BY area_m2 ASC LIMIT N` returns innermost first; matcher disambiguates by name.
5. **Overpass instability mid-ingest:** Mitigated by endpoint rotation, retries, tiling. If still failing, fall back to nightly cron and accept partial Houston coverage initially.


---

## 10. Revision 3 — Gemini ratification 2026-05-20 (pre-Block-C lockdown)

Three implementation requirements added to the build:

### 10.1 ST_Area on SRID 4326 returns square degrees

`ST_Area(geom)` on `geometry(MULTIPOLYGON, 4326)` returns area in square decimal degrees, not square meters. Min-area filters would reject every polygon.

**Required pattern in the ingest script:**

```sql
INSERT INTO routing.geofence_polygons (..., geom, area_m2)
VALUES (..., $1::geometry, ST_Area($1::geography));
```

Runtime `ST_Contains` queries stay planar (no cast) for sub-ms performance. Only the one-time area computation casts to geography.

### 10.2 Word-boundary tokenization for IATA codes (P18 prep)

When P18 implements name matching, IATA/ICAO codes (HOU, IAH, LAX, JFK, ORD, LGA, etc.) MUST be matched with word boundaries, not raw substrings. "HOU" must not match "Houston Street".

**Locked pattern for P18 implementation:**

```python
import re
def airport_code_match(code: str, name_normalized: str) -> bool:
    return bool(re.search(r'\b' + re.escape(code) + r'\b', name_normalized))
```

Recorded here to prevent the substring trap when P18 lands.

### 10.3 ON CONFLICT for tile-straddling features

The 4×4 tile grid will return duplicate OSM features when a polygon straddles tile boundaries (IAH at 38 km² will appear in 4+ tiles). The UNIQUE (osm_type, osm_id) constraint protects table integrity; the ingest script must absorb the conflict cleanly.

**Required pattern in the ingest script:**

```sql
INSERT INTO routing.geofence_polygons (...)
VALUES (...)
ON CONFLICT (osm_type, osm_id) DO UPDATE SET
  ingested_at = EXCLUDED.ingested_at,
  source_revision = EXCLUDED.source_revision,
  tags = EXCLUDED.tags;
```

`DO UPDATE` over `DO NOTHING` lets us refresh tags and timestamps on re-ingest without losing the row history.

