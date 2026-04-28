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


_NEW_CATALOG_ROWS = [
    # (schema_name, object_name, logical_name, layer_kind, description,
    #  primary_geometry_column, intended_use, display_order)
    (
        "curated_geo",
        "aoi_indicator_summary",
        "aoi_summaries",
        "canonical",
        "Cached AOI-level aggregated indicator values",
        None,
        "Inspect cached summary rows by AOI",
        50,
    ),
    (
        "serving_geo",
        "aoi_summary_v",
        "aoi_summaries_serving",
        "serving",
        "App-facing AOI-level summaries (status = current only)",
        None,
        "Inspect the read contract for the AOI summary cache",
        110,
    ),
]


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

    # Register the new read surfaces in the engineer-facing catalog.
    for row in _NEW_CATALOG_ROWS:
        op.execute(
            "INSERT INTO explore.layer_catalog "
            "(schema_name, object_name, logical_name, layer_kind, description, "
            "primary_geometry_column, intended_use, display_order) VALUES "
            f"('{row[0]}', '{row[1]}', '{row[2]}', '{row[3]}', '{row[4]}', "
            f"{_sql_null_or(row[5])}, '{row[6]}', {row[7]})"
        )


def downgrade() -> None:
    for row in _NEW_CATALOG_ROWS:
        op.execute(
            f"DELETE FROM explore.layer_catalog "
            f"WHERE schema_name = '{row[0]}' AND object_name = '{row[1]}'"
        )
    op.execute("DROP VIEW IF EXISTS serving_geo.aoi_summary_v")
    op.execute("DROP TABLE IF EXISTS curated_geo.aoi_indicator_summary")


def _sql_null_or(value: str | None) -> str:
    """Inline a text literal or SQL NULL for the catalog seeds."""
    return "NULL" if value is None else "'" + value.replace("'", "''") + "'"
