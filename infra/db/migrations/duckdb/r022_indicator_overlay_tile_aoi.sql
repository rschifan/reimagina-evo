-- r022: indicator_overlay_tile — add optional aoi_id param. Postgres-only.
--
-- DuckDB has no ST_AsMVT emitter (r017 / r021 skipped DuckDB for the
-- same reason). Refresh the catalog row's intended_use so explore
-- inspections show the new param even though the function lives in
-- Postgres only.
UPDATE explore.layer_catalog
SET intended_use =
  'Call as GET /indicator_overlay_tile/z/x/y?indicator_slug=…&feature_type=…&aoi_id=… (feature_type + aoi_id optional)'
WHERE schema_name = 'serving_geo'
  AND object_name = 'indicator_overlay_tile';
