# Testing

SQL-level contract tests for the database schema. These tests verify that the triggers, constraints, indexes, and serving views enforce the guarantees described in the layer documents.

**Framework:** [`pgTAP`](https://pgtap.org/) — a TAP-compliant PostgreSQL testing framework. Tests run against a disposable PostgreSQL + PostGIS database seeded with the schema and minimal reference data.

⚠ **This suite is not in the repository.** No `.sql` test file exists and nothing invokes `pg_prove`; the cases below are a specification of the schema guarantees, not a suite you can run today. What does run is Python under `tests/` (`testpaths`, `pyproject.toml:183`), split into `unit`, `contract`, `integration`, `conformance`, `performance`, `e2e`, `recovery`, and `workloads`, driven by `make test`, `make test-integration`, `make test-conformance`, `make test-e2e`, `make test-recovery`, and `make test-workloads` (`Makefile:57`, `:61`, `:65`, `:69`, `:73`, `:77`); `tests/performance` has no target. See *Running the Python suites* below.

**Related documents:** `DATA_LAYER.md`, `INDICATORS_LAYER.md`, `SCENARIOS_LAYER.md`, `JOB_LAYER.md`.

---

## Setup

```sql
-- Load pgTAP
CREATE EXTENSION IF NOT EXISTS pgtap;
CREATE EXTENSION IF NOT EXISTS postgis;

-- Build the schema by running the Alembic migrations (r001 → r052; there is no r033):
--   make migrate        # apps.cli.app bootstrap-db --with-test-db
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

## Running the Python suites

```bash
make lint                                    # ruff check + ruff format --check + mypy
pytest tests/unit tests/contract             # no database
TEST_DATABASE_URL=postgresql:///reimagina_test pytest tests/integration
```

| suite | how to run | CI (`.github/workflows/ci.yml`) | needs |
|---|---|---|---|
| `tests/unit`, `tests/contract` | `make test` | job `test-py-unit` | nothing |
| `tests/integration` | `make test-integration` | job `test-py-integration`, step `pytest (integration)` | Postgres + PostGIS + pgRouting at `TEST_DATABASE_URL`; CI points `REIMAGINA_APP_DSN` and `DATABASE_URL_DIRECT` at the same database |
| `tests/conformance` | `make test-conformance` | same job, step `pytest (conformance)` | the same database |
| `tests/e2e` | `make test-e2e` (sets `REIMAGINA_E2E=1`) | not run | an API (`REIMAGINA_E2E_API`, default `http://127.0.0.1:8001`), the web dev server (`REIMAGINA_E2E_WEB`, default `http://localhost:5173` — Vite listens on `localhost`, which macOS resolves to `::1`), and Node with the `playwright` module and its Chromium installed under `apps/web` (`tests/e2e/conftest.py` puts `apps/web/node_modules` on `NODE_PATH`) |
| `tests/recovery` | `make test-recovery` (sets `REIMAGINA_RECOVERY_TEST=1`) | not run | ≥20,000 blocks; kills processes (below) |
| `tests/workloads` | `make test-workloads` (sets `REIMAGINA_BENCH=1`) | not run | its own database, `TEST_DATABASE_URL` or `reimagina_test_bench_dbos`, migrated and never downgraded |
| `tests/performance` | `pytest tests/performance`; no target | not run | `TEST_DATABASE_URL`, plus seeded data at `REIMAGINA_PERF_DSN` → `REIMAGINA_APP_DSN` → `postgresql://localhost/reimagina_app` for `test_overlap_benchmarks.py` |

`tests/e2e` drives Chromium through two Node scripts rather than a Python
dependency. `test_web_app_loads.py` runs `_browser_probe.js` once per session
over the `map`, `scenarios`, `catalog` and `ops` pages and asserts what each
page reports about itself, never pixels. `test_scenario_flow.py` runs
`_scenario_flow.js`, which draws, edits, drags, overlaps and deletes scenario
rules through the page; it writes, so it also needs `REIMAGINA_E2E_WRITES=1`
and a scratch stack (`tests/e2e/README.md` has the recipe). Every missing
prerequisite is a skip that names itself.

CI also runs `make lint`'s three steps (job `lint-py`) and the web app's
`npm run build` and `npm test` (job `build-web`; locally `make build-web` and
`make test-web`). `make check` runs `lint`, `test`, `build-web` and `test-web`
— none of the database suites.

⚠ **Never a bare `pytest`.** `testpaths = ["tests"]` collects every suite.
`migrated_db` (`tests/conftest.py`), the session fixture that
`tests/integration`, `tests/conformance` and two of the three
`tests/performance` files depend on (directly or through `conn`), ends with
`alembic downgrade base`, which drops every
managed schema in `TEST_DATABASE_URL` (default
`postgresql://localhost/reimagina_test`). It refuses to run against a database
whose name does not have `test` as a word (`reimagina_test`, not
`reimagina_latest`), unless the name equals
`REIMAGINA_TEST_DB_ALLOW_NAME`; that guard is the only thing between the suite
and real data. When the database is unreachable it skips, unless
`REIMAGINA_REQUIRE_DB=1` (set by CI's `test-py-integration` job), which turns
the skip into a failure.

⚠ **Run `make lint`, not a hand-rolled `ruff check <paths>`.** The target
mirrors CI exactly — `ruff check .`, `ruff format --check .`, and mypy over
`packages/py/reimagina`, `apps` and `scripts` (`Makefile:47-50`,
`ci.yml:62-69`). A narrowed invocation skips the format gate and produced
three "lint clean" reports against red CI in one day.

⚠ **Do not point work-in-progress migrations at the shared `reimagina_test`.**
A revision that exists only on your branch leaves the shared database stamped
at a version alembic has no file for, and every subsequent `upgrade head` fails
for everyone — surfacing as ~224 setup errors that look like a defect in
whatever is being tested. Use a scratch database instead:

```bash
createdb reimagina_test_<yourthing>
psql -d reimagina_test_<yourthing> -c 'CREATE EXTENSION postgis; CREATE EXTENSION pgrouting;'
TEST_DATABASE_URL=postgresql:///reimagina_test_<yourthing> pytest tests/integration
```

The name must have `test` as a `_`-separated word for the fixture guard. Anything left in the shared
database survives between runs only by luck, since each run ends by dropping
every managed schema.

### `tests/recovery` — opt-in, kills processes

The only suite that exercises DBOS crash recovery, with a real `SIGKILL`
mid-compute in both of its tests (`tests/recovery/test_crash_recovery.py`).
`test_a_killed_executor_resumes_the_same_feature_set` relaunches under the
same executor id (`_recover.py`), so `DBOS.launch()` reclaims the workflow
itself. `test_a_peer_reclaims_a_dead_executors_run` relaunches under a
*different* id (`_sweep.py`), so the run finishes only because
`reimagina.orchestration.reclaim.sweep` judged the first executor dead from
its stale `ops_executor_heartbeat` row. It is the suite that most directly
covers the as-completed dispatch loop in `run_workflow`
(`packages/py/reimagina/orchestration/dbos_app.py`), whose correctness
argument rests on every branch input being checkpointed.

```bash
REIMAGINA_RECOVERY_TEST=1 TEST_DATABASE_URL=postgresql:///reimagina_test_recovery \
    pytest tests/recovery -q        # or: make test-recovery
```

It needs **≥20,000 blocks** (`MIN_BLOCKS`) and *skips* below that rather than
passing vacuously — an earlier version slept six seconds, killed nothing
because the run had already finished, and would have passed while proving the
opposite of what it claims. The kill now waits until a unit is observably
`running` with fewer than three succeeded; if the run still reaches a terminal
state before the signal lands, the test skips with the run's state attached.

It seeds the indicator catalog into the test database itself (`make migrate`
seeds only the app database, and every unit looks its indicator up there),
truncates `run_plan`, deletes every `job_unit` / `orchestration_run` row and
its own executors' `ops_executor_heartbeat` rows, and submits a full-city
recompute. Clone a populated database rather than using one you care about, or
seed a synthetic one (22,500 blocks, no credentials):

```bash
createdb -T reimagina_demo reimagina_test_recovery
# or, on a migrated database:
uv run python scripts/synthetic_territory.py --dsn "$TEST_DATABASE_URL" --blocks 150 --ageb-side 10
```

Last measured: 67,167 blocks, killed with a unit running, second process
reclaimed the workflow and finished it, plan reclaimed (`RUN_PLAN_ROWS = 0`).

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
SELECT plan(4);

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

-- type_slug and role are re-derived from type_id on every insert and update:
-- a conflicting manual edit is overwritten, not rejected
UPDATE curated_geo.feature
SET type_slug = 'road'
WHERE id = '33333333-0000-0000-0000-000000000001';

SELECT is(
  (SELECT type_slug FROM curated_geo.feature
   WHERE id = '33333333-0000-0000-0000-000000000001'),
  'block',
  'Manual edit to type_slug is overwritten from type_id'
);

SELECT * FROM finish();
ROLLBACK;
```

There is no same-context case: `curated_geo.feature_before_write` (r003)
compares parent and child `admin_level` only, never `context`, so a parent of
another context is accepted whenever its `admin_level` is lower — or NULL,
since the comparison is skipped for a parent with no `admin_level`.

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

-- indicator_dependency_validate_trigger is DEFERRABLE INITIALLY DEFERRED (r016):
-- left deferred it fires only at COMMIT, which the ROLLBACK below never reaches.
SET CONSTRAINTS ALL IMMEDIATE;

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
SET CONSTRAINTS ALL IMMEDIATE;  -- the cycle check is the same deferred trigger (5.2)

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

-- Setup. scenario_id is a bare uuid with no FK (r016:7-10; app_core.scenario exists
-- since r048, but no migration ties job_run.scenario_id to it), so no scenario
-- row is required here, and a made-up id is accepted.
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

INSERT INTO curated_geo.indicator_result
  (feature_id, feature_type, scenario_id, indicator_id, indicator_slug,
   job_run_id, value_numeric, quality_flag, status)
VALUES
  ('33333333-0000-0000-0000-000000000001', 'block', NULL,
   '66666666-0000-0000-0000-000000000010', 'test_walk',
   '99999999-0000-0000-0000-000000000002', 0.80, 'measured', 'current');

-- A second current base-state result for the same (feature, indicator): must fail
-- (ir_one_current_base, r016)
SELECT throws_ok(
  $$
    INSERT INTO curated_geo.indicator_result
      (feature_id, feature_type, scenario_id, indicator_id, indicator_slug,
       job_run_id, value_numeric, quality_flag, status)
    VALUES
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

A scenario is an `app_core.scenario` row, and its drawn rules are
`curated_geo.scenario_edit` rows, one attribute value each (both r048).
`materialize_scenario` (`packages/py/reimagina/scenarios/authoring.py`)
expands those rules into per-feature `curated_geo.scenario_override` rows
(r030), whose `scenario_id` is still a bare uuid with no foreign key to
`app_core.scenario`. So the override cases below need no scenario row.

### 8.1 Overrides

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

### 8.2 Scenarios and drawn rules

```sql
BEGIN;
SELECT plan(3);

INSERT INTO app_core.project (id, name)
VALUES ('44444444-0000-0000-0000-000000000003', 'Test Project 3');

INSERT INTO app_core.scenario (id, project_id, name)
VALUES ('88888888-0000-0000-0000-000000000004',
        '44444444-0000-0000-0000-000000000003', 'Taller blocks');

INSERT INTO curated_geo.scenario_edit
  (scenario_id, ordinal, geometry, type_id, attribute_key, value_numeric)
SELECT '88888888-0000-0000-0000-000000000004', 1,
       ST_SetSRID(ST_MakeEnvelope(-99.15, 19.42, -99.12, 19.45), 4326),
       id, 'floors', 10
FROM curated_geo.feature_type_catalog WHERE type_slug = 'block' AND context = 'MEX';

-- A scenario is addressed by name inside its project: UNIQUE (project_id, name)
SELECT throws_ok(
  $$
    INSERT INTO app_core.scenario (project_id, name)
    VALUES ('44444444-0000-0000-0000-000000000003', 'Taller blocks')
  $$,
  '23505',
  'Duplicate scenario name within a project must be rejected'
);

-- A drawn rule is polygonal (type check only; validity is the API's job)
SELECT throws_ok(
  $$
    INSERT INTO curated_geo.scenario_edit
      (scenario_id, ordinal, geometry, type_id, attribute_key, value_numeric)
    SELECT '88888888-0000-0000-0000-000000000004', 2,
           ST_SetSRID(ST_MakePoint(-99.14, 19.43), 4326),
           id, 'floors', 10
    FROM curated_geo.feature_type_catalog WHERE type_slug = 'block' AND context = 'MEX'
  $$,
  '23514',
  'Non-polygon scenario_edit geometry must be rejected'
);

-- ordinal decides which overlapping rule wins, so it is unique per scenario
SELECT throws_ok(
  $$
    INSERT INTO curated_geo.scenario_edit
      (scenario_id, ordinal, geometry, type_id, attribute_key, value_numeric)
    SELECT '88888888-0000-0000-0000-000000000004', 1,
           ST_SetSRID(ST_MakeEnvelope(-99.15, 19.42, -99.12, 19.45), 4326),
           id, 'num_levels', 3
    FROM curated_geo.feature_type_catalog WHERE type_slug = 'block' AND context = 'MEX'
  $$,
  '23505',
  'Duplicate (scenario_id, ordinal) must be rejected'
);

SELECT * FROM finish();
ROLLBACK;
```

There is no scenario-status test and no configuration test: `app_core.scenario`
has no status column (`materialized_at` is its only lifecycle field),
`app_core.configuration` does not exist, and `app_core` holds only `project`
(r001:27) and `scenario` (r048:67).

---

## 9. Serving Layer

```sql
BEGIN;
SELECT plan(4);

-- feature_v must merge feature.attributes with current observations.
-- Every case above rolls back, so insert the observation here, then refresh:
-- feature_v reads current observations through the materialized view
-- serving_geo.feature_current_attributes_mv (r008, r027), not live.
INSERT INTO curated_geo.observation
  (feature_id, attribute_key, value_type, value_numeric,
   source_id, ingestion_run_id, is_current, valid_from)
VALUES
  ('33333333-0000-0000-0000-000000000001',
   'population_total', 'numeric', 1200,
   '11111111-0000-0000-0000-000000000002',
   '22222222-0000-0000-0000-000000000002',
   true, now());
REFRESH MATERIALIZED VIEW serving_geo.feature_current_attributes_mv;

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

-- current_indicator_v must not return stale rows. The view has no status
-- column (r016), so join back to indicator_result to read it.
-- (Assumes a stale indicator_result row exists for the test block)
SELECT is(
  (SELECT count(*)::int
   FROM serving_geo.current_indicator_v v
   JOIN curated_geo.indicator_result ir ON ir.id = v.id
   WHERE v.feature_id = '33333333-0000-0000-0000-000000000001'
     AND ir.status <> 'current'),
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
  AND indicator_slug = 'livability_block_score';
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
  AND base.indicator_slug = 'livability_block_score';
-- Expected: Index Scan using ir_base_slug_idx + ir_scenario_slug_idx

-- Current observations for a feature
EXPLAIN (ANALYZE, BUFFERS)
SELECT attribute_key, value_numeric
FROM curated_geo.observation
WHERE feature_id  = :feature_id
  AND is_current;
-- Expected: Index Scan using obs_one_current (unique, partial WHERE is_current)
-- or obs_feature_key_idx (not partial); both are on (feature_id, attribute_key), r003

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
make test-integration  # needs Postgres + PostGIS (+ pgRouting, r037)
make test-conformance  # asserts the Orchestrator port contract (needs Postgres)
make test-e2e          # opt-in: every page in a real browser (API + web dev server)
make test-recovery     # opt-in: kills an executor mid-run (>=20,000 blocks)
make test-workloads    # opt-in: ops-plane scheduling measurements
make test-web          # the web app's vitest suite
```
