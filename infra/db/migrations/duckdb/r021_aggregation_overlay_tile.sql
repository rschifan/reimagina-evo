-- r021: aggregation_overlay_tile — Postgres-only.
--
-- DuckDB has no ST_AsMVT emitter (r017 skipped DuckDB for the same
-- reason). We only register the explore.layer_catalog row so inspection
-- commands see it; the function itself doesn't exist in DuckDB.
INSERT OR IGNORE INTO explore.layer_catalog
    (schema_name, object_name, logical_name, layer_kind, description,
     primary_geometry_column, intended_use, display_order)
VALUES
    ('serving_geo', 'aggregation_overlay_tile', 'aggregation_overlay_tiles', 'serving',
     'Live MVT choropleth for aggregation-recipe observations — martin function source (Postgres only)',
     NULL,
     'Call as GET /aggregation_overlay_tile/z/x/y?attribute_key=…&feature_type_slug=…',
     131);
