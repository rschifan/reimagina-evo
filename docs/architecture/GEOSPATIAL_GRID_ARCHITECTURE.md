# Geospatial Model: Feature Grains, Rasters and Moving Values Between Them

How Reimagina represents spatial data today: which spatial grains exist, how vector features and rasters enter the database, how values move from one grain to another, how the results are served, and what is not built.

> **Status.** Two spatial shapes are built: **features** (named entities with a geometry, one row each in `curated_geo.feature`) and **rasters** (Cloud Optimized GeoTIFFs on disk, summarised onto features). There are no grid cells: no H3, no fishnet, no table of values per cell. Every value queried in SQL belongs to a feature — measured values in `curated_geo.observation`, computed indicators in `curated_geo.indicator_result`. §14 says how a grid would fit if one is ever needed.

---

## Table of Contents

1. [Scope](#1-scope)
2. [The Model at a Glance](#2-the-model-at-a-glance)
3. [Principles](#3-principles)
4. [Grains: Feature Types and Features](#4-grains-feature-types-and-features)
5. [How Data Gets In](#5-how-data-gets-in)
6. [Moving Values Between Grains](#6-moving-values-between-grains)
7. [Serving](#7-serving)
8. [Scenarios and Grains](#8-scenarios-and-grains)
9. [Provenance and Reproducibility](#9-provenance-and-reproducibility)
10. [Schema Reference](#10-schema-reference)
11. [Query Patterns](#11-query-patterns)
12. [Performance](#12-performance)
13. [Anti-Patterns](#13-anti-patterns)
14. [Not Built: Grid Cells (H3, Fishnet)](#14-not-built-grid-cells-h3-fishnet)
15. [External References](#15-external-references)

---

## 1. Scope

This document covers the spatial side of the data model: grains, geometry, CRS, the bridge tables that relate grains (`aoi_feature`, `feature_overlap`), raster ingest and zonal statistics, cross-grain rollups and the AOI summary.

It does not repeat what other documents own:

- column-by-column definitions of `data_source_catalog`, `ingestion_run`, `feature`, `observation`, `file_asset`, `area_of_interest` — `DATA_LAYER.md`;
- indicators and `indicator_result` — `INDICATORS_LAYER.md`;
- runs, units and `job_run` — `JOB_LAYER.md`;
- scenario authoring, overrides and the scenario overlay — `SCENARIOS_LAYER.md`;
- tile functions, Martin and PMTiles — `TILE_SERVING.md`.

Words follow `TERMINOLOGY.md`. "Grain" here means a feature type used as a unit of analysis or aggregation (block, AGEB, municipality, ...).

---

## 2. The Model at a Glance

```
 DATA SOURCES
 INEGI marco + censo │ CDMX layers │ Overture buildings, places │ OSM network │ WorldPop
        │                                                              │
        ▼ vector loaders                                               ▼ raster ingest
 ┌──────────────────────────────┐                     ┌──────────────────────────────────┐
 │ FEATURES  (PostGIS)          │   zonal statistics  │ RASTERS  (COG files on disk)     │
 │ feature        one row per   │ ◀────────────────── │ data/silver/<source_id>/*.cog.tif│
 │                entity        │   one observation   │ file_asset   catalog row         │
 │ observation    measured or   │   per (feature,     │ raster_layer serving row         │
 │                derived value │   statistic)        └───────────────┬──────────────────┘
 │ indicator_result computed    │                                     │
 └──────┬───────────────────────┘                                     │
        │ relate grains (precomputed)                                 │
        │   aoi_feature      feature ↔ AOI                            │
        │   feature_overlap  feature ↔ feature, per declared type pair│
        │ move values                                                 │
        │   observation rollup (aggregation_recipe)                   │
        │   aoi_indicator_summary                                     │
        ▼                                                             ▼
 SERVE  serving_geo views + MVT tile functions        titiler at /cog (pixels)
```

| Shape | Built | Where values live | How it relates to other grains |
|---|---|---|---|
| Feature | Yes | `feature` (geometry, identity), `observation`, `indicator_result` | `aoi_feature`, `feature_overlap`, `feature.parent_id` |
| Raster | Yes | Pixels in a COG under the data root; per-feature summaries in `observation` | Zonal statistics onto one feature type |
| Grid cell | No | — | Would be a feature type (§14) |

The data root is `REIMAGINA_DATA_DIR`, default `<repo>/data` (`packages/py/reimagina/paths.py`).

---

## 3. Principles

**P1 — Everything queried in SQL is per feature.** A raster's pixels stay in the COG; the database holds a catalog row and per-feature summaries. A raster whose values are only needed per block becomes block observations (`raster zonal-stats`), not a pixel or cell table.

**P2 — Spatial relationships are precomputed, then read by key.** AOI membership is stored in `curated_geo.aoi_feature` (`aoi compute-features`), feature-to-feature intersections in `curated_geo.feature_overlap` (`overlap compute-all`), and each feature's interior point in the generated column `feature.geometry_centroid` (r027). Reads scoped to an AOI — indicator values and statistics, downloads, the overlay tiles, scenario materialisation, scenario comparison — filter with `EXISTS` on `aoi_feature`.

Run-time geometry tests remain where the geometry exists only at request time, and in the batch jobs that select their inputs:

| Where | Predicate |
|---|---|
| `POST /metrics/within-polygon`, `POST /metrics/indicator-within-polygon` | `ST_Covers(polygon, geometry_centroid)` (`POLYGON_COVERS_CENTROID`, `packages/py/reimagina/storage/_utils.py`) |
| Scenario materialisation | `ST_Covers(scenario_edit.geometry, feature.geometry_centroid)`, plus `EXISTS` on `aoi_feature` |
| `raster zonal-stats --aoi-id` (feature selection) | `ST_Intersects(feature.geometry, aoi.geometry)` (`iter_geometries_by_type`) |
| `overlap compute-all --aoi-id` | `ST_Intersects(source.geometry, aoi.geometry)` |
| Indicator computes run with an AOI | `ST_Intersects(feature.geometry, aoi.geometry)` (`geodesic_metrics_by_type`, `centroid_in_parent_counts`, `_AOI_JOIN` in `packages/py/reimagina/indicators/accessibility.py`) |

The batch selections by geometry are a superset of AOI membership (see "Possible inconsistencies" at the end of §6.1).

**P3 — One schema for every grain.** Every grain is a row in `curated_geo.feature_type_catalog`; every entity is a row in `curated_geo.feature`; grains relate through the same two bridge tables. Adding a grain adds rows, not tables. What is grain-specific is the code that reads a grain: all twelve shipped indicators declare `applies_to = ('block',)` (`SHIPPED_INDICATORS` in `packages/py/reimagina/indicators/catalog.py`), and the raster workers resolve their target type in the `MEX` context (§6.3).

**P4 — Rasters keep their native CRS.** Pixel values are never reprojected. Feature geometries are reprojected from EPSG:4326 to the raster's CRS at zonal-statistics time.

**P5 — Provenance is structural.** Every `feature`, `observation` and `file_asset` row references the `ingestion_run` that wrote it; every `indicator_result` references the `job_run` that wrote it (§9).

---

## 4. Grains: Feature Types and Features

### 4.1 Feature types

The registry of grains is `curated_geo.feature_type_catalog` (r003):

```sql
CREATE TABLE curated_geo.feature_type_catalog (
    id              uuid      PRIMARY KEY DEFAULT gen_random_uuid(),
    context         text      NOT NULL,          -- seeded: 'MEX', 'global'
    type_slug       text      NOT NULL,
    label           text      NOT NULL,
    geometry_family text      NOT NULL
                              CHECK (geometry_family IN ('point','line','polygon','geometry')),
    role            text      NOT NULL
                              CHECK (role IN
                                ('analysis_unit','context','infrastructure','poi','detection')),
    admin_level     smallint  CHECK (admin_level IS NULL OR admin_level > 0),
    description     text,
    UNIQUE (context, type_slug)
);
```

Seeded types are `FEATURE_TYPE_ROWS` in `packages/py/reimagina/data/feature.py`, the same rows r003 inserts; `feat seed-types` re-seeds them idempotently.

| context | type_slug | geometry_family | role | admin_level | Shipped loader |
|---|---|---|---|---|---|
| MEX | state | polygon | context | 1 | INEGI marco |
| MEX | municipality | polygon | context | 2 | INEGI marco, CDMX bootstrap |
| MEX | neighborhood | polygon | context | 3 | CDMX bootstrap |
| MEX | ageb | polygon | analysis_unit | 4 | INEGI marco, CDMX bootstrap |
| MEX | block | polygon | analysis_unit | 5 | INEGI marco, CDMX bootstrap |
| MEX | lot | polygon | analysis_unit | 6 | none |
| global | building | polygon | context | — | Overture buildings |
| global | tree | point | detection | — | none |
| global | bus_stop | point | poi | — | none |
| global | station | point | poi | — | none |
| global | establishment | point | poi | — | amenity points, Overture places |
| global | road | line | infrastructure | — | published from the routing graph |
| global | sidewalk | line | infrastructure | — | none |
| global | bike_lane | line | infrastructure | — | none |
| global | crossing | point | infrastructure | — | none |

`admin_level` orders the administrative hierarchy. A type with `admin_level` NULL is non-hierarchical: the feature trigger refuses a `parent_id` on it.

### 4.2 Features

`curated_geo.feature` (r003; `geometry_centroid` added by r027) holds one row per entity of any grain. The full column list is in `DATA_LAYER.md`; the spatial rules are:

- **Identity.** `id` is a UUID. A feature's identity within its source is `UNIQUE (source_id, dataset_version, source_record_ref)`, and `load_vector_source` inserts with `ON CONFLICT (source_id, dataset_version, source_record_ref) DO NOTHING`, so reloading the same version adds nothing. Re-ingesting a dataset can mint new ids, which is why identifier-based AOI membership matches on `attributes`, not on feature ids (r040).
- **Geometry.** `geometry geometry(Geometry, 4326) NOT NULL`. The stored generated column `geometry_centroid geometry(Point, 4326)` is `ST_PointOnSurface(geometry)`, a point guaranteed inside the polygon.
- **Trigger `feature_before_write`.** Sets `type_slug` and `role` from `type_id`; rejects geometry outside the type's `geometry_family` (`geometry` accepts anything); rejects a row whose `source_id` differs from its `ingestion_run`'s; enforces the `parent_id` hierarchy (no parent on a non-hierarchical type, parent `admin_level` strictly lower, no cycles).
- **Deletion.** There is no soft delete. Two code paths delete features: the Overture buildings loader removes the rows of its own run that fall outside the AOI (`packages/py/reimagina/pipeline/overture.py:651`), and the road publisher replaces its source's `road` features on every load (`packages/py/reimagina/data/roads.py:130`). Deleting a feature cascades to its `observation`, `aoi_feature`, `feature_overlap`, `indicator_result` and `scenario_override` rows.

### 4.3 Spatial reference

There is no grid or CRS registry. The spatial reference is fixed by column types and code:

| Data | CRS / measure | Fixed by |
|---|---|---|
| Vector geometry: `feature.geometry`, `area_of_interest.geometry`, `scenario_edit.geometry` | EPSG:4326 | Column type `geometry(Geometry, 4326)` (r003, r004, r048) |
| Vector input | Reprojected to EPSG:4326, invalid geometry repaired with `make_valid`, null and empty dropped | `clean_vector` in `packages/py/reimagina/pipeline/silver.py` |
| Areas and lengths in `aoi_feature`, `feature_overlap` | Geodesic, through `::geography` (m², m) | `PostgresAOIRepository.compute_overlaps`, `PostgresOverlapRepository.compute_feature_overlaps` |
| Raster pixels | The input's native CRS, recorded in `file_asset.crs` | `ingest_raster_source` |
| Zonal statistics | Feature geometry reprojected EPSG:4326 → raster CRS | `transform_geom` at `packages/py/reimagina/jobs/raster_zonal_stats.py:386` |
| Tiles | EPSG:3857 (`ST_Transform(..., 3857)` in the tile functions); titiler `WebMercatorQuad` | `TILE_SERVING.md` |

---

## 5. How Data Gets In

### 5.1 Vector features

Every vector loader follows the same route: register the source, land the raw file in bronze, clean it to silver (`clean_vector`), then `load_vector_source` (`packages/py/reimagina/pipeline/loader.py`) opens an `ingestion_run` (`transformation_step = 'silver_to_curated'`), inserts the features and completes the run. Loaders that use it: INEGI marco (`inegi_marco.py`), the CDMX bootstrap (`cdmx.py`), Overture buildings (`overture.py`), Overture places (`overture_places.py`) and amenity points (`points.py`), all under `packages/py/reimagina/pipeline/`.

Two writers do not go through `load_vector_source`: the road publisher projects `curated_geo.road_edge` (the pgRouting graph loaded by `osm_network.py`) into `global:road` features (`packages/py/reimagina/data/roads.py`), and Overture divisions only read boundaries to build an AOI (`overture aoi-from-division`).

### 5.2 Measured values

Values measured about existing features are `observation` rows: one per (feature, `attribute_key`), exactly one of `value_numeric`, `value_text`, `value_json`, with SCD-2 history (`is_current`, `valid_from`, `valid_to`; partial unique index `obs_one_current`). Writers retire the current row and insert a new one; nothing updates a value in place. Producers: the INEGI census loader (`inegi_censo.py`), the block-storeys derivation (`block_storeys.py`, §6.5), raster zonal statistics (§6.3) and the observation rollup (§6.4).

Every `ingestion_run` that completes as `succeeded` refreshes `serving_geo.feature_current_attributes_mv`, the snapshot of current observations that `serving_geo.feature_v` and the base tile function read.

### 5.3 Rasters

`raster ingest` (`ingest_raster_source`, `packages/py/reimagina/pipeline/raster.py:62`):

1. Opens the input and rejects anything but a single band with `UnsupportedRasterError` (`raster.py:116`), before any database write.
2. Registers the source (`storage_type = 'raster'`, `integration_role = 'enrichment'`; `storage_uri` is `--origin-uri`, or the input file).
3. Starts and commits an `ingestion_run` with `transformation_step = 'raster_landing'`.
4. Copies the input unchanged to `data/bronze/<source_id>/<filename>`.
5. Re-encodes it as a COG at `data/silver/<source_id>/<stem>.cog.tif`, in the input's native CRS (`raster.py:173`).
6. Writes `file_asset` (`format = 'COG'`, `storage_uri = 'file://data/silver/...'`, `crs`, `resolution_m`, `band_description`) and `raster_layer` (`layer_slug`, `aggregation_kind`, `minzoom`/`maxzoom` default 5/14, `published` unless `--unpublished`).

`resolution_m` is set only when the CRS is projected in metres and the pixels are square and north-up (`_resolution_in_metres`, `raster.py:278`); otherwise it is NULL. `asset_extent_geometry` is never populated. `file_asset.source_id` and `raster_layer.source_id` are both UNIQUE: one COG per source. Files live on the local filesystem, not in object storage, and the catalog is not STAC (`TODO.md` T3-11).

**WorldPop** is the one raster with a fetch command, `raster fetch-worldpop` (`packages/py/reimagina/pipeline/worldpop.py`):

| Parameter | Value |
|---|---|
| Product | `wpgp` (`DEFAULT_DATASET`; `--dataset` overrides) |
| Catalogue | `https://www.worldpop.org/rest/data` (`WORLDPOP_REST_BASE`), queried as `/pop/<dataset>?iso3=<ISO3>` |
| Download | One GeoTIFF per (dataset, country, year), cached as `<dataset>_<ISO3>_<year>.tif` under `--cache-dir` (default `data/cache/worldpop`) |
| AOI clip | Optional `--aoi-id` (`clip_to_aoi_polygon`) |
| Grid | EPSG:4326, 3 arc-seconds (about 90 m at the equator) — so `resolution_m` is NULL |
| Ingest | `raster ingest --raw-path <tif> ... --aggregation-kind extensive` (a count; the default kind is `intensive`) |

---

## 6. Moving Values Between Grains

Six mechanisms move or relate values across grains. All are explicit commands or functions; nothing runs automatically when features or AOIs are loaded.

| From → to | Mechanism | Writes | Weight |
|---|---|---|---|
| Feature → AOI (membership) | `aoi compute-features` | `aoi_feature` | — |
| Feature type → feature type (relation) | `overlap declare-pair` + `overlap compute-all` | `feature_overlap` | — |
| Raster → feature type | `raster zonal-stats`, or a raster recipe | `observation` | exactextract pixel coverage fraction |
| Finer → coarser feature type | Observation recipe (`raster derivation run`) | `observation` | `feature_overlap.overlap_measure` (for `weighted_mean`) |
| Child points/centroids → parent polygon | Inside indicator computes and `block_storeys` | `indicator_result`, `observation` | Count / median |
| Block indicator → AOI | `indicators aoi-summary` (`summarise_aoi`) | `aoi_indicator_summary` | `aoi_feature.overlap_measure` |

### 6.1 AOI membership — `aoi_feature`

`aoi compute-features [--aoi-id <aoi>]` (`PostgresAOIRepository.compute_overlaps`, `packages/py/reimagina/storage/postgres/repositories.py:1045`) deletes the AOI's rows and re-inserts them in three passes:

- **Polygons by area, lines by length** (geodesic). The AOI polygon is first cut with `ST_Subdivide` into chunks of at most `AOI_SUBDIVIDE_VERTICES` = 64 vertices (`repositories.py:52`), so the GiST index prunes candidates; per-chunk measures are summed. A feature is a member when `overlap_measure / feature_measure >= MEMBERSHIP_MIN_OVERLAP_RATIO` = 1e-9 (`repositories.py:44`), which drops shared-border slivers but keeps any feature that genuinely straddles the boundary.
- **Points by presence**: `ST_Within(feature.geometry, aoi.geometry)`, stored with `overlap_measure = feature_measure = overlap_ratio = 1`.
- **Identifier rule** (r040), applied in all three passes: when the AOI has a `membership_key` such as `{"CVE_ENT": "09", "CVE_MUN": "012"}`, a feature that carries every key must match the values exactly (`attributes @> membership_key`) and still pass the geometric test; a feature missing any key (Overture buildings, establishments) is decided by geometry alone. `cdmx aoi-from-state` and `cdmx aoi-from-alcaldia` set a key; `overture aoi-from-division` does not.

`aoi_feature` rows carry `overlap_basis` (`area` | `length` | `presence`), `overlap_measure` (m², m or 1), `feature_measure` and `overlap_ratio` (share of the feature inside the AOI). A trigger checks that the basis matches the feature's geometry type.

Creating an AOI does not populate `aoi_feature`, and loading features does not update it. Run `aoi compute-features` after either.

**Possible inconsistencies.** The batch jobs in the P2 table select AOI features by `ST_Intersects` with the AOI polygon, so they also process features `aoi_feature` excludes: identifier-mismatched neighbours and touching-only slivers. Those extra rows are computed but never read through an AOI-scoped path.

### 6.2 Feature-to-feature overlap — `feature_overlap`

`overlap declare-pair --source-context --source-slug --target-context --target-slug` registers a pair in `feature_overlap_type_pair`. `overlap compute-all [--aoi-id <aoi>]` (`compute_all_pairs` → `PostgresOverlapRepository.compute_feature_overlaps`, `repositories.py:1454`) computes every declared pair:

- the basis comes from the **source** type's `geometry_family`: `polygon` → area, `line` → length, `point` or `geometry` → presence (`ST_Within(source, target)`);
- area and length are geodesic; a row is written for any intersection with positive measure — there is no minimum ratio;
- `overlap_measure` is the intersection (m² or m), `feature_measure` the source feature's area or length, `overlap_ratio = overlap_measure / feature_measure`, the share of the **source** inside the target;
- rows are upserted on `(source_feature_id, target_feature_id)`. A recompute does not delete pairs that no longer intersect; only deleting a feature removes its rows.

The one reader of `feature_overlap` is the observation rollup (§6.4). Neither indicators nor tiles read it, and `feature.parent_id` is not used for aggregation.

### 6.3 Raster → feature: zonal statistics

`raster zonal-stats --source-id <uuid> --feature-type <slug> --attribute-key-prefix <prefix> [--aoi-id] [--statistics] [--coverage-threshold] [--force] [--direct-dsn]` runs `run_raster_zonal_stats` (`packages/py/reimagina/jobs/raster_zonal_stats.py`). A raster recipe (§6.4) runs the same function.

1. Resolves the feature type as `get_type_id("MEX", <slug>)` (`raster_zonal_stats.py:208`), so only `MEX` types can be targeted.
2. Commits a new `ingestion_run` on the raster's source (`orchestrator = 'worker'`, `transformation_step = 'raster_zonal_stats'`).
3. Reads every feature of the type (optionally intersecting the AOI), reprojects each geometry to the raster CRS, and runs exactextract once over all of them. exactextract weights each pixel by the fraction of it inside the polygon, so blocks smaller than a pixel get a correct share and the per-block sums of an extensive raster add up to the raster total.
4. Skips a feature whose fractional valid-pixel `count` is 0 or below `coverage_threshold` (default 0.0, `raster_zonal_stats.py:419`).
5. On a dedicated connection (`--direct-dsn` or `DATABASE_URL_DIRECT`, required), takes `pg_try_advisory_xact_lock` on a hash of the prefix — `ConcurrentRasterRun` if it is held — retires the prefix's current rows for that feature type (and AOI members, through `aoi_feature`, when `--aoi-id` is set), and inserts one observation per (feature, statistic) as `<prefix>_<stat>`.

Default statistics (`_DEFAULT_STATS_BY_KIND`, `raster_zonal_stats.py:120`): `mean, median, p05, p95, std` for an `intensive` raster, `sum, count` for an `extensive` one. Each row carries `lineage_ref = {"kind": "raster_zonal_stats", "asset_id", "stat_name", "fractional_pixel_count"}` and the raster's `source_id`.

The CLI adds two checks the worker does not make: it refuses a prefix that another source already owns at the same feature type unless `--force` (`has_active_prefix`), and it warns when the median feature's equivalent-circle diameter is below three times `resolution_m` for an extensive raster.

### 6.4 Finer → coarser grain: aggregation recipes

`curated_geo.aggregation_recipe` (r020) declares a derivation per target grain; `raster derivation add | list | enable | disable | remove | run` manages them, and `raster derivation run --recipe-id <uuid>` or `--raster-layer-slug <slug>` executes them (`run_recipe` / `run_all_for_raster`, `packages/py/reimagina/jobs/derivations.py`). Recipes are never run automatically.

- **Raster recipe** (`input_kind = 'raster'`): zonal statistics of `input_raster_layer_id` onto `target_feature_type_slug` (§6.3). Statistics come from `params.stats`, or else from the method (`mean`, `sum`, `min`, `max` map to the same statistic; `_METHOD_TO_RASTERSTAT`, `derivations.py:194`).
- **Observation recipe** (`input_kind = 'observation'`): `run_observation_rollup` (`packages/py/reimagina/jobs/observation_rollup.py`) reads current observations of `input_feature_type_slug` whose key is `input_attribute_key_prefix` or starts with `<prefix>_`, joins them to the target grain through `feature_overlap` (`rollup_observation_child_rows`, `repositories.py:1588`; §11 Q2), groups by (target feature, source key) and reduces each group with `reimagina.jobs.aggregation.aggregate`. The suffix is kept: `pop_2020_sum` at block grain becomes `<output_prefix>_sum` at the target grain.

Aggregation methods: `weighted_mean` weights by `feature_overlap.overlap_measure` (m² or m); `mean`, `sum`, `min`, `max` ignore the weight. A `sum` therefore adds a source feature's full value to every target it touches — correct for nesting grains such as block → AGEB, double-counting for grains that do not nest. `weight_basis` is required for `weighted_mean` but only checked for presence; the weight is always `overlap_measure`.

Rollup observations are written under a synthetic source per recipe (`name = 'observation_rollup::<recipe_id>'`, `provider = 'reimagina-rollup'`), with `transformation_step = 'observation_rollup'`, `lineage_ref = {"source_attribute_key", "input_feature_type", "target_feature_type", "method", "children"}`, and the same prefix lock and grain-scoped retire as zonal statistics. With `aoi_id`, source features are limited to AOI members (`aoi_feature`). `aggregation_recipe_prefix_uk` makes `(target_feature_type_slug, attribute_key_prefix)` unique, so one prefix has one producer per grain.

### 6.5 Point-in-polygon inside computes

Two producers relate a child grain to a parent without `feature_overlap`:

- `building_density` counts buildings per block with `ST_Within(ST_Centroid(child.geometry), parent.geometry)` (`centroid_in_parent_counts`, `repositories.py:466`).
- `block_storeys` writes a block's `floors` observation as the median `num_floors` of the Overture buildings whose `geometry_centroid` the block contains (`packages/py/reimagina/pipeline/block_storeys.py`).

Both assign a straddling child to one parent only.

### 6.6 Block indicator → AOI: `aoi_indicator_summary`

`indicators aoi-summary --aoi-id --slug [--scenario-id]` runs `summarise_aoi` (`packages/py/reimagina/jobs/aoi_summary.py`). It reads every `aoi_feature` row of the AOI, left-joined to the indicator's current values (through `scenario_overlay` for a scenario; `fetch_aoi_children`, `repositories.py:2081`), weights each by `aoi_feature.overlap_measure`, applies the shared missing-data policy (`aggregate_children`: missing if more than half of the weight is missing, `prorated` note if some is) and the indicator's `aggregation_method`, and upserts one row per (AOI, scenario, indicator). `GET /aois/{aoi_id}/summaries` reads them.

---

## 7. Serving

| What | Served by | Grain handling |
|---|---|---|
| Features with current attributes | `serving_geo.feature_v`, `serving_geo.feature_aoi_v` (one row per AOI × feature); `GET /features` | Any type; `feature_v.attributes = feature.attributes ‖ feature_current_attributes_mv` |
| Base map features | `serving_geo.get_tile` | Role-gated by zoom; `aoi_id` filters through `aoi_feature` (r039) |
| Indicator choropleth | `serving_geo.indicator_overlay_tile` | `feature_type` and `aoi_id` parameters; empty below z8 (r025) |
| Observation choropleth (recipes and raw observations) | `serving_geo.aggregation_overlay_tile` | `attribute_key` + `feature_type_slug`; `aoi_id` through `aoi_feature`; empty below z8 (r025) |
| Indicator values and downloads | `GET /indicators/{slug}/values`, `/stats`, `/values.csv`, `/values.geojson`, `/values.parquet` | AOI scope by `EXISTS` on `aoi_feature` |
| Raster pixels | titiler mounted at `/cog` (`apps/api/app.py:203`) | `/cog/tiles/WebMercatorQuad/{z}/{x}/{y}.png?url=<file URL>` |

The vector tile functions are served by Martin and by the API's `/tiles/{function}/{z}/{x}/{y}` route; `GET /tile-sources` lists them with every published raster layer and every enabled recipe (`packages/py/reimagina/tiles/sources.py`). `TILE_SERVING.md` has the details.

titiler only opens `file://` URLs that resolve, after symlinks, to an existing file inside the data root; any other scheme, a non-empty host or a path outside the root is rejected (`validated_cog_path`, `apps/api/cog_security.py`).

---

## 8. Scenarios and Grains

Scenarios touch only the feature path, and in practice only blocks:

- A scenario edit is a drawn polygon over one feature type (the API default is `MEX:block`). Materialisation catches a feature when `ST_Covers(edit.geometry, feature.geometry_centroid)` holds and the feature is a member of the scenario's AOI in `aoi_feature`, and writes `scenario_override` rows.
- Indicators read `override ⊕ current observation ⊕ feature.attributes` (`effective_numeric`) and write `indicator_result` rows under the scenario's `scenario_id`. The baseline is `scenario_id IS NULL`.
- Rasters, zonal statistics, rollup observations, `aoi_feature` and `feature_overlap` have no scenario dimension. A scenario never edits geometry.
- `aoi_indicator_summary` has a `scenario_id` and reads indicator values through `scenario_overlay`.

Reads of a scenario go through `scenario_overlay` (`packages/py/reimagina/storage/_utils.py`): per (feature, indicator), the scenario's current row if it carries a value, else the baseline's. `SCENARIOS_LAYER.md` covers authoring, materialisation, the overlay and `GET /scenarios/{scenario_id}/comparison`.

---

## 9. Provenance and Reproducibility

```
feature / observation / file_asset .ingestion_run_id
    └─ curated_geo.ingestion_run       → transformation_step, orchestrator, code_version, status, details
          └─ source_id → data_source_catalog → provider, version, origin storage_uri
observation.lineage_ref
    ├─ zonal statistics: asset_id → file_asset → silver COG storage_uri, crs, dataset_version
    └─ rollup: source_attribute_key, input_feature_type, target_feature_type, method, children

indicator_result.job_run_id
    └─ jobs_runtime.job_run            → code_version, model_family, scenario_id
          └─ run_id → jobs_runtime.orchestration_run (NULL for direct CLI computes)
indicator_result.model_version          → job_run.code_version unless the writer set it
```

`transformation_step` values on the spatial paths: `silver_to_curated` (vector and tabular loads), `raster_landing`, `raster_zonal_stats`, `observation_rollup`. `code_version` on `job_run` is the git commit SHA (`reimagina.provenance.code_version()`, `"dev"` outside a checkout); on `ingestion_run` it is whatever the caller passed — the raster commands default `--code-version` to `dev`, and the recipe dispatcher does not pass one.

```sql
-- A raster-derived observation, its run and its COG
SELECT o.attribute_key,
       o.value_numeric,
       o.valid_from,
       o.lineage_ref ->> 'stat_name'                       AS statistic,
       (o.lineage_ref ->> 'fractional_pixel_count')::float AS pixel_coverage,
       fa.storage_uri,
       fa.crs,
       fa.dataset_version,
       run.transformation_step,
       run.code_version,
       run.details
FROM curated_geo.observation o
JOIN curated_geo.ingestion_run run ON run.id = o.ingestion_run_id
LEFT JOIN curated_geo.file_asset fa ON fa.id = (o.lineage_ref ->> 'asset_id')::uuid
WHERE o.feature_id = :feature_id
  AND o.is_current
  AND o.lineage_ref ->> 'kind' = 'raster_zonal_stats';
```

What can and cannot be reproduced:

- Observations are SCD-2, so the inputs current at any time can be reconstructed from `valid_from` / `valid_to`.
- The bronze original of a raster is under `data/bronze/<source_id>/`; `file_asset` stores no checksum, so file integrity cannot be checked from the database.
- There is no per-run parameter snapshot. A zonal-statistics run records its statistics, `aggregation_kind` and counts in `ingestion_run.details`; a rollup records method, prefixes and grains there; recipe parameters live in `aggregation_recipe.params` (current state only).
- `feature_overlap` and `aoi_feature` are recomputed in place and keep no history.

---

## 10. Schema Reference

DDL of the tables this document owns, as the migrations leave them. Trigger bodies are summarised in comments.

```sql
-- r004. Populated by `aoi compute-features`.
CREATE TABLE curated_geo.aoi_feature (
    aoi_id          uuid             NOT NULL
                                     REFERENCES curated_geo.area_of_interest(id) ON DELETE CASCADE,
    feature_id      uuid             NOT NULL REFERENCES curated_geo.feature(id) ON DELETE CASCADE,
    overlap_basis   text             NOT NULL CHECK (overlap_basis IN ('area','length','presence')),
    overlap_measure double precision NOT NULL CHECK (overlap_measure > 0),
    feature_measure double precision NOT NULL CHECK (feature_measure > 0),
    overlap_ratio   double precision NOT NULL CHECK (overlap_ratio > 0 AND overlap_ratio <= 1),
    PRIMARY KEY (aoi_id, feature_id),
    CHECK (overlap_measure <= feature_measure),
    CHECK (overlap_basis <> 'presence'
           OR (overlap_measure = 1 AND feature_measure = 1 AND overlap_ratio = 1))
);
-- Trigger aoi_feature_before_write: overlap_basis must match the feature's geometry type.

-- r009. Which type pairs `overlap compute-all` computes.
CREATE TABLE curated_geo.feature_overlap_type_pair (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_type_id uuid NOT NULL REFERENCES curated_geo.feature_type_catalog(id),
    target_type_id uuid NOT NULL REFERENCES curated_geo.feature_type_catalog(id),
    UNIQUE (source_type_id, target_type_id)
);

-- r009. Populated by `overlap compute-all`.
CREATE TABLE curated_geo.feature_overlap (
    id                uuid             PRIMARY KEY DEFAULT gen_random_uuid(),
    source_feature_id uuid             NOT NULL REFERENCES curated_geo.feature(id) ON DELETE CASCADE,
    target_feature_id uuid             NOT NULL REFERENCES curated_geo.feature(id) ON DELETE CASCADE,
    overlap_basis     text             NOT NULL CHECK (overlap_basis IN ('area','length','presence')),
    overlap_measure   double precision NOT NULL CHECK (overlap_measure > 0),
    feature_measure   double precision NOT NULL CHECK (feature_measure > 0),
    overlap_ratio     double precision NOT NULL CHECK (overlap_ratio > 0 AND overlap_ratio <= 1),
    computed_at       timestamptz      NOT NULL DEFAULT now(),
    UNIQUE (source_feature_id, target_feature_id)
);

-- r019. Serving record of one COG.
CREATE TABLE curated_geo.raster_layer (
    id               uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id        uuid        NOT NULL UNIQUE
                                 REFERENCES curated_geo.data_source_catalog(id) ON DELETE CASCADE,
    asset_id         uuid        NOT NULL UNIQUE
                                 REFERENCES curated_geo.file_asset(id) ON DELETE CASCADE,
    layer_slug       text        NOT NULL UNIQUE,
    logical_name     text,
    description      text,
    colormap_name    text,
    rescale          text,
    minzoom          integer     NOT NULL DEFAULT 5,
    maxzoom          integer     NOT NULL DEFAULT 14,
    published        boolean     NOT NULL DEFAULT true,
    aggregation_kind text        NOT NULL DEFAULT 'intensive'
                                 CHECK (aggregation_kind IN ('intensive', 'extensive')),
    created_at       timestamptz NOT NULL DEFAULT now()
);

-- r020; coverage_threshold default lowered from 0.5 to 0.0 by r042 (existing rows kept).
CREATE TABLE curated_geo.aggregation_recipe (
    id                         uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    input_kind                 text        NOT NULL CHECK (input_kind IN ('raster', 'observation')),
    input_raster_layer_id      uuid        REFERENCES curated_geo.raster_layer(id) ON DELETE CASCADE,
    input_attribute_key_prefix text,
    input_feature_type_slug    text,
    target_feature_type_slug   text        NOT NULL,
    attribute_key_prefix       text        NOT NULL,
    method                     text        NOT NULL
                                           CHECK (method IN ('mean','weighted_mean','sum','min','max')),
    weight_basis               text        CHECK (weight_basis IN ('area','length','presence')),
    params                     jsonb       NOT NULL DEFAULT '{}'::jsonb,
    coverage_threshold         double precision NOT NULL DEFAULT 0.0
                                           CHECK (coverage_threshold >= 0 AND coverage_threshold <= 1),
    aoi_id                     uuid        REFERENCES curated_geo.area_of_interest(id) ON DELETE SET NULL,
    enabled                    boolean     NOT NULL DEFAULT true,
    created_at                 timestamptz NOT NULL DEFAULT now(),
    updated_at                 timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT aggregation_recipe_input_shape CHECK (
        (input_kind = 'raster' AND input_raster_layer_id IS NOT NULL
             AND input_attribute_key_prefix IS NULL AND input_feature_type_slug IS NULL)
     OR (input_kind = 'observation' AND input_raster_layer_id IS NULL
             AND input_attribute_key_prefix IS NOT NULL AND input_feature_type_slug IS NOT NULL)),
    CONSTRAINT aggregation_recipe_prefix_uk UNIQUE (target_feature_type_slug, attribute_key_prefix)
);
```

Related tables documented elsewhere:

| Table | Spatial columns and rules that matter here | Owner |
|---|---|---|
| `curated_geo.feature` | `geometry geometry(Geometry, 4326) NOT NULL`, `geometry_centroid` (r027), `UNIQUE (source_id, dataset_version, source_record_ref)` | `DATA_LAYER.md` |
| `curated_geo.observation` | `obs_one_current UNIQUE (feature_id, attribute_key) WHERE is_current`; `lineage_ref jsonb` | `DATA_LAYER.md` |
| `curated_geo.file_asset` | `storage_uri`, `format`, `crs`, `resolution_m`, `asset_extent_geometry geometry(Polygon, 4326)` (unused), `source_id UNIQUE` | `DATA_LAYER.md` |
| `curated_geo.area_of_interest` | `geometry geometry(Geometry, 4326) NOT NULL` with a Polygon/MultiPolygon CHECK; `membership_key jsonb` (r040) | `DATA_LAYER.md` |
| `curated_geo.aoi_indicator_summary` | One row per (AOI, scenario, indicator); partial unique indexes `aoi_indicator_summary_base_uk`, `aoi_indicator_summary_scenario_uk` (r015) | `DATA_LAYER.md` |
| `curated_geo.indicator_result` | Per feature; trigger rejects a feature type outside `indicator_catalog.applies_to` unless `quality_flag = 'not_applicable'` | `INDICATORS_LAYER.md` |
| `curated_geo.scenario_edit`, `curated_geo.scenario_override` | Drawn `geometry geometry(Geometry, 4326)` (r048); per-feature overrides (r030) | `SCENARIOS_LAYER.md` |

---

## 11. Query Patterns

### Q1 — Indicator values in an AOI

What `GET /indicators/{slug}/stats` and the value downloads filter on (`aoi_exists`, `packages/py/reimagina/storage/_utils.py`):

```sql
SELECT ir.feature_id, ir.value_numeric
FROM curated_geo.indicator_result ir
WHERE ir.indicator_slug = :indicator_slug
  AND ir.status = 'current'
  AND ir.scenario_id IS NULL        -- or the scenario_overlay('ir', :scenario_id) predicate
  AND EXISTS (SELECT 1 FROM curated_geo.aoi_feature af
              WHERE af.feature_id = ir.feature_id
                AND af.aoi_id = :aoi_id::uuid);
```

`EXISTS`, not a join: a feature in several AOIs would otherwise be returned once per AOI.

### Q2 — Observation rollup inputs

The rows an observation recipe aggregates (`rollup_observation_child_rows`), one per (target feature, source observation):

```sql
SELECT fo.target_feature_id::text,
       obs.attribute_key,
       obs.value_numeric,
       fo.overlap_measure                       -- the weight
FROM curated_geo.feature_overlap fo
JOIN curated_geo.feature src ON src.id = fo.source_feature_id
     AND src.type_slug = :source_feature_type
JOIN curated_geo.feature tgt ON tgt.id = fo.target_feature_id
     AND tgt.type_slug = :target_feature_type
JOIN curated_geo.observation obs
     ON obs.feature_id = fo.source_feature_id
    AND obs.is_current
    AND obs.value_numeric IS NOT NULL
    AND (obs.attribute_key = :input_prefix
         OR obs.attribute_key LIKE :input_prefix || '\_%' ESCAPE '\')
WHERE TRUE
  -- with aoi_id:
  -- AND fo.source_feature_id IN (SELECT feature_id FROM curated_geo.aoi_feature
  --                              WHERE aoi_id = :aoi_id)
;
```

### Q3 — Population-weighted indicator over an AOI

No code path computes one. With block population written by `raster zonal-stats` on an extensive raster (`<prefix>_sum`), it is:

```sql
SELECT SUM(ir.value_numeric * pop.value_numeric)
         / NULLIF(SUM(pop.value_numeric), 0) AS population_weighted_mean
FROM curated_geo.indicator_result ir
JOIN curated_geo.observation pop
  ON pop.feature_id    = ir.feature_id
 AND pop.attribute_key = :population_key     -- e.g. '<prefix>_sum'
 AND pop.is_current
WHERE ir.indicator_slug = :indicator_slug
  AND ir.status = 'current'
  AND ir.scenario_id IS NULL
  AND ir.value_numeric IS NOT NULL
  AND EXISTS (SELECT 1 FROM curated_geo.aoi_feature af
              WHERE af.feature_id = ir.feature_id
                AND af.aoi_id = :aoi_id::uuid);
```

### Q4 — Which grain pairs are related, and how many rows each

```sql
SELECT sc.context || ':' || sc.type_slug AS source,
       tc.context || ':' || tc.type_slug AS target,
       (SELECT count(*)
          FROM curated_geo.feature_overlap fo
          JOIN curated_geo.feature s ON s.id = fo.source_feature_id
          JOIN curated_geo.feature t ON t.id = fo.target_feature_id
         WHERE s.type_id = p.source_type_id
           AND t.type_id = p.target_type_id) AS overlap_rows
FROM curated_geo.feature_overlap_type_pair p
JOIN curated_geo.feature_type_catalog sc ON sc.id = p.source_type_id
JOIN curated_geo.feature_type_catalog tc ON tc.id = p.target_type_id
ORDER BY 1, 2;
```

---

## 12. Performance

### Indexes behind the patterns above

```
-- feature (r003, r007, r027)
feature_geometry_gist      GIST (geometry)
feature_block_geom_idx     GIST (geometry) WHERE type_slug = 'block'
feature_lot_geom_idx       GIST (geometry) WHERE type_slug = 'lot'
feature_centroid_gist      GIST (geometry_centroid)           -- drawn-polygon selection
feature_type_slug_idx (type_slug), feature_type_id_idx (type_id), feature_role_idx (role),
feature_parent_idx (parent_id), feature_source_idx (source_id), feature_ingestion_run_idx (ingestion_run_id)

-- area_of_interest (r004, r007)
aoi_geometry_gist GIST (geometry), area_of_interest_project_idx (project_id)

-- aoi_feature (r004)
PRIMARY KEY (aoi_id, feature_id)                              -- AOI filter
aoi_feature_feature_idx (feature_id)                          -- feature → AOIs

-- feature_overlap (r009, r012)
UNIQUE (source_feature_id, target_feature_id)
feature_overlap_target_idx (target_feature_id, overlap_ratio)
feature_overlap_source_idx (source_feature_id)
feature_overlap_type_pair_target_idx (target_type_id)

-- observation (r003, r007, r026)
obs_one_current        UNIQUE (feature_id, attribute_key) WHERE is_current
obs_feature_key_idx    (feature_id, attribute_key)
obs_attr_feat_current  (attribute_key, feature_id) WHERE is_current
obs_source_run_idx     (source_id, ingestion_run_id)
observation_ingestion_run_idx (ingestion_run_id)
```

`indicator_result` indexes (r016, r047) are listed in `INDICATORS_LAYER.md`. No table is partitioned; partitioning `observation` is deferred until it nears 10M rows (`TODO.md` T2-6).

### Measured costs

| Operation | Measurement | Recorded in |
|---|---|---|
| AOI membership, 343,696 Overture buildings vs. the Mexicali municipal boundary | polygon pass 508,006 ms → 20,249 ms; line pass 457,368 ms → 7,505 ms after subdividing the AOI | `PostgresAOIRepository.compute_overlaps` |
| AOI listing over 1.7 M `aoi_feature` rows | ~2.7 s → < 100 ms after folding six correlated subqueries into one pass | `PostgresAOIRepository.list_aois` |
| Adding `geometry_centroid` to a ~2 M-row `feature` table | ~30–90 s table rewrite | r027 |

`PERFORMANCE.md` holds the benchmark corpus.

### GeoParquet

GeoParquet is an exchange format, not an analytical store: the Overture loader reads Overture's GeoParquet release, large vector silver files are GeoParquet, and `GET /indicators/{slug}/values.parquet` writes GeoParquet 1.1 with a covering bbox (`apps/api/exports.py`).

---

## 13. Anti-Patterns

| Anti-pattern | Problem | Do instead |
|---|---|---|
| `ST_Intersects` against the AOI polygon in a read path | Slow at city scale; disagrees with identifier-based membership (r040) | `EXISTS` on `aoi_feature` (`aoi_exists`) |
| Joining `aoi_feature` instead of `EXISTS` | A feature in several AOIs is returned, and aggregated, more than once | `EXISTS (SELECT 1 FROM curated_geo.aoi_feature ...)` |
| Forgetting `aoi compute-features` after creating an AOI or loading features | AOI-scoped reads return nothing, or miss the new features | Run it after every AOI creation and every feature load |
| Reprojecting raster pixels at ingest | Resampling error | Keep the COG in its native CRS; reproject feature geometry to it |
| Centroid-in-polygon zonal statistics | Drops blocks smaller than a pixel; block sums no longer add up to the raster total | exactextract coverage fractions (`raster_zonal_stats.py`) |
| `sum` rollup across grains that do not nest | Each source value is added in full to every target it touches | Nesting grains only; or `weighted_mean` for intensive values |
| Computing feature-to-feature intersections at query time | Pairwise geometry work on every read | `overlap compute-all` once per declared pair, AOI-scoped |
| Reading a scenario as `scenario_id = :s OR scenario_id IS NULL` | Two rows for every feature the scenario recomputed | `scenario_overlay` |
| Deciding administrative membership by geometry when features carry identifiers | Neighbouring units that cross the boundary become members | `area_of_interest.membership_key` (r040) |

---

## 14. Not Built: Grid Cells (H3, Fishnet)

Nothing in the code stores, computes or serves values per grid cell. There is no `h3` Python dependency (`pyproject.toml`, `uv.lock`) and no `h3` PostgreSQL extension (r001 creates `postgis`, r037 `pgrouting`). The one gridded input, WorldPop, stays a raster and is summarised onto blocks with exactextract, which gives sub-pixel blocks a correct share without a pixel-aligned grid in the database.

If a grid is ever needed, it fits the existing schema as one more grain — no new table:

1. **Feature type.** A `feature_type_catalog` row with `geometry_family = 'polygon'` and `admin_level` NULL (H3 children are not exactly contained in their parent, so the hierarchy would come from `feature_overlap`, not `parent_id`). Today it must use context `MEX` to be a zonal-statistics or rollup target, because those resolve types in `MEX` (§6.3).
2. **Cells as features.** Generate the cells covering the AOI (H3 polygons, or one polygon per pixel of a source raster's transform) and load them through `load_vector_source`, with the cell id (H3 index, or `row:col` of the source raster) in `source_record_ref`. `feature.geometry` is NOT NULL, so every cell stores its polygon.
3. **Relations.** `overlap declare-pair` with the grid on either side, `overlap compute-all`, `aoi compute-features`.
4. **Values.** Raster summaries per cell with `raster zonal-stats --feature-type <grid>`; observations moved onto the grid with an observation recipe, using `weighted_mean` (a `sum` would double-count, §6.4); indicators per cell only after adding the grid's slug to the indicator's `applies_to`.

Choosing between the two: H3 suits analytical grids with no external alignment and hierarchical zoom between resolutions; a fishnet aligned to a source raster suits model outputs that must stay on that raster's pixels. Resampling a population raster to H3 introduces interpolation error; keeping the raster and summarising it per feature, as the platform does now, avoids it.

---

## 15. External References

- **exactextract** (github.com/isciences/exactextract): zonal statistics with each pixel weighted by the fraction of it covered by the polygon. Used for every zonal statistic in place of rasterstats, whose centroid-in-polygon rule dropped manzanas smaller than a WorldPop pixel.
- **WorldPop** (worldpop.org): the global population grid the platform ingests. GHS-POP (European Commission JRC) is the other common one; it is not ingested.
- **Cloud Optimized GeoTIFF / titiler**: the silver raster format and the in-process tile server at `/cog`.
- **STAC** (stacspec.org): the common raster catalog standard. `file_asset` + `raster_layer` play that role locally without STAC (`TODO.md` T3-11).
- **GeoParquet** (geoparquet.org): the format of Overture's releases, read by the Overture loaders, and of the indicator export.
- **H3** (h3geo.org): hierarchical hexagonal grid; relevant only to §14.
