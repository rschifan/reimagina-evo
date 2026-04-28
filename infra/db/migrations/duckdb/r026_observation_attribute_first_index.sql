-- r026: speed up the tile-source resolver's affinity scan by leading
-- the observation index with attribute_key. Postgres-side rationale
-- in the alembic migration; DuckDB doesn't support partial indexes so
-- we add an unconditional index. The is_current filter still gets a
-- predicate scan but the leading column already narrows the search.
CREATE INDEX IF NOT EXISTS obs_attr_feat_current
    ON curated_geo.observation (attribute_key, feature_id);
