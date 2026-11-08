# Scenarios Layer

How the platform records a proposed change to the city, and how that record turns into recomputed indicator results.

**Related documents:** `DATA_LAYER.md` — the city data scenarios read from (never modified). `INDICATORS_LAYER.md` — the computed outputs scenarios produce. `JOB_LAYER.md` — the `Orchestrator` port that executes a scenario run.

---

## What this layer does

A scenario is a named "what if" — a proposed state of the city that has not happened yet. It overlays changes on the baseline city without ever modifying it.

The workflow is:

```
project  →  scenario_id  →  scenario_override rows  →  submit_run  →  indicator_result
```

- **Project** — `app_core.project`, the workspace an area of interest belongs to. The only table in `app_core` (`r001_schemas_and_extensions.py:19-35`).
- **Scenario** — a bare uuid, not a row. There is no `app_core.scenario` table; every `scenario_id` column is a plain uuid with no FK, deliberately (`r016_indicator_layer.py:7-8`, `r030_scenario_override.py:5-6`).
- **Override** — one attribute edit, in `curated_geo.scenario_override` (r030). The override rows *are* the scenario.
- **Run** — an execution submitted through the `Orchestrator` port. Defined in `JOB_LAYER.md`.
- **Indicator result** — the output, in `curated_geo.indicator_result` with `scenario_id` set (see `INDICATORS_LAYER.md`).

**The baseline is never touched.** An override is a *read-time overlay resolved in SQL*, not an edit to `feature` or `observation` and not an in-memory merge inside a worker. The effective value of an attribute is `override ⊕ current observation ⊕ base feature.attributes`, first non-null wins (`scenarios/overrides.py:50`).

**The same rule applies on the way out** (r043). A scenario stores only what it
changed, so an indicator it did not recompute has no row under it. Reads
therefore resolve as an overlay too: *this scenario's row where it has one, the
baseline's otherwise.* Before that, a `floors` override dirtied one indicator, a
run materialised 1 of 14, and the other 13 were simply missing under that
scenario — holes on the map, and `compute_composite` intersecting its components
to nothing and writing zero rows.

Six read paths carry it, plus the tile: `list_current_for_indicator`,
`get_current`, `rollup_child_rows`, `fetch_aoi_children`,
`list_current_with_geometry`, `value_stats`, and
`serving_geo.indicator_overlay_tile`. The predicate is
`storage/_utils.scenario_overlay`.

⚠ **An anti-join, not `scenario_id = X OR scenario_id IS NULL`.** The widened
form looks equivalent and silently double-counts: where a scenario *has*
recomputed a feature, both rows match, so an aggregate averages it twice and a
tile emits two features with the same id. A baseline row qualifies only when no
scenario row exists for that `(feature, indicator)`, so exactly one row survives
per pair — and the predicate drops into a `WHERE`, a `JOIN … ON` or an aggregate
unchanged.

⚠ **A scenario row shadows the baseline only when it carries a value.** Keying
on row existence alone contradicts the `COALESCE` rule above:
`effective_numeric` resolves `COALESCE(override, observation, base)`, and
COALESCE skips a NULL override rather than letting it win.
`write_result(value_numeric=None, quality_flag="missing")` is a supported call,
so valueless current rows exist by design — and without the value test one of
them suppresses the baseline *and* is itself dropped by the readers' own
`value_numeric IS NOT NULL` filters, so the feature disappears from `value_stats`
and from every composite that consumes it rather than falling back.

The trade-off is deliberate: **a scenario cannot express "recomputed, and the
answer is now unknown" and have that beat the baseline.** Doing so needs a
distinct signal the read layer honours — a `quality_flag` it inspects — not the
absence of a value, which is indistinguishable from "not computed".

⚠ **Writes stay exact-match.** Supersession is not a fallback question:
`_mark_existing_current_stale` and the write path key on `scenario_id` exactly.
So does the indicator picker (`list_indicators(with_values_for_aoi=)`), which is
baseline-only by design.

⚠ The previous version of this document described this layer in terms of `app_core.intervention`, `app_core.intervention_feature.attribute_overrides` and `app_core.configuration`. None of those tables exist and none were ever created — `app_core` contains only `project` (r001:27). The mechanism is `curated_geo.scenario_override`.

---

## Tables

### app_core.project

Top-level workspace. Groups areas of interest.

```sql
-- r001_schemas_and_extensions.py:27-35
CREATE TABLE IF NOT EXISTS app_core.project (
  id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  name        text        NOT NULL,
  description text,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);
```

It exists because `curated_geo.area_of_interest.project_id` references it (`r004_aoi_tables.py:21`). Nothing else in the schema depends on it today.

---

### The scenario id — no table

A `scenario_id` is minted by the caller and carried on the rows that need it. `curated_geo.scenario_override`, `curated_geo.indicator_result`, `jobs_runtime.job_run` and `curated_geo.aoi_indicator_summary` all take a plain `uuid` with no foreign key.

This is a recorded decision, not an oversight: `app_core.scenario` was deferred to a later layer and the FK was to be added together with the table (`r016_indicator_layer.py:7-10`). r030 matched the same convention rather than inventing a different one (`r030:5-6`). `NULL` means baseline — the city as it is today.

Two consequences worth stating:

- Nothing enforces that a `scenario_id` refers to anything. A typo'd uuid produces a valid, empty scenario.
- There is no scenario name, status, or project link in the database. `app_core.project` and a scenario are not connected by any column.

---

### curated_geo.scenario_override

One attribute edit for one feature under one scenario. Keyed like `observation`'s one-current-per-`(feature, attribute)` shape.

```sql
-- r030_scenario_override.py:27-46
CREATE TABLE curated_geo.scenario_override (
  id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  scenario_id   uuid        NOT NULL,
  feature_id    uuid        NOT NULL
                            REFERENCES curated_geo.feature(id) ON DELETE CASCADE,
  attribute_key text        NOT NULL,
  value_numeric double precision,
  value_text    text,
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (scenario_id, feature_id, attribute_key),
  CHECK (num_nonnulls(value_numeric, value_text) = 1)
);

CREATE INDEX scenario_override_lookup_idx
  ON curated_geo.scenario_override (scenario_id, attribute_key);
```

The `CHECK` makes an override exactly one typed value — never both, never neither. The lookup index serves the change-set read below, which filters on `scenario_id` and returns `attribute_key`.

There is no `parameters` / model-configuration table. A scenario expresses itself entirely as per-feature attribute edits; model constants live in code.

---

### jobs_runtime.job_run

Defined in `JOB_LAYER.md`. Note that it is **not** created at submit time by this layer — the runner opens it when the compute starts (`jobs/_lifecycle.py:46`).

---

## How a scenario edit becomes recomputed results

```
1. Record the edit
     apply_override(conn, scenario_id=…, feature_id=…,
                    attribute_key="floors", value_numeric=10.0)   overrides.py:25
     → INSERT … ON CONFLICT (scenario_id, feature_id, attribute_key) DO UPDATE

2. Submit a run
     orchestrator.submit_run(conn, changed_attributes=…, edited_features=…,
                             scenario_id=…)                        JOB_LAYER.md

3. Resolve the change-set                                          plan.py:70-72
     resolve_change_set(...)                                       overrides.py:113
     -- Explicit lists win. When only a scenario_id is given, the diff is read
     -- back from that scenario's override rows — the rows are the diff, so a
     -- caller need not re-declare what changed (overrides.py:96-101).

4. Derive the dirty-set and run it                                 JOB_LAYER.md
     -- Every indicator whose declared `reads` intersects the changed
     -- attributes, plus everything transitively depending on it.

5. Each runner reads effective values, not baseline values
     effective_numeric(conn, type_id=…, attribute_key="floors",
                       scenario_id=…, feature_ids=…)               overrides.py:50

6. Results are written to curated_geo.indicator_result with the run's scenario_id
```

Step 3 is why an override edits `attribute_key` rather than some scenario-specific vocabulary: the key *is* the `changed_attributes` token the impact engine keys the dirty-set off, so no translation layer is needed (`overrides.py:9-10`).

**Attribute scoping is exact; feature scoping is not.** A scenario submit that names nothing goes through the asset graph, where there is one override asset per overridable attribute (`assets/registry.py`, `scenario_override_assets`). Editing `floors` publishes a version for `scenario.override.floors` alone, so only the indicators declaring an edge to `floors` — and whatever is downstream of them — are recomputed. Editing an attribute no indicator reads recomputes nothing.

Two paths remain a flat union and are honest about it. `resolve_change_set` (`overrides.py`) returns the union of `(attributes, features)` and is what runs when a caller passes an explicit change-set, or on a database with no asset graph. And *feature* scoping is whole-indicator on every path: an edit to one block invalidates that indicator everywhere it applies. Narrowing that is `impact.FeatureExpander`'s axis and is still unimplemented.

---

## Effective attribute resolution

The overlay is one `COALESCE` over three sources, evaluated in the database (`overrides.py:74-90`):

```sql
SELECT f.id::text,
       COALESCE(
         so.value_numeric,                                          -- scenario override
         obs.value_numeric,                                         -- current observation
         NULLIF(f.attributes->>%(key)s, '')::double precision       -- base attribute
       )
FROM curated_geo.feature f
LEFT JOIN curated_geo.observation obs
       ON obs.feature_id = f.id AND obs.attribute_key = %(key)s AND obs.is_current
LEFT JOIN curated_geo.scenario_override so
       ON so.feature_id = f.id AND so.attribute_key = %(key)s
      AND so.scenario_id = %(scenario)s
WHERE f.type_id = %(type_id)s
```

With `scenario_id = NULL` the override join matches nothing and the expression collapses to `observation ⊕ base` — the baseline. The same function therefore serves both modes, which is what makes a base/scenario comparison an apples-to-apples one.

Only features whose effective value is non-null are returned (`overrides.py:60`, `:93`). Postgres only — the function rejects any other backend (`overrides.py:20-22`).

`compute_planned_floors` is the worked example: it reads the effective `floors` per block through this function (`indicators/planned_floors.py:30`, `:42-48`) and writes the result inside a `job_run` context (`:50`).

---

## How baseline data participates in scenarios

Scenarios do not replace the baseline city. They sit on top of it. Three kinds of information participate in a run:

| Data role | Where it lives | How scenarios use it |
|---|---|---|
| City-defining baseline | `curated_geo.feature.attributes` | The stable objects, geometries, and base attributes every indicator starts from |
| Baseline enrichments | `curated_geo.observation` | Read-only evidence — population, NDVI, sidewalk width, footfall — overriding the base attribute when current |
| Scenario edits | `curated_geo.scenario_override` | Overrides a selected `(feature, attribute)` for one scenario, ranking above both |

This produces one rule:

- baseline `feature` and `observation` rows are never edited by a scenario,
- only the resolved effective value differs during computation,
- outputs are written as new `indicator_result` rows carrying that `scenario_id`.

Examples:

- A scenario may override `floors` for a block without rewriting the block's baseline attributes.
- A scenario may still use census population or NDVI observations as read-only evidence while recomputing.

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

### Recording a scenario edit

A scenario begins existing the moment its first override row is written. Mint a uuid client-side and use it.

```python
from uuid import uuid4
from reimagina.scenarios.overrides import apply_override

scenario_id = str(uuid4())
apply_override(
    conn,
    scenario_id=scenario_id,
    feature_id=block_id,
    attribute_key="floors",
    value_numeric=10.0,
)
```

`apply_override` upserts on `(scenario_id, feature_id, attribute_key)`, so re-recording an edit replaces it rather than failing (`overrides.py:42-44`).

No CLI command or API route writes override rows today, so nothing a user can reach authors an edit. The write surface itself exists and is exercised: `apply_override` (`overrides.py:26`), `apply_overrides` (`:62`) and `publish_scenario_version` (`:85`) are called from four integration tests — `test_scenario_whatif.py`, `test_edificability_pipeline.py`, `test_asset_publish.py` and `test_freshness_driven_recompute.py`. Everything downstream of the table — change-set derivation, the overlay, the run — is wired. What is missing is an HTTP or CLI route into it.

---

### Triggering scenario computation

Submit a run; the change-set is derived from the override rows when it is not given explicitly.

```bash
reimagina evaluate-scenario --scenario-id "$SCENARIO_ID" --changed-attribute floors
```

The CLI's flags are `--changed-attribute` (repeatable), `--feature` (repeatable), `--scenario-id`, `--aoi-id`, plus the mode flags `--detach` / `--sync` (`apps/cli/app.py:651-700`).

Over HTTP:

```http
POST /jobs
{"scenario_id": "…", "changed_attributes": ["floors"]}
→ 202 {"run_id": "…", "status": "queued"}
```

Omitting `changed_attributes` and `edited_features` is the supported shape when a `scenario_id` is given — the override rows are the diff (`apps/api/routers/jobs.py:40-41`).

A run whose dirty-set is empty comes back as `{"run_id": null, "status": "noop"}`: nothing was invalidated, no run row exists, and an id would 404 (`jobs.py:53-56`).

See `JOB_LAYER.md` for run modes, run state, and cancellation.

---

### Listing a scenario's overrides

```sql
SELECT
  so.feature_id,
  f.name,
  f.type_slug,
  so.attribute_key,
  so.value_numeric,
  so.value_text
FROM curated_geo.scenario_override so
JOIN serving_geo.feature_v f ON f.id = so.feature_id
WHERE so.scenario_id = :scenario_id
ORDER BY f.type_slug, so.attribute_key, so.feature_id;
```

The same read, reduced to `(changed_attributes, edited_features)`, is what `scenario_change_set` returns for the planner (`overrides.py:96`).

---

### Checking run status for a scenario

See `JOB_LAYER.md` — *The port* (`get_run`), *Progress* (the SSE stream), and *Cancellation*. Status is per run and per unit; there is no per-model-family status to poll.

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

`current_indicator_v` filters to `status = 'current'` (`r016:327-350`), so this returns the live result set for that scenario and nothing superseded.

---

### Comparing a scenario against the baseline

Baseline results are the rows with `scenario_id IS NULL`, written by the same indicator code through the same overlay.

```sql
SELECT
  f.id,
  f.name,
  base.value_numeric                        AS baseline_value,
  scen.value_numeric                        AS scenario_value,
  scen.value_numeric - base.value_numeric   AS delta
FROM serving_geo.feature_v f
JOIN serving_geo.current_indicator_v base
  ON base.feature_id     = f.id
 AND base.indicator_slug = 'urban_form_block_planned_floors'
 AND base.scenario_id IS NULL
JOIN serving_geo.current_indicator_v scen
  ON scen.feature_id     = f.id
 AND scen.indicator_slug = 'urban_form_block_planned_floors'
 AND scen.scenario_id    = :scenario_id
WHERE f.type_slug = 'block';
```

Swap `base.scenario_id IS NULL` for a second `= :scenario_b_id` to compare two scenarios. The shipped slugs are `urban_form_block_compactness`, `urban_form_block_building_density`, `urban_form_block_planned_floors`, and `urban_form_block_composite` (`indicators/catalog.py:47`, `:73`, `:98`, `:121`).

---

### Updating an override and recomputing

```sql
INSERT INTO curated_geo.scenario_override
  (scenario_id, feature_id, attribute_key, value_numeric)
VALUES (:scenario_id, :block_id, 'floors', 12.0)
ON CONFLICT (scenario_id, feature_id, attribute_key)
DO UPDATE SET value_numeric = EXCLUDED.value_numeric;
```

Then submit a run for the scenario. There is no separate "mark stale" step to perform: the write path stales the previous `current` row for the same `(feature, scenario, indicator)` as it inserts, and the partial unique index `ir_one_current_scenario` enforces one current row per triple (`r016:198-200`).

---

### Duplicating a scenario

A scenario is its override rows, so cloning one is a single copy.

```sql
INSERT INTO curated_geo.scenario_override
  (scenario_id, feature_id, attribute_key, value_numeric, value_text)
SELECT :new_scenario_id, feature_id, attribute_key, value_numeric, value_text
FROM curated_geo.scenario_override
WHERE scenario_id = :source_scenario_id;
```

The copy has no results until a run is submitted for the new id.

There is no archive operation: with no scenario row there is no status to set. Results for an unused scenario simply stop being recomputed, and stay readable for comparison.

---

## Build order

Within this layer:

1. `app_core.project` — r001, needed by `area_of_interest`
2. `curated_geo.feature` — DATA_LAYER (r003); `scenario_override.feature_id` references it
3. `curated_geo.scenario_override` — r030

`scenario_override` is created late (r030) rather than with `project`, because it depends on `feature` and nothing depends on it. See `JOB_LAYER.md` — *Migration order* for how the job-layer tables interleave.

---

## How the layers relate

```
DATA_LAYER                      SCENARIOS_LAYER            JOB_LAYER              INDICATORS_LAYER

curated_geo.feature ─────────┐
curated_geo.observation ─────┼──► scenario_override ──► submit_run ──► job_run ──► indicator_result
        │                    │            │                 │                       (scenario_id NULL
        │                    └────────────┴── COALESCE ──────┤                        or a uuid)
        └──► serving_geo.feature_v                           └── runner resolves effective values
                (map/API reads)                                  writes results citing job_run_id
```

`feature` is the shared anchor. Scenarios contribute only override rows. A run resolves the three-way overlay in SQL at read time and writes its effects to `indicator_result`. The baseline is never modified.

Note the two distinct read paths: the API and tiles read features through `serving_geo.feature_v`, which merges base attributes with current observations (`r027_feature_centroid_index.py:82-101`) but knows nothing about scenarios. Indicator computation does not go through that view — it resolves `override ⊕ observation ⊕ base` itself (`effective_numeric`, `overrides.py:169-181`).
