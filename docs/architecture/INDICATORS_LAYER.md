# Indicators Layer

How the platform defines, computes, stores, and serves spatial indicators — the analytical outputs shown to users for both the base city and for scenarios.

**Related documents:** `DATA_LAYER.md` — the city data indicators read from. `SCENARIOS_LAYER.md` — scenarios and interventions that drive computation. `JOB_LAYER.md` — the async execution model that produces these results.

---

## What this layer does

An indicator is a computed value for a specific spatial feature in a specific state of the world. This layer stores every computed output — both for the base city as it exists today, and for any scenario.

Three tables do all the work:

| Table | Purpose |
|---|---|
| `indicator_catalog` | Defines what indicators exist — name, unit, model, spatial scope |
| `indicator_dependency` | Defines how composite indicators are built from simpler ones |
| `indicator_result` | Stores computed values, one row per (feature, scenario state, indicator) |

**Key design choice — indicators are rows, not columns.** Adding a new metric means inserting a row into `indicator_catalog` and writing new `indicator_result` rows. No schema migration, no `ALTER TABLE`.

**Base state vs scenario state:**
- `scenario_id = NULL` → result for the base city (Diagnosis mode)
- `scenario_id = :uuid` → result for a specific scenario (Evaluation mode)

Both live in the same table.

---

## Tables

### indicator_catalog

Defines every indicator before any value is stored. Every result row references a catalog entry.

```sql
CREATE TABLE curated_geo.indicator_catalog (
  id                 uuid             PRIMARY KEY DEFAULT gen_random_uuid(),

  slug               text             NOT NULL UNIQUE,
  -- Stable code-level identifier used in queries and the API.
  -- e.g. 'accessibility_walkability', 'green_coverage', 'edificability_far', 'liveability_index'

  name               text             NOT NULL,
  description        text,

  model_family       text,
  -- Which async worker computes this indicator.
  -- 'accessibility', 'housing', 'green_area', 'fiscal', 'urban_form'
  -- NULL for composite indicators (derived from other indicators, not from a model run).

  computation_kind   text             NOT NULL
                                      CHECK (computation_kind IN ('direct','composite')),
  -- direct:    produced by a model worker reading city data
  -- composite: weighted combination of other indicators

  value_type         text             NOT NULL
                                      CHECK (value_type IN ('numeric','text','json')),
  -- numeric:  a single float  (most indicators)
  -- text:     a category or label
  -- json:     a distribution or breakdown object

  unit               text,
  -- 'score_0_1', 'm2_per_m2', 'ratio', 'count', 'MXN_per_m2', 'index'

  valid_min          double precision,
  valid_max          double precision,
  -- Expected numeric range. Values outside this range are flagged at write time.
  -- Only meaningful for value_type = 'numeric'. Must be NULL for all other types.

  aggregation_method text             NOT NULL
                                      CHECK (aggregation_method IN
                                        ('mean','weighted_mean','sum','min','max','none')),
  -- How to roll up this indicator from a finer to a coarser spatial level.
  -- 'none' for indicators that are not aggregable.

  applies_to         text[]           NOT NULL,
  -- Which type_slugs this indicator is computed for.
  -- ARRAY['block'] or ARRAY['block','ageb','lot']

  CHECK (cardinality(applies_to) > 0),
  CHECK (valid_min IS NULL OR valid_max IS NULL OR valid_min <= valid_max),
  CHECK (value_type = 'numeric' OR (valid_min IS NULL AND valid_max IS NULL)),
  CHECK (
    (computation_kind = 'direct'    AND model_family IS NOT NULL) OR
    (computation_kind = 'composite' AND model_family IS NULL)
  )
);
```

**Example rows:**
```sql
INSERT INTO curated_geo.indicator_catalog
  (slug, name, model_family, computation_kind, value_type, unit,
   valid_min, valid_max, aggregation_method, applies_to)
VALUES
  ('accessibility_walkability', 'Walkability Score',
   'accessibility', 'direct',    'numeric', 'score_0_1', 0, 1,  'weighted_mean', ARRAY['block']),

  ('accessibility_transit',     'Transit Accessibility',
   'accessibility', 'direct',    'numeric', 'score_0_1', 0, 1,  'weighted_mean', ARRAY['block','ageb']),

  ('green_coverage',            'Green Area Coverage',
   'green_area',    'direct',    'numeric', 'ratio_0_1', 0, 1,  'weighted_mean', ARRAY['block','ageb']),

  ('edificability_far',         'Floor Area Ratio',
   'housing',       'direct',    'numeric', 'm2_per_m2', 0, 20, 'mean',          ARRAY['lot','block']),

  ('fiscal_tax_base',           'Municipal Tax Base',
   'fiscal',        'direct',    'numeric', 'MXN_per_m2',0, NULL,'sum',          ARRAY['lot','block']),

  ('liveability_index',         'Liveability Index',
   NULL,            'composite', 'numeric', 'score_0_1', 0, 1,  'weighted_mean', ARRAY['block']);
```

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
  -- For weighted composites. NULL means equal-weight or the formula is custom.
  -- If any dependency for a given indicator_id has a weight, all must have weights.
  -- Weights for a given indicator_id must sum to 1.0.

  PRIMARY KEY (indicator_id, depends_on_id),
  CHECK (indicator_id <> depends_on_id),
  CHECK (weight IS NULL OR (weight >= 0 AND weight <= 1))
);
```

**Example — liveability_index = 0.4 × walkability + 0.3 × transit + 0.3 × green_coverage:**
```sql
INSERT INTO curated_geo.indicator_dependency (indicator_id, depends_on_id, weight)
VALUES
  (:liveability_id, :walkability_id,  0.4),
  (:liveability_id, :transit_id,      0.3),
  (:liveability_id, :green_id,        0.3);
```

Enforce via a deferred trigger — `curated_geo.indicator_dependency_validate` — that fires `AFTER INSERT OR UPDATE OR DELETE`, `DEFERRABLE INITIALLY DEFERRED`. It must check:
- `indicator_id` references a composite indicator (`computation_kind = 'composite'`)
- No cycles exist in the dependency graph (recursive CTE reachability check)
- If any weight for a given `indicator_id` is non-null, all must be non-null and sum to 1.0 (within 1e-9 tolerance)

Deferred execution is required because acyclicity and weight-sum can only be verified after all dependency rows for a given indicator have been inserted in the same transaction.

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
  scenario_id     uuid             REFERENCES app_core.scenario(id) ON DELETE CASCADE,
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

  -- The value — exactly one column populated per row
  value_numeric   double precision,
  value_text      text,
  value_json      jsonb,
  -- value_json examples:
  --   distribution:  {"0_14": 0.23, "15_64": 0.65, "65_plus": 0.12}
  --   composite breakdown: {"walkability": 0.71, "transit": 0.65, "green": 0.80}

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
  -- For indicators derived from probabilistic models or ML outputs.
  -- Set when the model produces a per-feature uncertainty estimate (e.g. 0.87 = 87% confidence).
  -- Leave NULL for deterministic indicators (FAR, tax base, area ratios).
  missing_reason  text,
  -- Required when quality_flag = 'missing'. Human-readable explanation.

  -- Lifecycle
  status          text             NOT NULL DEFAULT 'current'
                                   CHECK (status IN ('current','stale','failed')),
  -- current: latest valid result for this (feature, scenario, indicator) triple
  -- stale:   input data or scenario changed; needs recomputation
  -- failed:  last computation attempt failed; see job_run.error_message
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
```

**Trigger requirement — `curated_geo.indicator_result_before_write`:**
- Set `feature_type` from `feature.type_slug` (denormalize at insert; reject if it drifts on update).
- Set `indicator_slug` from `indicator_catalog.slug` (same rule).
- Set `model_version` from `job_run.code_version` if `model_version IS NULL`.
- Reject if `job_run.scenario_id` does not match `indicator_result.scenario_id`.
- Reject if `feature.type_slug` is not in `indicator_catalog.applies_to`, unless `quality_flag = 'not_applicable'`.
- **`not_applicable` rows:** workers do NOT write these proactively. A missing row means "not yet computed or not applicable." Write `not_applicable` explicitly only when needed for audit or to suppress recomputation. The API treats a missing row as not-applicable for features whose `type_slug` is not in `applies_to`.
- Require exactly one value column populated when `quality_flag IN ('measured','estimated','imputed')` and `status != 'failed'`.
- Require all value columns NULL when `quality_flag IN ('missing','not_applicable')` or `status = 'failed'`.
- Require `missing_reason` when `quality_flag = 'missing'`.
- Reject `value_numeric` outside `[valid_min, valid_max]` when those bounds are defined.
- Maintain `updated_at`.

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

One INSERT into `indicator_catalog`. No migration.

```sql
INSERT INTO curated_geo.indicator_catalog
  (slug, name, model_family, computation_kind, value_type, unit,
   valid_min, valid_max, aggregation_method, applies_to)
VALUES
  ('urban_heat_exposure', 'Urban Heat Exposure Index',
   'green_area', 'direct', 'numeric', 'score_0_1',
   0, 1, 'weighted_mean', ARRAY['block','ageb']);
```

The model worker for `green_area` can now write results for this slug.

---

### Adding a new composite indicator

```sql
-- 1. Register the composite
INSERT INTO curated_geo.indicator_catalog
  (slug, name, computation_kind, value_type, unit,
   valid_min, valid_max, aggregation_method, applies_to)
VALUES
  ('urban_quality_index', 'Urban Quality Index',
   'composite', 'numeric', 'score_0_1',
   0, 1, 'weighted_mean', ARRAY['block'])
RETURNING id; -- :uqi_id

-- 2. Define its components
INSERT INTO curated_geo.indicator_dependency (indicator_id, depends_on_id, weight)
VALUES
  (:uqi_id, :walkability_id,  0.40),
  (:uqi_id, :green_id,        0.35),
  (:uqi_id, :heat_id,         0.25);
```

No new model is required. A composite worker reads the component results and applies weights.

---

### Writing results from a model job

The model worker:

1. Marks any previous current result as stale.
2. Writes the new current result.

```sql
-- Step 1: mark previous result stale
UPDATE curated_geo.indicator_result
SET status = 'stale', updated_at = now()
WHERE feature_id    = :block_id
  AND scenario_id  IS NULL
  AND indicator_id  = :walkability_id
  AND status        = 'current';

-- Step 2: write new result
-- Workers supply only feature_id, indicator_id, job_run_id, and the value.
-- The before-write trigger sets feature_type, indicator_slug, and model_version automatically.
INSERT INTO curated_geo.indicator_result
  (feature_id, scenario_id,
   indicator_id,
   job_run_id, model_version,
   value_numeric, quality_flag, status)
VALUES
  (:block_id, NULL,
   :walkability_id,
   :job_run_id, 'accessibility-v1.4',
   0.73, 'measured', 'current');
```

Set `confidence` when the model produces a per-feature uncertainty estimate; leave NULL for deterministic indicators.

For a result that could not be computed due to missing data:
```sql
INSERT INTO curated_geo.indicator_result
  (feature_id, scenario_id,
   indicator_id,
   job_run_id, quality_flag, missing_reason, status)
VALUES
  (:block_id, NULL,
   :walkability_id,
   :job_run_id, 'missing', 'No road network data within 500m', 'current');
```

---

### Computing a composite indicator result

The composite worker follows the same stale-then-insert pattern, but reads component results instead of city data.

1. Read all `current` component results for the same `(feature_id, scenario_id)`.
2. Apply weights to compute the composite value.
3. Mark any previous current composite result stale, then insert the new one.

```sql
-- Step 1: compute weighted aggregate from components
SELECT
  SUM(ir.value_numeric * d.weight) AS composite_value
FROM curated_geo.indicator_result ir
JOIN curated_geo.indicator_dependency d ON d.depends_on_id = ir.indicator_id
WHERE d.indicator_id  = :liveability_id
  AND ir.feature_id   = :feature_id
  AND ir.scenario_id  IS NOT DISTINCT FROM :scenario_id
  AND ir.status       = 'current';
-- Example: liveability_index = 0.4 × walkability + 0.3 × transit + 0.3 × green_coverage

-- Step 2: mark previous result stale (same as direct indicator pattern)
UPDATE curated_geo.indicator_result
SET status = 'stale', updated_at = now()
WHERE feature_id    = :feature_id
  AND scenario_id   IS NOT DISTINCT FROM :scenario_id
  AND indicator_id  = :liveability_id
  AND status        = 'current';

-- Step 3: insert composite result
INSERT INTO curated_geo.indicator_result
  (feature_id, scenario_id, indicator_id, job_run_id,
   value_numeric, quality_flag, status)
VALUES
  (:feature_id, :scenario_id, :liveability_id, :job_run_id,
   :composite_value, 'measured', 'current');
```

If any component result is missing or failed, write `quality_flag = 'missing'` with a `missing_reason` explaining which component was unavailable. Do not produce a partial composite.

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
 AND base.indicator_slug = 'accessibility_walkability'
 AND base.scenario_id   IS NULL
JOIN serving_geo.current_indicator_v scen
  ON scen.feature_id    = f.id
 AND scen.indicator_slug = 'accessibility_walkability'
 AND scen.scenario_id   = :scenario_id
WHERE f.type_slug = 'block'
  AND ST_Intersects(f.geometry, :aoi_geometry);
```

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

### Marking indicators stale after a scenario intervention

When an intervention changes features in a scenario, all affected indicator results — including composite indicators that depend on them — must be marked stale before recomputation.

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

After this, a new job is queued for each affected model family (see `JOB_LAYER.md`).

### Rolling up a block-grain indicator to AGEB

*Worked example: walkability.* The walkability model writes
`indicator_result` rows at the `block` grain. Walkability has
`indicator_catalog.aggregation_method = 'weighted_mean'`, and a row exists
in `curated_geo.feature_overlap_type_pair` declaring `(MEX:block) → (MEX:ageb)`.

After the model worker marks its `job_run` as `succeeded`, the rollup
worker (see `JOB_LAYER.md`) is queued. For each AGEB, it:

1. reads the current block-grain walkability `indicator_result` rows,
2. joins them to `curated_geo.feature_overlap` on the target AGEB,
3. computes the weighted mean using `overlap_ratio` as the per-block weight,
4. marks any existing AGEB-grain `current` rows stale,
5. inserts a new AGEB-grain row with `quality_flag = 'estimated'` and
   `lineage_ref.triggering_job_run_ids` pointing back to the block model run.

Consumers read the AGEB-grain value through `serving_geo.current_indicator_v`
exactly as they read the block-grain value — the rollup is invisible at
the read boundary except for `quality_flag`. The pure aggregation math
is implemented in `reimagina.jobs.aggregation.aggregate` so it can be
reused and tested outside of the worker.

---

## Build order

Within this layer (after DATA_LAYER and JOB_LAYER tables exist):

1. `curated_geo.indicator_catalog` — seed with known indicators
2. `curated_geo.indicator_dependency` — define composite structures
3. `curated_geo.indicator_result` — with all indexes and trigger
4. `serving_geo.current_indicator_v`

See `JOB_LAYER.md` for the full cross-document build order.

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
