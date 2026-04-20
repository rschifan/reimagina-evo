CREATE SCHEMA IF NOT EXISTS explore;

-- `explore.layer_catalog` is a real TABLE, matching the Postgres r006
-- shape. `explore.layer_catalog_v` selects from it ordered by
-- display_order so future migrations just INSERT one row per new read
-- surface — no CREATE OR REPLACE of a growing UNION ALL in every
-- migration.

CREATE TABLE IF NOT EXISTS explore.layer_catalog (
    schema_name             VARCHAR NOT NULL,
    object_name             VARCHAR NOT NULL,
    logical_name            VARCHAR NOT NULL,
    layer_kind              VARCHAR NOT NULL,
    description             VARCHAR NOT NULL,
    primary_geometry_column VARCHAR,
    intended_use            VARCHAR NOT NULL,
    display_order           INTEGER NOT NULL DEFAULT 1000,
    PRIMARY KEY (schema_name, object_name)
);

INSERT INTO explore.layer_catalog
    (schema_name, object_name, logical_name, layer_kind, description,
     primary_geometry_column, intended_use, display_order)
VALUES
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
     'Verify what the app-facing layer exposes', 930);

CREATE OR REPLACE VIEW explore.layer_catalog_v AS
SELECT schema_name, object_name, logical_name, layer_kind,
       description, primary_geometry_column, intended_use
FROM explore.layer_catalog
ORDER BY display_order, schema_name, object_name;

CREATE OR REPLACE VIEW explore.source_runs_v AS
SELECT
    CAST(s.id AS VARCHAR) AS source_id,
    s.name AS source_name,
    s.provider,
    s.storage_type,
    s.integration_role,
    s.version,
    s.storage_uri,
    CAST(r.id AS VARCHAR) AS ingestion_run_id,
    r.status AS ingestion_status,
    r.transformation_step,
    r.orchestrator,
    r.orchestrator_run_id,
    r.started_at,
    r.completed_at,
    CAST(r.details AS VARCHAR) AS details
FROM curated_geo.data_source_catalog s
LEFT JOIN curated_geo.ingestion_run r
  ON r.source_id = s.id;

CREATE OR REPLACE VIEW explore.feature_type_stats_v AS
SELECT
    f.type_slug,
    f.role,
    CAST(f.source_id AS VARCHAR) AS source_id,
    f.dataset_version,
    COUNT(*) AS row_count
FROM curated_geo.feature f
GROUP BY f.type_slug, f.role, CAST(f.source_id AS VARCHAR), f.dataset_version;

CREATE OR REPLACE VIEW explore.serving_stats_v AS
SELECT
    'all' AS stat_scope,
    CAST(NULL AS VARCHAR) AS type_slug,
    CAST(NULL AS VARCHAR) AS role,
    COUNT(*) AS row_count
FROM serving_geo.feature_v
UNION ALL
SELECT
    'type_role' AS stat_scope,
    type_slug,
    role,
    COUNT(*) AS row_count
FROM serving_geo.feature_v
GROUP BY type_slug, role;
