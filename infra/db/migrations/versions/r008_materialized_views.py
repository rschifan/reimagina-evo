"""Materialise feature_current_attributes_v to eliminate hot aggregation on every read.

The live view re-runs jsonb_object_agg over the full observation table on every
viewport query.  Replacing the JOIN target in feature_v with a materialised view
keeps serving reads fast while ingestion refreshes the snapshot after each run.

Revision ID: r008
Revises: r007
Create Date: 2026-04-14
"""
from __future__ import annotations

from alembic import op

revision = "r008"
down_revision = "r007"
branch_labels = None
depends_on = None

# All views that depend on feature_v (built in r005).  Must be recreated after
# dropping feature_v CASCADE.
_DEPENDENT_VIEWS = [
    ("serving_geo.feature_aoi_v", """
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
    """),
    ("serving_geo.analysis_units_v",
     "CREATE VIEW serving_geo.analysis_units_v AS SELECT * FROM serving_geo.feature_v WHERE role = 'analysis_unit'"),
    ("serving_geo.infrastructure_v",
     "CREATE VIEW serving_geo.infrastructure_v AS SELECT * FROM serving_geo.feature_v WHERE role = 'infrastructure'"),
    ("serving_geo.pois_v",
     "CREATE VIEW serving_geo.pois_v AS SELECT * FROM serving_geo.feature_v WHERE role = 'poi'"),
    ("serving_geo.detections_v",
     "CREATE VIEW serving_geo.detections_v AS SELECT * FROM serving_geo.feature_v WHERE role = 'detection'"),
    ("serving_geo.admin_v",
     "CREATE VIEW serving_geo.admin_v AS SELECT * FROM serving_geo.feature_v WHERE role = 'context'"),
    ("serving_geo.blocks",
     "CREATE VIEW serving_geo.blocks AS SELECT * FROM serving_geo.feature_v WHERE type_slug = 'block'"),
    ("serving_geo.lots",
     "CREATE VIEW serving_geo.lots AS SELECT * FROM serving_geo.feature_v WHERE type_slug = 'lot'"),
    ("serving_geo.municipalities",
     "CREATE VIEW serving_geo.municipalities AS SELECT * FROM serving_geo.feature_v WHERE type_slug = 'municipality'"),
    ("serving_geo.roads",
     "CREATE VIEW serving_geo.roads AS SELECT * FROM serving_geo.feature_v WHERE type_slug = 'road'"),
    ("serving_geo.sidewalks",
     "CREATE VIEW serving_geo.sidewalks AS SELECT * FROM serving_geo.feature_v WHERE type_slug = 'sidewalk'"),
    ("serving_geo.trees",
     "CREATE VIEW serving_geo.trees AS SELECT * FROM serving_geo.feature_v WHERE type_slug = 'tree'"),
    ("serving_geo.bus_stops",
     "CREATE VIEW serving_geo.bus_stops AS SELECT * FROM serving_geo.feature_v WHERE type_slug = 'bus_stop'"),
]

_FEATURE_V_MV = """
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
    JOIN serving_geo.feature_current_attributes_mv fca ON fca.feature_id = f.id
"""

_FEATURE_V_LIVE = """
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
"""


def upgrade() -> None:
    # 1. Create the materialised snapshot — same aggregation as the live view.
    op.execute("""
        CREATE MATERIALIZED VIEW serving_geo.feature_current_attributes_mv AS
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
        WITH DATA
    """)

    # 2. Unique index on feature_id — required for REFRESH CONCURRENTLY.
    op.execute("""
        CREATE UNIQUE INDEX feature_current_attributes_mv_feature_id_idx
        ON serving_geo.feature_current_attributes_mv (feature_id)
    """)

    # 3. Swap feature_v to read from the MV.  CASCADE drops all dependent views.
    op.execute("DROP VIEW serving_geo.feature_v CASCADE")
    op.execute(_FEATURE_V_MV)

    for _name, sql in _DEPENDENT_VIEWS:
        op.execute(sql)


def downgrade() -> None:
    op.execute("DROP VIEW serving_geo.feature_v CASCADE")
    op.execute("DROP MATERIALIZED VIEW IF EXISTS serving_geo.feature_current_attributes_mv CASCADE")
    op.execute(_FEATURE_V_LIVE)
    for _name, sql in _DEPENDENT_VIEWS:
        op.execute(sql)
