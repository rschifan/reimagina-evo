# Indicators Layer

How the platform defines, computes, stores, and serves spatial indicators — the analytical outputs shown to users for both the base city and for scenarios.

**Related documents:** `DATA_LAYER.md` — the city data indicators read from. `SCENARIOS_LAYER.md` — scenarios and attribute overrides that drive computation. `JOB_LAYER.md` — the `Orchestrator` port and the runs that produce these results.

---

## What this layer does

An indicator is a computed value for a specific spatial feature in a specific state of the world. This layer stores every computed output — both for the base city as it exists today, and for any scenario.

Three tables do all the work:

| Table | Purpose |
|---|---|
| `indicator_catalog` | Defines what indicators exist — name, unit, model, spatial scope |
| `indicator_dependency` | Defines how composite indicators are built from simpler ones |
| `indicator_result` | Stores computed values, one row per (feature, scenario state, indicator) |

**Key design choice — indicators are rows, not columns.** Adding a new metric means a new `indicator_catalog` row — seeded from an `IndicatorSpec` in `indicators/catalog.py` — and new `indicator_result` rows. No schema migration, no `ALTER TABLE`.

**Base state vs scenario state:**
- `scenario_id = NULL` → result for the base city (Diagnosis mode)
- `scenario_id = :uuid` → result for a specific scenario (Evaluation mode)

Both live in the same table. A scenario stores only the rows it recomputed, so every scenario read is an overlay: the scenario's row where it has one carrying a value, the baseline's otherwise (`storage/_utils.scenario_overlay`).

---

## Tables

### indicator_catalog

Defines every indicator before any value is stored. Every result row references a catalog entry.

```sql
CREATE TABLE curated_geo.indicator_catalog (
  id                 uuid             PRIMARY KEY DEFAULT gen_random_uuid(),

  slug               text             NOT NULL UNIQUE,
  -- Stable code-level identifier used in queries and the API.
  -- e.g. 'urban_form_block_compactness', 'edificability_block_density',
  --      'accessibility_block_school_network_m', 'livability_block_score'

  name               text             NOT NULL,
  description        text,

  model_family       text,
  -- Which analytical family this indicator belongs to. A grouping label carried
  -- onto job_run, not a dispatch key — the runner registry binds compute
  -- functions per slug, not per family (`runners._DIRECT_COMPUTE`, `runners.default_runners`).
  -- Shipped direct indicators use 'urban_form' or 'accessibility' (`indicators/catalog.py`).
  -- NULL for composite indicators (derived from other indicators, not from a model run);
  -- their job_run rows carry 'composite'.

  computation_kind   text             NOT NULL
                                      CHECK (computation_kind IN ('direct','composite')),
  -- direct:    produced by a compute fn reading city data
  -- composite: weighted combination of other indicators

  value_type         text             NOT NULL
                                      CHECK (value_type IN ('numeric','text','json')),
  -- numeric:  a single float  (every shipped indicator)
  -- text:     a category or label
  -- json:     a distribution or breakdown object

  unit               text,
  -- Shipped: 'ratio_0_1', 'buildings_per_ha', 'floors', 'score_0_1', 'units',
  --          'people', 'units_per_ha', 'm'

  valid_min          double precision,
  valid_max          double precision,
  -- Expected numeric range. The write trigger rejects values outside it.
  -- Only meaningful for value_type = 'numeric'. Must be NULL for all other types.

  cap_value          double precision,
  -- Added by r051. The value an indicator is clamped to, so a reading at the cap
  -- means "at least this", not a measurement (the accessibility distances at
  -- 1500 m). NULL for every indicator that is not clamped. Not a validation range.

  aggregation_method text             NOT NULL
                                      CHECK (aggregation_method IN
                                        ('mean','weighted_mean','sum','min','max','none')),
  -- How to combine this indicator's values over a coarser area (the AOI summary).
  -- 'none' for indicators that are not aggregable.

  applies_to         text[]           NOT NULL,
  -- Which type_slugs this indicator is computed for.
  -- ARRAY['block'] for every shipped indicator.

  created_at         timestamptz      NOT NULL DEFAULT now(),

  CHECK (cardinality(applies_to) > 0),
  CHECK (valid_min IS NULL OR valid_max IS NULL OR valid_min <= valid_max),
  CHECK (value_type = 'numeric' OR (valid_min IS NULL AND valid_max IS NULL)),
  CHECK (
    (computation_kind = 'direct'    AND model_family IS NOT NULL) OR
    (computation_kind = 'composite' AND model_family IS NULL)
  )
);
```

**Shipped rows.** The catalog is seeded from code, not from SQL: `SHIPPED_INDICATORS` in `indicators/catalog.py` holds one `IndicatorSpec` per indicator, and `seed_catalog` upserts each one (`upsert_indicator`, `ON CONFLICT (slug) DO UPDATE`) and rewrites a composite's `indicator_dependency` rows (`set_dependencies`). `indicators seed-catalog` and `bootstrap-db` both run it. Every shipped row is `value_type='numeric'`, `aggregation_method='weighted_mean'`, `applies_to={block}`; in `SHIPPED_INDICATORS` order:

| slug | kind | model_family | unit | valid_min–valid_max | cap_value | higher_is_better | reads / depends_on (weight) |
|---|---|---|---|---|---|---|---|
| `urban_form_block_compactness` | direct | urban_form | ratio_0_1 | 0–1 | — | true | `geometry` |
| `urban_form_block_building_density` | direct | urban_form | buildings_per_ha | 0–500 | — | true | `geometry` |
| `urban_form_block_planned_floors` | direct | urban_form | floors | 0–200 | — | true | `floors` |
| `urban_form_block_composite` | composite | NULL | score_0_1 | 0–1 | — | true | compactness 0.5, building_density 0.5 |
| `edificability_block_housing_units` | direct | urban_form | units | 0–200,000 | — | true | `num_levels`, `unit_size`, `people_per_household`, `lot_coverage` |
| `edificability_block_population` | direct | urban_form | people | 0–750,000 | — | true | same four |
| `edificability_block_density` | direct | urban_form | units_per_ha | 0–2,000 | — | true | same four |
| `accessibility_block_school_network_m` | direct | accessibility | m | 0–1,500 | 1,500 | false | `poi_version`, `network_version` |
| `accessibility_block_health_network_m` | direct | accessibility | m | 0–1,500 | 1,500 | false | same two |
| `accessibility_block_park_network_m` | direct | accessibility | m | 0–1,500 | 1,500 | false | same two |
| `accessibility_block_culture_network_m` | direct | accessibility | m | 0–1,500 | 1,500 | false | same two |
| `livability_block_score` | composite | NULL | score_0_1 | 0–1 | — | true | `edificability_block_density` 0.4, scored against a 50–150 units/ha target band; school, health and park `*_network_m` 0.2 each |

What each computes:

- **Compactness** — Polsby-Popper, `4π × area / perimeter²` over the block's geodesic area and perimeter, clamped to [0, 1].
- **Building density** — `global:building` features whose centroid falls inside the block, divided by block area in hectares.
- **Planned floors** — the block's effective `floors` (scenario override, then observation, then base attributes).
- **Edificability** — `housing_units = area × lot_coverage × num_levels / unit_size`, `population = housing_units × people_per_household`, `density = housing_units / area_ha`. Block geodesic area stands in for lot area. `lot_coverage` is optional and defaults to 1.0, so `num_levels × lot_coverage` is the CUS term; a block missing `num_levels`, `unit_size` or `people_per_household` is skipped and keeps whatever it already held (`indicators/edificability.py`). On a pilot install the inputs are fitted from the Overture buildings and the census so the baseline gives the census dwellings and population back (`scripts/seed_demo_attributes.py --fit-to-census`; see [SCENARIOS_LAYER.md](SCENARIOS_LAYER.md)); a block with no census dwellings carries no `unit_size` and is skipped.
- **Accessibility** — metres from the block's point-on-surface centroid to the nearest amenity of the category along the OSM pedestrian network (one multi-source `pgr_drivingDistance` per category), plus the block's and the amenity's walks to the network, clamped to [0, 1,500 m]; nothing reachable reports the 1,500 m cutoff.
- **Composites** — see *Each input is rescaled before weighting* below.

`IndicatorSpec` carries four fields that are not catalog columns: `inputs` (typed `AssetEdge`s — datasets, the attributes read off them, and the indicators combined), `reads` and `depends_on` (both derived from `inputs`; a different value declared alongside it raises `ValueError`), and `higher_is_better`. Besides the attributes above, building density declares a dataset edge to `global.building`, and each accessibility indicator edges to `global.poi` and `global.road_network` within 1,500 m.

---

### indicator_dependency

Defines how composite indicators are built from simpler ones. Forms a directed acyclic graph (DAG).

```sql
CREATE TABLE curated_geo.indicator_dependency (
  indicator_id   uuid             NOT NULL REFERENCES curated_geo.indicator_catalog(id) ON DELETE CASCADE,
  -- The composite indicator.

  depends_on_id  uuid             NOT NULL REFERENCES curated_geo.indicator_catalog(id) ON DELETE RESTRICT,
  -- The base indicator it is built from.

  weight         double precision,
  -- For weighted composites. If any dependency for a given indicator_id has a
  -- weight, all must have weights, and they must sum to 1.0.
  -- The schema allows NULL weights, but `compute_composite` counts a NULL
  -- weight as 0; every shipped composite declares weights.

  PRIMARY KEY (indicator_id, depends_on_id),
  CHECK (indicator_id <> depends_on_id),
  CHECK (weight IS NULL OR (weight >= 0 AND weight <= 1))
);
```

**Example — `urban_form_block_composite` = 0.5 × compactness + 0.5 × building density** (each normalised first, see below). `seed_catalog` writes these rows through `set_dependencies`, which deletes the composite's existing rows and inserts one per input:
```sql
INSERT INTO curated_geo.indicator_dependency (indicator_id, depends_on_id, weight)
VALUES
  (:composite_id, :compactness_id,      0.5),
  (:composite_id, :building_density_id, 0.5);
```

Enforced by the constraint trigger `indicator_dependency_validate_trigger` (r016), which runs `curated_geo.indicator_dependency_validate()` `AFTER INSERT OR UPDATE OR DELETE`, `DEFERRABLE INITIALLY DEFERRED`, and checks the whole table:
- every `indicator_id` references a composite indicator (`computation_kind = 'composite'`)
- no cycles exist in the dependency graph (recursive CTE reachability check, depth-capped at 50)
- for each `indicator_id`, weights are either all NULL or all non-null, and non-null weights sum to 1.0 (within 1e-9 tolerance)

Deferred execution is required because acyclicity and weight-sum can only be verified after all dependency rows for a given indicator have been inserted in the same transaction — which is why `seed_catalog` must not run on an autocommit connection.

---

### indicator_result

One row per computed result. Long format — new indicators produce new rows, not new columns.

```sql
CREATE TABLE curated_geo.indicator_result (
  id              uuid             PRIMARY KEY DEFAULT gen_random_uuid(),

  -- What spatial unit
  feature_id      uuid             NOT NULL REFERENCES curated_geo.feature(id) ON DELETE CASCADE,
  feature_type    text             NOT NULL,
  -- Copied from feature.type_slug at insert time. Immutable after insert.

  -- Which state of the world
  scenario_id     uuid             REFERENCES app_core.scenario(id)
                                   ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
  -- r016 left it a bare uuid; r055 added the FK (indicator_result_scenario_id_fkey).
  -- Deleting a scenario deletes its results. A compute unit defers the check
  -- (runners._fenced) so its inserts do not lock the scenario row.
  -- NULL  = base city state (Diagnosis mode — the city as it exists today)
  -- UUID  = a specific scenario (Evaluation mode — a proposed future state)

  -- Which indicator
  indicator_id    uuid             NOT NULL REFERENCES curated_geo.indicator_catalog(id),
  indicator_slug  text             NOT NULL,
  -- Copied from indicator_catalog.slug at insert time. Immutable after insert.

  -- Which job produced this
  job_run_id      uuid             NOT NULL REFERENCES jobs_runtime.job_run(id) ON DELETE CASCADE,
  model_version   text,
  -- Denormalized from job_run.code_version at insert time. Set by the write trigger if NULL.
  -- Kept on the result row so reads don't require joining job_run.

  -- The value — at most one column populated per row (exactly one for a
  -- measured/estimated/imputed row that has not failed; see the trigger)
  value_numeric   double precision,
  value_text      text,
  value_json      jsonb,
  -- Every shipped indicator writes value_numeric only. value_json is meant for
  -- e.g. a distribution: {"0_14": 0.23, "15_64": 0.65, "65_plus": 0.12}

  -- Data quality (ISO 19157 style)
  quality_flag    text             NOT NULL DEFAULT 'measured'
                                   CHECK (quality_flag IN
                                     ('measured','estimated','imputed','missing','not_applicable')),
  -- measured:       directly computed from authoritative source data
  -- estimated:      derived at a different resolution or scale
  -- imputed:        filled using a spatial or statistical model
  -- missing:        input data was unavailable; see missing_reason
  -- not_applicable: this indicator does not apply to this feature type

  confidence      double precision CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
  -- For indicators derived from probabilistic models or ML outputs: a
  -- per-feature uncertainty estimate (e.g. 0.87 = 87% confidence).
  -- Every shipped indicator is deterministic and leaves it NULL.
  missing_reason  text,
  -- Required when quality_flag = 'missing'. Human-readable explanation.

  -- Lifecycle
  status          text             NOT NULL DEFAULT 'current'
                                   CHECK (status IN ('current','stale','failed')),
  -- current: latest result for this (feature, scenario, indicator) triple
  -- stale:   superseded. The write path stales the previous current row as it
  --          inserts the new one; scenario authoring stales a scenario's rows
  --          when the overrides behind them are removed (deleting the scenario
  --          deletes its rows). Nothing re-queues a stale row — what is out of
  --          date is answered by the asset graph (ASSET_LAYER.md).
  -- failed:  allowed by the CHECK, written by nothing. A failed computation is
  --          recorded on job_run (status 'failed', error_message).
  -- Execution state ('queued', 'running') lives on job_run, not here.

  computed_at     timestamptz      NOT NULL DEFAULT now(),
  updated_at      timestamptz      NOT NULL DEFAULT now()
);
```

**Uniqueness — only one current result per (feature, scenario, indicator):**

Two indexes are required because `scenario_id` can be NULL and PostgreSQL treats each NULL as distinct in a unique index, which would allow duplicates.

```sql
-- Base state
CREATE UNIQUE INDEX ir_one_current_base
  ON curated_geo.indicator_result (feature_id, indicator_id)
  WHERE scenario_id IS NULL AND status = 'current';

-- Scenario state
CREATE UNIQUE INDEX ir_one_current_scenario
  ON curated_geo.indicator_result (feature_id, scenario_id, indicator_id)
  WHERE scenario_id IS NOT NULL AND status = 'current';
```

**Read path indexes:**
```sql
CREATE INDEX ir_feature_scenario_idx
  ON curated_geo.indicator_result (feature_id, scenario_id, indicator_id)
  WHERE status = 'current';

CREATE INDEX ir_scenario_slug_idx
  ON curated_geo.indicator_result (scenario_id, indicator_slug)
  WHERE status = 'current';

CREATE INDEX ir_base_slug_idx
  ON curated_geo.indicator_result (indicator_slug, feature_id)
  WHERE scenario_id IS NULL AND status = 'current';

CREATE INDEX ir_stale_idx
  ON curated_geo.indicator_result (status)
  WHERE status = 'stale';

CREATE INDEX ir_job_run_idx
  ON curated_geo.indicator_result (job_run_id);

-- r047: plain, non-partial. Every index above on feature_id is partial, so none
-- can serve the feature_id foreign key, and deleting a feature fell back to a
-- sequential scan of this table.
CREATE INDEX indicator_result_feature_id_idx
  ON curated_geo.indicator_result (feature_id);
```

**Trigger — `curated_geo.indicator_result_before_write()`**, run by `indicator_result_before_write_trigger` `BEFORE INSERT OR UPDATE` (r016):
- On insert, set `feature_type` from `feature.type_slug`; on update, reject a change to it.
- Set `indicator_slug` from `indicator_catalog.slug` (same rule).
- On insert, set `model_version` from `job_run.code_version` if `model_version IS NULL`.
- Reject if `job_run.scenario_id` does not match `indicator_result.scenario_id`.
- Reject if `feature.type_slug` is not in `indicator_catalog.applies_to`, unless `quality_flag = 'not_applicable'`.
- **`not_applicable` rows:** no shipped compute writes them. A feature with no row is simply absent from every read — "not yet computed" and "not applicable" look the same; the API serves `applies_to` with each catalog row.
- Require exactly one value column populated when `quality_flag IN ('measured','estimated','imputed')` and `status != 'failed'`.
- Require all value columns NULL when `quality_flag IN ('missing','not_applicable')` or `status = 'failed'`.
- Require `missing_reason` when `quality_flag = 'missing'`.
- Reject `value_numeric` outside `[valid_min, valid_max]` when those bounds are defined.
- On update, set `updated_at = now()`.

---

## Serving layer

```sql
CREATE VIEW serving_geo.current_indicator_v AS
SELECT
  ir.id,
  ir.feature_id,
  ir.feature_type,
  ir.scenario_id,
  ir.indicator_id,
  ir.indicator_slug,
  ic.name               AS indicator_name,
  ic.unit,
  ic.value_type,
  ic.computation_kind,
  ir.job_run_id,
  ir.model_version,
  ir.value_numeric,
  ir.value_text,
  ir.value_json,
  ir.quality_flag,
  ir.confidence,
  ir.missing_reason,
  ir.computed_at
FROM curated_geo.indicator_result ir
JOIN curated_geo.indicator_catalog ic ON ic.id = ir.indicator_id
WHERE ir.status = 'current';
```

---

## Use cases

### Adding a new direct indicator

Add an `IndicatorSpec` to `SHIPPED_INDICATORS` in `indicators/catalog.py`, bind a
compute fn for its slug in `_DIRECT_COMPUTE` (`orchestration/runners.py`) —
usually built with `compute_indicator`, see *How a direct indicator is wired* —
then run `indicators seed-catalog` and `assets seed` (`bootstrap-db` runs both).
No migration. `default_runners` binds a direct spec only when `_DIRECT_COMPUTE`
has an entry for it, so a catalog row alone computes nothing.

A shipped spec, abridged (description omitted):

```python
URBAN_FORM_BLOCK_COMPACTNESS = IndicatorSpec(
    slug="urban_form_block_compactness",
    name="Block Compactness (Polsby-Popper)",
    computation_kind="direct",
    model_family="urban_form",
    value_type="numeric",
    unit="ratio_0_1",
    valid_min=0.0,
    valid_max=1.0,
    aggregation_method="weighted_mean",
    inputs=(AssetEdge("mex.block", attributes=("geometry",)),),
    applies_to=("block",),
)
```

---

### Adding a new composite indicator

A composite is an `IndicatorSpec` with `computation_kind="composite"`, no
`model_family` (r016's CHECK), and one `relation="derived_from"` edge per input,
weights summing to 1.0. Every input must declare `valid_min` and `valid_max`.
New composites go at the **end** of `SHIPPED_INDICATORS`: dependencies must be
upserted first, and `tests/unit/test_failed_job_run_provenance.py` exercises the
first spec with `model_family is None`.

```python
LIVABILITY_BLOCK_SCORE = IndicatorSpec(        # abridged
    slug="livability_block_score",
    name="Livability Score",
    computation_kind="composite",
    value_type="numeric",
    unit="score_0_1",
    valid_min=0.0,
    valid_max=1.0,
    aggregation_method="weighted_mean",
    inputs=(
        AssetEdge(indicator_asset_key(EDIFICABILITY_BLOCK_DENSITY.slug),
                  relation="derived_from", weight=0.4,
                  target_band=(LIVABILITY_DENSITY_BAND_LOW,     # 50
                               LIVABILITY_DENSITY_BAND_HIGH)),  # 150
        # ...one 0.2 edge per category in LIVABILITY_ACCESSIBILITY_CATEGORIES
        # ("school", "health", "park")
    ),
    applies_to=("block",),
)
```

No new model is required. `default_runners` binds every
`computation_kind="composite"` spec to the generic `compute_composite`
(`orchestration/runners.py`), so a new composite needs only a catalog
entry, not new compute code. `livability_block_score` is the worked example:
0.4 housing density (`edificability_block_density`) plus 0.2 each for walking
distance to schools, health facilities and parks, added as a spec and nothing
else. `culture` is deliberately not an input.

#### Each input is rescaled before weighting

⚠ **Raw summing makes the weights decorative.** `urban_form_block_composite`
declared 0.5/0.5 and behaved 1.25 / 98.75 — mean contributions of 0.265 from
compactness (`ratio_0_1`, range 0–1) against 20.946 from building density
(`buildings_per_ha`, range 0–500). Not a weighting bug; a units bug. Metres and
dwellings-per-hectare do not add.

`compute_composite` rescales each input to 0–1 against its **declared**
`valid_min`/`valid_max` and clamps it to 0–1 before weighting, so no input can
exceed its weight and the result cannot leave 0–1. An input that declares no
bounds, or `valid_max <= valid_min`, makes it refuse with `ValueError` rather
than fall back to the raw value.

**A target band, for an input that is good in a range.** An edge may carry
`target_band=(low, high)`; that input then scores 1 inside the band and falls
linearly to 0 at its `valid_min` below and at `2 × high` above, clamped to 0–1,
instead of being normalised and oriented (`composite.component_score`).
Livability's density edge uses it with `LIVABILITY_DENSITY_BAND_LOW` /
`_HIGH` = 50 / 150 units/ha (`indicators/catalog.py`): density scores 0 at 0,
0.5 at 25, 1 from 50 to 150, 0.5 at 225 and 0 from 300 up. Normalised against
its 2,000 units/ha safety cap, the score rose with density all the way, so the
densest block was the most livable. The bands are folded into the indicator's
`params_digest`, so moving one restages its results.

Against the *declared* bounds, not the observed population, on purpose:
percentile or AOI min–max would make every value depend on every other value, so
editing one block would invalidate all of them and the dirty set could no longer
be a subset of features. That is a different scheduling shape.

⚠ **This makes the maths sound; it does not make a score balanced.**
`valid_max` is now doing two jobs — the validation ceiling r016's trigger
enforces on writes, and the reference a score divides by. `building_density`
declares 500 as a safety cap against an observed maximum of 212.1 on CDMX
(153.8 on the Mexicali pilot), so its CDMX values occupy the bottom 42% of their range and the same 0.5/0.5 composite reads 86%
compactness. Separating the two roles is the fix when a real score needs a
defensible balance.

#### Direction: `higher_is_better`

⚠ `indicator_dependency` enforces `weight >= 0` (r016:93), so a lower-is-better
input **cannot** be folded in with a negative weight. Four shipped indicators are
distances in metres (the `accessibility_block_*_network_m` family); a score
adding one raw ranks "far from a school" as good — silently, because the
arithmetic stays sound and only the meaning inverts. Measured on two blocks
differing only in distance, in a 0.5/0.5 compactness + school-distance composite
without orientation: near (100 m) scored 0.283, far (1400 m) scored 0.717
(`tests/integration/test_composite_pipeline.py` pins the oriented result).

Each spec declares `higher_is_better`; `compute_composite` flips a `False` input
to `1 - normalised`, **after clamping and before weighting**. The accessibility
family reports exactly its `valid_max` (1,500 m) for an unreachable block, which
normalises to 1 and contributes 0.

The field is Python-side rather than a catalog column: its only consumer is the
composite, which already reads these specs for bounds. Unlike `reads`, it cannot
drift from the compute — it states what the number *means*, not what the code
touches. A distance is lower-is-better whatever the implementation does.

---

### Writing results from a model job

Every shipped compute writes through
`IndicatorResultRepository.bulk_write_numeric_results`, inside its `job_run`:

1. Marks the previous current results stale — one `UPDATE`, only for the feature ids being written.
2. Inserts the new current results (`execute_batch`, 500 rows a page).

```sql
-- Step 1: mark previous results stale
UPDATE curated_geo.indicator_result
SET status = 'stale', updated_at = now()
WHERE feature_id = ANY(:feature_ids::uuid[])
  AND scenario_id IS NULL            -- or = :scenario_id
  AND indicator_id = :compactness_id
  AND status = 'current';

-- Step 2: write new results
-- The repository supplies feature_id, scenario_id, indicator_id, job_run_id and
-- the value, with '' placeholders for feature_type and indicator_slug: the
-- before-write trigger sets those, and model_version from job_run.code_version.
INSERT INTO curated_geo.indicator_result
  (feature_id, feature_type, scenario_id, indicator_id, indicator_slug,
   job_run_id, value_numeric, quality_flag, status)
VALUES
  (:block_id, '', NULL, :compactness_id, '',
   :job_run_id, 0.73, 'measured', 'current');
```

Because only written features are staled, a feature a compute skips keeps its
previous value — which `indicators/edificability.py` relies on.

`bulk_write_numeric_results` never sets `confidence`. The single-row
`write_result` can set it, and can write a valueless row such as the one below,
which the trigger accepts; no shipped compute calls it:
```sql
INSERT INTO curated_geo.indicator_result
  (feature_id, feature_type, scenario_id, indicator_id, indicator_slug,
   job_run_id, quality_flag, missing_reason, status)
VALUES
  (:block_id, '', NULL, :school_network_id, '',
   :job_run_id, 'missing', 'No road network data within 1500 m', 'current');
```

A missing *layer* is handled by refusing the whole compute instead — see *An
indicator refuses to compute against a missing input*.

---

### Computing a composite indicator result

`compute_composite` (`indicators/composite.py`) follows the same stale-then-insert write, but reads component results instead of city data, and combines them in Python rather than SQL. It is an ordinary unit in the same run as its components, ordered after them by the impact engine's topological sort — not a separate worker or a second run (see `JOB_LAYER.md` — *Impact engine*).

1. For each `(input, weight)` in `spec.depends_on`, read the input's current results with `list_current_for_indicator` — under a scenario, an overlay of the scenario's rows on the baseline's. Keep rows with a `value_numeric` at a feature type in the composite's `applies_to`.
2. Normalise each value against the input's declared bounds, clamp it to 0–1, and flip it when the input is `higher_is_better=False` — or, for an edge with a `target_band`, score it against the band.
3. Keep only features every input has a value for, narrowed to the unit's `feature_ids` when it is feature-scoped and to the AOI's blocks (`feature_ids_in_aoi`) when it carries an `aoi_id`, and sum the weighted values.
4. Open a `job_run` (`model_family='composite'`) and write with `bulk_write_numeric_results`.

```
composite(f) = Σᵢ wᵢ · orientᵢ( clamp₀₁( (xᵢ(f) − valid_minᵢ) / (valid_maxᵢ − valid_minᵢ) ) )
orientᵢ(v)   = v if higher_is_betterᵢ else 1 − v          (a NULL wᵢ counts as 0)

urban_form_block_composite = 0.5·n(compactness) + 0.5·n(building_density)
livability_block_score     = 0.4·band(edificability_block_density)
                           + 0.2·(1 − n(school)) + 0.2·(1 − n(health)) + 0.2·(1 − n(park))
band(d)      = clamp₀₁( d < 50 ? d / 50 : d > 150 ? (300 − d) / 150 : 1 )
```

A feature missing any component gets no composite row in that run — not a `missing` row — and keeps whatever composite row it already had. No partial composite is produced.

---

### Reading all current indicators for a feature

```sql
SELECT
  indicator_slug,
  indicator_name,
  unit,
  value_numeric,
  value_text,
  value_json,
  quality_flag,
  computed_at
FROM serving_geo.current_indicator_v
WHERE feature_id  = :feature_id
  AND scenario_id IS NULL
ORDER BY indicator_slug;
```

---

### Comparing base city vs scenario for one indicator

```sql
SELECT
  f.id,
  f.name,
  base.value_numeric                          AS base_value,
  scen.value_numeric                          AS scenario_value,
  scen.value_numeric - base.value_numeric     AS delta
FROM serving_geo.feature_v f
JOIN serving_geo.current_indicator_v base
  ON base.feature_id    = f.id
 AND base.indicator_slug = 'edificability_block_housing_units'
 AND base.scenario_id   IS NULL
JOIN serving_geo.current_indicator_v scen
  ON scen.feature_id    = f.id
 AND scen.indicator_slug = 'edificability_block_housing_units'
 AND scen.scenario_id   = :scenario_id
WHERE f.type_slug = 'block'
  AND ST_Intersects(f.geometry, :aoi_geometry);
```

A scenario holds rows only for the features it recomputed, so this inner join returns exactly those; every other block reads through to its baseline value under the scenario.

---

### Getting all indicators for an AOI — current base state

```sql
SELECT
  f.id,
  f.name,
  af.overlap_ratio,
  ci.indicator_slug,
  ci.indicator_name,
  ci.unit,
  ci.value_numeric,
  ci.quality_flag
FROM serving_geo.analysis_units_v f
JOIN curated_geo.aoi_feature af          ON af.feature_id = f.id
                                        AND af.aoi_id     = :aoi_id
                                        AND af.overlap_basis = 'area'
JOIN serving_geo.current_indicator_v ci  ON ci.feature_id = f.id
                                        AND ci.scenario_id IS NULL
WHERE f.type_slug = 'block'
ORDER BY f.id, ci.indicator_slug;
```

---

### Marking indicators stale after a scenario edit

When a scenario override changes features, all affected indicator results — including composite indicators that depend on them — can be marked stale before recomputation.

This is a diagnostic/bulk-invalidation query, not a required step in the run path: the write path stales the previous `current` row for the same `(feature, scenario, indicator)` as it inserts the new one, so a recompute needs no separate stale pass. The two places that do stale rows outside a write are in `scenarios/authoring.py`: materialising a scenario stales its rows for each `(indicator, feature)` whose override was removed, the indicators that read the attribute and their dependents such as the livability composite (`_supersede_unbacked_results`). Deleting a scenario does not stale its rows: `delete_scenario` deletes them (the `ON DELETE CASCADE` foreign key r055 added).

```sql
WITH RECURSIVE impacted AS (
  -- Direct indicators affected by the changed features
  SELECT DISTINCT ir.indicator_id
  FROM curated_geo.indicator_result ir
  WHERE ir.feature_id  = ANY(:affected_feature_ids)
    AND ir.scenario_id = :scenario_id
    AND ir.status      = 'current'

  UNION

  -- Composite indicators that depend on the above (recursive)
  SELECT d.indicator_id
  FROM curated_geo.indicator_dependency d
  JOIN impacted ON d.depends_on_id = impacted.indicator_id
)
UPDATE curated_geo.indicator_result
SET    status     = 'stale',
       updated_at = now()
WHERE  feature_id  = ANY(:affected_feature_ids)
  AND  scenario_id = :scenario_id
  AND  status      = 'current'
  AND  indicator_id IN (SELECT indicator_id FROM impacted);
```

Recomputation is triggered by submitting a run for the scenario, not by queueing one job per model family: the dirty-set is derived per indicator from the changed attributes and the dependency graph (see `JOB_LAYER.md` — *The submit prelude*). `POST /scenarios/{scenario_id}/recompute` submits the scenario's footprint — every block a current rule overrides or whose override was just removed, with those attributes — so `compute_dirty_set` seeds the indicators whose `reads` include an attribute and scopes each unit, composites included, to those features. A submit that names no attribute and no feature is planned from asset freshness instead (`ASSET_LAYER.md`).

### How a direct indicator is wired

Every direct indicator shares one envelope: resolve the backend, check the
catalog is seeded, open a `job_run`, read rows, run them through Hamilton,
bulk-write, return a `ComputeResult`. Only three things vary — the read, the
Hamilton node, and that node's input key — so the envelope lives once, in
`indicators/_compute.py`:

```python
compute_block_compactness = compute_indicator(
    URBAN_FORM_BLOCK_COMPACTNESS,
    reader=_compactness_rows,
    node="compactness_result_frame",
    input_key="geodesic_metrics",
)
```

`reader` is typed by a `Reader` Protocol rather than a bare `Callable`, because
the readers take keyword-only arguments that `Callable` cannot express; mypy
rejects a mis-shaped one with the expected signature named.

⚠ **The read happens inside the `job_run` context, not before it.**
`indicator_result.job_run_id` is a NOT NULL FK (r016), so a read that raises
outside the context leaves a failure with no provenance row anywhere.
`compute_indicator`, `building_density` and `planned_floors` (its
`effective_numeric` call) all read inside it.

⚠ **Two indicators deliberately keep their own body.** `planned_floors` has no
Hamilton stage, and `building_density` reports an extra statistic
(`buildings_attributed`) on a `ComputeResult` subclass. Adding hooks so the
factory could swallow them would grow shared code to serve one caller each.
They still use the shared `require_seeded` guard — which `planned_floors`
lacked while five hand-written copies of it existed, so an unseeded database
failed there with a foreign-key error instead of the actionable message.

### An indicator refuses to compute against a missing input

An indicator that joins another feature type cannot distinguish "no buildings on
this block" from "no buildings were ever loaded". Both are a count of zero, and
zero is a perfectly plausible measurement — so it flows into composites as if it
had been measured.

That is not hypothetical. With the building layer absent,
`urban_form_block_building_density` wrote `0.0` for all 5,273 Tlalpan blocks and
`urban_form_block_composite` came out as **exactly `0.5 × compactness` on all
68,767 rows compared**. Every layer reported success.

Two guards in `indicators/_compute.py`, both raising `MissingInputData`:

- **`require_nonempty`** — refuses when the input layer is empty. Callers own
  the existence query; the helper owns the message, so the "what is missing /
  what loads it" pair cannot drift between call sites. Used for the
  `global:building` layer (building density), the establishment layer and
  `curated_geo.road_edge` (accessibility).
- **`require_measured`** — refuses when the layer is non-empty but contributed
  nothing *in scope*: loaded for a different AOI, loaded partially, or never
  linked by `aoi compute-features`. Free, because `building_density` already
  computed `buildings_attributed` and then discarded it.

`NULL` means unknown and `0` means a confirmed none; they are not
interchangeable. Where the difference cannot be represented downstream, failing
is the only honest option — Delta Live Tables' `ON VIOLATION FAIL UPDATE` rather
than `DROP ROW`.

**Accessibility guards the layer, deliberately not the category.** With no
establishments at all, every block reports the 1500 m cutoff and renders as a
real, uniformly bad accessibility surface — harder to spot than a zero. But a
category genuinely absent while the layer is loaded is a different question this
module already answered: the cutoff is the wanted value, pinned by
`test_no_amenity_of_that_category_reports_the_cutoff`, which seeds a park and
asks for a school. Guarding per category would overturn that silently. What it
does instead, when a category matches no establishment at all, is log a warning
naming the categories present in the layer (`_warn_if_category_absent`); it
changes no value and refuses nothing.

⚠ **Ceiling, accepted deliberately.** A scope that genuinely contains none of
the joined type trips `require_measured`. A loud false positive an operator
clears in one query beats a silent zero reaching a composite. Relax only with a
real such AOI in hand.

⚠ **What `reads` does *not* tell you.** `reads` is an invalidation
vocabulary, not a data manifest. `urban_form_block_building_density` has
`reads=("geometry",)` while its compute joins `global:building`; the
accessibility family reads `poi_version` / `network_version`, which are
hand-maintained sentinels standing in for whole tables. Walk `reads` through
`depends_on` for `urban_form_block_composite` and the answer is `['geometry']`
— it needs every `global:building` feature. `depends_on` is exact and
machine-walkable; the attribute leaves are under-declared by exactly that gap.

The datasets are declared in `inputs`, the field `reads` and `depends_on` are
projected from: building density has an `AssetEdge("global.building",
relation="child_in_parent")`, and the accessibility family has `within_radius`
edges to `global.poi` and `global.road_network`. Those edges become the asset graph, so
`assets inputs indicator.urban_form_block_composite` answers with
`global.building` (`ASSET_LAYER.md`). `inputs` is still hand-typed: nothing
checks it against the queries a compute runs — a contract that is not
executable can drift. What it removes is the second statement: the attribute
and dataset dependencies are one declaration, not two that can disagree.

### Multi-grain rollup — removed

Every shipped indicator is `applies_to=("block",)`. The block→AGEB rollup that
produced coarser-grain `indicator_result` rows is gone: `jobs/rollup.py`, the
`indicators rollup` CLI command, and `rollup_child_rows` on the overlap
repository. r016's write trigger rejects any result written at a grain the
indicator does not declare — *"feature type ageb is not in indicator
applies_to ({block})"* — and `compute_composite` reads only its inputs' rows at
its own `applies_to` grain.

What remains, and is unaffected:

- **`curated_geo.feature_overlap`** and its type pairs. Still populated, still
  read by the *observation* rollup (`rollup_observation_child_rows`), which is a
  different mechanism from the indicator one. AOI summaries weight by
  `aoi_feature.overlap_measure`, not by `feature_overlap`.
- **`jobs/aggregation.py`** — `aggregate_children`, the weighted-mean and
  missing-data policy `jobs/aoi_summary.py` uses, and `aggregate`, which the
  observation rollup uses.
- **`aggregation_method`**, declared on `IndicatorSpec` and seeded into
  `indicator_catalog`, which the AOI summary reads from the catalog row to
  decide how children combine.
- **The tile's `feature_type` param** (r018). Nothing in the shipped catalog can
  now produce the two-grain overlap it was added to prevent, but the client
  still sends it and it still filters.

⚠ Restoring it is not just re-adding a grain to `applies_to`: the trigger would
then accept AGEB rows with nothing writing them, which is the shape that lets an
indicator look computed at a grain it never covered. Bring back the worker and
its test in the same change.

## Build order

Within this layer (after DATA_LAYER and JOB_LAYER tables exist):

1. `curated_geo.indicator_catalog` — seed with known indicators (`seed_catalog`); `cap_value` added by r051
2. `curated_geo.indicator_dependency` — define composite structures
3. `curated_geo.indicator_result` — with all indexes and trigger; `indicator_result_feature_id_idx` added by r047
4. `serving_geo.current_indicator_v`

Items 1–4 are all created by r016.

See `JOB_LAYER.md` — *Migration order* — for how these interleave with the job-layer tables. `indicator_result.job_run_id` is `NOT NULL` with an FK, so `jobs_runtime.job_run` (r016) must exist first.

---

## Standards referenced

| Decision | Standard |
|---|---|
| Long-format indicator storage | Fact-table pattern (UrbanSim, ActivitySim, BI semantic layers) |
| Indicator catalog with dependency graph | UN SDG indicator framework; BI semantic-layer pattern |
| `computation_kind` discriminator | Direct vs. derived metric separation |
| Quality flags | ISO 19157 — Geographic information data quality |
| Two partial unique indexes for NULL-safe uniqueness | PostgreSQL NULL handling in unique indexes |
| Composite dependency as DAG | Standard graph modeling; enforced acyclicity |
