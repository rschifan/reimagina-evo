"""Add a precomputed centroid column + GiST index on curated_geo.feature.

The sub-AOI selection panel calls
``serving_reader.count_features_within_polygon`` on every drag-pause
to refresh the per-layer count. The current SQL uses
``ST_Intersects(geometry, polygon)`` which the planner serves via the
existing ``feature_geometry_gist`` index — fast bbox prefilter but
the ST_Intersects refilter still touches the actual polygon for each
of ~280 k candidates inside a CDMX-scale circle, costing ~465 ms per
query.

A centroid-based ``ST_Contains(polygon, centroid)`` query is 5-10×
faster at this scale because (a) the GiST index on a Point column
filters down to exactly the rows we need (no refilter pass), and (b)
the per-row test is a cheap point-in-polygon. The catch is that
``ST_PointOnSurface(geometry)`` recomputed inline negates the win —
the centroid must be precomputed.

PostgreSQL ``GENERATED ALWAYS AS ... STORED`` columns let us derive
the centroid at row insert/update time without trigger code. Same
semantics as a maintained column but managed by the engine.

Revision ID: r027
Revises: r026
Create Date: 2026-04-28
"""

from __future__ import annotations

from alembic import op


revision = "r027"
down_revision = "r026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Generated-column add. ``ST_PointOnSurface`` (rather than
    # ``ST_Centroid``) guarantees the point lies INSIDE the polygon
    # even for non-convex shapes — important for L-shaped manzanas
    # whose vertex/area centroid can fall outside the polygon and
    # mis-classify when the selection skirts the boundary.
    #
    # Note: GENERATED ALWAYS AS STORED rewrites the table to populate
    # the new column. On the prototype's ~2 M-row feature table this
    # takes ~30-90 s. Acceptable for a one-time deploy.
    op.execute(
        """
        ALTER TABLE curated_geo.feature
            ADD COLUMN IF NOT EXISTS geometry_centroid
                geometry(Point, 4326)
                GENERATED ALWAYS AS (ST_PointOnSurface(geometry)) STORED
        """
    )

    # GiST index on the new column. Backs the spotlight-selection
    # ``ST_Contains(polygon, centroid)`` query path — see
    # ``PostgresServingReader.count_features_within_polygon``.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS feature_centroid_gist
            ON curated_geo.feature
            USING GIST (geometry_centroid)
        """
    )

    # Expose the centroid through the serving views so the API can
    # read it via the existing ``feature_aoi_v`` contract. Two-step
    # CREATE OR REPLACE: first ``feature_v`` (re-issued whole because
    # adding a column requires a redefine), then ``feature_aoi_v``
    # which selects from ``feature_v``. Postgres rejects column adds
    # to a view in one shot, so the views are recreated as wholes.
    # CREATE OR REPLACE VIEW only permits appending new columns at the
    # END of the column list (Postgres restriction). New
    # ``geometry_centroid`` columns therefore appended last in both
    # views — column ordering is informational only; the API addresses
    # by name.
    op.execute(
        """
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
            f.updated_at,
            f.geometry_centroid
        FROM curated_geo.feature f
        LEFT JOIN serving_geo.feature_current_attributes_mv fca ON fca.feature_id = f.id
        """
    )

    op.execute(
        """
        CREATE OR REPLACE VIEW serving_geo.feature_aoi_v AS
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
            f.updated_at,
            f.geometry_centroid
        FROM serving_geo.feature_v f
        JOIN curated_geo.aoi_feature af ON af.feature_id = f.id
        """
    )


def downgrade() -> None:
    # Full reversibility: undo everything r027 added. ``feature_v``
    # and ``feature_aoi_v`` revert to their pre-r027 column lists;
    # the index and column drop. Postgres' ``CREATE OR REPLACE VIEW``
    # only permits APPENDING columns, never removing — so the views
    # have to be DROPPED + recreated. Dropping ``feature_v`` cascades
    # through every dependent view (roads, sidewalks, trees,
    # bus_stops, blocks, lots, municipalities, analysis_units_v,
    # infrastructure_v, pois_v, detections_v, admin_v, and
    # explore.serving_stats_v), so the downgrade has to recreate all
    # of them too. Definitions are copied from r014 (feature_v),
    # r005 (feature_aoi_v), r008 (the role / type_slug views), and
    # r014 (serving_stats_v).
    op.execute("DROP INDEX IF EXISTS curated_geo.feature_centroid_gist")
    op.execute("DROP VIEW IF EXISTS serving_geo.feature_v CASCADE")
    op.execute(
        """
        CREATE VIEW serving_geo.feature_v AS
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
        """
    )
    op.execute(
        """
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
        """
    )
    # Role-based views (canonical defs from r008).
    for role, view_name in (
        ("analysis_unit", "analysis_units_v"),
        ("infrastructure", "infrastructure_v"),
        ("poi", "pois_v"),
        ("detection", "detections_v"),
        ("context", "admin_v"),
    ):
        op.execute(
            f"CREATE VIEW serving_geo.{view_name} AS "
            f"SELECT * FROM serving_geo.feature_v WHERE role = '{role}'"
        )
    # type_slug-based views (canonical defs from r008).
    for slug, view_name in (
        ("block", "blocks"),
        ("lot", "lots"),
        ("municipality", "municipalities"),
        ("road", "roads"),
        ("sidewalk", "sidewalks"),
        ("tree", "trees"),
        ("bus_stop", "bus_stops"),
    ):
        op.execute(
            f"CREATE VIEW serving_geo.{view_name} AS "
            f"SELECT * FROM serving_geo.feature_v WHERE type_slug = '{slug}'"
        )
    # explore.serving_stats_v (canonical def from r014).
    op.execute(
        """
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
        """
    )
    op.execute(
        """
        ALTER TABLE curated_geo.feature DROP COLUMN IF EXISTS geometry_centroid
        """
    )
