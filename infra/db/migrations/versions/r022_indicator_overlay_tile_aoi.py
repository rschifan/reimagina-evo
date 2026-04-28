"""serving_geo.indicator_overlay_tile — add optional aoi_id filter.

Multi-site adoption (CDMX-Cuauhtémoc + Tlalpan + Mexicali) needs the
choropleth tile function to be able to scope results to one AOI's
features. Without this, opening the picker on Mexicali still returns
CDMX features in every tile because the function returns every feature
matching the indicator regardless of city.

Change: add an optional ``aoi_id`` query param. When provided, an
``EXISTS`` subquery against ``curated_geo.aoi_feature`` filters out
any feature that doesn't overlap that AOI. When omitted, behaviour is
identical to r017.

``EXISTS`` over JOIN to keep the planner from inflating cardinality
when a single feature belongs to multiple AOIs (which can happen for
features near alcaldía borders). The index ``aoi_feature_pkey`` on
``(aoi_id, feature_id)`` services the lookup.

Revision ID: r022
Revises: r021
Create Date: 2026-04-26
"""

from __future__ import annotations

from alembic import op


revision = "r022"
down_revision = "r021"
branch_labels = None
depends_on = None


_INDICATOR_OVERLAY_TILE_BODY = """
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
          NULLIF(query_params->>'scenario_id', '')    AS scenario_id_txt,
          NULLIF(query_params->>'feature_type', '')   AS feature_type,
          NULLIF(query_params->>'aoi_id', '')         AS aoi_id_txt
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
          CAST(f.id AS text)     AS feature_id,
          f.type_slug             AS type_slug,
          ir.value_numeric        AS value_numeric,
          ir.quality_flag         AS quality_flag
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
          -- r018: optional feature_type filter so multi-grain indicators
          -- don't stack overlapping polygons in the same tile.
          AND (
            p.feature_type IS NULL
            OR ir.feature_type = p.feature_type
          )
          -- r022: optional AOI scoping. EXISTS over JOIN to keep the
          -- planner from inflating cardinality when a feature belongs
          -- to multiple AOIs (border alcaldías do).
          AND (
            p.aoi_id_txt IS NULL
            OR EXISTS (
              SELECT 1
              FROM curated_geo.aoi_feature af
              WHERE af.feature_id = f.id
                AND af.aoi_id = p.aoi_id_txt::uuid
            )
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
    op.execute(_INDICATOR_OVERLAY_TILE_BODY)
    op.execute("""
        COMMENT ON FUNCTION serving_geo.indicator_overlay_tile(integer, integer, integer, json) IS
        'Martin function source — MVT choropleth for one indicator. '
        'Required: indicator_slug. '
        'Optional: scenario_id, feature_type, aoi_id '
        '(aoi_id filters features to that AOI''s membership). '
        'Returns empty below z=10.'
    """)
    op.execute("""
        UPDATE explore.layer_catalog
        SET intended_use =
          'Call as GET /indicator_overlay_tile/z/x/y?indicator_slug=…&feature_type=…&aoi_id=… '
          '(feature_type + aoi_id optional)'
        WHERE schema_name = 'serving_geo'
          AND object_name = 'indicator_overlay_tile'
    """)


_R018_FALLBACK_BODY = """
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
          NULLIF(query_params->>'scenario_id', '')    AS scenario_id_txt,
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
          CAST(f.id AS text)     AS feature_id,
          f.type_slug             AS type_slug,
          ir.value_numeric        AS value_numeric,
          ir.quality_flag         AS quality_flag
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


def downgrade() -> None:
    # Restore the r018 function body verbatim so a downgrade leaves the
    # function exactly as r018 created it (one revision below r022).
    op.execute(_R018_FALLBACK_BODY)
    op.execute("""
        COMMENT ON FUNCTION serving_geo.indicator_overlay_tile(integer, integer, integer, json) IS
        'Martin function source — MVT choropleth for one indicator. '
        'Required: indicator_slug. Optional: scenario_id, feature_type. '
        'Returns empty below z=10. feature_type filter added in r018 to '
        'stop multi-grain indicators rendering overlapping polygons.'
    """)
    op.execute("""
        UPDATE explore.layer_catalog
        SET intended_use =
          'Call as GET /indicator_overlay_tile/z/x/y?indicator_slug=…&feature_type=… (feature_type optional)'
        WHERE schema_name = 'serving_geo'
          AND object_name = 'indicator_overlay_tile'
    """)
