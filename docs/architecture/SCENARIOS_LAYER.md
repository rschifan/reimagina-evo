# Scenarios Layer

How the platform records a proposed change to the city, and how that record turns into recomputed indicator results.

**Related documents:** `DATA_LAYER.md` — the city data scenarios read from (never modified). `INDICATORS_LAYER.md` — the computed outputs scenarios produce. `JOB_LAYER.md` — the `Orchestrator` port that executes a scenario run.

---

## What this layer does

A scenario is a named "what if" — a proposed state of the city that has not happened yet. It overlays changes on the baseline city without ever modifying it.

The workflow is:

```
project  →  scenario  →  scenario_edit (drawn areas)  →  scenario_override rows  →  submit_run  →  indicator_result
```

- **Project** — `app_core.project`, the workspace an area of interest and its scenarios belong to (`r001_schemas_and_extensions.py:19-35`).
- **Scenario** — a named row in `app_core.scenario` (r048), unique by name within its project. It is the addressed resource: created, listed, shown, recomputed and deleted by id. The `scenario_id` columns on `scenario_override`, `indicator_result`, `job_run` and `aoi_indicator_summary` are still plain uuids with no FK (`r016_indicator_layer.py:7-8`, `r030_scenario_override.py:5-6`), so what ties them to the scenario row is code, not schema — see *The scenario* below.
- **Edit** — one drawn area carrying one attribute value, in `curated_geo.scenario_edit` (r048). This is what a person authors.
- **Override** — one per-feature attribute edit, in `curated_geo.scenario_override` (r030). Materialising a scenario expands its edits into override rows, and those rows are what the indicators read.
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

**The difference, on the same tile function** (r049).
`serving_geo.indicator_overlay_tile` takes an optional `delta` parameter and
then emits `delta` and `state` per feature instead of `value_numeric` and
`quality_flag`. The classification is `scenarios/comparison.py`'s, not a second
opinion: the effective value is `COALESCE(scenario, baseline)` resolved before
anything is classified, a feature with no baseline is `new` and carries no
delta, and both sides require a numeric value — so a map and the comparison
endpoint cannot report different counts for the same scenario. `delta` with no
scenario is an empty tile rather than the baseline's own values, which would
read as "my scenario did nothing".

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

⚠ **The value test belongs on both arms of the anti-join.** Putting it on the
probe alone — which is what r043 did, and what `scenario_overlay` still does —
stops a valueless scenario row from *suppressing* the baseline but leaves
`scenario_id = X` matching that row unconditionally. Both rows then qualify, so
the one-row-per-pair property above is broken: the double-count the anti-join
exists to prevent, in the one case the probe was taught to ignore. It is harder
to see than the widened form, because it needs a scenario to have recorded a
valueless row, and because every reader keyed by feature — a dict comprehension,
`dict(row)`, `fetchone()`, `ST_AsMVT`'s client — collapses the pair to whichever
row arrived *last*. That order follows the query plan, so it reads as an
intermittent wrong value rather than as a duplicate.

A scenario row therefore qualifies when it carries a value **or** when the
baseline has no row for the pair. The second half is load-bearing: excluding
valueless rows outright would drop a `missing` row for a feature the baseline
never computed out of both arms and the feature would vanish — the hole this
predicate closes, one configuration over. Exactly one row survives per pair in
all four configurations: a valued scenario row wins, a valueless one yields to
the baseline, a valueless one with no baseline is still returned, and no
scenario row leaves the baseline.

`serving_geo.indicator_overlay_tile` resolves it this way as of r049
(`_OVERLAY`; `downgrade` still installs r043's one-sided `_OVERLAY_R043`).
`storage._utils.scenario_overlay` — and with it the six Python read paths — is
still one-sided here and fixed in a separate change, so until that lands the
tile and those readers disagree on exactly this case.

⚠ **Writes stay exact-match.** Supersession is not a fallback question:
`_mark_existing_current_stale` and the write path key on `scenario_id` exactly.

⚠ **The indicator picker is the one read that uses the widened form, because
it emits no rows.** `list_indicators(with_values_for_aoi=, scenario_id=)` was
baseline-only, which hid every indicator that exists only inside a scenario —
the whole edificability family — from the picker that a scenario was authored
to drive. It now counts the named scenario's rows *and* the baseline's, written
as `(ir.scenario_id = %s OR ir.scenario_id IS NULL)` inside its `EXISTS` probe
over `indicator_catalog`.

That is not an exception to the rule above; it is the rule's boundary. The
anti-join exists because a read that *returns* rows must not match both a
feature's scenario row and its baseline row. This predicate sits inside an
`EXISTS`: it emits nothing and answers "is there at least one", so one matching
row and two are the same answer and double-counting is unrepresentable. The
anti-join would add a correlated `NOT EXISTS` per catalog row for an identical
result — measured at 34× the planner's cost on the dev dataset for the same 12
slugs. Both halves of the `OR` are load-bearing: baseline-only is the bug
above, and scenario-only would drop every indicator the scenario did not
recompute, each of which still renders through to its baseline value by the
overlay rule.

`scenario_id` is a *filter* here and not the resource being addressed, so a
well-formed id naming no scenario is answered with the baseline set — which is
also what would render — rather than refused. A malformed one is rejected at
the API boundary as 422, typed `UUID`, so it never reaches the `uuid` cast.
Pinned in `tests/integration/test_indicator_picker_scenario.py`, whose
`test_an_indicator_is_offered_exactly_once_under_a_scenario` gives one feature
both rows — the state a join would duplicate.

⚠ The previous version of this document described this layer in terms of `app_core.intervention`, `app_core.intervention_feature.attribute_overrides` and `app_core.configuration`. None of those tables exist and none were ever created. The mechanism is `curated_geo.scenario_edit` expanded into `curated_geo.scenario_override`, with `app_core.scenario` naming the whole.

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

It exists because `curated_geo.area_of_interest.project_id` references it (`r004_aoi_tables.py:21`), and since r048 `app_core.scenario.project_id` does too.

---

### app_core.scenario

A named what-if inside a project (r048). `project_id` is required and `aoi_id` is not: a scenario may be created before any AOI exists, so the project cannot always be derived from the AOI. `UNIQUE (project_id, name)` is what lets a scenario be addressed by name. `materialized_at` is null until the drawn edits have first been expanded into overrides. Row-level security is enabled on this table, keyed on `project_id`.

### curated_geo.scenario_edit

One drawn area carrying one attribute value, ordered by `ordinal` ("later edits win"; not `created_at`, because `now()` is transaction-start time). The geometry CHECK is a type check only. One proposal (levels, unit size, people per household) writes four edits. Deleting the scenario cascades them.

### The scenario id — what the schema does not tie together

`curated_geo.scenario_override`, `curated_geo.indicator_result`, `jobs_runtime.job_run` and `curated_geo.aoi_indicator_summary` still take a plain `uuid` with no foreign key: r016 and r030 chose that while the entity was deferred, and r048 added the entity without adding the FKs. `NULL` means baseline — the city as it is today.

Consequences worth stating:

- Nothing in the schema enforces that a `scenario_id` on those four tables refers to a scenario row. A typo'd uuid produces a valid, empty scenario, and the low-level `apply_override` path (below) can still write overrides for an id that has no row.
- ⚠ **A scenario's AOI is tied to its project by code, not by the schema.** r048 declares `aoi_id` as a plain reference to `curated_geo.area_of_interest(id)`, so the database accepts a scenario in project A bounded by project B's AOI. Materialisation scopes its selection by `aoi_id` alone, so such a scenario would write overrides over B's features. `create_scenario` is the column's only writer and refuses the pair (`_require_aoi_in_project`); a new insert path has to repeat that check.
- Because those four tables have no FK, deleting a scenario does not cascade into them. `delete_scenario` retires the traces by hand: it removes the scenario's overrides, retires its asset versions and materialisations, and marks its `current` results `stale`. Only then does it delete the row, which cascades the edits.

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

### Authoring a scenario

Create the scenario, draw an area with the three policy inputs, then recompute:

```bash
python -m apps.cli.app scenario create --name "Upzoning" --aoi-id <uuid>
python -m apps.cli.app scenario set-attribute --scenario-id <uuid> \
    --geojson area.geojson --levels 12 --unit-size 80 --people-per-household 3.2
python -m apps.cli.app scenario recompute --scenario-id <uuid> [--sync | --detach]
python -m apps.cli.app scenario compare --scenario-id <uuid> \
    --slug urban_form_block_planned_floors
python -m apps.cli.app scenario list      # reads explore.scenario_v
python -m apps.cli.app scenario show --scenario-id <uuid>
```

`recompute` materialises the edits into override rows (selecting blocks with `ST_Covers(area, geometry_centroid)`) and then runs the selective recompute. `scripts/acceptance_run.sh` step 10 exercises exactly this path.

---

### Recording a scenario edit directly

The low-level path writes override rows without a scenario row. It remains for code that already holds a scenario id; the authoring path above is the one that creates a scenario others can find by name.

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

Three surfaces author an edit, and none of them owns a rule. `apps/cli/scenario.py` at a terminal, `apps/api/routers/scenarios.py` over HTTP, and `apps/web/src/scenario.ts` on the map all call `reimagina.scenarios.proposal`, which is where the four edits one proposal writes, the refusal of a degenerate number and the density precheck live — so the three refuse the same things for the same reasons. A surface adds exactly two things of its own: the dialect of its refusals (`--levels` on a command line, `levels` in a request body, a highlighted input on a form) and, on the map, the one check a form can make before the round trip.

**The map's precheck is a saved round trip, not a guarantee**, and `scenario.ts` says so next to the inputs. Only the density ceiling is independent of block area, so it is the only one checkable before a block is chosen; the housing-unit and population ceilings are the server's, per block, at materialisation. The bound itself is read from `GET /indicators/edificability_block_density` rather than compiled into the frontend, because `ceilings._bound_for` reads it from the same catalog — a second copy would agree until the day the catalog moved.

**The map carries the scenario in one place and four reads.** `scenario_id` goes on the indicator list (without it the picker hides the five edificability indicators, which have no baseline rows at all), the overlay's tile URL, the ramp's stats and the download URLs — and nowhere else, because the overlay is resolved in SQL (r043) rather than in the client. ⚠ The baseline's tile URL is byte-identical to the one that existed before scenarios: MapLibre caches tiles by URL, so a parameter added to the baseline invalidates every cached tile of every indicator on every AOI.

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

The copy has no results until a run is submitted for the new id. A copied id with no `app_core.scenario` row is invisible to `explore.scenario_v`; cloning a *named* scenario means creating the row first.

There is no archive operation and no status column. Deleting a scenario retires its traces (see *The scenario id* above): its overrides are removed and its results are marked `stale`, so `current_indicator_v` no longer returns them. The stale rows remain in `indicator_result` as history, but they are no longer readable as the scenario's results or for comparison.

---

## Build order

Within this layer:

1. `app_core.project` — r001, needed by `area_of_interest`
2. `curated_geo.feature` — DATA_LAYER (r003); `scenario_override.feature_id` references it
3. `curated_geo.scenario_override` — r030
4. `app_core.scenario` and `curated_geo.scenario_edit` — r048

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
