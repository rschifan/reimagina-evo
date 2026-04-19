"""serving_geo.indicator_overlay_tile — live MVT choropleth source.

Joins serving_geo.current_indicator_v with curated_geo.feature and emits a
vector tile whose fill-color expressions in the frontend can key directly
off the `value_numeric` property. Every feature in a given request is
scoped to one indicator_slug (required) plus an optional scenario_id.

Design decisions that follow the get_tile (r010) precedent:

- Feature source is `curated_geo.feature` joined to
  `curated_geo.indicator_result WHERE status='current'` — not
  `serving_geo.current_indicator_v`, because the view hides indicator_id
  and joining through it adds a second catalog lookup the planner can't
  eliminate. One query, two tables.
- Geometry is simplified by zoom the same way get_tile does. Below z=10
  the function returns an empty tile — choropleth at city / country zoom
  is misleading (every feature becomes a single pixel) and the manifest
  reflects this with `minzoom: 10`.
- MVT layer name is `indicator_overlay` so frontend layer specs can use
  a stable `source-layer` value across every indicator.
- PARALLEL SAFE + STABLE to match the existing tile function's caching
  + concurrency posture in martin.

MVT properties emitted per feature:
- feature_id  (text)        — stable UUID for hover / click interactions
- type_slug    (text)        — so frontends can gate styling by grain
- value_numeric (double)     — the choropleth value; fill-color reads this
- quality_flag (text)        — 'measured' / 'estimated' / 'missing' / …

DuckDB has no MVT emitter. Tile serving is Postgres-only.

Revision ID: r017
Revises: r016
Create Date: 2026-04-20
"""
from __future__ import annotations

from alembic import op

revision = "r017"
down_revision = "r016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
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
              NULLIF(query_params->>'scenario_id', '')    AS scenario_id_txt
          )
          SELECT ST_AsMVT(tile, 'indicator_overlay', 4096, 'geom')
          FROM (
            SELECT
              -- Transform + clip + quantise geometry to tile pixel coordinates.
              -- Same simplification ladder as get_tile so both layers register
              -- at the same resolution at each zoom level.
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
                4096,     -- tile extent
                64,       -- edge buffer
                true      -- clip
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
              -- Tile function is useless without a slug; return empty tile
              -- rather than the full dataset if the caller forgot to filter.
              p.indicator_slug IS NOT NULL
              AND ir.indicator_slug = p.indicator_slug
              AND ir.status = 'current'
              AND (
                (p.scenario_id_txt IS NULL AND ir.scenario_id IS NULL)
                OR (p.scenario_id_txt IS NOT NULL
                    AND ir.scenario_id = p.scenario_id_txt::uuid)
              )
              -- Bounding-box filter hits feature_geometry_gist.
              AND f.geometry && ST_Transform(
                ST_TileEnvelope(z, x, y, margin => 64.0 / 4096),
                4326
              )
              -- Choropleth below z=10 is misleading at city scale — one
              -- tile covers a whole district, so every feature collapses
              -- to a single pixel with an averaged colour. Return empty
              -- instead so the layer manifest's minzoom=10 is enforced
              -- server-side too.
              AND z >= 10
          ) tile
          WHERE tile.geom IS NOT NULL
        $tile$
    """)

    op.execute("""
        COMMENT ON FUNCTION serving_geo.indicator_overlay_tile(integer, integer, integer, json) IS
        'Martin function source — MVT choropleth for one indicator. '
        'Required query param: indicator_slug. Optional: scenario_id. '
        'Returns empty below z=10 (choropleth is misleading at city/country scale).'
    """)

    # Register the new read surface in the engineer-facing explore catalog so
    # `explore layers` surfaces it the same way every other serving object
    # shows up.
    op.execute("""
        CREATE OR REPLACE VIEW explore.layer_catalog_v AS
        SELECT 'curated_geo'::text AS schema_name, 'data_source_catalog'::text AS object_name, 'source_registry'::text AS logical_name,
               'canonical'::text AS layer_kind, 'Registered source datasets and versions'::text AS description,
               NULL::text AS primary_geometry_column, 'Inspect available datasets and storage metadata'::text AS intended_use
        UNION ALL
        SELECT 'curated_geo', 'ingestion_run', 'ingestion_runs', 'canonical',
               'Lineage and status for source loads', NULL, 'Inspect pipeline runs and failure details'
        UNION ALL
        SELECT 'curated_geo', 'feature', 'canonical_features', 'canonical',
               'Stable canonical spatial entities', 'geometry', 'Debug source-of-truth feature storage'
        UNION ALL
        SELECT 'curated_geo', 'observation', 'feature_observations', 'canonical',
               'Measured and derived values attached to features', NULL, 'Inspect current and historical enrichments'
        UNION ALL
        SELECT 'curated_geo', 'aoi_indicator_summary', 'aoi_summaries', 'canonical',
               'Cached AOI-level aggregated indicator values', NULL, 'Inspect cached summary rows by AOI'
        UNION ALL
        SELECT 'curated_geo', 'indicator_catalog', 'indicator_catalog', 'canonical',
               'Registry of every indicator computed by the platform', NULL, 'See what indicators are defined and how they aggregate'
        UNION ALL
        SELECT 'curated_geo', 'indicator_result', 'indicator_results', 'canonical',
               'Long-format computed indicator values per (feature, scenario, indicator)', NULL, 'Inspect raw indicator outputs and lineage'
        UNION ALL
        SELECT 'serving_geo', 'feature_v', 'serving_features', 'serving',
               'App-facing merged baseline features', 'geometry', 'Inspect what API and workers read'
        UNION ALL
        SELECT 'serving_geo', 'aoi_summary_v', 'aoi_summaries_serving', 'serving',
               'App-facing AOI-level summaries (status = current only)', NULL, 'Inspect the read contract for the AOI summary cache'
        UNION ALL
        SELECT 'serving_geo', 'current_indicator_v', 'current_indicators', 'serving',
               'App-facing indicator read contract (status = current only) joined with catalog metadata', NULL, 'Inspect the read path API and workers use for indicator values'
        UNION ALL
        SELECT 'serving_geo', 'indicator_overlay_tile', 'indicator_overlay_tiles', 'serving',
               'Live MVT choropleth for one indicator — martin function source', NULL, 'Call as GET /indicator_overlay_tile/z/x/y?indicator_slug=…'
        UNION ALL
        SELECT 'explore', 'layer_catalog_v', 'layer_catalog', 'explore',
               'Curated registry of important database layers', NULL, 'Use as the entrypoint for database exploration'
        UNION ALL
        SELECT 'explore', 'source_runs_v', 'source_runs', 'explore',
               'Flattened source registry joined with ingestion runs', NULL, 'Inspect source lineage quickly'
        UNION ALL
        SELECT 'explore', 'feature_type_stats_v', 'feature_type_stats', 'explore',
               'Counts of canonical features by type, role, source, and version', NULL, 'Inspect loaded feature volumes'
        UNION ALL
        SELECT 'explore', 'serving_stats_v', 'serving_stats', 'explore',
               'High-level counts over the serving layer', NULL, 'Verify what the app-facing layer exposes'
    """)


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS serving_geo.indicator_overlay_tile(integer, integer, integer, json)"
    )
    # Restore the r016-shaped explore catalog.
    op.execute("""
        CREATE OR REPLACE VIEW explore.layer_catalog_v AS
        SELECT 'curated_geo'::text AS schema_name, 'data_source_catalog'::text AS object_name, 'source_registry'::text AS logical_name,
               'canonical'::text AS layer_kind, 'Registered source datasets and versions'::text AS description,
               NULL::text AS primary_geometry_column, 'Inspect available datasets and storage metadata'::text AS intended_use
        UNION ALL
        SELECT 'curated_geo', 'ingestion_run', 'ingestion_runs', 'canonical',
               'Lineage and status for source loads', NULL, 'Inspect pipeline runs and failure details'
        UNION ALL
        SELECT 'curated_geo', 'feature', 'canonical_features', 'canonical',
               'Stable canonical spatial entities', 'geometry', 'Debug source-of-truth feature storage'
        UNION ALL
        SELECT 'curated_geo', 'observation', 'feature_observations', 'canonical',
               'Measured and derived values attached to features', NULL, 'Inspect current and historical enrichments'
        UNION ALL
        SELECT 'curated_geo', 'aoi_indicator_summary', 'aoi_summaries', 'canonical',
               'Cached AOI-level aggregated indicator values', NULL, 'Inspect cached summary rows by AOI'
        UNION ALL
        SELECT 'curated_geo', 'indicator_catalog', 'indicator_catalog', 'canonical',
               'Registry of every indicator computed by the platform', NULL, 'See what indicators are defined and how they aggregate'
        UNION ALL
        SELECT 'curated_geo', 'indicator_result', 'indicator_results', 'canonical',
               'Long-format computed indicator values per (feature, scenario, indicator)', NULL, 'Inspect raw indicator outputs and lineage'
        UNION ALL
        SELECT 'serving_geo', 'feature_v', 'serving_features', 'serving',
               'App-facing merged baseline features', 'geometry', 'Inspect what API and workers read'
        UNION ALL
        SELECT 'serving_geo', 'aoi_summary_v', 'aoi_summaries_serving', 'serving',
               'App-facing AOI-level summaries (status = current only)', NULL, 'Inspect the read contract for the AOI summary cache'
        UNION ALL
        SELECT 'serving_geo', 'current_indicator_v', 'current_indicators', 'serving',
               'App-facing indicator read contract (status = current only) joined with catalog metadata', NULL, 'Inspect the read path API and workers use for indicator values'
        UNION ALL
        SELECT 'explore', 'layer_catalog_v', 'layer_catalog', 'explore',
               'Curated registry of important database layers', NULL, 'Use as the entrypoint for database exploration'
        UNION ALL
        SELECT 'explore', 'source_runs_v', 'source_runs', 'explore',
               'Flattened source registry joined with ingestion runs', NULL, 'Inspect source lineage quickly'
        UNION ALL
        SELECT 'explore', 'feature_type_stats_v', 'feature_type_stats', 'explore',
               'Counts of canonical features by type, role, source, and version', NULL, 'Inspect loaded feature volumes'
        UNION ALL
        SELECT 'explore', 'serving_stats_v', 'serving_stats', 'explore',
               'High-level counts over the serving layer', NULL, 'Verify what the app-facing layer exposes'
    """)
