"""Post-review fixups: tile function LEFT JOIN, missing indexes, CHECK parity.

Changes:
  1. Redeploys serving_geo.get_tile with a LEFT JOIN on feature_current_attributes_mv
     (instead of INNER JOIN) so features with no observations appear in tiles, and
     with AND z>=12 on the join condition so the MV lookup is skipped at overview
     zoom where attributes are never returned.  This is idempotent via
     CREATE OR REPLACE FUNCTION.

  2. Adds feature_overlap_type_pair_target_idx — a B-tree index on
     feature_overlap_type_pair(target_type_id) for reverse lookups
     ("which source types overlap with this target?").

The DuckDB counterpart (r012_fixups.sql) adds:
  - RTREE spatial index on curated_geo.feature.geometry
  - CHECK constraints on curated_geo.feature_overlap to match Postgres schema
  - The same target_type_id index

Revision ID: r012
Revises: r011
Create Date: 2026-04-14
"""

from __future__ import annotations

from alembic import op

revision = "r012"
down_revision = "r011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. Redeploy get_tile with LEFT JOIN fix ──────────────────────────────
    op.execute("""
        CREATE OR REPLACE FUNCTION serving_geo.get_tile(
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
          SELECT ST_AsMVT(tile, 'features', 4096, 'geom')
          FROM (
            SELECT
              ST_AsMVTGeom(
                ST_Transform(
                  CASE
                    WHEN z >= 14 THEN f.geometry
                    WHEN z >= 12 THEN ST_Simplify(f.geometry, 0.00002)
                    WHEN z >= 10 THEN ST_Simplify(f.geometry, 0.0001)
                    WHEN z >= 8  THEN ST_Simplify(f.geometry, 0.0005)
                    ELSE              ST_Simplify(f.geometry, 0.002)
                  END,
                  3857
                ),
                ST_TileEnvelope(z, x, y),
                4096,
                64,
                true
              ) AS geom,
              CAST(f.id AS text) AS id,
              f.type_slug,
              f.role,
              f.name,
              CASE
                WHEN z >= 14 THEN f.attributes || COALESCE(fca.current_attributes, '{}'::jsonb)
                WHEN z >= 12 THEN f.attributes
                ELSE NULL
              END AS attributes
            FROM
              curated_geo.feature f
              -- LEFT JOIN so features without observations are never silently
              -- dropped from tiles.  AND z >= 12 lets the planner skip the MV
              -- lookup at overview zoom where attributes are never returned.
              LEFT JOIN serving_geo.feature_current_attributes_mv fca
                ON fca.feature_id = f.id AND z >= 12
            WHERE
              f.geometry && ST_Transform(
                ST_TileEnvelope(z, x, y, margin => 64.0 / 4096),
                4326
              )
              AND CASE
                    WHEN z < 8  THEN f.role = 'context'
                    WHEN z < 12 THEN f.role IN ('context', 'analysis_unit')
                    ELSE TRUE
                  END
              AND (
                (query_params->>'type_slug') IS NULL
                OR f.type_slug = (query_params->>'type_slug')
              )
              AND (
                (query_params->>'role') IS NULL
                OR f.role = (query_params->>'role')
              )
          ) tile
          WHERE tile.geom IS NOT NULL
        $tile$
    """)

    # ── 2. Index on feature_overlap_type_pair(target_type_id) ───────────────
    op.execute("""
        CREATE INDEX IF NOT EXISTS feature_overlap_type_pair_target_idx
        ON curated_geo.feature_overlap_type_pair (target_type_id)
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS feature_overlap_type_pair_target_idx")
    # Restore the original INNER JOIN version of the tile function.
    op.execute("""
        CREATE OR REPLACE FUNCTION serving_geo.get_tile(
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
          SELECT ST_AsMVT(tile, 'features', 4096, 'geom')
          FROM (
            SELECT
              ST_AsMVTGeom(
                ST_Transform(
                  CASE
                    WHEN z >= 14 THEN f.geometry
                    WHEN z >= 12 THEN ST_Simplify(f.geometry, 0.00002)
                    WHEN z >= 10 THEN ST_Simplify(f.geometry, 0.0001)
                    WHEN z >= 8  THEN ST_Simplify(f.geometry, 0.0005)
                    ELSE              ST_Simplify(f.geometry, 0.002)
                  END,
                  3857
                ),
                ST_TileEnvelope(z, x, y),
                4096,
                64,
                true
              ) AS geom,
              CAST(f.id AS text) AS id,
              f.type_slug,
              f.role,
              f.name,
              CASE
                WHEN z >= 14 THEN f.attributes || fca.current_attributes
                WHEN z >= 12 THEN f.attributes
                ELSE NULL
              END AS attributes
            FROM
              curated_geo.feature f
              JOIN serving_geo.feature_current_attributes_mv fca
                ON fca.feature_id = f.id
            WHERE
              f.geometry && ST_Transform(
                ST_TileEnvelope(z, x, y, margin => 64.0 / 4096),
                4326
              )
              AND CASE
                    WHEN z < 8  THEN f.role = 'context'
                    WHEN z < 12 THEN f.role IN ('context', 'analysis_unit')
                    ELSE TRUE
                  END
              AND (
                (query_params->>'type_slug') IS NULL
                OR f.type_slug = (query_params->>'type_slug')
              )
              AND (
                (query_params->>'role') IS NULL
                OR f.role = (query_params->>'role')
              )
          ) tile
          WHERE tile.geom IS NOT NULL
        $tile$
    """)
