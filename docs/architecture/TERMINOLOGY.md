# Terminology and Conceptual Framing

Terminology of the prototype. Every term defined here is used consistently across `DATA_LAYER.md`, `INDICATORS_LAYER.md`, `SCENARIOS_LAYER.md`, `JOB_LAYER.md`, and `ASSET_LAYER.md`. When in doubt about what something means or where it belongs, start here.

---

## Platform modes

The platform operates in three modes. Understanding which mode you are in determines which tables and queries are relevant.

| Mode | What it does | Key tables involved |
|---|---|---|
| **Diagnosis** | Describes the city as it exists today. No scenario. | `feature`, `observation`, `indicator_result` (where `scenario_id IS NULL`) |
| **Exploration** | Sketches proposed changes to the city without running full models. | `app_core.scenario`, `scenario_rule`, `scenario_override` |
| **Evaluation** | Computes indicators for a specific scenario and compares results against the base state. | `orchestration_run`, `job_unit`, `job_run`, `indicator_result` (where `scenario_id IS NOT NULL`) |

These modes are not mutually exclusive — a project will typically use all three in sequence.

---

## Pipeline

### Bronze
The raw landing zone: `data/bronze/<source_id>/` under the data directory (`REIMAGINA_DATA_DIR`, default `<repo>/data`; `paths.data_dir`). An external dataset is copied here exactly as received from the source, before any transformation (`pipeline/bronze.py::land_file`). Bronze files are **immutable** — never modified or deleted — which is what makes full re-processing possible without re-fetching from the source. Bronze and silver are local files; no object-storage backend is implemented.

⚠ **Bronze is required unless the registered source is already an immutable mirror, in which case that mirror *is* bronze.** The source bundle, checked by its SHA256SUMS, is such a copy, so `points` and `inegi_marco` land straight to silver rather than duplicating it to buy a guarantee that already holds; both record the reason in their own docstrings. `cdmx`, which fetches from upstream portals directly, does land bronze. Read the rule as being about the guarantee, not about the copy.

### Silver
The cleaned intermediate zone, `data/silver/<source_id>/`. Vector data is standardized here by `pipeline/silver.py::clean_vector`: a CRS is required, data is reprojected to EPSG:4326, invalid geometries are repaired with `make_valid`, and null or empty geometries are dropped; the result is written as GeoParquet. Rasters are re-encoded to a Cloud-Optimised GeoTIFF in their **native** CRS, not reprojected (`pipeline/raster.py`). Silver files are still in source structure, not yet in the canonical model.

### Gold / Curated (`curated_geo`)
The canonical layer in PostgreSQL. This is what the platform reads for analysis and computation. Data enters here from Silver via a loader that assigns provenance metadata. Also referred to as the **curated layer**.

### Serving (`serving_geo`)
A thin view layer over `curated_geo`, plus the tile functions (`get_tile`, `indicator_overlay_tile`, `aggregation_overlay_tile`) Martin and the API's `/tiles` route serve. It is a read contract, not a second source of truth. Serving is implemented as views; one hot path is a materialized view (`feature_current_attributes_mv`, r008). Baseline feature reads (`serving_geo.feature_v`, `feature_aoi_v`) and `GET /indicators/{slug}/values` (`current_indicator_v`) go through it. It is not exclusive: indicator statistics, per-feature indicator ranks and scenario comparison read `curated_geo.indicator_result` directly, scenario authoring reads `app_core` and `curated_geo`, run state is read from `jobs_runtime`, and the indicator computes read `curated_geo`.

### Ingestion run
A record of one curated-load execution — one step of the Bronze → Silver → curated pipeline for a specific dataset. Every canonical row in `feature` and `observation` carries an `ingestion_run_id` that traces it back to the exact script version and run that produced it.

### Job run
A record of one indicator computation that reads city data and writes indicator results — `jobs_runtime.job_run`, one row per compute, so one per executed unit of a run (or per standalone `indicators compute` from the CLI). Job runs are distinct from ingestion runs: ingestion brings data in, job runs compute outputs from that data. Every `indicator_result` row carries a `job_run_id` (`NOT NULL`). The row is opened by the runner at compute time, straight to `running`, not when work is submitted (the `job_run` context manager in `jobs/_lifecycle.py`); `job_run.run_id` names the run it belongs to, `NULL` for a standalone compute.

### Run
One submitted computation — the unit the `Orchestrator` port deals in, persisted as `jobs_runtime.orchestration_run` (the run id is also its DBOS workflow id) with one `jobs_runtime.job_unit` per indicator it must recompute. A run is not a job run: a run is scheduler state and spans many indicators, a job run is domain provenance for one compute. The port, run state, and status vocabulary are defined in `JOB_LAYER.md`.

### Unit
One indicator inside a run — a `jobs_runtime.job_unit` row, unique on `(run_id, indicator_slug)`, with its own status (`queued`, `running`, `succeeded`, `failed`, `skipped`, `cancelled`). On the DBOS engine each unit is one `unit_workflow` on the `units` queue, with workflow id `<run_id>-<indicator_slug>`, and its resolved feature set lives in the **run plan** (`jobs_runtime.run_plan`, r036: one row per dirty indicator, written at submit, read by the unit, deleted when the run is terminal), so `job_unit.feature_scope` is written empty there. Executing a unit opens one job run.

### Change-set and dirty set
The **change-set** is what a submit names as changed: attributes and the features they changed on (`changed_attributes`, `edited_features`), or whole datasets (`changed_datasets`). The **dirty set** is the list of `DirtyUnit`s — indicator plus feature scope — that must recompute, in dependency order. `orchestration/plan.py::plan_run` derives it: an explicit indicator list as given; otherwise from the change-set through the impact engine (`orchestration/impact.py`, seeded from each indicator's `reads`, propagated along `depends_on`); with nothing named, from asset freshness. An empty dirty set is a `noop` and persists no run.

### Wave
The units of a run that may start now: queued units whose dependencies inside the run have all succeeded (`orchestration/wave.py::plan_wave`). Re-evaluated after every completion, so a unit starts as soon as it unblocks.

### Worker
The process that executes runs: `python -m apps.worker`, which sets `REIMAGINA_DBOS_EXECUTION_MODE=executor` and drains the DBOS queues. The API is a `client` by default — it submits, reads, cancels and streams, but executes nothing — so without a worker a submitted run stays `queued`.

---

## Data

### Source dataset
A named external dataset or feed registered in `data_source_catalog` before any data is loaded. Every source must declare both its raw-format class (`storage_type`) and its semantic routing class (`integration_role`). Examples: INEGI Block Polygons 2020, Sentinel-2 NDVI 2024-Q1, GSV Street Tree Detection Batch, cellphone footfall by block and hour. Registering a source is always the first step.

### File asset (`curated_geo.file_asset`)
A file a source produced — a GeoTIFF, a batch of Street View images, a LAZ point cloud — recorded as one `file_asset` row (`format` one of `COG`, `GeoTIFF`, `NetCDF`, `LAZ`, `JPEG`, `PNG`; allowed only for `raster`, `imagery` and `point_cloud` sources). The file itself stays on disk under the data directory (a raster's silver COG is `file://data/silver/…`); only metadata and derived values enter PostgreSQL.

### Asset (asset graph)
Not the same thing as a file asset. In the asset graph (`curated_geo.asset`, r041; `ASSET_LAYER.md`) an asset is any named artifact the platform reads or produces — a feature type, an observation prefix, a raster layer, a scenario override attribute, an indicator — of kind `source`, `external` or `derived`, linked by `asset_dependency` edges and versioned per partition (the baseline, or one scenario). A source asset's version is recorded by whatever changed it; a derived asset's version is a hash of its whole upstream closure.

### Freshness
Whether a derived asset's materialized version matches the version its inputs imply, computed on read by `serving_geo.asset_freshness_v`: `fresh`, `stale` (versions differ), `partial` (materialized over only some of its features) or `absent` (never materialized in that partition). `stale` and `partial` count as dirty; `absent` counts in a scenario partition but on the baseline only when asked for (`assets/freshness.py`). A run over a whole AOI is `partial` in the view but done for that AOI: planning scoped to the same AOI does not count it as dirty.

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
| Scenario-editable context | Values that scenarios may override through `curated_geo.scenario_override` | the five authorable attributes today (`assets.registry.authorable_attributes`): `floors`, `num_levels`, `unit_size`, `people_per_household`, `lot_coverage` |
| Output-only | Results produced by models, not inputs | network distance to schools, `livability_block_score` |

---

## Spatial objects

### Feature
A stable canonical spatial object — something that should exist in the city model with a persistent identity, a geometry, and provenance. Examples: a city block, a parcel, a road segment, a bus stop, a validated tree detection. All features live in `curated_geo.feature`, regardless of type.

### Feature type
The kind of spatial object a feature represents. Defined in `feature_type_catalog` as a row — not a table. Examples: `block`, `lot`, `tree`, `road`, `building`. Adding a new feature type is one INSERT; the seeded set is `FEATURE_TYPE_ROWS` in `data/feature.py` (the same rows r003 inserts).

### Role
The semantic function of a feature type in the model. Every feature type has exactly one role (a CHECK on `feature_type_catalog.role`). The examples are the seeded types.

| Role | Meaning | Examples |
|---|---|---|
| `analysis_unit` | Spatial units that receive indicator values | block, lot, ageb |
| `context` | Framing — administrative units, and building footprints; not directly analysed | state, municipality, neighborhood, building |
| `infrastructure` | Physical urban fabric | road, sidewalk, bike_lane, crossing |
| `poi` | Point of interest, establishment, amenity | bus_stop, station, establishment |
| `detection` | ML-derived object with a confidence score | tree (from GSV) |

### Hierarchy / admin_level
For types that are spatially nested (e.g., block inside municipality inside state), `admin_level` records the depth of each type in the hierarchy (1 = coarsest, N = finest). A feature's `parent_id` points to its containing feature. Types with `admin_level = NULL` are non-hierarchical (trees, bus stops).

### Context
The geographic or project scope that a feature type belongs to. `'global'` types apply everywhere. `'MEX'` types are Mexico-specific. A project UUID can scope types to a single project. The `UNIQUE (context, type_slug)` constraint allows the same slug to mean different things in different contexts without conflict.

### Feature attributes (`feature.attributes`)
Immutable base properties that belong to the canonical object itself and do not change after the object is created. Examples: `{"area_m2": 4200}` for a block, `{"species": "Ficus benjamina"}` for a tree. These are set at insert time and do not carry their own provenance.

⚠ One writer breaks the rule on purpose: the demo fixture `scripts/seed_demo_attributes.py` merges `num_levels`, `floors`, `unit_size`, `people_per_household`, `lot_coverage` (fit mode only), `poi_version` and `network_version` (fitted to the buildings and the census with `--fit-to-census`) into the `attributes` of existing blocks, because no shipped pipeline populates them all. Its docstring says so; real values belong in `observation`, which `effective_numeric` prefers.

### Observation (`curated_geo.observation`)
A measured or derived value about an existing feature that comes from a separate dataset, computed at a different time, and must retain its own provenance. Implements the OGC Observations & Measurements pattern (ISO 19156). Examples: census population count, NDVI mean from a satellite image, sidewalk width from GSV. Each observation carries its own `source_id`, `ingestion_run_id`, and currency flag (`is_current`). The current value for each `(feature_id, attribute_key)` pair is enforced by a partial unique index.

**The distinction in one sentence:** `feature.attributes` is what the object *is*; `observation` is what we have *learned about it* from external data.

### ML detection
An object identified by a machine learning model run over imagery or point cloud data — a specific tree, a barrier, a building front — each with a geometry and a confidence score. If the team considers a detection reliable enough to persist as a stable city object, it is promoted to a `feature` row (with `confidence` and `model_version` set). Measurements about a detected object (height estimate, canopy area) go to `observation`.

Note: in OGC Observations & Measurements, an ML detection is a kind of observation about a place. Here we use the word in a narrower operational sense: a candidate spatial object produced by a model, which may be promoted to a `feature`.

### Area of interest (AOI)
A polygon that defines the spatial scope of a project. It is not a city feature — it is project-scoped and has a different lifecycle. It defines where analysis runs. Stored in `curated_geo.area_of_interest`, linked to `app_core.project`. Today AOIs are created from administrative boundaries by the CLI (`cdmx aoi-from-state`, `cdmx aoi-from-alcaldia`, `overture aoi-from-division`); an AOI may declare `membership_key` (r040), so features join it by identifier (`feature.attributes @> membership_key`) rather than by geometry alone.

### Dataset extent
The geographic footprint of a source dataset or asset collection. Stored as metadata such as `data_source_catalog.source_extent_geometry` or `file_asset.asset_extent_geometry`. This describes where the source is available, not how much of a feature lies inside a project AOI.

### AOI overlap ratio
The fraction of a feature's area, length, or presence that falls inside an AOI. Stored in `curated_geo.aoi_feature.overlap_ratio`. Used to prorate indicators for features that straddle an AOI boundary. Computed using exact geometry, never in angular degrees.

---

## Scenarios

### Project
A workspace row in `app_core.project` (r001). It exists because `curated_geo.area_of_interest.project_id` references it (r004). It groups scenarios: `app_core.scenario.project_id` (r048) references it.

### Scenario
A named proposed state of the city. A scenario is a row in `app_core.scenario` (r048), unique by name within its project, bound to an AOI (`aoi_id`, required at creation; nullable only because deleting the AOI empties it), and is the addressed resource for authoring. Its rules, overrides, results, job runs and AOI summaries reference it `ON DELETE CASCADE` (r055). A scenario never modifies baseline data; it overlays changes through override rows and expresses its effects through recomputed indicator results. `NULL` means the base city.

### Rule
A **rule** is one row of `curated_geo.scenario_rule` (r055): one drawn area (polygon or multipolygon), one feature type, and the numbers a planner set over it (`inputs`: storeys, m² per dwelling, people per household, lot coverage — only the ones given). Its `ordinal` is its place in "later wins" where areas overlap, per number; it is assigned when the rule is created and kept when the rule is edited or moved. Materialisation writes storeys as both `floors` and `num_levels` (`scenarios/proposal.py::edits_for`). Before r055 a rule was a set of per-attribute **scenario edits** (`scenario_edit`, r048) grouped by identical geometry; `scenario_edit` no longer exists. In code an **edit** is now one `{attribute_key, value_numeric}` pair a rule's numbers expand to (`proposal.edits_for`: one per attribute, so storeys give two), the unit `apply_overrides` upserts.

### Ordinal
`scenario_rule.ordinal`, a rule's place in "later wins": `max + 1` of the scenario's current rules, under its row lock, when the rule is created; never renumbered after a delete, and unchanged by `PATCH …/rules/{rule_id}` or `scenario update-rule`. Where rules overlap, each number is taken from the highest-ordinal rule that sets it; a later rule that leaves a number out does not override an earlier rule's value for it. The page shows it as "Rule N", N being the rank in ordinal order (`rule_numbers` in a `ceiling_exceeded` refusal).

### Materialisation
Resolving a scenario's rules into per-feature `scenario_override` rows inside its AOI (`scenarios/authoring.py::materialize_scenario`). It selects each rule's blocks (members of the AOI whose centroid the area covers, `ST_Covers`), resolves overlaps by ordinal (later wins, per number), refuses the whole scenario if any block would breach an indicator ceiling (`CeilingExceeded`), deletes overrides no rule implies any more, publishes the scenario's override versions, marks the scenario's now-unbacked results `stale`, and sets `app_core.scenario.materialized_at`. Writing, editing or deleting a rule materialises in the same transaction (`materialize_for_rule`). Its **footprint** — every block a current rule overrides, with the attributes involved (those whose last override was removed included) — is the change-set the rule routes and `POST /scenarios/{id}/recompute` submit; an empty footprint submits nothing.

### Attribute override
One row in `curated_geo.scenario_override` (r030) — a `(scenario_id, feature_id, attribute_key)` triple carrying exactly one of `value_numeric` / `value_text`. It outranks observations and base attributes when the effective value is resolved. The underlying `feature` row is never modified. Override rows are written by materialisation, not drawn directly, and reading them back yields the scenario's change-set (`scenarios.overrides.scenario_change_set`).

### Partition
The scenario dimension of the asset graph: an asset's versions and materialisations are keyed by `partition_key`, `''` for the baseline and `scenario=<id>` for one scenario (`assets/keys.py::partition_key`). A string, not an FK, so `delete_scenario` deletes the scenario's partition explicitly.

### Estimated block
A block whose scenario inputs the census fit had to guess: `feature.attributes.estimated` = `no_footprint` (dwellings but no Overture footprint) or `thin_footprint` (footprint leaving under 20 m² per dwelling), set by `scripts/seed_demo_attributes.py --fit-to-census`; such a block takes the median lot coverage. `GET /indicators/{slug}/stats` counts the rows on them (`count_estimated`) and `serving_geo.indicator_overlay_tile` emits the reason as an `estimated` property (r056). Not the same thing as the `estimated` quality flag below.

### Effective value
What an indicator actually reads for an attribute: `override ⊕ current observation ⊕ base feature.attributes`, first non-null wins, resolved in SQL by `scenarios.overrides.effective_numeric`. With no scenario it collapses to `observation ⊕ base` — the baseline.

### Base state
The city as it exists today. Represented by `indicator_result` rows where `scenario_id IS NULL`. This is the Diagnosis mode output.

### Scenario state
The computed result for a specific proposed future. Represented by `indicator_result` rows where `scenario_id = :uuid`. This is the Evaluation mode output. Base and scenario results live in the same table and are compared by joining on `feature_id` and `indicator_slug`.

---

## Indicators

### Indicator
A computed value for a specific spatial feature in a specific state of the world. Defined in `indicator_catalog` (what it means), stored in `indicator_result` (the computed value).

### Direct indicator
An indicator produced directly by a model worker reading city data — `computation_kind = 'direct'`, with a `model_family` set. Examples: `accessibility_block_school_network_m` (`model_family = 'accessibility'`), `urban_form_block_compactness` and the edificability family (`'urban_form'`).

### Composite indicator
An indicator derived from a weighted combination of other indicators — `computation_kind = 'composite'`, with `model_family = NULL` (enforced by a CHECK, r016). Defined via `indicator_dependency`. Two ship: `urban_form_block_composite` (compactness + building density, 0.5/0.5) and `livability_block_score` (housing density 0.4, scored against a 50–150 dwellings/ha **density band** — 1 inside it, falling linearly to 0 at 0 and at 300 (`catalog.LIVABILITY_DENSITY_BAND_LOW`/`_HIGH`, `composite.component_score`) — network distance to school, health and park 0.2 each — the distances are lower-is-better and flipped before weighting). Composites are ordinary units in the same run as their components, ordered after them by the impact engine, and all share the generic `compute_composite` runner (`composite.compute_composite`, bound in `runners.default_runners`).

### Quality flag (ISO 19157)
A classification of how reliable a computed indicator value is.

| Flag | Meaning |
|---|---|
| `measured` | Directly computed from authoritative source data |
| `estimated` | Derived at a different resolution or scale. Every scenario row a compute writes as `measured` is stored as `estimated` (a what-if is a projection, not a measurement; `_flag_for` in the Postgres result repository) |
| `imputed` | Filled using a spatial or statistical model |
| `missing` | Input data was unavailable; `missing_reason` must be set |
| `not_applicable` | This indicator does not apply to this feature type |

### Current / stale / failed
The lifecycle status of an `indicator_result` row (CHECK in r016).

| Status | Meaning |
|---|---|
| `current` | The latest valid result for this (feature, scenario, indicator). What the API reads. |
| `stale` | Superseded. Written when a newer result for the same (feature, scenario, indicator) is written (stale-then-insert in the result repository), and when materialisation leaves a scenario result with no override behind it. A deleted scenario's results are deleted, not staled. It does **not** mean "an input changed" — that is freshness, below. |
| `failed` | Permitted by the CHECK, but no code path writes it. A failed compute is recorded on `job_run` (`status = 'failed'`, `error_message`), and the previous `current` row stays. |

Execution state lives on `job_unit` (`queued`, `running`, …) and `job_run` (opened as `running`), not on `indicator_result`.

### Staleness propagation
Not marked on rows. Whether an indicator is out of date is derived on read by the asset graph: an edit, a reload or a logic-version bump moves an upstream version, and every indicator whose upstream closure contains it — composites included, through their `depends_on` edges — reads as `stale` in `serving_geo.asset_freshness_v` — in every partition for a baseline change, in that scenario's partition only for a scenario's override. A submit that names nothing recomputes exactly those (`orchestration/plan.py::plan_run`). See `ASSET_LAYER.md`.

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
| **Produced by** | ETL loaders (Bronze → curated pipeline), and the raster zonal-stats and observation-rollup workers that write observations | Indicator compute functions, one per unit |
| **Referenced by** | `feature.ingestion_run_id`, `observation.ingestion_run_id` | `indicator_result.job_run_id` |

### `orchestration_run` vs `job_unit` vs `job_run`

| | `orchestration_run` | `job_unit` | `job_run` |
|---|---|---|---|
| **One row per** | run (one submit) | indicator in a run | executed compute |
| **Owner** | ops-plane (scheduler state) | ops-plane (scheduler state) | domain (provenance) |
| **Statuses** | `queued`, `running`, `succeeded`, `partial`, `failed`, `cancelling`, `cancelled` | `queued`, `running`, `succeeded`, `failed`, `skipped`, `cancelled` | `queued`, `running`, `succeeded`, `failed`, `cancelled` (CHECK); written as `running`, then `succeeded` or `failed` |
| **Links** | — | `run_id` → `orchestration_run` | `run_id` (no FK, `NULL` outside a run); cited by `indicator_result.job_run_id` |

### `curated_geo` vs `serving_geo`

| | `curated_geo` | `serving_geo` |
|---|---|---|
| **What it is** | Canonical tables — source of truth for the platform | Read contract — views and tile functions for baseline features and current indicator values; not the only thing the API reads (see Serving above) |
| **Written by** | ETL loaders, observation workers, indicator computes and scenario authoring | Never written to directly; derived from `curated_geo` |
| **Implementation** | Tables with indexes and triggers | Views (one materialized view, r008) and SQL tile functions |

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
   Large file asset (raster, image, LiDAR)?       → file on disk (data/) + file_asset metadata
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
Changing an attribute over an area, for one scenario? → curated_geo.scenario_rule (a rule),
                                                     materialised into
                                                     curated_geo.scenario_override
                                                     (one row per feature × attribute)
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

**When is an indicator out of date?** (freshness, `serving_geo.asset_freshness_v`)
```
A baseline source published a new version?         → stale in every partition of every indicator downstream
  (an ingest finished, an observed probe moved)
An override for one scenario written or removed?    → stale in that scenario's partition only
A composite's component went stale?                 → stale too: its version hashes the whole upstream closure
The indicator's own code version or params moved?   → stale everywhere it was materialized
Materialized over only some of its features?        → partial
  (over a whole AOI: partial in the view, but not
   dirty when planning for that same AOI)
Never computed in that partition?                   → absent (dirty in a scenario partition;
                                                       on the baseline only when asked for)
A change no pipeline records (hand-copied mirror,   → invisible to the graph
  direct merge into feature.attributes)?
indicator_result.status = 'stale'?                  → only "superseded by a newer write"; not this question
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
