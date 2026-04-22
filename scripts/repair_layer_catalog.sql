-- One-off repair for dev Postgres DBs stamped before r006 was rewritten
-- from a "view-of-literals" into a real table + catalog backfill.
--
-- Symptom: explore.layer_catalog_v exists as a VIEW with hardcoded UNION ALL
-- rows, but explore.layer_catalog (table) does not exist — so r019's
-- `INSERT INTO explore.layer_catalog …` fails.
--
-- Fix: drop the literal view, create the real table, backfill the 15 rows
-- that earlier migrations (r006 + r015 + r016 + r017) would have inserted,
-- then recreate the view as SELECT * FROM the table. After running this,
-- `alembic upgrade head` progresses cleanly through r019.
--
-- Idempotent: safe to re-run if the table already exists.

BEGIN;

DROP VIEW IF EXISTS explore.layer_catalog_v;

CREATE TABLE IF NOT EXISTS explore.layer_catalog (
    schema_name              text    NOT NULL,
    object_name              text    NOT NULL,
    logical_name             text    NOT NULL,
    layer_kind               text    NOT NULL,
    description              text    NOT NULL,
    primary_geometry_column  text,
    intended_use             text    NOT NULL,
    display_order            integer NOT NULL DEFAULT 1000,
    PRIMARY KEY (schema_name, object_name)
);

INSERT INTO explore.layer_catalog
    (schema_name, object_name, logical_name, layer_kind, description,
     primary_geometry_column, intended_use, display_order)
VALUES
    -- r006 initial rows
    ('curated_geo', 'data_source_catalog', 'source_registry', 'canonical',
     'Registered source datasets and versions', NULL,
     'Inspect available datasets and storage metadata', 10),
    ('curated_geo', 'ingestion_run', 'ingestion_runs', 'canonical',
     'Lineage and status for source loads', NULL,
     'Inspect pipeline runs and failure details', 20),
    ('curated_geo', 'feature', 'canonical_features', 'canonical',
     'Stable canonical spatial entities', 'geometry',
     'Debug source-of-truth feature storage', 30),
    ('curated_geo', 'observation', 'feature_observations', 'canonical',
     'Measured and derived values attached to features', NULL,
     'Inspect current and historical enrichments', 40),
    ('serving_geo', 'feature_v', 'serving_features', 'serving',
     'App-facing merged baseline features', 'geometry',
     'Inspect what API and workers read', 100),
    ('explore', 'layer_catalog_v', 'layer_catalog', 'explore',
     'Curated registry of important database layers', NULL,
     'Use as the entrypoint for database exploration', 900),
    ('explore', 'source_runs_v', 'source_runs', 'explore',
     'Flattened source registry joined with ingestion runs', NULL,
     'Inspect source lineage quickly', 910),
    ('explore', 'feature_type_stats_v', 'feature_type_stats', 'explore',
     'Counts of canonical features by type, role, source, and version', NULL,
     'Inspect loaded feature volumes', 920),
    ('explore', 'serving_stats_v', 'serving_stats', 'explore',
     'High-level counts over the serving layer', NULL,
     'Verify what the app-facing layer exposes', 930),
    -- r015
    ('curated_geo', 'aoi_indicator_summary', 'aoi_summaries', 'canonical',
     'Cached AOI-level aggregated indicator values', NULL,
     'Inspect cached summary rows by AOI', 50),
    ('serving_geo', 'aoi_summary_v', 'aoi_summaries_serving', 'serving',
     'App-facing AOI-level summaries (status = current only)', NULL,
     'Inspect the read contract for the AOI summary cache', 110),
    -- r016
    ('curated_geo', 'indicator_catalog', 'indicator_catalog', 'canonical',
     'Registry of every indicator computed by the platform', NULL,
     'See what indicators are defined and how they aggregate', 60),
    ('curated_geo', 'indicator_result', 'indicator_results', 'canonical',
     'Long-format computed indicator values per (feature, scenario, indicator)', NULL,
     'Inspect raw indicator outputs and lineage', 70),
    ('serving_geo', 'current_indicator_v', 'current_indicators', 'serving',
     'App-facing indicator read contract (status = current only) joined with catalog metadata', NULL,
     'Inspect the read path API and workers use for indicator values', 120),
    -- r017
    ('serving_geo', 'indicator_overlay_tile', 'indicator_overlay_tiles', 'serving',
     'Live MVT choropleth for one indicator — martin function source', NULL,
     'Call as GET /indicator_overlay_tile/z/x/y?indicator_slug=…', 130)
ON CONFLICT (schema_name, object_name) DO NOTHING;

CREATE VIEW explore.layer_catalog_v AS
SELECT schema_name, object_name, logical_name, layer_kind,
       description, primary_geometry_column, intended_use
FROM explore.layer_catalog
ORDER BY display_order, schema_name, object_name;

COMMIT;
