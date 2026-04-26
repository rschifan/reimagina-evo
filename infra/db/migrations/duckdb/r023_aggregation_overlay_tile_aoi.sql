-- r023: aggregation_overlay_tile — add optional aoi_id param. Postgres-only.
--
-- Same as r022 for the aggregation overlay function. Catalog refresh only.
UPDATE explore.layer_catalog
SET intended_use =
  'Call as GET /aggregation_overlay_tile/z/x/y?attribute_key=…&feature_type_slug=…&aoi_id=… (aoi_id optional)'
WHERE schema_name = 'serving_geo'
  AND object_name = 'aggregation_overlay_tile';
