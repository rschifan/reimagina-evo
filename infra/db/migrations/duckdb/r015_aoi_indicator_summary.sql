-- r015: AOI-level indicator summary cache.
-- The row stores a single aggregated value per (aoi_id, scenario_id, indicator_id).
-- scenario_id is nullable to represent "base city" (Diagnosis mode).
-- indicator_id has no FK yet because curated_geo.indicator_catalog lands with
-- Layer 2 indicators; the column is a plain UUID until then.

CREATE TABLE IF NOT EXISTS curated_geo.aoi_indicator_summary (
    id                UUID    PRIMARY KEY DEFAULT uuid(),
    aoi_id            UUID    NOT NULL,
    scenario_id       UUID,
    indicator_id      UUID    NOT NULL,
    value             DOUBLE,
    aggregation_note  VARCHAR,
    status            VARCHAR NOT NULL DEFAULT 'current',
    computed_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (status IN ('current', 'stale', 'missing'))
);

-- DuckDB does not support partial indexes (unlike Postgres r015, which uses
-- two partial unique indexes). Uniqueness is enforced repo-side via
-- DuckDBAOISummaryRepository.upsert_summary (lookup-then-update); a read
-- lookup index covers the common (aoi_id, scenario_id) access pattern.
CREATE INDEX IF NOT EXISTS aoi_indicator_summary_aoi_idx
    ON curated_geo.aoi_indicator_summary (aoi_id, scenario_id, indicator_id);

-- Serving view — a minimal projection until indicator_catalog arrives in
-- Layer 2; at that point the view will be extended to join slug/name/unit.
CREATE OR REPLACE VIEW serving_geo.aoi_summary_v AS
SELECT
    CAST(s.id AS VARCHAR)           AS id,
    CAST(s.aoi_id AS VARCHAR)       AS aoi_id,
    CAST(s.scenario_id AS VARCHAR)  AS scenario_id,
    CAST(s.indicator_id AS VARCHAR) AS indicator_id,
    s.value,
    s.aggregation_note,
    s.status,
    s.computed_at
FROM curated_geo.aoi_indicator_summary s
WHERE s.status = 'current';

-- Register the new read surface in the engineer-facing catalog so
-- `python -m apps.cli.app explore layers` surfaces it.
CREATE OR REPLACE VIEW explore.layer_catalog_v AS
SELECT 'curated_geo' AS schema_name, 'data_source_catalog' AS object_name, 'source_registry' AS logical_name,
       'canonical' AS layer_kind, 'Registered source datasets and versions' AS description,
       NULL AS primary_geometry_column, 'Inspect available datasets and storage metadata' AS intended_use
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
SELECT 'serving_geo', 'feature_v', 'serving_features', 'serving',
       'App-facing merged baseline features', 'geometry', 'Inspect what API and workers read'
UNION ALL
SELECT 'serving_geo', 'aoi_summary_v', 'aoi_summaries_serving', 'serving',
       'App-facing AOI-level summaries (status = current only)', NULL, 'Inspect the read contract for the AOI summary cache'
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
       'High-level counts over the serving layer', NULL, 'Verify what the app-facing layer exposes';
