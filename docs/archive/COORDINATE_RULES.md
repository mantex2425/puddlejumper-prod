# PuddleJumper Canonical Coordinate & Time Rules

## THE GOLDEN RULE
Coordinates are ALWAYS expressed as (lat, lng) in that order in all Python code, 
SQL function arguments, and API payloads. No exceptions.

## CANONICAL DB FUNCTIONS — USE THESE EXCLUSIVELY

### Coordinate → H3
```sql
app_private.coords_to_h3(lat, lng, resolution DEFAULT 8) → text
```
**NEVER use:** `h3_latlng_to_cell(POINT(lng, lat), 8)` directly

### H3 → Coordinates  
```sql
app_private.h3_to_lat(h3text) → double precision  
app_private.h3_to_lng(h3text) → double precision
```
**NEVER use:** `ST_Y(h3_cell_to_latlng(...)::geometry)` directly

### Coordinates → PostGIS
```sql
app_private.coords_to_point(lat, lng) → geometry
app_private.coords_to_geography(lat, lng) → geography
```
**NEVER use:** `ST_SetSRID(ST_MakePoint(lng, lat), 4326)` directly

### Distance
```sql
app_private.distance_miles(lat1, lng1, lat2, lng2) → double precision
```
**NEVER use:** `ST_Distance(ST_MakePoint(lng, lat)...)` directly

### Time — existing rule
```sql
NOW() AT TIME ZONE 'America/Chicago'
```
**NEVER use:** hardcoded UTC offsets

## INTERNAL CONVENTIONS (handled by canonical functions)
These are implementation details — never write them directly:
- PostGIS ST_MakePoint takes (lng, lat) — handled by coords_to_point()
- h3_latlng_to_cell point takes (lng, lat) — handled by coords_to_h3()
- h3_cell_to_latlng returns point(lng, lat) — handled by h3_to_lat/lng()

## ENFORCEMENT
- Every new SQL query must use canonical functions
- Code review must reject any direct use of ST_MakePoint, h3_latlng_to_cell, 
  h3_cell_to_latlng with raw coordinates
- safe_h3() is now a wrapper around coords_to_h3() — use coords_to_h3() for new code
- If you find yourself thinking about coordinate order, STOP and use the canonical function

## WHY THIS EXISTS
The coordinate order bug (POINT(lat,lng) vs POINT(lng,lat)) caused:
- Every H3 index written before March 27, 2026 to be wrong (Africa/Antarctica)
- 12,937km Nail It errors (distance to antipodal point of Houston)
- Multiple backfills and data corruption events
- Hours of debugging across multiple sessions
