-- r012: DuckDB-side fixups matching the Postgres r012 migration.
--
-- 1. RTREE spatial index on curated_geo.feature(geometry).
--    The Postgres backend has feature_geometry_gist (r007); DuckDB lacked any
--    spatial index, causing full geometry scans on all spatial queries.
--
-- 2. CHECK constraints on curated_geo.feature_overlap to match the Postgres
--    schema defined in r009.  DuckDB's r009 migration omitted these.
--
-- 3. B-tree index on feature_overlap_type_pair(target_type_id) for reverse
--    lookups — also missing from both backends' r009.

-- ── 1. Spatial index ────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS feature_geometry_rtree
ON curated_geo.feature USING RTREE (geometry);

-- ── 2. CHECK constraints on feature_overlap ─────────────────────────────────
ALTER TABLE curated_geo.feature_overlap
    ADD CONSTRAINT feature_overlap_basis_check
    CHECK (overlap_basis IN ('area', 'length', 'presence'));

ALTER TABLE curated_geo.feature_overlap
    ADD CONSTRAINT feature_overlap_measure_check
    CHECK (overlap_measure > 0);

ALTER TABLE curated_geo.feature_overlap
    ADD CONSTRAINT feature_overlap_feature_measure_check
    CHECK (feature_measure > 0);

ALTER TABLE curated_geo.feature_overlap
    ADD CONSTRAINT feature_overlap_ratio_check
    CHECK (overlap_ratio > 0 AND overlap_ratio <= 1);

-- ── 3. Index on feature_overlap_type_pair(target_type_id) ───────────────────
CREATE INDEX IF NOT EXISTS feature_overlap_type_pair_target_idx
ON curated_geo.feature_overlap_type_pair (target_type_id);
