"""serving_geo views — the API read contract over curated_geo.

Revision ID: r005
Revises: r004
Create Date: 2026-04-12
"""

from __future__ import annotations
from alembic import op

revision = "r005"
down_revision = "r004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Aggregates all current observations into one JSON object per feature
    op.execute("""
        CREATE VIEW serving_geo.feature_current_attributes_v AS
        SELECT
            f.id AS feature_id,
            COALESCE(
                jsonb_object_agg(
                    obs.attribute_key,
                    COALESCE(
                        to_jsonb(obs.value_numeric),
                        to_jsonb(obs.value_text),
                        obs.value_json
                    )
                ) FILTER (WHERE obs.id IS NOT NULL),
                '{}'::jsonb
            ) AS current_attributes
        FROM curated_geo.feature f
        LEFT JOIN curated_geo.observation obs
            ON obs.feature_id = f.id AND obs.is_current
        GROUP BY f.id
    """)

    # Full feature view: base attributes merged with current observations
    op.execute("""
        CREATE VIEW serving_geo.feature_v AS
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
        JOIN serving_geo.feature_current_attributes_v fca ON fca.feature_id = f.id
    """)

    op.execute("""
        CREATE VIEW serving_geo.feature_aoi_v AS
        SELECT
            af.aoi_id,
            af.overlap_basis,
            af.overlap_measure,
            af.feature_measure,
            af.overlap_ratio,
            f.id,
            f.type_id,
            f.type_slug,
            f.role,
            f.parent_id,
            f.name,
            f.geometry,
            f.attributes,
            f.source_id,
            f.source_record_ref,
            f.dataset_version,
            f.confidence,
            f.model_version,
            f.created_at,
            f.updated_at
        FROM serving_geo.feature_v f
        JOIN curated_geo.aoi_feature af ON af.feature_id = f.id
    """)

    # Role-based views — zero-maintenance: new types appear automatically
    op.execute("""
        CREATE VIEW serving_geo.analysis_units_v AS
            SELECT * FROM serving_geo.feature_v WHERE role = 'analysis_unit'
    """)
    op.execute("""
        CREATE VIEW serving_geo.infrastructure_v AS
            SELECT * FROM serving_geo.feature_v WHERE role = 'infrastructure'
    """)
    op.execute("""
        CREATE VIEW serving_geo.pois_v AS
            SELECT * FROM serving_geo.feature_v WHERE role = 'poi'
    """)
    op.execute("""
        CREATE VIEW serving_geo.detections_v AS
            SELECT * FROM serving_geo.feature_v WHERE role = 'detection'
    """)
    op.execute("""
        CREATE VIEW serving_geo.admin_v AS
            SELECT * FROM serving_geo.feature_v WHERE role = 'context'
    """)

    # Per-type precision views for the highest-frequency types
    op.execute("""
        CREATE VIEW serving_geo.blocks        AS SELECT * FROM serving_geo.feature_v WHERE type_slug = 'block'
    """)
    op.execute("""
        CREATE VIEW serving_geo.lots          AS SELECT * FROM serving_geo.feature_v WHERE type_slug = 'lot'
    """)
    op.execute("""
        CREATE VIEW serving_geo.municipalities AS SELECT * FROM serving_geo.feature_v WHERE type_slug = 'municipality'
    """)
    op.execute("""
        CREATE VIEW serving_geo.roads         AS SELECT * FROM serving_geo.feature_v WHERE type_slug = 'road'
    """)
    op.execute("""
        CREATE VIEW serving_geo.sidewalks     AS SELECT * FROM serving_geo.feature_v WHERE type_slug = 'sidewalk'
    """)
    op.execute("""
        CREATE VIEW serving_geo.trees         AS SELECT * FROM serving_geo.feature_v WHERE type_slug = 'tree'
    """)
    op.execute("""
        CREATE VIEW serving_geo.bus_stops     AS SELECT * FROM serving_geo.feature_v WHERE type_slug = 'bus_stop'
    """)


def downgrade() -> None:
    for view in [
        "serving_geo.bus_stops",
        "serving_geo.trees",
        "serving_geo.sidewalks",
        "serving_geo.roads",
        "serving_geo.municipalities",
        "serving_geo.lots",
        "serving_geo.blocks",
        "serving_geo.admin_v",
        "serving_geo.detections_v",
        "serving_geo.pois_v",
        "serving_geo.infrastructure_v",
        "serving_geo.analysis_units_v",
        "serving_geo.feature_aoi_v",
        "serving_geo.feature_v",
        "serving_geo.feature_current_attributes_v",
    ]:
        op.execute(f"DROP VIEW IF EXISTS {view} CASCADE")
