"""Fix serving views after the Postgres materialized-attributes rollout.

Revision ID: r014
Revises: r013
Create Date: 2026-04-15
"""

from __future__ import annotations

from alembic import op

revision = "r014"
down_revision = "r013"
branch_labels = None
depends_on = None


def _create_feature_v_left_join() -> None:
    op.execute("""
        CREATE OR REPLACE VIEW serving_geo.feature_v AS
        SELECT
            f.id,
            f.type_id,
            f.type_slug,
            f.role,
            f.parent_id,
            f.name,
            f.geometry,
            f.attributes || COALESCE(fca.current_attributes, '{}'::jsonb) AS attributes,
            f.source_id,
            f.source_record_ref,
            f.dataset_version,
            f.confidence,
            f.model_version,
            f.created_at,
            f.updated_at
        FROM curated_geo.feature f
        LEFT JOIN serving_geo.feature_current_attributes_mv fca ON fca.feature_id = f.id
    """)


def _create_serving_stats_view() -> None:
    op.execute("""
        CREATE OR REPLACE VIEW explore.serving_stats_v AS
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


def upgrade() -> None:
    _create_feature_v_left_join()
    _create_serving_stats_view()


def downgrade() -> None:
    op.execute("""
        CREATE OR REPLACE VIEW serving_geo.feature_v AS
        SELECT
            f.id,
            f.type_id,
            f.type_slug,
            f.role,
            f.parent_id,
            f.name,
            f.geometry,
            f.attributes || fca.current_attributes AS attributes,
            f.source_id,
            f.source_record_ref,
            f.dataset_version,
            f.confidence,
            f.model_version,
            f.created_at,
            f.updated_at
        FROM curated_geo.feature f
        JOIN serving_geo.feature_current_attributes_mv fca ON fca.feature_id = f.id
    """)
    _create_serving_stats_view()
