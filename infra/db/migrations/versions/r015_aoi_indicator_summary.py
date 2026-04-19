"""aoi_indicator_summary — cached AOI-level aggregated indicator values.

Layer 1 ships the schema, two partial unique indexes for NULL-safe
scenario scoping, and a minimal serving view. The worker that populates
it joins aoi_feature to indicator_result, which lands with Layer 2.

Revision ID: r015
Revises: r014
Create Date: 2026-04-19
"""
from __future__ import annotations

from alembic import op

revision = "r015"
down_revision = "r014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE curated_geo.aoi_indicator_summary (
            id                uuid             PRIMARY KEY DEFAULT gen_random_uuid(),
            aoi_id            uuid             NOT NULL
                                               REFERENCES curated_geo.area_of_interest(id)
                                               ON DELETE CASCADE,
            scenario_id       uuid,
            -- indicator_id has no FK yet; curated_geo.indicator_catalog lands
            -- with the Layer 2 indicators milestone. The FK will be added then.
            indicator_id      uuid             NOT NULL,
            value             double precision,
            aggregation_note  text,
            status            text             NOT NULL DEFAULT 'current'
                                               CHECK (status IN ('current', 'stale', 'missing')),
            computed_at       timestamptz      NOT NULL DEFAULT now()
        )
    """)

    # Two partial unique indexes — required in Postgres because a single
    # UNIQUE (aoi_id, scenario_id, indicator_id) would treat each NULL
    # scenario as distinct, permitting duplicate base-city rows.
    op.execute("""
        CREATE UNIQUE INDEX aoi_indicator_summary_base_uk
          ON curated_geo.aoi_indicator_summary (aoi_id, indicator_id)
          WHERE scenario_id IS NULL
    """)
    op.execute("""
        CREATE UNIQUE INDEX aoi_indicator_summary_scenario_uk
          ON curated_geo.aoi_indicator_summary (aoi_id, scenario_id, indicator_id)
          WHERE scenario_id IS NOT NULL
    """)

    op.execute("""
        CREATE OR REPLACE VIEW serving_geo.aoi_summary_v AS
        SELECT
            s.id,
            s.aoi_id,
            s.scenario_id,
            s.indicator_id,
            s.value,
            s.aggregation_note,
            s.status,
            s.computed_at
        FROM curated_geo.aoi_indicator_summary s
        WHERE s.status = 'current'
    """)

    # Register the new layers in the engineer-facing catalog.
    op.execute("""
        CREATE OR REPLACE VIEW explore.layer_catalog_v AS
        SELECT 'curated_geo'::text AS schema_name, 'data_source_catalog'::text AS object_name, 'source_registry'::text AS logical_name,
               'canonical'::text AS layer_kind, 'Registered source datasets and versions'::text AS description,
               NULL::text AS primary_geometry_column, 'Inspect available datasets and storage metadata'::text AS intended_use
        UNION ALL
        SELECT 'curated_geo', 'ingestion_run', 'ingestion_runs', 'canonical',
               'Lineage and status for source loads', NULL, 'Inspect pipeline runs and failure details'
        UNION ALL
        SELECT 'curated_geo', 'feature', 'canonical_features', 'canonical',
               'Stable canonical spatial entities', 'geometry', 'Debug source-of-truth feature storage'
        UNION ALL
        SELECT 'curated_geo', 'observation', 'feature_observations', 'canonical',
               'Measured and derived values attached to features', NULL, 'Inspect current and historical enrichments'
        UNION ALL
        SELECT 'curated_geo', 'aoi_indicator_summary', 'aoi_summaries', 'canonical',
               'Cached AOI-level aggregated indicator values', NULL, 'Inspect cached summary rows by AOI'
        UNION ALL
        SELECT 'serving_geo', 'feature_v', 'serving_features', 'serving',
               'App-facing merged baseline features', 'geometry', 'Inspect what API and workers read'
        UNION ALL
        SELECT 'serving_geo', 'aoi_summary_v', 'aoi_summaries_serving', 'serving',
               'App-facing AOI-level summaries (status = current only)', NULL, 'Inspect the read contract for the AOI summary cache'
        UNION ALL
        SELECT 'explore', 'layer_catalog_v', 'layer_catalog', 'explore',
               'Curated registry of important database layers', NULL, 'Use as the entrypoint for database exploration'
        UNION ALL
        SELECT 'explore', 'source_runs_v', 'source_runs', 'explore',
               'Flattened source registry joined with ingestion runs', NULL, 'Inspect source lineage quickly'
        UNION ALL
        SELECT 'explore', 'feature_type_stats_v', 'feature_type_stats', 'explore',
               'Counts of canonical features by type, role, source, and version', NULL, 'Inspect loaded feature volumes'
        UNION ALL
        SELECT 'explore', 'serving_stats_v', 'serving_stats', 'explore',
               'High-level counts over the serving layer', NULL, 'Verify what the app-facing layer exposes'
    """)


def downgrade() -> None:
    # Restore the pre-r015 explore catalog (without the two new rows).
    op.execute("""
        CREATE OR REPLACE VIEW explore.layer_catalog_v AS
        SELECT 'curated_geo'::text AS schema_name, 'data_source_catalog'::text AS object_name, 'source_registry'::text AS logical_name,
               'canonical'::text AS layer_kind, 'Registered source datasets and versions'::text AS description,
               NULL::text AS primary_geometry_column, 'Inspect available datasets and storage metadata'::text AS intended_use
        UNION ALL
        SELECT 'curated_geo', 'ingestion_run', 'ingestion_runs', 'canonical',
               'Lineage and status for source loads', NULL, 'Inspect pipeline runs and failure details'
        UNION ALL
        SELECT 'curated_geo', 'feature', 'canonical_features', 'canonical',
               'Stable canonical spatial entities', 'geometry', 'Debug source-of-truth feature storage'
        UNION ALL
        SELECT 'curated_geo', 'observation', 'feature_observations', 'canonical',
               'Measured and derived values attached to features', NULL, 'Inspect current and historical enrichments'
        UNION ALL
        SELECT 'serving_geo', 'feature_v', 'serving_features', 'serving',
               'App-facing merged baseline features', 'geometry', 'Inspect what API and workers read'
        UNION ALL
        SELECT 'explore', 'layer_catalog_v', 'layer_catalog', 'explore',
               'Curated registry of important database layers', NULL, 'Use as the entrypoint for database exploration'
        UNION ALL
        SELECT 'explore', 'source_runs_v', 'source_runs', 'explore',
               'Flattened source registry joined with ingestion runs', NULL, 'Inspect source lineage quickly'
        UNION ALL
        SELECT 'explore', 'feature_type_stats_v', 'feature_type_stats', 'explore',
               'Counts of canonical features by type, role, source, and version', NULL, 'Inspect loaded feature volumes'
        UNION ALL
        SELECT 'explore', 'serving_stats_v', 'serving_stats', 'explore',
               'High-level counts over the serving layer', NULL, 'Verify what the app-facing layer exposes'
    """)
    op.execute("DROP VIEW IF EXISTS serving_geo.aoi_summary_v")
    op.execute("DROP TABLE IF EXISTS curated_geo.aoi_indicator_summary")
