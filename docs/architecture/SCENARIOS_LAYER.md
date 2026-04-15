# Scenarios Layer

How the platform organizes planning work, defines proposed changes, configures models, runs computations, and surfaces results.

**Related documents:** `DATA_LAYER.md` — the city data scenarios read from (never modified). `INDICATORS_LAYER.md` — the computed outputs scenarios produce. `JOB_LAYER.md` — the async execution model, queue architecture, and worker contract.

---

## What this layer does

A scenario is a named "what if" — a proposed state of the city that has not happened yet. It overlays changes on the baseline city without ever modifying it.

The workflow is:

```
project  →  scenario  →  interventions + configuration  →  job_run  →  indicator_result
```

- **Project** — workspace that groups scenarios and areas of interest.
- **Scenario** — a named proposed state within a project.
- **Intervention** — a set of proposed changes to specific features in a scenario.
- **Configuration** — model input parameters specific to a scenario.
- **Job run** — an async model execution that computes indicator results for a scenario. Defined and managed in `JOB_LAYER.md`.
- **Indicator result** — the output. Stored in `curated_geo.indicator_result` with `scenario_id` set (see `INDICATORS_LAYER.md`).

**The baseline is never touched.** Scenarios read `serving_geo.feature_v`, apply intervention overrides in-memory, run models, and write new `indicator_result` rows tagged with their `scenario_id`.

---

## Tables

### app_core.project

Top-level workspace. Groups scenarios, areas of interest, and users.

```sql
CREATE TABLE app_core.project (
  id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  name        text        NOT NULL,
  description text,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);
```

---

### app_core.scenario

A named what-if state within a project. One project can have many scenarios.

```sql
CREATE TABLE app_core.scenario (
  id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id  uuid        NOT NULL REFERENCES app_core.project(id) ON DELETE CASCADE,
  name        text        NOT NULL,
  description text,
  status      text        NOT NULL DEFAULT 'draft'
                          CHECK (status IN ('draft','active','archived')),
  -- draft:    being configured, results may not exist yet
  -- active:   in use, can run jobs and read results
  -- archived: no new jobs; results are retained for comparison
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX scenario_project_idx ON app_core.scenario (project_id);
```

---

### app_core.intervention

A named set of proposed changes within a scenario. A scenario can have multiple interventions — for example, "Widen sidewalks on Av. Juárez" and "Add park on Block 42" can be two separate interventions in the same scenario.

```sql
CREATE TABLE app_core.intervention (
  id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  scenario_id uuid        NOT NULL REFERENCES app_core.scenario(id) ON DELETE CASCADE,
  name        text        NOT NULL,
  description text,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX intervention_scenario_idx ON app_core.intervention (scenario_id);
```

---

### app_core.intervention_feature

Which features an intervention targets, and what changes for each one.

```sql
CREATE TABLE app_core.intervention_feature (
  intervention_id     uuid  NOT NULL REFERENCES app_core.intervention(id) ON DELETE CASCADE,
  feature_id          uuid  NOT NULL REFERENCES curated_geo.feature(id)   ON DELETE CASCADE,

  attribute_overrides jsonb NOT NULL DEFAULT '{}',
  -- Proposed changes to the feature's attributes for this scenario.
  -- Models apply these on top of the baseline when computing indicators.
  -- The baseline feature row is never modified.
  --
  -- Examples:
  --   sidewalk:  {"width_m": 3.5, "has_planters": true, "tree_count": 12}
  --   block:     {"land_use": "green_area", "green_area_pct": 0.60}
  --   road:      {"lanes": 2, "has_bike_lane": true, "speed_limit_kmh": 30}

  PRIMARY KEY (intervention_id, feature_id)
);

CREATE INDEX intervention_feature_feature_idx ON app_core.intervention_feature (feature_id);
```

---

### app_core.configuration

Model input parameters for a scenario — the "knobs" that analytical models read when computing scenario indicators. One row per model family per scenario.

```sql
CREATE TABLE app_core.configuration (
  id           uuid  PRIMARY KEY DEFAULT gen_random_uuid(),
  scenario_id  uuid  NOT NULL REFERENCES app_core.scenario(id) ON DELETE CASCADE,
  model_family text  NOT NULL,
  -- 'accessibility', 'housing', 'green_area', 'fiscal', 'urban_form'

  parameters   jsonb NOT NULL DEFAULT '{}',
  -- Model-specific input parameters. Examples:
  --
  -- accessibility:
  --   {"walking_speed_kmh": 5, "max_walk_minutes": 15, "include_cycling": true}
  --
  -- housing:
  --   {"avg_floors": 4, "m2_per_unit": 85, "parking_ratio": 0.5,
  --    "social_housing_pct": 0.30}
  --
  -- green_area:
  --   {"target_canopy_pct": 0.25, "tree_growth_years": 5,
  --    "min_park_area_m2": 500}
  --
  -- fiscal:
  --   {"tax_rate_pct": 1.2, "assessment_ratio": 0.80}

  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now(),

  UNIQUE (scenario_id, model_family)
);
```

---

### jobs_runtime.job_run

Defined in `JOB_LAYER.md`. Must be created before `indicator_result` (which references `job_run_id`) and before the use cases below.

---

## How interventions feed into indicator computation

When a scenario job runs for a given model family, it follows this sequence:

```
1. Read canonical baseline via pre-computed aoi_feature
     SELECT f.*, af.overlap_ratio
     FROM serving_geo.feature_v f
     JOIN curated_geo.aoi_feature af
       ON af.feature_id = f.id
      AND af.aoi_id     = :aoi_id
     WHERE f.type_slug IN ('block','lot','road', ...);
     -- When aoi_id IS NULL: omit the JOIN and read all features of the relevant types.

2. Read intervention overrides for this scenario
     SELECT inf.feature_id, inf.attribute_overrides
     FROM app_core.intervention_feature inf
     JOIN app_core.intervention i ON i.id = inf.intervention_id
     WHERE i.scenario_id = :scenario_id;

3. Read model configuration for this scenario
     SELECT parameters
     FROM app_core.configuration
     WHERE scenario_id  = :scenario_id
       AND model_family = :model_family;

4. Merge in-memory (never written back to feature tables):
     effective_attributes = baseline.attributes
                         || intervention.attribute_overrides   -- overrides win

5. Run model using effective_attributes + configuration.parameters

6. Write results to curated_geo.indicator_result with scenario_id = :scenario_id
```

The baseline is never modified. The scenario result is always a new `indicator_result` row.

---

## How baseline data participates in scenarios

Scenarios do not replace the baseline city. They sit on top of it. In practice, four different kinds of baseline information participate in a run:

| Data role | Where it lives | How scenarios use it |
|---|---|---|
| City-defining baseline | `curated_geo.feature` → `serving_geo.feature_v` | Provides the stable objects, geometries, and base attributes that all models start from |
| Baseline enrichments | `curated_geo.observation` → `serving_geo.feature_v` | Adds read-only evidence such as population, NDVI, sidewalk width, or cellphone footfall |
| Scenario-editable context | `app_core.intervention_feature.attribute_overrides` | Temporarily overrides selected feature attributes during computation |
| Scenario-wide assumptions | `app_core.configuration.parameters` | Sets model-family parameters that are not tied to one feature |

This produces one simple rule:

- baseline `feature` and `observation` rows are never edited by a scenario,
- only the in-memory effective view of a feature changes during computation,
- outputs are written as new `indicator_result` rows for that scenario.

Examples:

- A scenario may override `sidewalk.width_m` for a corridor, but it does not rewrite the baseline sidewalk feature.
- A scenario may still use census population, NDVI, or cellphone footfall observations as read-only evidence while recomputing accessibility or green indicators.
- A new tree-planting intervention changes model inputs through overrides, not by updating the detected-tree baseline rows in place.

---

## Use cases

### Creating a new project

```sql
INSERT INTO app_core.project (name, description)
VALUES (
  'Centro Histórico Regeneration',
  'Urban renewal study for the downtown core area'
)
RETURNING id; -- :project_id
```

---

### Creating an area of interest for a project

The AOI is stored in the data layer but linked to a project.

```sql
INSERT INTO curated_geo.area_of_interest (project_id, name, description, geometry)
VALUES (
  :project_id,
  'Study boundary — downtown core',
  'Covers the 12 blocks within the historic district perimeter',
  ST_GeomFromGeoJSON(:aoi_geojson)
)
RETURNING id; -- :aoi_id
```

A background job then populates `curated_geo.aoi_feature` with the intersection of this geometry and all canonical features.

---

### Creating a scenario

```sql
INSERT INTO app_core.scenario (project_id, name, description, status)
VALUES (
  :project_id,
  'Green Corridor — Av. Juárez',
  'Convert Av. Juárez into a tree-lined pedestrian priority corridor with widened sidewalks',
  'draft'
)
RETURNING id; -- :scenario_id
```

---

### Adding an intervention to a scenario

```sql
-- Create the intervention
INSERT INTO app_core.intervention (scenario_id, name, description)
VALUES (
  :scenario_id,
  'Widen sidewalks on Av. Juárez',
  'Increase sidewalk width from 1.5m to 3.5m and add tree planters every 8m'
)
RETURNING id; -- :intervention_id

-- Specify which features change and how
INSERT INTO app_core.intervention_feature (intervention_id, feature_id, attribute_overrides)
VALUES
  (:intervention_id, :sidewalk_segment_1_id,
   '{"width_m": 3.5, "tree_count": 12, "has_planters": true}'),
  (:intervention_id, :sidewalk_segment_2_id,
   '{"width_m": 3.5, "tree_count": 8, "has_planters": true}'),
  (:intervention_id, :road_av_juarez_id,
   '{"lanes": 2, "has_bike_lane": true, "speed_limit_kmh": 30}');
```

---

### Configuring model parameters for a scenario

```sql
INSERT INTO app_core.configuration (scenario_id, model_family, parameters)
VALUES
  (:scenario_id, 'accessibility',
   '{"walking_speed_kmh": 5, "max_walk_minutes": 15, "include_cycling": true}'),

  (:scenario_id, 'green_area',
   '{"target_canopy_pct": 0.25, "tree_growth_years": 5}'),

  (:scenario_id, 'housing',
   '{"avg_floors": 4, "m2_per_unit": 85, "social_housing_pct": 0.30}')

ON CONFLICT (scenario_id, model_family) DO UPDATE
  SET parameters = EXCLUDED.parameters,
      updated_at = now();
```

---

### Triggering scenario computation

The API creates one `job_run` row per model family affected by the scenario, then enqueues each one.

```sql
INSERT INTO jobs_runtime.job_run (scenario_id, model_family, status, code_version)
VALUES
  (:scenario_id, 'accessibility', 'queued', 'v2.1.0'),
  (:scenario_id, 'green_area',    'queued', 'v1.3.2'),
  (:scenario_id, 'housing',       'queued', 'v3.0.1')
RETURNING id, model_family;
-- Each returned ID is enqueued as a message on its model family's queue.
```

Only model families whose inputs are affected by this scenario's interventions or configuration need to be queued. See `JOB_LAYER.md` for the full worker execution contract, result write pattern, and failure handling.

---

### Reading all scenario results for an AOI

```sql
SELECT
  f.id,
  f.name,
  f.geometry,
  af.overlap_ratio,
  ci.indicator_slug,
  ci.indicator_name,
  ci.value_numeric,
  ci.quality_flag
FROM curated_geo.aoi_feature af
JOIN serving_geo.feature_v f            ON f.id  = af.feature_id
JOIN serving_geo.current_indicator_v ci ON ci.feature_id = f.id
                                       AND ci.scenario_id = :scenario_id
WHERE af.aoi_id    = :aoi_id
  AND f.type_slug  = 'block'
  AND af.overlap_basis = 'area'
ORDER BY ci.indicator_slug, f.id;
```

---

### Comparing two scenarios

```sql
SELECT
  f.id,
  f.name,
  s1.value_numeric                          AS scenario_a_value,
  s2.value_numeric                          AS scenario_b_value,
  s2.value_numeric - s1.value_numeric       AS delta_a_to_b
FROM serving_geo.feature_v f
JOIN serving_geo.current_indicator_v s1
  ON s1.feature_id    = f.id
 AND s1.indicator_slug = 'accessibility_walkability'
 AND s1.scenario_id   = :scenario_a_id
JOIN serving_geo.current_indicator_v s2
  ON s2.feature_id    = f.id
 AND s2.indicator_slug = 'accessibility_walkability'
 AND s2.scenario_id   = :scenario_b_id
WHERE f.type_slug = 'block'
  AND ST_Intersects(f.geometry, :aoi_geometry);
```

---

### Getting all interventions and their targets for a scenario

```sql
SELECT
  i.id               AS intervention_id,
  i.name             AS intervention_name,
  f.id               AS feature_id,
  f.name             AS feature_name,
  f.type_slug,
  inf.attribute_overrides
FROM app_core.intervention i
JOIN app_core.intervention_feature inf ON inf.intervention_id = i.id
JOIN serving_geo.feature_v f           ON f.id = inf.feature_id
WHERE i.scenario_id = :scenario_id
ORDER BY i.name, f.type_slug, f.id;
```

---

### Checking job status for a scenario

See `JOB_LAYER.md` — polling, failure handling, and re-queuing patterns are defined there.

---

### Updating an intervention and triggering recomputation

```sql
-- Update the override for one feature
UPDATE app_core.intervention_feature
SET attribute_overrides = '{"width_m": 4.0, "tree_count": 15, "has_planters": true}'
WHERE intervention_id = :intervention_id
  AND feature_id      = :sidewalk_segment_1_id;

-- Mark affected indicator results stale (see INDICATORS_LAYER.md for the full query)
-- Then queue new jobs for affected model families (see JOB_LAYER.md for the re-queue pattern).
```

---

### Archiving a scenario

```sql
UPDATE app_core.scenario
SET status = 'archived', updated_at = now()
WHERE id = :scenario_id;
```

Archived scenarios retain all `indicator_result` rows and can be read for comparison. No new jobs should be queued for them.

---

### Duplicating a scenario (copy interventions and configuration)

A scenario can be cloned to create a variation without starting from scratch.

```sql
-- Create the new scenario
INSERT INTO app_core.scenario (project_id, name, description, status)
SELECT project_id, :new_name, description, 'draft'
FROM app_core.scenario WHERE id = :source_scenario_id
RETURNING id; -- :new_scenario_id

-- Copy interventions
WITH new_interventions AS (
  INSERT INTO app_core.intervention (scenario_id, name, description)
  SELECT :new_scenario_id, name, description
  FROM app_core.intervention WHERE scenario_id = :source_scenario_id
  RETURNING id, name
)
INSERT INTO app_core.intervention_feature (intervention_id, feature_id, attribute_overrides)
SELECT ni.id, inf.feature_id, inf.attribute_overrides
FROM app_core.intervention_feature inf
JOIN app_core.intervention i   ON i.id = inf.intervention_id
JOIN new_interventions ni      ON ni.name = i.name
WHERE i.scenario_id = :source_scenario_id;

-- Copy configuration
INSERT INTO app_core.configuration (scenario_id, model_family, parameters)
SELECT :new_scenario_id, model_family, parameters
FROM app_core.configuration WHERE scenario_id = :source_scenario_id;
```

---

## Build order

1. `app_core.project`
2. `app_core.scenario`
3. `jobs_runtime.job_run` — defined in `JOB_LAYER.md`; depends on `app_core.scenario`
4. `app_core.intervention`
5. `app_core.intervention_feature` — depends on `curated_geo.feature` (from DATA_LAYER)
6. `app_core.configuration`

See `JOB_LAYER.md` for the full cross-document build order.

---

## How the four layers relate

```
DATA_LAYER                     SCENARIOS_LAYER           JOB_LAYER               INDICATORS_LAYER

curated_geo.feature ───────►   intervention_feature   ─┐
curated_geo.observation ───►   configuration          ─┼──► job_run ─────────► indicator_result
        │                                              │         │
        └──────────────► serving_geo.feature_v ────────┘         └── reads serving baseline
                                                                  applies overrides in memory
                                                                  writes scenario-tagged outputs
```

The `feature` table is still the shared anchor, but workers read it through `serving_geo.feature_v` so current observations are already merged in. Scenarios contribute only overrides and configuration. Jobs apply those overlays in memory and write their effects to `indicator_result`. The baseline is never modified. See `JOB_LAYER.md` for the full execution flow.
