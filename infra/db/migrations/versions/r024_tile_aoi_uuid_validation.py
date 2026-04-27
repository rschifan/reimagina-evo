"""serving_geo tile functions — pre-validate aoi_id before ::uuid cast.

Codex review flagged that r022/r023 cast ``aoi_id`` straight to UUID
inside the WHERE clause. A malformed value (any string that isn't
exactly 36 chars in the canonical 8-4-4-4-12 form) raises a Postgres
error rather than returning an empty tile, so a fuzzed request to
Martin can produce a 5xx instead of an empty 200.

Fix: gate the EXISTS subquery on a regex check that the value matches
the canonical UUID form. A value that fails the regex skips the
subquery (treated as "no match") so the tile function returns the
unfiltered set — same shape as omitting aoi_id entirely. That keeps
the failure mode bounded and prevents server errors from malformed
client input.

Both r022 (indicator_overlay_tile) and r023 (aggregation_overlay_tile)
get the same patch.

Revision ID: r024
Revises: r023
Create Date: 2026-04-27
"""
from __future__ import annotations

from alembic import op


revision = "r024"
down_revision = "r023"
branch_labels = None
depends_on = None


# Canonical UUID format used by every aoi_id in the system. Pinning to
# the standard 8-4-4-4-12 hex shape avoids accidentally matching
# upstream-formatted variants (e.g. with curly braces) and gives
# Postgres a literal regex it can short-circuit cheaply.
_UUID_RE = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"


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
                AND p.scenario_id_txt ~ '{_UUID_RE}'
                AND ir.scenario_id = p.scenario_id_txt::uuid)
          )
          AND (
            p.feature_type IS NULL
            OR ir.feature_type = p.feature_type
          )
          -- r024: gate aoi_id ::uuid cast behind a UUID-shape regex so
          -- malformed input becomes "no match" instead of a 500.
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
          AND z >= 10
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
          AND z >= 10
      ) tile
      WHERE tile.geom IS NOT NULL
    $tile$
"""


def upgrade() -> None:
    op.execute(_INDICATOR_OVERLAY_TILE_BODY)
    op.execute(_AGGREGATION_OVERLAY_TILE_BODY)


# r023 fallback bodies — restored verbatim on downgrade.
_R023_INDICATOR_BODY = """
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
            4096, 64, true
          ) AS geom,
          CAST(f.id AS text) AS feature_id,
          f.type_slug         AS type_slug,
          ir.value_numeric    AS value_numeric,
          ir.quality_flag     AS quality_flag
        FROM curated_geo.indicator_result ir
        JOIN curated_geo.feature f ON f.id = ir.feature_id
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
          AND (
            p.aoi_id_txt IS NULL
            OR EXISTS (
              SELECT 1 FROM curated_geo.aoi_feature af
              WHERE af.feature_id = f.id AND af.aoi_id = p.aoi_id_txt::uuid
            )
          )
          AND f.geometry && ST_Transform(
            ST_TileEnvelope(z, x, y, margin => 64.0 / 4096), 4326
          )
          AND z >= 10
      ) tile
      WHERE tile.geom IS NOT NULL
    $tile$
"""


_R023_AGGREGATION_BODY = """
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
            4096, 64, true
          ) AS geom,
          CAST(f.id AS text) AS feature_id,
          f.type_slug         AS type_slug,
          obs.attribute_key   AS attribute_key,
          obs.value_numeric   AS value_numeric
        FROM curated_geo.observation obs
        JOIN curated_geo.feature f ON f.id = obs.feature_id
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
              SELECT 1 FROM curated_geo.aoi_feature af
              WHERE af.feature_id = f.id AND af.aoi_id = p.aoi_id_txt::uuid
            )
          )
          AND f.geometry && ST_Transform(
            ST_TileEnvelope(z, x, y, margin => 64.0 / 4096), 4326
          )
          AND z >= 10
      ) tile
      WHERE tile.geom IS NOT NULL
    $tile$
"""


def downgrade() -> None:
    op.execute(_R023_INDICATOR_BODY)
    op.execute(_R023_AGGREGATION_BODY)
