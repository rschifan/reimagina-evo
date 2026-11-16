# Terminology and Conceptual Framing

Reference document for the Reimagina platform. Every term defined here is used consistently across `DATA_LAYER.md`, `INDICATORS_LAYER.md`, `SCENARIOS_LAYER.md`, and `JOB_LAYER.md`. When in doubt about what something means or where it belongs, start here.

---

## Platform modes

The platform operates in three modes. Understanding which mode you are in determines which tables and queries are relevant.

| Mode | What it does | Key tables involved |
|---|---|---|
| **Diagnosis** | Describes the city as it exists today. No scenario. | `feature`, `observation`, `indicator_result` (where `scenario_id IS NULL`) |
| **Exploration** | Sketches proposed changes to the city without running full models. | `scenario_override` |
| **Evaluation** | Computes indicators for a specific scenario and compares results against the base state. | `job_run`, `indicator_result` (where `scenario_id IS NOT NULL`) |

These modes are not mutually exclusive — a project will typically use all three in sequence.

---

## Pipeline

### Bronze
The raw landing zone in object storage. An external dataset is copied here exactly as received from the source, before any transformation. Bronze files are **immutable** — never modified or deleted — which is what makes full re-processing possible without re-fetching from the source.

⚠ **Bronze is required unless the registered source is already an immutable mirror, in which case that mirror *is* bronze.** The Azure data lake is such a mirror, so `points` and `inegi_marco` land straight to silver rather than duplicating gigabytes to buy a guarantee that already holds; both record the reason in their own docstrings. `cdmx`, which fetches from upstream portals directly, does land bronze. Read the rule as being about the guarantee, not about the copy.

### Silver
The cleaned intermediate zone in object storage. Bronze data is standardized here: coordinate reference systems are reprojected to EPSG:4326, invalid geometries are repaired, and records are validated against the registered schema. Silver files are still in source structure, not yet in the canonical model.

### Gold / Curated (`curated_geo`)
The canonical layer in PostgreSQL. This is what the platform reads for analysis and computation. Data enters here from Silver via a loader that assigns provenance metadata. Also referred to as the **curated layer**.

### Serving (`serving_geo`)
A thin view layer over `curated_geo`. The API reads only from `serving_geo`. It is a read contract, not a second source of truth. In v1, serving is implemented as views. Views can be promoted to materialized views for specific hot paths when query performance requires it.

### Ingestion run
A record of one curated-load execution — one step of the Bronze → Silver → curated pipeline for a specific dataset. Every canonical row in `feature` and `observation` carries an `ingestion_run_id` that traces it back to the exact script version and run that produced it.

### Job run
A record of one model computation that reads city data and writes indicator results. Job runs are distinct from ingestion runs: ingestion brings data in, job runs compute outputs from that data. Every `indicator_result` row carries a `job_run_id`. The row is opened by the runner at compute time, not when work is submitted (`jobs/_lifecycle.py:46`).

### Run
One submitted computation — the unit the `Orchestrator` port deals in, persisted as `jobs_runtime.orchestration_run` with one `jobs_runtime.job_unit` per indicator it must recompute. A run is not a job run: a run is scheduler state and spans many indicators, a job run is domain provenance for one compute. The port, run state, and status vocabulary are defined in `JOB_LAYER.md`.

---

## Data

### Source dataset
A named external dataset or feed registered in `data_source_catalog` before any data is loaded. Every source must declare both its raw-format class (`storage_type`) and its semantic routing class (`integration_role`). Examples: INEGI Block Polygons 2020, Sentinel-2 NDVI 2024-Q1, GSV Street Tree Detection Batch, cellphone footfall by block and hour. Registering a source is always the first step.

### Asset
A file or object in object storage — a GeoTIFF, a batch of Street View images, a LAZ point cloud. Large assets stay in object storage permanently. Only metadata and derived values enter PostgreSQL.

### Storage type
The operational classification of a dataset, recorded in `data_source_catalog.storage_type`. It answers "what kind of asset arrived?" and helps determine the loading path.

| Storage type | Typical Gold destination |
|---|---|
| `vector` | `feature` rows |
| `tabular` | `observation` rows |
| `raster` | `file_asset` metadata + zonal statistics → `observation` |
| `imagery` | `file_asset` metadata + ML detections → `feature` rows |
| `point_cloud` | Derived metrics → `observation` |

### Integration role
The conceptual purpose of a dataset in the model, recorded in `data_source_catalog.integration_role`. It answers "how does this dataset participate in the city model?" and is separate from `storage_type`.

| `integration_role` | Meaning | Lands in |
|---|---|---|
| `canonical_entity` | Defines stable objects that should exist in the city model | `feature` |
| `enrichment` | Adds measurements or attributes to existing features | `observation` |
| `model_output` | User-facing computed outputs imported from outside the normal worker pipeline | `indicator_result` |

For most baseline datasets, `storage_type` and `integration_role` work together. Example: GSV detections are usually `storage_type = 'imagery'` and `integration_role = 'canonical_entity'`; a zonal NDVI dataset is `storage_type = 'raster'` and `integration_role = 'enrichment'`.

### Scenario relevance
The role a dataset plays during scenario work. This is not a schema column in v1, but it must be decided during onboarding because it changes how workers use the data.

| Scenario relevance | Meaning | Typical examples |
|---|---|---|
| Baseline-defining | Describes what physically exists in the city today | blocks, lots, roads, bus stops |
| Baseline evidence | Read-only measurements that help models describe current conditions | census counts, NDVI, sidewalk width from GSV, cellphone footfall |
| Scenario-editable context | Values that scenarios may override through `curated_geo.scenario_override` | floors (the one wired today); sidewalk width, lane count, green area share |
| Output-only | Results produced by models, not inputs | walkability score, liveability index |

---

## Spatial objects

### Feature
A stable canonical spatial object — something that should exist in the city model with a persistent identity, a geometry, and provenance. Examples: a city block, a parcel, a road segment, a bus stop, a validated tree detection. All features live in `curated_geo.feature`, regardless of type.

### Feature type
The kind of spatial object a feature represents. Defined in `feature_type_catalog` as a row — not a table. Examples: `block`, `lot`, `tree`, `road`, `grid_cell`. Adding a new feature type is one INSERT.

### Role
The semantic function of a feature type in the model. Every feature type has exactly one role.

| Role | Meaning | Examples |
|---|---|---|
| `analysis_unit` | Spatial units that receive indicator values | block, lot, ageb, grid_cell |
| `context` | Administrative framing; not directly analysed | municipality, state |
| `infrastructure` | Physical urban fabric | road, sidewalk, bike_lane |
| `poi` | Point of interest, establishment, amenity | bus_stop, station, establishment |
| `detection` | ML-derived object with a confidence score | tree (from GSV), barrier, crossing |

### Hierarchy / admin_level
For types that are spatially nested (e.g., block inside municipality inside state), `admin_level` records the depth of each type in the hierarchy (1 = coarsest, N = finest). A feature's `parent_id` points to its containing feature. Types with `admin_level = NULL` are non-hierarchical (trees, bus stops).

### Context
The geographic or project scope that a feature type belongs to. `'global'` types apply everywhere. `'MEX'` types are Mexico-specific. A project UUID can scope types to a single project. The `UNIQUE (context, type_slug)` constraint allows the same slug to mean different things in different contexts without conflict.

### Feature attributes (`feature.attributes`)
Immutable base properties that belong to the canonical object itself and do not change after the object is created. Examples: `{"area_m2": 4200}` for a block, `{"species": "Ficus benjamina"}` for a tree. These are set at insert time and do not carry their own provenance.

### Observation (`curated_geo.observation`)
A measured or derived value about an existing feature that comes from a separate dataset, computed at a different time, and must retain its own provenance. Implements the OGC Observations & Measurements pattern (ISO 19156). Examples: census population count, NDVI mean from a satellite image, sidewalk width from GSV. Each observation carries its own `source_id`, `ingestion_run_id`, and currency flag (`is_current`). The current value for each `(feature_id, attribute_key)` pair is enforced by a partial unique index.

**The distinction in one sentence:** `feature.attributes` is what the object *is*; `observation` is what we have *learned about it* from external data.

### ML detection
An object identified by a machine learning model run over imagery or point cloud data — a specific tree, a barrier, a building front — each with a geometry and a confidence score. If the team considers a detection reliable enough to persist as a stable city object, it is promoted to a `feature` row (with `confidence` and `model_version` set). Measurements about a detected object (height estimate, canopy area) go to `observation`.

Note: in OGC Observations & Measurements, an ML detection is a kind of observation about a place. Here we use the word in a narrower operational sense: a candidate spatial object produced by a model, which may be promoted to a `feature`.

### Area of interest (AOI)
A user-drawn polygon that defines the spatial scope of a project. It is not a city feature — it is project-scoped and has a different lifecycle. It defines where analysis runs. Stored in `curated_geo.area_of_interest`, linked to `app_core.project`.

### Dataset extent
The geographic footprint of a source dataset or asset collection. Stored as metadata such as `data_source_catalog.source_extent_geometry` or `file_asset.asset_extent_geometry`. This describes where the source is available, not how much of a feature lies inside a project AOI.

### AOI overlap ratio
The fraction of a feature's area, length, or presence that falls inside an AOI. Stored in `curated_geo.aoi_feature.overlap_ratio`. Used to prorate indicators for features that straddle an AOI boundary. Computed using exact geometry, never in angular degrees.

---

## Scenarios

### Project
A workspace row in `app_core.project` (r001:27). It exists because `curated_geo.area_of_interest.project_id` references it (r004:21). It groups scenarios: `app_core.scenario.project_id` (r048) references it.

### Scenario
A named proposed state of the city. A scenario is a row in `app_core.scenario` (r048), unique by name within its project, and is the addressed resource for authoring. The `scenario_id` columns on `scenario_override`, `indicator_result`, `job_run` and `aoi_indicator_summary` remain plain uuids with no foreign key (r016:7-10, r030:5-6); the authoring code, not the schema, keeps them consistent with the row. A scenario never modifies baseline data; it overlays changes through override rows and expresses its effects through recomputed indicator results. `NULL` means the base city.

### Attribute override
One row in `curated_geo.scenario_override` (r030) — a `(scenario_id, feature_id, attribute_key)` triple carrying exactly one of `value_numeric` / `value_text`. It outranks observations and base attributes when the effective value is resolved. The underlying `feature` row is never modified. The override rows *are* the scenario: reading them back yields the change-set a run needs (`scenarios/overrides.py:96`).

### Effective value
What an indicator actually reads for an attribute: `override ⊕ current observation ⊕ base feature.attributes`, first non-null wins, resolved in SQL by `scenarios.overrides.effective_numeric` (`overrides.py:50`). With no scenario it collapses to `observation ⊕ base` — the baseline.

### Base state
The city as it exists today. Represented by `indicator_result` rows where `scenario_id IS NULL`. This is the Diagnosis mode output.

### Scenario state
The computed result for a specific proposed future. Represented by `indicator_result` rows where `scenario_id = :uuid`. This is the Evaluation mode output. Base and scenario results live in the same table and are compared by joining on `feature_id` and `indicator_slug`.

---

## Indicators

### Indicator
A computed value for a specific spatial feature in a specific state of the world. Defined in `indicator_catalog` (what it means), stored in `indicator_result` (the computed value).

### Direct indicator
An indicator produced directly by a model worker reading city data. Has a `model_family` set. Examples: walkability score (accessibility model), floor area ratio (housing model).

### Composite indicator
An indicator derived from a weighted combination of other indicators. Has `model_family = NULL` (enforced by a CHECK, r016:86-87). Defined via `indicator_dependency`. The shipped example is `urban_form_block_composite` — compactness + building density, 0.5/0.5. Composites are ordinary units in the same run as their components, ordered after them by the impact engine, and all share the generic `compute_composite` runner (`orchestration/runners.py:86-87`).

### Quality flag (ISO 19157)
A classification of how reliable a computed indicator value is.

| Flag | Meaning |
|---|---|
| `measured` | Directly computed from authoritative source data |
| `estimated` | Derived at a different resolution or scale |
| `imputed` | Filled using a spatial or statistical model |
| `missing` | Input data was unavailable; `missing_reason` must be set |
| `not_applicable` | This indicator does not apply to this feature type |

### Current / stale / failed
The lifecycle status of an `indicator_result` row.

| Status | Meaning |
|---|---|
| `current` | The latest valid result for this (feature, scenario, indicator). What the API reads. |
| `stale` | Input data or scenario changed; this result needs recomputation. |
| `failed` | The last computation attempt failed; see `job_run.error_message`. |

Execution state (`queued`, `running`) lives on `job_run`, not on `indicator_result`.

### Staleness propagation
When a scenario override changes features, the affected indicator results are marked `stale`. Any composite indicator that depends on a stale base indicator is also marked `stale`, recursively, using the `indicator_dependency` graph.

---

## Key distinctions

These are the points most likely to cause confusion. Refer to this section when deciding where something belongs.

### `feature.attributes` vs `observation`

| | `feature.attributes` | `observation` |
|---|---|---|
| **What it stores** | Immutable base properties of the object | Mutable or derived values from external datasets |
| **Provenance** | Inherits from the feature row | Has its own `source_id` and `ingestion_run_id` |
| **Changes over time** | Never updated after insert | New rows added; old rows closed with `valid_to` |
| **Examples** | `{"area_m2": 4200}` on a block | `population_total` from census, `ndvi_mean` from satellite |

### `feature` vs `observation`

When a dataset arrives, ask: does it define a new spatial entity that should have its own geometry and identity in the city model? If yes → `feature`. If it describes or measures something about an existing entity → `observation`.

### `ingestion_run` vs `job_run`

| | `ingestion_run` | `job_run` |
|---|---|---|
| **Purpose** | Records one data loading execution | Records one model computation execution |
| **Produced by** | ETL loaders (Bronze → curated pipeline) | Analytical model workers |
| **Referenced by** | `feature.ingestion_run_id`, `observation.ingestion_run_id` | `indicator_result.job_run_id` |

### `curated_geo` vs `serving_geo`

| | `curated_geo` | `serving_geo` |
|---|---|---|
| **What it is** | Canonical tables — source of truth for the platform | Read contract — what the API is allowed to read |
| **Written by** | ETL loaders and model workers | Never written to directly; derived from `curated_geo` |
| **Implementation** | Tables with indexes and triggers | Views (or materialized views for hot paths) |

### `scenario_id IS NULL` vs `scenario_id IS NOT NULL`

Both patterns appear in `indicator_result`. `NULL` means the base city (Diagnosis). A UUID means a specific scenario (Evaluation). Comparisons between the two states are done by joining `indicator_result` on `feature_id` and `indicator_slug` with the two different scenario conditions.

---

## Decision rules

Quick-reference rules for the most common decisions.

**Where does a new dataset land?**
```
1. Set storage_type                               → vector / tabular / raster / imagery / point_cloud
2. Set integration_role                           → canonical_entity / enrichment / model_output
3. Decide scenario relevance                      → baseline-defining / baseline evidence / scenario-editable / output-only
4. Route to canonical table:
   Defines stable city objects with geometry?     → feature
   Describes or measures an existing object?      → observation
   Large file asset (raster, image, LiDAR)?       → object storage + file_asset metadata
                                                     + derived values → observation
   Computed output shown to users?                → indicator_result
```

**How should unusual datasets be treated?**
```
Cellphone aggregates by block/zone/time window?   → observation on the relevant feature grain
Pairwise OD matrix or network flow table?         → future mobility-flow extension, not the core per-feature model
ML detection with stable geometry and reuse?      → feature (with confidence and model_version)
ML score about an existing object?                → observation
```

**What is the difference between extent and overlap?**
```
Where does the dataset exist geographically?       → dataset extent metadata
How much of a feature lies inside a project AOI?   → aoi_feature overlap fields
```

**Where do scenario-related edits go?**
```
Changing a feature's attribute for one scenario?   → curated_geo.scenario_override
Changing model assumptions (not feature-specific)? → nowhere — no configuration
                                                     table exists; model constants
                                                     live in code
Never modify feature or observation rows for a scenario.
```

**When should a detection become a feature vs an observation?**
```
Stable identity, persistent geometry, reused across analyses?   → feature (with confidence)
Measurement or score about an existing feature?                  → observation
```

**When is an indicator result stale?**
```
A feature's baseline attributes changed?           → stale for all scenarios that used that feature
A scenario_override row changed?                   → stale for that scenario only
A composite's component changed?                   → stale transitively via indicator_dependency
```

---

## Source notes

| Concept | Origin |
|---|---|
| Bronze / Silver / Gold pipeline | Databricks Medallion Architecture |
| Single typed feature table | OGC Simple Features for SQL; Overture Maps theme model |
| `admin_level` integer for hierarchy | OpenStreetMap convention |
| `observation` table name and pattern | OGC Observations and Measurements (ISO 19156) |
| Raster asset metadata | STAC — SpatioTemporal Asset Catalog |
| Quality flags | ISO 19157 — Geographic information data quality |
| Long-format indicator results | Analytical fact-table pattern (UrbanSim, ActivitySim) |
| Composite indicator dependency graph | BI semantic-layer / UN SDG indicator framework |
| SCD Type 2 pattern for observations | Data warehouse slowly changing dimension pattern |
