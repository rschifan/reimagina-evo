"""serving_geo.aggregation_overlay_tile — add optional aoi_id filter.

Same multi-site rationale as r022 — without an ``aoi_id`` filter the
aggregation overlays return every feature in the database matching
``attribute_key + feature_type_slug``, so picking Mexicali in the web
app still shows CDMX blocks.

Mirrors r022's structure exactly: optional ``aoi_id`` query param,
``EXISTS`` subquery against ``curated_geo.aoi_feature`` when provided,
unchanged behaviour when omitted.

Revision ID: r023
Revises: r022
Create Date: 2026-04-26
"""
from __future__ import annotations

from alembic import op


revision = "r023"
down_revision = "r022"
branch_labels = None
depends_on = None


_AGGREGATION_OVERLAY_TILE_BODY = """
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
    op.execute(_AGGREGATION_OVERLAY_TILE_BODY)
    op.execute("""
        COMMENT ON FUNCTION serving_geo.aggregation_overlay_tile(integer, integer, integer, json) IS
        'Martin function source — MVT choropleth for aggregation-recipe observations. '
        'Required: attribute_key, feature_type_slug. '
        'Optional: aoi_id (filters features to that AOI''s membership). '
        'Returns empty below z=10.'
    """)
    op.execute("""
        UPDATE explore.layer_catalog
        SET intended_use =
          'Call as GET /aggregation_overlay_tile/z/x/y?attribute_key=…&feature_type_slug=…&aoi_id=… (aoi_id optional)'
        WHERE schema_name = 'serving_geo'
          AND object_name = 'aggregation_overlay_tile'
    """)


_R021_FALLBACK_BODY = """
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
"""


def downgrade() -> None:
    op.execute(_R021_FALLBACK_BODY)
    op.execute("""
        COMMENT ON FUNCTION serving_geo.aggregation_overlay_tile(integer, integer, integer, json) IS
        'Martin function source — MVT choropleth for aggregation-recipe observations. '
        'Required query params: attribute_key, feature_type_slug. '
        'Returns empty below z=10 (choropleth misleading at city scale).'
    """)
    op.execute("""
        UPDATE explore.layer_catalog
        SET intended_use =
          'Call as GET /aggregation_overlay_tile/z/x/y?attribute_key=…&feature_type_slug=…'
        WHERE schema_name = 'serving_geo'
          AND object_name = 'aggregation_overlay_tile'
    """)
