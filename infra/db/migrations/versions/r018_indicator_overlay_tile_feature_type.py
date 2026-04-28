"""indicator_overlay_tile — feature_type filter.

The r017 function joined every feature whose `indicator_slug` matched,
regardless of `feature_type`. For indicators that apply to more than
one grain (e.g. `urban_form_block_building_density` with
`applies_to = (block, ageb)`) that renders both grains into the same
tile — the AGEB polygons stack on top of the block polygons and visually
wash them out.

This migration replaces the function body to honour an optional
`feature_type` parameter in `query_params`. Omitting it preserves the
pre-r018 all-grains behaviour, so callers that didn't set it keep
working (including the existing integration tests that pass no grain).

Schema is unchanged; this is a function-body swap.

Revision ID: r018
Revises: r017
Create Date: 2026-04-20
"""

from __future__ import annotations

from alembic import op

revision = "r018"
down_revision = "r017"
branch_labels = None
depends_on = None


_R018_BODY = """
CREATE OR REPLACE FUNCTION serving_geo.indicator_overlay_tile(
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
      NULLIF(query_params->>'indicator_slug', '') AS indicator_slug,
      NULLIF(query_params->>'scenario_id',    '') AS scenario_id_txt,
      NULLIF(query_params->>'feature_type',   '') AS feature_type
  )
  SELECT ST_AsMVT(tile, 'indicator_overlay', 4096, 'geom')
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
      f.type_slug        AS type_slug,
      ir.value_numeric   AS value_numeric,
      ir.quality_flag    AS quality_flag
    FROM curated_geo.indicator_result ir
    JOIN curated_geo.feature f
      ON f.id = ir.feature_id
    JOIN params p ON TRUE
    WHERE
      p.indicator_slug IS NOT NULL
      AND ir.indicator_slug = p.indicator_slug
      AND ir.status = 'current'
      AND (
        (p.scenario_id_txt IS NULL AND ir.scenario_id IS NULL)
        OR (p.scenario_id_txt IS NOT NULL
            AND ir.scenario_id = p.scenario_id_txt::uuid)
      )
      -- New in r018: optional feature_type filter so choropleth overlays
      -- don't stack multi-grain rows (e.g. block + ageb) on the same tile.
      AND (
        p.feature_type IS NULL
        OR ir.feature_type = p.feature_type
      )
      AND f.geometry && ST_Transform(
        ST_TileEnvelope(z, x, y, margin => 64.0 / 4096),
        4326
      )
      AND z >= 10
  ) tile
  WHERE tile.geom IS NOT NULL
$tile$
"""


def upgrade() -> None:
    op.execute(_R018_BODY)
    op.execute(
        """
        COMMENT ON FUNCTION serving_geo.indicator_overlay_tile(integer, integer, integer, json) IS
        'Martin function source — MVT choropleth for one indicator. '
        'Required: indicator_slug. Optional: scenario_id, feature_type. '
        'Returns empty below z=10. feature_type filter added in r018 to '
        'stop multi-grain indicators rendering overlapping polygons.'
        """
    )


def downgrade() -> None:
    # Restore the r017 function body (no feature_type parameter).
    op.execute("""
        CREATE OR REPLACE FUNCTION serving_geo.indicator_overlay_tile(
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
              NULLIF(query_params->>'indicator_slug', '') AS indicator_slug,
              NULLIF(query_params->>'scenario_id', '')    AS scenario_id_txt
          )
          SELECT ST_AsMVT(tile, 'indicator_overlay', 4096, 'geom')
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
                4096, 64, true
              ) AS geom,
              CAST(f.id AS text) AS feature_id,
              f.type_slug        AS type_slug,
              ir.value_numeric   AS value_numeric,
              ir.quality_flag    AS quality_flag
            FROM curated_geo.indicator_result ir
            JOIN curated_geo.feature f
              ON f.id = ir.feature_id
            JOIN params p ON TRUE
            WHERE
              p.indicator_slug IS NOT NULL
              AND ir.indicator_slug = p.indicator_slug
              AND ir.status = 'current'
              AND (
                (p.scenario_id_txt IS NULL AND ir.scenario_id IS NULL)
                OR (p.scenario_id_txt IS NOT NULL
                    AND ir.scenario_id = p.scenario_id_txt::uuid)
              )
              AND f.geometry && ST_Transform(
                ST_TileEnvelope(z, x, y, margin => 64.0 / 4096),
                4326
              )
              AND z >= 10
          ) tile
          WHERE tile.geom IS NOT NULL
        $tile$
    """)
