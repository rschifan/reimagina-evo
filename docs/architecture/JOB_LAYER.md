# Job Layer

How the platform executes model computations asynchronously — the queue architecture, worker contract, job lifecycle, and execution tracking.

**Related documents:** `SCENARIOS_LAYER.md` — scenarios and interventions that trigger jobs. `INDICATORS_LAYER.md` — indicator results that jobs produce. `DATA_LAYER.md` — canonical city data that jobs read from.

---

## What this layer does

Computation in Reimagina is always asynchronous. When a scenario is ready to be evaluated, the API does not run models in the request path. Instead:

```
API                 Queue               Worker              Database
 │                    │                    │                    │
 ├── create job_run ──►                    │                    │
 ├── enqueue message ─►                    │                    │
 ├── return jobId ────►                    │                    │
 │                    ├── deliver message ─►                    │
 │                    │                    ├── read feature_v ──►
 │                    │                    ├── apply overrides  │
 │                    │                    ├── run model        │
 │                    │                    ├── write results ───►
 │                    │                    └── update job_run ──►
 │                    │                    │                    │
Frontend polls job status via API ──────────────────────────────►
```

**One queue per model family.** Each analytical model (accessibility, housing, green_area, fiscal, urban_form, composite) has its own queue and its own worker pool. This isolates failures and allows independent scaling.

**`job_run` is the audit record.** Every computation — whether for the base city or a scenario — produces a `job_run` row before it starts. Workers read and update this row. `indicator_result` rows reference it as their provenance.

---

## Queue architecture

| Model family | Queue name | Notes |
|---|---|---|
| `accessibility` | `jobs-accessibility` | Walkability, transit access |
| `housing` | `jobs-housing` | Floor area ratio, housing units, edificability |
| `green_area` | `jobs-green-area` | Green coverage, urban heat, canopy |
| `fiscal` | `jobs-fiscal` | Tax base, land value |
| `urban_form` | `jobs-urban-form` | Street areas, block metrics |
| `composite` | `jobs-composite` | Runs after direct indicators are ready |
| `rollup` | `jobs-rollup` | Aggregates indicator results to a coarser grain via `feature_overlap` |

Each queue has a **dead-letter queue (DLQ)** for messages that exceed the retry limit. DLQ messages require human investigation — they are never automatically reprocessed.

**Message schema** (JSON envelope on the queue):

```json
{
  "job_run_id": "uuid",
  "scenario_id": "uuid | null",
  "model_family": "accessibility",
  "aoi_id": "uuid | null",
  "code_version": "v2.1.0"
}
```

Workers must be idempotent: receiving the same message twice must produce the same result without duplicating `indicator_result` rows.

---

## Tables

### jobs_runtime.job_run

Tracks one async model execution. One row per (scenario, model family) computation attempt. Referenced by every `indicator_result` row as its execution provenance.

```sql
CREATE TABLE jobs_runtime.job_run (
  id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),

  scenario_id   uuid        REFERENCES app_core.scenario(id) ON DELETE CASCADE,
  -- NULL = base city computation (Diagnosis mode — no scenario)
  -- UUID = a specific scenario (Evaluation mode)

  model_family  text        NOT NULL,
  -- 'accessibility', 'housing', 'green_area', 'fiscal', 'urban_form', 'composite'

  status        text        NOT NULL
                            CHECK (status IN ('queued','running','succeeded','failed','cancelled')),
  -- queued:    message is on the queue; worker has not started
  -- running:   worker has claimed the job
  -- succeeded: worker completed; indicator_result rows are written
  -- failed:    worker encountered an unrecoverable error; see error_message
  -- cancelled: job was cancelled before a worker claimed it

  code_version  text        NOT NULL,
  -- git SHA or package version — records exactly what code ran

  queued_at     timestamptz NOT NULL DEFAULT now(),
  started_at    timestamptz,
  completed_at  timestamptz,
  error_message text,
  -- Set when status = 'failed'. Human-readable error summary from the worker.

  CHECK (started_at   IS NULL OR started_at   >= queued_at),
  CHECK (completed_at IS NULL OR started_at   IS NULL OR completed_at >= started_at)
);

CREATE INDEX job_run_scenario_idx ON jobs_runtime.job_run (scenario_id);
CREATE INDEX job_run_status_idx   ON jobs_runtime.job_run (status);
CREATE INDEX job_run_family_idx   ON jobs_runtime.job_run (model_family, status);
```

---

## Job lifecycle

```
          ┌──────────┐
          │  queued  │  ← API creates job_run row and enqueues message
          └────┬─────┘
               │  worker claims message
          ┌────▼─────┐
          │ running  │  ← worker sets started_at = now()
          └────┬─────┘
         ┌─────┴──────┐
    ┌────▼────┐  ┌─────▼────┐
    │succeeded│  │  failed  │  ← worker sets completed_at, error_message
    └─────────┘  └──────────┘
                      │  after retry limit exceeded
                 ┌────▼────┐
                 │   DLQ   │  ← requires manual investigation
                 └─────────┘

    cancelled: API-initiated cancellation before worker claims the job
```

**Execution state lives here, not on `indicator_result`.** `indicator_result.status` values are `current`, `stale`, `failed` — lifecycle states of a result. `job_run.status` values are `queued`, `running`, `succeeded`, `failed`, `cancelled` — lifecycle states of an execution. These are different concerns.

---

## Worker execution contract

This is the sequence every model worker must follow. Deviation breaks result consistency.

```
1. Claim the job
   UPDATE jobs_runtime.job_run
   SET status = 'running', started_at = now()
   WHERE id = :job_run_id AND status = 'queued';
   -- If 0 rows updated: another worker claimed it first → stop.

2. Read the baseline from the serving layer
   -- When aoi_id is provided: filter to AOI membership via pre-computed aoi_feature.
   -- overlap_ratio is available for prorating indicators at AOI boundaries.
   SELECT f.*, af.overlap_ratio
   FROM serving_geo.feature_v f
   JOIN curated_geo.aoi_feature af
     ON af.feature_id = f.id
    AND af.aoi_id     = :aoi_id
   WHERE f.type_slug IN ('block', 'lot', 'road', ...);
   -- When aoi_id IS NULL: omit the JOIN and read all features of the relevant types.

3. Read intervention overrides (if scenario_id IS NOT NULL)
   SELECT inf.feature_id, inf.attribute_overrides
   FROM app_core.intervention_feature inf
   JOIN app_core.intervention i ON i.id = inf.intervention_id
   WHERE i.scenario_id = :scenario_id;

4. Read model configuration (if scenario_id IS NOT NULL)
   SELECT parameters
   FROM app_core.configuration
   WHERE scenario_id  = :scenario_id
     AND model_family = :model_family;

5. Merge in memory — never write back to feature tables
   effective_attributes = baseline.attributes
                       || intervention.attribute_overrides  -- overrides win

6. Run the model using effective_attributes + configuration.parameters

7. Mark previous current results stale
   UPDATE curated_geo.indicator_result
   SET status = 'stale', updated_at = now()
   WHERE feature_id   = ANY(:computed_feature_ids)
     AND scenario_id  IS NOT DISTINCT FROM :scenario_id
     AND indicator_id = ANY(:computed_indicator_ids)
     AND status       = 'current';

8. Write new results
   INSERT INTO curated_geo.indicator_result
     (feature_id, scenario_id,
      indicator_id,
      job_run_id, model_version,
      value_numeric, quality_flag, status)
   VALUES (...);
   -- The before-write trigger sets feature_type, indicator_slug, and model_version automatically.
   -- Workers must NOT supply feature_type or indicator_slug — the trigger derives and validates them.
   -- See INDICATORS_LAYER.md for the full write pattern.

9. Mark job succeeded
   UPDATE jobs_runtime.job_run
   SET status = 'succeeded', completed_at = now()
   WHERE id = :job_run_id;

10. On unrecoverable error: mark job failed
    UPDATE jobs_runtime.job_run
    SET status = 'failed', completed_at = now(),
        error_message = :error_summary
    WHERE id = :job_run_id;
    -- Do not write partial results. Let the DLQ handle retry policy.
```

**Idempotency:** if a worker re-processes the same message (e.g., after a crash and restart), step 7 marks prior rows stale, step 8 inserts new rows. The partial unique indexes in `indicator_result` enforce that only one `current` result exists per `(feature, scenario, indicator)`. Duplicate `succeeded` job_run rows are acceptable — `indicator_result` lineage points to the latest one.

---

## Rollup worker

The rollup worker moves indicator results from a fine-grain `feature_type`
(e.g., `block`) up to a coarser grain (e.g., `ageb`). It does not run a model
— it aggregates results that other workers already wrote.

**When does it run?** Queued automatically after a model-family worker
reports `succeeded`, but only when:

1. the indicator's `indicator_catalog.aggregation_method` is not `'none'`, and
2. a row exists in `curated_geo.feature_overlap_type_pair` where the
   `source_type` matches the indicator's grain and the `target_type` is
   coarser.

**Execution ordering.** Rollup runs **before** composite so composite workers
that reference the coarser grain can read the rolled-up values through
`serving_geo.indicator_result_v`.

**Input message:**

```json
{
  "rollup_run_id": "uuid",
  "scenario_id": "uuid | null",
  "indicator_id": "uuid",
  "target_feature_type": "ageb",
  "aggregation_method": "weighted_mean",
  "triggering_job_run_ids": ["uuid", "uuid"]
}
```

**Contract:**

1. Claim the `job_run` row (status → `running`, `model_family = 'rollup'`).
2. Read current fine-grain `indicator_result` rows for `indicator_id` at the
   source grain, scoped to the relevant features (all features for base;
   intervention + inherited features for a scenario).
3. Join to `curated_geo.feature_overlap` where the target feature has
   `type_slug = target_feature_type`.
4. Apply `aggregation_method` over the fine-grain values grouped by target
   feature. The supported methods are:
   - `mean` — simple arithmetic mean
   - `weighted_mean` — weights are `feature_overlap.overlap_measure`, i.e. the absolute intersection measure (m² for polygon-polygon rollups). Using the ratio would collapse to an arithmetic mean for fully-nested children and produce wrong numbers for extensive metrics like density.
   - `sum`
   - `min`, `max`
5. Mark any existing `current` parent rows stale (step 7 of the standard
   worker contract).
6. Insert the new parent rows with:
   - `quality_flag = 'estimated'` (values are derived at a different
     spatial resolution, not measured)
   - `job_run_id = rollup_run_id`
   - `lineage_ref.triggering_job_run_ids = [...]`

**Missing-input policy (prototype).** For each parent feature, compute the
fraction of child weight whose input is `missing` or `failed`. If that
fraction is > 50%, write the parent as `missing`; otherwise prorate the
aggregation over the remaining children and note `aggregation_note = "prorated"`.

**Idempotency.** The `(indicator_id, target_feature_id, scenario_id)`
partial-unique constraint on `indicator_result` guarantees one `current`
row per parent. Re-running with the same input set replaces existing
`current` rows and marks the old ones stale — no duplicates.

**Stale-propagation from below (prototype).** If a child `indicator_result`
becomes stale (e.g., a re-ingest), the parent rollup is **not** auto-marked
stale. The caller that staled the child must enqueue a rollup message with
the same `indicator_id` + `target_feature_type`. This is intentional for v1;
a trigger-based propagation is tracked as future work.

**Pure aggregation module.** Aggregation math lives in
`reimagina.jobs.aggregation` and is reusable outside of workers (e.g., in
CLI tools and tests). See that module for the exact semantics of each
`aggregation_method`.

**Implementation status (Layer 1).** The aggregation module and this
contract are in place; the worker body reads from `indicator_result`, which
lands with Layer 2. The full worker implementation therefore arrives with
the indicators layer.

---

## Read/write boundary

The execution boundary across layers is intentionally narrow:

- canonical baseline is written in `curated_geo`,
- readers consume the baseline through `serving_geo.feature_v`,
- scenario edits live in `app_core.intervention_feature` and `app_core.configuration`,
- workers apply those edits in memory only,
- computed outputs are written to `curated_geo.indicator_result`.

This keeps the baseline reproducible and prevents scenario execution from mutating source-of-truth city data.

---

## Use cases

### Triggering base city computation (Diagnosis mode)

Base state jobs have `scenario_id = NULL`. They compute the city as it exists today.

```sql
-- API creates job_run rows for all model families
INSERT INTO jobs_runtime.job_run (scenario_id, model_family, status, code_version)
VALUES
  (NULL, 'accessibility', 'queued', 'v2.1.0'),
  (NULL, 'housing',       'queued', 'v3.0.1'),
  (NULL, 'green_area',    'queued', 'v1.3.2'),
  (NULL, 'fiscal',        'queued', 'v1.1.0'),
  (NULL, 'urban_form',    'queued', 'v2.0.0')
RETURNING id, model_family;

-- Each ID is then enqueued as a message on the corresponding queue.
-- Composite jobs are queued after direct jobs succeed.
```

---

### Triggering scenario computation (Evaluation mode)

Called by the API after a scenario is configured with interventions and parameters.

```sql
-- API creates job_run rows for the affected model families
INSERT INTO jobs_runtime.job_run (scenario_id, model_family, status, code_version)
VALUES
  (:scenario_id, 'accessibility', 'queued', 'v2.1.0'),
  (:scenario_id, 'green_area',    'queued', 'v1.3.2'),
  (:scenario_id, 'housing',       'queued', 'v3.0.1')
RETURNING id, model_family;
-- Each row ID is enqueued on its model family's queue.
-- Composite jobs are queued after direct jobs succeed.
```

Only model families whose inputs are affected by the scenario's interventions or configuration need to be queued. The API determines this based on which `attribute_overrides` keys map to which model families.

---

### Polling job status for a scenario

Used by the frontend to show computation progress.

```sql
SELECT
  model_family,
  status,
  code_version,
  queued_at,
  started_at,
  completed_at,
  error_message
FROM jobs_runtime.job_run
WHERE scenario_id = :scenario_id
ORDER BY queued_at DESC;
```

A scenario is fully computed when all its `job_run` rows have `status = 'succeeded'` (or `failed`).

---

### Handling a failed job

```sql
-- Inspect the error
SELECT model_family, error_message, code_version, started_at, completed_at
FROM jobs_runtime.job_run
WHERE id = :job_run_id AND status = 'failed';

-- Re-queue after the issue is resolved
INSERT INTO jobs_runtime.job_run (scenario_id, model_family, status, code_version)
VALUES (:scenario_id, :model_family, 'queued', :fixed_code_version)
RETURNING id;
-- Enqueue the new job_run_id on the model family's queue.
-- Do NOT update the failed row — keep it for audit.
```

Failed `indicator_result` rows (from a previous failed attempt) remain with `status = 'failed'`. The new job will overwrite them in step 7 (mark stale) and step 8 (insert current).

---

### Finding all stale results that need recomputation

```sql
-- Which model families need to run for a scenario?
SELECT DISTINCT ic.model_family
FROM curated_geo.indicator_result ir
JOIN curated_geo.indicator_catalog ic ON ic.id = ir.indicator_id
WHERE ir.scenario_id = :scenario_id
  AND ir.status      = 'stale'
  AND ic.model_family IS NOT NULL  -- exclude composites — they rerun after their dependencies
ORDER BY ic.model_family;
```

---

## How the four layers connect

```
DATA_LAYER                     SCENARIOS_LAYER           JOB_LAYER                INDICATORS_LAYER

curated_geo.feature ───────►   intervention_feature   ─┐
curated_geo.observation ───►   configuration          ─┼──► job_run ──────────► indicator_result
        │                                              │         │                (scenario_id NULL or UUID)
        └──────────────► serving_geo.feature_v ────────┘         └── reads serving baseline
                                                                  applies overrides in memory
                                                                  writes indicator_result
```

**The flow in one sentence:** a scenario triggers a job → the job reads baseline city data through `serving_geo` → the job applies scenario overrides in memory → the job writes indicator results back to `curated_geo` → the frontend reads current outputs through serving views.

---

## Build order

`job_run` must exist before `indicator_result` is created, because `indicator_result` references `job_run_id`.

Full cross-document order:
1. Schemas (`app_core`, `jobs_runtime`, `curated_geo`, `serving_geo`)
2. `app_core.project`, `app_core.scenario`
3. `jobs_runtime.job_run`
4. DATA_LAYER tables (`data_source_catalog` → `ingestion_run` → `feature_type_catalog` → `feature` → `observation` → `file_asset` → `area_of_interest` → `aoi_feature`)
5. `app_core.intervention`, `app_core.intervention_feature`, `app_core.configuration`
6. `curated_geo.indicator_catalog`, `curated_geo.indicator_dependency`, `curated_geo.indicator_result`
7. All `serving_geo` views

---

## Standards referenced

| Decision | Standard |
|---|---|
| One queue per model family | Message-queue fan-out pattern (competing consumers per family) |
| Dead-letter queue | Standard async messaging reliability pattern |
| Idempotent workers | At-least-once delivery guarantee handling |
| Execution state on job_run, not on result | Separation of concerns: execution lifecycle vs. result lifecycle |
| Audit trail — never update failed rows | Append-only audit log pattern |
