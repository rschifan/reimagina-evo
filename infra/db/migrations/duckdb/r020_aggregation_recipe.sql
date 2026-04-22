-- r020: aggregation_recipe registry + serving/explore views.
--
-- Paired with infra/db/migrations/versions/r020_aggregation_recipe.py.
-- DuckDB supports inline REFERENCES and CHECK constraints so the
-- shape is near-identical to Postgres. The enabled/input-raster
-- indexes are full (no partial-index support) — cheap at prototype
-- scale.

CREATE TABLE IF NOT EXISTS curated_geo.aggregation_recipe (
    id                          UUID        PRIMARY KEY DEFAULT uuid(),
    input_kind                  VARCHAR     NOT NULL
                                            CHECK (input_kind IN ('raster', 'observation')),
    input_raster_layer_id       UUID        REFERENCES curated_geo.raster_layer(id),
    input_attribute_key_prefix  VARCHAR,
    input_feature_type_slug     VARCHAR,
    target_feature_type_slug    VARCHAR     NOT NULL,
    attribute_key_prefix        VARCHAR     NOT NULL,
    method                      VARCHAR     NOT NULL
                                            CHECK (method IN ('mean','weighted_mean','sum','min','max')),
    weight_basis                VARCHAR     CHECK (weight_basis IN ('area','length','presence')),
    params                      VARCHAR     NOT NULL DEFAULT '{}',
    coverage_threshold          DOUBLE      NOT NULL DEFAULT 0.5
                                            CHECK (coverage_threshold >= 0 AND coverage_threshold <= 1),
    aoi_id                      UUID        REFERENCES curated_geo.area_of_interest(id),
    enabled                     BOOLEAN     NOT NULL DEFAULT true,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (
        (input_kind = 'raster'
            AND input_raster_layer_id IS NOT NULL
            AND input_attribute_key_prefix IS NULL
            AND input_feature_type_slug IS NULL)
        OR
        (input_kind = 'observation'
            AND input_raster_layer_id IS NULL
            AND input_attribute_key_prefix IS NOT NULL
            AND input_feature_type_slug IS NOT NULL)
    ),
    UNIQUE (target_feature_type_slug, attribute_key_prefix)
);

CREATE INDEX IF NOT EXISTS aggregation_recipe_enabled_idx
    ON curated_geo.aggregation_recipe (enabled);
CREATE INDEX IF NOT EXISTS aggregation_recipe_input_raster_idx
    ON curated_geo.aggregation_recipe (input_raster_layer_id);

CREATE OR REPLACE VIEW serving_geo.aggregation_recipe_v AS
SELECT
    CAST(r.id AS VARCHAR)                      AS id,
    r.input_kind,
    CAST(r.input_raster_layer_id AS VARCHAR)   AS input_raster_layer_id,
    rl.layer_slug                              AS input_raster_layer_slug,
    r.input_attribute_key_prefix,
    r.input_feature_type_slug,
    r.target_feature_type_slug,
    r.attribute_key_prefix,
    r.method,
    r.weight_basis,
    r.params,
    r.coverage_threshold,
    CAST(r.aoi_id AS VARCHAR)                  AS aoi_id,
    r.enabled,
    r.created_at,
    r.updated_at
FROM curated_geo.aggregation_recipe r
LEFT JOIN curated_geo.raster_layer rl ON rl.id = r.input_raster_layer_id;

CREATE OR REPLACE VIEW explore.aggregation_recipe_v AS
SELECT
    CAST(r.id AS VARCHAR)                      AS id,
    r.input_kind,
    rl.layer_slug                              AS input_raster_layer_slug,
    r.input_attribute_key_prefix,
    r.input_feature_type_slug,
    r.target_feature_type_slug,
    r.attribute_key_prefix,
    r.method,
    r.weight_basis,
    r.coverage_threshold,
    r.enabled,
    r.created_at
FROM curated_geo.aggregation_recipe r
LEFT JOIN curated_geo.raster_layer rl ON rl.id = r.input_raster_layer_id;

INSERT OR IGNORE INTO explore.layer_catalog
    (schema_name, object_name, logical_name, layer_kind, description,
     primary_geometry_column, intended_use, display_order)
VALUES
    ('curated_geo', 'aggregation_recipe', 'aggregation_recipes', 'canonical',
     'Declarative raster→observation and observation→observation derivation bindings',
     NULL,
     'Inspect what recipes produce which observation prefixes at which grain', 56),
    ('serving_geo', 'aggregation_recipe_v', 'aggregation_recipes_serving', 'serving',
     'App/worker-facing view of enabled aggregation recipes (hides disabled rows)',
     NULL,
     'Inspect what the derivation dispatcher sees', 116),
    ('explore', 'aggregation_recipe_v', 'aggregation_recipe_catalog', 'explore',
     'Engineer view: recipes joined with raster layer slugs + feature type labels',
     NULL,
     'Audit recipes + debug dispatcher input resolution', 945);
