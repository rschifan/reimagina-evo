INSTALL spatial;
LOAD spatial;

CREATE SCHEMA IF NOT EXISTS app_core;
CREATE SCHEMA IF NOT EXISTS jobs_runtime;
CREATE SCHEMA IF NOT EXISTS curated_geo;
CREATE SCHEMA IF NOT EXISTS serving_geo;

CREATE TABLE IF NOT EXISTS app_core.project (
    id UUID PRIMARY KEY DEFAULT uuid(),
    name VARCHAR NOT NULL,
    description VARCHAR,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
