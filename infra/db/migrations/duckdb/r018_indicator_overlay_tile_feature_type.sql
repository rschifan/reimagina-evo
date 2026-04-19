-- r018: Postgres-only. DuckDB has no MVT emitter, so the
-- indicator_overlay_tile function has no DuckDB mirror.
--
-- The Postgres r018 migration adds an optional `feature_type` filter to
-- serving_geo.indicator_overlay_tile so multi-grain indicators don't render
-- overlapping polygons on the map. DuckDB side stays a no-op for parity
-- with r010 and r017.

SELECT 1 WHERE FALSE;
