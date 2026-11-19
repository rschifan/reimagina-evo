---
title: "Database and API reference"
subtitle: "Reimagina · Conceptualización y diseño de una centralidad urbana en Mexicali — Schedule A, Work 3"
author: "Rossano Schifanella"
date: "20 November 2026"
---

## Document control

| | |
|---|---|
| **Project** | Conceptualización y diseño de una centralidad urbana en Mexicali |
| **Deliverable** | Schedule A, Work 3 — Database/API schemas and documentation |
| **Version** | 1.0 |
| **Status** | Issued |
| **Audience** | Technical reviewers, maintainers and operators |

| Version | Date | Author | Summary |
|---|---|---|---|
| 1.0 | 2026-11-20 | Rossano Schifanella | Issued with the v1.0.0 delivery |

> **Generated — do not edit.** Regenerate with
> `python scripts/build_schema_reference.py --dsn … --date …`. The
> schema is introspected from a migrated database and the endpoints
> come from the application's own OpenAPI document, so this reference
> describes the delivered system by construction rather than by
> assertion.

## Database

The delivered stack runs PostgreSQL 16, PostGIS 3.4 and pgRouting 3.6.1 (`infra/docker/docker-compose.yml`). This section was introspected from a database migrated to Alembic revision **`r051`**; the server version of the host it was introspected on is deliberately not recorded, because it is not part of the delivery.

Types are as PostgreSQL prints them (`format_type`), so a geometry column shows its geometry type and SRID.

Two things live in the same database without belonging to this schema. The `dbos` schema is created at runtime by the DBOS workflow library, which orchestrates runs, for its own checkpoint tables; it is third-party and not described here. The three tile functions in `serving_geo` follow Martin's function-source URL contract, `/{function}/{z}/{x}/{y}` with the query string passed as `query_params`; the same contract is served by Martin or by the API's `/tiles` router (chosen by `REIMAGINA_TILE_LIVE_BASE_URL`), and clients take the URLs from `GET /tile-sources`.

### `app_core`

Tenancy. The project a scenario or AOI belongs to, and the scenarios themselves. The API reads and writes it when scenarios are authored.

2 table(s), 0 view(s), 0 materialized view(s), 0 function(s).

#### `app_core.project`

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | uuid | no | gen_random_uuid() |
| `name` | text | no | — |
| `description` | text | yes | — |
| `created_at` | timestamp with time zone | no | now() |
| `updated_at` | timestamp with time zone | no | now() |

Constraints:

- `project_pkey` — PRIMARY KEY (id)

Indexes:

- `project_pkey` — CREATE UNIQUE INDEX project_pkey ON app_core.project USING btree (id)

Row-level security: enabled (not forced, so the table owner bypasses it). Policies:

- `tenant_isolation` — ALL to public, using `((current_setting('app.current_project_id'::text, true) IS NULL) OR (current_setting('app.current_project_id'::text, true) = ''::text) OR ((id)::text = current_setting('app.current_project_id'::text, true)))`

#### `app_core.scenario`

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | uuid | no | gen_random_uuid() |
| `project_id` | uuid | no | — |
| `aoi_id` | uuid | yes | — |
| `name` | text | no | — |
| `description` | text | yes | — |
| `created_at` | timestamp with time zone | no | now() |
| `updated_at` | timestamp with time zone | no | now() |
| `materialized_at` | timestamp with time zone | yes | — |

Constraints:

- `scenario_aoi_id_fkey` — FOREIGN KEY (aoi_id) REFERENCES curated_geo.area_of_interest(id) ON DELETE SET NULL
- `scenario_project_id_fkey` — FOREIGN KEY (project_id) REFERENCES app_core.project(id) ON DELETE CASCADE
- `scenario_pkey` — PRIMARY KEY (id)
- `scenario_project_id_name_key` — UNIQUE (project_id, name)

Indexes:

- `scenario_pkey` — CREATE UNIQUE INDEX scenario_pkey ON app_core.scenario USING btree (id)
- `scenario_project_id_name_key` — CREATE UNIQUE INDEX scenario_project_id_name_key ON app_core.scenario USING btree (project_id, name)

Row-level security: enabled (not forced, so the table owner bypasses it). Policies:

- `tenant_isolation` — ALL to public, using `((current_setting('app.current_project_id'::text, true) IS NULL) OR (current_setting('app.current_project_id'::text, true) = ''::text) OR ((project_id)::text = current_setting('app.current_project_id'::text, true)))`

### `curated_geo`

Source of truth. Features, their observations, overlaps, indicator results, the asset graph and scenario overrides. Everything else is derived from here. Besides the pipeline and the worker, the API reads it directly for the indicator catalog, scenario authoring and comparison, tile-source discovery and source attribution, and writes scenario edits and the scenario-scoped rows that follow from them.

25 table(s), 0 view(s), 0 materialized view(s), 7 function(s).

#### `curated_geo.aggregation_recipe`

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | uuid | no | gen_random_uuid() |
| `input_kind` | text | no | — |
| `input_raster_layer_id` | uuid | yes | — |
| `input_attribute_key_prefix` | text | yes | — |
| `input_feature_type_slug` | text | yes | — |
| `target_feature_type_slug` | text | no | — |
| `attribute_key_prefix` | text | no | — |
| `method` | text | no | — |
| `weight_basis` | text | yes | — |
| `params` | jsonb | no | '{}'::jsonb |
| `coverage_threshold` | double precision | no | 0.0 |
| `aoi_id` | uuid | yes | — |
| `enabled` | boolean | no | true |
| `created_at` | timestamp with time zone | no | now() |
| `updated_at` | timestamp with time zone | no | now() |

Constraints:

- `aggregation_recipe_coverage_threshold_check` — CHECK (((coverage_threshold >= (0)::double precision) AND (coverage_threshold <= (1)::double precision)))
- `aggregation_recipe_input_kind_check` — CHECK ((input_kind = ANY (ARRAY['raster'::text, 'observation'::text])))
- `aggregation_recipe_input_shape` — CHECK ((((input_kind = 'raster'::text) AND (input_raster_layer_id IS NOT NULL) AND (input_attribute_key_prefix IS NULL) AND (input_feature_type_slug IS NULL)) OR ((input_kind = 'observation'::text) AND (input_raster_layer_id IS NULL) AND (input_attribute_key_prefix IS NOT NULL) AND (input_feature_type_slug IS NOT NULL))))
- `aggregation_recipe_method_check` — CHECK ((method = ANY (ARRAY['mean'::text, 'weighted_mean'::text, 'sum'::text, 'min'::text, 'max'::text])))
- `aggregation_recipe_weight_basis_check` — CHECK ((weight_basis = ANY (ARRAY['area'::text, 'length'::text, 'presence'::text])))
- `aggregation_recipe_aoi_id_fkey` — FOREIGN KEY (aoi_id) REFERENCES curated_geo.area_of_interest(id) ON DELETE SET NULL
- `aggregation_recipe_input_raster_layer_id_fkey` — FOREIGN KEY (input_raster_layer_id) REFERENCES curated_geo.raster_layer(id) ON DELETE CASCADE
- `aggregation_recipe_pkey` — PRIMARY KEY (id)
- `aggregation_recipe_prefix_uk` — UNIQUE (target_feature_type_slug, attribute_key_prefix)

Indexes:

- `aggregation_recipe_enabled_idx` — CREATE INDEX aggregation_recipe_enabled_idx ON curated_geo.aggregation_recipe USING btree (enabled) WHERE enabled
- `aggregation_recipe_input_raster_idx` — CREATE INDEX aggregation_recipe_input_raster_idx ON curated_geo.aggregation_recipe USING btree (input_raster_layer_id) WHERE (input_raster_layer_id IS NOT NULL)
- `aggregation_recipe_pkey` — CREATE UNIQUE INDEX aggregation_recipe_pkey ON curated_geo.aggregation_recipe USING btree (id)
- `aggregation_recipe_prefix_uk` — CREATE UNIQUE INDEX aggregation_recipe_prefix_uk ON curated_geo.aggregation_recipe USING btree (target_feature_type_slug, attribute_key_prefix)

#### `curated_geo.aoi_feature`

| Column | Type | Null | Default |
|---|---|---|---|
| `aoi_id` | uuid | no | — |
| `feature_id` | uuid | no | — |
| `overlap_basis` | text | no | — |
| `overlap_measure` | double precision | no | — |
| `feature_measure` | double precision | no | — |
| `overlap_ratio` | double precision | no | — |

Constraints:

- `aoi_feature_check` — CHECK ((overlap_measure <= feature_measure))
- `aoi_feature_check1` — CHECK (((overlap_basis <> 'presence'::text) OR ((overlap_measure = (1)::double precision) AND (feature_measure = (1)::double precision) AND (overlap_ratio = (1)::double precision))))
- `aoi_feature_feature_measure_check` — CHECK ((feature_measure > (0)::double precision))
- `aoi_feature_overlap_basis_check` — CHECK ((overlap_basis = ANY (ARRAY['area'::text, 'length'::text, 'presence'::text])))
- `aoi_feature_overlap_measure_check` — CHECK ((overlap_measure > (0)::double precision))
- `aoi_feature_overlap_ratio_check` — CHECK (((overlap_ratio > (0)::double precision) AND (overlap_ratio <= (1)::double precision)))
- `aoi_feature_aoi_id_fkey` — FOREIGN KEY (aoi_id) REFERENCES curated_geo.area_of_interest(id) ON DELETE CASCADE
- `aoi_feature_feature_id_fkey` — FOREIGN KEY (feature_id) REFERENCES curated_geo.feature(id) ON DELETE CASCADE
- `aoi_feature_pkey` — PRIMARY KEY (aoi_id, feature_id)

Indexes:

- `aoi_feature_feature_idx` — CREATE INDEX aoi_feature_feature_idx ON curated_geo.aoi_feature USING btree (feature_id)
- `aoi_feature_pkey` — CREATE UNIQUE INDEX aoi_feature_pkey ON curated_geo.aoi_feature USING btree (aoi_id, feature_id)

#### `curated_geo.aoi_indicator_summary`

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | uuid | no | gen_random_uuid() |
| `aoi_id` | uuid | no | — |
| `scenario_id` | uuid | yes | — |
| `indicator_id` | uuid | no | — |
| `value` | double precision | yes | — |
| `aggregation_note` | text | yes | — |
| `status` | text | no | 'current'::text |
| `computed_at` | timestamp with time zone | no | now() |

Constraints:

- `aoi_indicator_summary_status_check` — CHECK ((status = ANY (ARRAY['current'::text, 'stale'::text, 'missing'::text])))
- `aoi_indicator_summary_aoi_id_fkey` — FOREIGN KEY (aoi_id) REFERENCES curated_geo.area_of_interest(id) ON DELETE CASCADE
- `aoi_indicator_summary_pkey` — PRIMARY KEY (id)

Indexes:

- `aoi_indicator_summary_base_uk` — CREATE UNIQUE INDEX aoi_indicator_summary_base_uk ON curated_geo.aoi_indicator_summary USING btree (aoi_id, indicator_id) WHERE (scenario_id IS NULL)
- `aoi_indicator_summary_pkey` — CREATE UNIQUE INDEX aoi_indicator_summary_pkey ON curated_geo.aoi_indicator_summary USING btree (id)
- `aoi_indicator_summary_scenario_uk` — CREATE UNIQUE INDEX aoi_indicator_summary_scenario_uk ON curated_geo.aoi_indicator_summary USING btree (aoi_id, scenario_id, indicator_id) WHERE (scenario_id IS NOT NULL)

#### `curated_geo.area_of_interest`

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | uuid | no | gen_random_uuid() |
| `project_id` | uuid | no | — |
| `name` | text | no | — |
| `description` | text | yes | — |
| `geometry` | geometry(Geometry,4326) | no | — |
| `created_at` | timestamp with time zone | no | now() |
| `updated_at` | timestamp with time zone | no | now() |
| `membership_key` | jsonb | yes | — |

Constraints:

- `area_of_interest_geometry_check` — CHECK ((st_geometrytype(geometry) = ANY (ARRAY['ST_Polygon'::text, 'ST_MultiPolygon'::text])))
- `area_of_interest_membership_key_nonempty` — CHECK (((membership_key IS NULL) OR ((jsonb_typeof(membership_key) = 'object'::text) AND (membership_key <> '{}'::jsonb))))
- `area_of_interest_project_id_fkey` — FOREIGN KEY (project_id) REFERENCES app_core.project(id) ON DELETE CASCADE
- `area_of_interest_pkey` — PRIMARY KEY (id)

Indexes:

- `aoi_geometry_gist` — CREATE INDEX aoi_geometry_gist ON curated_geo.area_of_interest USING gist (geometry)
- `area_of_interest_pkey` — CREATE UNIQUE INDEX area_of_interest_pkey ON curated_geo.area_of_interest USING btree (id)
- `area_of_interest_project_idx` — CREATE INDEX area_of_interest_project_idx ON curated_geo.area_of_interest USING btree (project_id)

Row-level security: enabled (not forced, so the table owner bypasses it). Policies:

- `tenant_isolation` — ALL to public, using `((current_setting('app.current_project_id'::text, true) IS NULL) OR (current_setting('app.current_project_id'::text, true) = ''::text) OR ((project_id)::text = current_setting('app.current_project_id'::text, true)))`

#### `curated_geo.asset`

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | uuid | no | gen_random_uuid() |
| `asset_key` | text | no | — |
| `kind` | text | no | — |
| `producer` | text | yes | — |
| `backing_kind` | text | no | — |
| `backing_ref` | text | yes | — |
| `feature_type_id` | uuid | yes | — |
| `partition_dimensions` | text[] | no | '{}'::text[] |
| `code_version` | text | yes | — |
| `params_digest` | text | yes | — |
| `version_strategy` | text | yes | — |
| `probe_sql` | text | yes | — |
| `description` | text | yes | — |
| `created_at` | timestamp with time zone | no | now() |

Constraints:

- `asset_backing_kind_check` — CHECK ((backing_kind = ANY (ARRAY['feature_type'::text, 'observation_prefix'::text, 'relation'::text, 'raster_layer'::text, 'file'::text, 'config'::text, 'indicator'::text, 'rollup'::text, 'aoi_summary'::text, 'tile_snapshot'::text, 'scenario_override'::text])))
- `asset_kind_check` — CHECK ((kind = ANY (ARRAY['source'::text, 'external'::text, 'derived'::text])))
- `asset_kind_shape` — CHECK ((((kind = 'derived'::text) AND (producer IS NOT NULL) AND (code_version IS NOT NULL) AND (version_strategy IS NULL)) OR ((kind = ANY (ARRAY['source'::text, 'external'::text])) AND (producer IS NULL) AND (code_version IS NULL) AND (version_strategy IS NOT NULL))))
- `asset_probe_shape` — CHECK (((COALESCE(version_strategy, ''::text) = 'observed'::text) = (probe_sql IS NOT NULL)))
- `asset_version_strategy_check` — CHECK ((version_strategy = ANY (ARRAY['commit'::text, 'digest'::text, 'observed'::text])))
- `asset_feature_type_id_fkey` — FOREIGN KEY (feature_type_id) REFERENCES curated_geo.feature_type_catalog(id) ON DELETE RESTRICT
- `asset_pkey` — PRIMARY KEY (id)
- `asset_asset_key_key` — UNIQUE (asset_key)

Indexes:

- `asset_asset_key_key` — CREATE UNIQUE INDEX asset_asset_key_key ON curated_geo.asset USING btree (asset_key)
- `asset_kind_idx` — CREATE INDEX asset_kind_idx ON curated_geo.asset USING btree (kind)
- `asset_pkey` — CREATE UNIQUE INDEX asset_pkey ON curated_geo.asset USING btree (id)

#### `curated_geo.asset_dependency`

| Column | Type | Null | Default |
|---|---|---|---|
| `consumer_asset_id` | uuid | no | — |
| `producer_asset_id` | uuid | no | — |
| `relation` | text | no | — |
| `attributes` | text[] | no | '{}'::text[] |
| `radius_m` | double precision | yes | — |
| `weight` | double precision | yes | — |
| `partition_mapping` | text | no | 'identity'::text |
| `created_at` | timestamp with time zone | no | now() |

Constraints:

- `asset_dependency_check` — CHECK ((consumer_asset_id <> producer_asset_id))
- `asset_dependency_partition_mapping_check` — CHECK ((partition_mapping = ANY (ARRAY['identity'::text, 'baseline'::text, 'fan_in'::text])))
- `asset_dependency_radius` — CHECK ((((relation = 'within_radius'::text) AND (radius_m IS NOT NULL) AND (radius_m > (0)::double precision)) OR ((relation <> 'within_radius'::text) AND (radius_m IS NULL))))
- `asset_dependency_relation_check` — CHECK ((relation = ANY (ARRAY['self'::text, 'child_in_parent'::text, 'overlap'::text, 'within_radius'::text, 'derived_from'::text])))
- `asset_dependency_weight` — CHECK (((weight IS NULL) OR ((relation = 'derived_from'::text) AND (weight >= (0)::double precision) AND (weight <= (1)::double precision))))
- `asset_dependency_consumer_asset_id_fkey` — FOREIGN KEY (consumer_asset_id) REFERENCES curated_geo.asset(id) ON DELETE CASCADE
- `asset_dependency_producer_asset_id_fkey` — FOREIGN KEY (producer_asset_id) REFERENCES curated_geo.asset(id) ON DELETE RESTRICT
- `asset_dependency_pkey` — PRIMARY KEY (consumer_asset_id, producer_asset_id, relation)
- `asset_dependency_validate_trigger` — TRIGGER DEFERRABLE INITIALLY DEFERRED

Indexes:

- `asset_dependency_pkey` — CREATE UNIQUE INDEX asset_dependency_pkey ON curated_geo.asset_dependency USING btree (consumer_asset_id, producer_asset_id, relation)
- `asset_dependency_producer_idx` — CREATE INDEX asset_dependency_producer_idx ON curated_geo.asset_dependency USING btree (producer_asset_id)

#### `curated_geo.asset_materialization`

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | uuid | no | gen_random_uuid() |
| `asset_id` | uuid | no | — |
| `partition_key` | text | no | ''::text |
| `version` | text | no | — |
| `input_versions` | jsonb | no | '{}'::jsonb |
| `coverage_scope` | jsonb | no | '{"complete": true}'::jsonb |
| `job_run_id` | uuid | yes | — |
| `row_count` | integer | yes | — |
| `status` | text | no | 'succeeded'::text |
| `started_at` | timestamp with time zone | no | clock_timestamp() |
| `completed_at` | timestamp with time zone | no | clock_timestamp() |

Constraints:

- `asset_materialization_check` — CHECK ((completed_at >= started_at))
- `asset_materialization_coverage_scope_check` — CHECK ((jsonb_typeof((coverage_scope -> 'complete'::text)) = 'boolean'::text))
- `asset_materialization_row_count_check` — CHECK (((row_count IS NULL) OR (row_count >= 0)))
- `asset_materialization_status_check` — CHECK ((status = ANY (ARRAY['succeeded'::text, 'failed'::text])))
- `asset_materialization_version_check` — CHECK ((version <> ''::text))
- `asset_materialization_asset_id_fkey` — FOREIGN KEY (asset_id) REFERENCES curated_geo.asset(id) ON DELETE CASCADE
- `asset_materialization_job_run_id_fkey` — FOREIGN KEY (job_run_id) REFERENCES jobs_runtime.job_run(id) ON DELETE SET NULL
- `asset_materialization_pkey` — PRIMARY KEY (id)

Indexes:

- `asset_materialization_latest_idx` — CREATE INDEX asset_materialization_latest_idx ON curated_geo.asset_materialization USING btree (asset_id, partition_key, completed_at DESC)
- `asset_materialization_pkey` — CREATE UNIQUE INDEX asset_materialization_pkey ON curated_geo.asset_materialization USING btree (id)

#### `curated_geo.asset_version`

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | uuid | no | gen_random_uuid() |
| `asset_id` | uuid | no | — |
| `partition_key` | text | no | ''::text |
| `version` | text | no | — |
| `sequence` | bigint | no | — |
| `ingestion_run_id` | uuid | yes | — |
| `dataset_version` | text | yes | — |
| `row_count` | integer | yes | — |
| `observed_at` | timestamp with time zone | no | now() |
| `is_current` | boolean | no | true |
| `valid_from` | timestamp with time zone | no | now() |
| `valid_to` | timestamp with time zone | yes | — |

Constraints:

- `asset_version_check` — CHECK (((valid_to IS NULL) OR (valid_to >= valid_from)))
- `asset_version_check1` — CHECK ((is_current = (valid_to IS NULL)))
- `asset_version_row_count_check` — CHECK (((row_count IS NULL) OR (row_count >= 0)))
- `asset_version_version_check` — CHECK ((version <> ''::text))
- `asset_version_asset_id_fkey` — FOREIGN KEY (asset_id) REFERENCES curated_geo.asset(id) ON DELETE CASCADE
- `asset_version_ingestion_run_id_fkey` — FOREIGN KEY (ingestion_run_id) REFERENCES curated_geo.ingestion_run(id) ON DELETE SET NULL
- `asset_version_pkey` — PRIMARY KEY (id)
- `asset_version_asset_id_partition_key_sequence_key` — UNIQUE (asset_id, partition_key, sequence)

Indexes:

- `asset_version_asset_id_partition_key_sequence_key` — CREATE UNIQUE INDEX asset_version_asset_id_partition_key_sequence_key ON curated_geo.asset_version USING btree (asset_id, partition_key, sequence)
- `asset_version_one_current` — CREATE UNIQUE INDEX asset_version_one_current ON curated_geo.asset_version USING btree (asset_id, partition_key) WHERE is_current
- `asset_version_pkey` — CREATE UNIQUE INDEX asset_version_pkey ON curated_geo.asset_version USING btree (id)

#### `curated_geo.data_source_catalog`

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | uuid | no | gen_random_uuid() |
| `name` | text | no | — |
| `provider` | text | no | — |
| `storage_type` | text | no | — |
| `integration_role` | text | no | — |
| `storage_uri` | text | no | — |
| `schema_definition` | jsonb | yes | — |
| `version` | text | no | — |
| `license` | text | yes | — |
| `update_frequency` | text | yes | — |
| `source_extent_geometry` | geometry(Polygon,4326) | yes | — |
| `ingested_at` | timestamp with time zone | yes | — |
| `notes` | text | yes | — |
| `created_at` | timestamp with time zone | no | now() |

Constraints:

- `data_source_catalog_integration_role_check` — CHECK ((integration_role = ANY (ARRAY['canonical_entity'::text, 'enrichment'::text, 'model_output'::text])))
- `data_source_catalog_storage_type_check` — CHECK ((storage_type = ANY (ARRAY['vector'::text, 'tabular'::text, 'raster'::text, 'imagery'::text, 'point_cloud'::text])))
- `data_source_catalog_update_frequency_check` — CHECK (((update_frequency IS NULL) OR (update_frequency = ANY (ARRAY['daily'::text, 'weekly'::text, 'monthly'::text, 'quarterly'::text, 'annual'::text, 'on_demand'::text]))))
- `data_source_catalog_pkey` — PRIMARY KEY (id)
- `data_source_catalog_provider_name_version_key` — UNIQUE (provider, name, version)

Indexes:

- `data_source_catalog_pkey` — CREATE UNIQUE INDEX data_source_catalog_pkey ON curated_geo.data_source_catalog USING btree (id)
- `data_source_catalog_provider_name_version_key` — CREATE UNIQUE INDEX data_source_catalog_provider_name_version_key ON curated_geo.data_source_catalog USING btree (provider, name, version)

#### `curated_geo.feature`

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | uuid | no | gen_random_uuid() |
| `type_id` | uuid | no | — |
| `type_slug` | text | no | — |
| `role` | text | no | — |
| `parent_id` | uuid | yes | — |
| `name` | text | yes | — |
| `geometry` | geometry(Geometry,4326) | no | — |
| `attributes` | jsonb | no | '{}'::jsonb |
| `source_id` | uuid | no | — |
| `source_record_ref` | text | no | — |
| `ingestion_run_id` | uuid | no | — |
| `dataset_version` | text | no | — |
| `confidence` | double precision | yes | — |
| `model_version` | text | yes | — |
| `created_at` | timestamp with time zone | no | now() |
| `updated_at` | timestamp with time zone | no | now() |
| `geometry_centroid` | geometry(Point,4326) | yes | generated: st_pointonsurface(geometry) |

Constraints:

- `feature_confidence_check` — CHECK (((confidence IS NULL) OR ((confidence >= (0)::double precision) AND (confidence <= (1)::double precision))))
- `feature_ingestion_run_id_fkey` — FOREIGN KEY (ingestion_run_id) REFERENCES curated_geo.ingestion_run(id)
- `feature_parent_id_fkey` — FOREIGN KEY (parent_id) REFERENCES curated_geo.feature(id)
- `feature_source_id_fkey` — FOREIGN KEY (source_id) REFERENCES curated_geo.data_source_catalog(id)
- `feature_type_id_fkey` — FOREIGN KEY (type_id) REFERENCES curated_geo.feature_type_catalog(id)
- `feature_pkey` — PRIMARY KEY (id)
- `feature_source_id_dataset_version_source_record_ref_key` — UNIQUE (source_id, dataset_version, source_record_ref)

Indexes:

- `feature_block_geom_idx` — CREATE INDEX feature_block_geom_idx ON curated_geo.feature USING gist (geometry) WHERE (type_slug = 'block'::text)
- `feature_centroid_gist` — CREATE INDEX feature_centroid_gist ON curated_geo.feature USING gist (geometry_centroid)
- `feature_context_geom_idx` — CREATE INDEX feature_context_geom_idx ON curated_geo.feature USING gist (geometry) WHERE (role = 'context'::text)
- `feature_geometry_gist` — CREATE INDEX feature_geometry_gist ON curated_geo.feature USING gist (geometry)
- `feature_ingestion_run_idx` — CREATE INDEX feature_ingestion_run_idx ON curated_geo.feature USING btree (ingestion_run_id)
- `feature_lot_geom_idx` — CREATE INDEX feature_lot_geom_idx ON curated_geo.feature USING gist (geometry) WHERE (type_slug = 'lot'::text)
- `feature_parent_idx` — CREATE INDEX feature_parent_idx ON curated_geo.feature USING btree (parent_id)
- `feature_pkey` — CREATE UNIQUE INDEX feature_pkey ON curated_geo.feature USING btree (id)
- `feature_role_idx` — CREATE INDEX feature_role_idx ON curated_geo.feature USING btree (role)
- `feature_source_id_dataset_version_source_record_ref_key` — CREATE UNIQUE INDEX feature_source_id_dataset_version_source_record_ref_key ON curated_geo.feature USING btree (source_id, dataset_version, source_record_ref)
- `feature_source_idx` — CREATE INDEX feature_source_idx ON curated_geo.feature USING btree (source_id)
- `feature_type_id_idx` — CREATE INDEX feature_type_id_idx ON curated_geo.feature USING btree (type_id)
- `feature_type_slug_idx` — CREATE INDEX feature_type_slug_idx ON curated_geo.feature USING btree (type_slug)

#### `curated_geo.feature_overlap`

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | uuid | no | gen_random_uuid() |
| `source_feature_id` | uuid | no | — |
| `target_feature_id` | uuid | no | — |
| `overlap_basis` | text | no | — |
| `overlap_measure` | double precision | no | — |
| `feature_measure` | double precision | no | — |
| `overlap_ratio` | double precision | no | — |
| `computed_at` | timestamp with time zone | no | now() |

Constraints:

- `feature_overlap_feature_measure_check` — CHECK ((feature_measure > (0)::double precision))
- `feature_overlap_overlap_basis_check` — CHECK ((overlap_basis = ANY (ARRAY['area'::text, 'length'::text, 'presence'::text])))
- `feature_overlap_overlap_measure_check` — CHECK ((overlap_measure > (0)::double precision))
- `feature_overlap_overlap_ratio_check` — CHECK (((overlap_ratio > (0)::double precision) AND (overlap_ratio <= (1)::double precision)))
- `feature_overlap_source_feature_id_fkey` — FOREIGN KEY (source_feature_id) REFERENCES curated_geo.feature(id) ON DELETE CASCADE
- `feature_overlap_target_feature_id_fkey` — FOREIGN KEY (target_feature_id) REFERENCES curated_geo.feature(id) ON DELETE CASCADE
- `feature_overlap_pkey` — PRIMARY KEY (id)
- `feature_overlap_source_feature_id_target_feature_id_key` — UNIQUE (source_feature_id, target_feature_id)

Indexes:

- `feature_overlap_pkey` — CREATE UNIQUE INDEX feature_overlap_pkey ON curated_geo.feature_overlap USING btree (id)
- `feature_overlap_source_feature_id_target_feature_id_key` — CREATE UNIQUE INDEX feature_overlap_source_feature_id_target_feature_id_key ON curated_geo.feature_overlap USING btree (source_feature_id, target_feature_id)
- `feature_overlap_source_idx` — CREATE INDEX feature_overlap_source_idx ON curated_geo.feature_overlap USING btree (source_feature_id)
- `feature_overlap_target_idx` — CREATE INDEX feature_overlap_target_idx ON curated_geo.feature_overlap USING btree (target_feature_id, overlap_ratio)

#### `curated_geo.feature_overlap_type_pair`

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | uuid | no | gen_random_uuid() |
| `source_type_id` | uuid | no | — |
| `target_type_id` | uuid | no | — |

Constraints:

- `feature_overlap_type_pair_source_type_id_fkey` — FOREIGN KEY (source_type_id) REFERENCES curated_geo.feature_type_catalog(id)
- `feature_overlap_type_pair_target_type_id_fkey` — FOREIGN KEY (target_type_id) REFERENCES curated_geo.feature_type_catalog(id)
- `feature_overlap_type_pair_pkey` — PRIMARY KEY (id)
- `feature_overlap_type_pair_source_type_id_target_type_id_key` — UNIQUE (source_type_id, target_type_id)

Indexes:

- `feature_overlap_type_pair_pkey` — CREATE UNIQUE INDEX feature_overlap_type_pair_pkey ON curated_geo.feature_overlap_type_pair USING btree (id)
- `feature_overlap_type_pair_source_type_id_target_type_id_key` — CREATE UNIQUE INDEX feature_overlap_type_pair_source_type_id_target_type_id_key ON curated_geo.feature_overlap_type_pair USING btree (source_type_id, target_type_id)
- `feature_overlap_type_pair_target_idx` — CREATE INDEX feature_overlap_type_pair_target_idx ON curated_geo.feature_overlap_type_pair USING btree (target_type_id)

#### `curated_geo.feature_type_catalog`

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | uuid | no | gen_random_uuid() |
| `context` | text | no | — |
| `type_slug` | text | no | — |
| `label` | text | no | — |
| `geometry_family` | text | no | — |
| `role` | text | no | — |
| `admin_level` | smallint | yes | — |
| `description` | text | yes | — |

Constraints:

- `feature_type_catalog_admin_level_check` — CHECK (((admin_level IS NULL) OR (admin_level > 0)))
- `feature_type_catalog_geometry_family_check` — CHECK ((geometry_family = ANY (ARRAY['point'::text, 'line'::text, 'polygon'::text, 'geometry'::text])))
- `feature_type_catalog_role_check` — CHECK ((role = ANY (ARRAY['analysis_unit'::text, 'context'::text, 'infrastructure'::text, 'poi'::text, 'detection'::text])))
- `feature_type_catalog_pkey` — PRIMARY KEY (id)
- `feature_type_catalog_context_type_slug_key` — UNIQUE (context, type_slug)

Indexes:

- `feature_type_catalog_context_type_slug_key` — CREATE UNIQUE INDEX feature_type_catalog_context_type_slug_key ON curated_geo.feature_type_catalog USING btree (context, type_slug)
- `feature_type_catalog_pkey` — CREATE UNIQUE INDEX feature_type_catalog_pkey ON curated_geo.feature_type_catalog USING btree (id)

#### `curated_geo.file_asset`

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | uuid | no | gen_random_uuid() |
| `source_id` | uuid | no | — |
| `ingestion_run_id` | uuid | no | — |
| `storage_uri` | text | no | — |
| `format` | text | yes | — |
| `crs` | text | yes | — |
| `resolution_m` | double precision | yes | — |
| `band_description` | jsonb | no | '{}'::jsonb |
| `asset_extent_geometry` | geometry(Polygon,4326) | yes | — |
| `captured_at` | timestamp with time zone | yes | — |
| `dataset_version` | text | no | — |
| `created_at` | timestamp with time zone | no | now() |

Constraints:

- `file_asset_format_check` — CHECK ((format = ANY (ARRAY['COG'::text, 'GeoTIFF'::text, 'NetCDF'::text, 'LAZ'::text, 'JPEG'::text, 'PNG'::text])))
- `file_asset_ingestion_run_id_fkey` — FOREIGN KEY (ingestion_run_id) REFERENCES curated_geo.ingestion_run(id)
- `file_asset_source_id_fkey` — FOREIGN KEY (source_id) REFERENCES curated_geo.data_source_catalog(id)
- `file_asset_pkey` — PRIMARY KEY (id)
- `file_asset_source_id_key` — UNIQUE (source_id)

Indexes:

- `file_asset_ingestion_run_idx` — CREATE INDEX file_asset_ingestion_run_idx ON curated_geo.file_asset USING btree (ingestion_run_id)
- `file_asset_pkey` — CREATE UNIQUE INDEX file_asset_pkey ON curated_geo.file_asset USING btree (id)
- `file_asset_source_id_key` — CREATE UNIQUE INDEX file_asset_source_id_key ON curated_geo.file_asset USING btree (source_id)

#### `curated_geo.indicator_catalog`

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | uuid | no | gen_random_uuid() |
| `slug` | text | no | — |
| `name` | text | no | — |
| `description` | text | yes | — |
| `model_family` | text | yes | — |
| `computation_kind` | text | no | — |
| `value_type` | text | no | — |
| `unit` | text | yes | — |
| `valid_min` | double precision | yes | — |
| `valid_max` | double precision | yes | — |
| `aggregation_method` | text | no | — |
| `applies_to` | text[] | no | — |
| `created_at` | timestamp with time zone | no | now() |
| `cap_value` | double precision | yes | — |

Constraints:

- `indicator_catalog_aggregation_method_check` — CHECK ((aggregation_method = ANY (ARRAY['mean'::text, 'weighted_mean'::text, 'sum'::text, 'min'::text, 'max'::text, 'none'::text])))
- `indicator_catalog_applies_to_check` — CHECK ((cardinality(applies_to) > 0))
- `indicator_catalog_check` — CHECK (((valid_min IS NULL) OR (valid_max IS NULL) OR (valid_min <= valid_max)))
- `indicator_catalog_check1` — CHECK (((value_type = 'numeric'::text) OR ((valid_min IS NULL) AND (valid_max IS NULL))))
- `indicator_catalog_check2` — CHECK ((((computation_kind = 'direct'::text) AND (model_family IS NOT NULL)) OR ((computation_kind = 'composite'::text) AND (model_family IS NULL))))
- `indicator_catalog_computation_kind_check` — CHECK ((computation_kind = ANY (ARRAY['direct'::text, 'composite'::text])))
- `indicator_catalog_value_type_check` — CHECK ((value_type = ANY (ARRAY['numeric'::text, 'text'::text, 'json'::text])))
- `indicator_catalog_pkey` — PRIMARY KEY (id)
- `indicator_catalog_slug_key` — UNIQUE (slug)

Indexes:

- `indicator_catalog_pkey` — CREATE UNIQUE INDEX indicator_catalog_pkey ON curated_geo.indicator_catalog USING btree (id)
- `indicator_catalog_slug_key` — CREATE UNIQUE INDEX indicator_catalog_slug_key ON curated_geo.indicator_catalog USING btree (slug)

#### `curated_geo.indicator_dependency`

| Column | Type | Null | Default |
|---|---|---|---|
| `indicator_id` | uuid | no | — |
| `depends_on_id` | uuid | no | — |
| `weight` | double precision | yes | — |

Constraints:

- `indicator_dependency_check` — CHECK ((indicator_id <> depends_on_id))
- `indicator_dependency_weight_check` — CHECK (((weight IS NULL) OR ((weight >= (0)::double precision) AND (weight <= (1)::double precision))))
- `indicator_dependency_depends_on_id_fkey` — FOREIGN KEY (depends_on_id) REFERENCES curated_geo.indicator_catalog(id) ON DELETE RESTRICT
- `indicator_dependency_indicator_id_fkey` — FOREIGN KEY (indicator_id) REFERENCES curated_geo.indicator_catalog(id) ON DELETE CASCADE
- `indicator_dependency_pkey` — PRIMARY KEY (indicator_id, depends_on_id)
- `indicator_dependency_validate_trigger` — TRIGGER DEFERRABLE INITIALLY DEFERRED

Indexes:

- `indicator_dependency_pkey` — CREATE UNIQUE INDEX indicator_dependency_pkey ON curated_geo.indicator_dependency USING btree (indicator_id, depends_on_id)

#### `curated_geo.indicator_result`

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | uuid | no | gen_random_uuid() |
| `feature_id` | uuid | no | — |
| `feature_type` | text | no | — |
| `scenario_id` | uuid | yes | — |
| `indicator_id` | uuid | no | — |
| `indicator_slug` | text | no | — |
| `job_run_id` | uuid | no | — |
| `model_version` | text | yes | — |
| `value_numeric` | double precision | yes | — |
| `value_text` | text | yes | — |
| `value_json` | jsonb | yes | — |
| `quality_flag` | text | no | 'measured'::text |
| `confidence` | double precision | yes | — |
| `missing_reason` | text | yes | — |
| `status` | text | no | 'current'::text |
| `computed_at` | timestamp with time zone | no | now() |
| `updated_at` | timestamp with time zone | no | now() |

Constraints:

- `indicator_result_confidence_check` — CHECK (((confidence IS NULL) OR ((confidence >= (0)::double precision) AND (confidence <= (1)::double precision))))
- `indicator_result_quality_flag_check` — CHECK ((quality_flag = ANY (ARRAY['measured'::text, 'estimated'::text, 'imputed'::text, 'missing'::text, 'not_applicable'::text])))
- `indicator_result_status_check` — CHECK ((status = ANY (ARRAY['current'::text, 'stale'::text, 'failed'::text])))
- `indicator_result_feature_id_fkey` — FOREIGN KEY (feature_id) REFERENCES curated_geo.feature(id) ON DELETE CASCADE
- `indicator_result_indicator_id_fkey` — FOREIGN KEY (indicator_id) REFERENCES curated_geo.indicator_catalog(id)
- `indicator_result_job_run_id_fkey` — FOREIGN KEY (job_run_id) REFERENCES jobs_runtime.job_run(id) ON DELETE CASCADE
- `indicator_result_pkey` — PRIMARY KEY (id)

Indexes:

- `indicator_result_feature_id_idx` — CREATE INDEX indicator_result_feature_id_idx ON curated_geo.indicator_result USING btree (feature_id)
- `indicator_result_pkey` — CREATE UNIQUE INDEX indicator_result_pkey ON curated_geo.indicator_result USING btree (id)
- `ir_base_slug_idx` — CREATE INDEX ir_base_slug_idx ON curated_geo.indicator_result USING btree (indicator_slug, feature_id) WHERE ((scenario_id IS NULL) AND (status = 'current'::text))
- `ir_feature_scenario_idx` — CREATE INDEX ir_feature_scenario_idx ON curated_geo.indicator_result USING btree (feature_id, scenario_id, indicator_id) WHERE (status = 'current'::text)
- `ir_job_run_idx` — CREATE INDEX ir_job_run_idx ON curated_geo.indicator_result USING btree (job_run_id)
- `ir_one_current_base` — CREATE UNIQUE INDEX ir_one_current_base ON curated_geo.indicator_result USING btree (feature_id, indicator_id) WHERE ((scenario_id IS NULL) AND (status = 'current'::text))
- `ir_one_current_scenario` — CREATE UNIQUE INDEX ir_one_current_scenario ON curated_geo.indicator_result USING btree (feature_id, scenario_id, indicator_id) WHERE ((scenario_id IS NOT NULL) AND (status = 'current'::text))
- `ir_scenario_slug_idx` — CREATE INDEX ir_scenario_slug_idx ON curated_geo.indicator_result USING btree (scenario_id, indicator_slug) WHERE (status = 'current'::text)
- `ir_stale_idx` — CREATE INDEX ir_stale_idx ON curated_geo.indicator_result USING btree (status) WHERE (status = 'stale'::text)

#### `curated_geo.ingestion_run`

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | uuid | no | gen_random_uuid() |
| `source_id` | uuid | no | — |
| `orchestrator` | text | no | — |
| `orchestrator_run_id` | text | no | — |
| `transformation_step` | text | no | — |
| `code_version` | text | no | — |
| `status` | text | no | — |
| `started_at` | timestamp with time zone | no | now() |
| `completed_at` | timestamp with time zone | yes | — |
| `details` | jsonb | no | '{}'::jsonb |

Constraints:

- `ingestion_run_check` — CHECK (((completed_at IS NULL) OR (completed_at >= started_at)))
- `ingestion_run_status_check` — CHECK ((status = ANY (ARRAY['running'::text, 'succeeded'::text, 'failed'::text, 'cancelled'::text])))
- `ingestion_run_source_id_fkey` — FOREIGN KEY (source_id) REFERENCES curated_geo.data_source_catalog(id)
- `ingestion_run_pkey` — PRIMARY KEY (id)
- `ingestion_run_source_id_orchestrator_orchestrator_run_id_tr_key` — UNIQUE (source_id, orchestrator, orchestrator_run_id, transformation_step)

Indexes:

- `ingestion_run_pkey` — CREATE UNIQUE INDEX ingestion_run_pkey ON curated_geo.ingestion_run USING btree (id)
- `ingestion_run_source_id_orchestrator_orchestrator_run_id_tr_key` — CREATE UNIQUE INDEX ingestion_run_source_id_orchestrator_orchestrator_run_id_tr_key ON curated_geo.ingestion_run USING btree (source_id, orchestrator, orchestrator_run_id, transformation_step)
- `ingestion_run_source_idx` — CREATE INDEX ingestion_run_source_idx ON curated_geo.ingestion_run USING btree (source_id)
- `ingestion_run_source_started_idx` — CREATE INDEX ingestion_run_source_started_idx ON curated_geo.ingestion_run USING btree (source_id, started_at DESC)
- `ingestion_run_status_idx` — CREATE INDEX ingestion_run_status_idx ON curated_geo.ingestion_run USING btree (status)

#### `curated_geo.observation`

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | uuid | no | gen_random_uuid() |
| `feature_id` | uuid | no | — |
| `attribute_key` | text | no | — |
| `value_type` | text | no | — |
| `value_numeric` | double precision | yes | — |
| `value_text` | text | yes | — |
| `value_json` | jsonb | yes | — |
| `source_id` | uuid | no | — |
| `ingestion_run_id` | uuid | no | — |
| `lineage_ref` | jsonb | yes | — |
| `is_current` | boolean | no | true |
| `valid_from` | timestamp with time zone | no | now() |
| `valid_to` | timestamp with time zone | yes | — |
| `created_at` | timestamp with time zone | no | now() |
| `updated_at` | timestamp with time zone | no | now() |

Constraints:

- `observation_attribute_key_check` — CHECK ((attribute_key <> ''::text))
- `observation_check` — CHECK (((is_current AND (valid_to IS NULL)) OR ((NOT is_current) AND (valid_to IS NOT NULL))))
- `observation_check1` — CHECK (((valid_to IS NULL) OR (valid_to >= valid_from)))
- `observation_value_type_check` — CHECK ((value_type = ANY (ARRAY['numeric'::text, 'text'::text, 'json'::text])))
- `observation_feature_id_fkey` — FOREIGN KEY (feature_id) REFERENCES curated_geo.feature(id) ON DELETE CASCADE
- `observation_ingestion_run_id_fkey` — FOREIGN KEY (ingestion_run_id) REFERENCES curated_geo.ingestion_run(id)
- `observation_source_id_fkey` — FOREIGN KEY (source_id) REFERENCES curated_geo.data_source_catalog(id)
- `observation_pkey` — PRIMARY KEY (id)

Indexes:

- `obs_attr_feat_current` — CREATE INDEX obs_attr_feat_current ON curated_geo.observation USING btree (attribute_key, feature_id) WHERE is_current
- `obs_feature_key_idx` — CREATE INDEX obs_feature_key_idx ON curated_geo.observation USING btree (feature_id, attribute_key)
- `obs_one_current` — CREATE UNIQUE INDEX obs_one_current ON curated_geo.observation USING btree (feature_id, attribute_key) WHERE is_current
- `obs_source_run_idx` — CREATE INDEX obs_source_run_idx ON curated_geo.observation USING btree (source_id, ingestion_run_id)
- `observation_ingestion_run_idx` — CREATE INDEX observation_ingestion_run_idx ON curated_geo.observation USING btree (ingestion_run_id)
- `observation_pkey` — CREATE UNIQUE INDEX observation_pkey ON curated_geo.observation USING btree (id)

#### `curated_geo.raster_layer`

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | uuid | no | gen_random_uuid() |
| `source_id` | uuid | no | — |
| `asset_id` | uuid | no | — |
| `layer_slug` | text | no | — |
| `logical_name` | text | yes | — |
| `description` | text | yes | — |
| `colormap_name` | text | yes | — |
| `rescale` | text | yes | — |
| `minzoom` | integer | no | 5 |
| `maxzoom` | integer | no | 14 |
| `published` | boolean | no | true |
| `aggregation_kind` | text | no | 'intensive'::text |
| `created_at` | timestamp with time zone | no | now() |

Constraints:

- `raster_layer_aggregation_kind_check` — CHECK ((aggregation_kind = ANY (ARRAY['intensive'::text, 'extensive'::text])))
- `raster_layer_asset_id_fkey` — FOREIGN KEY (asset_id) REFERENCES curated_geo.file_asset(id) ON DELETE CASCADE
- `raster_layer_source_id_fkey` — FOREIGN KEY (source_id) REFERENCES curated_geo.data_source_catalog(id) ON DELETE CASCADE
- `raster_layer_pkey` — PRIMARY KEY (id)
- `raster_layer_asset_id_key` — UNIQUE (asset_id)
- `raster_layer_layer_slug_key` — UNIQUE (layer_slug)
- `raster_layer_source_id_key` — UNIQUE (source_id)

Indexes:

- `raster_layer_asset_id_key` — CREATE UNIQUE INDEX raster_layer_asset_id_key ON curated_geo.raster_layer USING btree (asset_id)
- `raster_layer_layer_slug_key` — CREATE UNIQUE INDEX raster_layer_layer_slug_key ON curated_geo.raster_layer USING btree (layer_slug)
- `raster_layer_pkey` — CREATE UNIQUE INDEX raster_layer_pkey ON curated_geo.raster_layer USING btree (id)
- `raster_layer_published_idx` — CREATE INDEX raster_layer_published_idx ON curated_geo.raster_layer USING btree (published) WHERE published
- `raster_layer_source_id_key` — CREATE UNIQUE INDEX raster_layer_source_id_key ON curated_geo.raster_layer USING btree (source_id)

#### `curated_geo.road_edge`

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | bigint | no | nextval('curated_geo.road_edge_id_seq'::regclass) |
| `osm_id` | bigint | yes | — |
| `source` | bigint | no | — |
| `target` | bigint | no | — |
| `cost` | double precision | no | — |
| `reverse_cost` | double precision | no | — |
| `geometry` | geometry(LineString,4326) | no | — |

Constraints:

- `road_edge_cost_check` — CHECK ((cost >= (0)::double precision))
- `road_edge_reverse_cost_check` — CHECK ((reverse_cost >= (0)::double precision))
- `road_edge_source_fkey` — FOREIGN KEY (source) REFERENCES curated_geo.road_node(id) ON DELETE CASCADE
- `road_edge_target_fkey` — FOREIGN KEY (target) REFERENCES curated_geo.road_node(id) ON DELETE CASCADE
- `road_edge_pkey` — PRIMARY KEY (id)

Indexes:

- `road_edge_geom_gist` — CREATE INDEX road_edge_geom_gist ON curated_geo.road_edge USING gist (geometry)
- `road_edge_pkey` — CREATE UNIQUE INDEX road_edge_pkey ON curated_geo.road_edge USING btree (id)
- `road_edge_source_idx` — CREATE INDEX road_edge_source_idx ON curated_geo.road_edge USING btree (source)
- `road_edge_target_idx` — CREATE INDEX road_edge_target_idx ON curated_geo.road_edge USING btree (target)

#### `curated_geo.road_node`

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | bigint | no | — |
| `geometry` | geometry(Point,4326) | no | — |

Constraints:

- `road_node_pkey` — PRIMARY KEY (id)

Indexes:

- `road_node_geom_gist` — CREATE INDEX road_node_geom_gist ON curated_geo.road_node USING gist (geometry)
- `road_node_pkey` — CREATE UNIQUE INDEX road_node_pkey ON curated_geo.road_node USING btree (id)

#### `curated_geo.scenario_edit`

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | uuid | no | gen_random_uuid() |
| `scenario_id` | uuid | no | — |
| `ordinal` | integer | no | — |
| `geometry` | geometry(Geometry,4326) | no | — |
| `type_id` | uuid | no | — |
| `attribute_key` | text | no | — |
| `value_numeric` | double precision | no | — |
| `created_at` | timestamp with time zone | no | now() |

Constraints:

- `scenario_edit_geometry_check` — CHECK ((st_geometrytype(geometry) = ANY (ARRAY['ST_Polygon'::text, 'ST_MultiPolygon'::text])))
- `scenario_edit_scenario_id_fkey` — FOREIGN KEY (scenario_id) REFERENCES app_core.scenario(id) ON DELETE CASCADE
- `scenario_edit_type_id_fkey` — FOREIGN KEY (type_id) REFERENCES curated_geo.feature_type_catalog(id)
- `scenario_edit_pkey` — PRIMARY KEY (id)
- `scenario_edit_scenario_id_ordinal_key` — UNIQUE (scenario_id, ordinal)

Indexes:

- `scenario_edit_geometry_gist` — CREATE INDEX scenario_edit_geometry_gist ON curated_geo.scenario_edit USING gist (geometry)
- `scenario_edit_pkey` — CREATE UNIQUE INDEX scenario_edit_pkey ON curated_geo.scenario_edit USING btree (id)
- `scenario_edit_scenario_id_ordinal_key` — CREATE UNIQUE INDEX scenario_edit_scenario_id_ordinal_key ON curated_geo.scenario_edit USING btree (scenario_id, ordinal)
- `scenario_edit_scenario_idx` — CREATE INDEX scenario_edit_scenario_idx ON curated_geo.scenario_edit USING btree (scenario_id)

#### `curated_geo.scenario_override`

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | uuid | no | gen_random_uuid() |
| `scenario_id` | uuid | no | — |
| `feature_id` | uuid | no | — |
| `attribute_key` | text | no | — |
| `value_numeric` | double precision | yes | — |
| `value_text` | text | yes | — |
| `created_at` | timestamp with time zone | no | now() |

Constraints:

- `scenario_override_check` — CHECK ((num_nonnulls(value_numeric, value_text) = 1))
- `scenario_override_feature_id_fkey` — FOREIGN KEY (feature_id) REFERENCES curated_geo.feature(id) ON DELETE CASCADE
- `scenario_override_pkey` — PRIMARY KEY (id)
- `scenario_override_scenario_id_feature_id_attribute_key_key` — UNIQUE (scenario_id, feature_id, attribute_key)

Indexes:

- `scenario_override_lookup_idx` — CREATE INDEX scenario_override_lookup_idx ON curated_geo.scenario_override USING btree (scenario_id, attribute_key)
- `scenario_override_pkey` — CREATE UNIQUE INDEX scenario_override_pkey ON curated_geo.scenario_override USING btree (id)
- `scenario_override_scenario_id_feature_id_attribute_key_key` — CREATE UNIQUE INDEX scenario_override_scenario_id_feature_id_attribute_key_key ON curated_geo.scenario_override USING btree (scenario_id, feature_id, attribute_key)

#### `curated_geo.tile_snapshot_registry`

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | uuid | no | gen_random_uuid() |
| `layer_id` | text | no | — |
| `artifact_path` | text | no | — |
| `artifact_uri` | text | yes | — |
| `snapshot_version` | text | no | — |
| `source_relation` | text | no | — |
| `source_lineage_ref` | jsonb | no | '{}'::jsonb |
| `status` | text | no | 'ready'::text |
| `created_at` | timestamp with time zone | no | now() |

Constraints:

- `tile_snapshot_status_check` — CHECK ((status = ANY (ARRAY['ready'::text, 'failed'::text, 'superseded'::text])))
- `tile_snapshot_registry_pkey` — PRIMARY KEY (id)
- `tile_snapshot_registry_layer_id_snapshot_version_key` — UNIQUE (layer_id, snapshot_version)

Indexes:

- `tile_snapshot_layer_created_idx` — CREATE INDEX tile_snapshot_layer_created_idx ON curated_geo.tile_snapshot_registry USING btree (layer_id, created_at DESC)
- `tile_snapshot_registry_layer_id_snapshot_version_key` — CREATE UNIQUE INDEX tile_snapshot_registry_layer_id_snapshot_version_key ON curated_geo.tile_snapshot_registry USING btree (layer_id, snapshot_version)
- `tile_snapshot_registry_pkey` — CREATE UNIQUE INDEX tile_snapshot_registry_pkey ON curated_geo.tile_snapshot_registry USING btree (id)

#### Functions in `curated_geo`

- **`curated_geo.aoi_feature_before_write()`** → trigger (plpgsql). Checks that `overlap_basis` fits the feature's geometry: presence for points, length for lines, area for polygons.

- **`curated_geo.asset_dependency_validate()`** → trigger (plpgsql). Only a derived asset may consume; the graph must stay acyclic; `derived_from` weights of one consumer are all null or sum to 1.

- **`curated_geo.feature_before_write()`** → trigger (plpgsql). Copies `type_slug` and `role` from the feature type; checks the geometry family, that `source_id` matches the ingestion run, and the parent hierarchy (no self-parent, admin level strictly increasing, no cycles); stamps `updated_at`.

- **`curated_geo.file_asset_before_write()`** → trigger (plpgsql). The source must store raster, imagery or point-cloud data, and `source_id` must match the ingestion run.

- **`curated_geo.indicator_dependency_validate()`** → trigger (plpgsql). Only a composite indicator may have dependencies; the graph must stay acyclic; weights of one parent are all null or sum to 1.

- **`curated_geo.indicator_result_before_write()`** → trigger (plpgsql). Fills `feature_type`, `indicator_slug` (both immutable afterwards) and `model_version`; checks the scenario matches the job run, the feature type is one the indicator applies to, exactly one value for a measured, estimated or imputed result and none otherwise, a `missing_reason` for missing results, and the catalog's valid range.

- **`curated_geo.observation_before_write()`** → trigger (plpgsql). Exactly one value column is set and it matches `value_type`; `source_id` must match the ingestion run; stamps `updated_at`.

#### Triggers in `curated_geo`

- `aoi_feature_before_write` on `curated_geo.aoi_feature` — CREATE TRIGGER aoi_feature_before_write BEFORE INSERT ON curated_geo.aoi_feature FOR EACH ROW EXECUTE FUNCTION curated_geo.aoi_feature_before_write()
- `asset_dependency_validate_trigger` on `curated_geo.asset_dependency` — CREATE CONSTRAINT TRIGGER asset_dependency_validate_trigger AFTER INSERT OR DELETE OR UPDATE ON curated_geo.asset_dependency DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION curated_geo.asset_dependency_validate()
- `feature_before_write` on `curated_geo.feature` — CREATE TRIGGER feature_before_write BEFORE INSERT OR UPDATE ON curated_geo.feature FOR EACH ROW EXECUTE FUNCTION curated_geo.feature_before_write()
- `file_asset_before_write` on `curated_geo.file_asset` — CREATE TRIGGER file_asset_before_write BEFORE INSERT OR UPDATE ON curated_geo.file_asset FOR EACH ROW EXECUTE FUNCTION curated_geo.file_asset_before_write()
- `indicator_dependency_validate_trigger` on `curated_geo.indicator_dependency` — CREATE CONSTRAINT TRIGGER indicator_dependency_validate_trigger AFTER INSERT OR DELETE OR UPDATE ON curated_geo.indicator_dependency DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION curated_geo.indicator_dependency_validate()
- `indicator_result_before_write_trigger` on `curated_geo.indicator_result` — CREATE TRIGGER indicator_result_before_write_trigger BEFORE INSERT OR UPDATE ON curated_geo.indicator_result FOR EACH ROW EXECUTE FUNCTION curated_geo.indicator_result_before_write()
- `observation_before_write` on `curated_geo.observation` — CREATE TRIGGER observation_before_write BEFORE INSERT OR UPDATE ON curated_geo.observation FOR EACH ROW EXECUTE FUNCTION curated_geo.observation_before_write()

### `serving_geo`

The read contract for feature, indicator and map reads by the API, the worker and the tile server: views, one materialized view and the three vector-tile functions. Nothing writes rows here; the materialized view is refreshed when an ingestion run succeeds.

0 table(s), 23 view(s), 1 materialized view(s), 3 function(s).

#### Views in `serving_geo`

- **`serving_geo.admin_v`** — `id` uuid, `type_id` uuid, `type_slug` text, `role` text, `parent_id` uuid, `name` text, `geometry` geometry(Geometry,4326), `attributes` jsonb, `source_id` uuid, `source_record_ref` text, `dataset_version` text, `confidence` double precision, `model_version` text, `created_at` timestamp with time zone, `updated_at` timestamp with time zone

- **`serving_geo.aggregation_recipe_v`** — `id` uuid, `input_kind` text, `input_raster_layer_id` uuid, `input_raster_layer_slug` text, `input_attribute_key_prefix` text, `input_feature_type_slug` text, `target_feature_type_slug` text, `attribute_key_prefix` text, `method` text, `weight_basis` text, `params` jsonb, `coverage_threshold` double precision, `aoi_id` uuid, `enabled` boolean, `created_at` timestamp with time zone, `updated_at` timestamp with time zone

- **`serving_geo.analysis_units_v`** — `id` uuid, `type_id` uuid, `type_slug` text, `role` text, `parent_id` uuid, `name` text, `geometry` geometry(Geometry,4326), `attributes` jsonb, `source_id` uuid, `source_record_ref` text, `dataset_version` text, `confidence` double precision, `model_version` text, `created_at` timestamp with time zone, `updated_at` timestamp with time zone

- **`serving_geo.aoi_summary_v`** — `id` uuid, `aoi_id` uuid, `scenario_id` uuid, `indicator_id` uuid, `value` double precision, `aggregation_note` text, `status` text, `computed_at` timestamp with time zone

- **`serving_geo.asset_component_v`** — `asset_id` uuid, `asset_key` text, `partition_key` text, `part` text

- **`serving_geo.asset_expected_version_v`** — `asset_id` uuid, `asset_key` text, `partition_key` text, `expected_version` text

- **`serving_geo.asset_freshness_v`** — `asset_id` uuid, `asset_key` text, `kind` text, `partition_key` text, `expected_version` text, `materialized_version` text, `materialized_at` timestamp with time zone, `job_run_id` uuid, `coverage_complete` boolean, `state` text

- **`serving_geo.asset_lineage_v`** — `direction` text, `root_key` text, `related_key` text, `related_kind` text, `depth` integer

- **`serving_geo.blocks`** — `id` uuid, `type_id` uuid, `type_slug` text, `role` text, `parent_id` uuid, `name` text, `geometry` geometry(Geometry,4326), `attributes` jsonb, `source_id` uuid, `source_record_ref` text, `dataset_version` text, `confidence` double precision, `model_version` text, `created_at` timestamp with time zone, `updated_at` timestamp with time zone

- **`serving_geo.bus_stops`** — `id` uuid, `type_id` uuid, `type_slug` text, `role` text, `parent_id` uuid, `name` text, `geometry` geometry(Geometry,4326), `attributes` jsonb, `source_id` uuid, `source_record_ref` text, `dataset_version` text, `confidence` double precision, `model_version` text, `created_at` timestamp with time zone, `updated_at` timestamp with time zone

- **`serving_geo.current_indicator_v`** — `id` uuid, `feature_id` uuid, `feature_type` text, `scenario_id` uuid, `indicator_id` uuid, `indicator_slug` text, `indicator_name` text, `unit` text, `value_type` text, `computation_kind` text, `job_run_id` uuid, `model_version` text, `value_numeric` double precision, `value_text` text, `value_json` jsonb, `quality_flag` text, `confidence` double precision, `missing_reason` text, `computed_at` timestamp with time zone

- **`serving_geo.detections_v`** — `id` uuid, `type_id` uuid, `type_slug` text, `role` text, `parent_id` uuid, `name` text, `geometry` geometry(Geometry,4326), `attributes` jsonb, `source_id` uuid, `source_record_ref` text, `dataset_version` text, `confidence` double precision, `model_version` text, `created_at` timestamp with time zone, `updated_at` timestamp with time zone

- **`serving_geo.feature_aoi_v`** — `aoi_id` uuid, `overlap_basis` text, `overlap_measure` double precision, `feature_measure` double precision, `overlap_ratio` double precision, `id` uuid, `type_id` uuid, `type_slug` text, `role` text, `parent_id` uuid, `name` text, `geometry` geometry(Geometry,4326), `attributes` jsonb, `source_id` uuid, `source_record_ref` text, `dataset_version` text, `confidence` double precision, `model_version` text, `created_at` timestamp with time zone, `updated_at` timestamp with time zone, `geometry_centroid` geometry(Point,4326)

- **`serving_geo.feature_current_attributes_v`** — `feature_id` uuid, `current_attributes` jsonb

- **`serving_geo.feature_v`** — `id` uuid, `type_id` uuid, `type_slug` text, `role` text, `parent_id` uuid, `name` text, `geometry` geometry(Geometry,4326), `attributes` jsonb, `source_id` uuid, `source_record_ref` text, `dataset_version` text, `confidence` double precision, `model_version` text, `created_at` timestamp with time zone, `updated_at` timestamp with time zone, `geometry_centroid` geometry(Point,4326)

- **`serving_geo.infrastructure_v`** — `id` uuid, `type_id` uuid, `type_slug` text, `role` text, `parent_id` uuid, `name` text, `geometry` geometry(Geometry,4326), `attributes` jsonb, `source_id` uuid, `source_record_ref` text, `dataset_version` text, `confidence` double precision, `model_version` text, `created_at` timestamp with time zone, `updated_at` timestamp with time zone

- **`serving_geo.lots`** — `id` uuid, `type_id` uuid, `type_slug` text, `role` text, `parent_id` uuid, `name` text, `geometry` geometry(Geometry,4326), `attributes` jsonb, `source_id` uuid, `source_record_ref` text, `dataset_version` text, `confidence` double precision, `model_version` text, `created_at` timestamp with time zone, `updated_at` timestamp with time zone

- **`serving_geo.municipalities`** — `id` uuid, `type_id` uuid, `type_slug` text, `role` text, `parent_id` uuid, `name` text, `geometry` geometry(Geometry,4326), `attributes` jsonb, `source_id` uuid, `source_record_ref` text, `dataset_version` text, `confidence` double precision, `model_version` text, `created_at` timestamp with time zone, `updated_at` timestamp with time zone

- **`serving_geo.pois_v`** — `id` uuid, `type_id` uuid, `type_slug` text, `role` text, `parent_id` uuid, `name` text, `geometry` geometry(Geometry,4326), `attributes` jsonb, `source_id` uuid, `source_record_ref` text, `dataset_version` text, `confidence` double precision, `model_version` text, `created_at` timestamp with time zone, `updated_at` timestamp with time zone

- **`serving_geo.raster_layer_v`** — `id` uuid, `source_id` uuid, `asset_id` uuid, `layer_slug` text, `logical_name` text, `description` text, `colormap_name` text, `rescale` text, `minzoom` integer, `maxzoom` integer, `published` boolean, `aggregation_kind` text, `created_at` timestamp with time zone, `storage_uri` text, `crs` text, `resolution_m` double precision, `format` text, `band_description` jsonb

- **`serving_geo.roads`** — `id` uuid, `type_id` uuid, `type_slug` text, `role` text, `parent_id` uuid, `name` text, `geometry` geometry(Geometry,4326), `attributes` jsonb, `source_id` uuid, `source_record_ref` text, `dataset_version` text, `confidence` double precision, `model_version` text, `created_at` timestamp with time zone, `updated_at` timestamp with time zone

- **`serving_geo.sidewalks`** — `id` uuid, `type_id` uuid, `type_slug` text, `role` text, `parent_id` uuid, `name` text, `geometry` geometry(Geometry,4326), `attributes` jsonb, `source_id` uuid, `source_record_ref` text, `dataset_version` text, `confidence` double precision, `model_version` text, `created_at` timestamp with time zone, `updated_at` timestamp with time zone

- **`serving_geo.trees`** — `id` uuid, `type_id` uuid, `type_slug` text, `role` text, `parent_id` uuid, `name` text, `geometry` geometry(Geometry,4326), `attributes` jsonb, `source_id` uuid, `source_record_ref` text, `dataset_version` text, `confidence` double precision, `model_version` text, `created_at` timestamp with time zone, `updated_at` timestamp with time zone

#### Materialized views in `serving_geo`

- **`serving_geo.feature_current_attributes_mv`** — `feature_id` uuid, `current_attributes` jsonb

Indexes:

- `feature_current_attributes_mv_feature_id_idx` — CREATE UNIQUE INDEX feature_current_attributes_mv_feature_id_idx ON serving_geo.feature_current_attributes_mv USING btree (feature_id)

#### Functions in `serving_geo`

- **`serving_geo.aggregation_overlay_tile(z integer, x integer, y integer, query_params json)`** → bytea (sql). Martin function source — MVT choropleth for aggregation-recipe observations. Required: attribute_key, feature_type_slug. Optional: aoi_id (filters features to that AOI's membership). Returns empty below z=10.

- **`serving_geo.get_tile(z integer, x integer, y integer, query_params json)`** → bytea (sql). Martin function source — returns a Mapbox Vector Tile for the requested z/x/y. Optional query params: type_slug, role. Geometry is simplified at z < 14 and attributes are omitted below z 12 to keep tiles small.

- **`serving_geo.indicator_overlay_tile(z integer, x integer, y integer, query_params json)`** → bytea (sql). Martin function source — MVT choropleth for one indicator. Required: indicator_slug. Optional: scenario_id, feature_type, aoi_id (aoi_id filters features to that AOI's membership). Returns empty below z=10.

### `explore`

The engineer-facing inspection contract: views built for humans reading SQL. One application read goes through it: an indicator's lineage (`GET /indicators/{slug}/lineage`) reads `explore.asset_freshness_v`.

1 table(s), 11 view(s), 0 materialized view(s), 0 function(s).

#### `explore.layer_catalog`

| Column | Type | Null | Default |
|---|---|---|---|
| `schema_name` | text | no | — |
| `object_name` | text | no | — |
| `logical_name` | text | no | — |
| `layer_kind` | text | no | — |
| `description` | text | no | — |
| `primary_geometry_column` | text | yes | — |
| `intended_use` | text | no | — |
| `display_order` | integer | no | 1000 |

Constraints:

- `layer_catalog_pkey` — PRIMARY KEY (schema_name, object_name)

Indexes:

- `layer_catalog_pkey` — CREATE UNIQUE INDEX layer_catalog_pkey ON explore.layer_catalog USING btree (schema_name, object_name)

#### Views in `explore`

- **`explore.aggregation_recipe_v`** — `id` text, `input_kind` text, `input_raster_layer_slug` text, `input_attribute_key_prefix` text, `input_feature_type_slug` text, `target_feature_type_slug` text, `attribute_key_prefix` text, `method` text, `weight_basis` text, `coverage_threshold` double precision, `enabled` boolean, `created_at` timestamp with time zone

- **`explore.asset_freshness_v`** — `asset_key` text, `kind` text, `partition_key` text, `state` text, `expected_version` text, `materialized_version` text, `coverage_complete` boolean, `materialized_at` timestamp with time zone

- **`explore.asset_lineage_v`** — `direction` text, `root_key` text, `related_key` text, `related_kind` text, `depth` integer

- **`explore.asset_v`** — `asset_key` text, `kind` text, `backing_kind` text, `backing_ref` text, `version_strategy` text, `code_version` text, `partition_dimensions` text[], `feature_type_context` text, `feature_type_slug` text, `description` text, `created_at` timestamp with time zone

- **`explore.feature_type_stats_v`** — `type_slug` text, `role` text, `source_id` text, `dataset_version` text, `row_count` bigint

- **`explore.layer_catalog_v`** — `schema_name` text, `object_name` text, `logical_name` text, `layer_kind` text, `description` text, `primary_geometry_column` text, `intended_use` text

- **`explore.ops_executor_health_v`** — `executor_id` text, `role` text, `app_version` text, `started_at` timestamp with time zone, `heartbeat_at` timestamp with time zone, `heartbeat_age` interval

- **`explore.raster_layer_v`** — `layer_slug` text, `published` boolean, `aggregation_kind` text, `colormap_name` text, `rescale` text, `minzoom` integer, `maxzoom` integer, `source_name` text, `source_version` text, `format` text, `crs` text, `resolution_m` double precision, `storage_uri` text

- **`explore.scenario_v`** — `id` uuid, `project_id` uuid, `name` text, `description` text, `aoi_id` uuid, `edit_count` bigint, `override_count` bigint, `materialized_at` timestamp with time zone, `created_at` timestamp with time zone, `updated_at` timestamp with time zone

- **`explore.serving_stats_v`** — `stat_scope` text, `type_slug` text, `role` text, `row_count` bigint

- **`explore.source_runs_v`** — `source_id` text, `source_name` text, `provider` text, `storage_type` text, `integration_role` text, `version` text, `storage_uri` text, `ingestion_run_id` text, `ingestion_status` text, `transformation_step` text, `orchestrator` text, `orchestrator_run_id` text, `started_at` timestamp with time zone, `completed_at` timestamp with time zone, `details` jsonb

### `jobs_runtime`

Run and job bookkeeping for the ops plane. The API reads and writes it to submit, list, cancel and delete runs.

5 table(s), 0 view(s), 0 materialized view(s), 0 function(s).

#### `jobs_runtime.job_run`

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | uuid | no | gen_random_uuid() |
| `scenario_id` | uuid | yes | — |
| `model_family` | text | no | — |
| `status` | text | no | — |
| `code_version` | text | no | — |
| `queued_at` | timestamp with time zone | no | now() |
| `started_at` | timestamp with time zone | yes | — |
| `completed_at` | timestamp with time zone | yes | — |
| `error_message` | text | yes | — |
| `run_id` | uuid | yes | — |
| `backend_pid` | integer | yes | — |

Constraints:

- `job_run_check` — CHECK (((started_at IS NULL) OR (started_at >= queued_at)))
- `job_run_check1` — CHECK (((completed_at IS NULL) OR (started_at IS NULL) OR (completed_at >= started_at)))
- `job_run_status_check` — CHECK ((status = ANY (ARRAY['queued'::text, 'running'::text, 'succeeded'::text, 'failed'::text, 'cancelled'::text])))
- `job_run_pkey` — PRIMARY KEY (id)

Indexes:

- `job_run_family_idx` — CREATE INDEX job_run_family_idx ON jobs_runtime.job_run USING btree (model_family, status)
- `job_run_pkey` — CREATE UNIQUE INDEX job_run_pkey ON jobs_runtime.job_run USING btree (id)
- `job_run_run_idx` — CREATE INDEX job_run_run_idx ON jobs_runtime.job_run USING btree (run_id, status) WHERE (run_id IS NOT NULL)
- `job_run_scenario_idx` — CREATE INDEX job_run_scenario_idx ON jobs_runtime.job_run USING btree (scenario_id)
- `job_run_status_idx` — CREATE INDEX job_run_status_idx ON jobs_runtime.job_run USING btree (status)

#### `jobs_runtime.job_unit`

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | uuid | no | gen_random_uuid() |
| `run_id` | uuid | no | — |
| `indicator_slug` | text | no | — |
| `feature_scope` | jsonb | no | '[]'::jsonb |
| `status` | text | no | — |
| `detail` | text | yes | — |
| `job_run_id` | uuid | yes | — |
| `queued_at` | timestamp with time zone | no | now() |
| `started_at` | timestamp with time zone | yes | — |
| `completed_at` | timestamp with time zone | yes | — |

Constraints:

- `job_unit_status_check` — CHECK ((status = ANY (ARRAY['queued'::text, 'running'::text, 'succeeded'::text, 'failed'::text, 'skipped'::text, 'cancelled'::text])))
- `job_unit_job_run_id_fkey` — FOREIGN KEY (job_run_id) REFERENCES jobs_runtime.job_run(id) ON DELETE SET NULL
- `job_unit_run_id_fkey` — FOREIGN KEY (run_id) REFERENCES jobs_runtime.orchestration_run(id) ON DELETE CASCADE
- `job_unit_pkey` — PRIMARY KEY (id)
- `job_unit_run_id_indicator_slug_key` — UNIQUE (run_id, indicator_slug)

Indexes:

- `job_unit_pkey` — CREATE UNIQUE INDEX job_unit_pkey ON jobs_runtime.job_unit USING btree (id)
- `job_unit_run_id_indicator_slug_key` — CREATE UNIQUE INDEX job_unit_run_id_indicator_slug_key ON jobs_runtime.job_unit USING btree (run_id, indicator_slug)
- `job_unit_run_idx` — CREATE INDEX job_unit_run_idx ON jobs_runtime.job_unit USING btree (run_id, status)
- `job_unit_success_by_slug_idx` — CREATE INDEX job_unit_success_by_slug_idx ON jobs_runtime.job_unit USING btree (indicator_slug, completed_at DESC) WHERE (status = 'succeeded'::text)

#### `jobs_runtime.ops_executor_heartbeat`

| Column | Type | Null | Default |
|---|---|---|---|
| `executor_id` | text | no | — |
| `role` | text | no | — |
| `app_version` | text | yes | — |
| `started_at` | timestamp with time zone | no | clock_timestamp() |
| `heartbeat_at` | timestamp with time zone | no | clock_timestamp() |

Constraints:

- `ops_executor_heartbeat_executor_id_check` — CHECK ((length(executor_id) > 0))
- `ops_executor_heartbeat_role_check` — CHECK ((length(role) > 0))
- `ops_executor_heartbeat_pkey` — PRIMARY KEY (executor_id)

Indexes:

- `ops_executor_heartbeat_at_idx` — CREATE INDEX ops_executor_heartbeat_at_idx ON jobs_runtime.ops_executor_heartbeat USING btree (heartbeat_at DESC)
- `ops_executor_heartbeat_pkey` — CREATE UNIQUE INDEX ops_executor_heartbeat_pkey ON jobs_runtime.ops_executor_heartbeat USING btree (executor_id)

#### `jobs_runtime.orchestration_run`

| Column | Type | Null | Default |
|---|---|---|---|
| `id` | uuid | no | gen_random_uuid() |
| `scenario_id` | uuid | yes | — |
| `status` | text | no | — |
| `code_version` | text | no | — |
| `submitted_at` | timestamp with time zone | no | now() |
| `completed_at` | timestamp with time zone | yes | — |
| `aoi_id` | text | yes | — |

Constraints:

- `orchestration_run_check` — CHECK (((completed_at IS NULL) OR (completed_at >= submitted_at)))
- `orchestration_run_status_check` — CHECK ((status = ANY (ARRAY['queued'::text, 'running'::text, 'succeeded'::text, 'partial'::text, 'failed'::text, 'cancelling'::text, 'cancelled'::text])))
- `orchestration_run_pkey` — PRIMARY KEY (id)

Indexes:

- `orchestration_run_pkey` — CREATE UNIQUE INDEX orchestration_run_pkey ON jobs_runtime.orchestration_run USING btree (id)
- `orchestration_run_status_idx` — CREATE INDEX orchestration_run_status_idx ON jobs_runtime.orchestration_run USING btree (status)
- `orchestration_run_submitted_at_idx` — CREATE INDEX orchestration_run_submitted_at_idx ON jobs_runtime.orchestration_run USING btree (submitted_at DESC)

#### `jobs_runtime.run_plan`

| Column | Type | Null | Default |
|---|---|---|---|
| `plan_id` | uuid | no | — |
| `indicator_slug` | text | no | — |
| `feature_ids` | uuid[] | no | — |
| `created_at` | timestamp with time zone | no | now() |

Constraints:

- `run_plan_pkey` — PRIMARY KEY (plan_id, indicator_slug)

Indexes:

- `run_plan_pkey` — CREATE UNIQUE INDEX run_plan_pkey ON jobs_runtime.run_plan USING btree (plan_id, indicator_slug)

### Geometry columns

Every geometry column, with the SRID and dimension enforced by its type. A single SRID across the platform is what lets any two layers be compared without a reprojection step.

| Table | Column | Type | SRID | Dimensions |
|---|---|---|---|---|
| `curated_geo.area_of_interest` | `geometry` | GEOMETRY | 4326 | 2 |
| `curated_geo.data_source_catalog` | `source_extent_geometry` | POLYGON | 4326 | 2 |
| `curated_geo.feature` | `geometry` | GEOMETRY | 4326 | 2 |
| `curated_geo.feature` | `geometry_centroid` | POINT | 4326 | 2 |
| `curated_geo.file_asset` | `asset_extent_geometry` | POLYGON | 4326 | 2 |
| `curated_geo.road_edge` | `geometry` | LINESTRING | 4326 | 2 |
| `curated_geo.road_node` | `geometry` | POINT | 4326 | 2 |
| `curated_geo.scenario_edit` | `geometry` | GEOMETRY | 4326 | 2 |
| `serving_geo.admin_v` | `geometry` | GEOMETRY | 4326 | 2 |
| `serving_geo.analysis_units_v` | `geometry` | GEOMETRY | 4326 | 2 |
| `serving_geo.blocks` | `geometry` | GEOMETRY | 4326 | 2 |
| `serving_geo.bus_stops` | `geometry` | GEOMETRY | 4326 | 2 |
| `serving_geo.detections_v` | `geometry` | GEOMETRY | 4326 | 2 |
| `serving_geo.feature_aoi_v` | `geometry` | GEOMETRY | 4326 | 2 |
| `serving_geo.feature_aoi_v` | `geometry_centroid` | POINT | 4326 | 2 |
| `serving_geo.feature_v` | `geometry` | GEOMETRY | 4326 | 2 |
| `serving_geo.feature_v` | `geometry_centroid` | POINT | 4326 | 2 |
| `serving_geo.infrastructure_v` | `geometry` | GEOMETRY | 4326 | 2 |
| `serving_geo.lots` | `geometry` | GEOMETRY | 4326 | 2 |
| `serving_geo.municipalities` | `geometry` | GEOMETRY | 4326 | 2 |
| `serving_geo.pois_v` | `geometry` | GEOMETRY | 4326 | 2 |
| `serving_geo.roads` | `geometry` | GEOMETRY | 4326 | 2 |
| `serving_geo.sidewalks` | `geometry` | GEOMETRY | 4326 | 2 |
| `serving_geo.trees` | `geometry` | GEOMETRY | 4326 | 2 |

## API

Taken from the application's own OpenAPI document (`reimagina API` 0.1.0, OpenAPI 3.1.0), served at `/openapi.json` and rendered at `/docs`.

The API exposes **62 operations** in total. **43** are written for this project; the remaining **19** belong to titiler's raster router, which is mounted in-process rather than deployed separately, and are listed apart for that reason.

Each project operation below gives its description, its parameters (where they travel, their type and whether they are required), its request body and its responses, naming the schema of each JSON body; the schemas themselves follow in [Schemas](#schemas). Every operation that takes input can also answer 422 with FastAPI's `HTTPValidationError`; that response is not repeated per operation.

### Project endpoints (43)

#### `aois`

##### `GET /aois`

Every AOI registered in the current database.

- Response 200: `list[AOIRow]`

##### `GET /aois/{aoi_id}/summaries`

Cached aoi_indicator_summary rows for one AOI.

| Parameter | Type | Description |
|---|---|---|
| `aoi_id` (path, required) | string (uuid) | — |
| `scenario_id` (query, optional) | string (uuid) or null | — |
| `only_current` (query, optional) | boolean, default `true` | — |

- Response 200: `list[AOISummary]`

#### `features`

##### `GET /features`

Paginated feature inventory — wraps ServingReader.get_features.

| Parameter | Type | Description |
|---|---|---|
| `type_slug` (query, optional) | string or null | — |
| `role` (query, optional) | string or null | — |
| `aoi_id` (query, optional) | string (uuid) or null | — |
| `limit` (query, optional) | integer, default `500` | — |
| `after_cursor` (query, optional) | string or null | — |

- Response 200: `FeaturePage`

##### `GET /features/{feature_id}/indicators`

The feature's current values, each ranked among its peers in the AOI.

| Parameter | Type | Description |
|---|---|---|
| `feature_id` (path, required) | string (uuid) | — |
| `aoi_id` (query, optional) | string (uuid) or null | — |
| `scenario_id` (query, optional) | string (uuid) or null | — |

- Response 200: `list[FeatureIndicatorValue]`

#### `indicators`

##### `GET /indicators`

Indicators in the catalog, optionally only those with values in an AOI.

| Parameter | Type | Description |
|---|---|---|
| `aoi_id` (query, optional) | string (uuid) or null | Restrict to indicators that hold at least one current value on a feature in this AOI. Omitted, the full catalog is returned — a registered indicator with no values yet is a real catalog entry, just not something a picker should offer. |
| `scenario_id` (query, optional) | string (uuid) or null | Which results count toward `aoi_id`: this scenario's and the baseline's, rather than the baseline's alone. Needed because the edificability indicators exist only inside a scenario, so without it the picker hides exactly what a scenario was authored to show. A well-formed id naming no scenario offers the baseline set — the same thing that would render. No effect without `aoi_id`, which is what narrows. |

- Response 200: `list[IndicatorSummary]`

##### `GET /indicators/{slug}`

One indicator's full catalog metadata, by slug.

| Parameter | Type | Description |
|---|---|---|
| `slug` (path, required) | string | — |

- Response 200: `IndicatorSummary`

##### `GET /indicators/{slug}/dependencies`

What `slug` is computed from, and what is computed from it.

| Parameter | Type | Description |
|---|---|---|
| `slug` (path, required) | string | — |

- Response 200: `IndicatorDependencies`

##### `GET /indicators/{slug}/lineage`

The asset closure around `slug`, in both directions.

| Parameter | Type | Description |
|---|---|---|
| `slug` (path, required) | string | — |

- Response 200: `IndicatorLineage`

##### `GET /indicators/{slug}/stats`

Return count + percentiles for an indicator's current numeric values.

| Parameter | Type | Description |
|---|---|---|
| `slug` (path, required) | string | — |
| `scenario_id` (query, optional) | string (uuid) or null | — |
| `feature_type` (query, optional) | string or null | — |
| `aoi_id` (query, optional) | string (uuid) or null | Optional AOI scope. When provided, percentiles are computed from features overlapping that AOI only — so multi-site UIs get a colour ramp tuned to the picked city's distribution. |
| `bins` (query, optional) | integer, default `0` | When > 0, also return a `histogram` of equal-width bin counts over the full observed value range, computed from the same scoped population as the percentiles. The legend uses it to show distribution shape, which min/max/median cannot: two territories with identical summary statistics can have completely different distributions. |

- Response 200: `IndicatorValueStats`

##### `GET /indicators/{slug}/values`

Current indicator results, one row per feature.

| Parameter | Type | Description |
|---|---|---|
| `slug` (path, required) | string | — |
| `scenario_id` (query, optional) | string (uuid) or null | Optional scenario filter; base city if omitted |
| `limit` (query, optional) | integer, default `1000` | — |

- Response 200: `list[IndicatorValue]`

##### `GET /indicators/{slug}/values.csv`

Stream a CSV of current indicator values.

| Parameter | Type | Description |
|---|---|---|
| `slug` (path, required) | string | — |
| `scenario_id` (query, optional) | string (uuid) or null | — |
| `feature_type` (query, optional) | string or null | — |
| `aoi_id` (query, optional) | string (uuid) or null | Optional AOI scope, as on /stats |
| `limit` (query, optional) | integer, default `100000` | — |

- Response 200: no declared body

##### `GET /indicators/{slug}/values.geojson`

Return a GeoJSON FeatureCollection in one response.

| Parameter | Type | Description |
|---|---|---|
| `slug` (path, required) | string | — |
| `scenario_id` (query, optional) | string (uuid) or null | — |
| `feature_type` (query, optional) | string or null | — |
| `aoi_id` (query, optional) | string (uuid) or null | Optional AOI scope, as on /stats |
| `limit` (query, optional) | integer, default `100000` | — |

- Response 200: `application/json`

##### `GET /indicators/{slug}/values.parquet`

Return a GeoParquet byte payload (EPSG:4326).

| Parameter | Type | Description |
|---|---|---|
| `slug` (path, required) | string | — |
| `scenario_id` (query, optional) | string (uuid) or null | — |
| `feature_type` (query, optional) | string or null | — |
| `aoi_id` (query, optional) | string (uuid) or null | Optional AOI scope, as on /stats |
| `limit` (query, optional) | integer, default `100000` | — |

- Response 200: `application/json`

#### `jobs`

##### `DELETE /jobs`

Forget every finished run, leaving live ones alone.

- Response 200: `DeletedRuns`

##### `GET /jobs`

The Ops dashboard's index, in one response.

| Parameter | Type | Description |
|---|---|---|
| `status` (query, optional) | list[RunStatus] or null | — |
| `limit` (query, optional) | integer, default `50` | — |
| `offset` (query, optional) | integer, default `0` | — |

- Response 200: `RunListResponse`

##### `POST /jobs`

Submit Run

- Request body (application/json, required): `SubmitRunBody`
- Response 202: `SubmitRunResponse`

##### `GET /jobs/attributes`

What a change-set can name, derived from the registry's own edges.

- Response 200: `ChangeVocabulary`

##### `POST /jobs/preview`

Dry-run the planner: which indicators would this change-set recompute?

- Request body (application/json, required): `SubmitRunBody`
- Response 200: `ImpactPreview`

##### `DELETE /jobs/{run_id}`

Forget one finished run. 404 unknown, 409 still live.

| Parameter | Type | Description |
|---|---|---|
| `run_id` (path, required) | string | — |

- Response 200: `DeletedRuns`

##### `GET /jobs/{run_id}`

Get Run

| Parameter | Type | Description |
|---|---|---|
| `run_id` (path, required) | string | — |

- Response 200: `RunView`

##### `POST /jobs/{run_id}/cancel`

Stop a live run. 404 unknown, 409 already finished.

| Parameter | Type | Description |
|---|---|---|
| `run_id` (path, required) | string | — |

- Response 200: `RunView`

##### `GET /jobs/{run_id}/detail`

One run with per-unit timings — the drill-down the run list links to.

| Parameter | Type | Description |
|---|---|---|
| `run_id` (path, required) | string | — |

- Response 200: `RunDetailResponse`

##### `GET /jobs/{run_id}/events`

404 an unknown run, then stream. Owns its connection for the check only.

| Parameter | Type | Description |
|---|---|---|
| `run_id` (path, required) | string | — |

- Response 200: `application/json`

##### `POST /jobs/{run_id}/rerun`

Recompute this run's non-succeeded indicators, as a **new** run.

| Parameter | Type | Description |
|---|---|---|
| `run_id` (path, required) | string | — |

- Response 202: `RerunResponse`

#### `meta`

##### `GET /health`

Alias for /health/ready. Kept to avoid breaking older callers.

- Response 200: `ReadinessResponse`

##### `GET /health/live`

Process-alive probe. Never touches the database.

- Response 200: `LivenessResponse`

##### `GET /health/ready`

Readiness probe. 200 if the DB is reachable; 503 otherwise.

- Response 200: `ReadinessResponse`

##### `GET /health/tile-server`

Report Martin connectivity + flag API/Martin DB drift.

- Response 200: `TileServerResponse`

#### `metrics`

##### `POST /metrics/indicator-within-polygon`

An indicator's stats over the features whose centroid is in the polygon.

- Request body (application/json, required): `IndicatorWithinPolygonRequest`
- Response 200: `IndicatorValueStats`

##### `POST /metrics/within-polygon`

Count features intersecting a GeoJSON polygon, grouped by type_slug.

- Request body (application/json, required): `WithinPolygonRequest`
- Response 200: `WithinPolygonResponse`

#### `scenarios`

##### `GET /scenarios`

Scenarios, newest first, optionally narrowed by project and/or AOI.

| Parameter | Type | Description |
|---|---|---|
| `project_id` (query, optional) | string (uuid) or null | — |
| `aoi_id` (query, optional) | string (uuid) or null | — |

- Response 200: `list[ScenarioListRow]`

##### `POST /scenarios`

Create a named what-if and answer with it, as ``GET`` would.

- Request body (application/json, required): `CreateScenarioBody`
- Response 201: `ScenarioDetail`

##### `GET /scenarios/indicators`

The indicators a scenario rule can change, inputs before dependents.

- Response 200: `list[ReachableIndicator]`

##### `DELETE /scenarios/{scenario_id}`

Delete a scenario and the four traces that would otherwise outlive it.

| Parameter | Type | Description |
|---|---|---|
| `scenario_id` (path, required) | string | — |

- Response 200: `DeleteScenarioResponse`

##### `GET /scenarios/{scenario_id}`

One scenario with its edits in ordinal order, and its override count.

| Parameter | Type | Description |
|---|---|---|
| `scenario_id` (path, required) | string | — |

- Response 200: `ScenarioDetail`

##### `GET /scenarios/{scenario_id}/comparison`

One indicator under the scenario against the baseline it departs from.

| Parameter | Type | Description |
|---|---|---|
| `scenario_id` (path, required) | string | — |
| `indicator_slug` (query, required) | string | Indicator to compare. |
| `aoi_id` (query, optional) | string (uuid) or null | — |
| `limit` (query, optional) | integer or null | Per-feature rows to return. Omitted, every row is returned; `row_count` and `summary` always cover all of them. |

- Response 200: `ComparisonResponse`

##### `DELETE /scenarios/{scenario_id}/edits`

Remove one rule from this scenario.

| Parameter | Type | Description |
|---|---|---|
| `scenario_id` (path, required) | string | — |
| `edit_id` (query, required) | string (uuid) | The edit to remove. |

- Response 204: no declared body

##### `POST /scenarios/{scenario_id}/edits`

Draw one area and write the four edits of one proposal over it.

| Parameter | Type | Description |
|---|---|---|
| `scenario_id` (path, required) | string | — |

- Request body (application/json, required): `ProposalBody`
- Response 201: `EditsWritten`

##### `POST /scenarios/{scenario_id}/recompute`

Materialise the scenario's edits, then enqueue the recompute.

| Parameter | Type | Description |
|---|---|---|
| `scenario_id` (path, required) | string | — |

- Response 202: `RecomputeResponse`

#### `tiles`

##### `GET /tile-sources`

Resolve every tile layer (vector manifest, PMTiles snapshot, raster layer registry) into a frontend-ready source.

| Parameter | Type | Description |
|---|---|---|
| `live_tilejson_base_url` (query, optional) | string or null | Base URL Martin exposes the tile function at. Defaults to REIMAGINA_TILE_LIVE_BASE_URL when omitted. |
| `snapshot_base_url` (query, optional) | string or null | Static host serving PMTiles snapshot artifacts. Defaults to REIMAGINA_TILE_SNAPSHOT_BASE_URL when omitted. |
| `raster_base_url` (query, optional) | string or null | Base URL of the titiler COG endpoint (e.g. http://localhost:8001/cog for the in-process mount). Defaults to REIMAGINA_TILE_RASTER_BASE_URL when omitted. |

- Response 200: `list[TileSource]`

##### `GET /tile-sources/attribution`

Credit lines for every source with features loaded.

- Response 200: `list[AttributionRow]`

#### `vector-tiles`

##### `GET /tiles/{function}`

TileJSON 3.0.0 for one tile function.

| Parameter | Type | Description |
|---|---|---|
| `function` (path, required) | string | A function in `serving_geo` |

- Response 200: `object`
- Response 404: no declared body — Not a servable tile function

##### `GET /tiles/{function}/{z}/{x}/{y}`

Serve one tile from a `serving_geo` tile function.

| Parameter | Type | Description |
|---|---|---|
| `function` (path, required) | string | A function in `serving_geo` |
| `z` (path, required) | integer | Zoom |
| `x` (path, required) | integer | Tile column |
| `y` (path, required) | integer | Tile row |
| `accept-encoding` (header, optional) | string or null | — |
| `if-none-match` (header, optional) | string or null | — |

- Response 200: `application/vnd.mapbox-vector-tile` — A tile with features
- Response 204: no declared body — A valid tile with nothing in it
- Response 304: no declared body — The client's cached copy is current
- Response 404: no declared body — Not a servable tile function
- Response 422: no declared body — The coordinate addresses no tile that can exist

### Schemas

The 54 component schemas the project endpoints use, directly or nested, with each field's type and whether it is required. Titiler's own schemas are omitted.

#### `AOIRow`

Minimal AOI record. Geometry omitted — the web app fetches tiles instead.

| Field | Type | Description |
|---|---|---|
| `aoi_id` (required) | string | — |
| `project_id` (required) | string | — |
| `name` (required) | string | — |
| `description` (optional) | string or null | — |
| `bbox` (optional) | list[number] or null | — |
| `centroid` (optional) | list[number] or null | — |
| `features_bbox` (optional) | list[number] or null | — |

#### `AOISummary`

Shape of an aoi_indicator_summary row, API-facing.

| Field | Type | Description |
|---|---|---|
| `aoi_id` (required) | string | — |
| `scenario_id` (optional) | string or null | — |
| `indicator_id` (required) | string | — |
| `value` (optional) | number or null | — |
| `aggregation_note` (optional) | string or null | — |
| `status` (required) | string | — |
| `computed_at` (required) | string (date-time) | — |

#### `AttributionRow`

| Field | Type | Description |
|---|---|---|
| `provider` (required) | string | — |
| `license` (optional) | string or null | — |
| `text` (required) | string | — |

#### `ChangeInput`

One thing a change-set can name, with the dataset it belongs to.

| Field | Type | Description |
|---|---|---|
| `dataset` (required) | string | — |
| `attribute` (required) | string | — |
| `indicators` (optional) | list[string], default `[]` | — |
| `is_version_marker` (optional) | boolean, default `false` | — |

#### `ChangeVocabulary`

| Field | Type | Description |
|---|---|---|
| `attributes` (optional) | list[ChangeInput], default `[]` | — |
| `datasets` (optional) | list[DatasetDependency], default `[]` | — |
| `unconsumed` (optional) | list[string], default `[]` | — |

#### `ComparisonResponse`

| Field | Type | Description |
|---|---|---|
| `scenario_id` (required) | string | — |
| `indicator_slug` (required) | string | — |
| `aoi_id` (optional) | string or null | — |
| `summary` (required) | ComparisonSummary | — |
| `row_count` (optional) | integer, default `0` | — |
| `rows` (optional) | list[ComparisonRow], default `[]` | — |
| `rows_truncated` (optional) | boolean, default `false` | — |

#### `ComparisonRow`

| Field | Type | Description |
|---|---|---|
| `feature_id` (required) | string | — |
| `baseline` (optional) | number or null | — |
| `scenario` (optional) | number or null | — |
| `delta` (optional) | number or null | — |
| `state` (required) | string | — |

#### `ComparisonSummary`

⚠ Three counts and no total, on purpose.

| Field | Type | Description |
|---|---|---|
| `changed` (optional) | integer, default `0` | — |
| `unchanged` (optional) | integer, default `0` | — |
| `new` (optional) | integer, default `0` | — |
| `mean_delta` (optional) | number or null | — |
| `higher_is_better` (optional) | boolean, default `true` | — |

#### `CreateScenarioBody`

A named what-if inside a project, optionally bounded by an AOI.

| Field | Type | Description |
|---|---|---|
| `project_id` (required) | string (uuid) | — |
| `name` (required) | string | — |
| `aoi_id` (optional) | string (uuid) or null | — |
| `description` (optional) | string or null | — |

#### `DatasetDependency`

A whole-dataset input — no attribute, so no way to name it in a change-set.

| Field | Type | Description |
|---|---|---|
| `dataset` (required) | string | — |
| `relation` (required) | string | — |
| `indicators` (optional) | list[string], default `[]` | — |
| `stand_in_attribute` (optional) | string or null | — |

#### `DeleteScenarioResponse`

Four counts, one per trace that would otherwise outlive the scenario.

| Field | Type | Description |
|---|---|---|
| `scenario_id` (required) | string | — |
| `overrides` (optional) | integer, default `0` | — |
| `versions_retired` (optional) | integer, default `0` | — |
| `results_superseded` (optional) | integer, default `0` | — |
| `materializations` (optional) | integer, default `0` | — |

#### `DeletedRuns`

| Field | Type | Description |
|---|---|---|
| `deleted` (required) | integer | — |

#### `DependencyEdge`

One composite → input edge. `weight` is the input's share of the composite and is null for a dependency that carries no weighting.

| Field | Type | Description |
|---|---|---|
| `indicator_slug` (required) | string | — |
| `indicator_name` (required) | string | — |
| `depends_on_slug` (required) | string | — |
| `depends_on_name` (required) | string | — |
| `depends_on_unit` (optional) | string or null | — |
| `weight` (optional) | number or null | — |

#### `EditRow`

| Field | Type | Description |
|---|---|---|
| `id` (required) | string | — |
| `ordinal` (required) | integer | — |
| `type_id` (required) | string | — |
| `attribute_key` (required) | string | — |
| `value_numeric` (required) | number | — |
| `geometry_wkt` (required) | string | — |
| `created_at` (required) | string (date-time) | — |

#### `EditsWritten`

| Field | Type | Description |
|---|---|---|
| `scenario_id` (required) | string | — |
| `feature_type` (required) | string | — |
| `edits` (optional) | list[WrittenEdit], default `[]` | — |

#### `ExecutorRow`

| Field | Type | Description |
|---|---|---|
| `executor_id` (required) | string | — |
| `role` (required) | string | — |
| `app_version` (optional) | string or null | — |
| `heartbeat_age_s` (required) | number | — |

#### `FeatureIndicatorValue`

One indicator's value on one feature, with its exact rank.

| Field | Type | Description |
|---|---|---|
| `slug` (required) | string | — |
| `name` (required) | string | — |
| `unit` (optional) | string or null | — |
| `cap_value` (optional) | number or null | — |
| `value` (required) | number | — |
| `count_below` (required) | integer | — |
| `count_equal` (required) | integer | — |
| `count` (required) | integer | — |

#### `FeaturePage`

Cursor-paginated page of features.

| Field | Type | Description |
|---|---|---|
| `features` (required) | list[FeatureRow] | — |
| `next_cursor` (optional) | string or null | — |

#### `FeatureRow`

Serving-layer feature attributes (no geometry in the JSON payload).

| Field | Type | Description |
|---|---|---|
| `id` (required) | string | — |
| `type_slug` (required) | string | — |
| `role` (required) | string | — |
| `name` (optional) | string or null | — |
| `attributes` (optional) | object or null | — |

#### `HTTPValidationError`

| Field | Type | Description |
|---|---|---|
| `detail` (optional) | list[ValidationError] | — |

#### `ImpactPreview`

What a submit *would* do, without doing it.

| Field | Type | Description |
|---|---|---|
| `indicators` (optional) | list[string], default `[]` | — |
| `count` (optional) | integer, default `0` | — |
| `noop` (optional) | boolean, default `true` | — |
| `reason` (optional) | string, default `""` | — |

#### `IndicatorDependencies`

Both directions around one indicator.

| Field | Type | Description |
|---|---|---|
| `slug` (required) | string | — |
| `upstream` (required) | list[DependencyEdge] | — |
| `downstream` (required) | list[DependencyEdge] | — |

#### `IndicatorLineage`

| Field | Type | Description |
|---|---|---|
| `slug` (required) | string | — |
| `asset_key` (required) | string | — |
| `state` (optional) | string or null | — |
| `materialized_at` (optional) | string (date-time) or null | — |
| `upstream` (required) | list[LineageNode] | — |
| `downstream` (required) | list[LineageNode] | — |

#### `IndicatorSummary`

One row of curated_geo.indicator_catalog, API-facing shape.

| Field | Type | Description |
|---|---|---|
| `slug` (required) | string | — |
| `name` (required) | string | — |
| `description` (optional) | string or null | — |
| `model_family` (optional) | string or null | — |
| `computation_kind` (required) | string | — |
| `value_type` (required) | string | — |
| `unit` (optional) | string or null | — |
| `valid_min` (optional) | number or null | — |
| `valid_max` (optional) | number or null | — |
| `cap_value` (optional) | number or null | — |
| `aggregation_method` (required) | string | — |
| `applies_to` (required) | list[string] | — |

#### `IndicatorValue`

One row of serving_geo.current_indicator_v, API-facing shape.

| Field | Type | Description |
|---|---|---|
| `feature_id` (required) | string | — |
| `feature_type` (required) | string | — |
| `indicator_slug` (required) | string | — |
| `scenario_id` (optional) | string or null | — |
| `value_numeric` (optional) | number or null | — |
| `value_text` (optional) | string or null | — |
| `value_json` (optional) | object or list[any] or null | — |
| `quality_flag` (required) | string | — |
| `confidence` (optional) | number or null | — |
| `computed_at` (required) | string (date-time) | — |
| `unit` (optional) | string or null | — |
| `indicator_name` (optional) | string or null | — |

#### `IndicatorValueStats`

Summary stats of an indicator's current numeric values.

| Field | Type | Description |
|---|---|---|
| `slug` (required) | string | — |
| `scenario_id` (optional) | string or null | — |
| `feature_type` (optional) | string or null | — |
| `aoi_id` (optional) | string or null | — |
| `count` (required) | integer | — |
| `min` (optional) | number or null | — |
| `p05` (optional) | number or null | — |
| `p50` (optional) | number or null | — |
| `p95` (optional) | number or null | — |
| `max` (optional) | number or null | — |
| `sum` (optional) | number or null | — |
| `count_at_cap` (optional) | integer or null | — |
| `histogram` (optional) | ValueHistogram or null | — |

#### `IndicatorWithinPolygonRequest`

| Field | Type | Description |
|---|---|---|
| `aoi_id` (required) | string (uuid) | — |
| `polygon` (required) | object | GeoJSON Polygon (EPSG:4326). |
| `indicator_slug` (required) | string | — |
| `feature_type` (optional) | string or null | — |

#### `LatestRun`

| Field | Type | Description |
|---|---|---|
| `id` (required) | string | — |
| `status` (required) | string | — |

#### `LineageNode`

One asset in the closure around an indicator.

| Field | Type | Description |
|---|---|---|
| `asset_key` (required) | string | — |
| `kind` (required) | string | — |
| `depth` (required) | integer | — |
| `state` (optional) | string or null | — |

#### `LivenessResponse`

| Field | Type | Description |
|---|---|---|
| `status` (required) | string | — |

#### `ProposalBody`

One proposal: an area and the three numbers a planner proposes over it.

| Field | Type | Description |
|---|---|---|
| `area` (required) | object | The drawn area as GeoJSON: a FeatureCollection, a Feature, or a bare geometry. Several polygons are unioned into one area, because a selection made with two strokes is one rule. |
| `levels` (required) | number | — |
| `unit_size` (required) | number | — |
| `people_per_household` (required) | number | — |
| `type_context` (optional) | string, default `"MEX"` | — |
| `type_slug` (optional) | string, default `"block"` | — |

#### `ReachableIndicator`

An indicator a scenario rule can change, and the number a table shows.

| Field | Type | Description |
|---|---|---|
| `slug` (required) | string | — |
| `name` (required) | string | — |
| `description` (optional) | string or null | — |
| `model_family` (optional) | string or null | — |
| `computation_kind` (required) | string | — |
| `value_type` (required) | string | — |
| `unit` (optional) | string or null | — |
| `valid_min` (optional) | number or null | — |
| `valid_max` (optional) | number or null | — |
| `cap_value` (optional) | number or null | — |
| `aggregation_method` (required) | string | — |
| `applies_to` (required) | list[string] | — |
| `statistic` (required) | one of `sum`, `median` | — |

#### `ReadinessResponse`

| Field | Type | Description |
|---|---|---|
| `status` (required) | string | — |
| `db` (required) | string | — |

#### `RecomputeResponse`

What the materialisation did, and the run that will recompute from it.

| Field | Type | Description |
|---|---|---|
| `scenario_id` (required) | string | — |
| `matched_per_edit` (optional) | dict[string, integer], default `{}` | — |
| `overrides_written` (optional) | integer, default `0` | — |
| `overrides_removed` (optional) | integer, default `0` | — |
| `results_superseded` (optional) | integer, default `0` | — |
| `run_id` (optional) | string or null | — |
| `run_status` (optional) | string, default `"queued"` | — |
| `deduplicated` (optional) | boolean, default `false` | — |

#### `RerunResponse`

The *new* run, plus what it was scoped from.

| Field | Type | Description |
|---|---|---|
| `run_id` (required) | string or null | — |
| `status` (required) | string | — |
| `deduplicated` (optional) | boolean, default `false` | — |
| `rerun_of` (required) | string | — |
| `indicators` (optional) | list[string], default `[]` | — |

#### `RunDetailResponse`

| Field | Type | Description |
|---|---|---|
| `run` (required) | RunRow | — |
| `units` (optional) | list[UnitDetail], default `[]` | — |
| `server_now` (required) | string (date-time) | — |
| `unit_slots` (optional) | integer, default `1` | — |

#### `RunListResponse`

| Field | Type | Description |
|---|---|---|
| `runs` (optional) | list[RunRow], default `[]` | — |
| `server_now` (required) | string (date-time) | — |
| `total` (optional) | integer, default `0` | — |
| `status_counts` (optional) | dict[string, integer], default `{}` | — |
| `executors` (optional) | list[ExecutorRow], default `[]` | — |

#### `RunRow`

| Field | Type | Description |
|---|---|---|
| `run_id` (required) | string | — |
| `status` (required) | string | — |
| `scenario_id` (optional) | string or null | — |
| `aoi_id` (optional) | string or null | — |
| `code_version` (required) | string | — |
| `submitted_at` (required) | string (date-time) | — |
| `completed_at` (optional) | string (date-time) or null | — |
| `units_total` (optional) | integer, default `0` | — |
| `units_terminal` (optional) | integer, default `0` | — |
| `units_succeeded` (optional) | integer, default `0` | — |
| `units_failed` (optional) | integer, default `0` | — |
| `units_running` (optional) | integer, default `0` | — |
| `units_last_terminal_at` (optional) | string (date-time) or null | — |

#### `RunStatus`

The ``orchestration_run.status`` CHECK vocabulary (r028), as a filter type.

Type: one of `queued`, `running`, `succeeded`, `partial`, `failed`, `cancelling`, `cancelled`.

#### `RunView`

| Field | Type | Description |
|---|---|---|
| `run_id` (required) | string | — |
| `status` (required) | string | — |
| `units` (optional) | list[UnitView], default `[]` | — |

#### `ScenarioDetail`

| Field | Type | Description |
|---|---|---|
| `id` (required) | string | — |
| `project_id` (required) | string | — |
| `aoi_id` (optional) | string or null | — |
| `name` (required) | string | — |
| `description` (optional) | string or null | — |
| `created_at` (required) | string (date-time) | — |
| `updated_at` (required) | string (date-time) | — |
| `materialized_at` (optional) | string (date-time) or null | — |
| `override_count` (optional) | integer, default `0` | — |
| `edits` (optional) | list[EditRow], default `[]` | — |

#### `ScenarioListRow`

A list row: the scenario plus what a summary of every scenario needs.

| Field | Type | Description |
|---|---|---|
| `id` (required) | string | — |
| `project_id` (required) | string | — |
| `aoi_id` (optional) | string or null | — |
| `name` (required) | string | — |
| `description` (optional) | string or null | — |
| `created_at` (required) | string (date-time) | — |
| `updated_at` (required) | string (date-time) | — |
| `materialized_at` (optional) | string (date-time) or null | — |
| `edit_count` (required) | integer | — |
| `latest_run` (optional) | LatestRun or null | — |

#### `SubmitRunBody`

| Field | Type | Description |
|---|---|---|
| `changed_attributes` (optional) | list[string] or null | — |
| `edited_features` (optional) | list[string] or null | — |
| `scenario_id` (optional) | string or null | — |
| `aoi_id` (optional) | string or null | — |
| `indicators` (optional) | list[string] or null | — |
| `changed_datasets` (optional) | list[string] or null | — |
| `wait` (optional) | boolean, default `false` | — |

#### `SubmitRunResponse`

| Field | Type | Description |
|---|---|---|
| `run_id` (required) | string or null | — |
| `status` (required) | string | — |
| `deduplicated` (optional) | boolean, default `false` | — |

#### `TileServerResponse`

| Field | Type | Description |
|---|---|---|
| `status` (required) | string | — |
| `martin` (required) | string | — |
| `api_database` (optional) | string or null | — |
| `martin_reachable` (required) | boolean | — |
| `martin_url` (required) | string | — |
| `notes` (optional) | list[string], default `[]` | — |

#### `TileSource`

| Field | Type | Description |
|---|---|---|
| `layer_id` (required) | string | — |
| `logical_name` (optional) | string or null | — |
| `description` (optional) | string or null | — |
| `source_mode` (required) | string | — |
| `implemented` (required) | boolean | — |
| `available` (required) | boolean | — |
| `source_config` (required) | object | — |
| `latest_snapshot` (optional) | object or null | — |

#### `UnitDetail`

| Field | Type | Description |
|---|---|---|
| `indicator` (required) | string | — |
| `status` (required) | string | — |
| `detail` (optional) | string or null | — |
| `queued_at` (required) | string (date-time) | — |
| `started_at` (optional) | string (date-time) or null | — |
| `completed_at` (optional) | string (date-time) or null | — |
| `depends_on` (optional) | list[string], default `[]` | — |

#### `UnitView`

| Field | Type | Description |
|---|---|---|
| `indicator` (required) | string | — |
| `status` (required) | string | — |
| `detail` (optional) | string or null | — |

#### `ValidationError`

| Field | Type | Description |
|---|---|---|
| `loc` (required) | list[string or integer] | — |
| `msg` (required) | string | — |
| `type` (required) | string | — |
| `input` (optional) | any | — |
| `ctx` (optional) | object | — |

#### `ValueHistogram`

Equal-width bin counts over an indicator's observed value range.

| Field | Type | Description |
|---|---|---|
| `bins` (required) | integer | — |
| `lo` (required) | number | — |
| `hi` (required) | number | — |
| `width` (required) | number | — |
| `counts` (required) | list[integer] | — |

#### `WithinPolygonRequest`

Inputs for the within-polygon count.

| Field | Type | Description |
|---|---|---|
| `aoi_id` (required) | string (uuid) | AOI scope — bounds the search to one city / area. |
| `polygon` (required) | object | GeoJSON Polygon (EPSG:4326). The polygon's outer ring defines the selection; holes are honoured by ST_Intersects. |
| `type_slugs` (required) | list[string] | Feature types to count separately. Each slug appears in the response with its intersecting count; missing slugs resolve to 0 client-side. |

#### `WithinPolygonResponse`

| Field | Type | Description |
|---|---|---|
| `results` (required) | list[WithinPolygonResultRow] | — |

#### `WithinPolygonResultRow`

| Field | Type | Description |
|---|---|---|
| `type_slug` (required) | string | — |
| `count` (required) | integer | — |

#### `WrittenEdit`

| Field | Type | Description |
|---|---|---|
| `id` (required) | string | — |
| `attribute_key` (required) | string | — |
| `value_numeric` (required) | number | — |

### Mounted raster endpoints (19)

Provided by titiler. Access is restricted by `apps/api/cog_security.py` to `file://` URLs beneath the configured data root, with symlink escapes resolved and rejected; titiler's own default would hand any `url` parameter to GDAL.

| Method | Path | Summary | Parameters | Request body |
|---|---|---|---|---|
| `GET` | `/cog/bbox/{minx},{miny},{maxx},{maxy}.{format}` | Bbox Image | `minx`, `miny`, `maxx`, `maxy`, `format`, `url`, `bidx`, `expression`, `nodata`, `unscale`, `resampling`, `reproject`, `max_size`, `height`, `width`, `dst_crs`, `coord_crs`, `algorithm`, `algorithm_params`, `colormap_name`, `colormap`, `rescale`, `color_formula`, `return_mask` | no |
| `GET` | `/cog/bbox/{minx},{miny},{maxx},{maxy}/{width}x{height}.{format}` | Bbox Image | `minx`, `miny`, `maxx`, `maxy`, `format`, `height`, `width`, `url`, `bidx`, `expression`, `nodata`, `unscale`, `resampling`, `reproject`, `max_size`, `dst_crs`, `coord_crs`, `algorithm`, `algorithm_params`, `colormap_name`, `colormap`, `rescale`, `color_formula`, `return_mask` | no |
| `POST` | `/cog/feature` | Feature Image | `format`, `url`, `bidx`, `expression`, `nodata`, `unscale`, `resampling`, `reproject`, `max_size`, `height`, `width`, `coord_crs`, `dst_crs`, `algorithm`, `algorithm_params`, `colormap_name`, `colormap`, `rescale`, `color_formula`, `return_mask` | yes |
| `POST` | `/cog/feature.{format}` | Feature Image | `format`, `url`, `bidx`, `expression`, `nodata`, `unscale`, `resampling`, `reproject`, `max_size`, `height`, `width`, `coord_crs`, `dst_crs`, `algorithm`, `algorithm_params`, `colormap_name`, `colormap`, `rescale`, `color_formula`, `return_mask` | yes |
| `POST` | `/cog/feature/{width}x{height}.{format}` | Feature Image | `format`, `height`, `width`, `url`, `bidx`, `expression`, `nodata`, `unscale`, `resampling`, `reproject`, `max_size`, `coord_crs`, `dst_crs`, `algorithm`, `algorithm_params`, `colormap_name`, `colormap`, `rescale`, `color_formula`, `return_mask` | yes |
| `GET` | `/cog/info` | Info | `url` | no |
| `GET` | `/cog/info.geojson` | Info Geojson | `url`, `crs` | no |
| `GET` | `/cog/point/{lon},{lat}` | Point | `lon`, `lat`, `url`, `coord_crs`, `bidx`, `expression`, `nodata`, `unscale`, `resampling`, `reproject` | no |
| `GET` | `/cog/preview` | Preview | `format`, `url`, `bidx`, `expression`, `nodata`, `unscale`, `resampling`, `reproject`, `max_size`, `height`, `width`, `dst_crs`, `algorithm`, `algorithm_params`, `colormap_name`, `colormap`, `rescale`, `color_formula`, `return_mask` | no |
| `GET` | `/cog/preview.{format}` | Preview | `format`, `url`, `bidx`, `expression`, `nodata`, `unscale`, `resampling`, `reproject`, `max_size`, `height`, `width`, `dst_crs`, `algorithm`, `algorithm_params`, `colormap_name`, `colormap`, `rescale`, `color_formula`, `return_mask` | no |
| `GET` | `/cog/preview/{width}x{height}.{format}` | Preview | `format`, `height`, `width`, `url`, `bidx`, `expression`, `nodata`, `unscale`, `resampling`, `reproject`, `max_size`, `dst_crs`, `algorithm`, `algorithm_params`, `colormap_name`, `colormap`, `rescale`, `color_formula`, `return_mask` | no |
| `GET` | `/cog/statistics` | Statistics | `url`, `bidx`, `expression`, `nodata`, `unscale`, `resampling`, `reproject`, `max_size`, `height`, `width`, `algorithm`, `algorithm_params`, `categorical`, `c`, `p`, `histogram_bins`, `histogram_range` | no |
| `POST` | `/cog/statistics` | Geojson Statistics | `url`, `coord_crs`, `dst_crs`, `bidx`, `expression`, `nodata`, `unscale`, `resampling`, `reproject`, `max_size`, `height`, `width`, `algorithm`, `algorithm_params`, `cover_scale`, `categorical`, `c`, `p`, `histogram_bins`, `histogram_range` | yes |
| `GET` | `/cog/tiles` | Retrieve a list of available raster tilesets for the specified dataset. | `f`, `url`, `crs` | no |
| `GET` | `/cog/tiles/{tileMatrixSetId}` | Retrieve the raster tileset metadata for the specified dataset and tiling scheme (tile matrix set). | `tileMatrixSetId`, `minzoom`, `maxzoom`, `f`, `url` | no |
| `GET` | `/cog/tiles/{tileMatrixSetId}/{z}/{x}/{y}` | Tile | `z`, `x`, `y`, `tileMatrixSetId`, `format`, `tilesize`, `url`, `buffer`, `padding`, `bidx`, `expression`, `nodata`, `unscale`, `resampling`, `reproject`, `algorithm`, `algorithm_params`, `colormap_name`, `colormap`, `rescale`, `color_formula`, `return_mask` | no |
| `GET` | `/cog/tiles/{tileMatrixSetId}/{z}/{x}/{y}.{format}` | Tile | `z`, `x`, `y`, `tileMatrixSetId`, `format`, `tilesize`, `url`, `buffer`, `padding`, `bidx`, `expression`, `nodata`, `unscale`, `resampling`, `reproject`, `algorithm`, `algorithm_params`, `colormap_name`, `colormap`, `rescale`, `color_formula`, `return_mask` | no |
| `GET` | `/cog/{tileMatrixSetId}/map.html` | Map Viewer | `tileMatrixSetId`, `tile_format`, `tilesize`, `minzoom`, `maxzoom`, `url`, `buffer`, `padding`, `bidx`, `expression`, `nodata`, `unscale`, `resampling`, `reproject`, `algorithm`, `algorithm_params`, `colormap_name`, `colormap`, `rescale`, `color_formula`, `return_mask` | no |
| `GET` | `/cog/{tileMatrixSetId}/tilejson.json` | Tilejson | `tileMatrixSetId`, `tilesize`, `tile_format`, `minzoom`, `maxzoom`, `url`, `buffer`, `padding`, `bidx`, `expression`, `nodata`, `unscale`, `resampling`, `reproject`, `algorithm`, `algorithm_params`, `colormap_name`, `colormap`, `rescale`, `color_formula`, `return_mask` | no |

