CREATE TABLE IF NOT EXISTS curated_geo.data_source_catalog (
    id UUID PRIMARY KEY DEFAULT uuid(),
    name VARCHAR NOT NULL,
    provider VARCHAR NOT NULL,
    storage_type VARCHAR NOT NULL,
    integration_role VARCHAR NOT NULL,
    storage_uri VARCHAR NOT NULL,
    schema_definition JSON,
    version VARCHAR NOT NULL,
    license VARCHAR,
    update_frequency VARCHAR,
    source_extent_geometry GEOMETRY,
    ingested_at TIMESTAMPTZ,
    notes VARCHAR,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (provider, name, version)
);

CREATE TABLE IF NOT EXISTS curated_geo.ingestion_run (
    id UUID PRIMARY KEY DEFAULT uuid(),
    source_id UUID NOT NULL,
    orchestrator VARCHAR NOT NULL,
    orchestrator_run_id VARCHAR NOT NULL,
    transformation_step VARCHAR NOT NULL,
    code_version VARCHAR NOT NULL,
    status VARCHAR NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    details JSON NOT NULL DEFAULT '{}',
    UNIQUE (source_id, orchestrator, orchestrator_run_id, transformation_step)
);

CREATE INDEX IF NOT EXISTS ingestion_run_source_idx ON curated_geo.ingestion_run (source_id);
CREATE INDEX IF NOT EXISTS ingestion_run_status_idx ON curated_geo.ingestion_run (status);
