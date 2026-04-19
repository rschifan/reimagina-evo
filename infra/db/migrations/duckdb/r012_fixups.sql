-- r012: DuckDB-side fixups matching the Postgres r012 migration.
--
-- 1. RTREE spatial index on curated_geo.feature(geometry).
--    The Postgres backend has feature_geometry_gist (r007); DuckDB lacked any
--    spatial index, causing full geometry scans on all spatial queries.
--
-- 2. CHECK constraint parity for curated_geo.feature_overlap.
--    Fresh DuckDB databases now get these inline in r009. DuckDB does not
--    support ALTER TABLE ... ADD CONSTRAINT for the existing table shape.
--
-- 3. B-tree index on feature_overlap_type_pair(target_type_id) for reverse
--    lookups — also missing from both backends' r009.

-- ── 1. Spatial index ────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS feature_geometry_rtree
ON curated_geo.feature USING RTREE (geometry);

-- ── 2. CHECK constraints on feature_overlap ─────────────────────────────────
-- No-op on DuckDB for already-created tables. Postgres remains the authoritative
-- enforcement backend, and fresh DuckDB DBs receive the inline constraints from
-- r009_feature_overlap.sql.

-- ── 3. Index on feature_overlap_type_pair(target_type_id) ───────────────────
CREATE INDEX IF NOT EXISTS feature_overlap_type_pair_target_idx
ON curated_geo.feature_overlap_type_pair (target_type_id);
