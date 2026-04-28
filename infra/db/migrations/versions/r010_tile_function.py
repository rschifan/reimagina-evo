"""serving_geo.get_tile — PostGIS MVT function for martin tile server.

Adds a partial GIST index for context-only (low-zoom) spatial scans and a
function source that martin exposes as GET /get_tile/{z}/{x}/{y}.

Design decisions:
- Queries curated_geo.feature + serving_geo.feature_current_attributes_mv
  directly (not through feature_v) to give the planner full GIST pushdown.
- Uses ST_Simplify (Douglas-Peucker) at low zoom levels; topology errors are
  not visible at tile scale and DPeucker is much faster than
  ST_SimplifyPreserveTopology.
- Joins feature_current_attributes_mv only when z >= 12; at lower zoom the
  join would add cost with no visible benefit (attributes are NULL anyway).
- Zoom-based detail filters prevent tile bloat at overview scales:
    z < 8  → only role='context' (municipalities, states)
    z < 12 → role IN ('context','analysis_unit') (blocks, AGEBs, ...)
    z >= 12 → all types
- Buffer = 64px / 4096 extent (1.5625 %) — industry standard to prevent
  edge-rendering artefacts at tile boundaries.

Requires: PostGIS >= 3.1 (ST_TileEnvelope with margin parameter).

Revision ID: r010
Revises: r009
Create Date: 2026-04-14
"""

from __future__ import annotations

from alembic import op

revision = "r010"
down_revision = "r009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Partial GIST for role='context' features — the only rows fetched at z < 8.
    # A small, quickly-scanned index instead of scanning the full geometry GIST.
    op.execute("""
        CREATE INDEX IF NOT EXISTS feature_context_geom_idx
        ON curated_geo.feature USING GIST (geometry)
        WHERE role = 'context'
    """)

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
              -- Transform + clip + quantise geometry to tile pixel coordinates.
              -- ST_Simplify is applied before ST_Transform; tolerance in degrees
              -- is chosen so the loss is sub-pixel at the target zoom level.
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
                4096,     -- tile extent in pixels
                64,       -- 64px buffer for edge artefacts
                true      -- clip geometry to tile + buffer
              ) AS geom,

              -- Stable identifier for feature querying on click
              CAST(f.id AS text) AS id,
              f.type_slug,
              f.role,
              f.name,

              -- Attribute payload: full merge at z>=14, base only at z>=12,
              -- nothing below z12 to keep tile size minimal.
              CASE
                WHEN z >= 14 THEN f.attributes || COALESCE(fca.current_attributes, '{}'::jsonb)
                WHEN z >= 12 THEN f.attributes
                ELSE NULL
              END AS attributes

            FROM
              curated_geo.feature f
              -- LEFT JOIN so features without any observations are still included
              -- in tiles (INNER JOIN would silently drop them until first ingestion).
              -- The AND z >= 12 condition lets the planner eliminate the MV lookup
              -- entirely at overview zoom where attributes are never returned.
              LEFT JOIN serving_geo.feature_current_attributes_mv fca
                ON fca.feature_id = f.id AND z >= 12

            WHERE
              -- Bounding-box filter — hits feature_geometry_gist (or the
              -- feature_context_geom_idx partial index at z < 8 when role is
              -- also filtered, via bitmap AND plan).
              f.geometry && ST_Transform(
                ST_TileEnvelope(z, x, y, margin => 64.0 / 4096),
                4326
              )

              -- Zoom-dependent detail level: coarse at overview, fine at street zoom.
              AND CASE
                    WHEN z < 8  THEN f.role = 'context'
                    WHEN z < 12 THEN f.role IN ('context', 'analysis_unit')
                    ELSE TRUE
                  END

              -- Optional client filters passed via query string
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

    op.execute("""
        COMMENT ON FUNCTION serving_geo.get_tile(integer, integer, integer, json) IS
        'Martin function source — returns a Mapbox Vector Tile for the requested z/x/y.
Optional query params: type_slug, role.
Geometry is simplified at z < 14 and attributes are omitted below z 12 to keep tiles small.'
    """)


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS serving_geo.get_tile(integer, integer, integer, json)"
    )
    # DROP INDEX takes just the index name, never a schema-qualified name.
    op.execute("DROP INDEX IF EXISTS feature_context_geom_idx")
