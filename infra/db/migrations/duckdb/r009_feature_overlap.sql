-- Declares which (source_type, target_type) pairs should have overlaps computed.
CREATE TABLE IF NOT EXISTS curated_geo.feature_overlap_type_pair (
    id             UUID    PRIMARY KEY DEFAULT uuid(),
    source_type_id UUID    NOT NULL,
    target_type_id UUID    NOT NULL,
    UNIQUE (source_type_id, target_type_id)
);

-- Pre-computed spatial intersections — generalises aoi_feature to feature×feature.
CREATE TABLE IF NOT EXISTS curated_geo.feature_overlap (
    id                UUID    PRIMARY KEY DEFAULT uuid(),
    source_feature_id UUID    NOT NULL,
    target_feature_id UUID    NOT NULL,
    overlap_basis     VARCHAR NOT NULL,
    overlap_measure   DOUBLE  NOT NULL,
    feature_measure   DOUBLE  NOT NULL,
    overlap_ratio     DOUBLE  NOT NULL,
    computed_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_feature_id, target_feature_id)
);

CREATE INDEX IF NOT EXISTS feature_overlap_target_idx
    ON curated_geo.feature_overlap (target_feature_id, overlap_ratio);

CREATE INDEX IF NOT EXISTS feature_overlap_source_idx
    ON curated_geo.feature_overlap (source_feature_id);
