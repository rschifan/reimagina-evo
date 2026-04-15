CREATE TABLE IF NOT EXISTS curated_geo.feature_type_catalog (
    id UUID PRIMARY KEY DEFAULT uuid(),
    context VARCHAR NOT NULL,
    type_slug VARCHAR NOT NULL,
    label VARCHAR NOT NULL,
    geometry_family VARCHAR NOT NULL,
    role VARCHAR NOT NULL,
    admin_level INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (context, type_slug)
);

CREATE TABLE IF NOT EXISTS curated_geo.feature (
    id UUID PRIMARY KEY DEFAULT uuid(),
    type_id UUID NOT NULL,
    type_slug VARCHAR NOT NULL,
    role VARCHAR NOT NULL,
    parent_id UUID,
    name VARCHAR,
    geometry GEOMETRY NOT NULL,
    attributes JSON NOT NULL DEFAULT '{}',
    source_id UUID NOT NULL,
    source_record_ref VARCHAR NOT NULL,
    ingestion_run_id UUID NOT NULL,
    dataset_version VARCHAR NOT NULL,
    confidence DOUBLE,
    model_version VARCHAR,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_id, dataset_version, source_record_ref)
);

CREATE INDEX IF NOT EXISTS feature_type_idx ON curated_geo.feature (type_slug);
CREATE INDEX IF NOT EXISTS feature_role_idx ON curated_geo.feature (role);
CREATE INDEX IF NOT EXISTS feature_source_idx ON curated_geo.feature (source_id);

CREATE TABLE IF NOT EXISTS curated_geo.observation (
    id UUID PRIMARY KEY DEFAULT uuid(),
    feature_id UUID NOT NULL,
    attribute_key VARCHAR NOT NULL,
    value_type VARCHAR NOT NULL,
    value_numeric DOUBLE,
    value_text VARCHAR,
    value_json JSON,
    source_id UUID NOT NULL,
    ingestion_run_id UUID NOT NULL,
    lineage_ref JSON,
    is_current BOOLEAN NOT NULL DEFAULT true,
    valid_from TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_to TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS observation_feature_idx ON curated_geo.observation (feature_id);
CREATE INDEX IF NOT EXISTS observation_current_idx ON curated_geo.observation (feature_id, attribute_key, is_current);

CREATE TABLE IF NOT EXISTS curated_geo.file_asset (
    id UUID PRIMARY KEY DEFAULT uuid(),
    source_id UUID NOT NULL,
    ingestion_run_id UUID NOT NULL,
    storage_uri VARCHAR NOT NULL,
    format VARCHAR,
    crs VARCHAR,
    resolution_m DOUBLE,
    band_description JSON,
    asset_extent_geometry GEOMETRY,
    captured_at TIMESTAMPTZ,
    dataset_version VARCHAR NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
