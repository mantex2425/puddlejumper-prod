-- 2026-06-18_roads_by_name.sql
-- Precomputed per-road-name merged geometry, for the single_road pickup
-- projection (bead_on_wire.project_single_road_pickup). A bare road name
-- geocodes to the road CENTROID (measured median 1.5km from the actual pickup,
-- 0% within 100m), so for single_road offers we project onto the named road at
-- pickup_miles from the driver. Doing that against the live 1M-edge
-- routing.houston_ways is 1.5-27s (unstable, blows the 3s decision budget);
-- against this precomputed table the projection benchmarks 3-196ms.
--
-- ONE merged, ST_Simplify'd (~11m) geometry per road name (avg ~5 vertices,
-- max ~727), GiST-indexed. Build ~60s, ~73.5k rows.
--
-- REFRESH: this is DERIVED from routing.houston_ways — re-run this whole script
-- whenever houston_ways is re-imported (rare, OSM road network). Owned by
-- postgres (run as -U postgres; app role atjb lacks the DDL). Idempotent
-- (DROP IF EXISTS). Already applied to prod 2026-06-18.

DROP TABLE IF EXISTS app_private.roads_by_name;

CREATE TABLE app_private.roads_by_name AS
  SELECT name,
         ST_SimplifyPreserveTopology(
           ST_LineMerge(ST_Collect(the_geom)), 0.0001) AS geom
  FROM routing.houston_ways
  WHERE name IS NOT NULL AND length_m < 20000 AND the_geom IS NOT NULL
  GROUP BY name;

CREATE INDEX idx_roads_by_name_geom ON app_private.roads_by_name USING GIST (geom);
CREATE INDEX idx_roads_by_name_name ON app_private.roads_by_name (name);
ANALYZE app_private.roads_by_name;
