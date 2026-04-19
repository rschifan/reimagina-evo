-- r017: Postgres-only. DuckDB has no MVT emitter, so the
-- indicator_overlay_tile PostGIS function has no DuckDB mirror.
--
-- Tile serving is a Postgres/Martin concern across the platform (see
-- r010_tile_function.sql for the precedent — also a no-op on DuckDB).
-- This file exists so the shared revision manifest stays symmetric;
-- running a DuckDB database forward through r017 is a deliberate no-op.

SELECT 1 WHERE FALSE;
