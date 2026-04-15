"""Engineer-facing `explore` schema and read-only inspection views.

Revision ID: r006
Revises: r005
Create Date: 2026-04-13
"""
from __future__ import annotations

from alembic import op

revision = "r006"
down_revision = "r005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS explore")

    op.execute("""
        CREATE VIEW explore.layer_catalog_v AS
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

    op.execute("""
        CREATE VIEW explore.source_runs_v AS
        SELECT
            s.id::text AS source_id,
            s.name AS source_name,
            s.provider,
            s.storage_type,
            s.integration_role,
            s.version,
            s.storage_uri,
            r.id::text AS ingestion_run_id,
            r.status AS ingestion_status,
            r.transformation_step,
            r.orchestrator,
            r.orchestrator_run_id,
            r.started_at,
            r.completed_at,
            r.details
        FROM curated_geo.data_source_catalog s
        LEFT JOIN curated_geo.ingestion_run r
          ON r.source_id = s.id
    """)

    op.execute("""
        CREATE VIEW explore.feature_type_stats_v AS
        SELECT
            f.type_slug,
            f.role,
            f.source_id::text AS source_id,
            f.dataset_version,
            COUNT(*)::bigint AS row_count
        FROM curated_geo.feature f
        GROUP BY f.type_slug, f.role, f.source_id::text, f.dataset_version
    """)

    op.execute("""
        CREATE VIEW explore.serving_stats_v AS
        SELECT
            'all'::text AS stat_scope,
            NULL::text AS type_slug,
            NULL::text AS role,
            COUNT(*)::bigint AS row_count
        FROM serving_geo.feature_v
        UNION ALL
        SELECT
            'type_role'::text AS stat_scope,
            type_slug,
            role,
            COUNT(*)::bigint AS row_count
        FROM serving_geo.feature_v
        GROUP BY type_slug, role
    """)


def downgrade() -> None:
    for view in [
        "explore.serving_stats_v",
        "explore.feature_type_stats_v",
        "explore.source_runs_v",
        "explore.layer_catalog_v",
    ]:
        op.execute(f"DROP VIEW IF EXISTS {view} CASCADE")
    op.execute("DROP SCHEMA IF EXISTS explore CASCADE")
