"""aggregation_recipe registry + serving/explore views.

Introduces ``curated_geo.aggregation_recipe`` — the declarative binding
that says "one input (raster or observation) → observations at one
feature grain via one aggregation method". Lets the CLI and
post-ingest hooks materialise observations without baking feature_type
slugs and statistic lists into ad-hoc shell commands.

Scope:

- One table, one `input_kind` CHECK (``raster`` | ``observation``),
  one XOR check so exactly one of ``input_raster_layer_id`` /
  (``input_attribute_key_prefix`` + ``input_feature_type_slug``) is set.
- ``method`` reuses the existing ``indicator_catalog.aggregation_method``
  vocabulary verbatim (``mean``, ``weighted_mean``, ``sum``, ``min``,
  ``max``, ``none``). No new enum.
- ``weight_basis`` reuses ``feature_overlap.overlap_basis`` vocabulary.
- Paired read surfaces:
  * ``serving_geo.aggregation_recipe_v`` — consumed by the dispatcher
    and by CLI `derivation list` output; workers read only from here.
  * ``explore.aggregation_recipe_v`` — engineer inspection, joins in
    raster layer slugs + feature type labels for readability.

Revision ID: r020
Revises: r019
Create Date: 2026-04-21
"""
from __future__ import annotations

from alembic import op


revision = "r020"
down_revision = "r019"
branch_labels = None
depends_on = None


_NEW_CATALOG_ROWS = [
    # (schema, object, logical_name, layer_kind, description,
    #  primary_geometry_column, intended_use, display_order)
    ("curated_geo", "aggregation_recipe", "aggregation_recipes", "canonical",
     "Declarative raster→observation and observation→observation derivation bindings",
     None,
     "Inspect what recipes produce which observation prefixes at which grain", 56),
    ("serving_geo", "aggregation_recipe_v", "aggregation_recipes_serving", "serving",
     "App/worker-facing view of enabled aggregation recipes (hides disabled rows)",
     None,
     "Inspect what the derivation dispatcher sees", 116),
    ("explore", "aggregation_recipe_v", "aggregation_recipe_catalog", "explore",
     "Engineer view: recipes joined with raster layer slugs + feature type labels",
     None,
     "Audit recipes + debug dispatcher input resolution", 945),
]


def upgrade() -> None:
    op.execute("""
        CREATE TABLE curated_geo.aggregation_recipe (
            id                          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
            input_kind                  text        NOT NULL
                                                    CHECK (input_kind IN ('raster', 'observation')),
            input_raster_layer_id       uuid        REFERENCES curated_geo.raster_layer(id) ON DELETE CASCADE,
            input_attribute_key_prefix  text,
            input_feature_type_slug     text,
            target_feature_type_slug    text        NOT NULL,
            attribute_key_prefix        text        NOT NULL,
            method                      text        NOT NULL
                                                    CHECK (method IN ('mean','weighted_mean','sum','min','max')),
            weight_basis                text        CHECK (weight_basis IN ('area','length','presence')),
            params                      jsonb       NOT NULL DEFAULT '{}'::jsonb,
            coverage_threshold          double precision NOT NULL DEFAULT 0.5
                                                    CHECK (coverage_threshold >= 0 AND coverage_threshold <= 1),
            aoi_id                      uuid        REFERENCES curated_geo.area_of_interest(id) ON DELETE SET NULL,
            enabled                     boolean     NOT NULL DEFAULT true,
            created_at                  timestamptz NOT NULL DEFAULT now(),
            updated_at                  timestamptz NOT NULL DEFAULT now(),

            -- Exactly one input shape: raster XOR observation.
            CONSTRAINT aggregation_recipe_input_shape CHECK (
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

            -- Prefix uniqueness per target grain protects the retire-by-prefix
            -- contract: two recipes writing the same prefix at the same grain
            -- would clobber each other on replay. Uniqueness across recipes
            -- (regardless of input) is intentional — retire_current_by_prefix
            -- is cross-source by design.
            CONSTRAINT aggregation_recipe_prefix_uk UNIQUE
                (target_feature_type_slug, attribute_key_prefix)
        )
    """)

    op.execute("""
        CREATE INDEX aggregation_recipe_enabled_idx
            ON curated_geo.aggregation_recipe (enabled)
            WHERE enabled
    """)
    op.execute("""
        CREATE INDEX aggregation_recipe_input_raster_idx
            ON curated_geo.aggregation_recipe (input_raster_layer_id)
            WHERE input_raster_layer_id IS NOT NULL
    """)

    # Serving view — workers + CLI read here. Mirrors the raster_layer_v
    # pattern: join in related rows operators care about but don't invent
    # new columns.
    op.execute("""
        CREATE OR REPLACE VIEW serving_geo.aggregation_recipe_v AS
        SELECT
            r.id,
            r.input_kind,
            r.input_raster_layer_id,
            rl.layer_slug                          AS input_raster_layer_slug,
            r.input_attribute_key_prefix,
            r.input_feature_type_slug,
            r.target_feature_type_slug,
            r.attribute_key_prefix,
            r.method,
            r.weight_basis,
            r.params,
            r.coverage_threshold,
            r.aoi_id,
            r.enabled,
            r.created_at,
            r.updated_at
        FROM curated_geo.aggregation_recipe r
        LEFT JOIN curated_geo.raster_layer rl ON rl.id = r.input_raster_layer_id
    """)

    # Explore view — engineer inspection. Add feature-type labels so the
    # slug-to-label mapping is visible without a second query.
    op.execute("""
        CREATE OR REPLACE VIEW explore.aggregation_recipe_v AS
        SELECT
            r.id::text                             AS id,
            r.input_kind,
            rl.layer_slug                          AS input_raster_layer_slug,
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
        LEFT JOIN curated_geo.raster_layer rl ON rl.id = r.input_raster_layer_id
    """)

    for row in _NEW_CATALOG_ROWS:
        op.execute(
            "INSERT INTO explore.layer_catalog "
            "(schema_name, object_name, logical_name, layer_kind, description, "
            "primary_geometry_column, intended_use, display_order) VALUES "
            f"('{row[0]}', '{row[1]}', '{row[2]}', '{row[3]}', '{row[4]}', "
            f"{_sql_null_or(row[5])}, '{row[6]}', {row[7]})"
        )


def _sql_null_or(value: str | None) -> str:
    return "NULL" if value is None else "'" + value.replace("'", "''") + "'"


def downgrade() -> None:
    for row in _NEW_CATALOG_ROWS:
        op.execute(
            f"DELETE FROM explore.layer_catalog "
            f"WHERE schema_name = '{row[0]}' AND object_name = '{row[1]}'"
        )
    op.execute("DROP VIEW IF EXISTS explore.aggregation_recipe_v")
    op.execute("DROP VIEW IF EXISTS serving_geo.aggregation_recipe_v")
    op.execute("DROP INDEX IF EXISTS curated_geo.aggregation_recipe_input_raster_idx")
    op.execute("DROP INDEX IF EXISTS curated_geo.aggregation_recipe_enabled_idx")
    op.execute("DROP TABLE IF EXISTS curated_geo.aggregation_recipe")
