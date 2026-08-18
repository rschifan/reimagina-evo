"""Honour ``aoi_id`` in ``serving_geo.get_tile``.

``get_tile`` filtered on ``type_slug`` and ``role`` only. Unknown query params
are not rejected, they are **ignored**, so ``?aoi_id=<uuid>`` silently returned
the unscoped tile — byte-identical, 440,655 bytes with or without it.

The consequence is a map that mixes scopes without saying so. In
``reimagina_demo`` the baseline blocks layer drew all 67,167 blocks across 16
alcaldias while the indicator overlay — which *does* honour ``aoi_id`` — covered
Tlalpan's 5,273. An operator reads 7.9% coverage as "the indicator only computed
for part of the city"; it is complete coverage of the AOI, framed by 62k blocks
that were never in scope.

That is the same failure shape as the empty ``aoi_feature`` table, from the
other side: there, scoped reads returned nothing while unscoped worked; here,
scoped reads return everything. Both present as an indicator problem.

The clause is lifted verbatim from ``indicator_overlay_tile`` (r022, uuid guard
added in r024) rather than rewritten, so the two tile functions cannot answer
the same ``aoi_id`` differently. Two properties of it are deliberate:

- **``EXISTS``, not a join.** A feature may belong to several AOIs; a join would
  emit it once per matching row and duplicate it in the tile. ``EXISTS`` keeps
  cardinality at one row per feature regardless.
- **A malformed uuid yields an empty tile, not an error.** A tile function
  returns ``bytea``; raising would surface to MapLibre as a broken tile rather
  than a message, and PostGIS tile endpoints have no error channel. Empty is
  also the fail-*closed* direction — the bug being fixed here is that an
  unrecognised param showed everything.

Omitting ``aoi_id`` stays unscoped, deliberately: the CDMX-wide view of
``urban_form_*`` (67,567 current block rows) is a legitimate thing to ask for,
and the parameter is how you narrow it, not how you enable it.

**No new index — but read why before concluding it generalises.** Measured on
the heaviest real block tile (z11/459/912, 6,072 candidate blocks, 4,334 in
scope): scoped 233 ms vs unscoped 207 ms end to end, and the ``EXISTS`` probe
itself 41 ms. The planner does **not** use ``aoi_feature_feature_idx`` — it
seq-scans ``aoi_feature`` and hash-joins, and forcing the index path instead
(``SET enable_seqscan=off``) is **3.3x slower**, 134 ms.

That is the correct plan *for this database*, and the reason is specific to it:
``reimagina_demo`` holds exactly one AOI, so all 225,173 ``aoi_feature`` rows
match any ``aoi_id`` filter and a scan beats 225k index probes. It is therefore
**not** evidence that an index is unnecessary at multi-AOI scale — with several
AOIs the filter becomes selective and the planner should switch to the existing
``(feature_id)`` index on its own. What the measurement does establish is that
there is no problem to fix today and no basis for adding an index now: a
covering ``(feature_id, aoi_id)`` index would be a write cost paid forever,
justified by a prediction rather than a reading. Re-measure when a second AOI
lands; that is when the question actually becomes live.

Revision ID: r039
Revises: r038
Create Date: 2026-08-18
"""

from __future__ import annotations

from alembic import op

revision = "r039"
down_revision = "r038"
branch_labels = None
depends_on = None


# Same single-body-two-parameters shape as r038: CREATE OR REPLACE means the
# last writer wins outright, so upgrade and downgrade must not be able to drift.
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
        ON fca.feature_id = f.id AND z >= 14
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
      AND (z >= 14 OR f.type_slug <> 'building')
      {aoi_filter}
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

# NULLIF so that `?aoi_id=` (present but blank) reads as unscoped rather than
# as a uuid that fails the regex and blanks the map -- an empty string is how a
# client serialises "no AOI selected".
_AOI_FILTER = """
      AND (
        NULLIF(query_params->>'aoi_id', '') IS NULL
        OR (
          NULLIF(query_params->>'aoi_id', '') ~ '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
          AND EXISTS (
            SELECT 1
            FROM curated_geo.aoi_feature af
            WHERE af.feature_id = f.id
              AND af.aoi_id = (query_params->>'aoi_id')::uuid
          )
        )
      )"""


def upgrade() -> None:
    op.execute(_GET_TILE.format(aoi_filter=_AOI_FILTER))


def downgrade() -> None:
    # Back to r038: aoi_id present but silently ignored.
    op.execute(_GET_TILE.format(aoi_filter=""))
