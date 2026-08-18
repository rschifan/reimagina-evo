"""Stop serving building footprints below street zoom.

``serving_geo.get_tile`` served ``type_slug='building'`` at every zoom. With
365,954 Overture footprints concentrated in one alcaldia (``reimagina_demo``,
Tlalpan) that produces tiles far past what a renderer will paint, at exactly the
zooms where an operator looks at a whole AOI:

    zoom   candidates in tile   MVT bytes
    10          244,879           1.25 MB
    11          242,317           1.31 MB
    12           35,033           2.59 MB
    13           14,212           1.02 MB
    14              421             31 KB

MapLibre's practical budget is ~500 KB per tile and several are in flight at
once, so the layer never finishes painting. The symptom is an empty map rather
than a slow one, which is why this reads as missing data.

**Two plausible cheaper fixes were measured and both failed.** Recording them
because each looks obviously right and is obviously wrong:

1. *Withhold ``f.attributes`` below z14.* Saved **362 bytes** of 2.59 MB.
   ``ST_AsMVT`` encodes properties as a per-tile key/value dictionary, so 35,033
   buildings drawn from a handful of distinct Overture attribute sets cost
   almost nothing however verbose the jsonb looks (mean 192 chars each). Column
   size does not predict MVT size; cardinality does. The one column that *is*
   unique per feature — ``id``, a 36-char uuid — accounts for **1.71 MB of the
   2.59 MB**, and it cannot be dropped without breaking hit-testing.

2. *Simplify harder below z14* (the 0.0001 tolerance used at z10-11). Tile falls
   to 124 KB, which looks like a win until you count what is in it: of 37,858
   candidate footprints only **1,826 survive** — ``ST_Simplify`` at ~10 m
   collapses a ~10 m building to a degenerate ring and ``ST_AsMVTGeom`` returns
   NULL, which ``WHERE tile.geom IS NOT NULL`` then filters away. The existing
   z10/z11 rungs were *already* doing this: their apparent cheapness was 95% of
   the layer being silently discarded, not compression.

So the honest position is that there is no encoding trick here. At z12 one pixel
covers ~36 m and a building is ~10 m; individual footprints are sub-pixel and
cannot be drawn legibly at any price. Serve them from z14, where a tile holds
hundreds of features instead of tens of thousands.

**AOI-scale building information has a layer already** — the
``urban_form_block_building_density`` choropleth (0-212 buildings/ha on Tlalpan),
which is what "where are the buildings across this AOI" should actually render.
This change makes that the only answer at overview zoom rather than one of two,
one of which never painted.

**Why the gate is on ``type_slug`` and not ``role``.** The obvious lever is the
existing ``z < 12 THEN role IN ('context','analysis_unit')`` rung. It cannot be
used: buildings are ``role = 'context'`` — the *same role as municipality*. Any
role-based rule that drops buildings at overview zoom also drops the municipality
outlines, the one layer that must survive there. Role is the wrong axis, and
adding a role to express a rendering budget would be a schema change.

Also tightens the ``feature_current_attributes_mv`` join from ``z >= 12`` to
``z >= 14``. ``fca`` is referenced only in the ``z >= 14`` branch of the CASE, so
the lookup at z12-13 was already discarded. LEFT JOIN, so no row-count effect.

⚠ ``apps/web/src/layers.ts`` still declares ``minzoom: 11`` for the building
layer, with a comment asserting simplification keeps tiles bounded. That comment
was measured against ``reimagina_app``, which spreads 2.05M buildings across CDMX
plus Mexicali — density per tile, not global count, sets tile weight. After this
migration a z11 request returns an empty tile rather than a 1.3 MB one, so the
client is merely wasteful, not broken; the client-side floor should follow to 14.

Revision ID: r038
Revises: r037
Create Date: 2026-08-18
"""

from __future__ import annotations

from alembic import op

revision = "r038"
down_revision = "r037"
branch_labels = None
depends_on = None


# One body, parameterised on the two lines that differ from r012, so upgrade and
# downgrade cannot drift. CREATE OR REPLACE means whichever ran last wins
# outright, and a hand-maintained downgrade copy is exactly where that goes bad.
_GET_TILE = """
CREATE OR REPLACE FUNCTION serving_geo.get_tile(
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
        WHEN z >= 14 THEN f.attributes || COALESCE(fca.current_attributes, '{{}}'::jsonb)
        WHEN z >= 12 THEN f.attributes
        ELSE NULL
      END AS attributes
    FROM
      curated_geo.feature f
      -- LEFT JOIN so features without observations are never silently
      -- dropped from tiles.
      LEFT JOIN serving_geo.feature_current_attributes_mv fca
        ON fca.feature_id = f.id AND z >= {mv_join_zoom}
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
      {building_zoom_floor}
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
"""


def upgrade() -> None:
    op.execute(
        _GET_TILE.format(
            # Sub-pixel below z14 -- see the module docstring for why neither
            # cheaper encoding fix works.
            building_zoom_floor="AND (z >= 14 OR f.type_slug <> 'building')",
            mv_join_zoom=14,
        )
    )


def downgrade() -> None:
    # Restores r012 exactly: no zoom floor on buildings, MV join from z12.
    op.execute(_GET_TILE.format(building_zoom_floor="", mv_join_zoom=12))
