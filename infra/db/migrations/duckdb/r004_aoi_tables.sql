CREATE TABLE IF NOT EXISTS curated_geo.area_of_interest (
    id UUID PRIMARY KEY DEFAULT uuid(),
    project_id UUID NOT NULL,
    name VARCHAR NOT NULL,
    description VARCHAR,
    geometry GEOMETRY NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS curated_geo.aoi_feature (
    aoi_id UUID NOT NULL,
    feature_id UUID NOT NULL,
    overlap_basis VARCHAR NOT NULL,
    overlap_measure DOUBLE NOT NULL,
    feature_measure DOUBLE NOT NULL,
    overlap_ratio DOUBLE NOT NULL,
    PRIMARY KEY (aoi_id, feature_id)
);

CREATE INDEX IF NOT EXISTS aoi_feature_feature_idx ON curated_geo.aoi_feature (feature_id);
