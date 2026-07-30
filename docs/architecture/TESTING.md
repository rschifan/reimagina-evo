# Testing

SQL-level contract tests for the Reimagina database schema. These tests verify that the triggers, constraints, indexes, and serving views enforce the guarantees described in the layer documents.

**Framework:** [`pgTAP`](https://pgtap.org/) — a TAP-compliant PostgreSQL testing framework. Tests run against a disposable PostgreSQL + PostGIS database seeded with the schema and minimal reference data.

⚠ **This suite is not in the repository.** No `.sql` test file exists and nothing invokes `pg_prove`; the cases below are a specification of the schema guarantees, not a suite you can run today. What does run is Python under `tests/` (`pyproject.toml:86`), split into `unit`, `contract`, `integration`, `conformance`, `parity`, `performance`, and `e2e`, driven by `make test`, `make test-integration`, and `make test-conformance` (`Makefile:43`, `:47`, `:51`).

**Related documents:** `DATA_LAYER.md`, `INDICATORS_LAYER.md`, `SCENARIOS_LAYER.md`, `JOB_LAYER.md`.

---

## Setup

```sql
-- Load pgTAP
CREATE EXTENSION IF NOT EXISTS pgtap;
CREATE EXTENSION IF NOT EXISTS postgis;

-- Build the schema by running the Alembic migrations (r001 → r031):
--   make migrate        # python -m apps.cli.app bootstrap-db  (Makefile:71-73)
-- See JOB_LAYER.md — Migration order — for how the job-layer tables interleave.
-- Then seed minimal reference data:

INSERT INTO curated_geo.data_source_catalog
  (id, name, provider, storage_type, integration_role, storage_uri, version)
VALUES
  ('11111111-0000-0000-0000-000000000001', 'Test Vector Source',   'TEST', 'vector',  'canonical_entity', 's3://test/v', '1'),
  ('11111111-0000-0000-0000-000000000002', 'Test Tabular Source',  'TEST', 'tabular', 'enrichment',       's3://test/t', '1'),
  ('11111111-0000-0000-0000-000000000003', 'Test Raster Source',   'TEST', 'raster',  'enrichment',       's3://test/r', '1'),
  ('11111111-0000-0000-0000-000000000004', 'Test Imagery Source',  'TEST', 'imagery', 'canonical_entity', 's3://test/i', '1');

INSERT INTO curated_geo.ingestion_run
  (id, source_id, orchestrator, orchestrator_run_id, transformation_step, code_version, status)
VALUES
  ('22222222-0000-0000-0000-000000000001',
   '11111111-0000-0000-0000-000000000001', 'manual', 'run-001', 'silver_to_curated', 'v1', 'succeeded'),
  ('22222222-0000-0000-0000-000000000002',
   '11111111-0000-0000-0000-000000000002', 'manual', 'run-002', 'silver_to_curated', 'v1', 'succeeded'),
  ('22222222-0000-0000-0000-000000000003',
   '11111111-0000-0000-0000-000000000003', 'manual', 'run-003', 'zonal_stats',       'v1', 'succeeded');

-- Helper: insert a minimal valid block feature
INSERT INTO curated_geo.feature
  (id, type_id, type_slug, role, geometry, attributes,
   source_id, source_record_ref, ingestion_run_id, dataset_version)
VALUES
  ('33333333-0000-0000-0000-000000000001',
   (SELECT id FROM curated_geo.feature_type_catalog WHERE type_slug = 'block' AND context = 'MEX'),
   'block', 'analysis_unit',
   ST_SetSRID(ST_MakeEnvelope(-99.14, 19.43, -99.13, 19.44), 4326),
   '{"area_m2": 5000}',
   '11111111-0000-0000-0000-000000000001', 'MX09010001001', '22222222-0000-0000-0000-000000000001', '2020');
```

---

## 0. Data Layer — Source Registry

### 0.1 integration_role validation

```sql
BEGIN;
SELECT plan(1);

SELECT throws_ok(
  $$
    INSERT INTO curated_geo.data_source_catalog
      (name, provider, storage_type, integration_role, storage_uri, version)
    VALUES
      ('Bad Source', 'TEST', 'raster', 'coverage',
       's3://test/bad', '1')
  $$,
  '23514',
  'Invalid integration_role must be rejected'
);

SELECT * FROM finish();
ROLLBACK;
```

---

## 1. Data Layer — Feature

### 1.1 Geometry family enforcement

```sql
BEGIN;
SELECT plan(5);

-- polygon type must reject a point geometry
SELECT throws_ok(
  $$
    INSERT INTO curated_geo.feature
      (type_id, type_slug, role, geometry, attributes,
       source_id, source_record_ref, ingestion_run_id, dataset_version)
    SELECT id, 'block', 'analysis_unit',
      ST_SetSRID(ST_MakePoint(-99.14, 19.43), 4326),
      '{}',
      '11111111-0000-0000-0000-000000000001', 'bad-geom', '22222222-0000-0000-0000-000000000001', '2020'
    FROM curated_geo.feature_type_catalog WHERE type_slug = 'block' AND context = 'MEX'
  $$,
  'P0001',
  'Polygon type must reject point geometry'
);

-- line type must reject a polygon geometry
SELECT throws_ok(
  $$
    INSERT INTO curated_geo.feature
      (type_id, type_slug, role, geometry, attributes,
       source_id, source_record_ref, ingestion_run_id, dataset_version)
    SELECT id, 'road', 'infrastructure',
      ST_SetSRID(ST_MakeEnvelope(-99.14, 19.43, -99.13, 19.44), 4326),
      '{}',
      '11111111-0000-0000-0000-000000000001', 'bad-geom-road', '22222222-0000-0000-0000-000000000001', '2020'
    FROM curated_geo.feature_type_catalog WHERE type_slug = 'road' AND context = 'global'
  $$,
  'P0001',
  'Line type must reject polygon geometry'
);

-- point type must reject a polygon geometry
SELECT throws_ok(
  $$
    INSERT INTO curated_geo.feature
      (type_id, type_slug, role, geometry, attributes,
       source_id, source_record_ref, ingestion_run_id, dataset_version)
    SELECT id, 'tree', 'detection',
      ST_SetSRID(ST_MakeEnvelope(-99.14, 19.43, -99.13, 19.44), 4326),
      '{}',
      '11111111-0000-0000-0000-000000000001', 'bad-geom-tree', '22222222-0000-0000-0000-000000000001', '2020'
    FROM curated_geo.feature_type_catalog WHERE type_slug = 'tree' AND context = 'global'
  $$,
  'P0001',
  'Point type must reject polygon geometry'
);

-- MultiPolygon must be accepted for polygon type (real-world common case)
SELECT lives_ok(
  $$
    INSERT INTO curated_geo.feature
      (type_id, type_slug, role, geometry, attributes,
       source_id, source_record_ref, ingestion_run_id, dataset_version)
    SELECT id, 'block', 'analysis_unit',
      ST_SetSRID(ST_Multi(ST_MakeEnvelope(-99.14, 19.43, -99.13, 19.44)), 4326),
      '{}',
      '11111111-0000-0000-0000-000000000001', 'multipolygon-ok', '22222222-0000-0000-0000-000000000001', '2020'
    FROM curated_geo.feature_type_catalog WHERE type_slug = 'block' AND context = 'MEX'
  $$,
  'MultiPolygon accepted for polygon type'
);

-- geometry family type accepts any geometry
SELECT lives_ok(
  $$
    INSERT INTO curated_geo.feature_type_catalog
      (context, type_slug, label, geometry_family, role)
    VALUES ('test', 'mixed_test', 'Mixed Test', 'geometry', 'poi');

    INSERT INTO curated_geo.feature
      (type_id, type_slug, role, geometry, attributes,
       source_id, source_record_ref, ingestion_run_id, dataset_version)
    SELECT id, 'mixed_test', 'poi',
      ST_SetSRID(ST_MakePoint(-99.14, 19.43), 4326),
      '{}',
      '11111111-0000-0000-0000-000000000001', 'any-geom-ok', '22222222-0000-0000-0000-000000000001', '2020'
    FROM curated_geo.feature_type_catalog WHERE type_slug = 'mixed_test' AND context = 'test'
  $$,
  'geometry family accepts any geometry type'
);

SELECT * FROM finish();
ROLLBACK;
```

---

### 1.2 Hierarchy rules

```sql
BEGIN;
SELECT plan(5);

-- Self-parenting must be rejected
SELECT throws_ok(
  $$
    UPDATE curated_geo.feature
    SET parent_id = id
    WHERE id = '33333333-0000-0000-0000-000000000001'
  $$,
  'P0001',
  'Self-parenting must be rejected'
);

-- Non-hierarchical types (admin_level IS NULL) must not accept a parent_id
SELECT throws_ok(
  $$
    INSERT INTO curated_geo.feature
      (type_id, type_slug, role, parent_id, geometry, attributes,
       source_id, source_record_ref, ingestion_run_id, dataset_version)
    SELECT id, 'tree', 'detection',
      '33333333-0000-0000-0000-000000000001',
      ST_SetSRID(ST_MakePoint(-99.14, 19.43), 4326),
      '{}',
      '11111111-0000-0000-0000-000000000001', 'tree-with-parent', '22222222-0000-0000-0000-000000000001', '2020'
    FROM curated_geo.feature_type_catalog WHERE type_slug = 'tree' AND context = 'global'
  $$,
  'P0001',
  'Non-hierarchical type must not accept parent_id'
);

-- Parent and child must share the same context
SELECT throws_ok(
  $$
    INSERT INTO curated_geo.feature_type_catalog
      (context, type_slug, label, geometry_family, role, admin_level)
    VALUES ('OTHER', 'other_block', 'Other Block', 'polygon', 'analysis_unit', 5);

    INSERT INTO curated_geo.feature
      (type_id, type_slug, role, parent_id, geometry, attributes,
       source_id, source_record_ref, ingestion_run_id, dataset_version)
    SELECT id, 'other_block', 'analysis_unit',
      '33333333-0000-0000-0000-000000000001',
      ST_SetSRID(ST_MakeEnvelope(-99.14, 19.43, -99.13, 19.44), 4326),
      '{}',
      '11111111-0000-0000-0000-000000000001', 'cross-context', '22222222-0000-0000-0000-000000000001', '2020'
    FROM curated_geo.feature_type_catalog WHERE type_slug = 'other_block' AND context = 'OTHER'
  $$,
  'P0001',
  'Parent and child must share the same context'
);

-- Parent admin_level must be lower (coarser) than child admin_level
-- e.g., block (admin_level=5) cannot be parent of municipality (admin_level=2)
SELECT throws_ok(
  $$
    INSERT INTO curated_geo.feature
      (id, type_id, type_slug, role, geometry, attributes,
       source_id, source_record_ref, ingestion_run_id, dataset_version)
    SELECT '33333333-0000-0000-0000-000000000099',
      id, 'municipality', 'context',
      ST_SetSRID(ST_MakeEnvelope(-99.2, 19.3, -99.0, 19.6), 4326),
      '{}',
      '11111111-0000-0000-0000-000000000001', 'mun-001', '22222222-0000-0000-0000-000000000001', '2020'
    FROM curated_geo.feature_type_catalog WHERE type_slug = 'municipality' AND context = 'MEX';

    -- block as parent of municipality is wrong: level 5 > level 2
    UPDATE curated_geo.feature
    SET parent_id = '33333333-0000-0000-0000-000000000001'  -- block
    WHERE id = '33333333-0000-0000-0000-000000000099';       -- municipality
  $$,
  'P0001',
  'Parent admin_level must be lower than child admin_level'
);

-- type_slug and role must reflect type_id (trigger denormalization check)
SELECT throws_ok(
  $$
    UPDATE curated_geo.feature
    SET type_slug = 'road'
    WHERE id = '33333333-0000-0000-0000-000000000001'
  $$,
  'P0001',
  'Manual edit to type_slug must be rejected when it conflicts with type_id'
);

SELECT * FROM finish();
ROLLBACK;
```

---

### 1.3 Lineage: source_id / ingestion_run_id mismatch

```sql
BEGIN;
SELECT plan(1);

-- ingestion_run_id must belong to the same source as source_id
SELECT throws_ok(
  $$
    INSERT INTO curated_geo.feature
      (type_id, type_slug, role, geometry, attributes,
       source_id, source_record_ref, ingestion_run_id, dataset_version)
    SELECT ftc.id, 'block', 'analysis_unit',
      ST_SetSRID(ST_MakeEnvelope(-99.14, 19.43, -99.13, 19.44), 4326),
      '{}',
      '11111111-0000-0000-0000-000000000001',  -- vector source
      'mismatch-ref',
      '22222222-0000-0000-0000-000000000002',  -- tabular ingestion run ← mismatch
      '2020'
    FROM curated_geo.feature_type_catalog ftc WHERE type_slug = 'block' AND context = 'MEX'
  $$,
  'P0001',
  'source_id and ingestion_run_id must point to the same dataset'
);

SELECT * FROM finish();
ROLLBACK;
```

---

## 2. Data Layer — Observation

### 2.1 One current value per (feature, attribute_key)

```sql
BEGIN;
SELECT plan(2);

-- Insert first current value — must succeed
SELECT lives_ok(
  $$
    INSERT INTO curated_geo.observation
      (feature_id, attribute_key, value_type, value_numeric,
       source_id, ingestion_run_id, is_current, valid_from)
    VALUES
      ('33333333-0000-0000-0000-000000000001',
       'population_total', 'numeric', 1200,
       '11111111-0000-0000-0000-000000000002',
       '22222222-0000-0000-0000-000000000002',
       true, now())
  $$,
  'First current observation must be accepted'
);

-- Insert second current value for same (feature, key) without closing the first — must fail
SELECT throws_ok(
  $$
    INSERT INTO curated_geo.observation
      (feature_id, attribute_key, value_type, value_numeric,
       source_id, ingestion_run_id, is_current, valid_from)
    VALUES
      ('33333333-0000-0000-0000-000000000001',
       'population_total', 'numeric', 1350,
       '11111111-0000-0000-0000-000000000002',
       '22222222-0000-0000-0000-000000000002',
       true, now())
  $$,
  '23505',
  'Duplicate current observation for same (feature, attribute_key) must be rejected'
);

SELECT * FROM finish();
ROLLBACK;
```

### 2.2 SCD Type 2 — historical rows preserved on refresh

```sql
BEGIN;
SELECT plan(3);

-- Insert initial current value
INSERT INTO curated_geo.observation
  (feature_id, attribute_key, value_type, value_numeric,
   source_id, ingestion_run_id, is_current, valid_from)
VALUES
  ('33333333-0000-0000-0000-000000000001',
   'population_total', 'numeric', 1200,
   '11111111-0000-0000-0000-000000000002',
   '22222222-0000-0000-0000-000000000002',
   true, now() - interval '30 days');

-- Close prior current value
UPDATE curated_geo.observation
SET is_current = false, valid_to = now()
WHERE feature_id   = '33333333-0000-0000-0000-000000000001'
  AND attribute_key = 'population_total'
  AND is_current;

-- Insert new current value
INSERT INTO curated_geo.observation
  (feature_id, attribute_key, value_type, value_numeric,
   source_id, ingestion_run_id, is_current, valid_from)
VALUES
  ('33333333-0000-0000-0000-000000000001',
   'population_total', 'numeric', 1350,
   '11111111-0000-0000-0000-000000000002',
   '22222222-0000-0000-0000-000000000002',
   true, now());

-- Exactly one current row
SELECT is(
  (SELECT count(*)::int FROM curated_geo.observation
   WHERE feature_id   = '33333333-0000-0000-0000-000000000001'
     AND attribute_key = 'population_total'
     AND is_current),
  1,
  'Exactly one current observation after refresh'
);

-- Exactly one historical row
SELECT is(
  (SELECT count(*)::int FROM curated_geo.observation
   WHERE feature_id   = '33333333-0000-0000-0000-000000000001'
     AND attribute_key = 'population_total'
     AND NOT is_current),
  1,
  'Prior observation preserved as historical row'
);

-- Historical row has valid_to set
SELECT isnt(
  (SELECT valid_to FROM curated_geo.observation
   WHERE feature_id   = '33333333-0000-0000-0000-000000000001'
     AND attribute_key = 'population_total'
     AND NOT is_current),
  NULL,
  'Historical observation must have valid_to set'
);

SELECT * FROM finish();
ROLLBACK;
```

### 2.3 Value column integrity

```sql
BEGIN;
SELECT plan(3);

-- value_type = 'numeric' but only value_text populated — trigger must reject
SELECT throws_ok(
  $$
    INSERT INTO curated_geo.observation
      (feature_id, attribute_key, value_type, value_text,
       source_id, ingestion_run_id, is_current, valid_from)
    VALUES
      ('33333333-0000-0000-0000-000000000001',
       'bad_column', 'numeric', 'not-a-number',
       '11111111-0000-0000-0000-000000000002',
       '22222222-0000-0000-0000-000000000002',
       true, now())
  $$,
  'P0001',
  'value_type = numeric must reject a text value in value_text'
);

-- No value column populated at all — trigger must reject
SELECT throws_ok(
  $$
    INSERT INTO curated_geo.observation
      (feature_id, attribute_key, value_type,
       source_id, ingestion_run_id, is_current, valid_from)
    VALUES
      ('33333333-0000-0000-0000-000000000001',
       'empty_value', 'numeric',
       '11111111-0000-0000-0000-000000000002',
       '22222222-0000-0000-0000-000000000002',
       true, now())
  $$,
  'P0001',
  'Observation with no value column populated must be rejected'
);

-- Multiple value columns populated — trigger must reject
SELECT throws_ok(
  $$
    INSERT INTO curated_geo.observation
      (feature_id, attribute_key, value_type, value_numeric, value_text,
       source_id, ingestion_run_id, is_current, valid_from)
    VALUES
      ('33333333-0000-0000-0000-000000000001',
       'too_many_values', 'numeric', 10, 'ten',
       '11111111-0000-0000-0000-000000000002',
       '22222222-0000-0000-0000-000000000002',
       true, now())
  $$,
  'P0001',
  'Observation with multiple value columns populated must be rejected'
);

SELECT * FROM finish();
ROLLBACK;
```

### 2.4 Lineage mismatch

```sql
BEGIN;
SELECT plan(1);

SELECT throws_ok(
  $$
    INSERT INTO curated_geo.observation
      (feature_id, attribute_key, value_type, value_numeric,
       source_id, ingestion_run_id, is_current, valid_from)
    VALUES
      ('33333333-0000-0000-0000-000000000001',
       'population_total', 'numeric', 1200,
       '11111111-0000-0000-0000-000000000001',   -- vector source
       '22222222-0000-0000-0000-000000000002',   -- tabular run ← mismatch
       true, now())
  $$,
  'P0001',
  'Observation source_id and ingestion_run_id must point to the same dataset'
);

SELECT * FROM finish();
ROLLBACK;
```

---

## 3. Data Layer — File Asset

```sql
BEGIN;
SELECT plan(1);

-- file_asset must reject a source_id whose storage_type is 'vector'
SELECT throws_ok(
  $$
    INSERT INTO curated_geo.file_asset
      (source_id, ingestion_run_id, storage_uri, format, dataset_version)
    VALUES
      ('11111111-0000-0000-0000-000000000001',  -- vector source ← wrong
       '22222222-0000-0000-0000-000000000001',
       's3://test/bad.tif', 'COG', '1')
  $$,
  'P0001',
  'file_asset must reject non-raster/imagery/point_cloud source_id'
);

SELECT * FROM finish();
ROLLBACK;
```

---

## 4. Data Layer — AOI and aoi_feature

```sql
BEGIN;
SELECT plan(7);

-- Setup: create a project and AOI
INSERT INTO app_core.project (id, name) VALUES ('44444444-0000-0000-0000-000000000001', 'Test Project');

-- Non-polygon AOI geometry must be rejected
SELECT throws_ok(
  $$
    INSERT INTO curated_geo.area_of_interest (id, project_id, name, geometry)
    VALUES (
      '55555555-0000-0000-0000-000000000099',
      '44444444-0000-0000-0000-000000000001',
      'Bad AOI',
      ST_SetSRID(ST_MakeLine(ST_MakePoint(-99.15, 19.42), ST_MakePoint(-99.12, 19.45)), 4326)
    )
  $$,
  '23514',
  'AOI geometry must be polygonal'
);

INSERT INTO curated_geo.area_of_interest (id, project_id, name, geometry)
VALUES (
  '55555555-0000-0000-0000-000000000001',
  '44444444-0000-0000-0000-000000000001',
  'Test AOI',
  ST_SetSRID(ST_MakeEnvelope(-99.15, 19.42, -99.12, 19.45), 4326)
);

-- Setup: create a line feature and a point feature
INSERT INTO curated_geo.feature
  (id, type_id, type_slug, role, geometry, attributes,
   source_id, source_record_ref, ingestion_run_id, dataset_version)
SELECT
  '33333333-0000-0000-0000-000000000010',
  id, 'road', 'infrastructure',
  ST_SetSRID(ST_MakeLine(ST_MakePoint(-99.145, 19.425), ST_MakePoint(-99.125, 19.445)), 4326),
  '{}',
  '11111111-0000-0000-0000-000000000001', 'road-001', '22222222-0000-0000-0000-000000000001', '2020'
FROM curated_geo.feature_type_catalog WHERE type_slug = 'road' AND context = 'global';

INSERT INTO curated_geo.feature
  (id, type_id, type_slug, role, geometry, attributes,
   source_id, source_record_ref, ingestion_run_id, dataset_version)
SELECT
  '33333333-0000-0000-0000-000000000011',
  id, 'tree', 'detection',
  ST_SetSRID(ST_MakePoint(-99.14, 19.43), 4326),
  '{}',
  '11111111-0000-0000-0000-000000000001', 'tree-001', '22222222-0000-0000-0000-000000000001', '2020'
FROM curated_geo.feature_type_catalog WHERE type_slug = 'tree' AND context = 'global';

-- Polygon feature: overlap_basis must be 'area'
SELECT lives_ok(
  $$
    INSERT INTO curated_geo.aoi_feature
      (aoi_id, feature_id, overlap_basis, overlap_measure, feature_measure, overlap_ratio)
    VALUES
      ('55555555-0000-0000-0000-000000000001',
       '33333333-0000-0000-0000-000000000001',
       'area', 4000, 5000, 0.80)
  $$,
  'Polygon feature with overlap_basis = area must be accepted'
);

-- Line feature: overlap_basis must be 'length'
SELECT lives_ok(
  $$
    INSERT INTO curated_geo.aoi_feature
      (aoi_id, feature_id, overlap_basis, overlap_measure, feature_measure, overlap_ratio)
    VALUES
      ('55555555-0000-0000-0000-000000000001',
       '33333333-0000-0000-0000-000000000010',
       'length', 120, 200, 0.60)
  $$,
  'Line feature with overlap_basis = length must be accepted'
);

-- Line feature must reject area-based overlap semantics
SELECT throws_ok(
  $$
    INSERT INTO curated_geo.aoi_feature
      (aoi_id, feature_id, overlap_basis, overlap_measure, feature_measure, overlap_ratio)
    VALUES
      ('55555555-0000-0000-0000-000000000001',
       '33333333-0000-0000-0000-000000000010',
       'area', 120, 200, 0.60)
  $$,
  'P0001',
  'Line feature must reject overlap_basis = area'
);

-- overlap_ratio must not exceed 1.0
SELECT throws_ok(
  $$
    INSERT INTO curated_geo.aoi_feature
      (aoi_id, feature_id, overlap_basis, overlap_measure, feature_measure, overlap_ratio)
    VALUES
      ('55555555-0000-0000-0000-000000000001',
       '33333333-0000-0000-0000-000000000001',
       'area', 5000, 4000, 1.25)
  $$,
  '23514',
  'overlap_ratio > 1.0 must be rejected'
);

-- overlap_measure must not exceed feature_measure
SELECT throws_ok(
  $$
    INSERT INTO curated_geo.aoi_feature
      (aoi_id, feature_id, overlap_basis, overlap_measure, feature_measure, overlap_ratio)
    VALUES
      ('55555555-0000-0000-0000-000000000001',
       '33333333-0000-0000-0000-000000000001',
       'area', 6000, 5000, 0.80)
  $$,
  '23514',
  'overlap_measure > feature_measure must be rejected'
);

-- Point presence: all three measures must be 1
SELECT throws_ok(
  $$
    INSERT INTO curated_geo.aoi_feature
      (aoi_id, feature_id, overlap_basis, overlap_measure, feature_measure, overlap_ratio)
    VALUES
      ('55555555-0000-0000-0000-000000000001',
       '33333333-0000-0000-0000-000000000011',
       'presence', 1, 1, 0.5)  -- ratio must be 1 for presence
  $$,
  '23514',
  'Point presence overlap must have ratio = 1'
);

SELECT * FROM finish();
ROLLBACK;
```

---

## 5. Indicators Layer — Catalog and Dependency

### 5.1 Catalog constraints

```sql
BEGIN;
SELECT plan(3);

-- composite indicator must have model_family = NULL
SELECT throws_ok(
  $$
    INSERT INTO curated_geo.indicator_catalog
      (slug, name, model_family, computation_kind, value_type, unit,
       aggregation_method, applies_to)
    VALUES
      ('bad_composite', 'Bad Composite', 'accessibility', 'composite',
       'numeric', 'score_0_1', 'weighted_mean', ARRAY['block'])
  $$,
  '23514',
  'Composite indicator must have model_family = NULL'
);

-- direct indicator must have model_family set
SELECT throws_ok(
  $$
    INSERT INTO curated_geo.indicator_catalog
      (slug, name, model_family, computation_kind, value_type, unit,
       aggregation_method, applies_to)
    VALUES
      ('bad_direct', 'Bad Direct', NULL, 'direct',
       'numeric', 'score_0_1', 'weighted_mean', ARRAY['block'])
  $$,
  '23514',
  'Direct indicator must have model_family set'
);

-- valid_min must not exceed valid_max
SELECT throws_ok(
  $$
    INSERT INTO curated_geo.indicator_catalog
      (slug, name, model_family, computation_kind, value_type,
       valid_min, valid_max, aggregation_method, applies_to)
    VALUES
      ('bad_range', 'Bad Range', 'accessibility', 'direct', 'numeric',
       1.0, 0.5, 'mean', ARRAY['block'])
  $$,
  '23514',
  'valid_min > valid_max must be rejected'
);

SELECT * FROM finish();
ROLLBACK;
```

### 5.2 Dependency graph constraints

```sql
BEGIN;
SELECT plan(4);

-- Seed two catalog entries
INSERT INTO curated_geo.indicator_catalog
  (id, slug, name, model_family, computation_kind, value_type,
   valid_min, valid_max, aggregation_method, applies_to)
VALUES
  ('66666666-0000-0000-0000-000000000001',
   'test_walkability', 'Test Walkability',
   'accessibility', 'direct', 'numeric', 0, 1, 'weighted_mean', ARRAY['block']),
  ('66666666-0000-0000-0000-000000000002',
   'test_composite', 'Test Composite',
   NULL, 'composite', 'numeric', 0, 1, 'weighted_mean', ARRAY['block']);

-- Self-dependency must be rejected
SELECT throws_ok(
  $$
    INSERT INTO curated_geo.indicator_dependency (indicator_id, depends_on_id, weight)
    VALUES ('66666666-0000-0000-0000-000000000002',
            '66666666-0000-0000-0000-000000000002', 1.0)
  $$,
  '23514',
  'Self-dependency must be rejected'
);

-- Direct indicator cannot be the left-hand side (indicator_id must be composite)
SELECT throws_ok(
  $$
    INSERT INTO curated_geo.indicator_dependency (indicator_id, depends_on_id)
    VALUES ('66666666-0000-0000-0000-000000000001',   -- direct ← wrong
            '66666666-0000-0000-0000-000000000002')
  $$,
  'P0001',
  'Non-composite indicator cannot be left-hand side of dependency'
);

-- Weights must be between 0 and 1
SELECT throws_ok(
  $$
    INSERT INTO curated_geo.indicator_dependency (indicator_id, depends_on_id, weight)
    VALUES ('66666666-0000-0000-0000-000000000002',
            '66666666-0000-0000-0000-000000000001', 1.5)
  $$,
  '23514',
  'Weight > 1 must be rejected'
);

-- Valid dependency must be accepted
SELECT lives_ok(
  $$
    INSERT INTO curated_geo.indicator_dependency (indicator_id, depends_on_id, weight)
    VALUES ('66666666-0000-0000-0000-000000000002',
            '66666666-0000-0000-0000-000000000001', 1.0)
  $$,
  'Valid dependency with weight = 1.0 must be accepted'
);

SELECT * FROM finish();
ROLLBACK;
```

### 5.3 Cyclic dependency detection

```sql
BEGIN;
SELECT plan(1);

INSERT INTO curated_geo.indicator_catalog
  (id, slug, name, computation_kind, value_type, aggregation_method, applies_to)
VALUES
  ('77777777-0000-0000-0000-000000000001', 'cycle_a', 'Cycle A', 'composite', 'numeric', 'mean', ARRAY['block']),
  ('77777777-0000-0000-0000-000000000002', 'cycle_b', 'Cycle B', 'composite', 'numeric', 'mean', ARRAY['block']);

INSERT INTO curated_geo.indicator_dependency (indicator_id, depends_on_id)
VALUES ('77777777-0000-0000-0000-000000000001', '77777777-0000-0000-0000-000000000002');

-- Creating the reverse link would form a cycle A → B → A
SELECT throws_ok(
  $$
    INSERT INTO curated_geo.indicator_dependency (indicator_id, depends_on_id)
    VALUES ('77777777-0000-0000-0000-000000000002', '77777777-0000-0000-0000-000000000001')
  $$,
  'P0001',
  'Cyclic dependency must be rejected'
);

SELECT * FROM finish();
ROLLBACK;
```

---

## 6. Indicators Layer — Result

### 6.1 Value column and quality flag rules

```sql
BEGIN;
SELECT plan(6);

-- Setup. scenario_id is a bare uuid with no FK (r016:7-10), so no scenario
-- row is created — or creatable — here.
INSERT INTO jobs_runtime.job_run (id, scenario_id, model_family, status, code_version)
VALUES ('99999999-0000-0000-0000-000000000001', '88888888-0000-0000-0000-000000000001', 'accessibility', 'succeeded', 'v1');
INSERT INTO curated_geo.indicator_catalog
  (id, slug, name, model_family, computation_kind, value_type, valid_min, valid_max, aggregation_method, applies_to)
VALUES
  ('66666666-0000-0000-0000-000000000010', 'test_walk', 'Test Walk', 'accessibility', 'direct', 'numeric', 0, 1, 'mean', ARRAY['block']);

-- measured result with valid value: must succeed
SELECT lives_ok(
  $$
    INSERT INTO curated_geo.indicator_result
      (feature_id, feature_type, scenario_id, indicator_id, indicator_slug,
       job_run_id, value_numeric, quality_flag, status)
    VALUES
      ('33333333-0000-0000-0000-000000000001', 'block',
       '88888888-0000-0000-0000-000000000001',
       '66666666-0000-0000-0000-000000000010', 'test_walk',
       '99999999-0000-0000-0000-000000000001', 0.75, 'measured', 'current')
  $$,
  'Valid measured result must be accepted'
);

-- measured result with no value: must fail
SELECT throws_ok(
  $$
    INSERT INTO curated_geo.indicator_result
      (feature_id, feature_type, scenario_id, indicator_id, indicator_slug,
       job_run_id, quality_flag, status)
    VALUES
      ('33333333-0000-0000-0000-000000000001', 'block',
       '88888888-0000-0000-0000-000000000001',
       '66666666-0000-0000-0000-000000000010', 'test_walk',
       '99999999-0000-0000-0000-000000000001', 'measured', 'current')
  $$,
  'P0001',
  'Measured result without a value must be rejected'
);

-- value outside valid_max: must fail
SELECT throws_ok(
  $$
    INSERT INTO curated_geo.indicator_result
      (feature_id, feature_type, scenario_id, indicator_id, indicator_slug,
       job_run_id, value_numeric, quality_flag, status)
    VALUES
      ('33333333-0000-0000-0000-000000000001', 'block',
       '88888888-0000-0000-0000-000000000001',
       '66666666-0000-0000-0000-000000000010', 'test_walk',
       '99999999-0000-0000-0000-000000000001', 1.5, 'measured', 'current')
  $$,
  'P0001',
  'Value outside valid_max must be rejected'
);

-- missing result without missing_reason: must fail
SELECT throws_ok(
  $$
    INSERT INTO curated_geo.indicator_result
      (feature_id, feature_type, scenario_id, indicator_id, indicator_slug,
       job_run_id, quality_flag, status)
    VALUES
      ('33333333-0000-0000-0000-000000000001', 'block',
       '88888888-0000-0000-0000-000000000001',
       '66666666-0000-0000-0000-000000000010', 'test_walk',
       '99999999-0000-0000-0000-000000000001', 'missing', 'current')
  $$,
  'P0001',
  'Missing result without missing_reason must be rejected'
);

-- not_applicable result with a value: must fail
SELECT throws_ok(
  $$
    INSERT INTO curated_geo.indicator_result
      (feature_id, feature_type, scenario_id, indicator_id, indicator_slug,
       job_run_id, value_numeric, quality_flag, status)
    VALUES
      ('33333333-0000-0000-0000-000000000001', 'block',
       '88888888-0000-0000-0000-000000000001',
       '66666666-0000-0000-0000-000000000010', 'test_walk',
       '99999999-0000-0000-0000-000000000001', 0.5, 'not_applicable', 'current')
  $$,
  'P0001',
  'not_applicable result with a value must be rejected'
);

-- scenario_id mismatch between indicator_result and job_run: must fail
INSERT INTO jobs_runtime.job_run (id, scenario_id, model_family, status, code_version)
VALUES ('99999999-0000-0000-0000-000000000002', NULL, 'accessibility', 'succeeded', 'v1');
-- job_run has scenario_id = NULL but indicator_result claims a scenario_id

SELECT throws_ok(
  $$
    INSERT INTO curated_geo.indicator_result
      (feature_id, feature_type, scenario_id, indicator_id, indicator_slug,
       job_run_id, value_numeric, quality_flag, status)
    VALUES
      ('33333333-0000-0000-0000-000000000001', 'block',
       '88888888-0000-0000-0000-000000000001',     -- scenario UUID
       '66666666-0000-0000-0000-000000000010', 'test_walk',
       '99999999-0000-0000-0000-000000000002',     -- job_run with scenario_id = NULL ← mismatch
       0.70, 'measured', 'current')
  $$,
  'P0001',
  'scenario_id mismatch between indicator_result and job_run must be rejected'
);

SELECT * FROM finish();
ROLLBACK;
```

### 6.2 Uniqueness — one current result per (feature, scenario, indicator)

```sql
BEGIN;
SELECT plan(2);

-- Setup
INSERT INTO app_core.project (id, name)
VALUES ('44444444-0000-0000-0000-000000000002', 'Test Project 2');

INSERT INTO curated_geo.indicator_catalog
  (id, slug, name, model_family, computation_kind, value_type, valid_min, valid_max, aggregation_method, applies_to)
VALUES
  ('66666666-0000-0000-0000-000000000010', 'test_walk', 'Test Walk',
   'accessibility', 'direct', 'numeric', 0, 1, 'mean', ARRAY['block']);

INSERT INTO jobs_runtime.job_run (id, scenario_id, model_family, status, code_version)
VALUES ('99999999-0000-0000-0000-000000000002', NULL, 'accessibility', 'succeeded', 'v1');

-- Two current base-state results for the same (feature, indicator): must fail
SELECT throws_ok(
  $$
    INSERT INTO curated_geo.indicator_result
      (feature_id, feature_type, scenario_id, indicator_id, indicator_slug,
       job_run_id, value_numeric, quality_flag, status)
    VALUES
      ('33333333-0000-0000-0000-000000000001', 'block', NULL,
       '66666666-0000-0000-0000-000000000010', 'test_walk',
       '99999999-0000-0000-0000-000000000002', 0.80, 'measured', 'current'),
      ('33333333-0000-0000-0000-000000000001', 'block', NULL,
       '66666666-0000-0000-0000-000000000010', 'test_walk',
       '99999999-0000-0000-0000-000000000002', 0.70, 'measured', 'current')
  $$,
  '23505',
  'Duplicate current base-state result must be rejected'
);

-- Marking one stale then inserting new current must succeed
SELECT lives_ok(
  $$
    UPDATE curated_geo.indicator_result
    SET status = 'stale'
    WHERE feature_id    = '33333333-0000-0000-0000-000000000001'
      AND scenario_id   IS NULL
      AND indicator_id  = '66666666-0000-0000-0000-000000000010'
      AND status        = 'current';

    INSERT INTO curated_geo.indicator_result
      (feature_id, feature_type, scenario_id, indicator_id, indicator_slug,
       job_run_id, value_numeric, quality_flag, status)
    VALUES
      ('33333333-0000-0000-0000-000000000001', 'block', NULL,
       '66666666-0000-0000-0000-000000000010', 'test_walk',
       '99999999-0000-0000-0000-000000000002', 0.80, 'measured', 'current')
  $$,
  'New current result after staling previous must be accepted'
);

SELECT * FROM finish();
ROLLBACK;
```

---

## 7. Job Layer

```sql
BEGIN;
SELECT plan(3);

-- No scenario row: jobs_runtime.job_run.scenario_id is a plain uuid with no
-- foreign key (r016:7-10, r016:32), so any uuid is accepted.

-- Invalid status must be rejected
SELECT throws_ok(
  $$
    INSERT INTO jobs_runtime.job_run (scenario_id, model_family, status, code_version)
    VALUES ('88888888-0000-0000-0000-000000000002', 'accessibility', 'processing', 'v1')
  $$,
  '23514',
  'Invalid job_run status must be rejected'
);

-- completed_at before started_at must be rejected
SELECT throws_ok(
  $$
    INSERT INTO jobs_runtime.job_run
      (scenario_id, model_family, status, code_version, started_at, completed_at)
    VALUES
      ('88888888-0000-0000-0000-000000000002', 'accessibility', 'failed', 'v1',
       now(), now() - interval '5 minutes')
  $$,
  '23514',
  'completed_at before started_at must be rejected'
);

-- Null scenario_id is valid (base city computation)
SELECT lives_ok(
  $$
    INSERT INTO jobs_runtime.job_run (scenario_id, model_family, status, code_version)
    VALUES (NULL, 'accessibility', 'queued', 'v1')
  $$,
  'job_run with scenario_id = NULL must be accepted (base city computation)'
);

SELECT * FROM finish();
ROLLBACK;
```

---

## 8. Scenarios Layer

A scenario has no table of its own — it is the set of `curated_geo.scenario_override`
rows carrying its uuid (r030). The constraints to assert are therefore on that table.

```sql
BEGIN;
SELECT plan(4);

-- One typed value per override: exactly one of value_numeric / value_text.
SELECT lives_ok(
  $$
    INSERT INTO curated_geo.scenario_override
      (scenario_id, feature_id, attribute_key, value_numeric)
    VALUES ('88888888-0000-0000-0000-000000000003',
            '33333333-0000-0000-0000-000000000001', 'floors', 10)
  $$,
  'Override with exactly one typed value must be accepted'
);

SELECT throws_ok(
  $$
    INSERT INTO curated_geo.scenario_override
      (scenario_id, feature_id, attribute_key, value_numeric, value_text)
    VALUES ('88888888-0000-0000-0000-000000000003',
            '33333333-0000-0000-0000-000000000001', 'land_use', 5, 'green_area')
  $$,
  '23514',
  'Override with both value_numeric and value_text must be rejected'
);

SELECT throws_ok(
  $$
    INSERT INTO curated_geo.scenario_override
      (scenario_id, feature_id, attribute_key)
    VALUES ('88888888-0000-0000-0000-000000000003',
            '33333333-0000-0000-0000-000000000001', 'land_use')
  $$,
  '23514',
  'Override with neither typed value must be rejected'
);

-- One override per (scenario, feature, attribute) — the upsert key.
SELECT throws_ok(
  $$
    INSERT INTO curated_geo.scenario_override
      (scenario_id, feature_id, attribute_key, value_numeric)
    VALUES ('88888888-0000-0000-0000-000000000003',
            '33333333-0000-0000-0000-000000000001', 'floors', 12)
  $$,
  '23505',
  'Duplicate (scenario_id, feature_id, attribute_key) must be rejected'
);

SELECT * FROM finish();
ROLLBACK;
```

There is no scenario-status test and no configuration test: neither
`app_core.scenario` nor `app_core.configuration` exists, and `app_core` holds
only `project` (r001:27).

---

## 9. Serving Layer

```sql
BEGIN;
SELECT plan(4);

-- feature_v must merge feature.attributes with current observations
-- (Assumes the block and observation from earlier fixtures are present)

SELECT is(
  (SELECT (attributes->>'population_total')::int
   FROM serving_geo.feature_v
   WHERE id = '33333333-0000-0000-0000-000000000001'),
  1200,
  'feature_v must surface current observation value merged into attributes'
);

-- feature_v must preserve base feature.attributes when no observation exists for a key
SELECT is(
  (SELECT (attributes->>'area_m2')::int
   FROM serving_geo.feature_v
   WHERE id = '33333333-0000-0000-0000-000000000001'),
  5000,
  'feature_v must preserve feature.attributes keys not overridden by observations'
);

-- current_indicator_v must not return stale rows
-- (Assumes a stale indicator_result row exists for the test block)
SELECT is(
  (SELECT count(*)::int
   FROM serving_geo.current_indicator_v
   WHERE feature_id = '33333333-0000-0000-0000-000000000001'
     AND status IS DISTINCT FROM 'current'),
  0,
  'current_indicator_v must not return non-current rows'
);

-- Role-based view: analysis_units_v must not return infrastructure features
SELECT is(
  (SELECT count(*)::int
   FROM serving_geo.analysis_units_v
   WHERE role != 'analysis_unit'),
  0,
  'analysis_units_v must only contain features with role = analysis_unit'
);

SELECT * FROM finish();
ROLLBACK;
```

---

## 10. Performance Checks

These are not pgTAP assertions — run manually after loading a representative dataset. Pass criteria: no sequential scan on tables larger than ~10k rows; all documented indexes used.

```sql
-- Feature geometry lookup within AOI
EXPLAIN (ANALYZE, BUFFERS)
SELECT f.id, f.geometry
FROM serving_geo.analysis_units_v f
WHERE ST_Intersects(f.geometry, ST_MakeEnvelope(-99.15, 19.42, -99.12, 19.45, 4326));
-- Expected: Index Scan using feature_geometry_gist or feature_block_geom_idx

-- Current indicators for a scenario
EXPLAIN (ANALYZE, BUFFERS)
SELECT *
FROM serving_geo.current_indicator_v
WHERE scenario_id = :scenario_id
  AND indicator_slug = 'accessibility_walkability';
-- Expected: Index Scan using ir_scenario_slug_idx

-- Base-state vs scenario comparison
EXPLAIN (ANALYZE, BUFFERS)
SELECT base.value_numeric, scen.value_numeric
FROM serving_geo.current_indicator_v base
JOIN serving_geo.current_indicator_v scen
  ON scen.feature_id = base.feature_id
 AND scen.indicator_slug = base.indicator_slug
WHERE base.scenario_id IS NULL
  AND scen.scenario_id  = :scenario_id
  AND base.indicator_slug = 'accessibility_walkability';
-- Expected: Index Scan using ir_base_slug_idx + ir_scenario_slug_idx

-- Current observations for a feature
EXPLAIN (ANALYZE, BUFFERS)
SELECT attribute_key, value_numeric
FROM curated_geo.observation
WHERE feature_id  = :feature_id
  AND is_current;
-- Expected: Index Scan using obs_feature_key_idx (partial WHERE is_current)

-- Stale indicator detection
EXPLAIN (ANALYZE, BUFFERS)
SELECT feature_id, indicator_slug
FROM curated_geo.indicator_result
WHERE status = 'stale';
-- Expected: Bitmap Index Scan using ir_stale_idx
```

---

## Running the tests

Nothing here runs today — there is no `.sql` test file in the tree and no
`pg_prove` target. Wiring the suite up would take:

```bash
# Start a disposable test database
createdb reimagina_test
psql reimagina_test -c "CREATE EXTENSION IF NOT EXISTS pgtap; CREATE EXTENSION IF NOT EXISTS postgis;"

# Apply the schema — Alembic migrations, not a build script
DATABASE_URL=postgresql:///reimagina_test make migrate

# Run all tests (once the cases above are extracted into .sql files)
pg_prove -d reimagina_test tests/sql/*.sql
```

The Python suite that does run:

```bash
make test              # unit + contract
make test-integration  # needs Postgres + PostGIS
make test-conformance  # asserts the Orchestrator port contract
```
