"""Lower the zoom floor on overlay tile functions from z>=10 to z>=8.

The user expects the censo / aggregation choropleth to render at the
same zoom as the underlying baseline blocks. Block layer minzoom is
z=8 (see ``apps/web/src/layers.ts``); the overlay tile functions
hard-coded ``z >= 10`` for performance reasons that no longer apply
now that AOI filtering is in place — a z=8 tile of Mexicali (one
muni) returns ~19 k blocks rather than country-scale volume.

Adds an extra simplification step for the new z=8/z=9 tier so the
output stays compact: ``ST_Simplify(geometry, 0.0005)`` matches the
floor that ``serving_geo.get_tile`` (r010) uses for the same zoom.

Both ``indicator_overlay_tile`` and ``aggregation_overlay_tile``
get the patch.

Revision ID: r025
Revises: r024
Create Date: 2026-04-28
"""

from __future__ import annotations

from alembic import op


revision = "r025"
down_revision = "r024"
branch_labels = None
depends_on = None


_UUID_RE = (
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


_INDICATOR_OVERLAY_TILE_BODY = f"""
    CREATE OR REPLACE FUNCTION serving_geo.indicator_overlay_tile(
        z           integer,
        x           integer,
        y           integer,
        query_params json DEFAULT '{{}}'
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
                WHEN z >= 10 THEN ST_Simplify(f.geometry, 0.0001)
                ELSE              ST_Simplify(f.geometry, 0.0005)
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
                AND p.scenario_id_txt ~ '{_UUID_RE}'
                AND ir.scenario_id = p.scenario_id_txt::uuid)
          )
          AND (
            p.feature_type IS NULL
            OR ir.feature_type = p.feature_type
          )
          AND (
            p.aoi_id_txt IS NULL
            OR (
              p.aoi_id_txt ~ '{_UUID_RE}'
              AND EXISTS (
                SELECT 1
                FROM curated_geo.aoi_feature af
                WHERE af.feature_id = f.id
                  AND af.aoi_id = p.aoi_id_txt::uuid
              )
            )
          )
          AND f.geometry && ST_Transform(
            ST_TileEnvelope(z, x, y, margin => 64.0 / 4096),
            4326
          )
          AND z >= 8
      ) tile
      WHERE tile.geom IS NOT NULL
    $tile$
"""


_AGGREGATION_OVERLAY_TILE_BODY = f"""
    CREATE OR REPLACE FUNCTION serving_geo.aggregation_overlay_tile(
        z           integer,
        x           integer,
        y           integer,
        query_params json DEFAULT '{{}}'
    )
    RETURNS bytea
    LANGUAGE sql
    STABLE
    PARALLEL SAFE
    AS $tile$
      WITH params AS (
        SELECT
          NULLIF(query_params->>'attribute_key', '')      AS attribute_key,
          NULLIF(query_params->>'feature_type_slug', '')  AS feature_type_slug,
          NULLIF(query_params->>'aoi_id', '')             AS aoi_id_txt
      )
      SELECT ST_AsMVT(tile, 'aggregation_overlay', 4096, 'geom')
      FROM (
        SELECT
          ST_AsMVTGeom(
            ST_Transform(
              CASE
                WHEN z >= 14 THEN f.geometry
                WHEN z >= 12 THEN ST_Simplify(f.geometry, 0.00002)
                WHEN z >= 10 THEN ST_Simplify(f.geometry, 0.0001)
                ELSE              ST_Simplify(f.geometry, 0.0005)
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
          AND (
            p.aoi_id_txt IS NULL
            OR (
              p.aoi_id_txt ~ '{_UUID_RE}'
              AND EXISTS (
                SELECT 1
                FROM curated_geo.aoi_feature af
                WHERE af.feature_id = f.id
                  AND af.aoi_id = p.aoi_id_txt::uuid
              )
            )
          )
          AND f.geometry && ST_Transform(
            ST_TileEnvelope(z, x, y, margin => 64.0 / 4096),
            4326
          )
          AND z >= 8
      ) tile
      WHERE tile.geom IS NOT NULL
    $tile$
"""


# Verbatim copies of the r024 bodies — the downgrade path restores the
# z>=10 floor without dragging the simplification ladder along.
_R024_INDICATOR_BODY = _INDICATOR_OVERLAY_TILE_BODY.replace(
    "WHEN z >= 10 THEN ST_Simplify(f.geometry, 0.0001)\n                ELSE              ST_Simplify(f.geometry, 0.0005)",
    "ELSE              ST_Simplify(f.geometry, 0.0001)",
).replace("AND z >= 8", "AND z >= 10")

_R024_AGGREGATION_BODY = _AGGREGATION_OVERLAY_TILE_BODY.replace(
    "WHEN z >= 10 THEN ST_Simplify(f.geometry, 0.0001)\n                ELSE              ST_Simplify(f.geometry, 0.0005)",
    "ELSE              ST_Simplify(f.geometry, 0.0001)",
).replace("AND z >= 8", "AND z >= 10")


def upgrade() -> None:
    op.execute(_INDICATOR_OVERLAY_TILE_BODY)
    op.execute(_AGGREGATION_OVERLAY_TILE_BODY)


def downgrade() -> None:
    op.execute(_R024_INDICATOR_BODY)
    op.execute(_R024_AGGREGATION_BODY)
