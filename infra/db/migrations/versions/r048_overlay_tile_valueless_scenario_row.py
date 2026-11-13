"""Overlay tiles: a valueless scenario row must not double-draw the feature.

r043 made `indicator_overlay_tile` read a scenario as an *overlay* — its own
rows, else the baseline's — and was careful about the one way that can
double-count. Its anti-join arm admits a baseline row only when no scenario row
shadows it, and the shadow probe tests for a *value*: a current scenario row
carrying none cannot suppress the baseline, matching
`effective_numeric`'s `COALESCE(override, observation, base)`, which skips a
NULL override rather than letting it win.

The value test went into the probe but not into the arm beside it. So for a
feature with a current baseline row carrying a number and a current scenario row
carrying none, *both* halves of the predicate matched:

  - `ir.scenario_id = <scenario>` matched the valueless scenario row — there was
    no value test on `ir` itself;
  - `ir.scenario_id IS NULL AND NOT EXISTS (… value test …)` matched the
    baseline row, because the probe had just ignored that same valueless
    scenario row so it could not shadow the baseline.

`ST_AsMVT` then emitted two features with one `feature_id` — exactly the
double-draw the anti-join exists to prevent, reached through the arm the
anti-join does not guard. Measured at r047 on one block with `scenario_id` set:

    value tile features for one block: 2
        {'feature_id': '43b8fce6-…', 'type_slug': 'block',
         'quality_flag': 'missing'}
        {'feature_id': '43b8fce6-…', 'type_slug': 'block',
         'value_numeric': 0.7848538896415339, 'quality_flag': 'measured'}

`apps/web/src/choropleth.ts` reads `value_numeric` straight off the feature, so
the block painted as data or as a hole depending on which of the two the
renderer drew last — and the order is the planner's, not the query's.

The fix is the value test the probe already applies, applied to the scenario arm
too, so the two halves stop overlapping: a valueless scenario row no longer
qualifies through the equality arm, the baseline survives the anti-join exactly
as before, and the feature is drawn once, at its baseline value. That is the
rule `SCENARIOS_LAYER.md` states — "a scenario row shadows the baseline only
when it carries a value" — and the trade-off it records stands unchanged: a
scenario still cannot express "recomputed, and the answer is now unknown" and
have that beat the baseline. Doing so needs a `quality_flag` the read layer
honours, not the absence of a value, which is indistinguishable from "not
computed".

⚠ `storage/_utils.scenario_overlay`, the canonical predicate the six Python read
paths share, has the same unguarded equality arm and so returns both rows for
this feature. It is left alone here deliberately: the arm is reached by five call
sites whose downstream `value_numeric IS NOT NULL` filters mask the duplicate to
differing degrees, and correcting it is a change to an aggregate's denominator,
not to a tile's draw order. This migration closes the tile, which has no such
filter and paints the duplicate directly.

Baseline tiles are untouched: with no `scenario_id` the predicate is unchanged,
`ir.scenario_id IS NULL` as before. The aggregation overlay is left alone for
r043's reason — it reads `observation`, which has no scenario dimension at all.

Everything but the scenario predicate is r043's body verbatim, re-stated because
migrations here are self-contained and `CREATE OR REPLACE FUNCTION` must carry
the whole definition. The signature is unchanged:
`(z, x, y, query_params json) RETURNS bytea`.

Revision ID: r048
Revises: r047
Create Date: 2026-11-13
"""

from __future__ import annotations

from alembic import op

revision = "r048"
down_revision = "r047"
branch_labels = None
depends_on = None

_UUID_RE = (
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _body(scenario_predicate: str) -> str:
    return f"""
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
          AND ({scenario_predicate})
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


# A scenario row wins only when it carries a value — the same test the
# anti-join's probe below applies, so the two arms cannot both match one
# feature. Without it a valueless scenario row matched here while the baseline
# matched the anti-join, and the tile drew the feature twice.
_OVERLAY_VALUE_TESTED = f"""
            (p.scenario_id_txt IS NULL AND ir.scenario_id IS NULL)
            OR (p.scenario_id_txt IS NOT NULL
                AND p.scenario_id_txt ~ '{_UUID_RE}'
                AND (
                  (ir.scenario_id = p.scenario_id_txt::uuid
                    AND (ir.value_numeric IS NOT NULL
                      OR ir.value_text IS NOT NULL
                      OR ir.value_json IS NOT NULL))
                  OR (ir.scenario_id IS NULL AND NOT EXISTS (
                        SELECT 1
                        FROM curated_geo.indicator_result _ov
                        WHERE _ov.feature_id = ir.feature_id
                          AND _ov.indicator_id = ir.indicator_id
                          AND _ov.status = 'current'
                          AND _ov.scenario_id = p.scenario_id_txt::uuid
                          AND (_ov.value_numeric IS NOT NULL
                            OR _ov.value_text IS NOT NULL
                            OR _ov.value_json IS NOT NULL)
                      ))
                ))
"""

# r043's predicate: the value test in the probe only. Restored on downgrade,
# double-draw included.
_OVERLAY_R043 = f"""
            (p.scenario_id_txt IS NULL AND ir.scenario_id IS NULL)
            OR (p.scenario_id_txt IS NOT NULL
                AND p.scenario_id_txt ~ '{_UUID_RE}'
                AND (
                  ir.scenario_id = p.scenario_id_txt::uuid
                  OR (ir.scenario_id IS NULL AND NOT EXISTS (
                        SELECT 1
                        FROM curated_geo.indicator_result _ov
                        WHERE _ov.feature_id = ir.feature_id
                          AND _ov.indicator_id = ir.indicator_id
                          AND _ov.status = 'current'
                          AND _ov.scenario_id = p.scenario_id_txt::uuid
                          AND (_ov.value_numeric IS NOT NULL
                            OR _ov.value_text IS NOT NULL
                            OR _ov.value_json IS NOT NULL)
                      ))
                ))
"""


def upgrade() -> None:
    op.execute(_body(_OVERLAY_VALUE_TESTED))


def downgrade() -> None:
    op.execute(_body(_OVERLAY_R043))
