"""Scenario overlay tiles: a scenario's own rows, falling back to the baseline.

A scenario stores only what it changed. `indicator_overlay_tile` matched
`ir.scenario_id = <scenario>` exactly (r025), so a scenario tile carried values
only for the indicators that scenario had recomputed — every other indicator was
a hole on the map, not a baseline value.

That is not a hypothetical gap. On `main` today a scenario override on `floors`
dirties exactly one indicator, so a run materialises 1 of 13 and the other 12
have no rows under that scenario at all. The web client passes `scenario_id`
straight through (`apps/web/src/api.ts`), so the holes are user-visible.

The rule here is the one the platform already applies to indicator *inputs* —
`scenarios.overrides.effective_numeric` resolves
`COALESCE(override, observation, base)` — finally applied to their outputs.

⚠ Expressed as an anti-join, not as `scenario_id = X OR scenario_id IS NULL`.
The widened form looks equivalent and silently double-counts: where a scenario
*has* recomputed a feature, both its row and the baseline's match, so the tile
would carry two features with the same id and `ST_AsMVT` would emit both. Here a
baseline row qualifies only when no scenario row exists for the same
(feature, indicator), so exactly one survives per pair.

The probe is index-backed: `ir_one_current_scenario` (r016) is exactly
`(feature_id, scenario_id, indicator_id) WHERE status='current'`.

⚠ A scenario row shadows the baseline only when it *carries a value*, matching
`effective_numeric`'s `COALESCE(so.value_numeric, obs.value_numeric, base)` —
COALESCE skips a NULL override rather than letting it win.
`write_result(value_numeric=None, quality_flag='missing')` is supported, so
valueless current rows exist by design; without the value test one of them would
suppress the baseline and emit a NULL-valued feature to the map, which the
frontend's `fill-color` expression reads straight off `value_numeric`.

Baseline tiles are untouched — with no `scenario_id` the predicate is unchanged,
which is also why the aggregation overlay is left alone: it reads `observation`,
which has no scenario dimension at all.

Revision ID: r043
Revises: r042
Create Date: 2026-08-18
"""

from __future__ import annotations

from alembic import op

revision = "r043"
down_revision = "r042"
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


_OVERLAY = f"""
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

_EXACT = f"""
            (p.scenario_id_txt IS NULL AND ir.scenario_id IS NULL)
            OR (p.scenario_id_txt IS NOT NULL
                AND p.scenario_id_txt ~ '{_UUID_RE}'
                AND ir.scenario_id = p.scenario_id_txt::uuid)
"""


def upgrade() -> None:
    op.execute(_body(_OVERLAY))


def downgrade() -> None:
    op.execute(_body(_EXACT))
