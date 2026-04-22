"""serving_geo.aggregation_overlay_tile — live MVT choropleth for observations.

Renders aggregation-recipe outputs as map tiles so the web app can show
them alongside the raw raster and the indicator overlays.

Design mirrors r017's indicator_overlay_tile one-for-one:
- Same ``(z, x, y, query_params json)`` signature so Martin auto-publishes.
- Same simplification ladder + z>=10 guard: choropleth at city zoom is
  misleading, so below z=10 we return empty.
- Same MVT layer name convention: ``aggregation_overlay`` — stable
  ``source-layer`` value across every (raster, grain, method) triplet.

Required query params:
- ``attribute_key`` (e.g. ``pop_worldpop_2020_block_sum``) — the exact
  observation key to render. The recipe registry (r020) owns the prefix;
  the suffix comes from the method (``<prefix>_<method>``) or an explicit
  ``params['stats']`` override.
- ``feature_type_slug`` (e.g. ``block``, ``ageb``, ``municipality``) —
  gates the join so a multi-grain observation family doesn't overlap.

Why not reuse indicator_overlay_tile: observations aren't indicators —
they live in ``curated_geo.observation``, not ``curated_geo.indicator_result``,
and don't carry a scenario_id or a quality_flag column. Generalising the
existing function would need two JOIN branches keyed on source table.
A dedicated function keeps both queries narrow and self-documenting.

Revision ID: r021
Revises: r020
Create Date: 2026-04-21
"""
from __future__ import annotations

from alembic import op


revision = "r021"
down_revision = "r020"
branch_labels = None
depends_on = None


_NEW_CATALOG_ROWS = [
    ("serving_geo", "aggregation_overlay_tile", "aggregation_overlay_tiles", "serving",
     "Live MVT choropleth for aggregation-recipe observations — martin function source",
     None,
     "Call as GET /aggregation_overlay_tile/z/x/y?attribute_key=…&feature_type_slug=…",
     131),
]


def upgrade() -> None:
    op.execute("""
        CREATE OR REPLACE FUNCTION serving_geo.aggregation_overlay_tile(
            z           integer,
            x           integer,
            y           integer,
            query_params json DEFAULT '{}'
        )
        RETURNS bytea
        LANGUAGE sql
        STABLE
        PARALLEL SAFE
        AS $tile$
          WITH params AS (
            SELECT
              NULLIF(query_params->>'attribute_key', '')      AS attribute_key,
              NULLIF(query_params->>'feature_type_slug', '')  AS feature_type_slug
          )
          SELECT ST_AsMVT(tile, 'aggregation_overlay', 4096, 'geom')
          FROM (
            SELECT
              ST_AsMVTGeom(
                ST_Transform(
                  CASE
                    WHEN z >= 14 THEN f.geometry
                    WHEN z >= 12 THEN ST_Simplify(f.geometry, 0.00002)
                    ELSE              ST_Simplify(f.geometry, 0.0001)
                  END,
                  3857
                ),
                ST_TileEnvelope(z, x, y),
                4096,
                64,
                true
              ) AS geom,
              CAST(f.id AS text) AS feature_id,
              f.type_slug         AS type_slug,
              obs.attribute_key   AS attribute_key,
              obs.value_numeric   AS value_numeric
            FROM curated_geo.observation obs
            JOIN curated_geo.feature f
              ON f.id = obs.feature_id
            JOIN params p ON TRUE
            WHERE
              p.attribute_key IS NOT NULL
              AND p.feature_type_slug IS NOT NULL
              AND obs.is_current
              AND obs.attribute_key = p.attribute_key
              AND f.type_slug = p.feature_type_slug
              AND f.geometry && ST_Transform(
                ST_TileEnvelope(z, x, y, margin => 64.0 / 4096),
                4326
              )
              AND z >= 10
          ) tile
          WHERE tile.geom IS NOT NULL
        $tile$
    """)

    op.execute("""
        COMMENT ON FUNCTION serving_geo.aggregation_overlay_tile(integer, integer, integer, json) IS
        'Martin function source — MVT choropleth for aggregation-recipe observations. '
        'Required query params: attribute_key, feature_type_slug. '
        'Returns empty below z=10 (choropleth misleading at city scale).'
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
    op.execute(
        "DROP FUNCTION IF EXISTS serving_geo.aggregation_overlay_tile(integer, integer, integer, json)"
    )
