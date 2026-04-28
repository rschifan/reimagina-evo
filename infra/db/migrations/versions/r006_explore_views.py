"""Engineer-facing `explore` schema and read-only inspection views.

`explore.layer_catalog` is a real TABLE (not a view-of-literals). The
`explore.layer_catalog_v` view selects from it ordered by display_order,
so every future migration that adds a read surface only has to INSERT
one row — no CREATE-OR-REPLACE of a growing UNION ALL.

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


def _insert_rows(rows: list[tuple]) -> str:
    """Build a single multi-row INSERT for the initial catalog seed."""
    values = ",\n    ".join(
        "({})".format(
            ", ".join(
                "NULL" if v is None else "'" + v.replace("'", "''") + "'"
                for v in row[:-1]
            )
            + f", {row[-1]}"
        )
        for row in rows
    )
    return f"""
        INSERT INTO explore.layer_catalog (
            schema_name, object_name, logical_name, layer_kind, description,
            primary_geometry_column, intended_use, display_order
        ) VALUES
    {values}
    """


_INITIAL_CATALOG_ROWS: list[tuple] = [
    # (schema_name, object_name, logical_name, layer_kind, description,
    #  primary_geometry_column, intended_use, display_order)
    (
        "curated_geo",
        "data_source_catalog",
        "source_registry",
        "canonical",
        "Registered source datasets and versions",
        None,
        "Inspect available datasets and storage metadata",
        10,
    ),
    (
        "curated_geo",
        "ingestion_run",
        "ingestion_runs",
        "canonical",
        "Lineage and status for source loads",
        None,
        "Inspect pipeline runs and failure details",
        20,
    ),
    (
        "curated_geo",
        "feature",
        "canonical_features",
        "canonical",
        "Stable canonical spatial entities",
        "geometry",
        "Debug source-of-truth feature storage",
        30,
    ),
    (
        "curated_geo",
        "observation",
        "feature_observations",
        "canonical",
        "Measured and derived values attached to features",
        None,
        "Inspect current and historical enrichments",
        40,
    ),
    (
        "serving_geo",
        "feature_v",
        "serving_features",
        "serving",
        "App-facing merged baseline features",
        "geometry",
        "Inspect what API and workers read",
        100,
    ),
    (
        "explore",
        "layer_catalog_v",
        "layer_catalog",
        "explore",
        "Curated registry of important database layers",
        None,
        "Use as the entrypoint for database exploration",
        900,
    ),
    (
        "explore",
        "source_runs_v",
        "source_runs",
        "explore",
        "Flattened source registry joined with ingestion runs",
        None,
        "Inspect source lineage quickly",
        910,
    ),
    (
        "explore",
        "feature_type_stats_v",
        "feature_type_stats",
        "explore",
        "Counts of canonical features by type, role, source, and version",
        None,
        "Inspect loaded feature volumes",
        920,
    ),
    (
        "explore",
        "serving_stats_v",
        "serving_stats",
        "explore",
        "High-level counts over the serving layer",
        None,
        "Verify what the app-facing layer exposes",
        930,
    ),
]


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS explore")

    op.execute("""
        CREATE TABLE explore.layer_catalog (
            schema_name              text    NOT NULL,
            object_name              text    NOT NULL,
            logical_name             text    NOT NULL,
            layer_kind               text    NOT NULL,
            description              text    NOT NULL,
            primary_geometry_column  text,
            intended_use             text    NOT NULL,
            display_order            integer NOT NULL DEFAULT 1000,
            PRIMARY KEY (schema_name, object_name)
        )
    """)
    op.execute(_insert_rows(_INITIAL_CATALOG_ROWS))
    op.execute("""
        CREATE VIEW explore.layer_catalog_v AS
        SELECT schema_name, object_name, logical_name, layer_kind,
               description, primary_geometry_column, intended_use
        FROM explore.layer_catalog
        ORDER BY display_order, schema_name, object_name
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
    op.execute("DROP TABLE IF EXISTS explore.layer_catalog CASCADE")
    op.execute("DROP SCHEMA IF EXISTS explore CASCADE")
